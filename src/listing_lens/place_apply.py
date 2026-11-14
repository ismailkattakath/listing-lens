"""Apply place decisions in derive: one final point per ad, with its provenance (pure functions).

Inputs, both in DATA/decisions/ (never git):
- the OPERATOR's reviews, `place-review.*.jsonl` (a Label Studio batch exported by the team): one row per
  review group (`item` place_group: `group`, `ad_ids`, `decision` accept_google | pick_alternative (value = the
  alternative's index, alternatives = geocode candidates[1:], numbered from 1) | other_place (value = the
  operator's place text, geocoded by `llens geocode`, query = review_query)) or per abbreviation token (`item`
  abbreviation: not_a_place, which places-resolve reads);
- the MACHINE's, `place-check.jsonl` rows decided_by place-check@N (text_only_verified, auto_alternative, review).

Precedence per ad: operator > machine > derive's own auto_close (the precise text point <= 5 km from a real
pin) > a bare pin (no precise text point to decide on, not a city default) > nothing. An operator row fans out
to EVERY ad_id of its group, but only while the ad's current review group (place_check.group_key) is still the
one the operator saw: a changed Google answer makes the decision STALE (not applied, counted), because
accepting it would accept a point nobody reviewed.

The final point gets the region rule (derive.region_validity: the label's census divisions, else > REGION_KM from the label centre): flagged,
never deleted. An operator's other_place answer gets derive's hint-number check too ("Kennedy Rd & Highway
401" must not come back as "401 Kennedy Rd"). When Google's answer is rejected or failed, an "A & B" text gets an
OFFLINE point from the OSM street snapshot (osm_crossings / osm_point; source operator_osm, never a paid call):
the two roads' closest approach, accepted only within T_osm and as the ONE interchange near the ad's label.

EVAL GOLD: `gold_v2` rows are operator decisions on ads of the frozen address-gold-v2 sample. They are applied
like any other, and carried as `place_gold_v2`, so that nothing scored against that gold can read them: a
builder of an address/place gold must never take its labels from place-review files or from place_* fields
(they ARE the operator's answers, i.e. the gold's own labels, leaked into the pipeline output).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import derive

TAG = "placedec"  # in derive's method string
REVIEW_GLOB = "place-review.*.jsonl"
MACHINE_PREFIX = "place-check@"
GROUP_DECISIONS = {"accept_google", "pick_alternative", "other_place"}
TOKEN_DECISIONS = {"not_a_place"}
SOURCES = ("google", "alternative", "operator_geocode", "operator_osm", "machine_verified", "pin")
# The OSM fallback for an other_place "A & B" text whose Google answer was rejected (no paid call):
OSM_GAP_KM = (
    0.5  # T_osm, frozen in the place-check addendum (FREEZE section): the two roads must come this close
)
OSM_LABEL_KM = (
    15.0  # place-check's LABEL_KM (A.6 / C.3): a crossing farther from the ad's label centre is another place
)
OSM_CLUSTER_KM = 2.0  # judgement call: crossings this close are ONE interchange (carriageways, ramps)
DESCRIPTORS = {"interchange"}  # a trailing word that names the junction, not the road
CARRIAGEWAYS = (
    "collector",
    "express",
)  # OSM's names for a highway's separate lanes ("Highway 401 Collector")


def review_files(data: Path) -> list[Path]:
    return sorted((data / "decisions").glob(REVIEW_GLOB))


def load_reviews(data: Path) -> list[dict]:
    """Every operator review row, oldest file first. Refuses (ValueError) an unknown item or decision: a new
    decision kind must be given a meaning here before it can change a point."""
    out = []
    for f in review_files(data):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if not line.strip():
                continue
            r = json.loads(line)
            item, dec = r.get("item"), r.get("decision")
            ok = (
                item == "place_group" and dec in GROUP_DECISIONS and r.get("group") and r.get("ad_ids")
            ) or (item == "abbreviation" and dec in TOKEN_DECISIONS and r.get("group"))
            if not ok:
                raise ValueError(f"{f.name}:{n}: unknown place review (item={item!r}, decision={dec!r})")
            if dec == "pick_alternative" and not str(r.get("value") or "").isdigit():
                raise ValueError(f"{f.name}:{n}: pick_alternative needs an alternative index as value")
            if dec == "other_place" and not str(r.get("value") or "").strip():
                raise ValueError(f"{f.name}:{n}: other_place needs the place text as value")
            out.append(r | {"_ref": f"{r.get('batch') or f.stem}#{r.get('ls_task', n)}"})
    return out


def by_ad(reviews: list[dict]) -> dict[str, dict]:
    """ad_id -> its operator place decision; a later row (file order, then line) replaces an earlier one."""
    out: dict[str, dict] = {}
    for r in reviews:
        if r["item"] == "place_group":
            for a in r["ad_ids"]:
                out[str(a)] = r
    return out


def not_place_tokens(reviews: list[dict]) -> set[str]:
    return {r["group"] for r in reviews if r["item"] == "abbreviation" and r["decision"] == "not_a_place"}


def machine_by_key(rows) -> dict[tuple[str, str], dict]:
    return {
        (r["site"], r["ad_id"]): r for r in rows if str(r.get("decided_by", "")).startswith(MACHINE_PREFIX)
    }


def review_query(text: str) -> str:
    """THE cache key of an other_place answer: the operator's text as written (it names its own city and
    province; the geocoder's components already pin country and province). Used by geocode and derive."""
    return " ".join(text.split())


