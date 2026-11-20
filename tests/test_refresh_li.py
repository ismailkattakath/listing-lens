"""`llens refresh` as listing-lens declares it (the site file's [pipeline.crawl.refresh]; synthetic here). The chain's mechanics (the
bounded approval, each bound, the paid-count gate, resume, the run-id lock) are scrape-harness's and tested
there (its tests/test_refresh.py); these tests pin li's own declaration and that nothing is sent unapproved."""

import json

import pytest
from scrape_harness import pipeline
from scrape_harness.cli import main

ORDER = [
    "crawl-lists",
    "crawl-lists",
    "fetch-ads",
    "reparse",
    "geocode",
    "derive",
    "derive-advertisers",
    "places-resolve",
    "services",
    "export-db",
]


def steps():
    return pipeline.current().refresh


def test_the_refresh_chain_is_the_operators_order():
    assert [s.name for s in steps()] == ORDER
    subcats = [s.args[s.args.index("--subcategory") + 1] for s in steps() if s.name == "crawl-lists"]
    assert subcats == [
        "sample-list-a",
        "sample-list-b",
    ]  # both lists (the site file declares them; synthetic here)
    adv = next(s for s in steps() if s.name == "derive-advertisers")
    assert adv.args[adv.args.index("--method") + 1] == "v1.1,v2"  # v2 before export-db (its D1 guard)


def test_geocode_is_count_gated_and_export_db_is_exempt_with_a_reason():
    by = {s.name: s for s in steps()}
    assert by["geocode"].count_field == "pending"
    assert (by["export-db"].paid_exempt or "").strip()


def test_refresh_without_an_approval_prints_the_plan_and_sends_nothing(data_dir, capsys, monkeypatch):
    # planned under `secret exec`, as the operator does: refresh checks its steps' secrets_env first (exit 2)
    monkeypatch.setenv("GEOCODE_KEY", "synthetic-key")
    monkeypatch.setenv("LLENS_DB_URL", "synthetic-dsn")
    assert main(["refresh"]) == 10
    out = capsys.readouterr()
    text = out.out + out.err
    assert "plan_hash" in text or "approve" in text
    assert "sample-list-a" in text and "sample-list-b" in text  # both lists are in the signed bounds


def test_refresh_dry_run_sends_and_saves_nothing(data_dir, capsys):
    assert main(["refresh", "--dry-run"]) == 0
    assert not (data_dir / "runs").exists() or not any((data_dir / "runs").iterdir())


@pytest.mark.parametrize("bad", ["v3", "v1.1,v9", ""])
def test_derive_advertisers_refuses_an_unknown_method(data_dir, bad):
    with pytest.raises(SystemExit):
        main(["derive-advertisers", "--method", bad])


def test_derive_advertisers_runs_several_methods_in_order(data_dir, capsys, monkeypatch):
    from listing_lens import advertisers, commands

    seen = []
    monkeypatch.setattr(commands.store, "read", lambda kind: iter([]))
    monkeypatch.setattr(commands.store, "replace", lambda kind, rows: len(list(rows)))
    monkeypatch.setattr(advertisers, "clusters_v2", lambda latest: seen.append("v2") or [])
    monkeypatch.setattr(advertisers, "links_v2", lambda latest, rows: [])
    real = commands.cmd_derive_advertisers

    def spy(args):
        if "," not in args.method:
            seen.append(f"call:{args.method}")
            if args.method == "v1.1":
                return 0  # v1.1's own output is tested elsewhere; here only the order matters
        return real(args)

    monkeypatch.setattr(commands, "cmd_derive_advertisers", spy)
    assert main(["derive-advertisers", "--method", "v1.1,v2"]) == 0
    assert seen == ["call:v1.1", "call:v2", "v2"]
    json.loads(capsys.readouterr().out.strip().splitlines()[-1])  # v2's summary line
