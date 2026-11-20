"""llens export-db against a THROWAWAY Postgres (conftest.pg: initdb + pg_ctl on a unix socket), synthetic data.

The nix build sets LLENS_REQUIRE_PG=1 so a missing server binary FAILS there instead of skipping."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import FIXTURES
from scrape_harness.cli import EXIT_NETWORK, main

from listing_lens import export_db

FAST = ["--max-tasks-per-minute", "6000", "--approve-politeness-override"]
FIX = ["--driver", "fixture", "--fixtures", str(FIXTURES), *FAST]
GEO = ["--geocoder", "fixture", "--geocode-fixture", str(FIXTURES / "geocode-answers.json")]
SITE = "example.invalid"


@pytest.fixture
def fresh_db(pg):
    import psycopg

    with psycopg.connect(pg, autocommit=True) as c:
        for s in ("li", "li_new", "li_old"):
            c.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
    return pg


def write_phone(d: Path):
    """A SYNTHETIC phone-session import with the real files' shapes, 3 ads: one li also opened, one li
    saw only as a list card (a card the fixture crawl never fetched), one li never saw."""
    base = d / export_db.PHONE_DIR
    (base / "harvest").mkdir(parents=True)
    (base / "bodies").mkdir()
    (base / "geo").mkdir()
    opened = {json.loads(x)["ad_id"] for x in (d / "listings.jsonl").read_text().splitlines() if x.strip()}
    seen = {json.loads(x)["ad_id"] for x in (d / "observations.jsonl").read_text().splitlines() if x.strip()}
    card_only = sorted(seen - opened)[0]
    paths = {
        a: f"/listings/sample-list-a/sample-region/exampleton_synthetic_ad-{a}"
        for a in (sorted(opened)[0], card_only, "9000001")
    }
    cards = [
        {
            "h": p,
            "page": 1,
            "stamp": "2026-01-15 07:00:00",
            "lat": None,
            "lon": None,
            "city": "Exampleton",
            "card": "synthetic",
            "title": f"Phone title {a}",
            "loc": [],
        }
        for a, p in paths.items()
    ]
    (base / "harvest" / "all-today.json").write_text(json.dumps(cards))
    bodies = [
        {"h": p, "title": f"Phone title {a}", "body": "A much longer synthetic phone body text " * 3}
        for a, p in paths.items()
    ]
    (base / "bodies" / "c0.json").write_text(json.dumps(bodies))
    geo = [
        {
            "url": "https://www.example.invalid" + p,
            "title": "t",
            "city_filter": "Exampleton",
            "bumped": "2026-01-15 07:00:00",
            "hint_kind": "intersection",
            "address_as_written": "Example St & Sample Ave",
            "precise": True,
            "lat": 43.71,
            "lon": -79.41,
            "pin_lat": 43.7,
            "pin_lon": -79.4,
            "maps_url": "https://maps.example.invalid/?q=43.71,-79.41",
            "km_to_target": 2.5,
            "d_text": 0.4,
            "d_city": 1.5,
            "pin_class": "at_address",
            "confidence": "high",
        }
        for p in paths.values()
    ]
    (base / "geo" / "ads.jsonl").write_text("".join(json.dumps(g) + "\n" for g in geo))


def write_gold_v3(d: Path, tranche1=(), frame=("9000101",), labelled_ids=None):
    """A SYNTHETIC address-gold-v3 manifest with the real one's shape: a tranche-1 sample beside it (its key
    is the file name, its value the sha256), a tranche-2 frame under derived/samples/ named by
    tranche2_frame.file (NOT part of the mask), and `labelled_ids` (n + sha256 of the sorted ids joined by
    newlines). Ids are synthetic or the fixture's own."""
    import hashlib

    g = d / "eval" / "address-gold-v3"
    g.mkdir(parents=True, exist_ok=True)
    t1 = json.dumps({"schema": "synthetic", "n": len(tranche1), "ad_ids": list(tranche1)}).encode()
    (g / "sample.tranche1.json").write_bytes(t1)
    fp = d / "derived" / "samples" / "zz-gold-frame.jsonl"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fb = "".join(json.dumps({"run_id": "zz-frame", "site": SITE, "ad_id": a}) + "\n" for a in frame).encode()
    fp.write_bytes(fb)
    ids = sorted({str(a) for a in (tranche1 if labelled_ids is None else labelled_ids)})
    m = {
        "schema": "address-gold-manifest.v1",
        "gold": "address-gold-v3",
        "sample.tranche1.json": hashlib.sha256(t1).hexdigest(),
        "tranche2_frame": {
            "file": "derived/samples/zz-gold-frame.jsonl",
            "sha256": hashlib.sha256(fb).hexdigest(),
            "n": len(frame),
        },
        "labelled_ids": {
            "n": len(ids),
            "sha256_sorted_newline_joined": hashlib.sha256("\n".join(ids).encode()).hexdigest(),
        },
    }
    (g / "MANIFEST.json").write_text(json.dumps(m))
    return g


@pytest.fixture
def data(data_dir):
    assert main(["crawl-lists", "--subcategory", "sample-list-a", "--stride", "2", "--start", "1", *FIX]) == 0
    assert main(["fetch-ads", *FIX]) == 0
    assert main(["geocode", *GEO]) == 0
    assert main(["derive", "--provider", "fixture-geocoding"]) == 0
    write_phone(data_dir)
    write_gold_v3(data_dir)
    return data_dir


def q(dsn, sql):
    import psycopg

    with psycopg.connect(dsn) as c:
        return c.execute(sql).fetchall()


def export(dsn, monkeypatch, capsys, *extra):
    monkeypatch.setenv("LLENS_DB_URL", dsn)
    capsys.readouterr()
    code = main(["export-db", "--approve-network", *extra])
    out, err = capsys.readouterr()
    return code, (json.loads(out.splitlines()[-1]) if out.strip() else None), err


def table_hashes(dsn):
    names = [
        r[0]
        for r in q(
            dsn,
            "SELECT table_name FROM information_schema.tables WHERE table_schema='li' AND table_type='BASE TABLE' AND table_name <> 'meta'",
        )
    ]
    return {
        n: q(dsn, f"SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM li.{n} t")[0][0]
        for n in sorted(names)
    }


# ── offline: the network gate and the dry run ─────────────────────────────────────────────────


def test_a_real_export_needs_the_network_approval(data, monkeypatch):
    monkeypatch.setenv("LLENS_DB_URL", "postgresql:///nowhere")
    with pytest.raises(SystemExit) as e:
        main(["export-db"])
    assert e.value.code == EXIT_NETWORK


def test_the_dry_run_builds_every_row_and_needs_no_dsn(data, capsys, monkeypatch):
    monkeypatch.delenv("LLENS_DB_URL", raising=False)
    capsys.readouterr()
    assert main(["export-db", "--dry-run"]) == 0
    s = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert s["rows"]["listings"] == 3 and s["rows"]["import_phone_ads"] == 3
    m = s["merge"]
    assert (m["ads"], m["both_opened"], m["li_listings_only"], m["phone_only"]) == (5, 1, 2, 2)
    assert m["phone_seen_by_li_only_as_cards"] == 1 and m["phone_disjoint_from_li"] == 1


def test_the_dsn_is_never_printed(data, monkeypatch, capsys):
    secret = "pw-SYNTHETIC-MARKER"
    code, _, err = export(f"postgresql://u:{secret}@/db?host=/nonexistent-socket-dir", monkeypatch, capsys)
    assert code == export_db.EXIT_DB and secret not in err and "database error" in err


# ── the merge (pure) ──────────────────────────────────────────────────────────────────────────


def test_merge_rules_and_provenance():
    assert export_db.merge_text("short", "a longer text") == ("a longer text", "both-differ")
    assert export_db.merge_text("same  text", "same text") == ("same  text", "both-agree")
    assert export_db.merge_text(None, "x") == ("x", "phone") and export_db.merge_text("x", None) == (
        "x",
        "li",
    )
    assert export_db.merge_value((1.0, 2.0), (1.0, 2.0000001), export_db._close) == ((1.0, 2.0), "both-agree")
    assert export_db.merge_value((1.0, 2.0), (3.0, 4.0), export_db._close) == ((1.0, 2.0), "both-differ")
    assert export_db.merge_value(None, None) == (None, None)


def test_coordinates_past_their_ttl_are_not_exported(data):
    later = datetime(2027, 1, 1, tzinfo=UTC)  # past the 30-day cache and the import's 2026-12-07
    tables, _ = export_db.build(later)
    assert all(r["geocode_lat"] is None and r["city_centre_lat"] is None for r in tables["derived"])
    assert all(r["lat"] is None for r in tables["geocodes"])
    assert all(r["geocode_lat"] is None for r in tables["import_phone_ads"])
    assert all("lat" not in r["record"]["geo"] for r in tables["import_phone_ads"])
    assert all(
        "maps_url" not in r["record"]["geo"] for r in tables["import_phone_ads"]
    )  # it embeds the point
    assert any(r["pin_lat"] is not None for r in tables["import_phone_ads"])  # the site's pin is not Google's


