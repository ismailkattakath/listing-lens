"""services_match on SYNTHETIC text and the synthetic SKOS fixture (made-up concepts alpha/beta only)."""

import pytest
from conftest import FIXTURES

from listing_lens import services_match as sm


@pytest.fixture(scope="module")
def vocab():
    return sm.load_vocab(FIXTURES / "services.synthetic.ttl")


def found(text, vocab):
    return [(m["concept"], m["label_matched"], m["stance"]) for m in sm.match(text, vocab)]


def test_version_and_refusal_of_a_non_vocabulary(tmp_path):
    g = sm.load_vocab(FIXTURES / "services.synthetic.ttl")
    assert sm.vocab_version(g) == "0.0.0-test"
    bad = tmp_path / "x.ttl"
    bad.write_text("@prefix ex: <urn:x:> . ex:a ex:b ex:c .")
    with pytest.raises(ValueError):
        sm.load_vocab(bad)


def test_pref_alt_and_hidden_labels_match(vocab):
    assert found("synthetic: alpha service here", vocab) == [("alpha", "alpha service", "offered")]
    assert found("synthetic: AS today", vocab) == [("alpha", "AS", "offered")]
    assert found("synthetic: 4lpha service", vocab) == [("alpha", "4lpha service", "offered")]
    assert found("synthetic: beta thing", vocab) == [("beta", "beta thing", "offered")]


def test_offsets_index_the_original_text_even_after_normalisation(vocab):
    text = "x 𝓪𝓵𝓹𝓱𝓪 𝓼𝓮𝓻𝓿𝓲𝓬𝓮 y"  # decorative letters fold via ftfy NFKC; the span stays in the original
    (m,) = sm.match(text, vocab)
    assert m["concept"] == "alpha" and text[m["start"] : m["end"]] == "𝓪𝓵𝓹𝓱𝓪 𝓼𝓮𝓻𝓿𝓲𝓬𝓮"


def test_look_alike_letters_match_via_the_icu_skeleton(vocab):
    cyr = "аlpha service"  # first letter is CYRILLIC a
    assert found(cyr, vocab) == [("alpha", cyr, "offered")]


def test_case_is_ignored(vocab):
    assert found("ALPHA SERVICE", vocab) == [("alpha", "ALPHA SERVICE", "offered")]


def test_one_typo_on_a_long_single_token_label_only(tmp_path):
    ttl = tmp_path / "v.ttl"
    ttl.write_text(
        "@prefix skos: <http://www.w3.org/2004/02/skos/core#> . @prefix svc: <urn:li:vocab:services:> .\n"
        "svc: a skos:ConceptScheme .\n"
        'svc:gamma a skos:Concept ; skos:notation "gamma" ; skos:prefLabel "gammaword"@en ; skos:altLabel "GW"@en .\n'
    )
    g = sm.load_vocab(ttl)
    assert [m["concept"] for m in sm.match("one gamaword here", g)] == ["gamma"]  # 1 edit on a 9-letter label
    assert sm.match("one GX here", g) == []  # short abbreviations never fuzzy-match


def test_negation_refuses_and_pseudo_negation_does_not(vocab):
    assert found("synthetic: no alpha service", vocab)[0][2] == "refused"
    assert found("synthetic: alpha service not available", vocab)[0][2] == "refused"
    assert found("synthetic: ❌ alpha service", vocab)[0][2] == "refused"
    assert found("synthetic: no rush, alpha service", vocab)[0][2] == "offered"
    assert found("synthetic: free alpha service", vocab)[0][2] == "offered"


def test_a_line_break_ends_the_negation_scope(vocab):
    out = found("no beta thing\nalpha service ok", vocab)
    assert ("beta", "beta thing", "refused") in out and ("alpha", "alpha service", "offered") in out


def test_termination_word_ends_the_scope(vocab):
    out = found("no beta thing but alpha service", vocab)
    assert ("beta", "beta thing", "refused") in out and ("alpha", "alpha service", "offered") in out


def test_overlaps_keep_the_longest_span(tmp_path):
    ttl = tmp_path / "v.ttl"
    ttl.write_text(
        "@prefix skos: <http://www.w3.org/2004/02/skos/core#> . @prefix svc: <urn:li:vocab:services:> .\n"
        "svc: a skos:ConceptScheme .\n"
        'svc:a a skos:Concept ; skos:notation "a" ; skos:prefLabel "delta"@en .\n'
        'svc:b a skos:Concept ; skos:notation "b" ; skos:prefLabel "delta epsilon"@en .\n'
    )
    assert [m["concept"] for m in sm.match("one delta epsilon two", sm.load_vocab(ttl))] == ["b"]


