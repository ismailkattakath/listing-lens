from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# listing-lens' own pipeline.toml (its [plugins] declare the source, extractors, geocoders, commands
# and schemas), explicitly: a test never inherits some other pipeline from the environment.
os.environ["SCRAPE_HARNESS_PIPELINE"] = str(ROOT / "pipeline.toml")
FIXTURES = ROOT / "tests" / "fixtures"
# A SYNTHETIC site (invented places, a placeholder highway system): the real site's config is DATA's, never the
# repo's, and a test never reads it. Set before any test module imports the extractor (it loads at import).
os.environ["LLENS_SITE_CONFIG"] = str(ROOT / "tests" / "site" / "site.toml")
# The live stack's names (Grafana uids/titles, the tofu key name) are bound outside the repo; the tests always
# see the generic defaults, whatever the shell exports.
for _k in [k for k in os.environ if k.startswith(("LLENS_GRAFANA_", "LLENS_TOFU_KEY_NAME"))]:
    del os.environ[_k]
EXAMPLES = ROOT / "schemas" / "examples"
# the SYNTHETIC source (tests/site/plugins/sample_source): its origin and its one list
ORIGIN = "https://www.example.invalid"
LIST = f"{ORIGIN}/listings/sample-list-a/sample-region"


@pytest.fixture(autouse=True)
def pipeline_tests_run_gta_hints_at6(monkeypatch):
    """The LIVE default is gta-hints@8.3 (commands.DEFAULT_EXTRACTOR, pinned by
    test_the_live_extractor_default_is_at83). It needs DATA's gazetteer snapshot, which the synthetic
    pipeline fixtures do not carry. So pipeline-MECHANICS tests run on @6, whose behaviour they and the
    switch goldens were written against; the extractors are tested on their own in test_gta_hints*.py."""
    from listing_lens import commands

    monkeypatch.setattr(commands, "DEFAULT_EXTRACTOR", "gta-hints@6")


@pytest.fixture(autouse=True)
def no_clipboard(monkeypatch):
    """The darwin build sandbox is off and /usr/bin/pbcopy exists: never touch the real clipboard."""
    monkeypatch.setenv("LLENS_NO_CLIPBOARD", "1")
    monkeypatch.setenv("LLENS_NO_NOTIFY", "1")
    monkeypatch.setenv("LLENS_NO_CAFFEINATE", "1")  # harness d03c24f+: no real sleep assertion in tests


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any attempt to open an internet socket fails the test (stdio subprocesses are unaffected)."""

    def refuse(*_a, **_k):
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("LLENS_DATA", str(d))
    monkeypatch.setenv("LLENS_STATE", str(tmp_path / "state"))  # never the real ~/.local/state
    monkeypatch.delenv("LLENS_NETWORK_APPROVED", raising=False)
    # the operator's REAL signer pin (.envrc.local): with it, approval lookups read as a working pin, and a
    # test's outcome depended on whether the shell sourced it (ship's sandbox never does)
    monkeypatch.delenv("LLENS_APPROVAL_SIGNERS", raising=False)
    return d


@pytest.fixture(scope="module")
def pg():
    """A THROWAWAY Postgres (initdb + pg_ctl on a unix socket, no TCP), shared by export-db and the board tests.
    The nix build sets LLENS_REQUIRE_PG=1 so a missing server binary FAILS there instead of skipping."""
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if not (initdb and pg_ctl):
        if os.environ.get("LLENS_REQUIRE_PG") == "1":
            pytest.fail("initdb/pg_ctl not on PATH but LLENS_REQUIRE_PG=1")
        pytest.skip("no Postgres server binaries on PATH")
    base = Path(tempfile.mkdtemp(prefix="lipg", dir="/tmp"))  # short: a unix socket path is <= 104 bytes
    data = base / "d"
    subprocess.run(
        [initdb, "-D", str(data), "-A", "trust", "-U", "postgres", "--no-locale", "-E", "UTF8"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            pg_ctl,
            "-D",
            str(data),
            "-o",
            f"-k {base} -c listen_addresses= -p 5432",
            "-w",
            "-l",
            str(base / "log"),
            "start",
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield f"postgresql:///postgres?host={base}&port=5432&user=postgres"
    finally:
        subprocess.run([pg_ctl, "-D", str(data), "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(base, ignore_errors=True)
