"""The `llens db` group: `db export` / `db token` are export-db / api-token by another name (one definition each),
`db check` runs every board target read-only and reports COUNTS, `db api-check` proves the Data API's access
rules by status codes. Fakes stand in for the database and HTTP: nothing is sent, SYNTHETIC values only."""

import argparse
import contextlib
import json

import pytest
from scrape_harness import clispec, contracts, pipeline
from scrape_harness.cli import EXIT_NETWORK, main, parser

from listing_lens import api_token, db_check, export_db, site

MARKER = "synthetic-row-value-never-printed"
PASSWORD = "synthetic-password-never-printed"
JWT = "eyJ.synthetic.operator"
ANON = "eyJ.synthetic.anonymous"


def run(capsys, *argv):
    capsys.readouterr()
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# ── aliases: one definition each ─────────────────────────────────────────────────────────────────


def _sub(p: argparse.ArgumentParser, *path: str) -> argparse.ArgumentParser:
    for word in path:
        p = clispec._subparsers(p).choices[word]
    return p


@pytest.mark.parametrize(
    ("alias", "target"), [(("db", "export"), ("export-db",)), (("db", "token"), ("api-token",))]
)
def test_an_alias_is_the_same_parser_definition_and_function(alias, target):
    p = parser()
    a, t = _sub(p, *alias), _sub(p, *target)
    assert a.get_default("fn") is t.get_default("fn")
    opts = lambda q: sorted((x.option_strings, x.default, x.nargs, x.help) for x in q._actions)  # noqa: E731
    assert opts(a) == opts(t)


@pytest.mark.parametrize(("alias", "target"), [("db export", "export-db"), ("db token", "api-token")])
def test_cli_spec_rows_of_an_alias_equal_its_targets(alias, target):
    rs = clispec.rows(parser())

    def args(c):
        return [
            {k: v for k, v in r.items() if k != "cmd"} for r in rs if r["cmd"] == c and r["kind"] != "command"
        ]

    assert args(alias) == args(target) and args(alias)
    row = next(r for r in rs if r["cmd"] == alias and r["kind"] == "command")
    assert row["help"].startswith(f"alias of `{target}`")


@pytest.mark.parametrize(("alias", "target"), [("db export", "export-db"), ("db token", "api-token")])
def test_an_alias_is_declared_exactly_like_its_target(alias, target):
    """pipeline.v1 has no alias form, so the alias carries its own entry (the guard, preflight and the route
    table read names): it must equal its target's in every field but the name."""
    pl = pipeline.current()
    a, t = pl.command(alias), pl.command(target)
    for f in ("network", "operator_only", "inputs", "outputs", "secrets_env"):
        assert getattr(a, f) == getattr(t, f), f


@pytest.mark.parametrize(
    ("name", "env"), [("db check", "LLENS_DB_URL"), ("db api-check", "LLENS_API_PASSWORD")]
)
def test_the_new_commands_are_paid_utilities_with_their_secret(name, env):
    c = pipeline.current().command(name)
    assert c.network == pipeline.current().command("export-db").network == "paid"
    assert c.secrets_env == (env,)


def test_db_export_refuses_without_approve_network(data_dir, capsys, monkeypatch):
    """The alias reaches export-db's own gate: same refusal, same exit code."""
    monkeypatch.setattr(export_db, "build", lambda now: ({}, {}))
    with pytest.raises(SystemExit) as e:
        main(["db", "export"])
    assert e.value.code == EXIT_NETWORK


# ── db check ─────────────────────────────────────────────────────────────────────────────────────


def board(*targets, variables=()):
    return {
        "uid": "test-board",
        "templating": {"list": list(variables)},
        "panels": [
            {"id": i + 1, "title": f"p{i}", "targets": [{"refId": "A", "rawSql": sql}]}
            for i, sql in enumerate(targets)
        ],
    }


QVAR = {
    "type": "query",
    "name": "who",
    "query": "SELECT who FROM t",
    "includeAll": False,
    "multi": False,
    "current": {},
}


class FakeDb:
    """SQL -> rows by substring: 'boom' fails (its message carries the marker), 'none' returns no rows."""

    def __init__(self):
        self.sql = []

    def _q(self, sql):
        self.sql.append(sql)
        if "boom" in sql:
            raise db_check.QueryFailed("UndefinedTable", "42P01")
        return [] if "none" in sql else [(MARKER,), (MARKER,)]

    def count(self, sql):
        return len(self._q(sql))

    def first(self, sql):
        rows = self._q(sql)
        return rows[0] if rows else None

    def explain(self, sql):
        self._q("EXPLAIN " + sql)


