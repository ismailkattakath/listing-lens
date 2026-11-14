from datetime import UTC, datetime

import pytest
from scrape_harness import plan

from listing_lens import derive
from listing_lens.extractors.gta_hints import GtaHints


def test_stratified_female_escorts_design():
    start, pages = plan.systematic_pages(530, 15, start=3)
    assert (start, len(pages), pages[:3], pages[-1]) == (3, 36, [3, 18, 33], 528)
    assert plan.sample_weight(530, pages) == pytest.approx(530 / 36)


def test_census_and_seeded_start():
    assert plan.systematic_pages(36, 1)[1] == list(range(1, 37))
    assert plan.systematic_pages(530, 15, seed=7) == plan.systematic_pages(530, 15, seed=7)
    with pytest.raises(ValueError):
        plan.systematic_pages(530, 15, start=16)


def obs(t, h):
    return {"seen_at": t, "card_hash": h}


NOW = datetime(2026, 1, 20, tzinfo=UTC)


def test_refetch_rule():
    listing = {"fetched_at": "2026-01-15T12:00:00Z"}
    assert plan.fetch_reason(None, [obs("2026-01-15T11:00:00Z", "a")], NOW) == "new"
    assert plan.fetch_reason(None, [], NOW) is None
    same = [obs("2026-01-15T11:00:00Z", "a"), obs("2026-01-19T11:00:00Z", "a")]
    assert plan.fetch_reason(listing, same, NOW) is None  # nothing changed, <7 days: never refetch
    changed = [obs("2026-01-15T11:00:00Z", "a"), obs("2026-01-19T11:00:00Z", "b")]
    assert plan.fetch_reason(listing, changed, NOW) == "card_changed"
    later = datetime(2026, 1, 23, tzinfo=UTC)
    assert plan.fetch_reason(listing, same, later) == "stale_7d"
    gone = [obs("2026-01-15T11:00:00Z", "a")]  # stale but no longer listed: no fetch
    assert plan.fetch_reason(listing, gone, later) is None


@pytest.mark.parametrize(
    ("text", "kind", "raw"),
    [
        ("Synthetic | come by A1A 1A1 anytime", "postal", "A1A 1A1"),
        ("Synthetic | incall at 123 Placeholder Rd", "address", "123 Placeholder Rd"),
        ("Synthetic | near Example St & Sample Ave", "intersection", "Example St & Sample Ave"),
        ("Synthetic | close to glenmarket", "landmark", "glenmarket"),
        ("Synthetic | no place at all", None, None),
    ],
)
def test_gta_hints(text, kind, raw):
    hit = GtaHints().extract(text)
    assert (hit["kind"], hit["raw"]) == (kind, raw) if kind else hit is None


def test_verdicts_and_confidence():
    assert derive.verdict(None, None, has_pin=False, on_mode=False) == "no_pin"
    assert derive.verdict(None, 5.0, has_pin=True, on_mode=False) == "no_text"
    assert derive.verdict(0.4, 6.0, has_pin=True, on_mode=False) == "at_address"
    assert derive.verdict(4.0, 0.2, has_pin=True, on_mode=True) == "city_default"
    assert derive.verdict(2.0, 6.0, has_pin=True, on_mode=False) == "near_address"
    assert derive.verdict(8.0, 2.0, has_pin=True, on_mode=False) == "near_city"
    assert derive.confidence(True, True, 0.5) == "high"
    assert derive.confidence(False, False, None) == "none"


def test_haversine_known_distance():
    # One degree of latitude is ~111.19 km on a 6371 km sphere.
    assert derive.km({"lat": 0, "lon": 0}, {"lat": 1, "lon": 0}) == pytest.approx(111.19, abs=0.01)


def test_spearman_averages_tied_ranks():
    # average ranks: u -> [1.5, 1.5, 3], v -> [2, 3, 1]; rho = -1.5 / sqrt(1.5 * 2) = -0.866 (by hand)
    assert derive._spearman([1, 1, 2], [2, 3, 1]) == -0.866
    assert derive._spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0  # no ties: unchanged
    assert derive._spearman([1, 2], [2, 1]) is None  # fewer than 3 pairs
    assert derive._spearman([1, 1, 1], [1, 2, 3]) is None  # a constant input has no rank correlation


def test_spearman_does_not_depend_on_row_order_within_ties():
    # the same three (u, v) pairs; only the order of the two tied-u rows differs
    assert derive._spearman([1, 1, 2], [2, 3, 1]) == derive._spearman([1, 1, 2], [3, 2, 1])
