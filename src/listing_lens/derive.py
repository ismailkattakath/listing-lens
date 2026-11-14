"""Join the latest listing versions with text hints and cached geocodes, then measure the pin.

Pure function of its inputs: rerunning it on the same data yields the same output, and its
`derived.method` tag names everything that produced the numbers."""

from __future__ import annotations

import collections
import copy
import math
import re
import statistics

AT_ADDR_KM, NEAR_ADDR_KM, DEFAULT_MIN_ADS = 1.0, 3.0, 3
# Pre-registered by the analyst (plan-v2), never hand-tuned here. REGION_KM (a pin or text point further than
# this from the ad's own label centre is out of region) is the SITE's: site config [region] km, read on first
# use (module __getattr__), with its pre-registration note beside the value.
AUTO_ACCEPT_KM = 5.0  # a precise text point <= 5 km from a non-default pin is auto-accepted (sha e8c224da…)
IMPRECISE_KINDS = {"area", "street"}  # text-hint kinds that name no point
ADDRESS_KINDS = {"address", "postal"}  # hint kinds whose numbers may rightly be a house number
REJECTED_HINT_NUMBER = "REJECTED:hint_number_as_house_number"


def region_km() -> float:
    from listing_lens import site

    return float(site.get("region", "km"))


def __getattr__(name: str):
    if name == "REGION_KM":  # derive.REGION_KM, as before, but the value is the site config's
        return region_km()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def km(a: dict, b: dict) -> float:
    """Haversine great-circle distance, mean Earth radius 6371 km."""
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp, dl = p2 - p1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def _key(p: dict) -> tuple[float, float]:
    return (p["lat"], p["lon"])


def modal_pins(listings: list[dict]) -> dict[str, tuple[tuple[float, float], int]]:
    """Most common pin per city label: a site 'default pin' would show up as a large mode."""
    by: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in listings:
        loc = r["location"]
        if loc.get("pin") and loc.get("city_label"):
            by[loc["city_label"]][_key(loc["pin"])] += 1
    return {c: cnt.most_common(1)[0] for c, cnt in by.items()}


def verdict(d_text: float | None, d_city: float | None, has_pin: bool, on_mode: bool) -> str:
    """Precedence: at address > on a shared city default > near address > near city centre > neither."""
    if not has_pin:
        return "no_pin"
    if d_text is None:
        return "no_text"
    if d_text <= AT_ADDR_KM:
        return "at_address"
    if on_mode:
        return "city_default"
    if d_city is not None and d_text < d_city and d_text <= NEAR_ADDR_KM:
        return "near_address"
    if d_city is not None and d_city < d_text:
        return "near_city"
    return "neither"


def confidence(has_pin: bool, precise: bool, d_text: float | None) -> str:
    """Pin and text are both advertiser-supplied: agreement proves CONSISTENCY, not truth."""
    if has_pin and precise and d_text is not None:
        return "high" if d_text <= 1 else "medhigh" if d_text <= 3 else "medium" if d_text <= 5 else "low"
    return "medium" if has_pin or precise else "none"


