"""The geocoder defect's post-check: a street_address whose house number is a token of a NON-address hint
(a numbered highway in an intersection) is rejected in derive. Synthetic hints and answers only."""

import pytest

from listing_lens import derive

C = {"lat": 43.6, "lon": -79.5}  # a SYNTHETIC label centre


def geo(formatted, types=("street_address",), status="OK"):
    return {
        "provider": "fixture",
        "status": status,
        "formatted": formatted,
        "types": list(types),
        "precise": True,
        "location_type": "ROOFTOP",
        "point": C,
    }


@pytest.mark.parametrize(
    ("hint", "answer", "rejected"),
    [
        # the defect: the highway's number became a house number
        ({"raw": "Hwy 7 & Example St", "kind": "intersection"}, geo("7 Example St, Exampleton, SP"), True),
        ({"raw": "507/Sample Ave", "kind": "intersection"}, geo("507 Sample Ave, Exampleton, SP"), True),
        (
            {"raw": "Example Centre", "kind": "landmark", "query": "Unit 9 Example Centre"},
            geo("9 Example Rd, Exampleton, SP"),
            True,
        ),
        ({"raw": "Hwy 7 & Example St", "kind": "intersection"}, geo("3-7 Example St, Exampleton, SP"), True),
        # no token match: a different number is kept
        ({"raw": "Hwy 7 & Example St", "kind": "intersection"}, geo("17 Example St, Exampleton, SP"), False),
        # an intersection answer is never a street_address
        (
            {"raw": "Hwy 7 & Example St", "kind": "intersection"},
            geo("Highway 7 & Example St, Exampleton, SP", types=("intersection",)),
            False,
        ),
        # address / postal / a label holding an address are exempt
        ({"raw": "7 Example St", "kind": "address"}, geo("7 Example St, Exampleton, SP"), False),
        ({"raw": "Location: 7 Example St", "kind": "label"}, geo("7 Example St, Exampleton, SP"), False),
        # a label with a highway, not an address, is checked
        ({"raw": "Hwy 7 and Example", "kind": "label"}, geo("7 Example St, Exampleton, SP"), True),
        # only an OK answer is judged
        (
            {"raw": "Hwy 7 & Example St", "kind": "intersection"},
            geo("7 Example St, Exampleton, SP", status="ZERO_RESULTS"),
            False,
        ),
    ],
)
def test_hint_number_house(hint, answer, rejected):
    assert derive.hint_number_house(hint, answer) is rejected


def test_rejected_answer_leaves_the_decision():
    listing = {"location": {"city_label": "Synthville"}}
    hint = {"raw": "Hwy 7 & Example St", "kind": "intersection"}
    centre = {"label": "Synthville", "point": C, "provider": "fixture"}
    r = derive.enrich(listing, hint, geo("7 Example St, Exampleton, SP"), centre, {}, "test")
    assert r["location"]["geocode"]["status"] == derive.REJECTED_HINT_NUMBER
    assert r["location"]["geocode"]["precise"] is False
    d = r["derived"]
    assert d["geocode_decision"] is None  # no longer a no_pin review case
    assert d["km_text_city"] is None
    assert d["location_invalid_reason"] == "unresolvable"


def test_query_phrase_asks_intersections_as_a_and_b():
    from listing_lens.commands import query_phrase

    assert query_phrase({"raw": "Hwy 7/Example St", "kind": "intersection"}) == "Highway 7 & Example St"
    assert query_phrase({"raw": "507 and Sample Ave", "kind": "intersection"}) == "Highway 507 & Sample Ave"
    assert (
        query_phrase({"raw": "Example St & Sample Ave", "kind": "intersection"}) == "Example St & Sample Ave"
    )
    assert query_phrase({"raw": "12 Example St", "kind": "address"}) == "12 Example St"
    alias = {"raw": "EXC", "kind": "landmark", "query": "Example Centre, Exampleton"}
    assert query_phrase(alias) == "Example Centre, Exampleton"
