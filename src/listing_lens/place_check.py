"""`llens place-check` (offline): the multi-source check of derive's review cases, by the analyst's frozen rules
(DATA/notes/plan-v2-addendum-placecheck.md, sha256 280703c6…, on plan-v2 f89cd0de…). Every rule below cites
its section there; nothing is tuned here.

Cases (derived/listings.latest.jsonl):
- `no_pin` -> `text_only_verified` when ALL of A.1-A.6 hold (Google + OSM + the label agree), else review;
- `REJECTED:hint_number_as_house_number` -> review (B: OSM gets no say on a rejected point);
- `city_default_pin` -> review (C: a placeholder pin carries no address);
- `far` -> `auto_alternative` when exactly ONE stored alternative passes C.1-C.4, else review.
D(a) `snapped_by_bias` (an UNBIASED candidate > T_osm away) is a hard exclusion; derive's candidates all come
from the one label-biased request, so (a) is recorded as null (not measurable) until an unbiased answer is
stored. D(b) (Google's locality != the label) is recorded only.

T_osm is NOT a constant here: `--calibrate` measures it on the pinned auto_close ads only (the PRE-REG
recipe, max(0.5 km, p95)); the analyst freezes it into the addendum; judging needs `--t-osm`. Without it
criterion A.4 fails CLOSED (every address/intersection goes to review).

Street names are compared EXACTLY after folding case, punctuation and diacritics and expanding the street
type / direction abbreviations (addendum, OSM data). No maintained table exists in the toolchain (no
libpostal/usaddress), so ABBREV is the addendum's list plus Canada Post's common street types already in
gta-hints' SUF. Two rulings (addendum FREEZE), recorded per row as `name_match`: `base`, for an
INTERSECTION side written without a street type, matches the OSM names whose base equals it (the shared node
within T_osm still bounds it); `nodir`, for an ADDRESS whose exact name OSM lacks, matches the name without
its trailing direction.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import statistics
import unicodedata
from collections import defaultdict
from functools import cache
from pathlib import Path

from . import derive

METHOD = "place-check@1"
ADDENDUM_SHA = "96a5f6d2"  # with the FREEZE section (T_osm 0.5 km; base/nodir rulings); 280703c6 before it  # DATA/notes/plan-v2-addendum-placecheck.md (prefix), the rules' source
LABEL_KM = 15.0  # A.6 / C.3
T_OSM_FLOOR = 0.5  # the T_osm recipe's floor (km)
STREETS_SNAPSHOT = "gazetteer/2026-11-08-streets-geom"  # box + 25 km, the addendum's extract
LANDMARK_SNAPSHOT = "gazetteer/2026-11-08"
LANDMARK_FILES = ("osm-malls.json", "osm-stations.json", "osm-other.json")  # never osm-places (A: place=*)

ABBREV = {
    "st": "street", "ave": "avenue", "av": "avenue", "rd": "road", "dr": "drive", "blvd": "boulevard",
    "cres": "crescent", "crt": "court", "ct": "court", "pkwy": "parkway", "ln": "lane", "trl": "trail",
    "sq": "square", "pl": "place", "hwy": "highway", "cir": "circle", "terr": "terrace", "gdns": "gardens",
    "e": "east", "w": "west", "n": "north", "s": "south",
}  # fmt: skip
TYPES = {
    "street", "avenue", "road", "drive", "boulevard", "crescent", "court", "parkway", "lane", "trail",
    "square", "place", "way", "gate", "line", "circle", "terrace", "gardens",
}  # fmt: skip
DIRS = {"east", "west", "north", "south"}
ADDRESS_TYPES = {"street_address", "premise", "subpremise"}
LANDMARK_TYPES = {"establishment", "point_of_interest", "shopping_mall", "transit_station"}
ADDRESS_LOC = {"ROOFTOP", "RANGE_INTERPOLATED"}


# ── names ───────────────────────────────────────────────────────────────────────────────────────


def norm(name: str) -> str:
    """Fold case, punctuation and diacritics; expand the abbreviations (both sides of every comparison)."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    toks = re.findall(r"[a-z0-9]+", s)
    return " ".join(ABBREV.get(t, t) for t in toks)


