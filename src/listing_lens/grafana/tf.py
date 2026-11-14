"""The Grafana objects as an OpenTofu stack: folder, 7 dashboards, the datasource — imported, then
managed by plan/apply. `llens-grafana tf plan|apply` (via `nix run .#grafana-tf`).

The Terraform JSON is EMITTED from model.py/boards.py, so the uids, titles and datasource fields have
one source (a terranix module would hold a second copy of each in Nix). Everything else follows the
fleet precedent (nix-config infra/grafana/stack.nix): provider from nixpkgs (opentofu.withPlugins),
credentials as the provider's env vars via `secret exec`, an import block per object (an import is a
no-op once in state, and with state lost it re-adopts instead of creating), the datasource secret
ignored + prevent_destroy, state encrypted with a pbkdf2 key handed over in TF_ENCRYPTION.

STATE lives under the persona's DATA root (Drive-synced, the operator's choice 2026-11-08):
$LLENS_DATA/tofu/grafana.tfstate — never a path in git. Drive gives no locking and resolves
conflicts by writing a sibling copy, so:
- one local run at a time: fcntl.flock on a LOCAL lock file (never on the Drive path);
- plan and apply refuse while any sibling of the state file exists (Drive's "grafana (1).tfstate");
- apply refuses when the state changed since its plan (sha256 recorded at plan time);
- only tofu writes the state.
OUT of state, as before: folder/datasource permissions and the service account (`llens-grafana perms`).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from . import boards, model, v2

STATE_NAME = "grafana.tfstate"
KNOWN = {STATE_NAME, STATE_NAME + ".backup"}  # tofu's own files; anything else named grafana* is a conflict


def _tf_name(uid: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", uid.lower())


# The folder's and the datasource's resource names follow their (env-overridable) uids, so a stack imported
# under the live uids keeps its state addresses.
FOLDER_RES = _tf_name(model.FOLDER_UID)
DS_RES = _tf_name(model.DS_UID)
# The state/plan encryption's key-provider and method name. The encrypted state records it, so it must stay
# the name the state was first encrypted under (bound outside the repo for a live stack).
TOFU_KEY = re.sub(r"[^a-z0-9_]", "_", os.environ.get("LLENS_TOFU_KEY_NAME", "llens").lower())


# The dashboards' resource type. v2 (dashboard.grafana.app/v2, GA on 13.4) carries tabs; v1 is the
# ROLLBACK: LLENS_GRAFANA_SCHEMA=v1 renders the same boards as grafana_dashboard again. Either way the other
# type is FORGOTTEN (`removed`, destroy = false) and this one IMPORTED by uid, so switching destroys no
# dashboard. Lossless back to v1 only while no board uses tabs (v1 cannot hold a TabsLayout).
V2_TYPE, V1_TYPE = "grafana_apps_dashboard_dashboard_v2", "grafana_dashboard"


def schema() -> str:
    s = os.environ.get("LLENS_GRAFANA_SCHEMA", "v2")
    if s not in ("v1", "v2"):
        raise SystemExit(f"llens-grafana tf: LLENS_GRAFANA_SCHEMA must be v1 or v2, got {s!r}")
    return s


def _dash_v2(b: dict) -> dict:
    return {
        "metadata": {"uid": b["uid"], "folder_uid": f"${{grafana_folder.{FOLDER_RES}.uid}}"},
        "spec": {
            "json": f'${{file("${{path.module}}/boards/{b["uid"]}.json")}}',
            # import fills title/tags from the spec; set them equal so the first plan does not churn
            "title": b["title"],
            "tags": b.get("tags", []),
        },
        # import records overwrite = true and allow_ui_updates = false (UI edits stay locked)
        "options": {"overwrite": True, "allow_ui_updates": False},
    }


def _dash_v1(b: dict) -> dict:
    return {
        "folder": f"${{grafana_folder.{FOLDER_RES}.uid}}",
        "config_json": f'${{file("${{path.module}}/boards/{b["uid"]}.json")}}',
        # no `overwrite`: it only matters when CREATING over an existing uid; a managed (imported)
        # dashboard updates by uid, and the attribute alone made the first plan read "7 to change"
    }


def board_file(b: dict, which: str | None = None) -> dict:
    """What boards/<uid>.json holds: the v2 spec, or (rollback) the v1 dashboard JSON."""
    if (which or schema()) == "v2":
        return v2.spec(_board_json(b, keep_tabs=True))
    return _board_json(b)


def render(built: list[dict], which: str | None = None) -> dict:
    """main.tf.json for the stack; dashboards read their generated JSON from boards/<uid>.json."""
    which = which or schema()
    keep, drop = (V2_TYPE, V1_TYPE) if which == "v2" else (V1_TYPE, V2_TYPE)
    make = _dash_v2 if which == "v2" else _dash_v1
    dash = {_tf_name(b["uid"]): make(b) for b in built}
    ds = model.DS
    return {
        "terraform": {
            "required_providers": {"grafana": {"source": "grafana/grafana"}},
            "backend": {"local": {}},  # path given at init (-backend-config), never in git
        },
        "variable": {k: {"type": "string"} for k in ("ds_url", "ds_user", "ds_database", "ds_sslmode")},
        "provider": {"grafana": {}},  # GRAFANA_URL, GRAFANA_AUTH and GRAFANA_STACK_ID from the env
        "resource": {
            "grafana_folder": {FOLDER_RES: {"uid": model.FOLDER_UID, "title": model.FOLDER_TITLE}},
            keep: dash,
            "grafana_data_source": {
                DS_RES: {
                    "uid": model.DS_UID,
                    "name": f"{model.DS_UID} (Neon, read-only)",
                    "type": ds["type"],
                    "url": "${var.ds_url}",
                    "username": "${var.ds_user}",
                    "json_data_encoded": (
                        "${jsonencode({database = var.ds_database, sslmode = var.ds_sslmode, "
                        "postgresVersion = 1700, maxOpenConns = 2, maxIdleConns = 1, "
                        "connMaxLifetime = 14400, timescaledb = false})}"
                    ),
                    # the password exists only in Grafana: never in config, plan or state
                    "lifecycle": {"ignore_changes": ["secure_json_data_encoded"], "prevent_destroy": True},
                }
            },
        },
        "import": [
            {"to": f"grafana_folder.{FOLDER_RES}", "id": model.FOLDER_UID},
            {"to": f"grafana_data_source.{DS_RES}", "id": model.DS_UID},
            *[{"to": f"{keep}.{_tf_name(b['uid'])}", "id": b["uid"]} for b in built],
        ],
        "removed": [{"from": f"{drop}.{_tf_name(b['uid'])}", "lifecycle": {"destroy": False}} for b in built],
    }


SOURCE_ONLY = ("q", "expect_rows")  # panel keys the generator keeps for checks; Grafana never sees them

# variables whose saved default comes from the live data at plan time, so the board opens on a real value
# without that value (an address) ever being written into the repo
LIVE_DEFAULTS = {
    "viewpoint": "SELECT min(label) FROM viewpoint",
    # the explorer board's dossier opens on the newest UNMASKED ad (a gold ad's li answers read NULL, so it looks empty),
    # not on an empty ad_id that read as a broken board (listy's check 2026-11-13)
    "ad_id": "SELECT max(ad_id) FROM listings_masked WHERE NOT gold_masked",
}


def apply_live_defaults(built: list[dict], first_value) -> None:
    """Set each LIVE_DEFAULTS variable's saved current value from `first_value(sql) -> str | None`."""
    for name, sql in LIVE_DEFAULTS.items():
        val = first_value(sql)
        if not val:
            continue
        for b in built:
            for v in b["templating"]["list"]:
                if v["name"] == name:
                    v["current"] = {"text": val, "value": val}


