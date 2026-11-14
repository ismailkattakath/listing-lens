"""Place decisions applied in derive (place_apply): operator > machine > auto_close > bare pin.
SYNTHETIC points, hints, ids and answers only."""

import json
import math

import pytest
from conftest import FIXTURES
from scrape_harness.cli import main

from listing_lens import derive
from listing_lens import place_apply as pa
from listing_lens import place_check as pc

C = {"lat": 43.6, "lon": -79.5}  # a SYNTHETIC label centre
REF = {"provider": "fixture", "query": "synthetic phrase, Synthville"}


def north(km, of=C):
    return {"lat": of["lat"] + km / (6371.0088 * math.pi / 180), "lon": of["lon"]}


def row(
    ad="1",
    pin=None,
    gpoint=None,
    cands=None,
    decision=None,
    verdict="no_pin",
    status="OK",
    hint="Example St & Sample Ave",
):
    g = {"provider": "fixture", "status": status, "precise": True, "formatted": "Synthetic answer"}
    if gpoint:
        g["point"] = gpoint
    if cands is not None:
        g["candidates"] = cands
    return {
        "source": {"site": "example.test"},
        "ad_id": ad,
        "location": {
            "city_label": "Synthville",
            **({"pin": pin} if pin else {}),
            "text_hint": {"raw": hint, "kind": "intersection"},
            "geocode": g,
            "city_centre": {"label": "Synthville", "point": C},
        },
        "derived": {
            "method": "m",
            "geocode_decision": decision,
            "pin_verdict": verdict,
            "location_invalid_reason": None,
        },
    }


def op(r, decision, value=None, gold=False, ad_ids=None):
    return {
        "item": "place_group", "group": pc.group_key(r), "ad_ids": ad_ids or [r["ad_id"]], "decision": decision,
        "value": value, "gold_v2": gold, "reviewer": "operator", "_ref": "places/batch-x#1",
    }  # fmt: skip


def fields(r, operator=None, machine=None, answers=None, ref=REF):
    return pa.place_fields(r, operator, machine, ref, lambda q: (answers or {}).get(q), "fixture")


def test_accept_google_takes_the_answer_even_without_stored_candidates():
    r = row(gpoint=north(2), cands=None)  # a cache entry from before candidates were kept
    f = fields(r, op(r, "accept_google"))
    assert f["place_point"] == north(2) and f["place_point_source"] == "google"
    assert (f["place_decided_by"], f["place_decision_status"]) == ("operator", "applied")
    assert f["place_cache_ref"] == REF | {"candidate": None} and f["location_valid_place"] is True


def test_pick_alternative_indexes_candidates_from_one():
    a0, a1 = {"point": north(1)}, {"point": north(3)}
    r = row(gpoint=north(1), cands=[a0, a1])
    f = fields(r, op(r, "pick_alternative", "1"))
    assert f["place_point"] == north(3) and f["place_point_source"] == "alternative"
    assert f["place_cache_ref"]["candidate"] == 1
    assert fields(r, op(r, "pick_alternative", "2"))["place_decision_status"] == "missing_alternative"


def test_the_operator_wins_over_the_machine():
    r = row(gpoint=north(2), cands=[{"point": north(2)}])
    machine = {"decision": "text_only_verified", "decided_by": "place-check@1", "point": north(2)}
    f = fields(
        r,
        op(r, "other_place", "Synthetic Mall, Synthville, ON"),
        machine,
        {"Synthetic Mall, Synthville, ON": {"status": "OK", "point": north(4)}},
    )
    assert f["place_point"] == north(4) and f["place_point_source"] == "operator_geocode"
    assert f["place_cache_ref"]["query"] == "Synthetic Mall, Synthville, ON"


def test_a_changed_google_answer_makes_the_operator_decision_stale():
    seen = row(gpoint=north(2))
    decision = op(seen, "accept_google")
    now = row(gpoint=north(9))  # the cached answer moved since the review
    f = fields(now, decision)
    assert f["place_decision_status"] == "stale_group" and f["place_point"] is None


def test_other_place_pending_failed_and_the_hint_number_check():
    r = row(gpoint=north(2))
    text = "Example St & Highway 401 interchange, Synthville, ON"
    assert fields(r, op(r, "other_place", text))["place_decision_status"] == "geocode_pending"
    assert (
        fields(r, op(r, "other_place", text), answers={text: {"status": "ZERO_RESULTS"}})[
            "place_decision_status"
        ]
        == "geocode_failed"
    )
    invented = {
        "status": "OK",
        "point": north(1),
        "types": ["street_address"],
        "formatted": "401 Example St, Synthville, ON",
    }
    f = fields(r, op(r, "other_place", text), answers={text: invented})
    assert f["place_decision_status"] == "rejected_hint_number" and f["place_point"] is None