def test_times_are_utc_and_keep_the_original_string(data):
    tables, _ = export_db.build()
    ph = tables["import_phone_ads"][0]
    assert ph["bumped_at_raw"] == "2026-01-15 07:00:00"  # zone-less site wall time
    assert ph["bumped_at"] == datetime(2026, 1, 15, 12, 0, tzinfo=UTC)  # the synthetic source's zone, UTC-5


# ── against the throwaway server ──────────────────────────────────────────────────────────────


def test_export_is_atomic_idempotent_and_deduplicated(data, fresh_db, monkeypatch, capsys):
    code, s1, _ = export(fresh_db, monkeypatch, capsys)
    assert code == 0 and s1["exported"]
    h1 = table_hashes(fresh_db)
    code, s2, _ = export(fresh_db, monkeypatch, capsys)
    assert code == 0 and s2 == {"unchanged": True, "digest": s1["digest"]}  # the same inputs: a no-op
    code, s3, _ = export(fresh_db, monkeypatch, capsys, "--force")
    assert code == 0 and s3["digest"] == s1["digest"] and table_hashes(fresh_db) == h1  # rebuilt, identical
    schemas = {r[0] for r in q(fresh_db, "SELECT nspname FROM pg_namespace")}
    assert "li" in schemas and not {"li_new", "li_old"} & schemas
    rows, distinct = q(fresh_db, "SELECT count(*), count(DISTINCT (site, ad_id)) FROM li.ads")[0]
    assert rows == distinct == 5  # ONE row per ad across li + phone
    both = q(
        fresh_db, "SELECT title_src, description_src, pin_src FROM li.ads WHERE in_li_listings AND in_phone"
    )[0]
    assert both == ("both-differ", "both-differ", "both-agree")  # titles differ; the phone body is longer
    assert q(fresh_db, "SELECT count(*) FROM li.listings_latest")[0][0] == 3
    assert q(fresh_db, "SELECT value FROM li.meta WHERE key = 'digest'")[0][0] == s1["digest"]
    box = q(fresh_db, "SELECT south, west, north, east FROM li.region WHERE name = 'sample-region'")[0]
    assert box == (43.40, -80.10, 44.10, -78.80)  # from the source's region config, not hand-typed


def test_the_latest_view_lets_a_later_line_win_a_tie(data, fresh_db, monkeypatch, capsys):
    p = data / "listings.jsonl"
    first = json.loads(p.read_text().splitlines()[0])
    first["content"]["title"] = "Synthetic reparsed title"
    first["fetch_reason"] = "reparse"
    with p.open("a") as f:
        f.write(json.dumps(first) + "\n")  # same fetched_at, appended later
    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    (title,) = q(fresh_db, f"SELECT title FROM li.listings_latest WHERE ad_id = '{first['ad_id']}'")[0]
    assert title == "Synthetic reparsed title"


def test_a_failed_load_leaves_the_previous_copy(data, fresh_db, monkeypatch, capsys):
    code, s1, _ = export(fresh_db, monkeypatch, capsys)
    assert code == 0
    real = export_db.build

    def broken(now=None):
        t, m = real(now)
        t["ads"][0]["pin_lat"] = "not a number"  # COPY fails mid-load
        return t, m

    monkeypatch.setattr(export_db, "build", broken)
    code, _, err = export(fresh_db, monkeypatch, capsys, "--force")
    assert code == export_db.EXIT_DB and "database error" in err
    assert q(fresh_db, "SELECT value FROM li.meta WHERE key = 'digest'")[0][0] == s1["digest"]
    assert "li_new" not in {r[0] for r in q(fresh_db, "SELECT nspname FROM pg_namespace")}


def test_the_read_only_role_gets_select_on_the_new_schema(data, fresh_db, monkeypatch, capsys):
    import psycopg

    with psycopg.connect(fresh_db, autocommit=True) as c:
        c.execute("DROP ROLE IF EXISTS li_ro_test")
        c.execute("CREATE ROLE li_ro_test NOLOGIN")
    assert export(fresh_db, monkeypatch, capsys, "--grant-role", "li_ro_test")[0] == 0
    assert q(fresh_db, "SELECT has_table_privilege('li_ro_test', 'li.runs', 'SELECT')")[0][0] is True
    assert q(fresh_db, "SELECT has_table_privilege('li_ro_test', 'li.runs', 'INSERT')")[0][0] is False
    setting = q(
        fresh_db,
        "SELECT setconfig FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole WHERE r.rolname = 'li_ro_test' AND s.setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database())",
    )
    assert setting == [(["search_path=li, public"],)]  # li's tables, then PostGIS's functions in public


def test_the_dashboard_contract(data, fresh_db, monkeypatch, capsys):
    """grafana-engineer's contract: jsonb lists (+ n_media), list_* columns, depth_decile, the wide
    listings_latest, advertisers in LONG form, run logs, finding versions."""
    (data / "derived" / "advertisers.jsonl").write_text(
        json.dumps(
            {
                "schema": "advertiser.v1",
                "advertiser_id": "adv-synthetic",
                "site": SITE,
                "ad_ids": ["1000001", "1000002"],
                "link_kinds": ["phone"],
                "venue_only": False,
                "method": "m",
            }
        )
        + "\n"
    )
    (data / "derived" / "ab-2026-01-15T12_00_00Z.json").write_text(
        json.dumps(
            {
                "schema": "ab.v1",
                "at": "2026-01-15T12:00:00Z",
                "site": SITE,
                "subcategory": "sample-list-a",
                "variants": {
                    "desktop": {
                        "cards": 2,
                        "pins_on_pages": 0,
                        "stopped": "challenge: synthetic",
                        "requests": {},
                        "coverage": {},
                    },
                    "http": {"cards": 3, "pins_on_pages": 0, "stopped": None, "requests": {}, "coverage": {}},
                },
            }
        )
    )
    finding = {
        "id": "H9",
        "status": "open",
        "data": {"n": 3, "scope": "synthetic"},
        "tested_at": "2026-01-15T12:00:00Z",
    }
    (data / "findings.jsonl").write_text(
        json.dumps(finding) + "\n" + json.dumps({**finding, "status": "supported"}) + "\n"
    )
    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    types = dict(
        q(
            fresh_db,
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='li' AND table_name='listings_latest'",
        )
    )
    assert (
        types["media_urls"] == "jsonb"
        and types["service_mode"] == "jsonb"
        and types["fetched_at"] == "timestamp with time zone"
    )
    assert {
        "text_hint_raw",
        "geocode_lat",
        "city_centre_label",
        "derive_method",
        "advertiser_id",
        "pin_approximate",
        "n_media",
        "source_url",
    } <= set(types)
    media, n = q(fresh_db, "SELECT media_urls, n_media FROM li.listings_latest WHERE ad_id = '1000001'")[0]
    assert isinstance(media, list) and n == len(media)
    assert (
        q(fresh_db, "SELECT advertiser_id FROM li.listings_latest WHERE ad_id = '1000002'")[0][0]
        == "adv-synthetic"
    )
    assert q(fresh_db, "SELECT count(*), max(n_ads) FROM li.advertisers")[0] == (2, 2)
    bad = q(
        fresh_db,
        "SELECT count(*) FROM li.observations WHERE depth_decile IS DISTINCT FROM least(9, floor(10.0*(list_page-1)/list_page_count))::int",
    )
    assert bad[0][0] == 0
    assert q(fresh_db, "SELECT count(*) FROM li.run_logs")[0][0] > 0
    assert (
        q(fresh_db, "SELECT count(DISTINCT run_id) FROM li.run_logs")[0][0]
        == q(fresh_db, "SELECT count(*) FROM li.runs")[0][0]
    )
    assert q(fresh_db, "SELECT array_agg(version ORDER BY seq) FROM li.findings WHERE id = 'H9'")[0][0] == [
        1,
        2,
    ]
    assert q(fresh_db, "SELECT count(*) FROM li.runs WHERE requests_ad_pages IS NOT NULL")[0][0] > 0
    # a jsonb column holding a plain string (an A/B variant's stop reason) loads as a JSON string
    assert (
        q(fresh_db, "SELECT stopped #>> '{}' FROM li.ab_variants WHERE variant = 'desktop'")[0][0]
        == "challenge: synthetic"
    )
    assert q(fresh_db, "SELECT stopped FROM li.ab_variants WHERE variant = 'http'")[0][0] is None