def test_check_counts_rows_empties_and_errors_and_explains_the_other_states():
    db = FakeDb()
    r = db_check.check(db, [board("SELECT a FROM t", "SELECT none", "SELECT boom")])
    assert (r["targets"], r["executed"], r["explained"]) == (3, 2, 4)
    assert (r["empty"], r["errors"], r["ok"]) == (1, 1, False)
    by = {t["panel"]: t for t in r["results"]}
    assert by[1]["rows"] == 2 and by[1]["errors"] == []
    assert by[2]["rows"] == 0
    assert by[3]["rows"] is None and by[3]["errors"][0] == {
        "state": "all",
        "kind": "UndefinedTable",
        "sqlstate": "42P01",
    }
    contracts.validate("db_check.v1", r)


def test_a_query_variable_resolves_live_and_its_value_feeds_the_panels_in_memory_only():
    db = FakeDb()
    r = db_check.check(db, [board("SELECT x FROM t WHERE who = ${who:sqlstring}", variables=[QVAR])])
    assert r["variables"] == {"resolved": 1, "empty": 0, "errors": 0}
    assert any(f"'{MARKER}'" in s for s in db.sql if not s.startswith("EXPLAIN"))  # the live value was used
    assert MARKER not in json.dumps(r)


def test_the_time_filter_macro_becomes_true():
    db = FakeDb()
    db_check.check(db, [board("SELECT 1 FROM t WHERE $__timeFilter(ts)")])
    assert all("$__timeFilter" not in s for s in db.sql) and "WHERE TRUE" in db.sql[0]


def test_an_empty_result_is_reported_but_does_not_fail():
    r = db_check.check(FakeDb(), [board("SELECT none")])
    assert r["empty"] == 1 and r["ok"] is True


class _Conn:
    """A psycopg-shaped connection whose every statement fails with the marker in its message."""

    def transaction(self):
        return contextlib.nullcontext()

    def execute(self, sql):
        import psycopg

        raise psycopg.errors.UndefinedTable(f'relation "{MARKER}" does not exist')


def test_a_database_error_reports_its_class_and_sqlstate_never_its_message():
    pytest.importorskip("psycopg")
    with pytest.raises(db_check.QueryFailed) as e:
        db_check.PgDb(_Conn()).count("SELECT 1")
    assert (e.value.kind, e.value.sqlstate) == ("UndefinedTable", "42P01")
    assert MARKER not in str(e.value)


@pytest.fixture
def db_env(monkeypatch):
    monkeypatch.setenv("LLENS_DB_URL", "postgresql://synthetic.invalid/db")
    monkeypatch.delenv("LLENS_NETWORK_APPROVED", raising=False)
    fake = FakeDb()
    monkeypatch.setattr(db_check, "connect", lambda dsn, timeout_s: contextlib.nullcontext(fake))
    monkeypatch.setattr(db_check, "boards", lambda: [board("SELECT a", "SELECT boom", variables=[QVAR])])
    return fake


def test_db_check_refuses_without_approve_network_and_connects_to_nothing(db_env, capsys):
    with pytest.raises(SystemExit) as e:
        run(capsys, "db", "check")
    assert e.value.code == EXIT_NETWORK and db_env.sql == []


def test_db_check_without_the_dsn_is_the_export_db_exit(db_env, capsys, monkeypatch):
    monkeypatch.delenv("LLENS_DB_URL")
    code, _, err = run(capsys, "db", "check", "--approve-network")
    assert code == export_db.EXIT_DB and "LLENS_DB_URL" in err and db_env.sql == []


def test_db_check_dry_run_counts_targets_offline(db_env, capsys):
    code, out, _ = run(capsys, "db", "check", "--dry-run", "--json")
    d = json.loads(out)
    assert code == 0 and d["targets"] == 2 and d["executed"] == 0 and db_env.sql == []
    contracts.validate("db_check.v1", d)


@pytest.mark.parametrize("fmt", [["--json"], []], ids=["json", "table"])
def test_db_check_prints_counts_only_and_exits_1_on_an_error(db_env, capsys, fmt):
    code, out, err = run(capsys, "db", "check", "--approve-network", *fmt)
    assert code == 1
    assert MARKER not in out + err and "synthetic.invalid" not in out + err
    if fmt:
        d = json.loads(out)
        assert d["errors"] == 1 and d["variables"]["resolved"] == 1
        contracts.validate("db_check.v1", d)
    else:
        assert "42P01" in out and "test-board #2 [A]" in out