def test_a_final_point_out_of_region_is_flagged_never_removed():
    r = row(pin=north(1), gpoint=north(2), verdict="neither", decision="far")
    text = "Synthetic Mall, Elsewhere, ON"
    f = fields(r, op(r, "other_place", text), answers={text: {"status": "OK", "point": north(61)}})
    out = pa.apply(r, f)["derived"]
    assert out["place_point"] == north(61) and out["location_valid_place"] is False
    assert out["location_invalid_reason"] == "out_of_region_place"
    assert r["location"]["pin"] == north(1)  # the raw points stay
    inside = fields(r, op(r, "other_place", text), answers={text: {"status": "OK", "point": north(59)}})
    assert inside["location_valid_place"] is True


def test_a_pin_reason_keeps_its_meaning():
    r = row(pin=north(70), verdict="neither")
    r["derived"]["location_invalid_reason"] = "out_of_region_pin"
    out = pa.apply(r, fields(r, ref=None))["derived"]
    assert out["place_point_source"] == "pin" and out["location_valid_place"] is False
    assert out["location_invalid_reason"] == "out_of_region_pin"


def test_machine_decisions_and_their_stale_guard():
    r = row(gpoint=north(2), cands=[{"point": north(2)}, {"point": north(3)}])
    v = {"decision": "text_only_verified", "decided_by": "place-check@1", "point": north(2)}
    f = fields(r, machine=v)
    assert (f["place_point_source"], f["place_decided_by"]) == ("machine_verified", "place-check@1")
    assert fields(r, machine=v | {"point": north(5)})["place_decision_status"] == "stale_point"
    alt = {"decision": "auto_alternative", "decided_by": "place-check@1", "alternative": 1, "point": north(3)}
    assert fields(r, machine=alt)["place_point"] == north(3)
    review = fields(r, machine={"decision": "review", "decided_by": "place-check@1"})
    assert review["place_decision_status"] == "review" and review["place_point"] is None


def test_without_a_decision_auto_close_then_a_bare_pin_then_nothing():
    close = fields(row(pin=north(1), gpoint=north(1.5), decision="auto_close", verdict="at_address"))
    assert (close["place_decision"], close["place_point_source"]) == ("auto_close", "google")
    bare = fields(row(pin={**north(1), "approximate": True}, verdict="no_text"), ref=None)
    assert bare["place_point"] == north(1) and bare["place_point_source"] == "pin"  # site extras dropped
    assert fields(row(pin=north(1), verdict="city_default"), ref=None)["place_point"] is None
    assert (
        fields(row(pin=north(1), gpoint=north(9), decision="far", verdict="neither"))["place_point"] is None
    )
    assert fields(row(gpoint=north(1), decision="no_pin")) == pa.EMPTY | {"location_valid_place": None}


def test_one_group_decision_fans_out_to_every_ad_and_gold_v2_is_carried():
    a, b = row(ad="1", gpoint=north(2)), row(ad="2", gpoint=north(2))
    rv = op(a, "accept_google", gold=True, ad_ids=["1", "2"])
    by = pa.by_ad([rv])
    assert set(by) == {"1", "2"}
    for r in (a, b):
        f = fields(r, by[r["ad_id"]])
        assert f["place_decision_status"] == "applied" and f["place_gold_v2"] is True


