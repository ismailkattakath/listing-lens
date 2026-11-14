"""The geocode cache, compliant with a provider's coordinate-cache limit (Google: 30 days).

Keyed by (provider, query) for lookup, but the DURABLE key is place_id: when an entry's
coordinates expire, it is re-resolved by place_id (not re-searched by text), and the cache file is
rewritten so no expired coordinate survives on disk. Pure functions plus one rewrite.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from scrape_harness import store

FMT = "%Y-%m-%dT%H:%M:%SZ"


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fresh(entry: dict, now: datetime) -> bool:
    return _t(entry["expires_at"]) > now


def strip_expired(entry: dict, now: datetime) -> dict:
    """Drop coordinates past their TTL; keep place_id, the query and the non-coordinate facts."""
    if fresh(entry, now) or entry["result"].get("point") is None:
        return entry
    e = copy.deepcopy(entry)
    e["result"]["point"] = None
    return e


def entry(provider: str, query: str, result: dict, ttl: timedelta, now: datetime) -> dict:
    return {
        "schema": "geocode.v1",
        "provider": provider,
        "query": query,
        "place_id": result.get("place_id"),
        "resolved_at": now.strftime(FMT),
        "expires_at": (now + ttl).strftime(FMT),
        "result": result,
    }


def load(now: datetime | None = None) -> dict[tuple[str, str], dict]:
    now = now or datetime.now(UTC)
    return {(g["provider"], g["query"]): strip_expired(g, now) for g in store.read("geocodes")}


def usable(entry: dict | None, now: datetime | None = None) -> dict | None:
    """The result block only while its coordinates are within TTL; else None (derive treats it as missing)."""
    if not entry or not fresh(entry, now or datetime.now(UTC)):
        return None
    return entry["result"]


def save(entries: Iterable[dict], now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    return store.replace("geocodes", [strip_expired(e, now) for e in entries])