def enrich(
    listing: dict,
    hint: dict | None,
    geocode: dict | None,
    centre: dict | None,
    modes: dict,
    method: str,
    bounds=None,
) -> dict:
    r = copy.deepcopy(listing)
    loc = r["location"]
    if geocode and hint and hint.get("kind") in IMPRECISE_KINDS:
        # An area ("airport area", "Midmere") or a single street is never a point, even when the
        # geocoder answers with a point-like type (Google types an airport as precise).
        geocode = {**geocode, "precise": False}
    if hint_number_house(hint, geocode):
        # A number in a non-address hint (a numbered highway in an intersection) came back as a
        # street_address's house number: Google invented the address. Not a location for this ad.
        geocode = {**geocode, "status": REJECTED_HINT_NUMBER, "precise": False}
    loc["text_hint"] = hint
    loc["geocode"] = geocode
    loc["city_centre"] = centre
    pin = loc.get("pin")
    point = geocode.get("point") if geocode and geocode.get("status") == "OK" else None
    c = centre["point"] if centre else None
    d_text = round(km(pin, point), 3) if pin and point else None
    d_city = round(km(pin, c), 3) if pin and c else None
    mode = modes.get(loc.get("city_label"))
    on_mode = bool(pin and mode and mode[1] >= DEFAULT_MIN_ADS and _key(pin) == mode[0])
    d_tc = round(km(point, c), 3) if point and c else None
    pin_verdict = verdict(d_text, d_city, bool(pin), on_mode)
    label = loc.get("city_label")
    valid_pin, valid_text = region_validity(pin, point, c, label, bounds)
    r["derived"] = {
        "method": method,
        "km_pin_text": d_text,
        "km_pin_city": d_city,
        "km_text_city": d_tc,
        "pin_verdict": pin_verdict,
        "confidence": confidence(bool(pin), bool(geocode and geocode.get("precise")), d_text),
        # Region consistency (operator rule; threshold pre-registered): a point > REGION_KM from the ad's
        # own label centre is not a location for this ad. The raw points stay; this only flags them.
        "location_valid_pin": valid_pin,
        "location_valid_text": valid_text,
        "location_invalid_reason": invalid_reason(valid_pin, valid_text, hint, geocode),
        "location_region_test": region_test(label, c, bounds),
        "geocode_decision": decision(geocode, pin, pin_verdict, d_text, valid_pin, valid_text),
    }
    return r


def _tokens(s: str | None) -> set[str]:
    return {t.lower() for t in re.findall(r"[A-Za-z0-9]+", s or "")}


def hint_number_house(hint: dict | None, geocode: dict | None) -> bool:
    """True when a NON-address hint's answer is a street_address whose house number is a token of the hint
    (raw or alias query), e.g. "Hwy 7 & Example St" -> "7 Example St": the number was the highway's. An
    address / postal hint is exempt, and so is a label that holds a street address."""
    if not (hint and geocode and geocode.get("status") == "OK"):
        return False
    if hint.get("kind") in ADDRESS_KINDS or "street_address" not in (geocode.get("types") or []):
        return False
    if hint.get("kind") == "label":
        from listing_lens.extractors.gta_hints import STREET_ADDR

        if STREET_ADDR.search(hint["raw"]):
            return False
    m = re.match(r"\s*(?:\d+[A-Za-z]?-)?(\d+[A-Za-z]?)\s", geocode.get("formatted") or "")
    return bool(m) and m.group(1).lower() in _tokens(hint["raw"]) | _tokens(hint.get("query"))


def region_validity(
    pin: dict | None, point: dict | None, centre: dict | None, label: str | None = None, bounds=None
) -> tuple[bool | None, bool | None]:
    """THE region rule, shared by derive, place_apply and export-db's phone-import rows. By BOUNDARY when the
    label has a census-division mapping (boundaries.Boundaries: inside the union of its CDs + an edge buffer);
    else by RADIUS (pre-registered): within REGION_KM of the ad's own label centre. None when a point (or, for
    the radius, the centre) is missing. region_test names which test decided."""
    if bounds is not None and bounds.covers(label):
        return bounds.contains(label, pin), bounds.contains(label, point)
    r_km = region_km()
    vp = None if not (pin and centre) else km(pin, centre) <= r_km
    vt = None if not (point and centre) else km(point, centre) <= r_km
    return vp, vt


def region_test(label: str | None, centre: dict | None, bounds=None) -> str | None:
    if bounds is not None and bounds.covers(label):
        return "boundary"
    return "radius" if centre else None


def invalid_reason(valid_pin, valid_text, hint, geocode) -> str | None:
    if valid_pin is False and valid_text is False:
        return "both"
    if valid_pin is False:
        return "out_of_region_pin"
    if valid_text is False:
        return "out_of_region_text"
    if hint and not (geocode and geocode.get("status") == "OK" and geocode.get("point")):
        return "unresolvable"
    return None


