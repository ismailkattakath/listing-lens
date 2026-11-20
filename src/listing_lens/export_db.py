"""`llens export-db`: rebuild the DERIVED Postgres copy of DATA (schema `li`) in one atomic step (ADR-0014).

- Reads DATA only; never writes it. The copy holds no fact of its own: every row is rebuilt from the
  JSONL files, so the DDL (db/schema/*.sql) is applied to a FRESH schema each time (no migrations).
- Atomic: load everything into `li_new`, then ONE transaction renames li -> li_old, li_new -> li and
  drops li_old. A reader (Grafana) sees the old copy or the new one, never half of either.
- Idempotent: `meta.digest` is a SHA-256 over the DDL and every row; the same inputs give the same
  digest, and an unchanged digest is a no-op unless --force.
- Google's coordinate TTL holds in the copy too: a geocode point is exported only while the cache
  entry is usable (geocache.usable), and the phone import's Google points only until 2026-12-07.
- The phone-session import is merged with li into `ads`: ONE row per (site, ad_id), each field from
  either side with its provenance (MERGE below). `observations` stays its own table.
- Networked (our own database, network class "paid"): the DSN comes ONLY from an environment variable
  (`secret exec LLENS_DB_URL=<keychain item> -- llens export-db --approve-network`) and is never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from scrape_harness import liveness, pipeline, policy, store
from scrape_harness.cli import card, need_network
from scrape_harness.interfaces import load

from . import derive, geocache, site
from . import place_apply as pa
from .commands import geocode_query

LOADER = "export-db@26"  # @2: dashboard contract; @3: advertiser_links, search_path; @4: run_logs via log_ref, H3 columns; @5: advertisers_ablation; @6: advertisers_v2; @7: advertisers = v1.1, advertisers_v1 frozen; @8: viewpoint; @9: PostGIS geography columns; @10: grant also on an unchanged export; @11: services tables; @12: services_excluded_strata over the depth run only; @13: eval exclusion by v2 operator; @14: services_ads.eval_adjacent; @15: region flags + geocode_decision + location_type on derived; @16: the same region flags on import_phone_ads; @17: place decisions applied (place_* + location_valid_place on derived), refused when DATA decisions/ moved since li derive; @18: location_region_test (boundary | radius) on derived and import_phone_ads; @19: advertiser_v2_links (why v2 merged two ads), refused when inconsistent with advertisers_v2; ad_observation_span (survival: first/last sighting, gone); hint_gold_v3 on derived + listings_latest (the address-gold-v3 frames, via its MANIFEST); listings_masked view generated from gold_mask.view_select(); @20: hint_gold_v3 = the MANIFEST's d6_mask (labelled ads + their current v2 operator-mates), not the whole frame; @21: board_ro reads an explicit allowlist (READABLE) — derived, listings_latest, ads and geocodes are revoked; geocode_cache_summary view; @22: hint_gold_v3 = the labelled ads only (operator-mates are no longer masked on the boards); @23: ad_observation_span read from scrape_harness.liveness (good crawls only, the ledger's gone join; + liveness, gone_run, readmitted, held_since; gone_source and the run-log gone attribution dropped); @24: observations keep the FIRST sighting per (site, ad_id, crawl_id) (a walk's bump-displaced repeat), counted as merge.observations_repeat_dropped; @25: --api-role ROLE rebuilds schema api (one owner-rights view per READABLE relation) for the Neon Data API, ROLE reads api only; @26: every api view is gated on auth.user_id() IN --api-user (required with --api-role), security_barrier
SCHEMA = "li"
PHONE_DIR = "import/2026-11-07-phone-session"
# The site specifics, read through the configured source and the site file's [pipeline.consumer]:
PHONE_SITE = pipeline.consumer().default_site
PHONE_REGION = pipeline.consumer().default_region
SITE_TZ = site.source().tz
REGIONS = site.source().regions
# The import's README: Google coordinates resolved 2026-11-07 may be cached <= 30 days.
PHONE_GOOGLE_EXPIRES = datetime(2026, 12, 7, tzinfo=UTC)
# The import's keys that carry the GOOGLE point (maps_url embeds it); the site pin (pin_*) is not Google's.
GOOGLE_POINT_KEYS = ("lat", "lon", "maps_url")
EXIT_DB = 11
EXIT_CONFIG = 2
VIEWPOINT = "config/viewpoint.json"  # written by the operator in DATA, never in git
GOLD_V3_MANIFEST = "eval/address-gold-v3/MANIFEST.json"  # names its tranche files; never hard-coded here


class DbNetwork:
    """The export target is a network service (our own database, metered compute)."""

    network = True


# ── reading DATA ─────────────────────────────────────────────────────────────────────────────


def ddl() -> str:
    """The schema files, then listings_masked GENERATED from gold_mask (the ONE masked-column list, which the
    boards use too). It is part of the DDL, so a mask change moves the digest and forces a rebuild."""
    from . import gold_mask

    files = sorted(resources.files("listing_lens.db.schema").iterdir(), key=lambda p: p.name)
    sql = "\n".join(f.read_text() for f in files if f.name.endswith(".sql"))
    return (
        sql
        + f"\n-- generated: gold_mask.view_select()\nCREATE VIEW listings_masked AS {gold_mask.view_select()};\n"
    )


def utc(s: str | None) -> datetime | None:
    """ISO-8601 with an offset or Z -> UTC; a zone-less wall time is the site's (SITE_TZ)."""
    if not s:
        return None
    t = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
    return (t if t.tzinfo else t.replace(tzinfo=SITE_TZ)).astimezone(UTC)


def clean_text(v):
    """Make every string storable as UTF-8: a split UTF-16 surrogate pair (JSON text cut mid-character,
    seen in the phone import) is recombined, a lone half becomes U+FFFD. Recurses into dicts/lists."""
    if isinstance(v, str):
        return v.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(v, dict):
        return {k: clean_text(x) for k, x in v.items()}
    if isinstance(v, list):
        return [clean_text(x) for x in v]
    return v


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _num(x):
    return float(x) if isinstance(x, int | float) and not isinstance(x, bool) else None


def ad_id_from_path(path: str) -> str | None:
    """The ad id at the end of an ad path or URL ("…-<digits>", a trailing "/" allowed); a query or fragment
    is ignored."""
    m = re.search(r"-(\d+)/?$", re.sub(r"[?#].*$", "", path or ""))
    return m.group(1) if m else None


# ── table builders: each returns a list of row dicts (column -> value) ────────────────────────


def _region_rows() -> list[dict]:
    out = []
    for name, reg in sorted(REGIONS.items()):
        (s, w), (n, e) = (tuple(map(float, part.split(","))) for part in reg.bounds.split("|"))
        out.append({"site": PHONE_SITE, "name": name, "south": s, "west": w, "north": n, "east": e})
    return out


def depth_decile(page, count) -> int | None:
    """The sampler's decile (scrape_harness.plan.depth_decile): 0 = first tenth of the list."""
    if not page or not count:
        return None
    return min(9, int(10 * (page - 1) / count))


