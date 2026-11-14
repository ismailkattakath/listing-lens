"""gta-hints@5/@6: the misses and wrong phrases of @4 and @5, as SYNTHETIC shapes (no real ad text).

Each case names the miss pattern it pins down. Street words are public geography or placeholders."""

import copy

import pytest
from scrape_harness import contracts

from listing_lens import derive
from listing_lens.commands import TEXT_HINT, area_is_city
from listing_lens.extractors.gta_hints import GtaHints


def hit(text):
    h = GtaHints().extract(text)
    return (h["kind"], h["raw"]) if h else None


def test_version_tag():
    assert GtaHints.name == "gta-hints@6"
    assert GtaHints().extract("Synthetic\nnear Example St & Sample Ave")["extractor"] == "gta-hints@6"


@pytest.mark.parametrize(
    ("text", "want"),
    [
        # 1. a line break is a phrase boundary: neighbour words are no longer glued on
        ("Synthetic\nMenuword\nVarne and Pemberly\nText for rates", ("intersection", "Varne and Pemberly")),
        ("Synthetic\nPlaceholder\n123 Example Rd\nCall me", ("address", "123 Example Rd")),
        # 2. the suffix ends at a word boundary: STAR is not ST
        ("Synthetic\n123 Example STAR service", None),
        ("Synthetic\nservice at 123 Example St. anytime", ("address", "123 Example St.")),
        # 2. a phone fragment is never a house number; the real intersection wins
        (
            "Synthetic\nclick to view-0000 Sample St\nnear Example St and Sample Ave",
            ("intersection", "Example St and Sample Ave"),
        ),
        ("Synthetic\ntext 555-0000 Example Rd", None),
        # 3. Spanish connectors and a direction letter between two streets
        ("Synthetic\nnear Example St e Sample Ave", ("intersection", "Example St e Sample Ave")),
        ("Synthetic\nVarne y Pemberly", ("intersection", "Varne y Pemberly")),
        ("Synthetic\nVARNE E PEMBERLY UNIT 5", ("intersection", "VARNE E PEMBERLY")),
        # 3. a direction word inside a side, then a connector
        (
            "Synthetic\nExample street east and Sample road intersection",
            ("intersection", "Example street east and Sample road"),
        ),
        ("Synthetic\nExample St E & Sample Ave", ("intersection", "Example St E & Sample Ave")),
        # 4. hotel-anchored address with a lowercase street name
        (
            "Synthetic\nPlaceholder inn 123 sample example drive Exampleton",
            ("address", "123 sample example drive"),
        ),
        ("Synthetic\nPlaceholder hotel 12 sexy example drive", None),  # ad vocabulary is not a street name
        # 5. area mentions, last resort
        ("Synthetic\nincall in Midmere Exampleton", ("area", "Midmere")),
        ("Synthetic\nDOWNTOWN SWEETHEART", ("area", "DOWNTOWN")),
        ("Synthetic\nnear airport area", ("area", "airport area")),
        ("Synthetic\nnear airport area Dorran", ("area", "airport area Dorran")),
        ("Synthetic\nVarne area", ("area", "Varne area")),
        (
            "Synthetic\nincall near Example St & Sample Ave, Midmere",
            ("intersection", "Example St & Sample Ave"),
        ),
        # 5. an outcall coverage list or several areas on one line is not a location
        ("Synthetic\nOutcall to Downtown • Midmere • Upmere", None),
        ("Synthetic\nDowntown, Midmere, Oldmill", None),
        ("Synthetic\noutcall downtown only", None),
        ("Synthetic\nDOWNTOWN OUT CALLS ONLY, PLACEHOLDER SERVICE", None),
        ("Synthetic\nmidmere out-call available", None),
        # a city alone is never an area hint
        ("Synthetic\nExampleton area", None),
        # @4 behaviour kept
        ("Synthetic | come by A1A 1A1 anytime", ("postal", "A1A 1A1")),
        ("Synthetic\nclose to glenmarket", ("landmark", "glenmarket")),
        ("Synthetic\nno place at all", None),
    ],
)
def test_shapes(text, want):
    assert hit(text) == want


def test_strongest_kind_wins_across_lines():
    # an area on line 1 never beats an address on line 3
    assert hit("Synthetic\nMidmere cutie\nhello\nincall 123 Example Rd") == ("address", "123 Example Rd")


def test_area_hint_validates():
    contracts.validate(TEXT_HINT, GtaHints().extract("Synthetic\nincall in Midmere"))


def test_area_equal_to_the_city_label_is_dropped():
    assert area_is_city({"raw": "Eastmere", "kind": "area"}, "Eastmere")
    assert not area_is_city({"raw": "Midmere", "kind": "area"}, "City of Exampleton")
    assert not area_is_city({"raw": "Eastmere", "kind": "address"}, "Eastmere")


LISTING = {
    "location": {"pin": {"lat": 43.70, "lon": -79.40}, "city_label": "Exampleton"},
}
PRECISE_GEOCODE = {
    "provider": "fixture-geocoding",
    "status": "OK",
    "at": "2026-01-01T00:00:00Z",
    "point": {"lat": 43.701, "lon": -79.401},
    "types": ["airport"],
    "precise": True,
}


def test_an_area_hint_never_counts_as_precise():
    h = {"raw": "airport area", "kind": "area", "extractor": "gta-hints@6"}
    r = derive.enrich(copy.deepcopy(LISTING), h, copy.deepcopy(PRECISE_GEOCODE), None, {}, "m")
    assert r["location"]["geocode"]["precise"] is False
    assert r["derived"]["confidence"] == "medium"  # pin only, never "high"
    assert PRECISE_GEOCODE["precise"] is True  # the cached block is not mutated
    p = {"raw": "Example St & Sample Ave", "kind": "intersection", "extractor": "gta-hints@6"}
    r = derive.enrich(copy.deepcopy(LISTING), p, copy.deepcopy(PRECISE_GEOCODE), None, {}, "m")
    assert r["location"]["geocode"]["precise"] is True and r["derived"]["confidence"] == "high"