def base(n: str) -> str:
    """A normalised name without its trailing direction and street type ("yonge street north" -> "yonge")."""
    toks = n.split()
    while toks and toks[-1] in DIRS:
        toks.pop()
    if len(toks) > 1 and toks[-1] in TYPES:
        toks.pop()
    return " ".join(toks)


def typed(n: str) -> bool:
    return any(t in TYPES for t in n.split()) or n.startswith("highway ")


# ── geometry ────────────────────────────────────────────────────────────────────────────────────


def _xy(p: tuple[float, float], lat0: float) -> tuple[float, float]:
    return (p[1] * 111.32 * math.cos(math.radians(lat0)), p[0] * 110.574)


def seg_km(p: dict, a: tuple, b: tuple) -> float:
    """Distance from point p to segment a-b (local equirectangular; exact enough under a few km)."""
    lat0 = p["lat"]
    px, py = _xy((p["lat"], p["lon"]), lat0)
    ax, ay = _xy(a, lat0)
    bx, by = _xy(b, lat0)
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class Streets:
    """OSM street ways (name + geometry) from the snapshot: by normalised name, by base name, and the
    shared nodes (a coordinate on ways of two different names = their intersection)."""

    def __init__(self, ways: list[dict], bbox: tuple[float, float, float, float]):
        self.bbox = bbox
        self.by_name: dict[str, list[list]] = defaultdict(list)
        self.by_base: dict[str, set[str]] = defaultdict(set)
        self.node_names: dict[tuple, set[str]] = defaultdict(set)
        for w in ways:
            n = norm(w["name"])
            g = [tuple(p) for p in w["g"]]
            self.by_name[n].append(g)
            self.by_base[base(n)].add(n)
            for p in g:
                self.node_names[(round(p[0], 7), round(p[1], 7))].add(n)

    @classmethod
    def load(cls, data: Path) -> Streets | None:
        d = data / STREETS_SNAPSHOT
        f = d / "osm-streets-geom.slim.json.gz"
        if not f.exists():
            return None
        m = json.loads((d / "manifest.json").read_text())
        s, w, n, e = (float(x) for x in m["bbox"].split()[0].split(","))
        with gzip.open(f, "rt") as fh:
            return cls(json.load(fh)["ways"], (s, w, n, e))

    def covers(self, p: dict) -> bool:
        s, w, n, e = self.bbox
        return s <= p["lat"] <= n and w <= p["lon"] <= e

    def names(self, side: str, kind: str) -> tuple[set[str], str]:
        """Exact after normalisation; then the analyst's two rulings (addendum FREEZE): `nodir` for an
        ADDRESS only (no exact-name way in the snapshot), `base` for an INTERSECTION side only."""
        n = norm(side)
        if n in self.by_name:
            return {n}, "exact"
        toks = n.split()
        if kind == "address" and len(toks) > 2 and toks[-1] in DIRS and " ".join(toks[:-1]) in self.by_name:
            return {" ".join(toks[:-1])}, "nodir"  # OSM often omits the direction the ad writes
        if kind == "intersection" and not typed(n):
            return set(self.by_base.get(n, ())), "base"
        return set(), "exact"

    def way_km(self, p: dict, names: set[str]) -> float | None:
        best = None
        for n in names:
            for g in self.by_name[n]:
                for a, b in zip(g, g[1:], strict=False) if len(g) > 1 else [(g[0], g[0])]:
                    d = seg_km(p, a, b)
                    best = d if best is None or d < best else best
        return best

    def shared_node_km(self, p: dict, a: set[str], b: set[str]) -> float | None:
        best = None
        for na in a:
            for g in self.by_name[na]:
                for q in g:
                    names = self.node_names[(round(q[0], 7), round(q[1], 7))]
                    if (names & b) - {na}:
                        d = derive.km(p, {"lat": q[0], "lon": q[1]})
                        best = d if best is None or d < best else best
        return best

    def crossings(self, a: set[str], b: set[str], max_km: float) -> list[tuple[float, dict]]:
        """Every place where a way named in `a` comes within max_km of a way named in `b`: (gap km, point), one
        per way pair (its closest approach). A grade-separated interchange shares no node, so this measures the
        geometry itself (segment to segment, local equirectangular) rather than shared nodes."""

        def box(g):
            la, lo = [p[0] for p in g], [p[1] for p in g]
            pad = max_km / 111.0 * 2  # degrees, generous in longitude at this latitude
            return min(la) - pad, min(lo) - pad, max(la) + pad, max(lo) + pad

        bs = [(g, box(g)) for n in b - a for g in self.by_name[n]]
        out = []
        for n in a:
            for ga in self.by_name[n]:
                ba = box(ga)
                for gb, bb in bs:
                    if ba[0] > bb[2] or bb[0] > ba[2] or ba[1] > bb[3] or bb[1] > ba[3]:
                        continue
                    best = _polyline_gap(ga, gb)
                    if best[0] <= max_km:
                        out.append(best)
        return out


