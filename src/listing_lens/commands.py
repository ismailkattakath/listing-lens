"""listing-lens' OWN commands on top of scrape-harness: the analysis stages extract, geocode, derive
and qa-page (pipeline.toml [[stages]] 4-7). The generic stages (crawl-lists, fetch-ads, reparse) and the
utilities come from the harness CLI; pipeline.toml [plugins].commands registers these with it.

Moved verbatim from the pre-switch listing_lens/cli.py (333e075); only the imports changed.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime

from scrape_harness import contracts, store
from scrape_harness.cli import card, need_network
from scrape_harness.interfaces import load, plugin_class

from . import derive, geocache, qa, site
from . import place_apply as pa

# Sub-schemas of this consumer's listing.v1 (schemas/listing.v1.schema.json).
TEXT_HINT = "listing.v1.schema.json#/$defs/text_hint"
GEOCODE = "listing.v1.schema.json#/$defs/geocode"


def _geocoder(args: argparse.Namespace):
    kwargs = {"path": args.geocode_fixture} if args.geocoder == "fixture" else {}
    return load("geocoders", args.geocoder, **kwargs)


def latest_listings() -> list[dict]:
    return list(
        store.latest(
            store.read("listings"), lambda r: (r["source"]["site"], r["ad_id"]), "fetched_at"
        ).values()
    )


def area_is_city(hint: dict, city_label: str | None) -> bool:
    """An `area` hint that only repeats the ad's city label adds nothing: dropped."""
    return hint["kind"] == "area" and bool(city_label) and hint["raw"].lower() in city_label.lower()


def _road(side: str) -> str:
    """One intersection side as Google should read it: a bare numbered highway ("Hwy 7", a site highway number) is spelled
    "Highway N" (the extractor's CITY_HWY rule), so its number cannot be taken for a house number."""
    from listing_lens.extractors.gta_hints import HWY_NAMES, HWY_NUM

    s = side.strip()
    if m := re.fullmatch(rf"(?i)(?:hwy|highway)\s?(\d{{1,3}})|({HWY_NUM})", s):
        return f"Highway {m.group(1) or m.group(2)}"
    return s.upper() if s.lower() in set(HWY_NAMES.split("|")) else s


def intersection_sides(raw: str) -> tuple[str, str] | None:
    """The two roads of an intersection hint, whatever connector the ad used ("/", "@", "and", "corner of", a
    direction letter …): the extractor's own INTERSECTION groups, else a split at its first CONNECTOR; None
    when there is no connector. Shared by place-check (OSM's shared node) and the "A & B" query form."""
    from listing_lens.extractors.gta_hints import CONNECTOR, INTERSECTION

    raw = raw.strip()
    if m := INTERSECTION.fullmatch(raw):
        return _road(m.group(1)), _road(m.group(2))
    sides = re.split(CONNECTOR, raw, maxsplit=1)
    return (_road(sides[0]), _road(sides[1])) if len(sides) == 2 and all(x.strip() for x in sides) else None


def query_phrase(hint: dict) -> str:
    """What the geocoder is asked: an alias's target (`query`), else the phrase as written; an intersection in
    the "A & B" form with a bare numbered highway spelled "Highway N" (intersection_sides), so Google cannot
    read the highway's number as a house number (the 38 REJECTED:hint_number points)."""
    if hint.get("query"):
        return hint["query"].strip()
    if hint.get("kind") == "intersection" and (sides := intersection_sides(hint["raw"])):
        return " & ".join(sides)
    return hint["raw"].strip()


def hints(args: argparse.Namespace, rows: list[dict]) -> dict[tuple, dict | None]:
    ex = load("extractors", args.extractor)
    out = {}
    for r in rows:
        # The title is its own line: a line break is a phrase boundary for the extractor.
        h = ex.extract(f"{r['content']['title']}\n{r['content']['description']}")
        if h and area_is_city(h, r["location"].get("city_label")):
            h = None
        out[(r["source"]["site"], r["ad_id"])] = contracts.validate(TEXT_HINT, h) if h else None
    return out


def cmd_extract(args: argparse.Namespace) -> int:
    card("extract", "print the location phrase per latest listing")
    for (host, aid), h in hints(args, latest_listings()).items():
        print(json.dumps({"site": host, "ad_id": aid, "text_hint": h}, ensure_ascii=False))
    return 0


def geocode_query(row: dict, hint: dict | None, region) -> tuple[str | None, str | None]:
    """(phrase query, label query) for one listing: THE query shape, used by `geocode`, `derive` and
    export-db alike (the cache is keyed by it, so a second copy would silently miss)."""
    lab = row["location"].get("city_label")
    lab_q = region.city_queries.get(lab, f"{lab}, {region.query_suffix}") if lab else None
    if not hint:
        return None, lab_q
    base = query_phrase(hint)  # an alias already names its own place: no label added
    return (f"{base}, {region.query_suffix}" if hint.get("query") or not lab_q else f"{base}, {lab_q}"), lab_q