def review_file(d, rows):
    (d / "decisions").mkdir(parents=True, exist_ok=True)
    (d / "decisions" / "place-review.batch-x.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_an_unknown_review_is_refused(tmp_path):
    review_file(tmp_path, [{"item": "place_group", "group": "g", "ad_ids": ["1"], "decision": "move_it"}])
    with pytest.raises(ValueError, match="unknown place review"):
        pa.load_reviews(tmp_path)
    review_file(
        tmp_path,
        [
            {
                "item": "place_group",
                "group": "g",
                "ad_ids": ["1"],
                "decision": "pick_alternative",
                "value": "x",
            }
        ],
    )
    with pytest.raises(ValueError, match="alternative index"):
        pa.load_reviews(tmp_path)


def test_not_a_place_tokens_are_never_place_candidates(tmp_path):
    from listing_lens import places

    review_file(
        tmp_path, [{"item": "abbreviation", "group": "QX", "ad_ids": ["1"], "decision": "not_a_place"}]
    )
    words = places.not_place_words(tmp_path)
    assert "qx" in words
    src = {"QX": {"osm": {"Quixley Plaza"}}}
    ad = {"ad_id": "1", "content": {"title": "t", "description": "incall near QX"}}
    assert places.candidates([ad], src, set(), {}, words) == {}
    assert places.candidates([ad], src, set(), {}, set()) == {"QX": {"1"}}


# ── the stages end to end (fixture crawl + fixture geocoder, offline) ─────────────────────────

FAST = ["--max-tasks-per-minute", "6000", "--approve-politeness-override"]
FIX = ["--driver", "fixture", "--fixtures", str(FIXTURES), *FAST]


def derived(d):
    return {
        json.loads(x)["ad_id"]: json.loads(x)
        for x in (d / "derived/listings.latest.jsonl").read_text().splitlines()
    }


def test_geocode_only_place_review_and_derive_apply_the_operator(data_dir, tmp_path, capsys):
    answers = json.loads((FIXTURES / "geocode-answers.json").read_text())
    text = "Synthetic Galleria, Exampleton, ON"
    answers[text] = {
        "formatted_address": "Synthetic Galleria, Exampleton, ON, Canada",
        "geometry": {"location": {"lat": 43.705, "lng": -79.405}},
        "types": ["shopping_mall", "establishment"],
        "place_id": "synthetic-op-1",
    }
    fx = tmp_path / "answers.json"
    fx.write_text(json.dumps(answers))
    geo = ["--geocoder", "fixture", "--geocode-fixture", str(fx)]
    assert main(["crawl-lists", "--subcategory", "sample-list-a", "--stride", "2", "--start", "1", *FIX]) == 0
    assert main(["fetch-ads", *FIX]) == 0
    assert main(["geocode", *geo]) == 0
    assert main(["derive", "--provider", "fixture-geocoding"]) == 0
    rows = derived(data_dir)
    with_point = sorted(a for a, r in rows.items() if (r["location"].get("geocode") or {}).get("point"))
    other = sorted(set(rows) - {with_point[0]})[0]
    review_file(data_dir, [
        {"batch": "places/batch-x", "ls_task": 1, "item": "place_group", "group": pc.group_key(rows[with_point[0]]),
         "ad_ids": [with_point[0]], "decision": "accept_google", "value": None, "gold_v2": True, "reviewer": "operator"},
        {"batch": "places/batch-x", "ls_task": 2, "item": "place_group", "group": pc.group_key(rows[other]),
         "ad_ids": [other], "decision": "other_place", "value": text, "gold_v2": False, "reviewer": "operator"},
    ])  # fmt: skip
    capsys.readouterr()
    assert main(["geocode", *geo, "--only", "place-review", "--dry-run"]) == 0
    planned = [
        json.loads(x)["query"] for x in capsys.readouterr().out.splitlines() if x.startswith('{"query"')
    ]
    assert planned == [text]  # only the operator's text, nothing else pending or expired
    assert main(["geocode", *geo, "--only", "place-review", "--max-requests", "5"]) == 0
    assert '"requested": 1' in capsys.readouterr().out
    assert main(["derive", "--provider", "fixture-geocoding"]) == 0
    after = derived(data_dir)
    a, o = after[with_point[0]]["derived"], after[other]["derived"]
    assert (a["place_decision"], a["place_point_source"], a["place_gold_v2"]) == (
        "accept_google",
        "google",
        True,
    )
    assert (o["place_decision"], o["place_point_source"]) == ("other_place", "operator_geocode")
    assert o["place_point"] == {"lat": 43.705, "lon": -79.405} and o["place_review_ref"] == "places/batch-x#2"
    s = json.loads((data_dir / "derived/summary.json").read_text())["place"]
    assert s["operator_applied"] == 2 and s["decisions_sha256"] == pa.decisions_sha(data_dir)
    # the rule the place point is judged by is the LOADED site radius (site config [region] km; the synthetic
    # site sets the same 60 km, and the pre-registration note sits beside the real value in DATA)
    assert derive.REGION_KM == 60.0


# ── the OSM fallback for a rejected other_place text (synthetic geometry, no shared node = a bridge) ──

K = 1 / 111.195  # degrees of latitude per km


def osm_streets(extra=()):
    lat, lon = C["lat"], C["lon"]
    dl = K / math.cos(math.radians(lat))
    ways = [
        {"name": "Example Road", "g": [[lat - 2 * K, lon], [lat - 0.3 * K, lon], [lat + 0.4 * K, lon], [lat + 2 * K, lon]]},
        {"name": "Highway 9", "g": [[lat, lon - 2 * dl], [lat, lon - 0.2 * dl], [lat, lon + 0.6 * dl], [lat, lon + 2 * dl]]},
        *extra,
    ]  # fmt: skip
    return pc.Streets(ways, (43.0, -80.0, 44.0, -79.0))


def rejected(r, text):
    invented = {
        "status": "OK",
        "point": north(1),
        "types": ["street_address"],
        "formatted": "9 Example Rd, Synthville",
    }
    return pa.place_fields(
        r,
        op(r, "other_place", text),
        None,
        REF,
        lambda q: invented,
        "fixture",
        lambda t: pa.osm_crossings(ST, t),
    )


ST = osm_streets()
TEXT = "Example Rd & Hwy 9 interchange, Synthville, ON"


def test_a_rejected_google_answer_gets_the_osm_crossing():
    assert pa.osm_sides(TEXT) == ("Example Rd", "Highway 9")
    f = rejected(row(gpoint=north(2)), TEXT)
    assert (f["place_decision_status"], f["place_point_source"]) == ("applied", "operator_osm")
    assert derive.km(f["place_point"], C) < 0.01 and f["place_osm"]["gap_km"] == 0.0
    assert f["place_osm"]["google"] == "rejected_hint_number" and f["place_cache_ref"] is None
    assert f["location_valid_place"] is True


def test_a_good_google_answer_never_asks_osm():
    r = row(gpoint=north(2))
    called = []
    f = pa.place_fields(
        r,
        op(r, "other_place", TEXT),
        None,
        REF,
        lambda q: {"status": "OK", "point": north(3)},
        "fixture",
        lambda t: called.append(t),
    )
    assert f["place_point_source"] == "operator_geocode" and called == []


def gap_streets(gap_km):
    lat, lon = C["lat"], C["lon"]
    dl = K / math.cos(math.radians(lat))
    return pc.Streets(
        [{"name": "Example Road", "g": [[lat - 2 * K, lon], [lat + 2 * K, lon]]},
         {"name": "Highway 9", "g": [[lat, lon + gap_km * dl], [lat, lon + 2 * dl]]}],
        (43.0, -80.0, 44.0, -79.0),
    )  # fmt: skip


@pytest.mark.parametrize("gap, status", [(0.45, "applied"), (0.55, "osm_gap_too_large")])
def test_the_roads_must_come_within_t_osm(gap, status):
    s, p, info = pa.osm_point(pa.osm_crossings(gap_streets(gap), TEXT), C)
    assert s == status and (p is not None) == (status == "applied")
    if p:
        assert abs(info["gap_km"] - gap) < 0.01


def test_a_same_named_crossing_far_from_the_label_is_another_place():
    far = north(20)
    dl = K / math.cos(math.radians(far["lat"]))
    twin = [
        {"name": "Example Road", "g": [[far["lat"] - K, far["lon"]], [far["lat"] + K, far["lon"]]]},
        {"name": "Highway 9", "g": [[far["lat"], far["lon"] - dl], [far["lat"], far["lon"] + dl]]},
    ]
    res = pa.osm_crossings(osm_streets(twin), TEXT)
    s, p, _ = pa.osm_point(res, C)
    assert s == "applied" and derive.km(p, C) < 0.01  # the twin 20 km away does not count
    assert pa.osm_point(res, north(10))[0] == "osm_ambiguous"  # a centre within 15 km of both
    assert pa.osm_point(res, north(80))[0] == "osm_not_near_label"


def test_a_highways_carriageways_count_as_the_highway():
    lat, lon = C["lat"], C["lon"]
    dl = K / math.cos(math.radians(lat))
    s = pc.Streets(
        [{"name": "Example Road", "g": [[lat - 2 * K, lon], [lat + 2 * K, lon]]},
         {"name": "Highway 9", "g": [[lat + 5 * K, lon - dl], [lat + 5 * K, lon + dl]]},
         {"name": "Highway 9 Collector", "g": [[lat, lon - dl], [lat, lon + dl]]}],
        (43.0, -80.0, 44.0, -79.0),
    )  # fmt: skip
    res = pa.osm_crossings(s, TEXT)
    assert "highway 9 collector" in res["names"][1] and pa.osm_point(res, C)[0] == "applied"


def test_unknown_roads_and_unparsed_texts_say_why():
    assert pa.osm_crossings(ST, "Nowhere Rd & Hwy 9, Synthville, ON")["status"] == "osm_no_way"
    assert pa.osm_crossings(ST, "Galleria 9, Synthville, ON")["status"] == "osm_unparsed"
    assert pa.osm_point(pa.osm_crossings(ST, TEXT), None)[0] == "osm_no_label_centre"
    f = rejected(row(gpoint=north(2)), "Galleria 9, Synthville, ON")  # "9 Example Rd" took the 9: rejected
    assert f["place_decision_status"] == "osm_unparsed" and f["place_osm"]["google"] == "rejected_hint_number"


def test_an_osm_point_still_gets_the_region_rule():
    r = row(gpoint=north(2))
    r["location"]["city_centre"]["point"] = north(-61)  # the label centre 61 km south of the crossing
    s, p, _ = pa.osm_point(pa.osm_crossings(ST, TEXT), north(-61))
    assert s == "osm_not_near_label"  # 15 km comes first; the region rule still judges any accepted point:
    f = rejected(row(gpoint=north(2)), TEXT)
    assert derive.region_validity(None, f["place_point"], north(-61))[1] is False