SLOW_S = 2.0  # a marked target slower than this is reported at plan (a warning, never a refusal)


def empty_violations(built: list[dict], query, slow: list[str] | None = None) -> list[str]:
    """Every target a panel marks in expect_rows must return >= 1 row at the board default: a panel can
    render EMPTY without any error (the 2026-11-13 viewpoint rings), and only data shows it."""
    out = []
    for b in built:
        names = {v["name"]: v for v in b["templating"]["list"]}
        for p_ in model._walk_panels(b["panels"]):
            want = p_.get("expect_rows") or ()
            for t_ in p_.get("targets", []):
                if t_.get("refId") not in want:
                    continue
                t0 = time.monotonic()
                n = query(model.render(t_["rawSql"], names, "all"))
                dt = time.monotonic() - t0
                if slow is not None and dt > SLOW_S:
                    slow.append(f"{b['uid']} '{p_.get('title', '')[:40]}' {t_['refId']}: {dt:.1f} s")
                if not n:
                    why = "failed" if n is None else "returned 0 rows"
                    out.append(
                        f"{b['uid']} '{p_.get('title', '')[:40]}' {t_['refId']}: expected rows, query {why}"
                    )
    return out


V1_ONLY_DROP = ("tab",)  # a row's tab marker: read by v2.py, meaningless (dropped) in v1 JSON


def _strip(p: dict, drop: tuple) -> dict:
    out = {k: v for k, v in p.items() if k not in drop}
    if "panels" in out:
        out["panels"] = [_strip(c, drop) for c in out["panels"]]
    return out