def test_empty_and_missing_text(vocab):
    assert sm.match("", vocab) == [] and sm.match(None, vocab) == []


# ── services-match@2: the stock lookup lemma (synthetic labels; the real vocabulary never enters git) ──


def test_v2_method_tag():
    assert sm.METHOD == "services-match@2"


def test_lemma_matches_a_plural_of_a_multi_word_label(vocab):
    assert found("synthetic: alpha services today", vocab) == [("alpha", "alpha services", "offered")]
    assert found("synthetic: beta things here", vocab) == [("beta", "beta things", "offered")]


def test_lemma_works_on_shouted_and_capitalised_text(vocab):
    assert found("NO ALPHA SERVICES", vocab) == [("alpha", "ALPHA SERVICES", "refused")]
    assert found("Beta Things welcome", vocab)[0][:2] == ("beta", "Beta Things")


def test_perturbation_without_the_lemma_the_plurals_do_not_match(monkeypatch):
    """Proves the LEMMA matcher is what finds them: with an identity lemma table they are gone."""

    class Identity:
        def get(self, k, default=None):
            return default

    monkeypatch.setattr(sm, "_lemma_table", lambda: Identity())
    fresh = sm.load_vocab(FIXTURES / "services.synthetic.ttl")  # a new graph: a new pipeline, built patched
    assert found("synthetic: beta things here", fresh) == []
    assert found("NO ALPHA SERVICES", fresh) == []
    assert found("synthetic: alpha service here", fresh) == [
        ("alpha", "alpha service", "offered")
    ]  # exact still


# ── stop scheme <urn:li:vocab:stop:> (analyst's representation): matched, then dropped with what it overlaps ──
# Synthetic words only: "zeta" plays a one-word service label that a multi-word stop phrase contains.

STOP_TTL = """@prefix skos: <http://www.w3.org/2004/02/skos/core#> . @prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix svc: <urn:li:vocab:services:> . @prefix stop: <urn:li:vocab:stop:> .
stop: a skos:ConceptScheme ; owl:versionInfo "9.9.9-stop" .
svc: a skos:ConceptScheme ; owl:versionInfo "0.2.0-test" .
svc:zeta a skos:Concept ; skos:inScheme svc: ; skos:notation "zeta" ; skos:prefLabel "zeta"@en ;
    skos:altLabel "zeta sessions"@en, "zeta show night"@en .
stop:app a skos:Concept ; skos:inScheme stop: ; skos:notation "app" ; skos:prefLabel "omega zeta (an app)"@en ;
    skos:hiddenLabel "omega zeta"@en, "kappa & zeta"@en, "zeta show"@en .
"""


@pytest.fixture
def stopvocab(tmp_path):
    p = tmp_path / "stop.ttl"
    p.write_text(STOP_TTL)
    return sm.load_vocab(p)


@pytest.mark.parametrize(
    "text",
    [
        "OMEGA ZETA SHOW tonight",  # two overlapping stop phrases, then the service word inside them
        "Kappa & Zeta sessions",  # a 3-token stop phrase beats the 2-token service label 'zeta sessions'
        "zeta show night",  # a LONGER service label overlapping a stop phrase still loses (stop always wins)
    ],
)
def test_a_stop_phrase_suppresses_every_service_span_it_overlaps(stopvocab, text):
    assert sm.match(text, stopvocab) == []


def test_the_service_word_alone_still_matches(stopvocab):
    assert found("Zeta available", stopvocab) == [("zeta", "Zeta", "offered")]
    assert found("omega zeta call, then zeta later", stopvocab) == [
        ("zeta", "zeta", "offered")
    ]  # only the 2nd


def test_stop_concepts_never_appear_in_the_output(stopvocab):
    texts = ["omega zeta", "kappa & zeta", "zeta show", "OMEGA ZETAS", "no omega zeta", "zeta"]
    assert not any(
        m["concept"] == "app" or "stop" in m["concept"] for t in texts for m in sm.match(t, stopvocab)
    )


def test_the_version_is_the_service_schemes_never_the_stop_schemes(stopvocab):
    assert sm.vocab_version(stopvocab) == "0.2.0-test"


def test_a_file_with_only_the_stop_scheme_is_not_a_vocabulary(tmp_path):
    p = tmp_path / "only-stop.ttl"
    p.write_text(
        "@prefix skos: <http://www.w3.org/2004/02/skos/core#> . @prefix stop: <urn:li:vocab:stop:> .\n"
        'stop: a skos:ConceptScheme . stop:a a skos:Concept ; skos:inScheme stop: ; skos:prefLabel "x"@en .\n'
    )
    with pytest.raises(ValueError):
        sm.load_vocab(p)