def _segs(g: list) -> list[tuple]:
    return list(zip(g, g[1:], strict=False)) if len(g) > 1 else [(g[0], g[0])]


def _seg_gap(a1, a2, b1, b2) -> tuple[float, dict]:
    """(km, point) of the closest approach of segments a1-a2 and b1-b2: their crossing when they cross, else the
    midpoint of the nearest endpoint and its projection on the other segment."""
    lat0 = (a1[0] + b1[0]) / 2
    (ax, ay), (bx, by), (cx, cy), (dx, dy) = (_xy(p, lat0) for p in (a1, a2, b1, b2))
    den = (bx - ax) * (dy - cy) - (by - ay) * (dx - cx)
    if den:
        t = ((cx - ax) * (dy - cy) - (cy - ay) * (dx - cx)) / den
        u = ((cx - ax) * (by - ay) - (cy - ay) * (bx - ax)) / den
        if 0 <= t <= 1 and 0 <= u <= 1:
            lat, lon = a1[0] + t * (a2[0] - a1[0]), a1[1] + t * (a2[1] - a1[1])
            return 0.0, {"lat": lat, "lon": lon}
    best = None
    for p, (q1, q2) in ((a1, (b1, b2)), (a2, (b1, b2)), (b1, (a1, a2)), (b2, (a1, a2))):
        pt = {"lat": p[0], "lon": p[1]}
        d = seg_km(pt, q1, q2)
        if best is None or d < best[0]:
            best = (d, pt, q1, q2)
    d, pt, q1, q2 = best
    # the projection, for the midpoint
    qx1, qy1 = _xy(q1, pt["lat"])
    qx2, qy2 = _xy(q2, pt["lat"])
    px, py = _xy((pt["lat"], pt["lon"]), pt["lat"])
    vx, vy = qx2 - qx1, qy2 - qy1
    t = (
        0.0
        if vx == vy == 0
        else max(0.0, min(1.0, ((px - qx1) * vx + (py - qy1) * vy) / (vx * vx + vy * vy)))
    )
    proj = (q1[0] + t * (q2[0] - q1[0]), q1[1] + t * (q2[1] - q1[1]))
    return d, {"lat": (pt["lat"] + proj[0]) / 2, "lon": (pt["lon"] + proj[1]) / 2}


def _polyline_gap(ga: list, gb: list) -> tuple[float, dict]:
    best = None
    for a1, a2 in _segs(ga):
        for b1, b2 in _segs(gb):
            r = _seg_gap(a1, a2, b1, b2)
            if best is None or r[0] < best[0]:
                best = r
                if r[0] == 0.0:
                    return best
    return best


def load_landmarks(data: Path) -> list[dict]:
    out = []
    for f in LANDMARK_FILES:
        p = data / LANDMARK_SNAPSHOT / f
        if not p.exists():
            continue
        for e in json.loads(p.read_text())["elements"]:
            t = e.get("tags", {})
            pt = e.get("center") or ({"lat": e["lat"], "lon": e["lon"]} if "lat" in e else None)
            names = {norm(t[k]) for k in ("name", "alt_name", "short_name", "official_name") if t.get(k)}
            if pt and names:
                out.append({"names": names, "point": {"lat": pt["lat"], "lon": pt["lon"]}})
    return out


