"""Offline geocoder: answers from a JSON file of {query: Geocoding-API result | null}. It uses the
same mapping (to_block) as the Google geocoder, so a fixture exercises the real path, including
re-resolution by place_id."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .google import COORD_TTL, to_block, with_candidates


class FixtureGeocoder:
    provider = "fixture-geocoding"
    network = False
    coord_ttl = COORD_TTL

    def __init__(self, path: str | None = None) -> None:
        path = path or os.environ.get("LLENS_GEOCODE_FIXTURE")
        if not path:
            raise SystemExit("fixture geocoder: pass --geocode-fixture FILE or set LLENS_GEOCODE_FIXTURE")
        self.answers = json.loads(Path(path).read_text())
        self.calls: list[tuple[str, str]] = []

    def geocode(self, query: str, *, bounds: str | None, admin_area: str | None = None) -> dict:
        self.calls.append(("query", query))
        hit = self.answers.get(query)
        return with_candidates(hit if isinstance(hit, list) else [hit] if hit else [], self.provider)

    def by_place_id(self, place_id: str) -> dict:
        self.calls.append(("place_id", place_id))
        hit = next((a for a in self.answers.values() if a and a.get("place_id") == place_id), None)
        return to_block(hit, self.provider)
