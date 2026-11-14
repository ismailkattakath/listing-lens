"""Google terms: lat/lng cached at most 30 days; place_id kept. Proven on the cache, offline."""

import json
from datetime import UTC, datetime, timedelta

from conftest import FIXTURES
from scrape_harness import store

from listing_lens import geocache
from listing_lens.geocoders.fixture import FixtureGeocoder

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_expired_coordinates_are_stripped_but_place_id_kept(data_dir):
    geo = FixtureGeocoder(str(FIXTURES / "geocode-answers.json"))
    q = "Example St & Sample Ave, Exampleton, Sample Province"
    e = geocache.entry(geo.provider, q, geo.geocode(q, bounds=None), geo.coord_ttl, T0)
    assert e["place_id"] == "synthetic-1" and e["expires_at"] == "2026-01-31T00:00:00Z"
    geocache.save([e], T0)
    assert geocache.usable(geocache.load(T0)[(geo.provider, q)], T0)["point"]

    later = T0 + timedelta(days=31)
    geocache.save(geocache.load(later).values(), later)  # what every `llens geocode` run does
    on_disk = json.loads((data_dir / "geocode-cache.jsonl").read_text())
    assert on_disk["result"]["point"] is None and on_disk["place_id"] == "synthetic-1"
    assert geocache.usable(geocache.load(later)[(geo.provider, q)], later) is None
    assert list(store.read("geocodes"))  # still a valid geocode.v1 record


def test_stale_entry_is_re_resolved_by_place_id(data_dir):
    geo = FixtureGeocoder(str(FIXTURES / "geocode-answers.json"))
    assert geo.by_place_id("synthetic-2")["formatted"].startswith("123 Placeholder Rd")
    assert geo.calls == [("place_id", "synthetic-2")]


def test_google_seam_uses_geopy_and_never_leaks_the_key(monkeypatch):
    from geopy.location import Location

    from listing_lens.geocoders import google

    calls = []
    raw = json.loads((FIXTURES / "geocode-answers.json").read_text())["Exampleton, Sample Province"]

    def fake(self, query=None, **kw):
        calls.append((query, kw))
        if kw.get("place_id") == "boom":
            raise RuntimeError("https://maps.example/?key=SYNTHETIC-KEY")
        loc = Location("x", (raw["geometry"]["location"]["lat"], raw["geometry"]["location"]["lng"]), raw)
        return loc if kw.get("exactly_one", True) else [loc, loc]  # geopy: a LIST when exactly_one=False

    monkeypatch.setenv("GEOCODE_KEY", "SYNTHETIC-KEY")
    monkeypatch.setattr("geopy.geocoders.GoogleV3.geocode", fake)
    g = google.GoogleGeocoder(min_delay=0)
    assert (
        g.geocode("Exampleton, Sample Province", bounds="43.4,-80.1|44.1,-78.8")["place_id"] == "synthetic-3"
    )
    assert calls[0][1]["bounds"] == [(43.4, -80.1), (44.1, -78.8)]
    assert calls[0][1]["exactly_one"] is False and calls[0][1]["components"] == {"country": "XX"}
    assert g.by_place_id("synthetic-3")["status"] == "OK" and calls[1] == (None, calls[1][1])
    err = g.by_place_id("boom")
    assert err["status"] == "ERROR:RuntimeError" and "SYNTHETIC-KEY" not in json.dumps(err)
    on = g.geocode("Exampleton, Sample Province", bounds=None, admin_area="SP")
    assert calls[-1][1]["components"] == {"country": "XX", "administrative_area": "SP"}
    assert len(on["candidates"]) == 2 and on["candidates"][0]["place_id"] == "synthetic-3"
    assert "location_type" in on  # Google's own confidence is recorded (None when the answer lacks it)
