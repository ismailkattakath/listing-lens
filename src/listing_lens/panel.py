"""The H4 panel: scheduled revisits of chosen advertisers' ads, plus a light list crawl that finds their
NEW ads, all inside ONE standing approval (scrape-harness approvals: Touch ID signs bounds once).

  llens panel build --size N --seed S     choose advertisers -> DATA derived/panels/<id>.json; prints its
                                       hash and the bounds to sign (`llens approvals standing`)
  llens panel run --panel FILE            refuses when paused or while ANY incident is open; checks the run
                                       against a standing approval BEFORE any request (offline backends
                                       need none); revisits get "gone" on 404/410; matched new ads are
                                       fetched within the signed ad cap. --dry-run spends nothing.
  llens panel pause --reason / resume     the kill switch (STATE/panel/paused)
  llens panel install / uninstall         the LaunchAgent (rendered from the environment, never committed)

Every stop is an incident (scrape-harness); the next scheduled run refuses until it is acked.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from scrape_harness import approvals, contracts, crawl, egress, incidents, pipeline, proxy, store
from scrape_harness.cli import _execute, _networked, _variant, card
from scrape_harness.interfaces import load

from . import site
from .advertisers import keys

EXIT_INCIDENT_OPEN = 11
LABEL = "llens-panel"
ARG0 = "nix-llens-panel"  # the fleet's launchd naming rule: arg0 basename nix-<kebab>


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state() -> Path:
    p = store.state_dir() / "panel"
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p


def panel_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def card_keys(obs: dict) -> set[str]:
    """The keys a LIST CARD can show (name, title): how a new ad is recognised as a panel advertiser's."""
    key = site.source().record_key
    fake = {
        "content": {"title": obs["card"].get("title"), "description": ""},
        "site": {key: {"name": (obs["card"].get(key) or {}).get("name")}},
        "location": {},
    }
    return {k for k in keys(fake) if k.split(":", 1)[0] in ("n", "t")}


# ── build ────────────────────────────────────────────────────────────────────────────────────────


def build(size: int, seed: int) -> tuple[Path, dict]:
    listings = {r["ad_id"]: r for r in store.read("derived")}
    pool = [a for a in store.read("advertisers") if len(a["ad_ids"]) > 1 and not a["venue_only"]]
    if len(pool) < size:
        raise SystemExit(f"only {len(pool)} multi-ad advertisers (not venue-only); asked for {size}")
    by_sub: dict[str, list[dict]] = {}
    for a in pool:
        sub = listings[a["ad_ids"][0]]["source"]["subcategory"]
        by_sub.setdefault(sub, []).append(a)
    rng = random.Random(seed)
    picks: list[dict] = []
    for sub in sorted(by_sub):  # proportional allocation, largest remainder (deterministic)
        k = round(size * len(by_sub[sub]) / len(pool))
        picks += rng.sample(sorted(by_sub[sub], key=lambda a: a["advertiser_id"]), min(k, len(by_sub[sub])))
    picks = sorted(picks, key=lambda a: a["advertiser_id"])[:size]
    created = utcnow()
    panel = {
        "schema": "panel.v1",
        "panel_id": f"panel-{created.replace(':', '').replace('-', '')}-{seed}",
        "created_at": created,
        "seed": seed,
        "site": listings[picks[0]["ad_ids"][0]]["source"]["site"],
        "region": listings[picks[0]["ad_ids"][0]]["source"]["region"],
        "subcategories": sorted({listings[a["ad_ids"][0]]["source"]["subcategory"] for a in picks}),
        "advertisers": [
            {
                "advertiser_id": a["advertiser_id"],
                "ads": [{"ad_id": i, "url": listings[i]["source"]["url"]} for i in a["ad_ids"]],
                "card_keys": sorted(
                    {k for i in a["ad_ids"] for k in keys(listings[i]) if k.split(":", 1)[0] in ("n", "t")}
                ),
            }
            for a in picks
        ],
    }
    contracts.validate("panel.v1", panel)
    out = store.data_dir() / "derived" / "panels" / f"{panel['panel_id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(panel, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    return out, panel