def _observation_rows(d: Path) -> tuple[list[dict], int]:
    """One row per sighting, and at most ONE per (site, ad_id, crawl_id): a contiguous walk can see an ad twice
    when a bump mid-walk pushes its card a page down. The FIRST sighting (the shallower page) is kept, the
    repeats are counted (returned), and observations.jsonl is never rewritten."""
    rows, seen, dropped = [], set(), 0
    for i, o in enumerate(_jsonl(d / "observations.jsonl")):
        key = (o["site"], o["ad_id"], o.get("crawl_id"))
        if key[2] is not None and key in seen:
            dropped += 1
            continue
        seen.add(key)
        lst, c = o.get("list") or {}, o.get("card") or {}
        leo = c.get(site.source().record_key) or {}
        rows.append(
            {
                "seq": i,
                "site": o["site"],
                "ad_id": o["ad_id"],
                "crawl_id": o.get("crawl_id"),
                "seen_at": utc(o["seen_at"]),
                "seen_at_raw": o["seen_at"],
                "ad_url": o.get("ad_url"),
                "list_url": lst.get("url"),
                "list_subcategory": lst.get("subcategory"),
                "list_page": lst.get("page"),
                "list_page_count": lst.get("page_count"),
                "list_position": lst.get("position"),
                "depth_decile": depth_decile(lst.get("page"), lst.get("page_count")),
                "sponsored": lst.get("sponsored"),
                "list_sample_weight": _num(lst.get("sample_weight")),
                "card_title": c.get("title"),
                "card_city_label": c.get("city_label"),
                "card_name": leo.get("name"),
                "card_age": leo.get("age"),
                "card_ethnicity": leo.get("ethnicity"),
                "verified": leo.get("verified"),
                "available": leo.get("available"),
                "rate": leo.get("rate"),
                "bumped_at": utc(o.get("bumped_at")),
                "card_hash": o.get("card_hash"),
                "card": c,
            }
        )
    return rows, dropped


def _listing_rows(d: Path) -> list[dict]:
    rows = []
    for i, r in enumerate(_jsonl(d / "listings.jsonl")):
        src, content, loc = r["source"], r.get("content") or {}, r.get("location") or {}
        contact, t = content.get("contact") or {}, r.get("time") or {}
        pin, raw = loc.get("pin") or {}, r.get("raw_ref") or {}
        leo = (r.get("site") or {}).get(site.source().record_key) or {}
        media = list(content.get("media_urls") or [])
        rows.append(
            {
                "seq": i,
                "site": src["site"],
                "ad_id": r["ad_id"],
                "fetched_at": utc(r["fetched_at"]),
                "fetched_at_raw": r["fetched_at"],
                "fetch_reason": r["fetch_reason"],
                "parser": (r.get("derived") or {}).get("method"),
                "source_url": src.get("url"),
                "subcategory": src.get("subcategory"),
                "region": src.get("region"),
                "title": content.get("title"),
                "description": content.get("description"),
                "phone_masked": contact.get("phone_masked"),
                "email_masked": contact.get("email_masked"),
                "website": contact.get("website"),
                "whatsapp": contact.get("whatsapp"),
                "media_urls": media,
                "n_media": len(media),
                "first_published": utc(t.get("first_published")),
                "first_published_raw": t.get("first_published"),
                "bumped_at": utc(t.get("bumped_at")),
                "bumped_at_raw": t.get("bumped_at"),
                "city_label": loc.get("city_label"),
                "pin_lat": _num(pin.get("lat")),
                "pin_lon": _num(pin.get("lon")),
                "pin_approximate": pin.get("approximate") if pin else None,
                "raw_path": raw.get("path"),
                "raw_sha256": raw.get("sha256"),
                "raw_bytes": raw.get("bytes"),
                "city_code": leo.get("city_code"),
                "name": leo.get("name"),
                "age": leo.get("age"),
                "ethnicity": leo.get("ethnicity"),
                "service_mode": leo.get("service_mode"),
                "languages": leo.get("languages"),
                "height": leo.get("height"),
                "weight": leo.get("weight"),
                "body_stats": leo.get("body_stats"),
                "orientation": leo.get("orientation"),
                "hair": leo.get("hair"),
                "eye": leo.get("eye"),
                "tagline": leo.get("tagline"),
                "services": leo.get("services"),
                "page_schema_type": leo.get("page_schema_type"),
                "site_fields": r.get("site") or {},
            }
        )
    return rows


def latest(listings: list[dict]) -> dict[tuple, dict]:
    """The listings_latest view's rule in Python (for the merge): greatest fetched_at, later line wins."""
    out: dict[tuple, dict] = {}
    for r in listings:  # seq order
        k = (r["site"], r["ad_id"])
        if k not in out or r["fetched_at"] >= out[k]["fetched_at"]:
            out[k] = r
    return out