def decisions_sha(data: Path) -> str | None:
    """One digest over every decision file derive reads; export-db refuses when it moved since derive."""
    files = [*review_files(data), data / "decisions" / "place-check.jsonl"]
    files = [f for f in files if f.is_file()]
    if not files:
        return None
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


EMPTY: dict[str, Any] = {
    "place_decision": None,
    "place_decided_by": None,
    "place_decision_status": None,
    "place_review_ref": None,
    "place_point": None,
    "place_point_source": None,
    "place_cache_ref": None,
    "place_gold_v2": None,
    "place_osm": None,
    "location_valid_place": None,
}


def osm_sides(text: str) -> tuple[str, str] | None:
    """ "Example Rd & Highway 999 interchange, Sampleville, SP" -> ("Example Rd", "Highway 999"): the part before
    the first comma, split by the extractor's intersection parser, a trailing junction word dropped."""
    from .commands import _road, intersection_sides

    sides = intersection_sides(text.split(",")[0])
    if not sides:
        return None
    out = []
    for side in sides:
        toks = side.split()
        while len(toks) > 1 and toks[-1].lower() in DESCRIPTORS:
            toks.pop()
        out.append(_road(" ".join(toks)))  # "Hwy 9 interchange" -> "Hwy 9" -> "Highway 9"
    return out[0], out[1]


def osm_crossings(streets, text: str) -> dict:
    """{status, names, crossings: [(gap km, point)]} for an operator "A & B" text, from the OSM street snapshot
    (ODbL): each road's ways by place_check's name rules (exact after normalisation; `base` for an untyped side),
    a highway with its collector/express carriageways; every approach of the two within OSM_GAP_KM."""
    from .place_check import norm

    sides = osm_sides(text)
    if not sides:
        return {"status": "osm_unparsed", "names": None, "crossings": []}
    names = []
    for side in sides:
        found, _ = streets.names(side, "intersection")
        n = norm(side)
        found |= {f"{n} {c}" for c in CARRIAGEWAYS if f"{n} {c}" in streets.by_name} if n in found else set()
        if not found:
            return {"status": "osm_no_way", "names": None, "crossings": []}
        names.append(sorted(found))
    cr = streets.crossings(set(names[0]), set(names[1]), OSM_GAP_KM)
    return {"status": "ok" if cr else "osm_gap_too_large", "names": names, "crossings": cr}


def osm_point(res: dict, centre: dict | None) -> tuple[str, dict | None, dict]:
    """(status, point | None, info) for ONE ad: the crossings within OSM_LABEL_KM of its label centre must be ONE
    interchange (all within OSM_CLUSTER_KM of the closest); its closest approach is the point."""
    info = {"names": res["names"], "gap_km": None, "near_label": 0}
    if res["status"] != "ok":
        return res["status"], None, info
    if not centre:
        return "osm_no_label_centre", None, info
    near = sorted(
        (c for c in res["crossings"] if derive.km(c[1], centre) <= OSM_LABEL_KM), key=lambda c: c[0]
    )
    info["near_label"] = len(near)
    if not near:
        return "osm_not_near_label", None, info
    gap, best = near[0]
    if any(derive.km(c[1], best) > OSM_CLUSTER_KM for c in near):
        return "osm_ambiguous", None, info
    info["gap_km"] = round(gap, 3)
    return "applied", {"lat": round(best["lat"], 7), "lon": round(best["lon"], 7)}, info


def _same(a: dict | None, b: dict | None) -> bool:
    if not (a and b):
        return False
    return abs(a["lat"] - b["lat"]) < 1e-9 and abs(a["lon"] - b["lon"]) < 1e-9