def label_box(centre: dict, half_km: float) -> str:
    """A "south,west|north,east" bias box of +-half_km around a point (a label centre, never a pin)."""
    import math

    dlat = half_km / 111.0
    dlon = half_km / (111.32 * math.cos(math.radians(centre["lat"])))
    return f"{centre['lat'] - dlat},{centre['lon'] - dlon}|{centre['lat'] + dlat},{centre['lon'] + dlon}"


def queries(args: argparse.Namespace, rows: list[dict]) -> tuple[dict, dict]:
    """(listing key -> (phrase query, bounds, admin_area), city label -> (query, None, admin_area)).

    A phrase query carries the ad's CITY LABEL ("<phrase>, <label query>"; an alias already names its own
    place) and is biased to a box of +-REGION_KM around that label's cached centre, else the region box.
    The bias is the LABEL, never the pin: the pin is what the answer is later checked against (5 km rule),
    so biasing toward it would manufacture the agreement. One query string -> one bias box, so the cache
    (keyed by query) stays correct."""
    phrase, city = {}, {}
    hs = hints(args, rows)
    now = datetime.now(UTC)
    cache = geocache.load(now)
    provider = (
        getattr(args, "provider", None)
        or plugin_class("geocoders", getattr(args, "geocoder", None) or "google").provider
    )
    for r in rows:
        src = load("sources", r["source"]["site"])
        region = src.region(r["source"]["region"])
        admin = src.admin_area(r["source"]["region"]) if hasattr(src, "admin_area") else None
        lab = r["location"].get("city_label")
        h = hs[(r["source"]["site"], r["ad_id"])]
        q, lab_q = geocode_query(r, h, region)
        if lab:
            city[lab] = (lab_q, None, admin)
        if h:
            centre = (
                (geocache.usable(cache.get((provider, lab_q)), now) or {}).get("point") if lab_q else None
            )
            phrase[(r["source"]["site"], r["ad_id"])] = (
                q,
                label_box(centre, derive.REGION_KM) if centre else region.bounds,
                admin,
            )
    return phrase, city


def review_queries(rows: list[dict], provider: str, now) -> dict[str, tuple]:
    """The operator's other_place texts (DATA decisions/place-review.*.jsonl) -> (query, bounds, admin_area):
    query = place_apply.review_query (derive reads the same key), biased like a phrase query to +-REGION_KM
    around the label centre of the group's first listed ad, else the region box."""
    reviews = pa.load_reviews(store.data_dir())
    by_id = {r["ad_id"]: r for r in rows}
    cache = geocache.load(now)
    out: dict[str, tuple] = {}
    for rv in reviews:
        if rv["decision"] != "other_place":
            continue
        q = pa.review_query(rv["value"])
        r = next((by_id[a] for a in rv["ad_ids"] if a in by_id), None)
        if q in out or r is None:
            continue
        src = load("sources", r["source"]["site"])
        region = src.region(r["source"]["region"])
        admin = src.admin_area(r["source"]["region"]) if hasattr(src, "admin_area") else None
        _, lab_q = geocode_query(r, None, region)
        centre = (geocache.usable(cache.get((provider, lab_q)), now) or {}).get("point") if lab_q else None
        out[q] = (q, label_box(centre, derive.REGION_KM) if centre else region.bounds, admin)
    return out


def _geocode_card(args: argparse.Namespace, cls, pending: int) -> None:
    """Printed once the pending count is known, so Scale/ETA/Approval/Egress are the real ones."""
    networked = getattr(cls, "network", True)
    n = pending if args.dry_run else min(pending, args.max_requests)
    delay = getattr(cls, "min_delay", None)
    if not networked:
        approval = None  # -> "none needed (offline)"
    elif args.dry_run:
        approval = "--approve-network needed for a real run"
    else:
        approval = "--approve-network (operator-approved run)"
    card(
        "geocode",
        "geocode uncached or expired phrases and city labels" + (" (dry run)" if args.dry_run else ""),
        mode="dry-run" if args.dry_run else "run",
        planned={"geocode": n} if n else {},
        per_minute=round(60 / delay) if delay else None,
        approval=approval,
        egress=f"not checked: geocoding goes to {cls.provider}, not to the crawled site"
        if networked
        else None,
    )