def test_text_with_split_or_lone_surrogates_is_storable():
    pair = "𝓐"  # one decorative letter left as its two UTF-16 halves
    assert export_db.clean_text(f"a{pair}b") == "a\U0001d4d0b"
    assert export_db.clean_text("cut\ud835") == "cut�"  # a lone half: replacement character
    assert export_db.clean_text({"k": ["x\ud835"]}) == {"k": ["x�"]}
    assert export_db.clean_text(5) == 5


def test_a_lone_surrogate_in_the_import_loads(data, fresh_db, monkeypatch, capsys):
    p = data / export_db.PHONE_DIR / "bodies" / "c0.json"
    rows = json.loads(p.read_text())
    rows[0]["body"] = "synthetic body cut mid-letter \ud835"
    p.write_text(json.dumps(rows))  # json escapes it as \\ud835, as the real file does
    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    assert q(fresh_db, "SELECT count(*) FROM li.import_phone_ads WHERE body LIKE '%�'")[0][0] == 1


def test_advertiser_links_load(data, fresh_db, monkeypatch, capsys):
    link = {
        "schema": "advertiser_link.v1",
        "advertiser_id": "adv-synthetic",
        "site": SITE,
        "ad_a": "1000001",
        "ad_b": "1000002",
        "link_kind": "phone4",
        "link_key": "0142",
        "advertiser_b": "adv-synthetic",
        "used_for_clustering": True,
    }
    (data / "derived" / "advertiser_links.jsonl").write_text(json.dumps(link) + "\n")
    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    assert q(fresh_db, "SELECT advertiser_id, ad_a, ad_b, link_kind, link_key FROM li.advertiser_links") == [
        ("adv-synthetic", "1000001", "1000002", "phone4", "0142")
    ]


def test_run_logs_join_their_run_even_without_a_run_id_line(data):
    """A log file is named by the FILENAME-SAFE run id (':' -> '_'); its run is found through the run
    record's log_ref, never by guessing from the name."""
    for p in (data / "runs").glob("*.log.jsonl"):  # strip every run_id field: the worst case
        lines = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
        p.write_text("".join(json.dumps({k: v for k, v in x.items() if k != "run_id"}) + "\n" for x in lines))
    tables, _ = export_db.build()
    runs = {r["run_id"] for r in tables["runs"]}
    assert tables["run_logs"] and {r["run_id"] for r in tables["run_logs"]} <= runs


def test_the_import_geo_columns_for_h3(data):
    tables, _ = export_db.build()
    r = tables["import_phone_ads"][0]
    assert {"city_filter", "km_to_target", "d_text", "d_city", "pin_class", "confidence"} <= set(r)
    assert r["city_filter"] == "Exampleton" and r["d_city"] == 1.5


def test_advertisers_ablation_loads(data, fresh_db, monkeypatch, capsys):
    row = {
        "schema": "advertiser_ablation.v1",
        "variant": "-desc",
        "site": SITE,
        "ad_id": "1000001",
        "advertiser_id": "adv-synthetic",
        "n_ads": 1,
        "link_kinds": [],
    }
    (data / "derived" / "advertisers_ablation.jsonl").write_text(json.dumps(row) + "\n")
    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    assert q(
        fresh_db, "SELECT variant, ad_id, advertiser_id, n_ads, link_kinds FROM li.advertisers_ablation"
    ) == [("-desc", "1000001", "adv-synthetic", 1, [])]


def test_derive_advertisers_v2_writes_its_own_file_and_loads(data, fresh_db, monkeypatch, capsys):
    capsys.readouterr()
    assert main(["derive-advertisers", "--method", "v2"]) == 0
    out = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert out["method"] == "v2" and out["ads"] == 3
    assert (data / "derived" / "advertisers_v2.jsonl").exists()
    assert not (data / "derived" / "advertisers.jsonl").exists()  # v2 never writes v1's file
    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    rows = q(
        fresh_db,
        "SELECT count(*), count(DISTINCT (site, ad_id)), bool_and(operator_id LIKE 'v2:%' AND persona_id LIKE 'p2:%') FROM li.advertisers_v2",
    )
    assert rows == [(3, 3, True)]


def write_v2_links(data, pairs, kind="handle"):
    """SYNTHETIC advertiser_v2_link.v1 rows (ad_a, ad_b, operator_id), written AFTER advertisers_v2.jsonl."""
    rows = [
        {
            "schema": "advertiser_v2_link.v1",
            "method": "synthetic",
            "operator_id": op,
            "site": SITE,
            "ad_a": a,
            "ad_b": b,
            "link_kind": kind,
            "link_key": "handle_synthetic",
            "n_keys": 1,
            "edge_kinds": [kind],
            "cross_subcategory": False,
            "persona_edge": True,
        }
        for a, b, op in pairs
    ]
    p = data / "derived" / "advertiser_v2_links.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    v2 = data / "derived" / "advertisers_v2.jsonl"
    if v2.exists() and p.stat().st_mtime < v2.stat().st_mtime:  # coarse clocks: never older than the clusters
        import os

        os.utime(p, (v2.stat().st_atime, v2.stat().st_mtime))