def gold_v3_ad_ids(d: Path) -> set[str]:
    """The ads whose li location answer the BOARDS mask for address-gold-v3: the LABELLED ads only, i.e. the
    union of every `sample.tranche<N>.json` the manifest lists (export-db@22, operator decision 2026-11-13).
    @20/@21 also masked their current operator-mates; that wider set now governs only @8 DEVELOPMENT (its
    pinned deny-list), so on the boards a mate's li answer and pin return. A frame ad never drawn stays
    unmasked.

    Read ONLY through the MANIFEST, every file sha256-checked, and FAIL-CLOSED: a missing manifest or tranche,
    a moved sha, a labelled set that disagrees with the manifest's `labelled_ids` (n, sha256 of the sorted ids
    joined by newlines) raises ValueError (exit 2). An empty or short set would silently UNMASK gold ads."""
    mp = d / GOLD_V3_MANIFEST
    if not mp.exists():
        raise ValueError(f"{GOLD_V3_MANIFEST} is missing: hint_gold_v3 cannot be computed")
    try:
        m = json.loads(mp.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{GOLD_V3_MANIFEST}: not JSON ({e.msg})") from e

    def pinned(p: Path, sha) -> bytes:
        if not p.is_file():
            raise ValueError(f"{GOLD_V3_MANIFEST} names {p.name}, which is missing")
        b = p.read_bytes()
        if hashlib.sha256(b).hexdigest() != sha:
            raise ValueError(f"{GOLD_V3_MANIFEST}: {p.name} no longer matches its sha256")
        return b

    tranches = sorted(k for k in m if re.fullmatch(r"sample\.tranche\d+\.json", k))
    if not tranches:
        raise ValueError(f"{GOLD_V3_MANIFEST} names no sample.tranche<N>.json")
    labelled: set[str] = set()
    for k in tranches:
        labelled |= {str(a) for a in json.loads(pinned(mp.parent / k, m[k]))["ad_ids"]}
    want = m.get("labelled_ids") or {}
    if "n" in want and want["n"] != len(labelled):
        raise ValueError(
            f"{GOLD_V3_MANIFEST}: labelled_ids.n is {want['n']}, the tranche files hold {len(labelled)}"
        )
    if want.get("sha256_sorted_newline_joined") and (
        hashlib.sha256("\n".join(sorted(labelled)).encode()).hexdigest()
        != want["sha256_sorted_newline_joined"]
    ):
        raise ValueError(f"{GOLD_V3_MANIFEST}: the tranche files' ids do not match labelled_ids.sha256")
    return labelled


def _derived_rows(d: Path, now: datetime) -> list[dict]:
    cache = geocache.load(now)
    gold_v3 = gold_v3_ad_ids(d)
    rows = []
    for r in _jsonl(d / "derived" / "listings.latest.jsonl"):
        src, loc, der = r["source"], r.get("location") or {}, r.get("derived") or {}
        hint, g, centre = loc.get("text_hint") or {}, loc.get("geocode") or {}, loc.get("city_centre") or {}
        region = load("sources", src["site"]).region(src.get("region") or PHONE_REGION)
        point, entry = None, None
        if hint and g.get("provider"):  # the same (provider, query) key derive used: ONE query function
            entry = cache.get((g["provider"], geocode_query(r, hint, region)[0]))
            point = (geocache.usable(entry, now) or {}).get("point")
        cpoint = None
        if centre.get("label") and centre.get("provider"):
            cq = region.city_queries.get(centre["label"], f"{centre['label']}, {region.query_suffix}")
            cpoint = (geocache.usable(cache.get((centre["provider"], cq)), now) or {}).get("point")
        rows.append(
            {
                "site": src["site"],
                "ad_id": r["ad_id"],
                "text_hint_raw": hint.get("raw"),
                "text_hint_kind": hint.get("kind"),
                "text_hint_query": hint.get("query"),
                "text_hint_extractor": hint.get("extractor"),
                "geocode_provider": g.get("provider"),
                "geocode_status": g.get("status"),
                "geocode_formatted": g.get("formatted"),
                "geocode_lat": _num((point or {}).get("lat")),
                "geocode_lon": _num((point or {}).get("lon")),
                "geocode_precise": g.get("precise"),
                "geocode_place_id": g.get("place_id"),
                "geocode_types": list(g.get("types") or []) or None,
                "geocode_expires_at": utc((entry or {}).get("expires_at")),
                "city_centre_label": centre.get("label"),
                "city_centre_lat": _num((cpoint or {}).get("lat")),
                "city_centre_lon": _num((cpoint or {}).get("lon")),
                "km_pin_text": _num(der.get("km_pin_text")),
                "km_pin_city": _num(der.get("km_pin_city")),
                "km_text_city": _num(der.get("km_text_city")),
                "pin_verdict": der.get("pin_verdict"),
                "confidence": der.get("confidence"),
                "location_valid_pin": der.get("location_valid_pin"),
                "location_valid_text": der.get("location_valid_text"),
                "location_invalid_reason": der.get("location_invalid_reason"),
                "location_region_test": der.get("location_region_test"),
                "geocode_decision": der.get("geocode_decision"),
                "geocode_location_type": g.get("location_type"),
                "derive_method": der.get("method"),
                **_place_cols(der, cache, now),
                "hint_gold_v3": str(r["ad_id"]) in gold_v3,
            }
        )
    return rows


def _place_cols(der: dict, cache: dict, now: datetime) -> dict:
    """The applied place decision. A Google-sourced point is re-read through its cache entry (place_cache_ref),
    so it is exported only while within the coordinate TTL, like geocode_lat/lon; a pin is the site's own."""
    p = der.get("place_point")
    ref = der.get("place_cache_ref")
    if p and ref:
        res = geocache.usable(cache.get((ref["provider"], ref["query"])), now) or {}
        i, cands = ref.get("candidate"), res.get("candidates") or []
        hit = res if i is None else cands[i] if i < len(cands) else {}
        p = hit.get("point")
    return {
        "place_decision": der.get("place_decision"),
        "place_decided_by": der.get("place_decided_by"),
        "place_decision_status": der.get("place_decision_status"),
        "place_review_ref": der.get("place_review_ref"),
        "place_point_source": der.get("place_point_source"),
        "place_lat": _num((p or {}).get("lat")),
        "place_lon": _num((p or {}).get("lon")),
        "location_valid_place": der.get("location_valid_place"),
        "place_gold_v2": der.get("place_gold_v2"),
    }


def _place_decisions_current(d: Path) -> None:
    """Refuse a derived file built before the place decisions it should carry changed (a new review batch, a
    place-check re-run): llens derive records their digest in summary.json; a mismatch is a stale input."""
    if not (d / "derived" / "listings.latest.jsonl").exists():
        return
    sp = d / "derived" / "summary.json"
    want = (json.loads(sp.read_text()).get("place") or {}).get("decisions_sha256") if sp.exists() else None
    got = pa.decisions_sha(d)
    if want != got:
        raise ValueError(
            f"DATA decisions/ changed since llens derive (place decisions sha {str(want)[:12]} != {str(got)[:12]}): re-run `llens derive`"
        )


def _geocode_rows(now: datetime) -> list[dict]:
    rows = []
    for (prov, q), e in sorted(geocache.load(now).items()):
        res = e.get("result") or {}
        point = (geocache.usable(e, now) or {}).get("point")
        rows.append(
            {
                "provider": prov,
                "query": q,
                "place_id": e.get("place_id"),
                "status": res.get("status"),
                "lat": _num((point or {}).get("lat")),
                "lon": _num((point or {}).get("lon")),
                "precise": res.get("precise"),
                "types": list(res.get("types") or []) or None,
                "formatted": res.get("formatted"),
                "resolved_at": utc(e.get("resolved_at")),
                "expires_at": utc(e.get("expires_at")),
            }
        )
    return rows


REQUEST_KEYS = (
    "list_pages",
    "ad_pages",
    "robots",
    "other",
    "egress_trace",
    "site_subresources",
    "third_party",
    "redirects",
    "failed_before_request",
    "site_subresources_blocked",
    "third_party_blocked",
)


def _run_rows(d: Path) -> list[dict]:
    rows = []
    for p in sorted((d / "runs").glob("*.json")) if (d / "runs").exists() else []:
        r = json.loads(p.read_text())
        req = r.get("requests") or {}
        rows.append(
            {
                "run_id": r["run_id"],
                "command": r.get("command"),
                "variant": r.get("variant"),
                "site": r.get("source"),
                "status": r.get("status"),
                "created_at": utc(r.get("created_at")),
                **{f"requests_{k}": req.get(k) for k in REQUEST_KEYS},
                "sample": r.get("sample"),
                "approval_ref": r.get("approval_ref"),
                "record": r,
            }
        )
    return rows


def _run_log_rows(d: Path) -> list[dict]:
    """runs/<file-safe run id>.log.jsonl, one row per line. The run is the one whose record's log_ref
    names this file (the name has ':' as '_', so it is never parsed back into a run id)."""
    runs = d / "runs"
    if not runs.exists():
        return []
    by_log = {}
    for p in sorted(runs.glob("*.json")):
        r = json.loads(p.read_text())
        if r.get("log_ref"):
            by_log[Path(r["log_ref"]).name] = r["run_id"]
    rows = []
    for p in sorted(runs.glob("*.log.jsonl")):
        lines = _jsonl(p)
        run_id = by_log.get(p.name) or next((x["run_id"] for x in lines if x.get("run_id")), None)
        if run_id is None:
            continue  # a log with no run record and no run id line: not a run's log
        for i, x in enumerate(lines):
            st = x.get("status")
            rows.append(
                {
                    "run_id": run_id,
                    "seq": i,
                    "at": utc(x.get("at")),
                    "level": x.get("level"),
                    "logger": x.get("logger"),
                    "event": x.get("event"),
                    "kind": x.get("kind"),
                    "status": st if isinstance(st, int) and not isinstance(st, bool) else None,
                    "path": x.get("path"),
                    "ms": _num(x.get("ms")),
                    "msg": x.get("msg"),
                }
            )
    return rows


# ── survival: per-ad observation span (GQM G6.Q4) ─────────────────────────────────────────────

# A sighting this long after first_seen is a REVISIT. FITTED, not derived: on DATA (2026-11-13) the gaps between
# an ad's consecutive sightings fall <= 2 h (a list card, then its own ad fetch in the same session) or >= 8 h
# (the next day's re-fetch); none in (2 h, 8 h]. 4 h is that empty band's geometric middle.
REVISIT_H = 4


def observation_span_rows(observations, listings, events, runs) -> list[dict]:
    """ONE row per (site, ad_id) with a LIVE sighting, read from the harness's liveness ledger
    (scrape_harness.liveness, under the default SyncPolicy's rules): the ledger owns what a sighting is (a
    GOOD crawl's list card, or an OK fetch; a reparse is offline and no sighting), the tombstone (the earliest
    gone at or after the latest sighting, joined by the ledger's adkey rule) and the state (alive | tombstoned
    | quarantined | held). An ad with no live sighting (only a bad crawl's cards) has no row.
    On top, the survival label (GQM G6.Q4): status = gone (tombstoned) | still_listed (seen live >= REVISIT_H
    after first_seen) | unknown (never revisited). Inputs are the RAW records: events in file order."""
    led = liveness.build(observations, listings, events, runs, policy.DEFAULT.rules())
    rows = []
    for s in led.spans():
        if s["first_seen"] is None:
            continue
        first, last = utc(s["first_seen"]), utc(s["last_seen"])
        if s["status"] == "tombstoned":
            status = "gone"
        elif (last - first).total_seconds() >= REVISIT_H * 3600:
            status = "still_listed"
        else:
            status = "unknown"
        rows.append(
            {
                "site": s["site"],
                "ad_id": s["ad_id"],
                "first_seen": first,
                "last_seen": last,
                "n_observations": s["n_sightings"],
                "gone_at": utc(s["gone_at"]),
                "gone_reason": s["gone_reason"],
                "gone_run": s["gone_run"],
                "liveness": s["status"],
                "readmitted": s["readmitted"],
                "held_since": utc(s["held_since"]),
                "status": status,
            }
        )
    return rows


def _ab_rows(d: Path) -> list[dict]:
    rows = []
    for p in sorted((d / "derived").glob("ab-*.json")) if (d / "derived").exists() else []:
        a = json.loads(p.read_text())
        for name, v in sorted((a.get("variants") or {}).items()):
            rows.append(
                {
                    "ab_at": utc(a.get("at")),
                    "ab_at_raw": a.get("at"),
                    "site": a.get("site"),
                    "subcategory": a.get("subcategory"),
                    "variant": name,
                    "cards": v.get("cards"),
                    "pins_on_pages": v.get("pins_on_pages"),
                    "stopped": v.get("stopped"),
                    "requests": v.get("requests"),
                    "coverage": v.get("coverage"),
                }
            )
    return rows


def _event_rows(d: Path) -> list[dict]:
    return [
        {
            "seq": i,
            "at": utc(e["at"]),
            "run_id": e.get("run_id"),
            "kind": e["kind"],
            "site": e.get("site"),
            "reason": e.get("reason"),
            "url": e.get("url"),
            "record": e,
        }
        for i, e in enumerate(_jsonl(d / "events.jsonl"))
    ]


def _sample_rows(d: Path) -> list[dict]:
    rows = []
    for p in (
        sorted((d / "derived" / "samples").glob("*.jsonl")) if (d / "derived" / "samples").exists() else []
    ):
        for s in _jsonl(p):
            rows.append(
                {
                    "run_id": s["run_id"],
                    "site": s["site"],
                    "ad_id": s["ad_id"],
                    "stratum": f"{s.get('subcategory')}/d{s.get('decile')}",
                    "subcategory": s.get("subcategory"),
                    "decile": s.get("decile"),
                    "weight": _num(s.get("weight")),
                    "reason": s.get("reason"),
                    "seed": s.get("seed"),
                    "allocation": s.get("allocation"),
                }
            )
    return rows


def _finding_rows(d: Path) -> list[dict]:
    seen: dict[str, int] = {}
    rows = []
    for i, f in enumerate(_jsonl(d / "findings.jsonl")):
        seen[f.get("id")] = seen.get(f.get("id"), 0) + 1
        data = f.get("data") or {}
        rows.append(
            {
                "seq": i,
                "id": f.get("id"),
                "version": seen[f.get("id")],
                "tested_at": utc(f.get("tested_at")),
                "status": f.get("status"),
                "question": f.get("question"),
                "method": f.get("method"),
                "result": f.get("result"),
                "n": data.get("n"),
                "scope": data.get("scope"),
                "weakest_assumption": f.get("weakest_assumption"),
                "next_test": f.get("next_test"),
                "record": f,
            }
        )
    return rows


def _advertiser_rows(d: Path, name: str = "advertisers.jsonl") -> list[dict]:
    rows = []
    for a in _jsonl(d / "derived" / name):
        ads = list(dict.fromkeys(a.get("ad_ids") or []))
        for ad in ads:
            rows.append(
                {
                    "advertiser_id": a["advertiser_id"],
                    "site": a.get("site"),
                    "ad_id": ad,
                    "n_ads": len(ads),
                    "link_kinds": a.get("link_kinds"),
                    "venue_only": a.get("venue_only"),
                    "method": a.get("method"),
                }
            )
    return rows


def _advertiser_v2_rows(d: Path) -> list[dict]:
    keys = (
        "site",
        "ad_id",
        "operator_id",
        "persona_id",
        "n_ads",
        "level",
        "n_names",
        "top_shared_key_share",
        "edge_evidence",
        "persona_edge_evidence",
        "method",
    )
    return [{k: x.get(k) for k in keys} for x in _jsonl(d / "derived" / "advertisers_v2.jsonl")]


def _ablation_rows(d: Path) -> list[dict]:
    keys = ("variant", "site", "ad_id", "advertiser_id", "n_ads", "link_kinds")
    return [{k: x.get(k) for k in keys} for x in _jsonl(d / "derived" / "advertisers_ablation.jsonl")]


def _advertiser_link_rows(d: Path) -> list[dict]:
    keys = (
        "advertiser_id",
        "advertiser_b",
        "site",
        "ad_a",
        "ad_b",
        "link_kind",
        "link_key",
        "used_for_clustering",
    )
    return [{k: x.get(k) for k in keys} for x in _jsonl(d / "derived" / "advertiser_links.jsonl")]


def _advertiser_v2_link_rows(d: Path) -> list[dict]:
    keys = (
        "operator_id",
        "site",
        "ad_a",
        "ad_b",
        "link_kind",
        "link_key",
        "n_keys",
        "edge_kinds",
        "cross_subcategory",
        "persona_edge",
        "method",
    )
    return [{k: x.get(k) for k in keys} for x in _jsonl(d / "derived" / "advertiser_v2_links.jsonl")]


def _v2_links_current(d: Path) -> None:
    """Refuse an advertiser_v2_links.jsonl that does not explain advertisers_v2.jsonl (both are written by
    `derive-advertisers --method v2`, clusters first): missing, older than the clusters, or inconsistent with
    them. Consistent = every edge joins two ads of ONE operator (its operator_id), the edges' kinds per ad are
    exactly that ad's edge_evidence, and the edges' connected components are exactly the multi-ad operators."""
    v2p, lp = d / "derived" / "advertisers_v2.jsonl", d / "derived" / "advertiser_v2_links.jsonl"
    if not v2p.exists():
        return
    fix = "re-run `llens derive-advertisers --method v2`"
    if not lp.exists():
        raise ValueError(f"advertisers_v2.jsonl has no advertiser_v2_links.jsonl beside it: {fix}")
    if lp.stat().st_mtime < v2p.stat().st_mtime:
        raise ValueError(f"advertiser_v2_links.jsonl is older than advertisers_v2.jsonl: {fix}")
    from .advertisers import LINK_KIND_V2, UnionFind

    op = {str(r["ad_id"]): r for r in _jsonl(v2p)}
    name = {v: k for k, v in LINK_KIND_V2.items()}
    kinds: dict[str, set] = {a: set() for a in op}
    uf = UnionFind()
    bad = 0
    for e in _jsonl(lp):
        a, b = str(e["ad_a"]), str(e["ad_b"])
        if (
            a not in op
            or b not in op
            or not (op[a]["operator_id"] == op[b]["operator_id"] == e["operator_id"])
        ):
            bad += 1
            continue
        k = {name.get(x, x) for x in e.get("edge_kinds") or []}
        kinds[a] |= k
        kinds[b] |= k
        uf.union(a, b)
    bad += sum(1 for a, r in op.items() if kinds[a] != set(r.get("edge_evidence") or []))
    groups: dict[str, set] = {}
    for a in op:
        groups.setdefault(uf.find(a), set()).add(a)
    comps = {frozenset(g) for g in groups.values() if len(g) > 1}
    ops: dict[str, set] = {}
    for a, r in op.items():
        ops.setdefault(r["operator_id"], set()).add(a)
    if bad or comps != {frozenset(g) for g in ops.values() if len(g) > 1}:
        raise ValueError(
            f"advertiser_v2_links.jsonl does not match advertisers_v2.jsonl ({bad} rows off): {fix}"
        )


def _phone_rows(d: Path, now: datetime) -> list[dict]:
    """The import's own files joined per ad (by the ad path): harvest (card), bodies (text), geo (Google)."""
    base = d / PHONE_DIR
    if not base.exists():
        return []
    cards = {r["h"]: r for r in json.loads((base / "harvest" / "all-today.json").read_text())}
    bodies = {}
    for p in sorted((base / "bodies").glob("c*.json")):
        bodies.update({r["h"]: r for r in json.loads(p.read_text())})
    geo = {re.sub(r"^https?://[^/]+", "", r["url"]): r for r in _jsonl(base / "geo" / "ads.jsonl")}
    google_ok = now < PHONE_GOOGLE_EXPIRES
    # The region rule, shared with derive: each row's label centre from li's geocode cache (the same label
    # query derive uses). The flag is computed from the import's own points even once Google's point
    # itself may no longer be exported; only the boolean is kept.
    region = REGIONS[PHONE_REGION]
    cache = geocache.load(now)
    from . import boundaries

    bounds = boundaries.Boundaries.load(d)
    # the provider derive used for label centres (google-geocoding in production, a fixture in tests)
    provider = next(
        (
            (r.get("location") or {}).get("city_centre", {}).get("provider")
            for r in _jsonl(d / "derived" / "listings.latest.jsonl")
            if ((r.get("location") or {}).get("city_centre") or {}).get("provider")
        ),
        "google-geocoding",
    )

    def centre_of(label):
        if not label:
            return None
        q = region.city_queries.get(label, f"{label}, {region.query_suffix}")
        return (geocache.usable(cache.get((provider, q)), now) or {}).get("point")

    rows = []
    for path in sorted(cards):
        c, b, g = cards[path], bodies.get(path) or {}, geo.get(path) or {}
        aid = ad_id_from_path(path)
        if not aid:
            continue
        stamp = g.get("bumped") or c.get("stamp")
        plat, plon = (g.get("pin_lat"), g.get("pin_lon")) if g else (c.get("lat"), c.get("lon"))
        pin = {"lat": float(plat), "lon": float(plon)} if plat is not None and plon is not None else None
        tpt = (
            {"lat": float(g["lat"]), "lon": float(g["lon"])}
            if g.get("lat") is not None and g.get("lon") is not None
            else None
        )
        vp, vt = derive.region_validity(pin, tpt, centre_of(c.get("city")), c.get("city"), bounds)
        hint = {"raw": g.get("address_as_written")} if g.get("address_as_written") else None
        rows.append(
            {
                "site": PHONE_SITE,
                "ad_id": aid,
                "path": path,
                "title": b.get("title") or c.get("title") or g.get("title"),
                "body": b.get("body"),
                "city_label": c.get("city"),
                "bumped_at": utc(stamp),
                "bumped_at_raw": stamp,
                "pin_lat": _num(g.get("pin_lat")) if g else _num(c.get("lat")),
                "pin_lon": _num(g.get("pin_lon")) if g else _num(c.get("lon")),
                "text_hint_raw": g.get("address_as_written") or None,
                "text_hint_kind": g.get("hint_kind") or None,
                "geocode_lat": _num(g.get("lat")) if google_ok else None,
                "geocode_lon": _num(g.get("lon")) if google_ok else None,
                "geocode_precise": g.get("precise") if g else None,
                "city_filter": g.get("city_filter"),
                "km_to_target": _num(g.get("km_to_target")),
                "d_text": _num(g.get("d_text")),
                "d_city": _num(g.get("d_city")),
                "pin_class": g.get("pin_class"),
                "confidence": g.get("confidence"),
                "location_valid_pin": vp,
                "location_valid_text": vt,
                "location_invalid_reason": derive.invalid_reason(
                    vp, vt, hint, {"status": "OK", "point": tpt} if tpt else None
                ),
                "location_region_test": derive.region_test(c.get("city"), centre_of(c.get("city")), bounds),
                "record": {
                    "card": c,
                    "geo": {k: v for k, v in g.items() if google_ok or k not in GOOGLE_POINT_KEYS},
                },
            }
        )
    return rows


# ── the merge: ONE row per (site, ad_id) across li listings and the phone import ─────────────

AGREE, DIFFER = "both-agree", "both-differ"


def _norm(s: str) -> str:
    return " ".join(s.split())


def merge_text(li, ph):
    """Text: the RICHER (longer, whitespace-normalised) value; li on a tie."""
    if li is None and ph is None:
        return None, None
    if ph is None:
        return li, "li"
    if li is None:
        return ph, "phone"
    if _norm(li) == _norm(ph):
        return li, AGREE
    return (ph if len(_norm(ph)) > len(_norm(li)) else li), DIFFER


def merge_value(li, ph, same=lambda a, b: a == b):
    """Structured values (points, times, codes): li when both differ (it comes from a saved snapshot,
    the newer parser and extractor); provenance says so."""
    if li is None and ph is None:
        return None, None
    if ph is None:
        return li, "li"
    if li is None:
        return ph, "phone"
    return li, (AGREE if same(li, ph) else DIFFER)


def _close(a, b):
    return all(abs(x - y) <= 1e-6 for x, y in zip(a, b, strict=True))


def ads_rows(listings, derived, phone, observations, regions) -> tuple[list[dict], dict]:
    lat = latest(listings)
    der = {(r["site"], r["ad_id"]): r for r in derived}
    ph = {(r["site"], r["ad_id"]): r for r in phone}
    cards = {(o["site"], o["ad_id"]) for o in observations}
    box = {r["name"]: r for r in regions}.get(PHONE_REGION)
    rows = []
    for k in sorted(set(lat) | set(ph)):
        li, p, dv = lat.get(k), ph.get(k), der.get(k) or {}
        title, title_src = merge_text(li and li["title"], p and p["title"])
        desc, desc_src = merge_text(li and li["description"], p and p["body"])
        city, city_src = merge_text(li and li["city_label"], p and p["city_label"])
        bumped, bumped_src = merge_value(li and li["bumped_at"], p and p["bumped_at"])
        li_pin = (li["pin_lat"], li["pin_lon"]) if li and li["pin_lat"] is not None else None
        ph_pin = (p["pin_lat"], p["pin_lon"]) if p and p["pin_lat"] is not None else None
        pin, pin_src = merge_value(li_pin, ph_pin, _close)
        li_hint = (dv.get("text_hint_raw"), dv.get("text_hint_kind")) if dv.get("text_hint_raw") else None
        ph_hint = (p["text_hint_raw"], p["text_hint_kind"]) if p and p["text_hint_raw"] else None
        hint, hint_src = merge_value(li_hint, ph_hint, lambda a, b: _norm(a[0]) == _norm(b[0]))
        li_geo = (
            (dv["geocode_lat"], dv["geocode_lon"], dv.get("geocode_precise"))
            if dv.get("geocode_lat") is not None
            else None
        )
        ph_geo = (
            (p["geocode_lat"], p["geocode_lon"], p["geocode_precise"])
            if p and p["geocode_lat"] is not None
            else None
        )
        geo, geo_src = merge_value(li_geo, ph_geo, lambda a, b: _close(a[:2], b[:2]))
        rows.append(
            {
                "site": k[0],
                "ad_id": k[1],
                "in_li_listings": li is not None,
                "in_li_cards": k in cards,
                "in_phone": p is not None,
                "title": title,
                "title_src": title_src,
                "description": desc,
                "description_src": desc_src,
                "city_label": city,
                "city_label_src": city_src,
                "bumped_at": bumped,
                "bumped_at_src": bumped_src,
                "pin_lat": pin[0] if pin else None,
                "pin_lon": pin[1] if pin else None,
                "pin_src": pin_src,
                "text_hint_raw": hint[0] if hint else None,
                "text_hint_kind": hint[1] if hint else None,
                "text_hint_src": hint_src,
                "geocode_lat": geo[0] if geo else None,
                "geocode_lon": geo[1] if geo else None,
                "geocode_precise": geo[2] if geo else None,
                "geocode_src": geo_src,
                "in_box": bool(
                    pin
                    and box
                    and box["south"] <= pin[0] <= box["north"]
                    and box["west"] <= pin[1] <= box["east"]
                )
                if pin
                else None,
            }
        )
    both = sum(1 for r in rows if r["in_li_listings"] and r["in_phone"])
    counts = {
        "ads": len(rows),
        "li_listings_only": sum(1 for r in rows if r["in_li_listings"] and not r["in_phone"]),
        "phone_only": sum(1 for r in rows if r["in_phone"] and not r["in_li_listings"]),
        "both_opened": both,
        "phone_seen_by_li_only_as_cards": sum(
            1 for r in rows if r["in_phone"] and not r["in_li_listings"] and r["in_li_cards"]
        ),
        "phone_disjoint_from_li": sum(
            1 for r in rows if r["in_phone"] and not r["in_li_listings"] and not r["in_li_cards"]
        ),
        "provenance": {
            f: dict(sorted(_count(rows, f + "_src").items()))
            for f in ("title", "description", "city_label", "bumped_at", "pin", "text_hint", "geocode")
        },
    }
    return rows, counts


def _count(rows, col):
    out: dict[str, int] = {}
    for r in rows:
        v = r[col] or "none"
        out[v] = out.get(v, 0) + 1
    return out


# ── li services (derived/services.*) and the vocabulary's labels ─────────────────────────────


SERVICES_VOCAB = "vocab/services.ttl"  # the operator's SKOS vocabulary (DATA; `llens services` default)


def services_rows(d: Path) -> dict[str, list[dict]]:
    """services_ads (every processed ad, 0 mentions included), services_mentions, services_run (one row from
    services.summary.json), services_excluded_strata (counts only), services_concepts, services_codes."""
    recs = _jsonl(d / "derived" / "services.latest.jsonl")
    ads, mentions = [], []
    for r in recs:
        ms = r.get("mentions") or []
        ads.append(
            {
                "site": r["site"],
                "ad_id": r["ad_id"],
                "fetched_at": utc(r["fetched_at"]),
                "n_concepts_offered": len({m["concept"] for m in ms if m["stance"] == "offered"}),
                "n_concepts_refused": len({m["concept"] for m in ms if m["stance"] == "refused"}),
                "online_services_offered": list(r.get("online_services_offered") or []),
                "eval_adjacent": r.get("eval_adjacent"),
                "vocab_version": r["vocab_version"],
                "method": r["method"],
            }
        )
        mentions += [
            {
                "site": r["site"],
                "ad_id": r["ad_id"],
                "fetched_at": utc(r["fetched_at"]),
                "concept": m["concept"],
                "field": m["field"],
                "start": m["start"],
                "end": m["end"],
                "stance": m["stance"],
                "label_matched": m["label_matched"],
            }
            for m in ms
        ]
    sp = d / "derived" / "services.summary.json"
    summ = json.loads(sp.read_text()) if sp.exists() and recs else None
    run = (
        [
            {
                "ran_at": utc(summ.get("ran_at")),
                "eval_gate": summ.get("eval_gate", "unknown"),
                "eval_excluded": bool(summ.get("eval_excluded")),
                "eval_ads_excluded": int(summ.get("eval_ads_excluded") or 0),
                "eval_excluded_by_advertiser": int(summ.get("eval_excluded_by_advertiser") or 0),
                "concepts": int(summ.get("concepts") or 0),
                "ads": int(summ.get("ads") or len(recs)),
                "vocab_version": summ.get("vocab_version") or "",
                "method": summ.get("method") or "",
            }
        ]
        if summ
        else []
    )
    return {
        "services_ads": ads,
        "services_mentions": mentions,
        "services_run": run,
        "services_excluded_strata": _excluded_strata(d, summ),
        "services_concepts": _concept_rows(d),
        "services_codes": _code_rows(d),
    }


def _excluded_strata(d: Path, summ: dict | None) -> list[dict]:
    """Where the excluded ads sit in THE depth sample, so a weighted panel can measure (not guess) the
    re-weighting: per (subcategory, decile), the eval ads AND their v2-operator mates (both absent from
    services_ads). Counts only, never an ad id. The depth run is the boards' DEPTH_RUN rule (grafana/boards.py):
    the earliest run with sample.stratify = 'depth' and no sample.select (the recruit run is depth-stratified
    too; counting its rows was the +19 the analyst's n_h cross-check found). The exclusion is recomputed with the
    stage's own eval_exclusion; a total that differs from the stage's summary refuses (re-run `llens services`)."""
    if not summ or not summ.get("eval_excluded"):
        return []
    from .commands import eval_exclusion

    eval_ids, mates = eval_exclusion(d)
    derived = {str(r["ad_id"]) for r in _jsonl(d / "derived" / "listings.latest.jsonl")}
    want = (summ.get("eval_ads_excluded"), summ.get("eval_excluded_by_advertiser", 0))
    got = (len(derived & eval_ids), len(derived & mates))
    if got != want:
        raise ValueError(
            f"services.summary.json excluded {want} but the current split/clustering gives {got}: re-run `llens services`"
        )
    depth = [
        r
        for r in _run_rows(d)
        if (r["sample"] or {}).get("stratify") == "depth" and (r["sample"] or {}).get("select") is None
    ]
    if not depth:
        return []
    depth_run = min(depth, key=lambda r: (r["created_at"] is None, r["created_at"]))["run_id"]
    ev: dict[tuple, set] = {}
    adv: dict[tuple, set] = {}
    for s in _sample_rows(d):
        if s["run_id"] != depth_run:
            continue
        k, a = (s["subcategory"], s["decile"]), str(s["ad_id"])
        if a in eval_ids:
            ev.setdefault(k, set()).add(a)
        elif a in mates:
            adv.setdefault(k, set()).add(a)
    keys = sorted(set(ev) | set(adv), key=lambda k: (str(k[0]), k[1] if k[1] is not None else -1))
    return [
        {
            "subcategory": sub,
            "decile": dec,
            "n_eval_ads_excluded": len(ev.get((sub, dec), ())),
            "n_advertiser_excluded": len(adv.get((sub, dec), ())),
        }
        for sub, dec in keys
    ]


def _concept_rows(d: Path) -> list[dict]:
    """The vocabulary's labels (DATA): concept = skos:notation, as `llens services` writes it. No file, no rows."""
    p = d / SERVICES_VOCAB
    if not p.exists():
        return []
    import rdflib
    from rdflib.namespace import OWL, SKOS

    from .vocab import notation as vocab_notation
    from .vocab import service_concepts, service_schemes

    g = rdflib.Graph().parse(str(p), format="turtle")
    schemes = service_schemes(g)  # never the stop scheme
    version = str(g.value(schemes[0], OWL.versionInfo) or "0.0.0") if schemes else "0.0.0"

    def notation(c) -> str:
        return vocab_notation(g, c)

    def pref(c) -> str:
        labels = list(g.objects(c, SKOS.prefLabel))
        en = [x for x in labels if getattr(x, "language", None) in ("en", None)]
        return str((en or labels or [notation(c)])[0])

    rows = []
    for c in service_concepts(g):  # stop phrases are not services: no row
        b = g.value(c, SKOS.broader)
        rows.append(
            {"concept": notation(c), "pref_label": pref(c), "broader": notation(b) if b is not None else None}
        )
    rows.sort(key=lambda r: ((r["broader"] or "").lower(), r["pref_label"].lower(), r["concept"]))
    return [{**r, "sort_order": i, "vocab_version": version} for i, r in enumerate(rows)]


def _code_rows(d: Path) -> list[dict]:
    """The page's own service codes and the label it displays most often for each (ties: alphabetical)."""
    from collections import Counter

    labels: dict[str, Counter] = {}
    for r in _jsonl(d / "listings.jsonl"):
        for it in ((r.get("site") or {}).get(site.source().record_key) or {}).get("services") or []:
            if it.get("code"):
                labels.setdefault(it["code"], Counter())[it.get("label")] += 1
    out = []
    for code in sorted(labels):
        best = sorted(labels[code].items(), key=lambda kv: (-kv[1], str(kv[0])))[0][0]
        out.append({"code": code, "label": best})
    return out


# ── the whole copy ────────────────────────────────────────────────────────────────────────────


def viewpoint_rows(d: Path) -> list[dict]:
    """DATA/config/viewpoint.json: [{label, lat, lon}] or one such object (the operator's own points
    for the map's radius rings). No file = no rows. A malformed file raises ValueError (exit 2)."""
    p = d / VIEWPOINT
    if not p.exists():
        return []
    try:
        v = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{VIEWPOINT}: not JSON ({e.msg})") from e
    rows = v if isinstance(v, list) else [v]
    out = []
    for i, r in enumerate(rows):
        ok = (
            isinstance(r, dict)
            and isinstance(r.get("label"), str)
            and r["label"]
            and all(
                isinstance(r.get(k), int | float) and not isinstance(r.get(k), bool) for k in ("lat", "lon")
            )
        )
        if not ok or not (-90 <= r["lat"] <= 90 and -180 <= r["lon"] <= 180):
            raise ValueError(f"{VIEWPOINT}: entry {i} must be {{label: text, lat: -90..90, lon: -180..180}}")
        out.append({"label": r["label"], "lat": float(r["lat"]), "lon": float(r["lon"])})
    return out


def build(now: datetime | None = None) -> tuple[dict[str, list[dict]], dict]:
    now = now or datetime.now(UTC)
    d = store.data_dir()
    regions = _region_rows()
    obs, repeats = _observation_rows(d)
    listings = _listing_rows(d)
    _place_decisions_current(d)
    _v2_links_current(d)
    derived = _derived_rows(d, now)
    phone = _phone_rows(d, now)
    ads, merge = ads_rows(listings, derived, phone, obs, regions)
    events, run_logs, runs = _event_rows(d), _run_log_rows(d), _run_rows(d)
    tables = {
        "region": regions,
        "viewpoint": viewpoint_rows(d),
        "observations": obs,
        "listings": listings,
        "derived": derived,
        "geocodes": _geocode_rows(now),
        "runs": runs,
        "run_logs": run_logs,
        "ad_observation_span": observation_span_rows(
            _jsonl(d / "observations.jsonl"),
            _jsonl(d / "listings.jsonl"),
            [e["record"] for e in events],
            [r["record"] for r in runs],
        ),
        "ab_variants": _ab_rows(d),
        "events": events,
        "samples": _sample_rows(d),
        "findings": _finding_rows(d),
        "advertisers": _advertiser_rows(d),
        "advertisers_v1": _advertiser_rows(d, "advertisers_v1.jsonl"),
        "advertiser_links": _advertiser_link_rows(d),
        "advertisers_ablation": _ablation_rows(d),
        "advertisers_v2": _advertiser_v2_rows(d),
        "advertiser_v2_links": _advertiser_v2_link_rows(d),
        "import_phone_ads": phone,
        "ads": ads,
        **services_rows(d),
    }
    merge = merge | {"observations_repeat_dropped": repeats}
    return {name: [clean_text(r) for r in rows] for name, rows in tables.items()}, merge


def _canon(v):
    if isinstance(v, datetime):
        return v.astimezone(UTC).isoformat()
    return v


def digest(tables: dict[str, list[dict]], schema_sql: str) -> str:
    h = hashlib.sha256()
    h.update(LOADER.encode())
    h.update(schema_sql.encode())
    for name in sorted(tables):
        h.update(name.encode())
        for row in tables[name]:
            h.update(json.dumps({k: _canon(v) for k, v in row.items()}, sort_keys=True, default=str).encode())
    return h.hexdigest()


def _cell(table: str, col: str, v, jsonb_cols: set, jsonb):
    """Every non-null value bound for a jsonb column is wrapped as JSON, whatever its Python type (a
    plain string, e.g. an A/B stop reason, becomes a JSON string); everything else goes as it is."""
    if v is not None and (table, col) in jsonb_cols:
        return jsonb(v)
    return v


def load_db(
    conn,
    tables,
    schema_sql,
    meta: dict,
    grant_role: str | None = None,
    api_role: str | None = None,
    api_users: list[str] | None = None,
) -> None:
    """Build li_new, then swap it in with ONE transaction (old copy or new, never half)."""
    from psycopg import sql
    from psycopg.types.json import Jsonb

    with conn.transaction():
        # PostGIS lives in public: never in li, which every rebuild drops (CASCADE would take it along)
        conn.execute("CREATE EXTENSION IF NOT EXISTS postgis SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS li_new CASCADE")
        conn.execute("CREATE SCHEMA li_new")
        conn.execute("SET LOCAL search_path TO li_new, public")
        conn.execute(schema_sql)
        jsonb_cols = {
            (t, c)
            for t, c in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns"
                " WHERE table_schema = 'li_new' AND data_type = 'jsonb'"
            )
        }
        rows_meta = [{"key": k, "value": v} for k, v in sorted(meta.items())]
        for name, rows in [("meta", rows_meta), *tables.items()]:
            if not rows:
                continue
            cols = list(rows[0])
            stmt = sql.SQL("COPY li_new.{} ({}) FROM STDIN").format(
                sql.Identifier(name), sql.SQL(", ").join(map(sql.Identifier, cols))
            )
            with conn.cursor().copy(stmt) as cp:
                for r in rows:
                    cp.write_row([_cell(name, c, r[c], jsonb_cols, Jsonb) for c in cols])
    with conn.transaction():
        exists = conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'li'").fetchone()
        if exists:
            conn.execute("ALTER SCHEMA li RENAME TO li_old")
        conn.execute("ALTER SCHEMA li_new RENAME TO li")
        if exists:
            conn.execute("DROP SCHEMA li_old CASCADE")
        if grant_role:
            grant(conn, grant_role)
        if api_role:
            api_views(conn, api_role, api_users or [])


