"""The harness's CLI surface on listing-lens (scrape-harness 49f9cf5): `preflight` names each command's secret
env vars by NAME only, and the committed renders (`cli spec`, `claude-md`) are repo-only: they build with no
site file, and no site file value enters them. SYNTHETIC values only; nothing is sent."""

import json
import os
import subprocess
import sys

import pytest
from conftest import ROOT
from scrape_harness import pipeline
from scrape_harness.cli import main

# each command that needs a secret env var, and the one it needs (confirmed in the code that reads it)
SECRETS = {"geocode": "GEOCODE_KEY", "export-db": "LLENS_DB_URL", "api-token": "LLENS_API_PASSWORD"}
SENTINEL = "synthetic-secret-value-never-printed"


@pytest.fixture
def no_secrets(monkeypatch, data_dir):
    for env in SECRETS.values():
        monkeypatch.delenv(env, raising=False)
    return data_dir


def preflight(capsys, *words):
    capsys.readouterr()
    code = main(["preflight", *words, "--json"])
    out, err = capsys.readouterr()
    return code, json.loads(out), out + err


def secret_rows(d):
    return {r["name"]: r for r in d["checks"] if r["kind"] == "secret_env"}


def test_pipeline_declares_each_secret_by_name():
    pl = pipeline.current()
    for name, env in SECRETS.items():
        assert pl.command(name).secrets_env == (env,), name
    # places-resolve needs GEMINI_API_KEY only with --gemini: secrets_env has no per-flag form, and a
    # declared one would make every refresh refuse (it is a refresh step)
    assert pl.command("places-resolve").secrets_env == ()


def test_preflight_refresh_names_the_refresh_steps_secrets_when_missing(no_secrets, capsys):
    code, d, _ = preflight(capsys, "refresh")
    rows = secret_rows(d)
    assert code == 2 and not d["ok"]
    assert rows["GEOCODE_KEY"]["status"] == "missing" and rows["GEOCODE_KEY"]["needed_by"] == ["geocode"]
    assert rows["LLENS_DB_URL"]["status"] == "missing" and rows["LLENS_DB_URL"]["needed_by"] == ["export-db"]
    assert {"GEOCODE_KEY", "LLENS_DB_URL"} <= set(d["missing"])
    assert "LLENS_API_PASSWORD" not in rows  # api-token is not a refresh step


def test_preflight_api_token_names_its_password(no_secrets, capsys):
    _, d, _ = preflight(capsys, "api-token")
    assert secret_rows(d)["LLENS_API_PASSWORD"]["status"] == "missing"


@pytest.mark.parametrize("words", [["refresh"], ["api-token"], ["geocode"], ["export-db"]])
def test_preflight_reports_presence_only_never_the_value(no_secrets, monkeypatch, capsys, words):
    for env in SECRETS.values():
        monkeypatch.setenv(env, SENTINEL)
    _, d, text = preflight(capsys, *words)
    rows = secret_rows(d)
    assert rows and all(r["status"] == "present" for r in rows.values())
    assert SENTINEL not in text
    _, _, table = (capsys.readouterr(), main(["preflight", *words]), capsys.readouterr())
    assert SENTINEL not in table.out + table.err


def test_refresh_refuses_before_planning_when_a_step_secret_is_unset(no_secrets, capsys):
    capsys.readouterr()
    assert main(["refresh"]) == 2
    err = capsys.readouterr().err
    assert "GEOCODE_KEY" in err and "LLENS_DB_URL" in err and "nothing was planned" in err
    assert not list((no_secrets.parent / "state").glob("plans/*"))


# ── the committed renders: repo-only ─────────────────────────────────────────────────────────────

# values that live ONLY in the synthetic site file (tests/site/site.toml), never in pipeline.toml
SITE_ONLY = ["Exampleton", "proxy-a.example.invalid", "sample-list-a", "sample_source"]


def render(tmp_path, args, site: bool) -> str:
    """The CLI in a fresh process (conftest's site env and imported modules prove nothing in-process)."""
    env = {k: v for k, v in os.environ.items() if k != "LLENS_SITE_CONFIG"}
    (tmp_path / "data").mkdir(exist_ok=True)
    env |= {
        "SCRAPE_HARNESS_PIPELINE": str(ROOT / "pipeline.toml"),
        "LLENS_DATA": str(tmp_path / "data"),  # empty: no DATA/config/site.toml either
        "LLENS_STATE": str(tmp_path / "state"),
    }
    if site:
        env["LLENS_SITE_CONFIG"] = str(ROOT / "tests" / "site" / "site.toml")
    r = subprocess.run(
        [sys.executable, "-m", "scrape_harness.cli", *args], env=env, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.mark.parametrize("args", [["cli", "spec"], ["claude-md"]], ids=["cli-spec", "claude-md"])
def test_the_committed_renders_need_no_site_file_and_carry_none_of_its_values(tmp_path, args):
    if args == ["claude-md"] and not os.environ.get("SCRAPE_HARNESS_PLUGIN"):
        pytest.skip("SCRAPE_HARNESS_PLUGIN unset (the dev shell and the build set it)")
    bare = render(tmp_path, args, site=False)
    assert bare.strip()
    assert render(tmp_path, args, site=True) == bare  # byte-identical with the site file present
    for v in SITE_ONLY:
        assert v not in bare, v


def test_the_route_block_routes_each_secret_command(tmp_path):
    if not os.environ.get("SCRAPE_HARNESS_PLUGIN"):
        pytest.skip("SCRAPE_HARNESS_PLUGIN unset (the dev shell and the build set it)")
    block = render(tmp_path, ["claude-md"], site=False)
    for cmd in ("`llens geocode`", "`llens export-db`", "`llens api-token`"):
        assert cmd in block, cmd
