"""gta-hints@8 = @7 plus a street x numbered-highway intersection pass. SYNTHETIC text only: invented
phrasing around public road names (the module's own word lists). @6/@7 behaviour is pinned elsewhere."""

import pytest

from listing_lens.extractors.gta_hints import LEX6, GtaHints, hwy_intersection8, pick
from listing_lens.extractors.gta_hints import ROADWORDS as RW


def test_name_and_registration():
    from listing_lens.extractors.gta_hints import GtaHints8

    assert GtaHints8.name == "gta-hints@8"


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ("Located : Brannock / HWY501", "Brannock / HWY501"),  # upper-case highway
        ("near HWY7 AND CORVEL in town", "HWY7 AND CORVEL"),  # highway first, a trailing word
        ("HWY7/CORVEL_promo today", "HWY7/CORVEL"),  # a glued "_" word
        ("Upscale Incall Companion Brannock & 501", "Brannock & 501"),  # ad words before the street
        ("vexie/501", "vexie/501"),  # lower-case street
        ("vexie & 501 tonight", "vexie & 501"),
        ("501/vexie", "501/vexie"),
        ("Hwy 27 and Dorran Rd", "Hwy 27 and Dorran Rd"),  # a suffix stays in the phrase
        ("by the XPW & Tessaline", "XPW & Tessaline"),
    ],
)
def test_street_by_numbered_highway(line, want):
    assert hwy_intersection8(line, RW) == want


@pytest.mark.parametrize(
    "line",
    [
        "call 537.217.5899 anytime",  # a phone, not highway 537
        "special $500 / Brannock deal",  # a price
        "501 roses and a smile",  # no street leg
        "Juniper & 501",  # not a known street word
        "highway & 501",  # "highway" is not a street leg
        "Brannock & 5012",  # not a 5xx highway
    ],
)
def test_not_a_highway_intersection(line):
    assert hwy_intersection8(line, RW) is None


def test_at6_and_at7_lexicons_do_not_run_the_pass():
    text = "Synthetic\nvexie/501"
    assert LEX6.hwy_intersection is None
    assert pick(text, LEX6) is None  # @6 output unchanged
    assert GtaHints().extract(text) is None


def test_the_pass_runs_after_at7s_intersection_on_the_same_line_and_before_later_kinds():
    from dataclasses import replace

    lex8 = replace(LEX6, hwy_intersection=hwy_intersection8)
    assert pick("Synthetic\nQuill & Fenwick\nBrannock & 501", lex8) == ("intersection", "Quill & Fenwick")
    assert pick("Synthetic\nvexie/501\nnear Example Plaza", lex8) == ("intersection", "vexie/501")
    assert pick("Synthetic\nB2B 2B2\nvexie/501", lex8)[0] == "postal"  # a stronger kind still wins


# ── @8.1: the highway pass only when the base finds nothing in the WHOLE ad ─────────────────────────


def test_at81_never_replaces_a_base_hint():
    from dataclasses import replace

    from listing_lens.extractors.gta_hints import pick81

    text = "Synthetic\nvexie/501\nQuill & Fenwick"  # the highway line comes FIRST, the base hit is later
    lex8 = replace(LEX6, hwy_intersection=hwy_intersection8)
    assert pick(text, lex8) == ("intersection", "vexie/501")  # @8 replaced the base hint (the bug)
    assert pick81(text, LEX6) == ("intersection", "Quill & Fenwick")  # @8.1 keeps it
    # the same intersection twice: the base's own occurrence is kept, byte-identical
    twice = "Synthetic\nHWY501/BRANNOCK_promo\nnear 501 / Brannock tonight"
    assert pick81(twice, LEX6) == pick(twice, LEX6)


def test_at81_adds_a_highway_hint_only_where_the_base_has_none():
    from listing_lens.extractors.gta_hints import pick81

    assert pick("Synthetic\nLocated : Brannock / HWY501", LEX6) is None
    assert pick81("Synthetic\nLocated : Brannock / HWY501", LEX6) == ("intersection", "Brannock / HWY501")
    assert pick81("Synthetic\nnothing to see", LEX6) is None


