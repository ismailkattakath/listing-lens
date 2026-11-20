"""The `llens grafana` group: `llens-grafana` folded into the one CLI. One parser definition serves both
spellings, so every old `llens-grafana <sub> --help` equals `llens grafana <sub> --help` but for the prog
name, and both reach the same function. The functions that call Grafana are faked: nothing is sent, and
every value here is SYNTHETIC."""

import argparse

import pytest
from scrape_harness import clispec, pipeline
from scrape_harness.cli import EXIT_NETWORK, main, parser

from listing_lens.grafana import apply, tf
from listing_lens.grafana import cli as gcli

SUBS = ("check", "build", "tf", "plan", "apply", "perms", "ds-password", "bootstrap")
NETWORKED = ("tf", "plan", "apply", "perms", "ds-password", "bootstrap")
OFFLINE = ("check", "build")
ENV = {
    "LLENS_GRAFANA_URL": "https://grafana.example.invalid",
    "GRAFANA_TOKEN": "synthetic-token-never-printed",
    "LLENS_PG_DSN": "postgresql://u:synthetic-pw@db.example.invalid/x",
}


@pytest.fixture(autouse=True)
def wide(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")  # argparse wraps usage by width: pin it for both spellings


def _help(capsys, fn, argv) -> str:
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        fn(argv)
    assert e.value.code == 0
    return capsys.readouterr().out.replace("llens grafana", "PROG").replace("llens-grafana", "PROG")


# ── one definition, two spellings ────────────────────────────────────────────────────────────────


# `plan` and `apply` exist only in the new spelling: the old `llens-grafana apply` was a no-op word (kept so)
@pytest.mark.parametrize("sub", [s for s in SUBS if s not in ("plan", "apply")])
def test_old_help_equals_new_help_but_the_prog(capsys, sub):
    old = _help(capsys, gcli.legacy_main, [sub, "--help"])
    new = _help(capsys, main, ["grafana", sub, "--help"])
    assert old == new and f"usage: PROG {sub}" in new


def _sub(p: argparse.ArgumentParser, *path: str) -> argparse.ArgumentParser:
    for word in path:
        p = clispec._subparsers(p).choices[word]
    return p


@pytest.mark.parametrize("sub", SUBS)
def test_both_spellings_build_the_same_parser_and_function(sub):
    old, new = _sub(gcli.legacy_parser(), sub), _sub(parser(), "grafana", sub)
    assert old.get_default("fn") is new.get_default("fn")
    opts = lambda q: sorted((x.option_strings, x.default, x.nargs, x.help, x.dest) for x in q._actions)  # noqa: E731
    assert opts(old) == opts(new)
    assert old.get_default("gated") is False and new.get_default("gated") is True


@pytest.mark.parametrize(("alias", "action"), [("plan", "plan"), ("apply", "apply")])
def test_plan_and_apply_are_tf_plan_and_tf_apply(alias, action):
    p = parser()
    a = p.parse_args(["grafana", alias, "--approve-network"])
    t = p.parse_args(["grafana", "tf", action, "--approve-network"])
    assert a.fn is t.fn and a.tf_action == t.tf_action == action


# ── dispatch reaches the same function ───────────────────────────────────────────────────────────


@pytest.fixture
def calls(monkeypatch):
    seen: list = []
    monkeypatch.setattr(apply, "folder", lambda g: seen.append(("folder", g.url)))
    monkeypatch.setattr(apply, "datasource_grants", lambda g: seen.append(("grants", g.url)))
    monkeypatch.setattr(apply, "ds_password", lambda g, dsn: seen.append(("ds_password", g.url, dsn)))
    monkeypatch.setattr(apply, "bootstrap", lambda g, item: seen.append(("bootstrap", g.url, item)))
    monkeypatch.setattr(tf, "main", lambda argv: seen.append(("tf", list(argv))) or 0)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LLENS_GRAFANA_TOKEN_ITEM", "synthetic:item")
    return seen


URL, DSN = ENV["LLENS_GRAFANA_URL"], ENV["LLENS_PG_DSN"]
DISPATCH = [
    (["perms"], ["perms"], [("folder", URL), ("grants", URL)]),
    (["ds-password"], ["ds-password"], [("ds_password", URL, DSN)]),
    (["bootstrap"], ["bootstrap"], [("bootstrap", URL, "synthetic:item")]),
    (["tf", "plan"], ["tf", "plan"], [("tf", ["plan"])]),
    (["tf", "apply"], ["tf", "apply"], [("tf", ["apply"])]),
    (["plan"], ["tf", "plan"], [("tf", ["plan"])]),
    (["apply"], ["tf", "apply"], [("tf", ["apply"])]),
]


@pytest.mark.parametrize(("new", "old", "want"), DISPATCH)
def test_both_spellings_reach_the_same_function_with_the_same_arguments(calls, capsys, new, old, want):
    assert main(["grafana", *new, "--approve-network"]) == 0
    got_new = list(calls)
    calls.clear()
    assert gcli.legacy_main(old) == 0
    assert got_new == calls == want
    assert "synthetic-token-never-printed" not in "".join(capsys.readouterr())


@pytest.mark.parametrize("old", [["--check"], ["apply", "--check"], ["check"]])
def test_check_is_offline_and_the_old_flag_still_works(old, capsys, monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    assert gcli.legacy_main(old) == 0
    assert capsys.readouterr().out.endswith("boards clean\n")
    assert main(["grafana", "check"]) == 0
    assert capsys.readouterr().out.endswith("boards clean\n")


def test_build_prints_the_same_json_both_ways(capsys):
    assert gcli.legacy_main(["build"]) == 0
    old = capsys.readouterr().out
    assert main(["grafana", "build"]) == 0
    assert capsys.readouterr().out == old and old.startswith("{")


@pytest.mark.parametrize("old", [["apply"], ["plan"], ["apply", "tf", "plan"], []])
def test_old_non_words_stay_a_usage_error_never_a_tofu_run(calls, capsys, old):
    """`llens-grafana apply` was a no-op word in front of the real subcommand, and `tf` counted only first:
    none of these may become a tofu plan or apply."""
    assert gcli.legacy_main(old) == 2
    assert calls == [] and "usage: llens-grafana" in capsys.readouterr().err


def test_old_words_anywhere_keep_their_old_precedence(calls):
    """The old parser looked for words anywhere: --check, then bootstrap, ds-password, perms."""
    assert gcli.legacy_main(["apply", "perms"]) == 0
    assert calls == [("folder", URL), ("grants", URL)]


# ── the gate ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("sub", [["perms"], ["ds-password"], ["bootstrap"], ["tf", "plan"], ["apply"]])
def test_the_new_spelling_refuses_without_approve_network(calls, sub, monkeypatch):
    monkeypatch.delenv("LLENS_NETWORK_APPROVED", raising=False)
    with pytest.raises(SystemExit) as e:
        main(["grafana", *sub])
    assert e.value.code == EXIT_NETWORK and calls == []


def test_the_old_spelling_is_gated_as_before_and_names_the_new_one(calls, capsys):
    """No new flag for `llens-grafana` (the flake apps call it): its gate is `secret exec`, as today."""
    assert gcli.legacy_main(["perms"]) == 0
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "deprecated" in err and "llens grafana perms" in err


def test_a_missing_env_is_still_refused_with_exit_2(monkeypatch, capsys):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    assert main(["grafana", "perms", "--approve-network"]) == 2
    assert "LLENS_GRAFANA_URL and GRAFANA_TOKEN are required" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("cmd", "want"),
    [
        ("llens grafana perms", "block"),
        ("llens grafana tf plan", "block"),
        ("llens grafana apply", "block"),
        ("llens grafana perms --approve-network", "ask"),
        ("llens grafana bootstrap --approve-network", "ask"),
        ("llens grafana check", "allow"),
        ("llens grafana build", "allow"),
    ],
)
def test_the_guard_reads_the_declared_classes(cmd, want):
    from test_switch_goldens import verdict

    assert verdict(cmd) == want


# ── pipeline.toml and cli spec ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("sub", SUBS)
def test_every_subcommand_is_declared_with_its_network_class(sub):
    c = pipeline.current().command(f"grafana {sub}")
    assert c is not None
    assert c.network == ("paid" if sub in NETWORKED else "none")
    if sub in NETWORKED:
        assert "GRAFANA_TOKEN" in c.secrets_env
    else:
        assert c.secrets_env == ()


@pytest.mark.parametrize("alias", ["plan", "apply"])
def test_plan_and_apply_are_declared_exactly_like_tf(alias):
    pl = pipeline.current()
    a, t = pl.command(f"grafana {alias}"), pl.command("grafana tf")
    for f in ("network", "operator_only", "inputs", "outputs", "secrets_env"):
        assert getattr(a, f) == getattr(t, f), f


def test_secrets_are_declared_by_what_each_subcommand_reads():
    pl = pipeline.current()
    assert pl.command("grafana perms").secrets_env == ("GRAFANA_TOKEN",)
    assert pl.command("grafana bootstrap").secrets_env == ("GRAFANA_TOKEN",)
    assert pl.command("grafana ds-password").secrets_env == ("GRAFANA_TOKEN", "LLENS_PG_DSN")
    assert pl.command("grafana tf").secrets_env == ("GRAFANA_TOKEN", "LLENS_PG_DSN", "LLENS_TOFU_PASSPHRASE")
    assert pl.command("grafana bootstrap").operator_only


def test_cli_spec_lists_the_group_and_every_subcommand():
    rs = clispec.rows(parser())
    cmds = {r["cmd"]: r for r in rs if r["kind"] == "command"}
    assert "grafana" in cmds
    for sub in SUBS:
        assert cmds[f"grafana {sub}"]["declared"] is not None, sub
    nets = {r["cmd"] for r in rs if r["kind"] == "option" and r["opt"] == "--approve-network"}
    assert {f"grafana {s}" for s in NETWORKED} <= nets
    assert not {f"grafana {s}" for s in OFFLINE} & nets