# ── the hint's content ──────────────────────────────────────────────────────────────────────────


def content(hint: dict) -> tuple[str, dict]:
    """(kind to judge by, parts): a label is judged by the kind of its content (A.1)."""
    from .commands import intersection_sides
    from .extractors.gta_hints import INTERSECTION, LANDMARK, STREET_ADDR

    kind, raw = hint["kind"], hint["raw"]
    if kind == "label":
        if m := STREET_ADDR.search(raw):
            kind, raw = "address", m.group(0)
        elif m := INTERSECTION.search(raw):
            kind, raw = "intersection", m.group(0)
        elif LANDMARK.search(raw):
            kind = "landmark"
        else:
            return "label_unjudgeable", {}
    if kind == "address":
        return kind, {"street": re.sub(r"^\s*\S*\d\S*\s+", "", raw)}
    if kind == "intersection":
        sides = intersection_sides(raw)
        return (kind, {"sides": sides}) if sides else ("label_unjudgeable", {})
    if kind == "landmark":
        return kind, {"name": hint.get("query", raw).split(",")[0]}
    return kind, {}


@cache
def _admin_rx() -> re.Pattern:
    """The site's administrative-area codes (its source's admin_area() over its regions), as a part's prefix."""
    from . import site

    src = site.source()
    codes = sorted({a for r in src.regions if (a := src.admin_area(r))})
    if not codes:
        raise SystemExit("place-check: the site source names no administrative area for any region")
    return re.compile(rf"(?:{'|'.join(map(re.escape, codes))})\b")


def locality(formatted: str | None) -> str | None:
    """Google's locality: the part before the administrative area in "…, <locality>, <area> <postal>, <country>"."""
    parts = [x.strip() for x in (formatted or "").split(",")]
    for i, x in enumerate(parts):
        if _admin_rx().match(x) and i > 0:
            return parts[i - 1]
    return None


# ── the criteria ────────────────────────────────────────────────────────────────────────────────


def criteria(
    hint: dict, cand: dict, all_cands: list[dict], centre: dict | None, streets, landmarks, t_osm
) -> dict:
    """A.1-A.6 for ONE candidate (the answer, or a far ad's alternative). Each value: True / False / None
    (not applicable). `fail` is the first failing criterion's reason, None when all hold."""
    kind, parts = content(hint)
    p = cand.get("point")
    types = set(cand.get("types") or [])
    r: dict = {"judged_as": kind}
    # A.1 type match
    if kind == "address":
        r["c1_type"] = bool(types & ADDRESS_TYPES) and cand.get("location_type") in ADDRESS_LOC
    elif kind == "intersection":
        r["c1_type"] = "intersection" in types
    elif kind == "landmark":
        r["c1_type"] = bool(types & LANDMARK_TYPES)
    elif kind == "postal":
        r["c1_type"], r["fail"] = False, "area_only"
    else:
        r["c1_type"] = False
    # A.2 no number collision (the same rule derive applies)
    r["c2_number"] = not derive.hint_number_house(
        hint, {"status": "OK", "types": list(types), "formatted": cand.get("formatted")}
    )
    # A.3 unambiguous
    others = [c["point"] for c in all_cands[1:] if c.get("point")]
    r["c3_unambiguous"] = (
        None
        if t_osm is None and others
        else all(derive.km(p, o) <= t_osm for o in others)
        if others
        else True
    )
    # A.4 OSM agrees (address, intersection) / A.5 landmark
    r["osm_coverage"] = bool(streets and p and streets.covers(p))
    if kind in ("address", "intersection"):
        r["c4_osm_km"], r["name_match"] = osm_ref_km(kind, parts, p, streets)
        r["c4_osm"] = (
            None
            if t_osm is None
            else r["c4_osm_km"] is not None and r["osm_coverage"] and r["c4_osm_km"] <= t_osm
        )
    if kind == "landmark":
        n = norm(parts["name"])
        hits = [
            lm
            for lm in landmarks
            if n in lm["names"] and centre and derive.km(lm["point"], centre) <= LABEL_KM
        ]
        r["c5_landmark_matches"] = len(hits)
        r["c5_landmark"] = len(hits) == 1
    # A.6 label consistency
    r["c6_label_km"] = round(derive.km(p, centre), 3) if p and centre else None
    r["c6_label"] = r["c6_label_km"] is not None and r["c6_label_km"] <= LABEL_KM
    r.setdefault("fail", first_fail(kind, r))
    return r


