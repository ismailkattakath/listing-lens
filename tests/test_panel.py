"""The H4 panel, offline (fixture driver, synthetic pages): revisits, gone ads, new-ad discovery from a
light list crawl, the kill switch, the incident gate, the standing-approval gate, the LaunchAgent plist."""

import dataclasses
import json
import plistlib
import shutil
import subprocess

import pytest
from conftest import FIXTURES, LIST
from scrape_harness import approvals, incidents, pipeline, store
from scrape_harness.cli import main
from scrape_harness.drivers.fixture import FixtureBackend

from listing_lens import panel

AD1 = f"{LIST}/exampleton_synthetic_example_ad-1000001"
GONE = f"{LIST}/exampleton_synthetic_gone_ad-1000077"
FAST = ["--max-tasks-per-minute", "6000", "--approve-politeness-override"]


def fixtures(tmp_path):
    d = tmp_path / "fx"
    shutil.copytree(FIXTURES, d, dirs_exist_ok=True)
    urls = json.loads((d / "urls.json").read_text()) | {GONE: {"file": "", "status": 404}}
    (d / "urls.json").write_text(json.dumps(urls))
    return d


def panel_file(tmp_path):
    p = {
        "schema": "panel.v1",
        "panel_id": "panel-20260115T120000Z-7",
        "created_at": "2026-01-15T12:00:00Z",
        "seed": 7,
        "site": "example.invalid",
        "region": "sample-region",
        "subcategories": ["sample-list-a"],
        "advertisers": [
            {
                "advertiser_id": "adv-0123456789ab",
                "ads": [{"ad_id": "1000001", "url": AD1}, {"ad_id": "1000077", "url": GONE}],
                "card_keys": ["n:sample"],  # the list card of 1000002 shows the name "Sample"
            }
        ],
    }
    f = tmp_path / "panel.json"
    f.write_text(json.dumps(p))
    return f


def runcmd(tmp_path, *extra):
    return main(
        [
            "panel",
            "run",
            "--panel",
            str(panel_file(tmp_path)),
            "--driver",
            "fixture",
            "--fixtures",
            str(fixtures(tmp_path)),
            *FAST,
            *extra,
        ]
    )


def test_a_run_revisits_finds_new_ads_and_records_gone(data_dir, tmp_path):
    assert runcmd(tmp_path) == 0
    got = {r["ad_id"]: r["fetch_reason"] for r in store.read("listings")}
    assert got["1000001"] == "panel" and got["1000002"] == "new"  # revisit + the advertiser's NEW ad
    assert "1000077" not in got and [e["url"] for e in store.read("events") if e["kind"] == "gone"] == [GONE]
    (run,) = [json.loads(p.read_text()) for p in (data_dir / "runs").glob("*.json")]
    assert run["command"] == "panel" and run["status"] == "completed"
    assert run["requests"]["list_pages"] == 1 and run["requests"]["ad_pages"] == 3


def test_the_kill_switch_and_the_incident_gate_send_nothing(data_dir, tmp_path, capsys):
    assert main(["panel", "pause", "--reason", "operator holiday"]) == 0
    assert runcmd(tmp_path) == 0 and not (data_dir / "runs").exists()
    assert main(["panel", "resume"]) == 0
    inc = incidents.open_incident(5, "STOPPED", "r", "panel")
    assert runcmd(tmp_path) == 11 and not (data_dir / "runs").exists()
    incidents.ack(inc["id"], "looked at it")
    assert runcmd(tmp_path) == 0


def test_a_dry_run_sends_nothing(data_dir, tmp_path, capsys):
    capsys.readouterr()
    assert runcmd(tmp_path, "--dry-run") == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["would_send"] == {"list": 1, "ad": 22} and not (data_dir / "runs").exists()


