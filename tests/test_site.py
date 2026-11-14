"""The site config (site.py): located by LLENS_SITE_CONFIG, else DATA/config/site.toml; it fails CLOSED (an
extractor with no gazetteer would silently find nothing)."""

import json
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import FIXTURES, LIST
from scrape_harness import pipeline
from scrape_harness.interfaces import plugin_class
from scrape_harness.pipeline import PipelineError

from listing_lens import derive, site


@pytest.fixture
def fresh(monkeypatch):
    site._load.cache_clear()
    site._vocab.cache_clear()
    site.source.cache_clear()
    yield monkeypatch
    site._load.cache_clear()
    site._vocab.cache_clear()
    site.source.cache_clear()


def test_the_tests_run_on_the_synthetic_site():
    assert site.path().parent.name == "site" and site.get("places", "context").startswith("Exampleton")
    assert "exampleton" in site.hints_vocab()["cities"]


def test_unset_env_falls_to_data_and_a_missing_file_refuses(fresh, data_dir):
    # With site_overlay on, the harness itself reads the same file to load the pipeline (which locates
    # DATA), so a missing file refuses as early as site.path(); either refusal names DATA/config/site.toml.
    fresh.delenv(site.ENV)
    want = str(data_dir.resolve() / site.DEFAULT)
    with pytest.raises((site.SiteConfigError, PipelineError)) as e:
        site.config()
    assert want in str(e.value) and ("no site config" in str(e.value) or "unreadable" in str(e.value))


def test_a_missing_key_or_vocabulary_refuses(fresh, tmp_path):
    (tmp_path / "site.toml").write_text('[region]\nkm = 60.0\n[vocab]\nhints = "absent.json"\n')
    fresh.setenv(site.ENV, str(tmp_path / "site.toml"))
    with pytest.raises(site.SiteConfigError, match=r"no \[places\] box"):
        site.get("places", "box")
    with pytest.raises(site.SiteConfigError, match="absent"):
        site.hints_vocab()


def test_a_vocabulary_of_another_schema_refuses(fresh, tmp_path):
    (tmp_path / "v.json").write_text(json.dumps({"schema": "other.v9"}))
    (tmp_path / "site.toml").write_text('[vocab]\nhints = "v.json"\n')
    fresh.setenv(site.ENV, str(tmp_path / "site.toml"))
    with pytest.raises(site.SiteConfigError, match="schema"):
        site.hints_vocab()


def test_a_vocabulary_without_the_region_grammar_refuses(fresh, tmp_path):
    """v2: the postal grammar and the frozen @8.2/@8.3 words are the site's; a vocabulary lacking them refuses,
    never falls back to some region's pattern in code."""
    (tmp_path / "v.json").write_text(json.dumps({"schema": site.HINTS_SCHEMA, "postal": "x"}))
    (tmp_path / "site.toml").write_text('[vocab]\nhints = "v.json"\n')
    fresh.setenv(site.ENV, str(tmp_path / "site.toml"))
    with pytest.raises(site.SiteConfigError, match="no fsa, label_noise"):
        site.hints_vocab()


def test_the_extractor_grammar_is_the_vocabulary_s():
    from listing_lens.extractors import gta_hints

    v = site.hints_vocab()
    assert gta_hints.POSTAL.pattern == v["postal"] and gta_hints.FSA.pattern == v["fsa"]
    assert gta_hints.LABEL_NOISE82.search(" ".join(v["label_noise"]["@8.2"]["phrase"]))


def test_region_km_is_the_site_configs(fresh, tmp_path):
    (tmp_path / "site.toml").write_text("[region]\nkm = 12.5\n")
    fresh.setenv(site.ENV, str(tmp_path / "site.toml"))
    assert derive.REGION_KM == 12.5 and derive.region_km() == 12.5
    with pytest.raises(AttributeError):
        derive.NO_SUCH_CONSTANT  # noqa: B018


# ── the configured source: the core reads the site specifics through it (site.source()) ──────────────────


def test_the_source_is_the_site_files_default_site(fresh):
    src, co = site.source(), pipeline.consumer()
    assert type(src) is plugin_class("sources", co.default_site) and site.source() is src  # resolved once
    assert src.host == src.site == co.default_site
    assert isinstance(src.record_key, str) and src.record_key
    assert isinstance(src.tz, ZoneInfo)
    assert src.region(co.default_region) is src.regions[co.default_region]
    assert isinstance(src.upload_re, re.Pattern) and src.upload_re.groups == 1


def _ad(src):
    return src.parse_ad(
        (FIXTURES / "ad-1000001.html").read_text(),
        url=f"{LIST}/exampleton_synthetic_example_ad-1000001",
        fetched_at="2026-01-15T12:00:00Z",
        fetch_reason="new",
    )


def test_the_record_key_is_the_key_the_source_writes(fresh):
    src = site.source()
    lp = src.parse_list(
        (FIXTURES / "list-p1.html").read_text(),
        url=f"{LIST}?page=1",
        subcategory="sample-list-a",
        page=1,
        seen_at="2026-01-15T11:58:00Z",
    )
    assert lp.observations and all(src.record_key in o["card"] for o in lp.observations)
    rec = _ad(src)
    assert rec["source"]["site"] == src.host and src.record_key in rec["site"]
    assert rec["content"]["media_urls"] and all(src.upload_re.match(u) for u in rec["content"]["media_urls"])


def test_the_core_reads_the_site_through_the_source(fresh):
    from listing_lens import advertisers, export_db, panel

    src, co = site.source(), pipeline.consumer()
    assert (co.default_site, co.default_region) == (export_db.PHONE_SITE, export_db.PHONE_REGION)
    assert export_db.SITE_TZ is src.tz and export_db.REGIONS is src.regions
    # a zone-less wall time is the site's own zone
    assert export_db.utc("2026-01-15T08:30:00") == datetime(2026, 1, 15, 8, 30, tzinfo=src.tz).astimezone(UTC)
    assert [r["site"] for r in export_db._region_rows()] == [co.default_site] * len(src.regions)
    listing = {"content": {"title": "", "description": ""}, "site": {src.record_key: {"name": "Somename"}}}
    assert "n:somename" in advertisers.keys(listing)
    media = _ad(src)["content"]["media_urls"]
    listing["content"]["media_urls"] = media
    assert {"img:" + src.upload_re.match(u).group(1) for u in media} <= advertisers.keys_v2(listing)
    assert "n:somename" in panel.card_keys({"card": {"title": "", src.record_key: {"name": "Somename"}}})