def decision(geocode, pin, pin_verdict, d_text, valid_pin, valid_text) -> str | None:
    """The operator's resolution rule for a PRECISE text point (analyst's spec, pre-registered): auto-accept
    only when it lands <= AUTO_ACCEPT_KM from a real (non-default, in-region) pin; everything else is
    reviewed by a human, with the reason. None when there is no precise point to decide on."""
    if not (geocode and geocode.get("precise") and geocode.get("status") == "OK" and geocode.get("point")):
        return None
    if valid_pin is False or valid_text is False:
        return "out_of_region"
    if not pin:
        return "no_pin"
    if pin_verdict == "city_default":
        return "city_default_pin"
    return "auto_close" if d_text is not None and d_text <= AUTO_ACCEPT_KM else "far"


def _spearman(u: list[float], v: list[float]) -> float | None:
    """Spearman's rho, ties averaged: the stdlib's ranked correlation (Python 3.12+), so equal values
    share a rank and the answer cannot depend on row order (the hand-rolled ordinal ranks did both)."""
    if len(u) < 3:
        return None
    try:
        return round(statistics.correlation(u, v, method="ranked"), 3)
    except statistics.StatisticsError:  # e.g. a constant input
        return None


def summarise(rows: list[dict], method: str) -> dict:
    """The numbers a finding is argued from. Rank correlation, because one mis-geocode hundreds of
    km away dominates Pearson's r and cannot dominate Spearman's rho."""
    d = [r["derived"] for r in rows]
    both = [
        x
        for x, r in zip(d, rows, strict=True)
        if x["km_pin_text"] is not None
        and x["km_pin_city"] is not None
        and r["location"]["geocode"]
        and r["location"]["geocode"].get("precise")
        and x.get("location_valid_pin") is not False  # a flagged (out-of-region) point is "no location"
        and x.get("location_valid_text") is not False
    ]

    def block(sub: list[dict]) -> dict:
        dt = [x["km_pin_text"] for x in sub]
        dc = [x["km_pin_city"] for x in sub]
        return {
            "n": len(sub),
            "median_km_pin_text": round(statistics.median(dt), 2) if dt else None,
            "median_km_pin_city": round(statistics.median(dc), 2) if dc else None,
            "share_pin_closer_to_text": round(sum(a < b for a, b in zip(dt, dc, strict=True)) / len(sub), 3)
            if sub
            else None,
        }

    far = [x for x in both if x["km_text_city"] is not None and x["km_text_city"] > 5]
    xs = [x["km_text_city"] for x in both if x["km_text_city"] is not None]
    return {
        "method": method,
        "n_listings": len(rows),
        "n_with_pin": sum(1 for r in rows if r["location"].get("pin")),
        "n_with_text_hint": sum(1 for r in rows if r["location"].get("text_hint")),
        "verdicts": dict(collections.Counter(x["pin_verdict"] for x in d)),
        "region_flags": dict(collections.Counter(x.get("location_invalid_reason") or "valid" for x in d)),
        "geocode_decisions": dict(
            collections.Counter(x["geocode_decision"] for x in d if x.get("geocode_decision"))
        ),
        "confidence": dict(collections.Counter(x["confidence"] for x in d)),
        "precise_pin_and_text": block(both),
        "control_text_gt5km_from_centre": block(far),
        "spearman_precise": {
            "km_pin_text_vs_km_text_city": _spearman(
                xs, [x["km_pin_text"] for x in both if x["km_text_city"] is not None]
            ),
            "km_pin_city_vs_km_text_city": _spearman(
                xs, [x["km_pin_city"] for x in both if x["km_text_city"] is not None]
            ),
        },
        "distinct_pins": len({_key(r["location"]["pin"]) for r in rows if r["location"].get("pin")}),
    }