def cmd_geocode(args: argparse.Namespace) -> int:
    cls = plugin_class("geocoders", args.geocoder)  # no key needed to plan
    now = datetime.now(UTC)
    rows = latest_listings()
    phrase, city = queries(args, rows)
    review = review_queries(rows, cls.provider, now)
    cache = geocache.load(now)
    todo = {}
    wanted = (
        list(review.values())
        if args.only == "place-review"
        else [*phrase.values(), *city.values(), *review.values()]
    )
    for q, bounds, admin in wanted:
        e = cache.get((cls.provider, q))
        if e and geocache.fresh(e, now) and not e["result"]["status"].startswith("ERROR"):
            continue  # a fresh answer is never re-requested
        todo[q] = (bounds, admin, e)
    if not args.dry_run:
        need_network(args, cls, "geocode")  # refuse BEFORE a card claims an approved run
    _geocode_card(args, cls, len(todo))
    if args.dry_run:
        for q, (_, _, e) in sorted(todo.items()):
            print(json.dumps({"query": q, "how": "place_id" if e and e.get("place_id") else "search"}))
        # The refresh's paid gate reads `pending` from THIS dry run (pipeline paid_gate.count_field).
        # Without the line the gate found no count and refused every refresh at geocode.
        print(json.dumps({"pending": len(todo), "requested": 0, "cap": args.max_requests}))
        return 0
    geo = _geocoder(args)
    asked = 0
    for q, (bounds, admin, e) in sorted(todo.items())[: args.max_requests]:
        res = (
            geo.by_place_id(e["place_id"])
            if e and e.get("place_id")
            else geo.geocode(q, bounds=bounds, admin_area=admin)
        )
        cache[(geo.provider, q)] = geocache.entry(
            geo.provider, q, contracts.validate(GEOCODE, res), geo.coord_ttl, now
        )
        asked += 1
    geocache.save(cache.values(), now)  # rewrite: no expired coordinate survives on disk
    print(json.dumps({"pending": len(todo), "requested": asked, "cap": args.max_requests}))
    return 0


def cmd_derive(args: argparse.Namespace) -> int:
    card("derive", "join listings with cached geocodes and measure the pins")
    rows = latest_listings()
    hs = hints(args, rows)
    phrase, city = queries(args, rows)
    now = datetime.now(UTC)
    cache = geocache.load(now)
    from . import boundaries

    data = store.data_dir()
    bounds = boundaries.Boundaries.load(
        data
    )  # None: no [boundaries], boundary file or mapping -> the radius rule alone
    method = f"{args.extractor}+{args.provider}+hintnum+{pa.TAG}{'+cd2021' if bounds else ''}+haversine"
    modes = derive.modal_pins(rows)
    reviews = pa.by_ad(pa.load_reviews(data))  # the operator's, over the machine's (place_apply docstring)
    machine = (
        pa.machine_by_key(store.read("place_check"))
        if (data / "decisions/place-check.jsonl").exists()
        else {}
    )

    def review_result(q: str) -> dict | None:
        return geocache.usable(cache.get((args.provider, q)), now)

    osm_cache: dict = {}

    def osm(text: str) -> dict:  # the street snapshot is loaded only when a fallback is needed
        if "streets" not in osm_cache:
            from . import place_check as pc

            osm_cache["streets"] = pc.Streets.load(data)
        if osm_cache["streets"] is None:
            return {"status": "osm_no_snapshot", "names": None, "crossings": []}
        if text not in osm_cache:
            osm_cache[text] = pa.osm_crossings(osm_cache["streets"], text)
        return osm_cache[text]

    out = []
    for r in rows:
        k = (r["source"]["site"], r["ad_id"])
        g = geocache.usable(cache.get((args.provider, phrase[k][0])), now) if k in phrase else None
        lab = r["location"].get("city_label")
        cg = geocache.usable(cache.get((args.provider, city[lab][0])), now) if lab in city else None
        centre = (
            {"label": lab, "point": cg["point"], "provider": args.provider}
            if cg and cg.get("point")
            else None
        )
        e = derive.enrich(r, hs[k], g, centre, modes, method, bounds)
        ref = {"provider": args.provider, "query": phrase[k][0]} if g else None
        fields = pa.place_fields(
            e, reviews.get(r["ad_id"]), machine.get(k), ref, review_result, args.provider, osm, bounds
        )
        out.append(pa.apply(e, fields))
    n = store.replace("derived", out)
    summary = derive.summarise(out, method)
    summary["region_parity_box25"] = region_parity(out)
    summary["place"] = pa.summary(out) | {"decisions_sha256": pa.decisions_sha(data)}
    summary["region_test"] = boundary_summary(out, bounds, data)
    (data / "derived" / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps({"derived": n, "place": summary["place"], "summary": "derived/summary.json"}))
    return 0


def boundary_summary(rows: list[dict], bounds, data) -> dict:
    """Which test decided each row, and the labels the boundary mapping does not cover (they use the radius)."""
    import collections

    from . import boundaries

    cfg = boundaries.config() if bounds else None
    slim = data / cfg.dir / cfg.slim if cfg else None
    return {
        "by_test": dict(
            collections.Counter(r["derived"].get("location_region_test") or "none" for r in rows)
        ),
        "unmapped_labels": dict(
            collections.Counter(
                r["location"].get("city_label")
                for r in rows
                if r["derived"].get("location_region_test") == "radius"
            )
        ),
        "unknown_cds": bounds.unknown_cds if bounds else None,
        "slim_sha256": boundaries.sha256(slim) if slim else None,
    }