def suggested_bounds(panel: dict, h: str, list_pages: int, max_new: int, rate: float) -> dict:
    n_ads = sum(len(a["ads"]) for a in panel["advertisers"])
    return {
        "panel_hash": h,
        "site": panel["site"],
        "max_runs": 13,
        "max_ad_requests_per_run": n_ads + max_new,
        "max_list_pages_per_run": list_pages * len(panel["subcategories"]),
        "min_interval_s": 6 * 24 * 3600,
        "not_before": utcnow(),
        "expires_at": "<now + at most 90 days, ISO-8601 Z>",
        "egress_country": egress.home_country(),
        "variants": ["desktop"],
        "max_rate_per_minute": rate,
        **_proxy(),
    }


def _proxy(panel_hash: str | None = None) -> dict:
    """The run's ONE proxy URL ([crawl.proxy], never its credentials), activated for this process. Bounds
    (no panel_hash) CHOOSE it, to be signed; a scheduled run takes the URL its standing signature names."""
    ps = proxy.proxy_set()
    if not ps:
        return {}
    signed = (
        [b["proxy"] for b in approvals.standing_bounds(panel_hash) if b.get("proxy") in ps.urls]
        if panel_hash
        else []
    )
    url = signed[0] if signed else proxy.choose(ps)
    proxy.activate(url)
    return {"proxy": url}


# ── run ──────────────────────────────────────────────────────────────────────────────────────────


def paused() -> dict | None:
    p = _state() / "paused"
    return json.loads(p.read_text()) if p.exists() else None


def run(args) -> int:
    if (p := paused()) is not None:
        print(json.dumps({"panel": "paused", **p}))
        return 0
    if open_ := incidents.open_ones():
        print(
            f"refusing: {len(open_)} open incident(s) ({', '.join(i['id'] for i in open_)}); ack them first "
            "(`llens incidents ack ID --note ...`). No request was sent.",
            file=sys.stderr,
        )
        return EXIT_INCIDENT_OPEN
    path = Path(args.panel)
    panel, h = json.loads(path.read_text()), panel_hash(path)
    contracts.validate("panel.v1", panel)
    source = load("sources", panel["site"])
    variant = _variant(args, source, "ad")
    lists = [
        {
            "url": source.list_url(sub, panel["region"], page),
            "kind": "list",
            "meta": {"subcategory": sub, "region": panel["region"], "page": page, "page_count": None},
        }
        for sub in panel["subcategories"]
        for page in range(1, args.list_pages + 1)
    ]
    known = {ad["ad_id"] for a in panel["advertisers"] for ad in a["ads"]}
    revisits = [
        {"url": ad["url"], "kind": "ad", "meta": {"reason": "panel", "revisit": True, "ad_id": ad["ad_id"]}}
        for a in panel["advertisers"]
        for ad in a["ads"]
    ]
    networked = _networked(args, [variant])
    run_doc = {
        "panel_hash": h,
        "site": panel["site"],
        "ad_requests": len(revisits) + args.max_new_ads,
        "list_pages": len(lists),
        "variant": variant,
        "rate_per_minute": args.max_tasks_per_minute,
        "egress_country": egress.home_country(),
        **(_proxy(h) if networked else {}),  # the URL this panel's standing signature names
    }
    if args.dry_run:
        check = approvals.consume_standing(run_doc, dry=True) if networked else {"offline": True}
        print(
            json.dumps({"would_send": {"list": len(lists), "ad": run_doc["ad_requests"]}, "approval": check})
        )
        return 0
    if networked:  # refuse BEFORE the run record exists: a refused run leaves no trace in DATA
        approvals.consume_standing(run_doc, dry=True)
    job = crawl.Run.new("panel", variant, panel["site"])
    if networked:
        job.doc["approval_ref"] = approvals.consume_standing(run_doc, job.doc["run_id"])
    by_key = {k: a["advertiser_id"] for a in panel["advertisers"] for k in a["card_keys"]}
    budget = {"new": args.max_new_ads}

    def handle(m: dict, res):
        if m["kind"] == "list":
            lp = source.parse_list(
                res.html, url=res.url, subcategory=m["subcategory"], page=m["page"], seen_at=utcnow()
            )
            for o in lp.observations:
                o["crawl_id"] = job.doc["run_id"]
            store.append("observations", lp.observations)
            new = []
            for o in lp.observations:
                if o["ad_id"] in known or budget["new"] <= 0:
                    continue
                if card_keys(o) & by_key.keys():
                    known.add(o["ad_id"])
                    budget["new"] -= 1
                    new.append(
                        {"url": o["ad_url"], "kind": "ad", "meta": {"reason": "new", "ad_id": o["ad_id"]}}
                    )
            return new
        rec = source.parse_ad(res.html, url=res.url, fetched_at=utcnow(), fetch_reason=m["reason"])
        rec["raw_ref"] = store.write_raw(source.site, rec["ad_id"], rec["fetched_at"], res.html)
        store.append("listings", [rec])
        return None

    card(
        "panel run",
        f"panel {panel['panel_id']}: {len(revisits)} revisits + up to {args.max_new_ads} new ads, {len(lists)} list pages",
        state=True,
        planned={"ad": run_doc["ad_requests"], "list": len(lists), "robots": 1},
        per_minute=args.max_tasks_per_minute,
    )
    return _execute(args, job, source, handle, lists + revisits)


