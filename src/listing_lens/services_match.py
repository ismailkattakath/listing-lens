"""Service mentions in ad text: SKOS vocabulary -> spaCy matching -> negspaCy stance. Libraries do the work.

The vocabulary is the operator's DATA (never in git): ``load_vocab(path)`` reads a SKOS Turtle file with rdflib.
Each ``skos:Concept`` contributes its ``prefLabel`` (full form), ``altLabel`` (abbreviations) and ``hiddenLabel``
(spacing/leet/look-alike/emoji variants, as data, never as code).

Normalisation never rewrites the text, so offsets stay in the ORIGINAL string. Each token's spaCy ``NORM`` is set
to its matching key: ``ftfy.fix_text(NFKC)`` (mojibake, decorative/full-width letters) -> ``emoji.demojize`` (an
emoji becomes its name) -> lower-case -> ICU ``SpoofChecker.getSkeleton`` (Unicode TR39 look-alikes). Labels go
through the same function, so a label written once matches every look-alike. Matching is spaCy ``PhraseMatcher``
on ``NORM`` (exact token sequences), plus ``Matcher`` ``FUZZY1`` on ``LOWER`` for single-token labels of 6+ characters
(one typo; on LOWER because the TR39 skeleton rewrites letters, e.g. m -> rn). Since @2 a third ``PhraseMatcher`` on ``LEMMA`` (spaCy's stock lookup table, keyed on the lower-cased token) catches
inflected forms of a label (plurals, verb forms). Overlaps resolve longest-first (``spacy.util.filter_spans``). Stance comes from negspaCy with the packaged
termset as the WHOLE termset, on sentences cut by spaCy's sentencizer, where line breaks count as boundaries
(ads separate items by lines, not full stops).
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from pathlib import Path

import emoji
import ftfy
import icu
import rdflib
import spacy
from negspacy.negation import Negex  # noqa: F401  (registers the "negex" factory)
from rdflib.namespace import OWL, RDF, SKOS
from spacy.language import Language
from spacy.lookups import load_lookups
from spacy.matcher import Matcher, PhraseMatcher
from spacy.util import filter_spans

from .vocab import is_stop, notation, service_schemes

METHOD = "services-match@2"  # @2: + a LEMMA PhraseMatcher on spaCy's stock lookup table (plurals, verb forms)
LABEL = "SERVICE"
FUZZY_MIN_LEN = 6  # one-edit tolerance only where a typo cannot turn one short abbreviation into another
_SPOOF = icu.SpoofChecker()


def key(text: str) -> str:
    """The matching key for a token or a label (same function both sides)."""
    t = emoji.demojize(ftfy.fix_text(text, normalization="NFKC"), delimiters=("", ""))
    return _SPOOF.getSkeleton(icu.USpoofChecks.ALL_CHECKS, t.lower())


def load_vocab(path: str | Path) -> rdflib.Graph:
    """Parse the operator's SKOS vocabulary. Refuses a file with no ConceptScheme (not a vocabulary)."""
    g = rdflib.Graph().parse(str(path), format="turtle")
    if not service_schemes(g):  # the stop scheme alone is not a vocabulary
        raise ValueError(f"{path}: no service skos:ConceptScheme, not a vocabulary file")
    return g


def vocab_version(graph: rdflib.Graph) -> str:
    scheme = service_schemes(graph)[0]  # never the stop scheme's
    return str(graph.value(scheme, OWL.versionInfo) or "0.0.0")


STOP_KEY = "\x00stop:"  # matcher key prefix of a stop concept: can never be a written notation


def _labels(graph: rdflib.Graph) -> list[tuple[str, str]]:
    """(matcher key, label) for every pref/alt/hidden label of every concept, BOTH schemes. A stop concept's
    key is STOP_KEY + its notation, so match() can tell its spans apart and drop them."""
    out = []
    for c in graph.subjects(RDF.type, SKOS.Concept):
        k = (STOP_KEY if is_stop(graph, c) else "") + notation(graph, c)
        for p in (SKOS.prefLabel, SKOS.altLabel, SKOS.hiddenLabel):
            out += [(k, str(o)) for o in graph.objects(c, p)]
    return sorted(set(out))


def _termset() -> dict:
    t = json.loads(resources.files("listing_lens.vocab").joinpath("negation-termset.json").read_text())
    return {k: v for k, v in t.items() if not k.startswith("_")}