def cmd_boundaries_build(args: argparse.Namespace) -> int:
    from . import boundaries

    try:
        cfg = boundaries.require()  # the dataset is the site file's [boundaries]: never a default one
    except boundaries.BoundariesConfigError as e:
        raise SystemExit(f"refusing: {e}") from None
    card("boundaries-build", f"{cfg.describe} (offline)")
    d = store.data_dir() / cfg.dir
    out = boundaries.build(d / cfg.zip, d / cfg.slim, cfg, args.simplify_m)
    print(json.dumps(out | {"zip_sha256": boundaries.sha256(d / cfg.zip)}))
    return 0


def cmd_places_resolve(args: argparse.Namespace) -> int:
    from . import places

    card(
        "places-resolve",
        "resolve place abbreviations from OSM / Wikidata / GeoNames" + (" + Gemini suggestions" if args.gemini else ""),
        approval="operator-approved Gemini (bare tokens only)" if args.gemini else None,
        egress=f"not checked: Gemini {places.GEMINI_MODEL}, bare tokens only" if args.gemini else None,
    )  # fmt: skip
    out = places.run(
        store.data_dir(), list(store.read("derived")), use_gemini=args.gemini, max_gemini=args.max_gemini
    )
    print(json.dumps(out, indent=1))
    return 0


def cmd_place_check(args: argparse.Namespace) -> int:
    from . import place_check as pc

    card("place-check", "multi-source check of derive's review cases (offline)")
    data = store.data_dir()
    rows = list(store.read("derived"))
    streets = pc.Streets.load(data)
    if args.calibrate:
        print(
            json.dumps(
                {"calibration": pc.calibrate(rows, streets), "snapshot": pc.STREETS_SNAPSHOT}, indent=1
            )
        )
        return 0
    landmarks = pc.load_landmarks(data)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    decided, pending = [], []
    for r in rows:
        d = pc.decide(r, streets, landmarks, args.t_osm)
        if not d:
            continue
        decided.append({"schema": "place_check.v1", "decided_at": now, "decided_by": pc.METHOD} | d)
        if d["decision"] == "review":
            pending.append(
                {"schema": "place_check_pending.v1", "site": d["site"], "ad_id": d["ad_id"], "case": d["case"],
                 "reason": d["reason"], "span": pc.span(r), "group": pc.group_key(r), "method": pc.METHOD}
            )  # fmt: skip
    # Decisions by humans (a later Label Studio batch) live in the same file: only this stage's rows are replaced.
    kept = [
        x for x in store.read("place_check") if not str(x.get("decided_by", "")).startswith("place-check@")
    ]
    store.replace("place_check", kept + decided)
    store.replace("place_check_pending", pending)
    counts: dict = {}
    for d in decided:
        counts.setdefault(d["case"], {}).setdefault(d["reason"] or d["decision"], 0)
        counts[d["case"]][d["reason"] or d["decision"]] += 1
    print(
        json.dumps(
            {
                "decided": len(decided),
                "review": len(pending),
                "review_groups": len({x["group"] for x in pending}),
                "t_osm_km": args.t_osm,
                "by_case": counts,
            },  # fmt: skip
            indent=1,
        )
    )
    return 0


def region_parity(rows: list[dict], buffer_km: float = 25.0) -> dict:
    """The analyst's parity check for the label-relative region rule: the same counts from the source's
    region box + buffer_km. Equal today (24 pins, 4 texts); a disagreement is the weakest assumption showing
    (a label centre near the box edge, e.g. two towns on opposite edges of the box)."""

    def box_km(pt, bounds):
        if not pt or not bounds:
            return None
        (s_, w), (n_, e) = (tuple(map(float, part.split(","))) for part in bounds.split("|"))
        return derive.km(pt, {"lat": min(max(pt["lat"], s_), n_), "lon": min(max(pt["lon"], w), e)})

    c = {"flagged_pins": 0, "box_pins": 0, "flagged_texts": 0, "box_texts": 0}
    for r in rows:
        b = load("sources", r["source"]["site"]).region(r["source"]["region"]).bounds
        d = r["derived"]
        c["flagged_pins"] += d.get("location_valid_pin") is False
        c["flagged_texts"] += d.get("location_valid_text") is False
        c["box_pins"] += (box_km(r["location"].get("pin"), b) or 0) > buffer_km
        c["box_texts"] += (box_km((r["location"].get("geocode") or {}).get("point"), b) or 0) > buffer_km
    return c | {"agree": c["flagged_pins"] == c["box_pins"] and c["flagged_texts"] == c["box_texts"]}


def cmd_qa_page(args: argparse.Namespace) -> int:
    card("qa-page", "render the QA page from derived listings")
    rows = list(store.read("derived"))
    target = None
    if args.target:
        lat, lon = (float(x) for x in args.target.split(","))
        target = {"lat": lat, "lon": lon}
    (store.data_dir() / "derived" / "qa.html").write_text(qa.render(rows, target))
    print(json.dumps({"rows": len(rows), "page": "derived/qa.html"}))
    return 0


ADVERTISER_METHODS = ("v1.1", "v1", "v2")