def _board_json(b: dict, *, keep_tabs: bool = False) -> dict:
    # Grafana owns id and version; keeping them out lets the first plan read 0 to change
    drop = SOURCE_ONLY if keep_tabs else SOURCE_ONLY + V1_ONLY_DROP
    return {
        k: (v if k != "panels" else [_strip(p, drop) for p in v])
        for k, v in b.items()
        if k not in ("id", "version")
    }


def conflict_siblings(state_dir: Path) -> list[str]:
    if not state_dir.is_dir():
        return []
    return sorted(p.name for p in state_dir.iterdir() if p.name.startswith("grafana") and p.name not in KNOWN)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "absent"


def _ds_vars(dsn: str) -> dict:
    from .apply import direct_host

    u = urllib.parse.urlsplit(dsn)
    q = dict(urllib.parse.parse_qsl(u.query))
    return {
        "TF_VAR_ds_url": f"{direct_host(u.hostname or '')}:{u.port or 5432}",
        "TF_VAR_ds_user": urllib.parse.unquote(u.username or ""),
        "TF_VAR_ds_database": u.path.lstrip("/"),
        "TF_VAR_ds_sslmode": q.get("sslmode", "require"),
    }


def _encryption(passphrase: str) -> str:
    p = json.dumps(passphrase)  # an HCL string literal
    return f"""
key_provider "pbkdf2" "{TOFU_KEY}" {{
  passphrase = {p}
}}
method "aes_gcm" "{TOFU_KEY}" {{
  keys = key_provider.pbkdf2.{TOFU_KEY}
}}
state {{
  method = method.aes_gcm.{TOFU_KEY}
  enforced = true
}}
plan {{
  method = method.aes_gcm.{TOFU_KEY}
  enforced = true
}}
"""


# A row cap is written `LIMIT n /* cap */` (top-N LIMITs are not caps and are not checked). A query that
# returns n rows has been CUT — the Explorer silently dropped 288 ads at 1,000 (2026-11-08) — so a plan
# refuses until the cap is raised.
_CAP = re.compile(r"LIMIT\s+(\d+)\s*/\*\s*cap\s*\*/")


def cap_violations(built: list[dict], query) -> list[str]:
    """`query(sql) -> row count` (None on error). Every capped target, rendered at the board default (All)."""
    out = []
    for b in built:
        names = {v["name"]: v for v in b["templating"]["list"]}

        def walk(ps):
            for p_ in ps:
                yield p_
                yield from walk(p_.get("panels", []))

        for p_ in walk(b["panels"]):
            for t_ in p_.get("targets", []):
                m = _CAP.search(t_.get("rawSql", ""))
                if not m:
                    continue
                cap, n = int(m[1]), query(model.render(t_["rawSql"], names, "all"))
                if n is None:
                    out.append(f"{b['uid']} '{p_.get('title', '')[:40]}': capped query failed")
                elif n >= cap:
                    out.append(
                        f"{b['uid']} '{p_.get('title', '')[:40]}': {n} rows = its cap {cap} — rows are being cut"
                    )
    return out