def osm_ref_km(kind: str, parts: dict, p: dict | None, streets) -> tuple[float | None, str | None]:
    """The criterion-4 reference distance: the shared node for an intersection, the nearest point on a
    same-name way for an address. (None, …) when OSM has no such name / node or there is no snapshot."""
    if not (streets and p):
        return None, None
    if kind == "address":
        names, how = streets.names(parts["street"], "address")
        d = streets.way_km(p, names)
    else:
        (a, how_a), (b, how_b) = (streets.names(s, "intersection") for s in parts["sides"])
        how = next((h for h in ("base", "nodir") if h in (how_a, how_b)), "exact")
        d = streets.shared_node_km(p, a, b) if a and b else None
    return (round(d, 4) if d is not None else None), how


def first_fail(kind: str, r: dict) -> str | None:
    if kind == "label_unjudgeable":
        return "label_unjudgeable"
    order = [("c1_type", "type_mismatch"), ("c2_number", "number_collision"), ("c3_unambiguous", "ambiguous")]
    if kind in ("address", "intersection"):
        if not r["osm_coverage"]:
            order.append(("osm_coverage", "no_osm_coverage"))
        order.append(("c4_osm", "osm_disagrees" if r.get("c4_osm") is False else "osm_unjudged"))
    if kind == "landmark":
        order.append(
            (
                "c5_landmark",
                "landmark_ambiguous" if r.get("c5_landmark_matches", 0) > 1 else "landmark_unmatched",
            )
        )
    order.append(("c6_label", "label_far"))
    for key, reason in order:
        if r.get(key) is not True:
            return reason
    return None


# ── decisions ───────────────────────────────────────────────────────────────────────────────────


def case_of(row: dict) -> str | None:
    g = row["location"].get("geocode") or {}
    if g.get("status") == derive.REJECTED_HINT_NUMBER:
        return "rejected_hint_number"
    return {"no_pin": "no_pin", "far": "far", "city_default_pin": "city_default_pin"}.get(
        row["derived"].get("geocode_decision")
    )


def decide(row: dict, streets, landmarks, t_osm: float | None) -> dict | None:
    case = case_of(row)
    if not case:
        return None
    loc = row["location"]
    hint, g, centre = (
        loc.get("text_hint") or {},
        loc.get("geocode") or {},
        (loc.get("city_centre") or {}).get("point"),
    )
    label = loc.get("city_label")
    out = {
        "site": row["source"]["site"],
        "ad_id": row["ad_id"],
        "case": case,
        "hint_kind": hint.get("kind"),
        "method": METHOD,
        "rules_sha": ADDENDUM_SHA,
        "t_osm_km": t_osm,
        "snapped_by_bias_a": None,  # no unbiased candidate is stored (module docstring)
        "locality_mismatch_b": _loc_mismatch(g.get("formatted"), label),
    }
    cands = g.get("candidates") or (
        [{k: g.get(k) for k in ("formatted", "point", "types", "location_type")}] if g.get("point") else []
    )
    if case == "rejected_hint_number":
        return out | {"decision": "review", "reason": "rejected_hint_number"}
    if case == "city_default_pin":
        return out | {"decision": "review", "reason": "city_default_pin"}
    if case == "no_pin":
        c = criteria(hint, cands[0] if cands else g, cands, centre, streets, landmarks, t_osm)
        if c["fail"] is None:
            return out | {
                "decision": "text_only_verified",
                "reason": None,
                "criteria": c,
                "point": g["point"],
            }
        return out | {"decision": "review", "reason": f"sources_disagree:{c['fail']}", "criteria": c}
    # far: C.1-C.4 over the stored alternatives
    pin = loc.get("pin")
    ok = []
    for i, alt in enumerate(cands[1:], 1):
        if not (alt.get("point") and pin) or derive.km(alt["point"], pin) > derive.AUTO_ACCEPT_KM:
            continue
        c = criteria(hint, alt, [alt], centre, streets, landmarks, t_osm)
        in_region = centre is not None and derive.km(alt["point"], centre) <= derive.REGION_KM
        if c["c1_type"] and c["c2_number"] and in_region and c["c6_label"]:
            ok.append((i, alt, c))
    near = sum(
        1 for a in cands[1:] if a.get("point") and pin and derive.km(a["point"], pin) <= derive.AUTO_ACCEPT_KM
    )
    if len(ok) == 1 and near == 1:
        i, alt, c = ok[0]
        return out | {
            "decision": "auto_alternative",
            "reason": None,
            "alternative": i,
            "criteria": c,
            "point": alt["point"],
        }
    reason = (
        "ambiguous"
        if near > 1
        else ("no_alternative_near_pin" if near == 0 else "alternative_fails_criteria")
    )
    return out | {"decision": "review", "reason": reason}