# ── @6: the eight held-out misses of @5, as shapes ─────────────────────────────────────────────


def fancy(s):
    """ASCII -> MATHEMATICAL BOLD SCRIPT letters, the decorative font ads use; NFKC maps them back."""
    return "".join(
        chr(0x1D4D0 + ord(c) - 65)
        if "A" <= c <= "Z"
        else chr(0x1D4EA + ord(c) - 97)
        if "a" <= c <= "z"
        else c
        for c in s
    )


def full(text):
    return GtaHints().extract(text)


@pytest.mark.parametrize(
    ("text", "kind", "raw", "query"),
    [
        # 1-2. a documented alias resolves to a geocodable place phrase; raw stays as written
        ("EP1 placeholder title\nSynthetic", "landmark", "EP1", "Example Plaza Shopping Centre, Northmere"),
        (
            "Synthetic\nincall near ex plaza",
            "landmark",
            "ex plaza",
            "Example Plaza Shopping Centre, Northmere",
        ),
        ("Synthetic\nincall in SMC", "landmark", "SMC", "Sample Metro Centre station, Sampleville"),
        ("Synthetic\nclose to QTC", "landmark", "QTC", "Quarry Town Centre, Exampleton"),
        # 7. "<city> -<hwy>": one road near a city, never a point
        (
            "Exampleton placeholder\nSampleville -510 placeholder",
            "street",
            "Sampleville -510",
            "Highway 510, Sampleville",
        ),
        ("Synthetic\nNorthmere / 503", "street", "Northmere / 503", "Highway 503, Northmere"),
    ],
)
def test_aliases_and_city_highway_carry_a_query(text, kind, raw, query):
    h = full(text)
    assert (h["kind"], h["raw"], h["query"]) == (kind, raw, query)
    contracts.validate(TEXT_HINT, h)


@pytest.mark.parametrize(
    ("text", "want"),
    [
        # 3. "corner" connects an intersection
        (
            "Synthetic\nnear Varne St corner Pemberly Ave East area",
            ("intersection", "Varne St corner Pemberly Ave East"),
        ),
        ("Synthetic\ncorner of Example St and Sample Ave", ("intersection", "Example St and Sample Ave")),
        ("Synthetic\ncozy corner placeholder", None),
        # 4. a one-letter misspelling of a known street, only beside a real street
        ("Synthetic\nI am in Exampleton,Marvan & Tessaline.", ("intersection", "Marvan & Tessaline")),
        ("Synthetic\nVarne & Pembarly", ("intersection", "Varne & Pembarly")),
        ("Synthetic\nMarvan & Placeholder", None),  # no real street on either side
        ("Synthetic\nVarne & Kissy", None),  # too far from any street name
        ("Synthetic\nVarne & Pane", None),  # short words never fuzzy-match ("pane" ~ "pake")
        # 5. a unit letter on the house number
        (
            "Synthetic\nnew girls at 3021A Bramwick St north Exampleton",
            ("address", "3021A Bramwick St north"),
        ),
        ("Synthetic\n24H Example Rd service", None),  # "24H" is hours, not a house number
        ("Synthetic\n5ft Example Rd", None),
        # 6. decorative Unicode letters are read after NFKC
        (fancy("Synth In Westmere") + "\nSynthetic incall location", ("area", "Westmere")),
        ("Synthetic\nincall \uff11\uff12\uff13 Example Rd", ("address", "123 Example Rd")),
        ("Synthetic\nNear\u00a0Example St & Sample Ave", ("intersection", "Example St & Sample Ave")),
        # an outcall line is still dropped when the ad never offers incall
        ("Synthetic\nIn Westmere Outcalls!!!", None),
        # 7. negatives: a phone number is not a highway; a non-city word is not a city
        ("Synthetic\nSampleville -516-555-0000", None),
        ("Synthetic\nPlaceholder -510", None),
        # 8. a single street with no cross street: kind street, never precise
        ("Synthetic\nExampleton Morvan e", ("street", "Morvan e")),
        ("Synthetic\nincall on Varne Street", ("street", "Varne Street")),
        ("Synthetic\nqueen size bed", None),  # a street word in ordinary use is not a street
        ("Synthetic\nthe king of the main park", None),
        ("Synthetic\nVarne", None),  # a bare street word, no suffix or direction
    ],
)
def test_held_out_shapes(text, want):
    assert hit(text) == want


def test_alias_and_new_kinds_validate_and_plain_hints_have_no_query():
    for t in ("Synthetic\nincall in SMC", "Synthetic\nExampleton Morvan e"):
        contracts.validate(TEXT_HINT, full(t))
    assert "query" not in full("Synthetic\nnear Example St & Sample Ave")


def test_a_street_hint_never_counts_as_precise():
    h = {"raw": "Morvan e", "kind": "street", "extractor": "gta-hints@6"}
    r = derive.enrich(copy.deepcopy(LISTING), h, copy.deepcopy(PRECISE_GEOCODE), None, {}, "m")
    assert r["location"]["geocode"]["precise"] is False and r["derived"]["confidence"] == "medium"


def test_the_geocode_query_uses_the_alias_target():
    from listing_lens.commands import query_phrase

    assert query_phrase({"raw": "SMC", "query": "Sample Metro Centre station, Sampleville"}) == (
        "Sample Metro Centre station, Sampleville"
    )
    assert query_phrase({"raw": " Example St & Sample Ave "}) == "Example St & Sample Ave"