# The read-only role's relations: an ALLOWLIST, so a new relation is denied until it is classified here
# (a test fails on any relation in neither set). WITHHELD carry a gold-bearing location answer or the
# geocode queries; the boards read listings_masked (a view: it runs with its owner's rights) and
# geocode_cache_summary instead.
READABLE = (
    "ab_variants",
    "ad_observation_span",
    "advertiser_links",
    "advertiser_v2_links",
    "advertisers",
    "advertisers_ablation",
    "advertisers_v1",
    "advertisers_v2",
    "events",
    "findings",
    "geocode_cache_summary",
    "import_phone_ads",
    "listings",
    "listings_masked",
    "meta",
    "observations",
    "region",
    "run_logs",
    "runs",
    "samples",
    "services_ad_concepts",
    "services_ads",
    "services_codes",
    "services_concepts",
    "services_excluded_strata",
    "services_mentions",
    "services_run",
    "viewpoint",
)
WITHHELD = ("derived", "listings_latest", "ads", "geocodes")


def grant(conn, grant_role: str) -> None:
    """The read-only role's access, idempotent: run inside the swap AND on an unchanged export (the no-op
    must not skip it, or a grant fix never reaches the role; found live 2026-11-08).
    The search_path goes on BOTH levels, read back by a test: the role-wide value (pg_roles.rolconfig) and
    the per-database one (pg_db_role_setting, which wins for sessions). li first, then public for PostGIS's
    ST_*. (Applies on a direct connection; Neon's PgBouncer ignores role settings.)"""
    from psycopg import sql

    role = sql.Identifier(grant_role)
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA li TO {}").format(role))
    # Revoke first, then grant the allowlist: re-running on an existing database NARROWS it (export-db@21).
    conn.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA li FROM {}").format(role))
    for rel in WITHHELD:
        conn.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.Identifier("li", rel), role))
    for rel in READABLE:
        conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(sql.Identifier("li", rel), role))
    db = conn.execute("SELECT current_database()").fetchone()[0]
    conn.execute(sql.SQL("ALTER ROLE {} SET search_path = li, public").format(role))
    conn.execute(
        sql.SQL("ALTER ROLE {} IN DATABASE {} SET search_path = li, public").format(role, sql.Identifier(db))
    )