def _grafana_rows(url: str, token: str, *, first: bool = False):
    import urllib.error
    import urllib.request

    def q(sql: str):
        body = {
            "queries": [{"refId": "A", "datasource": {"uid": model.DS_UID}, "rawSql": sql, "format": "table"}]
        }
        r = urllib.request.Request(
            url.rstrip("/") + "/api/ds/query",
            json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(r, timeout=90) as resp:
                res = json.load(resp)["results"]["A"]
        except urllib.error.HTTPError:
            return None
        if res.get("error"):
            return None
        vals = ((res.get("frames") or [{}])[0].get("data") or {}).get("values") or [[]]
        return (vals[0][0] if vals and vals[0] else None) if first else (len(vals[0]) if vals else 0)

    return q


def _stack_id(url: str, token: str) -> str:
    """The Grafana Cloud stack id, from /api/frontend/settings' namespace ("stacks-<id>")."""
    import urllib.request

    if os.environ.get("GRAFANA_STACK_ID"):
        return os.environ["GRAFANA_STACK_ID"]
    r = urllib.request.Request(
        url.rstrip("/") + "/api/frontend/settings", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(r, timeout=30) as resp:
        ns = json.load(resp).get("namespace", "")
    m = re.fullmatch(r"stacks-(\d+)", ns)
    if not m:
        raise SystemExit(
            f"llens-grafana tf: cannot read the stack id (namespace {ns!r}); set GRAFANA_STACK_ID"
        )
    return m[1]


def _need(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"llens-grafana tf: {name} is required")
    return v


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd not in ("plan", "apply"):
        print("usage: llens-grafana tf plan|apply", file=sys.stderr)
        return 2
    built = boards.build_all()
    errs = [e for b in built for e in model.check(b)]
    if errs:
        print("llens-grafana tf: refusing, hard-rule violations:\n  " + "\n  ".join(errs), file=sys.stderr)
        return 2

    state_dir = Path(_need("LLENS_DATA")) / "tofu"
    state = state_dir / STATE_NAME
    local = (
        Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        / "listing-insights"
        / "tofu-grafana"
    )
    local.mkdir(parents=True, exist_ok=True)
    lock = open(local / "run.lock", "w")  # noqa: SIM115 — held for the whole run
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("llens-grafana tf: another plan/apply holds the lock; refusing", file=sys.stderr)
        return 3

    sib = conflict_siblings(state_dir)
    if sib:
        print(
            f"llens-grafana tf: refusing — a sync conflict copy sits next to the state: {', '.join(sib)}. "
            "Reconcile by hand (compare serials), keep ONE grafana.tfstate, then re-run.",
            file=sys.stderr,
        )
        return 4

    tofu = os.environ.get("LLENS_TOFU", "tofu")
    env = {
        **os.environ,
        "GRAFANA_URL": _need("LLENS_GRAFANA_URL"),
        "GRAFANA_AUTH": _need("GRAFANA_TOKEN"),
        "TF_ENCRYPTION": _encryption(_need("LLENS_TOFU_PASSPHRASE")),
        "TF_IN_AUTOMATION": "1",
        **_ds_vars(_need("LLENS_PG_DSN")),
    }
    work = local / "work"
    plan_file, plan_sha = local / "plan.tfplan", local / "plan.state-sha256"

    def with_stack(e: dict) -> dict:
        # the App Platform (v2) resources need the stack's namespace, in BOTH schemas: the v1 rollback must
        # still refresh the v2 resources it forgets (without it: HTTP 403, measured 2026-11-13). Read from the
        # stack itself, so no stack id is written anywhere; after the refusal checks (no network before them).
        if "GRAFANA_STACK_ID" not in e:
            e = {**e, "GRAFANA_STACK_ID": _stack_id(e["GRAFANA_URL"], e["GRAFANA_AUTH"])}
        return e

    if cmd == "plan":
        env = with_stack(env)
        rows = _grafana_rows(env["GRAFANA_URL"], env["GRAFANA_AUTH"])
        apply_live_defaults(built, _grafana_rows(env["GRAFANA_URL"], env["GRAFANA_AUTH"], first=True))
        cut = cap_violations(built, rows)
        if cut:
            print(
                "llens-grafana tf: refusing, a capped query is cut:\n  " + "\n  ".join(cut), file=sys.stderr
            )
            return 6
        slow: list[str] = []
        empty = empty_violations(built, rows, slow)
        if slow:  # the 7 s rings hid behind "it renders" for a day (2026-11-13): say it, don't block on it
            print(
                f"llens-grafana tf: WARNING, a marked target took > {SLOW_S:.0f} s:\n  " + "\n  ".join(slow),
                file=sys.stderr,
            )
        if empty:
            print(
                "llens-grafana tf: refusing, a panel would render empty:\n  " + "\n  ".join(empty),
                file=sys.stderr,
            )
            return 7
        if work.exists():
            shutil.rmtree(work)
        (work / "boards").mkdir(parents=True)
        (work / "main.tf.json").write_text(json.dumps(render(built), indent=1))
        for b in built:
            (work / "boards" / f"{b['uid']}.json").write_text(json.dumps(board_file(b), indent=1))
        state_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [tofu, "init", "-input=false", "-reconfigure", f"-backend-config=path={state}"],
            cwd=work,
            env=env,
            check=True,
        )
        rc = subprocess.run(
            [tofu, "plan", "-input=false", "-detailed-exitcode", f"-out={plan_file}"], cwd=work, env=env
        ).returncode
        if rc not in (0, 2):
            return rc
        plan_sha.write_text(_sha(state))
        print(f"llens-grafana tf: plan saved; state sha256 at plan = {plan_sha.read_text()[:12]}")
        return 0

    # apply: only the saved plan, only over the state it was made against
    if not plan_file.exists() or not plan_sha.exists() or not work.exists():
        print("llens-grafana tf: no saved plan; run `plan` first", file=sys.stderr)
        return 2
    if _sha(state) != plan_sha.read_text():
        print(
            "llens-grafana tf: refusing — the state changed since the plan (sync or another run); re-plan",
            file=sys.stderr,
        )
        return 5
    rc = subprocess.run(
        [tofu, "apply", "-input=false", str(plan_file)], cwd=work, env=with_stack(env)
    ).returncode
    plan_file.unlink(missing_ok=True)
    plan_sha.unlink(missing_ok=True)
    return rc