def cmd_derive_advertisers(args: argparse.Namespace) -> int:
    """Offline: cluster the latest (derived) listings into advertisers -> derived/advertisers.jsonl.
    `--method a,b` runs each method in turn, through this same function (the first failure stops it)."""
    methods = [m.strip() for m in str(args.method).split(",") if m.strip()]
    bad = [m for m in methods if m not in ADVERTISER_METHODS]
    if not methods or bad:
        raise SystemExit(
            f"derive-advertisers: --method {args.method!r}: choose from {', '.join(ADVERTISER_METHODS)}"
        )
    if len(methods) > 1:
        for m in methods:
            if rc := cmd_derive_advertisers(argparse.Namespace(**{**vars(args), "method": m})):
                return rc
        return 0
    from . import advertisers

    latest = list(store.read("derived"))
    if args.method == "v2":  # a NEW method next to v1; never writes v1's files
        card("derive-advertisers", "cluster the latest listings into advertisers, method v2 (offline)")
        rows2 = advertisers.clusters_v2(latest)
        n2 = store.replace("advertisers_v2", rows2)
        # after the clusters, so export-db's guard can also refuse an edge file older than them
        n2_links = store.replace("advertiser_v2_links", advertisers.links_v2(latest, rows2))
        groups = {r["operator_id"] for r in rows2}
        print(
            json.dumps(
                {
                    "method": "v2",
                    "ads": n2,
                    "advertisers": len(groups),
                    "multi_ad": len({r["operator_id"] for r in rows2 if r["n_ads"] > 1}),
                    "operators": len({r["operator_id"] for r in rows2 if r["level"] == "operator"}),
                    "personas": len({r["persona_id"] for r in rows2}),
                    "file": "derived/advertisers_v2.jsonl",
                    "links": n2_links,
                    "links_file": "derived/advertiser_v2_links.jsonl",
                }
            )
        )
        return 0
    if args.method == "v1":  # the frozen historical record, its own file; nothing else is written
        card("derive-advertisers", "cluster the latest listings into advertisers, frozen method v1 (offline)")
        rows1 = advertisers.clusters(latest)
        n1 = store.replace("advertisers_v1", rows1)
        print(json.dumps({"method": "v1", "advertisers": n1, "file": "derived/advertisers_v1.jsonl"}))
        return 0
    # v1.1, the default: the canonical advertisers.jsonl, and the links + ablation on v1.1's keys
    card("derive-advertisers", "cluster the latest listings into advertisers, method v1.1 (offline)")
    rows = advertisers.clusters_v1_1(latest)
    n = store.replace("advertisers", rows)
    n_links = store.replace("advertiser_links", advertisers.links(latest, rows, advertisers.keys_v1_1))
    n_ablation = store.replace("advertisers_ablation", advertisers.ablation(latest, advertisers.keys_v1_1))
    multi = [a for a in rows if len(a["ad_ids"]) > 1 and not a["venue_only"]]
    print(
        json.dumps(
            {
                "method": "v1.1",
                "advertisers": n,
                "ads": sum(len(a["ad_ids"]) for a in rows),
                "multi_ad": len(multi),
                "venue_only": sum(a["venue_only"] for a in rows),
                "links": n_links,
                "ablation_rows": n_ablation,
                "file": "derived/advertisers.jsonl",
                "links_file": "derived/advertiser_links.jsonl",
            }
        )
    )
    return 0


def services_eval_gate(graph) -> tuple[set[str] | None, set[str] | None, set[str] | None, dict]:
    """The blind-eval guard: matcher output must not exist on eval ads before their gold labels are frozen.

    Returns (eval ad_ids to exclude, their advertiser-mates to exclude, the eval-ADJACENT ad_ids, facts); the
    two exclusion sets are None for a full run. Raises ValueError to refuse. The adjacent set (an eval ad, or
    one sharing a current v2 operator with one; analyst rule R1) is computed in EVERY non-empty-vocabulary run,
    frozen included, because discovery/tuning must keep filtering eval-adjacent text after the freeze; it is
    None only for an empty vocabulary (nothing is matched, so nothing can be adjacent text).
    All three files are the analyst's, under DATA/eval/ (never git):
    - an empty vocabulary (0 concepts) cannot reveal anything: full run;
    - services-gold.freeze.json (eval-freeze.v1) whose labels_sha256 matches the bytes of its labels_path (and
      whose persona_split_sha256, when present, matches the split): full run;
    - no marker: every ad of an eval-half persona in services-persona-split.json is excluded, AND every ad that
      shares a CURRENT v2 operator with one (eval_exclusion: a new recruit can join an eval advertiser, and the
      split, built before it, does not list it; found in the post-run rehearsal, 106 persona / 145 operator mates);
    - a marker that does not verify, or no marker and no split to exclude by: refused."""
    import hashlib

    def sha(p) -> str | None:
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None

    from .vocab import service_concepts

    concepts = len(service_concepts(graph))  # the stop scheme's concepts produce no output
    if concepts == 0:
        return None, None, None, {"concepts": 0, "eval_gate": "empty-vocab", "eval_excluded": False}
    data = store.data_dir()
    marker = data / "eval" / "services-gold.freeze.json"
    split = data / "eval" / "services-persona-split.json"
    if marker.is_file():
        m = json.loads(marker.read_text())
        labels = data / m["labels_path"] if m.get("labels_path") else None
        ok = (
            m.get("schema") == "eval-freeze.v1"
            and labels is not None
            and bool(m.get("labels_sha256"))
            and sha(labels) == m["labels_sha256"]
        )
        if ok and m.get("persona_split_sha256"):
            ok = (
                sha(data / m.get("persona_split_path", "eval/services-persona-split.json"))
                == m["persona_split_sha256"]
            )
        if not ok:
            raise ValueError(
                f"{marker}: the frozen labels (or split) do not match their recorded sha256, or are absent"
            )
        eval_ids, mates = eval_exclusion(data)  # the full run still flags eval-adjacent rows (R1)
        return (
            None,
            None,
            eval_ids | mates,
            {
                "concepts": concepts,
                "eval_gate": "frozen",
                "eval_excluded": False,
                "frozen_at": m.get("frozen_at"),
            },
        )
    if not split.is_file():
        raise ValueError(f"no {marker} and no {split}: cannot keep matcher output off the blind-eval ads")
    eval_ids, mates = eval_exclusion(data)
    return (
        eval_ids,
        mates,
        eval_ids | mates,
        {"concepts": concepts, "eval_gate": "unfrozen", "eval_excluded": True},
    )