API_SCHEMA = "api"


def api_views(conn, api_role: str, api_users: list[str]) -> None:
    """The Neon Data API's surface (export-db@25, operator decision 2026-11-20): schema api holds one
    read-only view per READABLE relation (the boards' allowlist, ONE list), and api_role reads those views
    and NOTHING in li. Owner-rights views, so the role needs no right on li; SELECT only, so a simple
    view's auto-update is refused. Rebuilt on every load and on an unchanged export: the old views follow
    li to li_old and die with its CASCADE, so a load without --api-role leaves api empty (fail closed).
    The gate (export-db@26): every view answers only a request whose JWT subject (auth.user_id(), Neon's
    pg_session_jwt) is in api_users, so a token Neon Auth issues to anyone else reads 0 rows, whatever its
    sign-up settings let through; with no JWT, auth.user_id() is NULL and the view is empty too.
    security_barrier keeps a caller's filter from running before the gate."""
    if not api_users:
        raise ValueError("--api-role needs at least one --api-user")
    from psycopg import sql

    role, api = sql.Identifier(api_role), sql.Identifier(API_SCHEMA)
    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(api))
    conn.execute(sql.SQL("CREATE SCHEMA {}").format(api))
    for rel in READABLE:
        conn.execute(
            sql.SQL(
                "CREATE VIEW {} WITH (security_barrier) AS SELECT * FROM {} WHERE auth.user_id() = ANY({})"
            ).format(
                sql.Identifier(API_SCHEMA, rel),
                sql.Identifier("li", rel),
                sql.Literal(sorted(set(api_users))),
            )
        )
    conn.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA li FROM {}").format(role))
    conn.execute(sql.SQL("REVOKE ALL ON SCHEMA li FROM {}").format(role))
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(api, role))
    conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(api, role))


