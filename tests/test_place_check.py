"""llens place-check by the analyst's frozen rules (plan-v2 addendum). SYNTHETIC streets, hints and answers only:
a grid of invented streets around a synthetic label centre."""

import pytest

from listing_lens import derive
from listing_lens import place_check as pc
from listing_lens.commands import intersection_sides

C = {"lat": 43.6, "lon": -79.5}  # a SYNTHETIC label centre
D = 0.009  # ~1 km of latitude

# Example Street runs N-S through C; Sample Avenue runs E-W through C; they share the node C.
WAYS = [
    {
        "name": "Example Street",
        "g": [[C["lat"] - D, C["lon"]], [C["lat"], C["lon"]], [C["lat"] + D, C["lon"]]],
    },
    {
        "name": "Sample Avenue",
        "g": [[C["lat"], C["lon"] - D], [C["lat"], C["lon"]], [C["lat"], C["lon"] + D]],
    },
    {"name": "Placeholder Road", "g": [[C["lat"] + 5 * D, C["lon"]], [C["lat"] + 6 * D, C["lon"]]]},
]
ST = pc.Streets(WAYS, (43.0, -80.0, 44.0, -79.0))
T = 0.5


def north(km, of=C):
    return {"lat": of["lat"] + km / 111.195, "lon": of["lon"]}


def cand(
    point,
    types=("street_address",),
    loc="ROOFTOP",
    formatted="12 Example St, Synthville, SP X0X 0X0, Sampleland",
):
    return {"point": point, "types": list(types), "location_type": loc, "formatted": formatted}


def row(decision, hint, geocode, pin=None, ad="1"):
    return {
        "source": {"site": "example.test"},
        "ad_id": ad,
        "content": {"title": "t", "description": f"near {hint['raw']} today"},
        "location": {
            "city_label": "Synthville",
            "pin": pin,
            "text_hint": hint,
            "geocode": geocode,
            "city_centre": {"label": "Synthville", "point": C},
        },
        "derived": {"geocode_decision": decision},
    }


def geo(c, extra=()):
    return {"status": "OK", "precise": True, **c, "candidates": [c, *extra]}


def test_norm_expands_and_folds():
    assert pc.norm("Exämple St. W") == "example street west"
    assert pc.base("example street west") == "example"


def test_names_exact_base_nodir():
    assert ST.names("Example St", "address") == ({"example street"}, "exact")
    assert ST.names("Example", "intersection") == ({"example street"}, "base")
    assert ST.names("Example", "address") == (set(), "exact")  # base is for intersection sides only
    assert ST.names("Example St N", "address") == ({"example street"}, "nodir")
    assert ST.names("Example St N", "intersection") == (set(), "exact")  # nodir is for addresses only
    assert ST.names("Nowhere St", "address") == (set(), "exact")


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("Example St/Sample Ave", ("Example St", "Sample Ave")),
        ("Hwy 7 and Example St", ("Highway 7", "Example St")),
        ("507 & Sample Ave", ("Highway 507", "Sample Ave")),
        ("Example St", None),
    ],
)
def test_intersection_sides(raw, want):
    assert intersection_sides(raw) == want


def test_intersection_verified_at_shared_node():
    h = {"raw": "Example & Sample", "kind": "intersection"}
    g = geo(
        cand(
            north(0.1),
            types=("intersection",),
            loc="GEOMETRIC_CENTER",
            formatted="Example St & Sample Ave, Synthville, SP, Sampleland",
        )
    )
    d = pc.decide(row("no_pin", h, g), ST, [], T)
    assert d["decision"] == "text_only_verified", d
    assert d["criteria"]["name_match"] == "base"


def test_address_verified_near_same_name_way():
    h = {"raw": "12 Example St", "kind": "address"}
    d = pc.decide(row("no_pin", h, geo(cand(north(0.3)))), ST, [], T)
    assert d["decision"] == "text_only_verified"


@pytest.mark.parametrize(
    ("hint", "geocode", "reason"),
    [
        (
            {"raw": "12 Example St", "kind": "address"},
            geo(cand(north(0.3), loc="APPROXIMATE")),
            "type_mismatch",
        ),
        ({"raw": "12 Example St", "kind": "address"}, geo(cand(north(0.3)), [cand(north(3))]), "ambiguous"),
        ({"raw": "12 Placeholder Rd", "kind": "address"}, geo(cand(north(0.3))), "osm_disagrees"),
        (
            {"raw": "12 Example St", "kind": "address"},
            geo(cand(north(0.3) | {"lat": 45.5})),
            "no_osm_coverage",
        ),
        ({"raw": "A1A", "kind": "postal"}, geo(cand(north(0.3), types=("postal_code",))), "area_only"),
    ],
)
def test_no_pin_review_reasons(hint, geocode, reason):
    d = pc.decide(row("no_pin", hint, geocode), ST, [], T)
    assert d["decision"] == "review"
    assert d["reason"] == f"sources_disagree:{reason}"