def test_at81_name():
    from listing_lens.extractors.gta_hints import GtaHints81

    assert GtaHints81.name == "gta-hints@8.1"


# ── @8.2: "lofty demand" never makes a label a road ───────────────────────────────────────────────────


def _lex_with_word(noise):
    from dataclasses import replace

    return replace(LEX6, roadwords=LEX6.roadwords | {"lofty"}, label_noise=noise)  # @7's lexicon has "lofty"


def test_at82_drops_the_high_demand_label_template():
    from listing_lens.extractors.gta_hints import LABEL_NOISE82

    junk = "Synthetic\npopular girl in the area only by appointment cuz of lofty demand."
    assert pick(junk, _lex_with_word(None)) == ("label", "only by appointment cuz of lofty demand")  # @7/@8.1
    assert pick(junk, _lex_with_word(LABEL_NOISE82)) is None  # @8.2


def test_at82_keeps_a_real_location_beside_appointment():
    from listing_lens.extractors.gta_hints import LABEL_NOISE82

    lex = _lex_with_word(LABEL_NOISE82)
    assert pick("Synthetic\nlocation: by appointment at Quill & Ashgrove", lex) == (
        "label",
        "by appointment at Quill & Ashgrove",
    )
    assert pick("Synthetic\narea: Lofty Glade, lofty demand", lex) == ("label", "Lofty Glade, lofty demand")


def test_at82_name():
    from listing_lens.extractors.gta_hints import GtaHints82

    assert GtaHints82.name == "gta-hints@8.2"


def test_the_live_extractor_default_is_at83():
    """The operator's switches (2026-11-13): extract/geocode/derive default to gta-hints@8.3 (was @8.2, @6). Read from the
    module source, because conftest points the pipeline tests at @6."""
    import inspect

    from listing_lens import commands

    assert 'DEFAULT_EXTRACTOR = "gta-hints@8.3"' in inspect.getsource(commands)
    import tomllib
    from pathlib import Path

    toml = tomllib.loads((Path(__file__).resolve().parents[1] / "pipeline.toml").read_text())
    assert toml["plugins"]["extractors"]["gta-hints@8.3"].endswith(":GtaHints83")  # it resolves to @8.3


def test_at82_keeps_high_as_a_real_street_word_in_a_label():
    from listing_lens.extractors.gta_hints import LABEL_NOISE82

    lex = _lex_with_word(LABEL_NOISE82)
    assert pick("Synthetic\nlocation: Lofty St near the park", lex) == ("label", "Lofty St near the park")
    assert pick("Synthetic\nlocated in Lofty Glade", lex) == ("label", "Lofty Glade")


# ── @8.3: a bare "lofty" is never a label's road word ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "popular girl in the area only by appointment cuz of lofty demand.",
        "located in town for my amazing personality and lofty rate of repeat clients",
        "area: lofty class companion",
    ],
)
def test_at83_bare_high_is_not_a_label_road(line):
    from listing_lens.extractors.gta_hints import LABEL_NOISE83

    assert pick("Synthetic\n" + line, _lex_with_word(None))[0] == "label"  # @7/@8.1 took it
    assert pick("Synthetic\n" + line, _lex_with_word(LABEL_NOISE83)) is None


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ("location: Lofty St near the park", "Lofty St near the park"),
        ("located in Lofty Glade", "Lofty Glade"),
        ("location: lofty street west", "lofty street west"),
        ("location: by appointment at Quill & Ashgrove", "by appointment at Quill & Ashgrove"),
    ],
)
def test_at83_keeps_real_high_streets_and_other_labels(line, want):
    from listing_lens.extractors.gta_hints import LABEL_NOISE83

    assert pick("Synthetic\n" + line, _lex_with_word(LABEL_NOISE83)) == ("label", want)


def test_at83_name():
    from listing_lens.extractors.gta_hints import GtaHints83

    assert GtaHints83.name == "gta-hints@8.3"