def eval_exclusion(data) -> tuple[set[str], set[str]]:
    """(eval ad_ids from the split, ads sharing a CURRENT v2 operator with any of them). The stricter operator
    level, not persona: blindness beats coverage (team-lead, 2026-11-08). FAIL CLOSED on a clustering that is
    missing or stale: an ad that derive knows but advertisers_v2 does not (or a v2 file older than derived)
    could be an un-clustered bridge, so the gate refuses instead of letting it through. One function for the
    stage AND export-db (services_excluded_strata), so the two can never count differently."""
    eval_ids = split_eval_ad_ids(data / "eval" / "services-persona-split.json")
    v2, derived = data / "derived" / "advertisers_v2.jsonl", data / "derived" / "listings.latest.jsonl"
    if not v2.is_file():
        raise ValueError(
            f"no {v2}: run `derive-advertisers --method v2` first (the eval gate needs current operators)"
        )
    rows = [json.loads(x) for x in v2.read_text().splitlines() if x.strip()]
    clustered = {str(r["ad_id"]) for r in rows}
    known = (
        {str(json.loads(x)["ad_id"]) for x in derived.read_text().splitlines() if x.strip()}
        if derived.is_file()
        else set()
    )
    if v2.stat().st_mtime < derived.stat().st_mtime or known - clustered:
        raise ValueError(
            f"{v2} is stale ({len(known - clustered)} derived ads not clustered, or older than derived): "
            "re-run `derive-advertisers --method v2`"
        )
    ops: dict[str, set[str]] = {}
    for r in rows:
        ops.setdefault(r["operator_id"], set()).add(str(r["ad_id"]))
    mates = {a for ids in ops.values() if ids & eval_ids for a in ids} - eval_ids
    return eval_ids, mates