def test_label_too_far_from_centre():
    far_street = [
        *WAYS,
        {"name": "Remote Street", "g": [[C["lat"] + 0.18, C["lon"]], [C["lat"] + 0.19, C["lon"]]]},
    ]
    st = pc.Streets(far_street, (43.0, -80.0, 44.0, -79.0))
    h = {"raw": "12 Remote St", "kind": "address"}
    d = pc.decide(row("no_pin", h, geo(cand({"lat": C["lat"] + 0.185, "lon": C["lon"]}))), st, [], T)
    assert d["reason"] == "sources_disagree:label_far"


def test_without_t_osm_criterion_4_fails_closed():
    h = {"raw": "12 Example St", "kind": "address"}
    d = pc.decide(row("no_pin", h, geo(cand(north(0.3)))), ST, [], None)
    assert d["decision"] == "review" and d["reason"] == "sources_disagree:osm_unjudged"


def test_landmark_needs_exactly_one_feature():
    h = {"raw": "Example Centre", "kind": "landmark"}
    g = geo(cand(north(1), types=("shopping_mall",), formatted="Example Centre, Synthville, SP, Sampleland"))
    one = [{"names": {"example centre"}, "point": north(1)}]
    assert pc.decide(row("no_pin", h, g), ST, one, T)["decision"] == "text_only_verified"
    two = one + [{"names": {"example centre"}, "point": north(5)}]
    assert pc.decide(row("no_pin", h, g), ST, two, T)["reason"] == "sources_disagree:landmark_ambiguous"


def test_rejected_and_city_default_always_review():
    h = {"raw": "Hwy 7 & Example St", "kind": "intersection"}
    g = {"status": derive.REJECTED_HINT_NUMBER, "precise": False, **cand(north(0.1))}
    assert pc.decide(row(None, h, g), ST, [], T)["reason"] == "rejected_hint_number"
    h2 = {"raw": "12 Example St", "kind": "address"}
    assert (
        pc.decide(row("city_default_pin", h2, geo(cand(north(0.3)))), ST, [], T)["reason"]
        == "city_default_pin"
    )


def test_far_takes_the_one_alternative_near_the_pin():
    h = {"raw": "12 Example St", "kind": "address"}
    pin = north(0.4)
    g = geo(cand(north(9)), [cand(north(0.2))])
    d = pc.decide(row("far", h, g, pin=pin), ST, [], T)
    assert d["decision"] == "auto_alternative" and d["alternative"] == 1


def test_far_with_two_alternatives_near_the_pin_is_ambiguous():
    h = {"raw": "12 Example St", "kind": "address"}
    g = geo(cand(north(9)), [cand(north(0.2)), cand(north(1.0))])
    assert pc.decide(row("far", h, g, pin=north(0.4)), ST, [], T)["reason"] == "ambiguous"


def test_far_alternative_must_pass_type_match():
    h = {"raw": "12 Example St", "kind": "address"}
    g = geo(cand(north(9)), [cand(north(0.2), loc="APPROXIMATE")])
    assert pc.decide(row("far", h, g, pin=north(0.4)), ST, [], T)["reason"] == "alternative_fails_criteria"


def test_locality_mismatch_is_recorded_only():
    h = {"raw": "12 Example St", "kind": "address"}
    c = cand(north(0.3), formatted="12 Example St, Othertown, SP X0X 0X0, Sampleland")
    d = pc.decide(row("no_pin", h, geo(c)), ST, [], T)
    assert d["decision"] == "text_only_verified" and d["locality_mismatch_b"] is True
    assert d["snapped_by_bias_a"] is None


def test_span_is_offsets_never_text():
    h = {"raw": "12 Example St", "kind": "address"}
    s = pc.span(row("no_pin", h, geo(cand(north(0.3)))))
    assert s == {"field": "description", "start": 5, "end": 18, "basis": "original"}


def test_calibrate_uses_auto_close_only():
    h = {"raw": "12 Example St", "kind": "address"}
    rows = [row("auto_close", h, geo(cand(north(0.2))), ad=str(i)) for i in range(3)]
    rows.append(row("no_pin", h, geo(cand(north(3))), ad="9"))
    out = pc.calibrate(rows, ST)
    assert out["n"] == 3 and out["t_osm_km"] == 0.5


def test_group_key_joins_same_hint_and_result():
    h = {"raw": "12 Example St", "kind": "address"}
    a = row("no_pin", h, geo(cand(north(0.3))), ad="1")
    b = row("no_pin", {"raw": "12 example st.", "kind": "address"}, geo(cand(north(0.3))), ad="2")
    c = row("no_pin", h, geo(cand(north(0.9))), ad="3")
    assert pc.group_key(a) == pc.group_key(b) != pc.group_key(c)