def pause(reason: str) -> dict:
    if not reason.strip():
        raise SystemExit("panel pause needs a --reason")
    rec = {"paused_at": utcnow(), "reason": reason.strip()}
    (_state() / "paused").write_text(json.dumps(rec) + "\n")
    return rec


def resume() -> dict:
    p = _state() / "paused"
    was = json.loads(p.read_text()) if p.exists() else None
    p.unlink(missing_ok=True)
    return {"resumed": True, "was": was}


# ── the LaunchAgent: rendered from THIS machine's environment, never committed ───────────────────

PASS_ENV = ("data_env", "state_env")


def plist(panel_file: str, weekday: int, hour: int, repo: str) -> str:
    """launchd.plist(5): StartCalendarInterval coalesces missed runs into one on wake."""
    import plistlib
    import shlex

    c = pipeline.consumer()
    env = {name: os.environ[name] for name in (getattr(c, k) for k in PASS_ENV) if os.environ.get(name)}
    for knob in ("APPROVAL_SIGNERS", "HOME_COUNTRY"):
        if os.environ.get(pipeline.knob_name(knob)):
            env[pipeline.knob_name(knob)] = os.environ[pipeline.knob_name(knob)]
    env["PATH"] = "/nix/var/nix/profiles/default/bin:/usr/bin:/bin"
    nix = shutil.which("nix") or "/nix/var/nix/profiles/default/bin/nix"
    logs = _state() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    doc = {
        "Label": LABEL,
        "ProgramArguments": [
            "/bin/sh",
            "-c",
            f"exec -a {ARG0} {shlex.quote(nix)} run {shlex.quote(repo + '#default')}"
            f" -- panel run --panel {shlex.quote(panel_file)}",
        ],
        "StartCalendarInterval": {"Weekday": weekday, "Hour": hour, "Minute": 0},
        "EnvironmentVariables": env,
        "StandardOutPath": str(logs / "panel.out.log"),
        "StandardErrorPath": str(logs / "panel.err.log"),
        "ProcessType": "Background",
        "RunAtLoad": False,
    }
    return plistlib.dumps(doc).decode()


def plist_path(plist_dir: str | None) -> Path:
    return Path(plist_dir or Path.home() / "Library" / "LaunchAgents") / f"{LABEL}.plist"


def install(args) -> int:
    for name in (pipeline.consumer().data_env, pipeline.knob_name("APPROVAL_SIGNERS")):
        if not os.environ.get(name):
            raise SystemExit(f"{name} must be set: the agent runs with this machine's environment")
    p = plist_path(args.plist_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        plist(str(Path(args.panel).resolve()), args.weekday, args.hour, str(Path(args.repo).resolve()))
    )
    out = {"plist": str(p), "bootstrapped": False}
    if args.bootstrap:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(p)], check=True)
        out["bootstrapped"] = True
    print(json.dumps(out))
    return 0


def uninstall(args) -> int:
    p = plist_path(args.plist_dir)
    if args.bootstrap:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False)
    existed = p.exists()
    p.unlink(missing_ok=True)
    print(json.dumps({"plist": str(p), "removed": existed}))
    return 0
