"""The OpenTofu stack: every object imported, no secret in config, and the Drive-state guards bite."""

import fcntl
import json

import pytest

from listing_lens.grafana import boards, model, tf
from listing_lens.grafana.model import PFX


@pytest.fixture(scope="module")
def stack():
    return tf.render(boards.build_all())


def test_every_object_has_an_import_block(stack):
    to = {i["to"] for i in stack["import"]}
    assert f"grafana_folder.{tf.FOLDER_RES}" in to and f"grafana_data_source.{tf.DS_RES}" in to
    shipped = set(model.BOARD_UIDS) - set(boards.MERGE_INTO)  # a merged-away board is not deployed
    assert {f"{tf.V2_TYPE}.{tf._tf_name(u)}" for u in shipped} <= to
    assert len(to) == len(stack["import"]) == 2 + len(shipped)


def test_no_secret_and_no_place_in_the_config(stack):
    text = json.dumps(stack)
    ds = stack["resource"]["grafana_data_source"][tf.DS_RES]
    assert "password" not in text and "secure_json_data_encoded" not in ds  # never set, so never in state
    assert ds["lifecycle"] == {"ignore_changes": ["secure_json_data_encoded"], "prevent_destroy": True}
    assert stack["terraform"]["backend"] == {"local": {}}  # the state path is given at init, never in git
    # host, user and database arrive as TF_VAR_* from the DSN at run time
    assert ds["url"] == "${var.ds_url}" and ds["username"] == "${var.ds_user}"
    assert ".neon.tech" not in text and "LLENS_DATA" not in text


def test_conflict_copy_is_detected(tmp_path):
    (tmp_path / tf.STATE_NAME).write_text("{}")
    (tmp_path / (tf.STATE_NAME + ".backup")).write_text("{}")
    assert tf.conflict_siblings(tmp_path) == []
    (tmp_path / "grafana (1).tfstate").write_text("{}")  # Drive's conflict copy
    assert tf.conflict_siblings(tmp_path) == ["grafana (1).tfstate"]


def test_plan_refuses_next_to_a_conflict_copy(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    (data / "tofu").mkdir(parents=True)
    (data / "tofu" / "grafana (1).tfstate").write_text("{}")
    monkeypatch.setenv("LLENS_DATA", str(data))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert tf.main(["plan"]) == 4
    assert "sync conflict copy" in capsys.readouterr().err


def test_a_second_run_is_refused_while_the_lock_is_held(tmp_path, monkeypatch):
    monkeypatch.setenv("LLENS_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    lock_dir = tmp_path / "state" / "listing-insights" / "tofu-grafana"
    lock_dir.mkdir(parents=True)
    with open(lock_dir / "run.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert tf.main(["plan"]) == 3


def test_apply_refuses_when_the_state_moved_since_the_plan(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "tofu").mkdir(parents=True)
    (data / "tofu" / tf.STATE_NAME).write_text('{"serial": 2}')
    local = tmp_path / "state" / "listing-insights" / "tofu-grafana"
    (local / "work").mkdir(parents=True)
    (local / "plan.tfplan").write_text("x")
    (local / "plan.state-sha256").write_text("sha-of-serial-1")
    monkeypatch.setenv("LLENS_DATA", str(data))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for k, v in {
        "LLENS_GRAFANA_URL": "https://g.example",
        "GRAFANA_TOKEN": "t",
        "LLENS_TOFU_PASSPHRASE": "p",
        "LLENS_PG_DSN": "postgresql://u:x@h.example/db?sslmode=require",
    }.items():
        monkeypatch.setenv(k, v)
    assert tf.main(["apply"]) == 5


def test_a_capped_query_at_its_cap_is_refused():
    """The Explorer returned exactly its cap (1,000 of 1,288 ads): the plan must refuse, not ship a cut."""
    built = boards.build_all()
    capped = [
        t for b in built for p in b["panels"] for t in p.get("targets", []) if tf._CAP.search(t["rawSql"])
    ]
    assert capped, "the Explorer table (and the shared-media list) carry a /* cap */ LIMIT"
    assert tf.cap_violations(built, lambda sql: 0) == []
    hits = tf.cap_violations(built, lambda sql: int(tf._CAP.search(sql)[1]))  # every query returns its cap
    assert len(hits) == len(capped) and all("rows are being cut" in h for h in hits)
    assert (
        tf.cap_violations(built, lambda sql: None)
        and "failed" in tf.cap_violations(built, lambda sql: None)[0]
    )


def test_expect_rows_refuses_an_empty_panel_and_never_reaches_grafana():
    """A panel can render EMPTY with no error (the 2026-11-13 viewpoint rings): plan refuses a marked
    target that returns 0 rows or fails, and the marker is stripped before deploy."""
    built = boards.build_all()
    marked = [(b["uid"], p) for b in built for p in model._walk_panels(b["panels"]) if p.get("expect_rows")]
    assert {u for u, _ in marked} >= {f"{PFX}-geo", f"{PFX}-advertiser"}
    assert tf.empty_violations(built, lambda sql: 3) == []
    zero = tf.empty_violations(built, lambda sql: 0)
    assert zero and all("returned 0 rows" in e for e in zero)
    assert any("failed" in e for e in tf.empty_violations(built, lambda sql: None))
    assert all('"expect_rows"' not in json.dumps(tf.board_file(b, "v2")) for b in built)


def test_live_defaults_set_the_saved_value_and_render_with_it():
    built = boards.build_all()
    tf.apply_live_defaults(built, lambda sql: "A Place")
    geo = next(b for b in built if b["uid"] == f"{PFX}-geo")
    vp = next(v for v in geo["templating"]["list"] if v["name"] == "viewpoint")
    assert vp["current"] == {"text": "A Place", "value": "A Place"}
    names = {v["name"]: v for v in geo["templating"]["list"]}
    assert "'A Place'" in model.render(boards.VP, names, "all")
    untouched = boards.build_all()
    tf.apply_live_defaults(untouched, lambda sql: None)  # no live value: nothing is invented
    vp0 = next(v for b in untouched for v in b["templating"]["list"] if v["name"] == "viewpoint")
    assert not (vp0.get("current") or {}).get("value")


def test_plan_warns_on_a_slow_marked_target():
    built = boards.build_all()
    clock = iter(range(0, 10_000, 5))  # every query "takes" 5 s
    real = tf.time.monotonic
    tf.time.monotonic = lambda: next(clock)
    try:
        slow: list[str] = []
        assert tf.empty_violations(built, lambda sql: 3, slow) == []
    finally:
        tf.time.monotonic = real
    assert slow and all(s.endswith("5.0 s") for s in slow)