@lru_cache(maxsize=1)
def _lemma_table():
    """spacy-lookups-data's en `lemma_lookup` (the stock table behind spaCy's lookup lemmatizer, mode="lookup")."""
    return load_lookups("en", ["lemma_lookup"]).get_table("lemma_lookup")


@Language.component("li_token_keys")
def _set_keys(doc):
    table = _lemma_table()
    for t in doc:
        t.norm_ = key(t.text)
        # @2: spaCy's stock English lookup lemma, keyed on the LOWERCASE token: the table is case-sensitive
        # ("Calls" is not in it, "calls" is), and ads shout. Labels get the same lemma, so "x calls" meets "x call".
        t.lemma_ = table.get(t.lower_, t.lower_)
    return doc


def _nlp(graph: rdflib.Graph) -> Language:
    nlp = spacy.blank("en")  # tokenizer only; no model download
    nlp.add_pipe("li_token_keys")
    # Line breaks are boundaries; spaCy's own sentencizer, configured with the newline-bearing space tokens.
    nlp.add_pipe("sentencizer", config={"punct_chars": [".", "!", "?", "\n", "\n\n", "\n\n\n"]})
    phrase = PhraseMatcher(nlp.vocab, attr="NORM")
    lemma = PhraseMatcher(nlp.vocab, attr="LEMMA")  # @2: inflected forms of a label
    fuzzy = Matcher(nlp.vocab)
    make = nlp.make_doc
    for mkey, label in _labels(graph):
        d = _set_keys(make(label))
        phrase.add(mkey, [d])
        lemma.add(mkey, [d])
        # FUZZY on LOWER, not NORM: the TR39 skeleton rewrites letters (m -> rn), so edit distances on it are not
        # typo distances. Look-alikes stay the PhraseMatcher's job.
        if len(d) == 1 and len(label) >= FUZZY_MIN_LEN:
            fuzzy.add(mkey, [[{"LOWER": {"FUZZY1": label.lower()}}]])
    nlp.add_pipe("negex", config={"neg_termset": _termset(), "ent_types": [LABEL]})
    nlp.get_pipe("negex")  # fail early if negspaCy did not register
    nlp._li_matchers = (phrase, lemma, fuzzy)  # type: ignore[attr-defined]
    return nlp


@lru_cache(maxsize=4)
def _cached(
    graph_id: int, graph: rdflib.Graph
) -> Language:  # keyed by object identity; a new load = a new pipeline
    return _nlp(graph)


def match(text: str, graph: rdflib.Graph) -> list[dict]:
    """Service mentions in ``text`` with offsets into ``text`` itself. Field-agnostic (the stage adds ``field``)."""
    nlp = _cached(id(graph), graph)
    phrase, lemma, fuzzy = nlp._li_matchers  # type: ignore[attr-defined]
    doc = nlp.make_doc(text or "")
    for name, proc in nlp.pipeline:
        if name == "negex":
            hits = list(phrase(doc)) + list(lemma(doc)) + (list(fuzzy(doc)) if len(fuzzy) else [])
            keys: dict[tuple[int, int], set[str]] = {}
            for m, s, e in hits:
                keys.setdefault((s, e), set()).add(nlp.vocab.strings[m])
            # Stop phrases first (longest among themselves), then they WIN over every service span they overlap,
            # whatever its length; the rest resolve longest-first as before. Stop spans are never emitted.
            stops = filter_spans(
                [doc[s:e] for (s, e), ks in keys.items() if any(k.startswith(STOP_KEY) for k in ks)]
            )
            blocked = {i for sp in stops for i in range(sp.start, sp.end)}
            service = [
                doc[s:e]
                for (s, e), ks in keys.items()
                if any(not k.startswith(STOP_KEY) for k in ks) and not blocked.intersection(range(s, e))
            ]
            ents = []
            for sp in filter_spans(service):
                kb = min(k for k in keys[(sp.start, sp.end)] if not k.startswith(STOP_KEY))
                ent = doc.char_span(sp.start_char, sp.end_char, label=LABEL, kb_id=kb)
                if ent is not None:
                    ents.append(ent)
            doc.ents = ents
        doc = proc(doc)
    return [
        {
            "concept": e.kb_id_,
            "label_matched": e.text,
            "start": e.start_char,
            "end": e.end_char,
            "stance": "refused" if e._.negex else "offered",
        }
        for e in doc.ents
        if e.label_ == LABEL
    ]