@pytest.fixture
def signer(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("keys")
    key = d / "k"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(key)], check=True)
    pub = " ".join(key.with_suffix(".pub").read_text().split()[:2])
    fp = subprocess.run(
        ["ssh-keygen", "-l", "-E", "sha256", "-f", str(key)], capture_output=True, text=True
    ).stdout.split()[1]
    (d / "allowed").write_text(f'{approvals.PRINCIPAL} namespaces="{approvals.NAMESPACE}" {pub}\n')
    monkeypatch.setenv("LLENS_APPROVAL_SIGNERS", str(d / "allowed"))
    real = pipeline.consumer()
    monkeypatch.setattr(pipeline, "consumer", lambda: dataclasses.replace(real, approval_key_fingerprint=fp))

    class KeyFile:
        def sign(self, body, pubkey):
            msg = d / "m"
            msg.write_bytes(body)
            subprocess.run(
                ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", approvals.NAMESPACE, str(msg)],
                check=True,
                capture_output=True,
            )
            return msg.with_suffix(".sig").read_text()

    monkeypatch.setattr(approvals, "SIGNER", KeyFile)


def tmp_path_factory_dir(tmp_path):
    d = tmp_path.parent / (tmp_path.name + "-keychain")  # outside DATA/STATE: the fake Keychain
    d.mkdir(exist_ok=True)
    return d


def test_a_networked_run_needs_a_standing_approval(data_dir, tmp_path, monkeypatch, signer, capsys):
    import sys

    monkeypatch.setattr(FixtureBackend, "network", True)
    # pipeline.toml names a proxy: its credentials come through a FAKE secret CLI (no Keychain here)
    fake = tmp_path_factory_dir(tmp_path) / "secret"
    fake.write_text(
        f"#!{sys.executable}\nimport os, sys\nargs = sys.argv[2:]; i = args.index('--')\n"
        "env = dict(os.environ, **{a.split('=', 1)[0]: 'fake-proxy-value-q7' for a in args[:i]})\n"
        "os.execvpe(args[i + 1], args[i + 1:], env)\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("LLENS_SECRET_CLI", str(fake))
    monkeypatch.setenv("LLENS_EGRESS_TRACE_URL", (FIXTURES / "trace-ca.txt").as_uri())
    assert runcmd(tmp_path) == 10 and not (data_dir / "runs").exists()  # refused before any request
    assert [i["kind"] for i in incidents.open_ones()] == ["approval"]
    for i in incidents.open_ones():
        incidents.ack(i["id"], "expected: no standing approval yet")
    from datetime import UTC, datetime, timedelta

    h = __import__("hashlib").sha256(panel_file(tmp_path).read_bytes()).hexdigest()
    approvals.approve_standing(
        {
            "panel_hash": h,
            "site": "example.invalid",
            "max_runs": 2,
            "max_ad_requests_per_run": 22,
            "max_list_pages_per_run": 1,
            "min_interval_s": 3600,
            "not_before": "2026-01-01T00:00:00Z",
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "egress_country": "CA",
            "variants": ["desktop"],
            "max_rate_per_minute": 60,
            **panel._proxy(),  # the configured proxy URL is part of what is signed
        }  # fmt: skip
    )
    assert runcmd(tmp_path, "--max-tasks-per-minute", "60") == 0  # within the signed rate
    (run,) = [json.loads(p.read_text()) for p in (data_dir / "runs").glob("*.json")]
    assert run["approval_ref"]["standing"] and run["approval_ref"]["run"] == 1
    for f in [
        *store.data_dir().rglob("*"),
        *store.state_dir().rglob("*"),
    ]:  # credentials stay in the connection
        assert not f.is_file() or b"fake-proxy-value-q7" not in f.read_bytes(), f


def test_install_renders_the_agent_without_loading_it(data_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLENS_APPROVAL_SIGNERS", str(tmp_path / "allowed"))
    capsys.readouterr()
    assert (
        main(
            [
                "panel",
                "install",
                "--panel",
                str(panel_file(tmp_path)),
                "--plist-dir",
                str(tmp_path / "la"),
                "--weekday",
                "2",
                "--hour",
                "9",
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["bootstrapped"] is False
    doc = plistlib.loads((tmp_path / "la" / "llens-panel.plist").read_bytes())
    assert doc["Label"] == "llens-panel" and "exec -a nix-llens-panel" in doc["ProgramArguments"][2]
    assert doc["StartCalendarInterval"] == {"Weekday": 2, "Hour": 9, "Minute": 0}
    assert {"LLENS_DATA", "LLENS_APPROVAL_SIGNERS"} <= set(doc["EnvironmentVariables"])
    assert main(["panel", "uninstall", "--plist-dir", str(tmp_path / "la")]) == 0
    assert not (tmp_path / "la" / "llens-panel.plist").exists()