def split_eval_ad_ids(split) -> set[str]:
    """Every ad of an eval-half persona. FAIL CLOSED: an unrecognised layout or an empty eval set refuses, so a
    shape mismatch can never read as "0 eval ads to exclude" and become a full run (the leak)."""
    bad = ValueError(f"{split}: unrecognised persona-split layout; refusing rather than excluding nothing")
    try:
        personas = json.loads(split.read_text())["personas"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise bad from e
    if not isinstance(personas, dict) or not personas:
        raise bad
    eval_ids: set[str] = set()
    for p in personas.values():
        if (
            not isinstance(p, dict)
            or p.get("half") not in ("eval", "discovery")
            or not isinstance(p.get("ad_ids"), list)
        ):
            raise bad
        if p["half"] == "eval":
            eval_ids |= {str(a) for a in p["ad_ids"]}
    if not eval_ids:
        raise ValueError(f"{split}: the eval half lists no ads; refusing rather than running on every ad")
    return eval_ids


def cmd_services(args: argparse.Namespace) -> int:
    """Offline: the service concepts each ad's latest version names -> derived/services.latest.jsonl.

    The vocabulary is the operator's DATA (default DATA/vocab/services.ttl), never packaged; a missing file
    exits 2 rather than writing an empty result that would read as "no ad names a service"."""
    from pathlib import Path

    vocab_path = Path(args.vocab) if args.vocab else store.data_dir() / "vocab" / "services.ttl"
    if not vocab_path.is_file():
        print(json.dumps({"error": "vocabulary not found", "vocab": str(vocab_path), "hint": "--vocab PATH"}))
        return 2
    from . import services_match as sm  # spaCy loads only for this stage

    graph = sm.load_vocab(vocab_path)
    try:
        exclude, mates, adjacent, gate = services_eval_gate(graph)
    except ValueError as e:
        print(json.dumps({"error": "blind-eval gate", "detail": str(e)}))
        return 2
    rows = list(store.read("derived"))  # one row per ad: its latest version
    if exclude is not None:
        kept = [r for r in rows if str(r["ad_id"]) not in exclude]
        gate["eval_ads_excluded"] = len(rows) - len(kept)
        rows = [r for r in kept if str(r["ad_id"]) not in mates]
        gate["eval_excluded_by_advertiser"] = len(kept) - len(rows)
    card(
        "services",
        "match the service vocabulary in each ad's latest title and description (offline)"
        + (
            f"; {gate['eval_ads_excluded']} blind-eval ads + {gate['eval_excluded_by_advertiser']} of their"
            " v2-operator mates EXCLUDED (gold not frozen)"
            if gate["eval_excluded"]
            else ""
        ),
    )
    version = sm.vocab_version(graph)
    out = []
    for r in rows:
        mentions = [
            {**m, "field": field}
            for field in ("title", "description")
            for m in sm.match(r["content"].get(field), graph)
        ]
        shown = ((r.get("site") or {}).get(site.source().record_key) or {}).get("services") or []
        out.append(
            {
                "schema": "services.v1",
                "site": r["source"]["site"],
                "ad_id": r["ad_id"],
                "fetched_at": r["fetched_at"],
                "parser": (r.get("derived") or {}).get("method"),  # export-db's listings_latest.parser
                "vocab_version": version,
                "method": sm.METHOD,
                "online_services_offered": [s["code"] for s in shown if s.get("code")],
                # analyst R1: an eval ad, or one sharing a current v2 operator with one (keyed to AD ids)
                "eval_adjacent": None if adjacent is None else str(r["ad_id"]) in adjacent,
                "mentions": mentions,
            }
        )
    n = store.replace("services", out)
    stance = [m["stance"] for r in out for m in r["mentions"]]
    summary = {
        "ran_at": datetime.now(UTC).isoformat(),
        "ads": n,
        "ads_with_mentions": sum(bool(r["mentions"]) for r in out),
        "offered": stance.count("offered"),
        "refused": stance.count("refused"),
        "vocab_version": version,
        "method": sm.METHOD,
        **gate,
        "file": "derived/services.latest.jsonl",
    }
    # Next to the records, so a reader of the file can tell a partial (eval-excluded) run from a full one.
    (store.data_dir() / "derived" / "services.summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary))
    return 0


# The LIVE extractor for extract/geocode/derive (operator decisions 2026-11-13: @6 -> @8.2 -> @8.3, each after
# its freeze note). A test pins it, so a change here is a deliberate, reviewed switch.
DEFAULT_EXTRACTOR = "gta-hints@8.3"


def extractor_arg(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--extractor", default=DEFAULT_EXTRACTOR)


def register(sub) -> None:
    """[plugins].commands entry: add the analysis subcommands to the harness CLI."""
    e = sub.add_parser("extract", help="print the location phrase per latest listing (JSONL)")
    extractor_arg(e)
    e.set_defaults(fn=cmd_extract)

    g = sub.add_parser(
        "geocode", help="geocode uncached or expired phrases and city labels (30-day coordinate TTL)"
    )
    g.add_argument("--geocoder", default="google")
    g.add_argument("--geocode-fixture")
    g.add_argument("--max-requests", type=int, default=300, help="hard cap per run (default 300)")
    g.add_argument("--dry-run", action="store_true", help="print pending queries; no network")
    g.add_argument("--approve-network", action="store_true")
    g.add_argument(
        "--only",
        choices=["place-review"],
        help="place-review: only the operator's other_place texts (DATA decisions/place-review.*.jsonl)",
    )
    extractor_arg(g)
    g.set_defaults(fn=cmd_geocode)

    d = sub.add_parser("derive", help="join + measure -> derived/listings.latest.jsonl, derived/summary.json")
    d.add_argument("--provider", default="google-geocoding", help="which geocoder's cached answers to use")
    extractor_arg(d)
    d.set_defaults(fn=cmd_derive)

    da = sub.add_parser("derive-advertisers", help="cluster the latest listings into advertisers (offline)")
    da.add_argument(
        "--method",
        default="v1.1",
        help="one method, or several comma-separated, run in that order (e.g. v1.1,v2: what `llens refresh` runs). "
        "v1.1 (default): advertisers@1.1 (no email-domain handles) + links + ablation -> advertisers.jsonl; "
        "v1: the frozen advertisers@1 record -> advertisers_v1.jsonl; v2: advertisers@2 -> advertisers_v2.jsonl + advertiser_v2_links.jsonl",
    )
    da.set_defaults(fn=cmd_derive_advertisers)

    sv = sub.add_parser("services", help="service vocabulary mentions per latest listing (offline)")
    sv.add_argument("--vocab", help="the SKOS Turtle vocabulary (default: DATA/vocab/services.ttl)")
    sv.set_defaults(fn=cmd_services)

    _register_panel(sub)

    from . import export_db

    export_db.register(sub)

    from . import api_token

    api_token.register(sub)

    from . import db_check

    db_check.register(sub)  # `db export|token|check|api-check`

    bb = sub.add_parser(
        "boundaries-build", help="the site file's [boundaries] dataset: zip -> slim (offline)"
    )
    bb.add_argument(
        "--simplify-m", type=float, default=50.0, help="simplification tolerance in metres (default 50)"
    )
    bb.set_defaults(fn=cmd_boundaries_build)

    pr = sub.add_parser("places-resolve", help="place abbreviations -> DATA vocab/places.ttl (+ pending)")
    pr.add_argument("--gemini", action="store_true", help="ask Gemini about unresolved BARE tokens (network)")
    pr.add_argument("--max-gemini", type=int, default=50, help="hard cap on Gemini requests (default 50)")
    pr.set_defaults(fn=cmd_places_resolve)

    pc = sub.add_parser("place-check", help="review cases -> DATA decisions/place-check.jsonl (offline)")
    pc.add_argument(
        "--t-osm", type=float, help="the analyst-frozen T_osm (km); absent = criterion 4 fails closed"
    )
    pc.add_argument(
        "--calibrate", action="store_true", help="measure T_osm on auto_close ads; writes nothing"
    )
    pc.set_defaults(fn=cmd_place_check)

    q = sub.add_parser("qa-page", help="derived listings -> derived/qa.html")
    q.add_argument("--target", help="lat,lon to sort by distance from (a run argument, never stored)")
    q.set_defaults(fn=cmd_qa_page)


def _register_panel(sub) -> None:
    from scrape_harness import crawl

    from . import panel

    pn = sub.add_parser(
        "panel", help="H4 panel: build, scheduled run (standing approval), pause, LaunchAgent"
    )
    ps = pn.add_subparsers(dest="panel_cmd", required=True)

    b = ps.add_parser("build", help="choose advertisers -> DATA derived/panels/<id>.json (offline)")
    b.add_argument("--size", type=int, required=True)
    b.add_argument("--seed", type=int, required=True)
    b.add_argument("--list-pages", type=int, default=1)
    b.add_argument("--max-new-ads", type=int, default=20)
    b.add_argument("--max-tasks-per-minute", type=float, default=crawl.DEFAULT_RATE)

    def _build(args):
        out, p = panel.build(args.size, args.seed)
        h = panel.panel_hash(out)
        print(
            json.dumps(
                {
                    "panel": str(out.relative_to(store.data_dir())),
                    "panel_hash": h,
                    "advertisers": len(p["advertisers"]),
                    "bounds_to_sign": panel.suggested_bounds(
                        p, h, args.list_pages, args.max_new_ads, args.max_tasks_per_minute
                    ),
                },
                indent=1,
            )
        )
        return 0

    b.set_defaults(fn=_build)

    r = ps.add_parser("run", help="one scheduled panel run, inside a standing approval (networked)")
    r.add_argument("--panel", required=True)
    r.add_argument("--dry-run", action="store_true", help="check everything, send nothing, spend nothing")
    r.add_argument("--list-pages", type=int, default=1, help="list pages per subcategory (new-ad discovery)")
    r.add_argument("--max-new-ads", type=int, default=20)
    r.add_argument("--concurrency", type=int, default=crawl.CONCURRENCY)
    r.add_argument("--max-tasks-per-minute", type=float, default=crawl.DEFAULT_RATE)
    r.add_argument("--approve-politeness-override", action="store_true")
    r.add_argument("--driver")
    r.add_argument("--fixtures")
    r.add_argument("--variant")
    r.set_defaults(fn=panel.run)

    pa = ps.add_parser("pause", help="the kill switch: no panel run until resumed")
    pa.add_argument("--reason", default="")
    pa.set_defaults(fn=lambda a: print(json.dumps(panel.pause(a.reason))) or 0)
    ps.add_parser("resume").set_defaults(fn=lambda a: print(json.dumps(panel.resume())) or 0)

    i = ps.add_parser("install", help="render the LaunchAgent plist (and, with --bootstrap, load it)")
    i.add_argument("--panel", required=True)
    i.add_argument("--repo", default=".", help="the listing-lens checkout the agent runs from")
    i.add_argument("--weekday", type=int, default=1, help="0-6, Sunday = 0 (launchd)")
    i.add_argument("--hour", type=int, default=10)
    i.add_argument("--plist-dir", help="default ~/Library/LaunchAgents")
    i.add_argument(
        "--bootstrap", action="store_true", help="also `launchctl bootstrap` it (the operator's step)"
    )
    i.set_defaults(fn=panel.install)
    u = ps.add_parser("uninstall", help="bootout (with --bootstrap) and remove the plist")
    u.add_argument("--plist-dir")
    u.add_argument("--bootstrap", action="store_true")
    u.set_defaults(fn=panel.uninstall)