def write_v2_pair(data, a, b):
    """SYNTHETIC advertisers_v2.jsonl + its edge file: ads a and b share operator op-pair (one handle edge),
    every other fixture ad is its own operator. Consistent with the D1 guard."""
    ids = [json.loads(x)["ad_id"] for x in (data / "derived/listings.latest.jsonl").read_text().splitlines()]
    rows = [
        {
            "site": SITE,
            "ad_id": x,
            "operator_id": "op-pair" if x in (a, b) else f"op-{x}",
            "persona_id": f"p-{x}",
            "n_ads": 2 if x in (a, b) else 1,
            "level": "persona",
            "n_names": 1,
            "top_shared_key_share": 0.0,
            "edge_evidence": ["h"] if x in (a, b) else [],
            "persona_edge_evidence": [],
            "method": "synthetic",
        }
        for x in ids
    ]
    (data / "derived" / "advertisers_v2.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    write_v2_links(data, [(*sorted((a, b)), "op-pair")])


def v2_with_an_edge(data):
    """derive v2 on the fixture after giving two of its ads one long SYNTHETIC description (a strong key)."""
    long = "a synthetic description invented for this test only " * 6
    rows = [json.loads(x) for x in (data / "derived/listings.latest.jsonl").read_text().splitlines()]
    for r in rows[:2]:
        r["content"]["description"] = long
    (data / "derived/listings.latest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert main(["derive-advertisers", "--method", "v2"]) == 0
    return [json.loads(x) for x in (data / "derived/advertisers_v2.jsonl").read_text().splitlines()]


def test_v2_links_load_with_the_clusters_derive_writes(data, fresh_db, monkeypatch, capsys):
    v2_with_an_edge(data)
    code, _, err = export(fresh_db, monkeypatch, capsys)
    assert code == 0, err[-600:]
    kinds = dict(
        q(
            fresh_db,
            "SELECT link_kind, bool_and(length(link_key) = 12) FROM li.advertiser_v2_links GROUP BY 1",
        )
    )
    assert kinds.get("desc") is True  # the synthetic description: a fingerprint, never the text
    same = q(
        fresh_db,
        "SELECT count(*), bool_and(l.ad_a < l.ad_b AND a.operator_id = l.operator_id AND b.operator_id = l.operator_id)"
        " FROM li.advertiser_v2_links l"
        " JOIN li.advertisers_v2 a ON a.ad_id = l.ad_a JOIN li.advertisers_v2 b ON b.ad_id = l.ad_b",
    )
    n = q(fresh_db, "SELECT count(*) FROM li.advertiser_v2_links")[0][0]
    assert same == [(n, True)] and n >= 1
    text = q(fresh_db, "SELECT count(*) FROM li.advertiser_v2_links WHERE link_key LIKE '%synthetic%'")
    assert text == [(0,)]


def test_a_missing_or_inconsistent_v2_edge_file_is_refused(data, fresh_db, monkeypatch, capsys):
    import os

    v2 = v2_with_an_edge(data)
    links = data / "derived/advertiser_v2_links.jsonl"
    good = links.read_text()
    links.unlink()
    code, _, err = export(fresh_db, monkeypatch, capsys)
    assert code == 2 and "derive-advertisers --method v2" in err  # missing
    ops: dict = {}
    for r in v2:
        ops.setdefault(r["operator_id"], []).append(r["ad_id"])
    multi = next(sorted(v) for v in ops.values() if len(v) > 1)
    for pairs in (
        [],
        [(multi[0], "9999999", "v2:synthetic")],
    ):  # an operator without edges; an edge off the clusters
        write_v2_links(data, pairs)
        code, _, err = export(fresh_db, monkeypatch, capsys)
        assert code == 2 and "does not match" in err
    links.write_text(good)
    t = (data / "derived/advertisers_v2.jsonl").stat().st_mtime
    os.utime(links, (t - 60, t - 60))  # older than the clusters
    code, _, err = export(fresh_db, monkeypatch, capsys)
    assert code == 2 and "older" in err
    os.utime(links, (t, t))
    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    assert q(fresh_db, "SELECT value #>> '{}' FROM li.meta WHERE key = 'loader'") == [("export-db@25",)]


def test_the_frozen_v1_record_loads_next_to_the_v1_1_default(data, fresh_db, monkeypatch, capsys):
    assert main(["derive-advertisers"]) == 0 and main(["derive-advertisers", "--method", "v1"]) == 0
    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    assert q(fresh_db, "SELECT bool_and(advertiser_id LIKE 'v1.1:%') FROM li.advertisers")[0][0] is True
    assert q(fresh_db, "SELECT bool_and(advertiser_id LIKE 'adv-%'), count(*) FROM li.advertisers_v1")[0] == (
        True,
        3,
    )


VIEW = {"label": "synthetic viewpoint", "lat": 43.6, "lon": -79.5}  # a SYNTHETIC point, never a real one


def test_no_viewpoint_file_means_an_empty_table(data, fresh_db, monkeypatch, capsys):
    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    assert q(fresh_db, "SELECT count(*) FROM li.viewpoint")[0][0] == 0


def test_the_viewpoint_comes_from_the_data_config(data, fresh_db, monkeypatch, capsys):
    (data / "config").mkdir()
    (data / "config" / "viewpoint.json").write_text(json.dumps([VIEW]))
    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    assert q(fresh_db, "SELECT label, lat, lon FROM li.viewpoint") == [("synthetic viewpoint", 43.6, -79.5)]
    (data / "config" / "viewpoint.json").write_text(json.dumps(VIEW))  # a single object is accepted too
    tables, _ = export_db.build()
    assert tables["viewpoint"] == [VIEW]


@pytest.mark.parametrize("bad", [[{"label": "x", "lat": 95, "lon": 0}], [{"lat": 1, "lon": 2}], "not json"])
def test_a_malformed_viewpoint_file_fails_loudly(data, monkeypatch, capsys, bad):
    (data / "config").mkdir()
    (data / "config" / "viewpoint.json").write_text(bad if isinstance(bad, str) else json.dumps(bad))
    monkeypatch.delenv("LLENS_DB_URL", raising=False)
    capsys.readouterr()
    assert main(["export-db", "--dry-run"]) == export_db.EXIT_CONFIG
    assert "viewpoint.json" in capsys.readouterr().err


def test_postgis_geography_columns(data, fresh_db, monkeypatch, capsys):
    """export-db@9 (operator, 2026-11-08): PostGIS in public, a geography(Point) per located row."""
    (data / "config").mkdir()
    (data / "config" / "viewpoint.json").write_text(json.dumps([VIEW]))
    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    assert q(
        fresh_db, "SELECT extnamespace::regnamespace::text FROM pg_extension WHERE extname = 'postgis'"
    ) == [("public",)]
    n, geog = q(fresh_db, "SELECT count(pin_lat), count(pin_geog) FROM li.listings")[0]
    assert n == geog > 0  # every pinned version has its point
    assert (
        q(fresh_db, "SELECT count(*) FROM li.listings WHERE pin_lat IS NULL AND pin_geog IS NOT NULL")[0][0]
        == 0
    )
    assert q(fresh_db, "SELECT count(geog) FROM li.viewpoint")[0][0] == 1
    (d_geog,) = q(
        fresh_db,
        "SELECT round(ST_Distance(v.geog, l.pin_geog)::numeric / 1000, 4) FROM li.viewpoint v, li.listings_latest l WHERE l.pin_geog IS NOT NULL LIMIT 1",
    )[0]
    hav = q(
        fresh_db,
        """SELECT 6371 * 2 * asin(sqrt(power(sin(radians(l.pin_lat - v.lat) / 2), 2)
        + cos(radians(v.lat)) * cos(radians(l.pin_lat)) * power(sin(radians(l.pin_lon - v.lon) / 2), 2)))
        FROM li.viewpoint v, li.listings_latest l WHERE l.pin_geog IS NOT NULL LIMIT 1""",
    )[0][0]
    assert abs(float(d_geog) - hav) <= 0.003 * hav + 1e-6  # spheroid vs sphere: within 0.3 %
    for col in ("pin_geog", "geocode_geog", "city_centre_geog"):  # the view carries them
        assert (
            q(
                fresh_db,
                f"SELECT count(*) FROM information_schema.columns WHERE table_schema='li' AND table_name='listings_latest' AND column_name='{col}'",
            )[0][0]
            == 1
        )


def test_the_granted_roles_search_path_reads_back_li_public_at_both_levels(
    data, fresh_db, monkeypatch, capsys
):
    """Read the setting BACK from the catalogue (not the SQL issued): the role-wide value (pg_roles.rolconfig)
    and the per-database one (pg_db_role_setting) must both be li, public, so neither a reader of rolconfig
    nor a session sees a stale `li` (found live 2026-11-08: rolconfig kept a one-off `search_path=li`)."""
    import psycopg

    with psycopg.connect(fresh_db, autocommit=True) as c:
        c.execute("DROP ROLE IF EXISTS li_ro_sp")
        c.execute("CREATE ROLE li_ro_sp LOGIN")
        c.execute("ALTER ROLE li_ro_sp SET search_path = li")  # the stale one-off, as on the live copy
    assert export(fresh_db, monkeypatch, capsys, "--grant-role", "li_ro_sp")[0] == 0
    rolconfig = q(fresh_db, "SELECT rolconfig FROM pg_roles WHERE rolname = 'li_ro_sp'")[0][0]
    assert "search_path=li, public" in rolconfig
    per_db = q(
        fresh_db,
        "SELECT s.setconfig FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole WHERE r.rolname = 'li_ro_sp' AND s.setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database())",
    )
    assert per_db == [(["search_path=li, public"],)]
    with psycopg.connect(
        fresh_db.replace("user=postgres", "user=li_ro_sp")
    ) as c:  # a real session as that role
        assert c.execute("SHOW search_path").fetchone()[0] == "li, public"


def test_an_unchanged_export_still_applies_the_grant(data, fresh_db, monkeypatch, capsys):
    """The unchanged-digest path must not skip --grant-role (found live 2026-11-08: the no-op returned
    before the grant, so a grant fix never reached the role). Read the setting BACK after a no-op run."""
    import psycopg

    with psycopg.connect(fresh_db, autocommit=True) as c:
        c.execute("DROP ROLE IF EXISTS li_ro_noop")
        c.execute("CREATE ROLE li_ro_noop LOGIN")
    assert (
        export(fresh_db, monkeypatch, capsys, "--grant-role", "li_ro_noop")[0] == 0
    )  # the first, full build
    with psycopg.connect(fresh_db, autocommit=True) as c:
        c.execute(
            "ALTER ROLE li_ro_noop SET search_path = li"
        )  # a stale role-wide value, as on the live copy
        c.execute("REVOKE SELECT ON ALL TABLES IN SCHEMA li FROM li_ro_noop")
    code, out, _ = export(fresh_db, monkeypatch, capsys, "--grant-role", "li_ro_noop")
    assert code == 0 and out["unchanged"] is True  # the data did not change: no rebuild ...
    rolconfig = q(fresh_db, "SELECT rolconfig FROM pg_roles WHERE rolname = 'li_ro_noop'")[0][0]
    assert "search_path=li, public" in rolconfig  # ... but the grant ran
    assert q(fresh_db, "SELECT has_table_privilege('li_ro_noop', 'li.runs', 'SELECT')")[0][0] is True
    assert out.get("granted") == "li_ro_noop"


# ── li services tables (export-db@11) ─────────────────────────────────────────────────────────


def write_services(data, eval_excluded=False, excluded=(1, 0)):
    """SYNTHETIC services records over the fixture's real latest versions: ad A has two mentions of one
    concept (a span cut from its own title) + one refused; ad B has none (it must still get a row)."""
    shutil.copy(
        FIXTURES / "services.synthetic.ttl",
        (data / "vocab").mkdir(exist_ok=True) or data / "vocab" / "services.ttl",
    )
    derived = [json.loads(x) for x in (data / "derived" / "listings.latest.jsonl").read_text().splitlines()]
    a, b = derived[0], derived[1]
    title = a["content"]["title"]

    def rec(r, mentions, adjacent):
        return {
            "schema": "services.v1",
            "site": r["source"]["site"],
            "ad_id": r["ad_id"],
            "fetched_at": r["fetched_at"],
            "parser": None,
            "vocab_version": "0.0.0-test",
            "method": "services-match@1",
            "online_services_offered": ["alpha"],
            "eval_adjacent": adjacent,
            "mentions": mentions,
        }

    span = {"concept": "alpha", "label_matched": title[2:7], "field": "title", "start": 2, "end": 7}
    recs = [
        rec(
            a,
            [
                {**span, "stance": "offered"},
                {**span, "stance": "offered"},
                {**span, "concept": "beta", "stance": "refused"},
            ],
            True,
        ),
        rec(b, [], False),
    ]
    (data / "derived" / "services.latest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    summary = {
        "ran_at": "2026-01-01T00:00:00+00:00",
        "ads": 2,
        "concepts": 2,
        "vocab_version": "0.0.0-test",
        "method": "services-match@1",
        "eval_gate": "unfrozen" if eval_excluded else "frozen",
        "eval_excluded": eval_excluded,
        **(
            {"eval_ads_excluded": excluded[0], "eval_excluded_by_advertiser": excluded[1]}
            if eval_excluded
            else {}
        ),
    }
    (data / "derived" / "services.summary.json").write_text(json.dumps(summary))
    return a, b


def test_services_tables_load_with_every_ad_the_view_and_the_offset_convention(
    data, fresh_db, monkeypatch, capsys
):
    import psycopg

    a, b = write_services(data)
    with psycopg.connect(fresh_db, autocommit=True) as c:
        c.execute("DROP ROLE IF EXISTS li_ro_svc")
        c.execute("CREATE ROLE li_ro_svc NOLOGIN")
    assert export(fresh_db, monkeypatch, capsys, "--grant-role", "li_ro_svc")[0] == 0
    # the denominator: EVERY processed ad, the zero-mention one included
    assert q(
        fresh_db,
        "SELECT ad_id, n_concepts_offered, n_concepts_refused, online_services_offered FROM li.services_ads ORDER BY ad_id",
    ) == sorted([(a["ad_id"], 1, 1, ["alpha"]), (b["ad_id"], 0, 0, ["alpha"])])
    # analyst R1: the eval-adjacency flag, filterable in SQL
    assert q(fresh_db, "SELECT ad_id, eval_adjacent FROM li.services_ads ORDER BY ad_id") == sorted(
        [(a["ad_id"], True), (b["ad_id"], False)]
    )
    # the view: one row per (ad, concept), duplicates collapsed
    assert q(
        fresh_db, "SELECT concept, offered, refused, n_mentions FROM li.services_ad_concepts ORDER BY concept"
    ) == [
        ("alpha", True, False, 2),
        ("beta", False, True, 1),
    ]
    # the convention: code points, 0-based, end-exclusive, on the SAME version (joined on fetched_at)
    cut = q(
        fresh_db,
        'SELECT bool_and(substring(l.title FROM m.start + 1 FOR m."end" - m.start) = m.label_matched), count(*)'
        " FROM li.services_mentions m JOIN li.listings l USING (site, ad_id, fetched_at) WHERE m.field = 'title'",
    )
    assert cut == [(True, 3)]
    assert (
        q(
            fresh_db,
            "SELECT concept, pref_label, sort_order, vocab_version FROM li.services_concepts ORDER BY sort_order",
        )[0][3]
        == "0.0.0-test"
    )
    assert {r[0] for r in q(fresh_db, "SELECT concept FROM li.services_concepts")} == {"alpha", "beta"}
    assert q(fresh_db, "SELECT eval_gate, eval_excluded, eval_ads_excluded, ads FROM li.services_run") == [
        ("frozen", False, 0, 2)
    ]
    assert q(fresh_db, "SELECT count(*) FROM li.services_excluded_strata")[0][0] == 0
    for rel in (
        "services_ads",
        "services_mentions",
        "services_ad_concepts",
        "services_run",
        "services_concepts",
        "services_codes",
        "services_excluded_strata",
    ):
        assert q(fresh_db, f"SELECT has_table_privilege('li_ro_svc', 'li.{rel}', 'SELECT')")[0][0] is True, (
            rel
        )


def strata_setup(data, excluded):
    """Eval ad A (a real derived ad) and its v2-operator mate B (unlisted in the split); eval ad 9000003 seen
    only in the depth sample; discovery 9000002; a recruit-run row for A that must never count."""
    a, b = write_services(data, eval_excluded=True, excluded=excluded)
    A, B = a["ad_id"], b["ad_id"]
    (data / "eval").mkdir(exist_ok=True)
    split = {
        "seed": "s",
        "personas": {
            "pe": {"half": "eval", "subcategory": "sample-list-a", "ad_ids": [A, "9000003"]},
            "pd": {"half": "discovery", "subcategory": "sample-list-a", "ad_ids": ["9000002"]},
        },
    }
    (data / "eval" / "services-persona-split.json").write_text(json.dumps(split))
    derived_ids = [
        json.loads(x)["ad_id"] for x in (data / "derived" / "listings.latest.jsonl").read_text().splitlines()
    ]
    v2 = [
        {  # advertiser.v2's shape (export-db loads it too); only ad_id + operator_id matter here
            "site": "example.invalid",
            "ad_id": x,
            "operator_id": "op-e" if x in (A, B) else f"op-{x}",
            "persona_id": f"p-{x}",
            "n_ads": 2 if x in (A, B) else 1,
            "level": "persona",
            "n_names": 1,
            "top_shared_key_share": 0.0,
            "edge_evidence": ["h"] if x in (A, B) else [],
            "persona_edge_evidence": [],
            "method": "synthetic",
        }
        for x in derived_ids
    ]
    (data / "derived" / "advertisers_v2.jsonl").write_text("".join(json.dumps(r) + "\n" for r in v2))
    write_v2_links(data, [(*sorted((A, B)), "op-e")])
    for rid, sample in (
        ("zz-depth", {"stratify": "depth"}),
        ("zz-recruit", {"stratify": "depth", "select": "recruit"}),
    ):
        (data / "runs" / f"{rid}.json").write_text(
            json.dumps(
                {
                    "run_id": rid,
                    "command": "crawl-lists",
                    "created_at": "2000-01-01T00:00:00Z",
                    "sample": sample,
                }
            )
        )
    (data / "derived" / "samples").mkdir(parents=True, exist_ok=True)
    rows = [
        {"run_id": rid, "site": "example.invalid", "ad_id": ad, "subcategory": "sample-list-a", "decile": dec}
        for rid, ad, dec in [
            ("zz-depth", A, 2),  # eval
            ("zz-depth", B, 2),  # operator mate
            ("zz-depth", "9000003", 5),  # eval, sample-only
            ("zz-depth", "9000002", 2),  # discovery: never counted
            ("zz-recruit", A, 0),  # recruit run: never counted
        ]
    ]
    (data / "derived" / "samples" / "zz-synthetic.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows)
    )


def test_services_excluded_strata_count_eval_ads_and_operator_mates_never_ad_ids(
    data, fresh_db, monkeypatch, capsys
):
    strata_setup(data, excluded=(1, 1))
    code, _, err = export(fresh_db, monkeypatch, capsys)
    assert code == 0, err[-600:]
    assert q(
        fresh_db,
        "SELECT subcategory, decile, n_eval_ads_excluded, n_advertiser_excluded FROM li.services_excluded_strata ORDER BY decile",
    ) == [("sample-list-a", 2, 1, 1), ("sample-list-a", 5, 1, 0)]
    cols = {
        r[0]
        for r in q(
            fresh_db,
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'li' AND table_name = 'services_excluded_strata'",
        )
    }
    assert "ad_id" not in cols
    assert q(
        fresh_db, "SELECT eval_excluded, eval_ads_excluded, eval_excluded_by_advertiser FROM li.services_run"
    ) == [(True, 1, 1)]


def test_services_strata_refuse_when_the_stage_counted_differently(data, fresh_db, monkeypatch, capsys):
    """The stage's summary says 0 operator mates, the current clustering gives 1: a stale services run.
    Export refuses (and changes nothing) rather than publish strata that disagree with services_ads."""
    strata_setup(data, excluded=(1, 0))
    code, _, err = export(fresh_db, monkeypatch, capsys)
    assert code == 2 and "re-run `llens services`" in err


def test_no_services_files_means_empty_services_tables(data, fresh_db, monkeypatch, capsys):
    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    for t in (
        "services_ads",
        "services_mentions",
        "services_run",
        "services_concepts",
        "services_excluded_strata",
    ):
        assert q(fresh_db, f"SELECT count(*) FROM li.{t}")[0][0] == 0, t


def test_services_codes_take_each_codes_most_frequent_label(tmp_path):
    def row(items):
        return json.dumps({"site": {"sample": {"services": items}}}) + "\n"

    (tmp_path / "listings.jsonl").write_text(
        row([{"code": "alpha", "label": "Alpha"}, {"code": None, "label": "skipped"}])
        + row([{"code": "alpha", "label": "Alpha"}, {"code": "beta", "label": "Beta"}])
        + row([{"code": "alpha", "label": "ALPHA"}])
        + row(None)
    )
    assert export_db._code_rows(tmp_path) == [
        {"code": "alpha", "label": "Alpha"},
        {"code": "beta", "label": "Beta"},
    ]


def test_services_concepts_skip_the_stop_scheme_and_take_the_service_version(tmp_path):
    from test_services_match import STOP_TTL

    (tmp_path / "vocab").mkdir()
    (tmp_path / "vocab" / "services.ttl").write_text(STOP_TTL)
    rows = export_db._concept_rows(tmp_path)
    assert [(r["concept"], r["vocab_version"]) for r in rows] == [("zeta", "0.2.0-test")]


def test_exported_geocode_points_survive_the_query_shape(data, fresh_db, monkeypatch, capsys):
    """export-db re-reads each point from the geocode cache (the 30-day coordinate rule), keyed by THE query
    shape. A second copy of that shape silently missed and exported NULL points; one function now builds it."""
    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    ok, with_point = q(
        fresh_db,
        "SELECT count(*) FILTER (WHERE geocode_status = 'OK'), count(*) FILTER (WHERE geocode_status = 'OK' AND geocode_lat IS NOT NULL) FROM li.derived",
    )[0]
    assert ok > 0 and with_point == ok


def test_phone_import_rows_carry_the_same_region_flags(data, fresh_db, monkeypatch, capsys):
    """import_phone_ads gets li's region rule (derive.region_validity, one function): the synthetic import's
    pins sit ~1 km from Exampleton's centre (valid); one moved 100 km north is flagged out_of_region_pin."""
    base = data / export_db.PHONE_DIR / "geo" / "ads.jsonl"
    rows = [json.loads(x) for x in base.read_text().splitlines()]
    rows[0]["pin_lat"] = rows[0]["pin_lat"] + 0.9  # ~100 km north
    base.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    got = q(
        fresh_db,
        "SELECT location_invalid_reason, count(*) FROM li.import_phone_ads WHERE location_valid_pin IS NOT NULL GROUP BY 1 ORDER BY 1",
    )
    assert ("out_of_region_pin", 1) in got and any(r is None for r, _ in got)


def test_place_decisions_are_carried_and_a_stale_derive_is_refused(data, fresh_db, monkeypatch, capsys):
    """export-db@17 carries derive's place_* fields (a Google point re-read through its cache entry), and
    refuses (exit 2, nothing changed) when DATA decisions/ moved since llens derive."""
    rows = [json.loads(x) for x in (data / "derived/listings.latest.jsonl").read_text().splitlines()]
    assert all("place_decision" in r["derived"] for r in rows)
    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    src = dict(q(fresh_db, "SELECT coalesce(place_point_source, '-'), count(*) FROM li.derived GROUP BY 1"))
    assert sum(src.values()) == len(rows)
    with_pt = q(
        fresh_db, "SELECT count(*) FROM li.derived WHERE place_point_source IS NOT NULL AND place_lat IS NULL"
    )
    assert with_pt[0][0] == 0
    assert (
        q(fresh_db, "SELECT count(*) FROM li.listings_latest WHERE place_decision_status IS NOT NULL")[0][0]
        >= 0
    )
    (data / "decisions").mkdir(exist_ok=True)
    (data / "decisions" / "place-review.batch-x.jsonl").write_text(
        json.dumps({"item": "abbreviation", "group": "QX", "ad_ids": ["1"], "decision": "not_a_place"}) + "\n"
    )
    code, _, err = export(fresh_db, monkeypatch, capsys)
    assert code == 2 and "re-run `llens derive`" in err
    assert main(["derive", "--provider", "fixture-geocoding"]) == 0
    assert export(fresh_db, monkeypatch, capsys)[0] == 0


# ── survival: ad_observation_span (export-db@19; @23: read from the harness's liveness ledger) ───────────


def T(h):
    return datetime(2026, 1, 1, tzinfo=UTC) + __import__("datetime").timedelta(hours=h)


def Z(h):
    return T(h).isoformat().replace("+00:00", "Z")


def _obs(aid, h, crawl="c-good"):
    return {
        "site": SITE,
        "ad_id": aid,
        "seen_at": Z(h),
        "crawl_id": crawl,
        "ad_url": f"https://example.invalid/personals/x/synthetic-{aid}",
        "list": {"page": 1, "position": 1},
    }


def _lst(aid, h, reason="new"):
    return {
        "ad_id": aid,
        "fetched_at": Z(h),
        "fetch_reason": reason,
        "source": {"site": SITE, "url": f"https://example.invalid/personals/x/synthetic-{aid}"},
    }


def _gone(aid, h, reason="expired_notice", with_id=True, run="r1"):
    e = {"kind": "gone", "run_id": run, "site": SITE, "at": Z(h), "reason": reason}
    e["url"] = f"https://example.invalid/personals/x/synthetic-{aid}"
    if with_id:
        e["ad_id"] = aid
    return e


RUNS = [
    {"run_id": "c-good", "command": "crawl-lists", "status": "completed", "list": {"status": "ok"}},
    {"run_id": "c-drift", "command": "crawl-lists", "status": "completed", "list": {"status": "parse_drift"}},
]


def test_observation_span_statuses_come_from_the_liveness_ledger():
    """SYNTHETIC ids. 9000001 seen twice in one session (unknown), 9000002 revisited a day later
    (still_listed), 9000003 tombstoned by its EARLIEST gone (gone), 9000004 tombstoned by a url-only gone
    (the ledger's url join), 9000005 held by a parse_failed; a reparse is no sighting."""
    obs = [_obs(a, 0) for a in ("9000001", "9000002", "9000003", "9000004", "9000005")]
    listings = [_lst("9000001", 1), _lst("9000001", 30, "reparse"), _lst("9000002", 24, "recruit")]
    events = [
        _gone("9000003", 100),
        _gone("9000003", 90, "http_410"),  # earlier: the tombstone
        _gone("9000004", 50, with_id=False),
        {"kind": "parse_failed", "run_id": "r2", "site": SITE, "ad_id": "9000005", "at": Z(5)},
        {"kind": "started", "run_id": "r1", "site": SITE, "at": Z(80)},
    ]
    rows = {r["ad_id"]: r for r in export_db.observation_span_rows(obs, listings, events, RUNS)}
    st = {a: (r["status"], r["liveness"], r["gone_reason"]) for a, r in rows.items()}
    assert st == {
        "9000001": ("unknown", "alive", None),
        "9000002": ("still_listed", "alive", None),
        "9000003": ("gone", "tombstoned", "http_410"),
        "9000004": ("gone", "tombstoned", "expired_notice"),
        "9000005": ("unknown", "held", None),
    }
    assert rows["9000001"]["n_observations"] == 2 and rows["9000001"]["last_seen"] == T(1)  # reparse ignored
    assert rows["9000003"]["gone_at"] == T(90) and rows["9000003"]["gone_run"] == "r1"
    assert rows["9000005"]["held_since"] == T(5)
    assert rows["9000002"]["last_seen"] - rows["9000002"]["first_seen"] >= __import__("datetime").timedelta(
        hours=export_db.REVISIT_H
    )


def test_an_ok_fetch_after_a_gone_clears_it():
    """The ledger's rule: an OK fetch after a gone is the ad, live; the tombstone (and its gone_at) is
    cleared. (Before @23 the span kept gone_at; the ledger is now the one rule.)"""
    obs = [_obs("9000006", 0)]
    (r,) = export_db.observation_span_rows(obs, [_lst("9000006", 50)], [_gone("9000006", 10)], RUNS)
    assert (r["status"], r["liveness"], r["gone_at"]) == ("still_listed", "alive", None)


def test_a_bad_crawls_cards_are_no_sighting():
    """ADR-0017 B.2: only a completed crawl with list.status ok counts. 9000007's later card came from a
    parse_drift crawl, so its span ends at its good card; 9000008 was seen ONLY by that crawl: no live
    sighting, so no span row."""
    obs = [_obs("9000007", 0), _obs("9000007", 30, "c-drift"), _obs("9000008", 30, "c-drift")]
    rows = {r["ad_id"]: r for r in export_db.observation_span_rows(obs, [], [], RUNS)}
    assert set(rows) == {"9000007"}
    assert (rows["9000007"]["last_seen"], rows["9000007"]["n_observations"]) == (T(0), 1)


def test_the_span_is_the_ledgers_span():
    """One source of truth: every row's sightings, gone and state equal scrape_harness.liveness's."""
    from scrape_harness import liveness, policy

    obs = [_obs("9000009", 0), _obs("9000009", 30), _obs("9000010", 2), _obs("9000010", 40, "c-drift")]
    listings = [_lst("9000010", 3)]
    events = [_gone("9000009", 60)]
    led = liveness.build(obs, listings, events, RUNS, policy.DEFAULT.rules())
    got = export_db.observation_span_rows(obs, listings, events, RUNS)
    assert [(r["ad_id"], r["liveness"], r["n_observations"]) for r in got] == [
        (s["ad_id"], s["status"], s["n_sightings"]) for s in led.spans()
    ]
    assert [r["first_seen"] for r in got] == [export_db.utc(s["first_seen"]) for s in led.spans()]


def test_ad_id_from_path_ignores_a_query_or_fragment():
    assert export_db.ad_id_from_path("https://example.invalid/a/synthetic-9000007/?x=1#y") == "9000007"
    assert export_db.ad_id_from_path("/a/synthetic") is None


def test_observation_span_loads(data, fresh_db, monkeypatch, capsys):
    code, _, err = export(fresh_db, monkeypatch, capsys)
    assert code == 0, err[-600:]
    n_ads = q(
        fresh_db,
        "SELECT count(DISTINCT (site, ad_id)) FROM (SELECT site, ad_id FROM li.observations UNION SELECT site, ad_id FROM li.listings) u",
    )
    got = q(
        fresh_db,
        "SELECT count(*), count(DISTINCT (site, ad_id)), bool_and(first_seen <= last_seen) FROM li.ad_observation_span",
    )
    assert got == [(n_ads[0][0], n_ads[0][0], True)]


# ── hint_gold_v3 (export-db@19): the address-gold-v3 frames, read through the MANIFEST ─────────


def derived_ids(data):
    return sorted(
        json.loads(x)["ad_id"] for x in (data / "derived/listings.latest.jsonl").read_text().splitlines()
    )


def test_hint_gold_v3_masks_the_labelled_ads_only(data, fresh_db, monkeypatch, capsys):
    labelled, mate, dropped = derived_ids(data)[:3]  # dropped: in the frame, never drawn into a tranche
    write_v2_pair(data, labelled, mate)
    write_gold_v3(data, tranche1=[labelled], frame=[labelled, dropped, "9000101"])
    code, _, err = export(fresh_db, monkeypatch, capsys)
    assert code == 0, err[-600:]
    got = dict(q(fresh_db, "SELECT ad_id, hint_gold_v3 FROM li.listings_latest"))
    assert got[labelled] is True  # a labelled ad
    assert got[mate] is False  # an unlabelled operator-mate: unmasked since export-db@22
    assert got[dropped] is False  # a frame ad that is not labelled
    assert q(fresh_db, "SELECT count(*) FROM li.derived WHERE hint_gold_v3 IS NULL") == [(0,)]


@pytest.mark.parametrize("breakage", ["no_manifest", "tranche_edited", "labelled_ids_mismatch"])
def test_a_missing_or_moved_gold_v3_manifest_stops_the_export(data, fresh_db, monkeypatch, capsys, breakage):
    a = derived_ids(data)[0]
    write_v2_pair(data, *derived_ids(data)[:2])
    g = write_gold_v3(
        data, tranche1=[a], labelled_ids=["9999999"] if breakage == "labelled_ids_mismatch" else None
    )
    if breakage == "no_manifest":
        (g / "MANIFEST.json").unlink()
    elif breakage == "tranche_edited":
        (g / "sample.tranche1.json").write_text(json.dumps({"ad_ids": []}))
    code, _, err = export(fresh_db, monkeypatch, capsys)
    assert code == 2, err[-600:]
    assert "address-gold-v3/MANIFEST.json" in err
    assert q(fresh_db, "SELECT 1 FROM pg_namespace WHERE nspname = 'li'") == []  # nothing was sent


# ── listings_masked (export-db@19): the view generated from gold_mask ───────────────────────────


def test_listings_masked_nulls_a_gold_ads_answer_and_leaves_the_rest(data, fresh_db, monkeypatch, capsys):
    from listing_lens import gold_mask

    a, b = derived_ids(data)[:2]
    write_v2_pair(data, a, derived_ids(data)[2])  # b is not a's operator-mate
    write_gold_v3(data, tranche1=[a])
    import psycopg

    with psycopg.connect(fresh_db, autocommit=True) as c:
        c.execute("DROP ROLE IF EXISTS board_ro_t")
        c.execute("CREATE ROLE board_ro_t")
    code, _, err = export(fresh_db, monkeypatch, capsys, "--grant-role", "board_ro_t")
    assert code == 0, err[-600:]
    cols = [
        r[0]
        for r in q(
            fresh_db,
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'li' AND table_name = 'listings_latest' ORDER BY ordinal_position",
        )
    ]
    geo = {c for c in cols if c.endswith("_geog")}  # geography: compare as text

    def row(rel, ad):
        sel = ", ".join(f"{c}::text" if c in geo else c for c in cols)
        return dict(
            zip(cols, q(fresh_db, f"SELECT {sel} FROM li.{rel} WHERE ad_id = '{ad}'")[0], strict=True)
        )

    gold_l, gold_m, plain_l, plain_m = (
        row("listings_latest", a),
        row("listings_masked", a),
        row("listings_latest", b),
        row("listings_masked", b),
    )
    assert plain_m == plain_l  # a non-gold ad is unchanged
    assert all(gold_m[c] is None for c in gold_mask.MASKED_V3)
    assert {c: v for c, v in gold_m.items() if c not in gold_mask.MASKED_V3} == {
        c: v for c, v in gold_l.items() if c not in gold_mask.MASKED_V3
    }
    assert q(fresh_db, f"SELECT gold_masked, mask_reason FROM li.listings_masked WHERE ad_id = '{a}'") == [
        (True, "gold_v3")
    ]
    assert q(fresh_db, "SELECT has_table_privilege('board_ro_t', 'li.listings_masked', 'SELECT')") == [
        (True,)
    ]


def test_listings_masked_covers_every_location_answer_column(data, fresh_db, monkeypatch, capsys):
    """FAILS when listings_latest gains a place_* / geocode_* / text_hint_* (or one of the listed derived
    location) column that gold_mask does not mask, and when the view's columns drift from the real relation."""
    from listing_lens import gold_mask

    assert export(fresh_db, monkeypatch, capsys)[0] == 0

    def columns(rel):
        return [
            r[0]
            for r in q(
                fresh_db,
                f"SELECT column_name FROM information_schema.columns WHERE table_schema = 'li' AND table_name = '{rel}' ORDER BY ordinal_position",
            )
        ]

    latest = columns("listings_latest")
    answer = {
        c
        for c in latest
        if c.startswith(("place_", "geocode_", "text_hint_"))
        or c
        in (
            "km_pin_text",
            "km_text_city",
            "location_valid_text",
            "location_valid_place",
            "location_invalid_reason",
        )
    } - {"place_gold_v2"}  # the flag itself is not an answer
    assert answer - gold_mask.MASKED == set(), "a location-answer column the mask does not cover"
    assert columns("listings_masked") == [*latest, "gold_masked", "mask_reason"]


# ── board_ro's grants (export-db@21): an allowlist; the gold-bearing tables are withheld ─────────


def ro_role(fresh_db, name="board_ro_t21"):
    import psycopg

    with psycopg.connect(fresh_db, autocommit=True) as c:
        c.execute(f"DROP OWNED BY {name}") if q(
            fresh_db, f"SELECT 1 FROM pg_roles WHERE rolname = '{name}'"
        ) else None
        c.execute(f"DROP ROLE IF EXISTS {name}")
        c.execute(f"CREATE ROLE {name} NOLOGIN")
    return name


def test_every_li_relation_is_classified_readable_or_withheld(data, fresh_db, monkeypatch, capsys):
    from listing_lens.export_db import READABLE, WITHHELD

    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    rels = {
        r[0]
        for r in q(
            fresh_db,
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'li' AND c.relkind IN ('r', 'v', 'm', 'p', 'f')",
        )
    }
    assert not set(READABLE) & set(WITHHELD)
    # a new relation is denied by default: it fails here until someone lists it in one of the two sets
    assert rels == set(READABLE) | set(WITHHELD), rels ^ (set(READABLE) | set(WITHHELD))


def test_board_ro_is_denied_the_gold_tables_and_reads_the_masked_view_and_summary(
    data, fresh_db, monkeypatch, capsys
):
    import psycopg

    role = ro_role(fresh_db)
    assert export(fresh_db, monkeypatch, capsys, "--grant-role", role)[0] == 0
    for rel in ("derived", "listings_latest", "ads", "geocodes"):
        assert q(fresh_db, f"SELECT has_table_privilege('{role}', 'li.{rel}', 'SELECT')") == [(False,)], rel
    with psycopg.connect(fresh_db, autocommit=True) as c:
        c.execute(f"SET ROLE {role}")
        n = c.execute("SELECT count(*) FROM li.listings_masked").fetchone()[0]  # a view: owner's rights
        assert n == len(derived_ids(data))
        c.execute("SELECT * FROM li.geocode_cache_summary").fetchall()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT 1 FROM li.derived LIMIT 1")
    cols = {
        r[0]
        for r in q(
            fresh_db,
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'li' AND table_name = 'geocode_cache_summary'",
        )
    }
    assert not cols & {
        "query",
        "lat",
        "lon",
        "formatted",
        "place_id",
    }  # counts and expiry, never the cache itself


def test_a_rerun_narrows_a_role_that_had_every_table(data, fresh_db, monkeypatch, capsys):
    import psycopg

    role = ro_role(fresh_db, "board_ro_wide")
    assert export(fresh_db, monkeypatch, capsys, "--grant-role", role)[0] == 0
    with psycopg.connect(fresh_db, autocommit=True) as c:
        c.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA li TO {role}")  # the @20 state
    code, out, _ = export(fresh_db, monkeypatch, capsys, "--grant-role", role)
    assert code == 0 and out["unchanged"] is True  # no rebuild, yet the grant ran
    assert q(fresh_db, f"SELECT has_table_privilege('{role}', 'li.derived', 'SELECT')") == [(False,)]
    assert q(fresh_db, f"SELECT has_table_privilege('{role}', 'li.runs', 'SELECT')") == [(True,)]


def test_geocode_cache_summary_is_one_row_with_the_pinned_columns(data, fresh_db, monkeypatch, capsys):
    """grafaner's panel contract (2026-11-13): exactly these names and types, one row over li.geocodes."""
    assert export(fresh_db, monkeypatch, capsys)[0] == 0
    cols = q(
        fresh_db,
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'li' AND table_name = 'geocode_cache_summary' ORDER BY ordinal_position",
    )
    assert cols == [
        ("queries", "integer"),
        ("places", "integer"),
        ("precise_share", "double precision"),
        ("min_expires_at", "timestamp with time zone"),
    ]
    (row,) = q(fresh_db, "SELECT queries, places FROM li.geocode_cache_summary")
    assert row == q(fresh_db, "SELECT count(*)::int, count(DISTINCT place_id)::int FROM li.geocodes")[0]


def test_every_board_query_plans_against_a_real_export(data, fresh_db, monkeypatch, capsys):
    """Every panel target of every shipped board, rendered as Grafana would (All, single and multi values),
    PLANS in Postgres against a real export (EXPLAIN: parse, names and types resolved, nothing executed).
    The guard the 2026-11-13 bug needed: an unqualified `site` in the explorer board's ad-level graph passed every
    static check and failed live with "column reference is ambiguous"."""
    import re

    import psycopg

    from listing_lens.grafana import boards, model

    code, _, err = export(fresh_db, monkeypatch, capsys)
    assert code == 0, err
    failures, n = [], 0
    with psycopg.connect(fresh_db, autocommit=True) as c:
        c.execute("SET search_path = li, public")
        for b in boards.build_all():
            names = {v["name"]: v for v in b["templating"]["list"]}
            for p in model._walk_panels(b["panels"]):
                for t in p.get("targets", []):
                    for state in ("all", "single", "multi"):
                        sql = model.render(t["rawSql"], names, state)
                        sql = re.sub(r"\$__timeFilter\([^)]*\)", "TRUE", sql)
                        n += 1
                        try:
                            c.execute("EXPLAIN " + sql)
                        except psycopg.Error as e:
                            failures.append(
                                f"{b['uid']} {p.get('title', '')[:50]!r} {state}: {str(e).splitlines()[0]}"
                            )
    assert n > 300
    assert not failures, "\n".join(failures)


# ── repeat sightings in one crawl (export-db@24) ─────────────────────────────────────────────


def test_a_repeat_sighting_in_one_crawl_keeps_the_first_and_is_counted(data, fresh_db, monkeypatch, capsys):
    """A contiguous walk can see one ad twice: a bump mid-walk pushes its card one page down. observations
    keeps UNIQUE (site, ad_id, crawl_id), so the export keeps the FIRST sighting (the shallower page) and
    reports how many it dropped; observations.jsonl itself is never rewritten."""
    path = data / "observations.jsonl"
    lines = [x for x in path.read_text().splitlines() if x.strip()]
    first = json.loads(lines[0])
    again = json.loads(lines[0]) | {"list": (first.get("list") or {}) | {"page": 99, "position": 1}}
    path.write_text("\n".join([*lines, json.dumps(again)]) + "\n")
    code, summary, err = export(fresh_db, monkeypatch, capsys)
    assert code == 0, err[-600:]
    got = q(
        fresh_db,
        f"SELECT list_page FROM li.observations WHERE site = '{first['site']}' AND ad_id = '{first['ad_id']}'"
        f" AND crawl_id = '{first['crawl_id']}'",
    )
    assert got == [(first["list"]["page"],)]
    assert summary["merge"]["observations_repeat_dropped"] == 1
    assert len(path.read_text().splitlines()) == len(lines) + 1  # DATA untouched


def test_no_repeat_means_nothing_dropped(data):
    tables, merge = export_db.build()
    assert merge["observations_repeat_dropped"] == 0
    keys = [(r["site"], r["ad_id"], r["crawl_id"]) for r in tables["observations"]]
    assert len(keys) == len(set(keys))


# ── the api schema for the Neon Data API (export-db@25) ─────────────────────────────────────────


def _api_role(dsn, role="api_reader"):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS api CASCADE")  # the shared test server keeps the last test's api
        if c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
            c.execute(f"DROP OWNED BY {role}")
        c.execute(f"DROP ROLE IF EXISTS {role}")
        c.execute(f"CREATE ROLE {role} NOLOGIN")
    return role


def _as(dsn, role, sql):
    """Run one statement as `role`; the SQLSTATE on failure, else the rows."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f"SET ROLE {role}")
        try:
            return c.execute(sql).fetchall()
        except psycopg.Error as e:
            return e.sqlstate


def test_api_role_reads_the_allowlist_through_api_views_only(data, fresh_db, monkeypatch, capsys):
    """--api-role ROLE: one read-only view per READABLE relation in schema api, rebuilt on every load;
    ROLE reads api.* and nothing in li, and cannot write through a view."""
    role = _api_role(fresh_db)
    code, _, err = export(fresh_db, monkeypatch, capsys, "--api-role", role)
    assert code == 0, err[-600:]
    views = {
        r[0]
        for r in q(fresh_db, "SELECT table_name FROM information_schema.views WHERE table_schema = 'api'")
    }
    assert views == set(export_db.READABLE)
    n = q(fresh_db, "SELECT count(*) FROM li.listings")[0][0]
    assert _as(fresh_db, role, "SELECT count(*) FROM api.listings") == [(n,)]
    assert _as(fresh_db, role, "SELECT count(*) FROM li.listings") == "42501"  # no right on li
    for rel in export_db.WITHHELD:
        assert rel not in views
    assert _as(fresh_db, role, "DELETE FROM api.findings") == "42501"  # SELECT only


def test_api_views_survive_a_rebuild_and_an_unchanged_export(data, fresh_db, monkeypatch, capsys):
    role = _api_role(fresh_db)
    assert export(fresh_db, monkeypatch, capsys, "--api-role", role)[0] == 0
    assert export(fresh_db, monkeypatch, capsys, "--api-role", role, "--force")[0] == 0  # li swapped again
    assert _as(fresh_db, role, "SELECT count(*) > 0 FROM api.listings") == [(True,)]
    code, summary, _ = export(fresh_db, monkeypatch, capsys, "--api-role", role)  # digest unchanged
    assert code == 0 and summary["unchanged"] is True and summary["api_role"] == role
    assert _as(fresh_db, role, "SELECT count(*) > 0 FROM api.listings") == [(True,)]


def test_without_api_role_the_api_schema_is_empty(data, fresh_db, monkeypatch, capsys):
    """Fail closed: a load without --api-role leaves no view behind (the old ones go with li_old)."""
    role = _api_role(fresh_db)
    assert export(fresh_db, monkeypatch, capsys, "--api-role", role)[0] == 0
    assert export(fresh_db, monkeypatch, capsys, "--force")[0] == 0
    assert q(fresh_db, "SELECT count(*) FROM information_schema.views WHERE table_schema = 'api'") == [(0,)]
