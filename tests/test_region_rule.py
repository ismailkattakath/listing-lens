"""Region consistency + the resolution rule in derive (operator rules; thresholds pre-registered by the analyst).
Synthetic points only, placed at exact distances north of a synthetic label centre."""

import math

import pytest

from listing_lens import derive

C = {"lat": 43.6, "lon": -79.5}  # a SYNTHETIC label centre


def north(km: float, of: dict = C) -> dict:
    return {"lat": of["lat"] + km / (6371.0088 * math.pi / 180), "lon": of["lon"]}


def run(pin_km=None, text_km=None, precise=True, status="OK", modes=None, pin=None):
    pin = pin or (north(pin_km) if pin_km is not None else None)
    listing = {"location": {"city_label": "Synthville", **({"pin": pin} if pin else {})}}
    hint = {"raw": "synthetic phrase", "kind": "address"}
    geo = {
        "provider": "fixture",
        "status": status,
        "precise": precise,
        "point": north(text_km) if text_km is not None else None,
    }
    centre = {"label": "Synthville", "point": C, "provider": "fixture"}
    return derive.enrich(listing, hint, geo, centre, modes or {}, "test")["derived"]


@pytest.mark.parametrize("km, valid", [(59.9, True), (60.1, False)])
def test_the_pre_registered_60_km_boundary(km, valid):
    d = run(pin_km=km, text_km=1)
    assert d["location_valid_pin"] is valid
    assert d["location_invalid_reason"] == (None if valid else "out_of_region_pin")


def test_text_out_of_region_and_both():
    assert run(pin_km=1, text_km=60.1)["location_invalid_reason"] == "out_of_region_text"
    assert run(pin_km=70, text_km=80)["location_invalid_reason"] == "both"
    assert run(text_km=363)["geocode_decision"] == "out_of_region"  # a province-wide same-name hit: reviewed


def test_an_ungeocoded_hint_is_unresolvable():
    d = run(pin_km=1, status="ZERO_RESULTS")
    assert d["location_invalid_reason"] == "unresolvable" and d["geocode_decision"] is None


def _pin_and_text(pin_km_from_centre: float, text_from_pin: float):
    pin = north(pin_km_from_centre)
    return pin, north(text_from_pin, of=pin)


@pytest.mark.parametrize("gap, expected", [(4.9, "auto_close"), (5.1, "far")])
def test_the_pre_registered_5_km_auto_accept(gap, expected):
    pin, text = _pin_and_text(2.0, gap)
    listing = {"location": {"city_label": "Synthville", "pin": pin}}
    geo = {"provider": "fixture", "status": "OK", "precise": True, "point": text}
    d = derive.enrich(
        listing,
        {"raw": "x", "kind": "address"},
        geo,
        {"label": "Synthville", "point": C, "provider": "f"},
        {},
        "t",
    )
    assert d["derived"]["geocode_decision"] == expected


def test_a_city_default_pin_is_reviewed_even_when_close():
    pin, text = _pin_and_text(2.0, 4.3)
    modes = {"Synthville": (derive._key(pin), 5)}  # the pin is the label's shared default pin
    listing = {"location": {"city_label": "Synthville", "pin": pin}}
    geo = {"provider": "fixture", "status": "OK", "precise": True, "point": text}
    d = derive.enrich(
        listing,
        {"raw": "x", "kind": "address"},
        geo,
        {"label": "Synthville", "point": C, "provider": "f"},
        modes,
        "t",
    )
    assert (
        d["derived"]["pin_verdict"] == "city_default"
        and d["derived"]["geocode_decision"] == "city_default_pin"
    )


def test_no_pin_and_imprecise():
    assert run(text_km=3)["geocode_decision"] == "no_pin"
    assert run(pin_km=1, text_km=1, precise=False)["geocode_decision"] is None  # nothing precise to decide on


def test_flagged_points_leave_the_h1_statistics():
    good = derive.enrich(
        {"location": {"city_label": "S", "pin": north(1)}},
        {"raw": "x", "kind": "address"},
        {"provider": "f", "status": "OK", "precise": True, "point": north(1.5)},
        {"label": "S", "point": C, "provider": "f"},
        {},
        "t",
    )
    bad = derive.enrich(
        {"location": {"city_label": "S", "pin": north(70)}},
        {"raw": "x", "kind": "address"},
        {"provider": "f", "status": "OK", "precise": True, "point": north(1.5)},
        {"label": "S", "point": C, "provider": "f"},
        {},
        "t",
    )
    s = derive.summarise([good, bad], "t")
    assert s["precise_pin_and_text"]["n"] == 1  # the out-of-region pin is "no location"
    assert s["region_flags"] == {"valid": 1, "out_of_region_pin": 1}
    assert s["geocode_decisions"] == {"auto_close": 1, "out_of_region": 1}
