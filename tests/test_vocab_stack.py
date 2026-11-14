"""The service-vocabulary stack is importable from the Nix pins and works offline (synthetic text only)."""

import json
from importlib import resources

from scrape_harness import contracts


def test_a_skos_vocabulary_parses_with_rdflib():
    """The real vocabulary is DATA (outside git); this is a synthetic fixture with made-up concepts."""
    import rdflib
    from conftest import FIXTURES
    from rdflib.namespace import OWL, RDF, SKOS

    g = rdflib.Graph().parse(FIXTURES / "services.synthetic.ttl", format="turtle")
    (scheme,) = g.subjects(RDF.type, SKOS.ConceptScheme)
    assert str(g.value(scheme, OWL.versionInfo)) == "0.0.0-test"
    assert {str(n) for n in g.objects(None, SKOS.notation)} == {"alpha", "beta"}


def test_no_vocabulary_ships_in_the_package():
    names = {p.name for p in resources.files("listing_lens.vocab").iterdir()}
    assert not any(n.endswith(".ttl") for n in names)  # the lexicon is the operator's data, never in git


def test_negspacy_runs_on_a_blank_pipeline_with_the_data_termset_alone():
    """The data file IS the whole termset (no en_clinical base: its "free" would read "free X" as a refusal)."""
    import spacy
    from negspacy.negation import Negex  # noqa: F401  (registers the "negex" factory)

    terms = json.loads(resources.files("listing_lens.vocab").joinpath("negation-termset.json").read_text())
    neg = {k: v for k, v in terms.items() if not k.startswith("_")}
    assert set(neg) == {"pseudo_negations", "preceding_negations", "following_negations", "termination"}
    assert "free" not in {t for v in neg.values() for t in v}
    nlp = spacy.blank("en")  # no model download
    nlp.add_pipe("sentencizer")  # negspaCy scopes negation by sentence; a blank pipeline has no boundaries
    ruler = nlp.add_pipe("entity_ruler")
    ruler.add_patterns([{"label": "SERVICE", "pattern": "example service"}])
    nlp.add_pipe("negex", config={"neg_termset": neg, "ent_types": ["SERVICE"]})
    assert [e._.negex for e in nlp("synthetic text: no example service here").ents] == [True]
    assert [e._.negex for e in nlp("synthetic text: free example service here").ents] == [False]


def test_abbreviation_pairs_and_text_normalisers():
    import emoji
    import ftfy
    import icu
    from abbreviation_extractor import extract_abbreviation_definition_pairs

    pairs = extract_abbreviation_definition_pairs("A synthetic example uses Long Example Term (LET) here.")
    assert any(p.abbreviation == "LET" for p in pairs)
    assert ftfy.fix_text("cafÃ©") == "café"
    assert emoji.demojize("❤") == ":red_heart:"
    assert icu.SpoofChecker().getSkeleton(
        icu.USpoofChecks.ALL_CHECKS, "аbc"
    ) == icu.SpoofChecker().getSkeleton(icu.USpoofChecks.ALL_CHECKS, "abc")


def test_the_services_schema_accepts_a_synthetic_record():
    contracts.validate(
        "services.v1",
        {
            "schema": "services.v1",
            "site": "example.invalid",
            "ad_id": "1000001",
            "fetched_at": "2026-01-15T12:00:00Z",
            "parser": "sample-ad@1",
            "vocab_version": "0.0.0",
            "method": "services@1",
            "online_services_offered": ["synthetic-code"],
            "mentions": [
                {
                    "concept": "example",
                    "label_matched": "example service",
                    "field": "description",
                    "start": 3,
                    "end": 18,
                    "stance": "refused",
                }
            ],
        },
    )
