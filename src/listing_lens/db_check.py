"""The `llens db` group: the derived database's commands under one name.

- `db export` and `db token` are `export-db` and `api-token` by another name: the same argument definition
  (`export_db.export_args`, `api_token.token_args`), the same function, the same gate and exit codes.
- `db check` runs every Grafana board target against the export DB, read-only: the board default (All) is
  EXECUTED, the single/multi states are EXPLAINed, and a query variable with no saved default is resolved
  live to its first option, as Grafana opens it. It reports COUNTS only: rows per target, empties, errors
  (an error's class and SQLSTATE, never its message, which can echo a value or the DSN's parts).
- `db api-check` proves the Neon Data API's access rules (export-db@25/@26) by status codes: the operator's
  token reads rows; an anonymous token gets 403; no token gets 401; schema li, a withheld table and a public
  RPC are unreachable (404/406). Never a URL, the email, a token or a response body.

Both reach our own metered database, so they are `paid` like export-db: `--approve-network`, and the secret
only by NAME (`LLENS_DB_URL`, `LLENS_API_PASSWORD`, set by `secret exec`). Neither reads or writes DATA.
`db check --dry-run` builds the boards and counts the targets, offline."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager

from scrape_harness.cli import approve_network_arg, need_network

from . import api_token, export_db, site

STATES = ("all", "single", "multi")  # all is executed; single/multi carry placeholders, so EXPLAIN only
SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")
TIMEOUT_S = 30
EXIT_FAILED = 1  # a failed expectation; refusals keep their own codes (3 network, 2 config, 11 db/auth)


class QueryFailed(Exception):
    """A statement failed: its error class and SQLSTATE only (the message never leaves the adapter)."""

    def __init__(self, kind: str, sqlstate: str | None):
        super().__init__(f"{kind} (sqlstate {sqlstate})")
        self.kind = kind
        self.sqlstate = sqlstate if sqlstate and SQLSTATE.match(sqlstate) else None


class PgDb:
    """A psycopg connection inside ONE read-only transaction; each statement runs in its own savepoint,
    so a failing target never aborts the ones after it."""

    def __init__(self, conn):
        self.conn = conn

    def _run(self, sql: str):
        import psycopg

        try:
            with self.conn.transaction():  # a savepoint inside the outer read-only transaction
                cur = self.conn.execute(sql)
                return cur.fetchall() if cur.description else []
        except psycopg.Error as e:
            raise QueryFailed(type(e).__name__, getattr(e, "sqlstate", None)) from None

    def count(self, sql: str) -> int:
        return len(self._run(sql))

    def first(self, sql: str):
        rows = self._run(sql)
        return rows[0] if rows else None

    def explain(self, sql: str) -> None:
        self._run("EXPLAIN " + sql)


@contextmanager
def connect(dsn: str, timeout_s: int):
    """The export DB, read-only: `BEGIN READ ONLY` (psycopg's read_only), li then public on the search path
    (as the boards' role has it), and a statement timeout."""
    import psycopg

    with psycopg.connect(dsn, connect_timeout=30) as conn:
        conn.read_only = True
        with conn.transaction():
            conn.execute("SET LOCAL search_path = li, public")
            conn.execute(f"SET LOCAL statement_timeout = {int(timeout_s) * 1000}")
            yield PgDb(conn)


def boards() -> list[dict]:
    """The shipped boards, built (imported here: boards.py reads the site file's [boards] at import)."""
    from .grafana import boards as b

    return b.build_all()


def _query_sql(v: dict) -> str:
    q = v.get("query")
    return (q.get("rawSql") or q.get("query")) if isinstance(q, dict) else q


def _targets(b: dict):
    from .grafana import model

    for p in model._walk_panels(b["panels"]):  # noqa: SLF001 (the model's own panel walk)
        for i, t in enumerate(p.get("targets", [])):
            if t.get("rawSql"):
                yield p.get("id"), str(t.get("refId", i)), t["rawSql"]


def _err(state: str, e: QueryFailed) -> dict:
    return {"state": state, "kind": e.kind, "sqlstate": e.sqlstate}


def check(db, built: list[dict], dry_run: bool = False) -> dict:
    """Every board target, read-only, through `db` (PgDb or a fake): counts only."""
    from .grafana import model

    built = copy.deepcopy(built)  # a resolved variable is set on the copy, never on the caller's boards
    out = {
        "schema": "db_check.v1",
        "dry_run": dry_run,
        "boards": len(built),
        "targets": 0,
        "executed": 0,
        "explained": 0,
        "empty": 0,
        "errors": 0,
        "variables": {"resolved": 0, "empty": 0, "errors": 0},
        "results": [],
        "variable_results": [],
    }
    for b in built:
        names = {v["name"]: v for v in b["templating"]["list"]}
        for v in b["templating"]["list"]:
            if v.get("type") != "query" or v.get("includeAll") or (v.get("current") or {}).get("value"):
                continue  # All, or a saved default: what the board opens on is already known
            vr = {"board": b["uid"], "variable": v["name"], "status": "resolved", "error": None}
            if dry_run:
                vr["status"] = "planned"
            else:
                try:
                    row = db.first(model.strip_time_macros(model.render(_query_sql(v), names, "all")))
                    if row is None:
                        vr["status"] = "empty"
                    else:  # the live value stays in memory: it feeds the panels, it is never printed
                        v["current"] = {"value": str(row[0]), "text": "(resolved)"}
                except QueryFailed as e:
                    vr |= {"status": "error", "error": _err("all", e)}
                out["variables"][
                    {"resolved": "resolved", "empty": "empty", "error": "errors"}[vr["status"]]
                ] += 1
            out["variable_results"].append(vr)
        for panel, ref, raw in _targets(b):
            out["targets"] += 1
            r = {"board": b["uid"], "panel": panel, "ref": ref, "rows": None, "errors": []}
            out["results"].append(r)
            if dry_run:
                continue
            for state in STATES:
                sql = model.strip_time_macros(model.render(raw, names, state))  # all time
                try:
                    if state == "all":
                        r["rows"] = db.count(sql)
                        out["executed"] += 1
                        out["empty"] += r["rows"] == 0
                    else:
                        db.explain(sql)
                        out["explained"] += 1
                except QueryFailed as e:
                    r["errors"].append(_err(state, e))
            out["errors"] += bool(r["errors"])
    out["ok"] = out["errors"] == 0 and out["variables"]["errors"] == 0
    return out


def check_table(d: dict) -> str:
    v = d["variables"]
    lines = [
        f"db check{' (dry run)' if d['dry_run'] else ''}: {d['boards']} boards, {d['targets']} targets; "
        f"executed {d['executed']}, explained {d['explained']}; empty {d['empty']}, errors {d['errors']}",
        f"query variables: resolved {v['resolved']}, empty {v['empty']}, errors {v['errors']}",
    ]
    for r in d["variable_results"]:
        if r["status"] in ("empty", "error"):
            e = r["error"] or {}
            lines.append(
                f"  {r['status'].upper():<6} {r['board']} variable {r['variable']} {e.get('kind', '')} {e.get('sqlstate') or ''}".rstrip()
            )
    for r in d["results"]:
        where = f"{r['board']} #{r['panel']} [{r['ref']}]"
        for e in r["errors"]:
            lines.append(f"  ERROR  {where} {e['state']}: {e['kind']} {e['sqlstate'] or ''}".rstrip())
        if r["rows"] == 0:
            lines.append(f"  EMPTY  {where}")
    lines.append("OK" if d["ok"] else "FAILED")
    return "\n".join(lines) + "\n"


def cmd_db_check(args: argparse.Namespace) -> int:
    built = boards()
    if args.dry_run:
        d = check(None, built, dry_run=True)
    else:
        need_network(
            args, export_db.DbNetwork(), "db check"
        )  # the same gate, in the same order, as export-db
        dsn = os.environ.get(args.dsn_env)
        if not dsn:
            sys.stderr.write(
                f"db check: {args.dsn_env} is not set; run under `secret exec {args.dsn_env}=<keychain item> -- llens db check ...`\n"
            )
            return export_db.EXIT_DB
        import psycopg

        try:
            with connect(dsn, args.timeout_s) as db:
                d = check(db, built)
        except (
            psycopg.Error
        ) as e:  # a connection error: its class only (the message can echo the DSN's parts)
            sys.stderr.write(
                f"db check: database error {type(e).__name__} (sqlstate {getattr(e, 'sqlstate', None)})\n"
            )
            return export_db.EXIT_DB
    sys.stdout.write(json.dumps(d, sort_keys=True) + "\n" if args.json else check_table(d))
    return 0 if d["ok"] else EXIT_FAILED


# ── db api-check ─────────────────────────────────────────────────────────────────────────────────

LI = "li"  # export-db's schema: never exposed
READ = "listings"  # an api view every board reads (export_db.READABLE)
WITHHELD = export_db.WITHHELD[0]  # a relation export-db withholds from every reader
PUBLIC_RPC = "postgis_version"  # PostGIS lives in schema public (export-db@9); public is not exposed
UNREACHABLE = (404, 406)  # PostgREST: PGRST205/PGRST202 (no such table/function) 404, PGRST106 (schema) 406
PGRST_CODE = re.compile(r"^(PGRST\d{3}|[0-9A-Z]{5})$")


def request(method: str, url: str, headers: dict | None = None, body: dict | None = None):
    """(status, lower-cased headers, body bytes); a fresh opener, no cookies, for every call."""
    req = urllib.request.Request(url, method=method, headers={"Origin": api_token.ORIGIN, **(headers or {})})
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.build_opener().open(req, timeout=30) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read()


def _rows(headers: dict) -> int | None:
    total = (headers.get("content-range") or "").rpartition("/")[2]
    return int(total) if total.isdigit() else None


def _code(body: bytes) -> str | None:
    try:
        c = json.loads(body or b"{}").get("code")
    except (ValueError, AttributeError):
        return None
    return c if isinstance(c, str) and PGRST_CODE.match(c) else None


def _anonymous(auth_url: str) -> str | None:
    status, _, body = request("GET", auth_url + "/token/anonymous")
    try:
        token = json.loads(body or b"{}").get("token") if status == 200 else None
    except (ValueError, AttributeError):
        return None
    return token if isinstance(token, str) and token else None


def probes(rest: str, op: str, anon: str | None) -> list[dict]:
    """Each rule, probed: one row each with its expectation, the status, the PostgREST code, a row count."""
    plan = [
        ("operator_api", f"{READ}?select=ad_id", op, None, (200, 206)),
        ("anonymous_api", f"{READ}?select=ad_id", anon, None, (403,)),
        ("no_token_api", f"{READ}?select=ad_id", "", None, (401,)),
        ("operator_li", f"{READ}?select=ad_id", op, LI, UNREACHABLE),
        ("operator_withheld", f"{WITHHELD}?select=ad_id", op, None, UNREACHABLE),
        ("operator_public_rpc", f"rpc/{PUBLIC_RPC}", op, None, UNREACHABLE),
    ]
    out = []
    for name, path, token, profile, expect in plan:
        row = {
            "name": name,
            "expect": list(expect),
            "status": None,
            "code": None,
            "rows": None,
            "skipped": None,
        }
        if token is None:  # the anonymous token was not issued: never fall through to a tokenless probe
            out.append(row | {"skipped": "no_anonymous_token", "ok": False})
            continue
        hdr = {"Prefer": "count=exact", "Range": "0-0"}
        if token:
            hdr["Authorization"] = f"Bearer {token}"
        if profile:
            hdr["Accept-Profile"] = profile
        status, headers, body = request("GET", f"{rest}/{path}", hdr)
        row |= {"status": status, "rows": _rows(headers), "code": _code(body) if status >= 400 else None}
        ok = status in expect
        if name == "operator_api":  # export-db@26's gate answers a wrong subject with an EMPTY 2xx
            ok = ok and (row["rows"] or 0) > 0
        out.append(row | {"ok": ok})
    return out


def api_table(d: dict) -> str:
    lines = [f"db api-check: signed in {'yes' if d['signed_in'] else 'no'}"]
    for p in d["probes"]:
        got = p["skipped"] or f"HTTP {p['status']}"
        rows = "" if p["rows"] is None else f" rows {p['rows']}"
        code = f" {p['code']}" if p["code"] else ""
        want = "/".join(map(str, p["expect"]))
        lines.append(f"  {'ok  ' if p['ok'] else 'FAIL'} {p['name']:<20} {got}{code}{rows} (want {want})")
    lines.append("OK" if d["ok"] else "FAILED")
    return "\n".join(lines) + "\n"


def cmd_db_api_check(args: argparse.Namespace) -> int:
    password = os.environ.get(api_token.PASSWORD_ENV)
    if not password:
        sys.stderr.write(
            f"db api-check: {api_token.PASSWORD_ENV} is not set; run under "
            f"`secret exec {api_token.PASSWORD_ENV}=<keychain item> -- llens db api-check --approve-network`\n"
        )
        return api_token.EXIT_CONFIG
    try:
        auth_url = site.get("api", "auth_url").rstrip("/")
        email, rest = site.get("api", "email"), site.get("api", "rest_url").rstrip("/")
    except site.SiteConfigError as e:
        sys.stderr.write(f"db api-check: {e}\n")
        return api_token.EXIT_CONFIG
    need_network(args, api_token.AuthNetwork(), "db api-check")
    op, why = api_token.sign_in(api_token.transport(), auth_url, email, password)
    if not op:
        sys.stderr.write(f"db api-check: {why}\n")
        return api_token.EXIT_AUTH
    ps = probes(rest, op, _anonymous(auth_url))
    d = {"schema": "api_check.v1", "signed_in": True, "probes": ps, "ok": all(p["ok"] for p in ps)}
    sys.stdout.write(json.dumps(d, sort_keys=True) + "\n" if args.json else api_table(d))
    return 0 if d["ok"] else EXIT_FAILED


# ── the group ────────────────────────────────────────────────────────────────────────────────────


def register(sub) -> None:
    g = sub.add_parser("db", help="the derived database: export, token, check, api-check")
    ds = g.add_subparsers(dest="db_command", required=True, metavar="{export,token,check,api-check}")
    export_db.export_args(ds.add_parser("export", help=f"alias of `export-db`: {export_db.HELP}"))
    api_token.token_args(ds.add_parser("token", help=f"alias of `api-token`: {api_token.HELP}"))

    c = ds.add_parser(
        "check", help="every board target, read-only, against the export DB: counts only (networked)"
    )
    c.add_argument(
        "--dry-run", action="store_true", help="build the boards and count the targets; no network"
    )
    approve_network_arg(c)
    export_db.dsn_env_arg(c)
    c.add_argument("--timeout-s", type=int, default=TIMEOUT_S, help="statement timeout per query (seconds)")
    c.add_argument("--json", action="store_true", help="one db_check.v1 object instead of the table")
    c.set_defaults(fn=cmd_db_check)

    a = ds.add_parser("api-check", help="the Neon Data API's access rules, by status codes only (networked)")
    approve_network_arg(a)
    a.add_argument("--json", action="store_true", help="one api_check.v1 object instead of the table")
    a.set_defaults(fn=cmd_db_api_check)