def current_digest(conn) -> str | None:
    if not conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'li'").fetchone():
        return None
    row = conn.execute("SELECT value FROM li.meta WHERE key = 'digest'").fetchone()
    return row[0] if row else None


def cmd_export_db(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    if getattr(args, "api_role", None) and not getattr(args, "api_user", None):
        sys.stderr.write("export-db: --api-role needs at least one --api-user (the gate's allowlist)\n")
        return EXIT_CONFIG
    try:
        tables, merge = build(now)
    except ValueError as e:  # a malformed config file in DATA: say which, change nothing
        sys.stderr.write(f"export-db: {e}\n")
        return EXIT_CONFIG
    schema_sql = ddl()
    dig = digest(tables, schema_sql)
    counts = {k: len(v) for k, v in tables.items()}
    card(
        "export-db",
        "rebuild the derived Postgres copy (schema li) from DATA" + (" (dry run)" if args.dry_run else ""),
        mode="dry-run" if args.dry_run else "run",
        approval=None if args.dry_run else "--approve-network (operator-approved run)",
    )
    summary = {"digest": dig, "rows": counts, "merge": merge}
    if args.dry_run:
        print(json.dumps(summary, sort_keys=True))
        return 0
    need_network(args, DbNetwork(), "export-db")
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        sys.stderr.write(
            f"export-db: {args.dsn_env} is not set; run under `secret exec {args.dsn_env}=<keychain item> -- llens export-db ...`\n"
        )
        return EXIT_DB
    import psycopg

    meta = {
        "digest": dig,
        "loader": LOADER,
        "built_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "li_rev": os.environ.get("LLENS_BUILD_REV") or "unknown",
        "rows": counts,
        "merge": merge,
        "geocode_ttl_checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        with psycopg.connect(dsn, connect_timeout=30) as conn:
            if not args.force and current_digest(conn) == dig:
                if args.grant_role or args.api_role:  # the data is unchanged, the roles' access may not be
                    with conn.transaction():
                        if args.grant_role:
                            grant(conn, args.grant_role)
                        if args.api_role:
                            api_views(conn, args.api_role, args.api_user or [])
                print(
                    json.dumps(
                        {
                            "unchanged": True,
                            "digest": dig,
                            **({"granted": args.grant_role} if args.grant_role else {}),
                            **({"api_role": args.api_role} if args.api_role else {}),
                        }
                    )
                )
                return 0
            load_db(conn, tables, schema_sql, meta, args.grant_role, args.api_role, args.api_user)
    except psycopg.Error as e:  # never the message: a connection error can echo the DSN's parts
        sys.stderr.write(
            f"export-db: database error {type(e).__name__} (sqlstate {getattr(e, 'sqlstate', None)})\n"
        )
        return EXIT_DB
    print(json.dumps({"exported": True, **summary}, sort_keys=True))
    return 0


def register(sub) -> None:
    x = sub.add_parser(
        "export-db", help="rebuild the derived Postgres copy (schema li) from DATA, atomically"
    )
    x.add_argument(
        "--dry-run", action="store_true", help="build every row and print counts + digest; no network"
    )
    x.add_argument("--approve-network", action="store_true")
    x.add_argument(
        "--dsn-env", default="LLENS_DB_URL", help="environment variable holding the DSN (never printed)"
    )
    x.add_argument(
        "--grant-role", help="read-only role to GRANT USAGE + SELECT on the new schema (e.g. board_ro)"
    )
    x.add_argument(
        "--api-role",
        help="role the Neon Data API runs as (e.g. authenticated): SELECT on schema api's views of the "
        "boards' allowlist, nothing in li",
    )
    x.add_argument(
        "--api-user",
        action="append",
        help="a Neon Auth user id (JWT sub) the api views answer; repeatable, required with --api-role",
    )
    x.add_argument("--force", action="store_true", help="rebuild even when the digest is unchanged")
    x.set_defaults(fn=cmd_export_db)
