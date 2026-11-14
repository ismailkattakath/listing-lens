"""Phase-3 switch proof: what listing-insights produces BEFORE it moves onto scrape-harness, frozen, and
re-checked AFTER. Recorded at the last pre-switch commit (hand-kept runcard.STAGES/IO, store.FILES and
.claude/hooks/guard.py); after the switch the same file computes the same things from pipeline.toml and
the harness plugin's guard, and must match byte for byte, except the INTENDED verdict changes listed in
INTENDED (each with its reason).

Recording is explicit: LLENS_RECORD_GOLDEN=1 writes tests/golden/switch.static.json and FAILS on purpose.

The proof's e2e half (the full offline pipeline on the site's own pages, digested) moved with those pages
and the site's source to DATA (the site source plug-in's tests, run by `ship` as a local check). The two
direct-HTTP hook commands name the tests' synthetic host since the site left the repo (agnostic #5): the
golden's keys were renamed with them, and their verdicts (block) are unchanged.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT
from scrape_harness import runcard, store

GOLDEN = ROOT / "tests" / "golden"
RECORD = "LLENS_RECORD_GOLDEN"
CTX = {
    "queue": "2026-01-15t12-00-00z-x-desktop-abc123",
    "site": "example.invalid",
    "ads": 3,
    "ab_at": "2026-01-15T12_00_00Z",
    "sample_file": "2026-01-15T12_00_00Z-x-desktop-abc123",
}
RID = "2026-01-15T12:00:00Z-x-desktop-abc123"
HOOK_CMDS = [
    "llens crawl-lists --subcategory x",
    "llens fetch-ads",
    "llens fetch-ads --dry-run",
    "llens crawl resume",
    "llens ab --subcategory x",
    "llens geocode",
    "llens geocode --geocoder fixture",
    "llens derive",
    "llens derive --provider fixture-geocoding",
    "llens reparse",
    "llens extract",
    "llens qa-page",
    "llens plan crawl-lists --subcategory x --seed 1",
    "llens browser open",
    "llens fetch-ads --approve-network",
    "LLENS_NETWORK_APPROVED=1 llens fetch-ads",
    "nix run .#default -- fetch-ads",
    "curl -s https://www.example.invalid/",
    "wget https://www.example.invalid/x",
    "echo $GEOCODE_KEY",
    "llens approve " + "a" * 64,
    "llens cooldown clear --site s --reason r",
    "llens cooldown status",
    "llens crawl-lists --driver fixture --subcategory x",
    "nix flake check",
]
# Verdicts the switch changes ON PURPOSE (the harness decision, not drift):
INTENDED = {
    # scrape-harness step 5: approvals are signed by the operator's Secure Enclave key (Touch ID via
    # Secretive); the fingerprint is the gate, so an agent may start `approve` (was: block, TTY-only).
    "llens approve " + "a" * 64: ("block", "allow"),
}


def check(name: str, got) -> None:
    path = GOLDEN / f"switch.{name}.json"
    text = json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get(RECORD) == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        pytest.fail(f"RECORDED {path}; review and commit it, then re-run without {RECORD}")
    assert path.exists(), f"no golden {path}; record it deliberately with {RECORD}=1"
    assert json.loads(text) == json.loads(path.read_text()), f"differs from {path}"


# ── static: run-card rows, stage order, file table, hook verdicts ────────────────────────────────


def stage_names() -> list[str]:
    return list(runcard.STAGES) if hasattr(runcard, "STAGES") else runcard.stages()


def commands() -> list[str]:
    if hasattr(runcard, "IO"):
        return sorted(runcard.IO)
    return sorted(json.loads((GOLDEN / "switch.static.json").read_text())["runcard_commands"])


def files() -> dict:
    table = store.FILES if hasattr(store, "FILES") else store.files()
    return {k: list(v) for k, v in table.items()}


def guard() -> Path:
    """The hook in force: the project's own before the switch, the harness plugin's after."""
    plugin = os.environ.get("SCRAPE_HARNESS_PLUGIN")
    g = Path(plugin) / "hooks" / "guard.py" if plugin else ROOT / ".claude" / "hooks" / "guard.py"
    assert g.is_file(), (
        f"no guard at {g} (after the switch, SCRAPE_HARNESS_PLUGIN must name the harness plugin)"
    )
    return g


def verdict(cmd: str) -> str:
    r = subprocess.run(
        [sys.executable, "-I", str(guard()), "bash"],
        input=json.dumps({"tool_input": {"command": cmd}}),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)},
        timeout=60,
    )
    return "block" if r.returncode == 2 else ("ask" if '"ask"' in r.stdout else "allow")


def static() -> dict:
    rows = {}
    cmds = commands()
    for cmd in cmds:
        for mode in ("run", "plan", "dry-run"):
            c = runcard.make(
                cmd,
                "golden",
                RID,
                ctx=CTX,
                mode=mode,
                roots={"DATA": "/DATA", "STATE": "/STATE"},
                plan_hash="a" * 64,
            )
            rows[f"{cmd}|{mode}"] = {k: c[k] for k in ("stage", "inputs", "outputs")}
    hook = {}
    for cmd in HOOK_CMDS:
        v = verdict(cmd)
        if cmd in INTENDED and not hasattr(
            runcard, "IO"
        ):  # after the switch: compare to the pre-switch verdict
            want_before, want_after = INTENDED[cmd]
            assert v == want_after, f"{cmd}: {v}, intended {want_after}"
            v = want_before
        hook[cmd] = v
    return {
        "runcard_commands": cmds,
        "stages": stage_names(),
        "runcard": rows,
        "files": files(),
        "hook": hook,
    }


def test_static_contract_is_unchanged_by_the_switch():
    """Everything CAPTURED must be unchanged; what listing-lens added since (e.g. the advertisers file
    kind) is allowed, since the switch itself was proven equal at c24c2ee. Removing or altering a captured
    row, file or verdict still fails."""
    got = static()
    want = json.loads((GOLDEN / "switch.static.json").read_text())
    if os.environ.get(RECORD) == "1":
        check("static", got)
    assert got["stages"] == want["stages"] and got["runcard_commands"] == want["runcard_commands"]
    assert got["runcard"] == want["runcard"] and got["hook"] == want["hook"]
    assert {k: got["files"].get(k) for k in want["files"]} == want["files"], "a captured file kind changed"