def test_db_check_on_a_real_postgres_isolates_each_target_and_refuses_writes(pg):
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(pg, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS dbc_t; CREATE TABLE dbc_t(who text); INSERT INTO dbc_t VALUES ('w1')")
    b = board(
        "SELECT * FROM dbc_missing",  # fails: the next target must still run
        "SELECT who FROM dbc_t WHERE who = ${who:sqlstring}",
        "INSERT INTO dbc_t VALUES ('w2') RETURNING who",  # a write: refused by the read-only transaction
        variables=[{**QVAR, "query": "SELECT who FROM dbc_t"}],
    )
    with db_check.connect(pg, 10) as db:
        r = db_check.check(db, [b])
    by = {t["panel"]: t for t in r["results"]}
    assert by[1]["errors"][0]["sqlstate"] == "42P01"
    assert by[2]["rows"] == 1 and by[2]["errors"] == []
    assert by[3]["errors"][0]["sqlstate"] == "25006"  # read_only_sql_transaction
    with psycopg.connect(pg, autocommit=True) as c:
        assert c.execute("SELECT count(*) FROM dbc_t").fetchone()[0] == 1


# ── db api-check ─────────────────────────────────────────────────────────────────────────────────

AUTH, REST = "https://auth.example.invalid/db/auth", "https://rest.example.invalid/db/rest/v1"


class FakeApi:
    """The Data API as export-db@26 configures it: schema api only, gated on the operator's JWT."""

    def __init__(self, **over):
        self.over, self.calls = over, []

    def __call__(self, method, url, headers=None, body=None):
        h = headers or {}
        self.calls.append((method, url, h))
        if url == AUTH + "/token/anonymous":
            return 200, {}, json.dumps({"token": ANON}).encode()
        path = url[len(REST) + 1 :]
        auth = h.get("Authorization", "")
        for key, status in self.over.items():
            if key in path or key == h.get("Accept-Profile"):
                return status, {}, b"{}"
        if h.get("Accept-Profile") == "li":
            return 406, {}, json.dumps({"code": "PGRST106", "message": MARKER}).encode()
        if path.startswith("rpc/") or path.startswith("derived"):
            return 404, {}, json.dumps({"code": "PGRST205", "message": MARKER}).encode()
        if not auth:
            return 401, {}, json.dumps({"code": "PGRST302"}).encode()
        if auth == f"Bearer {ANON}":
            return 403, {}, json.dumps({"code": "42501", "message": MARKER}).encode()
        return 206, {"content-range": "0-0/42"}, json.dumps([{"ad_id": MARKER}]).encode()


class FakeAuth:
    def __call__(self, method, url, body=None):
        if url.endswith("/sign-in/email"):
            return 200, {}
        return 200, {"set-auth-jwt": JWT}


@pytest.fixture
def api(monkeypatch):
    cfg = {"auth_url": AUTH, "email": "operator@example.invalid", "rest_url": REST}
    monkeypatch.setattr(site, "get", lambda table, key: cfg[key] if table == "api" else pytest.fail(table))
    monkeypatch.setenv("LLENS_API_PASSWORD", PASSWORD)
    monkeypatch.setattr(api_token, "transport", FakeAuth)
    fake = FakeApi()
    monkeypatch.setattr(db_check, "request", fake)
    return fake


def test_api_check_passes_when_every_rule_holds_and_prints_codes_only(api, capsys):
    code, out, err = run(capsys, "db", "api-check", "--approve-network", "--json")
    d = json.loads(out)
    contracts.validate("api_check.v1", d)
    assert code == 0 and d["ok"] and d["signed_in"]
    by = {p["name"]: p for p in d["probes"]}
    assert by["operator_api"]["status"] == 206 and by["operator_api"]["rows"] == 42
    assert by["anonymous_api"]["status"] == 403 and by["no_token_api"]["status"] == 401
    assert {by[n]["status"] for n in ("operator_li", "operator_withheld", "operator_public_rpc")} <= {
        404,
        406,
    }
    for secret in (MARKER, PASSWORD, JWT, ANON, AUTH, REST, "operator@example.invalid"):
        assert secret not in out + err, secret


def test_api_check_table_never_prints_a_secret(api, capsys):
    code, out, err = run(capsys, "db", "api-check", "--approve-network")
    assert code == 0 and "operator_api" in out
    for secret in (MARKER, PASSWORD, JWT, ANON, AUTH, REST):
        assert secret not in out + err


@pytest.mark.parametrize(
    ("over", "probe"),
    [
        ({"listings": 200}, "anonymous_api"),  # every token reads: the gate is gone
        ({"li": 200}, "operator_li"),  # schema li reachable
        ({"rpc/": 200}, "operator_public_rpc"),
    ],
)
def test_a_broken_rule_is_exit_1_naming_the_probe(api, capsys, over, probe):
    api.over = over
    code, out, _ = run(capsys, "db", "api-check", "--approve-network", "--json")
    d = json.loads(out)
    assert code == 1 and not d["ok"] and probe in {p["name"] for p in d["probes"] if not p["ok"]}


def test_an_empty_operator_read_fails_the_gate_proof(api, capsys, monkeypatch):
    """export-db@26 answers a 2xx with NO rows to a subject outside its allowlist: a 2xx alone proves nothing."""
    orig = api.__call__

    def empty(method, url, headers=None, body=None):
        s, h, b = orig(method, url, headers, body)
        return (s, {"content-range": "*/0"}, b"[]") if s == 206 else (s, h, b)

    monkeypatch.setattr(db_check, "request", empty)
    code, out, _ = run(capsys, "db", "api-check", "--approve-network", "--json")
    op = next(p for p in json.loads(out)["probes"] if p["name"] == "operator_api")
    assert code == 1 and op["rows"] == 0 and not op["ok"]


def test_no_anonymous_token_is_its_own_failed_probe(api, capsys, monkeypatch):
    orig = api.__call__
    monkeypatch.setattr(
        db_check,
        "request",
        lambda m, u, headers=None, body=None: (
            (404, {}, b"") if u.endswith("/token/anonymous") else orig(m, u, headers, body)
        ),
    )
    code, out, _ = run(capsys, "db", "api-check", "--approve-network", "--json")
    anon = next(p for p in json.loads(out)["probes"] if p["name"] == "anonymous_api")
    assert code == 1 and anon["status"] is None and anon["skipped"] == "no_anonymous_token"


def test_api_check_refuses_without_approve_network(api, capsys):
    with pytest.raises(SystemExit) as e:
        run(capsys, "db", "api-check")
    assert e.value.code == EXIT_NETWORK and api.calls == []


def test_api_check_without_the_password_sends_nothing(api, capsys, monkeypatch):
    monkeypatch.delenv("LLENS_API_PASSWORD")
    code, _, err = run(capsys, "db", "api-check", "--approve-network")
    assert code == api_token.EXIT_CONFIG and "LLENS_API_PASSWORD" in err and api.calls == []


def test_a_refused_sign_in_is_the_api_token_exit(api, capsys, monkeypatch):
    monkeypatch.setattr(api_token, "transport", lambda: lambda m, u, b=None: (401, {}))
    code, out, err = run(capsys, "db", "api-check", "--approve-network")
    assert code == api_token.EXIT_AUTH and "HTTP 401" in err and api.calls == []


def test_db_check_dry_run_builds_the_shipped_boards(capsys):
    """The real board build (synthetic site file): every target is listed, nothing is sent."""
    code, out, _ = run(capsys, "db", "check", "--dry-run", "--json")
    d = json.loads(out)
    contracts.validate("db_check.v1", d)
    assert code == 0 and d["boards"] > 1 and d["targets"] > d["boards"] and d["executed"] == 0


# ── one rule for Grafana's time macros (model.strip_time_macros; db check and the export test share it) ──


@pytest.mark.parametrize(
    ("sql", "want"),
    [
        ("WHERE $__timeFilter(ts)", "WHERE TRUE"),
        ("WHERE $__timeFilter( l.at ) AND x", "WHERE TRUE AND x"),
        ("WHERE $__timeFilter(coalesce(a, b)) AND y", "WHERE TRUE AND y"),  # nested parentheses
        ("WHERE $__timeFilter(ts) OR $__timeFilter(u)", "WHERE TRUE OR TRUE"),
        (
            "WHERE t >= $__timeFrom() AND t < $__timeTo()",
            "WHERE t >= '-infinity'::timestamptz AND t < 'infinity'::timestamptz",
        ),
        ("SELECT '$__all'", "SELECT '$__all'"),  # not a time macro: untouched
    ],
)
def test_strip_time_macros_spans_all_time(sql, want):
    from listing_lens.grafana import model

    assert model.strip_time_macros(sql) == want


def test_db_check_uses_the_one_time_macro_rule(monkeypatch):
    from listing_lens.grafana import model

    seen: list = []
    real = model.strip_time_macros
    monkeypatch.setattr(model, "strip_time_macros", lambda s: seen.append(s) or real(s))
    db = FakeDb()
    db_check.check(db, [board("SELECT 1 FROM t WHERE $__timeFilter(coalesce(a, b))")])
    assert seen and all("$__timeFilter" not in s for s in db.sql) and "WHERE TRUE" in db.sql[0]
    assert not hasattr(db_check, "TIME_FILTER")  # no second copy of the rule
