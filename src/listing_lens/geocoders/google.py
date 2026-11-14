"""Google Geocoding through geopy's GoogleV3 (geopy's RateLimiter paces it). The key is read
from the environment variable GEOCODE_KEY ONLY, e.g.

    secret exec GEOCODE_KEY="${LLENS_GEOCODE_KEYCHAIN_ITEM:-llens:geocode:apikey}" -- llens geocode --approve-network

It is never printed, logged or written. Errors are recorded by exception TYPE only, because a
request URL (which carries the key) can appear in an exception's text.

Terms: Google allows caching lat/lng for at most 30 days; place_id may be kept indefinitely
(Google Maps Platform Service Specific Terms). `COORD_TTL` is that limit; cache.py enforces it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

ENDPOINT_NOTE = "maps.googleapis.com/maps/api/geocode"
COORD_TTL = timedelta(days=30)
# Result types that name a point rather than an area's centroid.
PRECISE = {
    "intersection",
    "street_address",
    "premise",
    "subpremise",
    "establishment",
    "point_of_interest",
    "shopping_mall",
    "airport",
    "transit_station",
    "postal_code",
}


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_block(raw: dict | None, provider: str, status: str = "OK") -> dict:
    """Map ONE Geocoding API result (geopy's Location.raw) onto listing.v1 $defs/geocode."""
    block = {"provider": provider, "status": status if raw else "ZERO_RESULTS", "at": now()}
    if not raw:
        return block
    loc = raw["geometry"]["location"]
    types = raw.get("types", [])
    return block | {
        "formatted": raw.get("formatted_address"),
        "point": {"lat": loc["lat"], "lon": loc["lng"]},
        "types": types,
        "precise": bool(PRECISE & set(types)),
        "partial": bool(raw.get("partial_match")),
        "location_type": raw["geometry"].get(
            "location_type"
        ),  # ROOFTOP … APPROXIMATE: Google's own confidence
        "place_id": raw.get("place_id"),
    }


def candidate(raw: dict) -> dict:
    """One alternative result, for a human 'pick the other match' (the same request returns them)."""
    loc = raw["geometry"]["location"]
    return {
        "formatted": raw.get("formatted_address"),
        "point": {"lat": loc["lat"], "lon": loc["lng"]},
        "types": raw.get("types", []),
        "location_type": raw["geometry"].get("location_type"),
        "place_id": raw.get("place_id"),
    }


def with_candidates(results: list[dict], provider: str, status: str = "OK", keep: int = 3) -> dict:
    """The first result as THE answer, plus up to `keep` results (the first included) as candidates."""
    block = to_block(results[0] if results else None, provider, status)
    if results:
        block["candidates"] = [candidate(r) for r in results[:keep]]
    return block


def _bounds(b: str | None) -> list | None:
    if not b:
        return None
    (s, w), (n, e) = (tuple(map(float, part.split(","))) for part in b.split("|"))
    return [(s, w), (n, e)]


class GoogleGeocoder:
    provider = "google-geocoding"
    network = True
    coord_ttl = COORD_TTL
    min_delay = 0.05  # seconds between requests (geopy RateLimiter); the run card's rate

    def __init__(self, min_delay: float = min_delay) -> None:
        item = os.environ.get("LLENS_GEOCODE_KEYCHAIN_ITEM", "llens:geocode:apikey")
        if not os.environ.get("GEOCODE_KEY"):
            raise SystemExit(
                f'GEOCODE_KEY not set. Run under: secret exec GEOCODE_KEY="{item}" -- llens geocode ...'
            )
        from geopy.extra.rate_limiter import RateLimiter
        from geopy.geocoders import GoogleV3

        self._g = GoogleV3(api_key=os.environ["GEOCODE_KEY"], timeout=15)
        # max_retries=0: a failed lookup is recorded and retried on the NEXT run, never hammered.
        self._call = RateLimiter(
            self._g.geocode, min_delay_seconds=min_delay, max_retries=0, swallow_exceptions=False
        )

    def _run(self, admin_area: str | None = None, **kw) -> dict:
        from listing_lens import site

        cc = site.get("geonames", "country")  # the site's ISO country code (site file), also its ccTLD bias
        components = {"country": cc} | ({"administrative_area": admin_area} if admin_area else {})
        try:
            locs = self._call(exactly_one=False, region=cc.lower(), components=components, **kw)
        except Exception as e:  # noqa: BLE001 - never let the URL (with key) reach a message
            return {"provider": self.provider, "status": f"ERROR:{type(e).__name__}", "at": now()}
        return with_candidates([loc.raw for loc in locs or []], self.provider)

    def geocode(self, query: str, *, bounds: str | None, admin_area: str | None = None) -> dict:
        return self._run(admin_area=admin_area, query=query, bounds=_bounds(bounds))

    def by_place_id(self, place_id: str) -> dict:
        return self._run(place_id=place_id)