def _loc_mismatch(formatted: str | None, label: str | None) -> bool | None:
    lo = locality(formatted)
    if not (lo and label):
        return None
    return norm(lo) not in norm(label) and norm(label) not in norm(lo)


def group_key(row: dict) -> str:
    """The review group: one human decision per distinct (normalised hint, result) pair fans out to every ad
    in it (analyst, addendum OUTCOME). A hash, so the pending file still holds no text."""
    import hashlib

    loc = row["location"]
    g = loc.get("geocode") or {}
    p = g.get("point") or {}
    key = [
        norm((loc.get("text_hint") or {}).get("raw") or ""),
        g.get("status"),
        g.get("formatted"),
        round(p["lat"], 5) if p else None,
        round(p["lon"], 5) if p else None,
    ]
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16]


def span(row: dict) -> dict | None:
    """Where the hint sits in the ad: field + character offsets in the ORIGINAL field text (never the
    text). gta-hints matches NFKC text, so a phrase found only after NFKC is located in NFKC(field)."""
    raw = (row["location"].get("text_hint") or {}).get("raw")
    if not raw:
        return None
    for field in ("title", "description"):
        t = row["content"].get(field) or ""
        if (i := t.find(raw)) >= 0:
            return {"field": field, "start": i, "end": i + len(raw), "basis": "original"}
    for field in ("title", "description"):
        t = unicodedata.normalize("NFKC", row["content"].get(field) or "")
        if (i := t.find(raw)) >= 0:
            return {"field": field, "start": i, "end": i + len(raw), "basis": "nfkc"}
    return None


def calibrate(rows: list[dict], streets) -> dict:
    """The PRE-REG T_osm recipe: Google's point -> the criterion-4 OSM reference, on auto_close ads ONLY."""
    ds, by_kind, missing = [], defaultdict(int), defaultdict(int)
    for r in rows:
        if r["derived"].get("geocode_decision") != "auto_close":
            continue
        kind, parts = content(r["location"]["text_hint"])
        if kind not in ("address", "intersection"):
            continue
        d, _ = osm_ref_km(kind, parts, r["location"]["geocode"]["point"], streets)
        if d is None:
            missing[kind] += 1
            continue
        ds.append(d)
        by_kind[kind] += 1
    if not ds:
        return {"n": 0, "missing": dict(missing)}
    ds.sort()
    p95 = statistics.quantiles(ds, n=20, method="inclusive")[-1] if len(ds) > 1 else ds[0]
    return {
        "n": len(ds),
        "by_kind": dict(by_kind),
        "no_osm_reference": dict(missing),
        "p50_km": round(statistics.median(ds), 4),
        "p95_km": round(p95, 4),
        "max_km": round(ds[-1], 4),
        "t_osm_km": round(max(T_OSM_FLOOR, p95), 4),
    }