def place_fields(
    row: dict,
    op: dict | None,
    machine: dict | None,
    phrase_ref: dict | None,
    review_result,
    provider: str,
    osm=None,
    bounds=None,
) -> dict:
    """The place_* fields for one ENRICHED row. `phrase_ref` = {provider, query} of the ad's own phrase
    geocode (None without one); `review_result(query)` = the usable cached answer for an other_place text."""
    from .place_check import group_key

    loc, der = row["location"], row["derived"]
    g = loc.get("geocode") or {}
    cands = g.get("candidates") or []
    out = dict(EMPTY)

    def point(p, source, ref=None):
        p = {"lat": p["lat"], "lon": p["lon"]}  # a pin also carries the site's own fields (approximate …)
        out.update(
            place_point=p, place_point_source=source, place_cache_ref=ref, place_decision_status="applied"
        )

    if op:
        out.update(
            place_decision=op["decision"],
            place_decided_by=op.get("reviewer") or "operator",
            place_review_ref=op["_ref"],
            place_gold_v2=bool(op.get("gold_v2")),
        )
        if group_key(row) != op["group"]:
            out["place_decision_status"] = "stale_group"
        elif op["decision"] == "accept_google":
            if g.get("point") and phrase_ref:
                point(g["point"], "google", phrase_ref | {"candidate": None})
            else:
                out["place_decision_status"] = "no_google_point"
        elif op["decision"] == "pick_alternative":
            i = int(op["value"])
            if 1 <= i < len(cands) and cands[i].get("point") and phrase_ref:
                point(cands[i]["point"], "alternative", phrase_ref | {"candidate": i})
            else:
                out["place_decision_status"] = "missing_alternative"
        else:  # other_place
            q = review_query(op["value"])
            res = review_result(q)
            if res is None:
                out["place_decision_status"] = "geocode_pending"
            elif res.get("status") != "OK" or not res.get("point"):
                out["place_decision_status"] = "geocode_failed"
            elif derive.hint_number_house({"kind": "operator", "raw": op["value"]}, res):
                out["place_decision_status"] = "rejected_hint_number"
            else:
                point(res["point"], "operator_geocode", {"provider": provider, "query": q, "candidate": None})
            if out["place_decision_status"] in ("rejected_hint_number", "geocode_failed") and osm:
                status, best, info = osm_point(osm(op["value"]), (loc.get("city_centre") or {}).get("point"))
                out["place_osm"] = {"google": out["place_decision_status"], "status": status, **info}
                if best:
                    point(best, "operator_osm")
                else:
                    out["place_decision_status"] = status
    elif machine:
        out.update(place_decision=machine["decision"], place_decided_by=machine["decided_by"])
        if machine["decision"] == "text_only_verified":
            if _same(machine.get("point"), g.get("point")) and phrase_ref:
                point(g["point"], "machine_verified", phrase_ref | {"candidate": None})
            else:
                out["place_decision_status"] = "stale_point"
        elif machine["decision"] == "auto_alternative":
            i = machine.get("alternative")
            alt = cands[i] if isinstance(i, int) and 0 < i < len(cands) else {}
            if _same(machine.get("point"), alt.get("point")) and phrase_ref:
                point(alt["point"], "alternative", phrase_ref | {"candidate": i})
            else:
                out["place_decision_status"] = "stale_point"
        else:
            out["place_decision_status"] = "review"
    elif der.get("geocode_decision") == "auto_close" and g.get("point") and phrase_ref:
        out.update(place_decision="auto_close", place_decided_by=der["method"])
        point(g["point"], "google", phrase_ref | {"candidate": None})
    elif loc.get("pin") and der.get("geocode_decision") is None and der.get("pin_verdict") != "city_default":
        point(loc["pin"], "pin")

    centre = (loc.get("city_centre") or {}).get("point")
    out["location_valid_place"] = derive.region_validity(
        None, out["place_point"], centre, loc.get("city_label"), bounds
    )[1]
    return out


def apply(row: dict, fields: dict) -> dict:
    """Merge the place fields into derived; a final point out of region is FLAGGED (reason), never removed.
    The pin / text reasons, when present, keep their meaning (they name the raw points)."""
    der = row["derived"]
    der.update(fields)
    if fields["location_valid_place"] is False and der.get("location_invalid_reason") in (
        None,
        "unresolvable",
    ):
        der["location_invalid_reason"] = "out_of_region_place"
    return row


def summary(rows: list[dict]) -> dict:
    import collections

    d = [r["derived"] for r in rows]
    return {
        "by_decision": dict(collections.Counter(x["place_decision"] for x in d if x.get("place_decision"))),
        "by_status": dict(
            collections.Counter(x["place_decision_status"] for x in d if x.get("place_decision_status"))
        ),
        "by_source": dict(
            collections.Counter(x["place_point_source"] for x in d if x.get("place_point_source"))
        ),
        "operator_applied": sum(
            1
            for x in d
            if x.get("place_decided_by") == "operator" and x["place_decision_status"] == "applied"
        ),
        "out_of_region_place": sum(1 for x in d if x.get("location_valid_place") is False),
        "gold_v2_ads": sum(1 for x in d if x.get("place_gold_v2")),
    }
