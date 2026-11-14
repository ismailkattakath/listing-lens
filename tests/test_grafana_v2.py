"""The v2 emitter re-serialises the one board model for grafana_apps_dashboard_dashboard_v2, without loss."""

import copy

import pytest

from listing_lens.grafana import boards, model, tf, v2
from listing_lens.grafana.model import PFX


@pytest.fixture(scope="module")
def built():
    return boards.build_all()


def test_tabs_group_rows():
    # every content board is tabbed now: take one before the regroup (its plain rows)
    b = copy.deepcopy(
        next(x for x in boards._fold([f() for f in boards.BOARDS]) if x["uid"] == f"{PFX}-process")
    )
    rows = [p for p in b["panels"] if p["type"] == "row"]
    assert rows, "the fixture board needs a row"
    rows[0]["tab"] = "First"
    lay = v2.spec(tf._board_json(b, keep_tabs=True))["layout"]
    assert lay["kind"] == "TabsLayout"
    tabs = lay["spec"]["tabs"]
    assert tabs[-1]["spec"]["title"] == "First"
    n_rows = sum(len(t["spec"]["layout"]["spec"]["rows"]) for t in tabs)
    assert n_rows == len(v2._sections(b["panels"]))  # no row lost or duplicated


def test_the_tab_marker_is_v2_only():
    r = model.row("x", tab="T")
    b = {"panels": [r, {**model.markdown("m", "t"), "id": 1}], "uid": "u"}
    assert tf._board_json(b)["panels"][0].get("tab") is None
    assert tf._board_json(b, keep_tabs=True)["panels"][0]["tab"] == "T"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["templating"]["list"].append({"type": "textbox", "name": "z"}),
        lambda b: next(p for p in b["panels"] if p["type"] != "row").update(pluginVersion="1"),
        lambda b: next(p for p in b["panels"] if p["type"] != "row").update(
            datasource={"uid": "other", "type": "x"}
        ),
    ],
)
def test_an_unknown_shape_raises(built, mutate):
    b = copy.deepcopy(tf._board_json(built[0], keep_tabs=True))
    mutate(b)
    with pytest.raises(v2.Unsupported):
        v2.spec(b)


def test_render_v2_imports_v2_and_forgets_v1(built):
    r = tf.render(built, "v2")
    names = {tf._tf_name(b["uid"]) for b in built}
    assert set(r["resource"][tf.V2_TYPE]) == names and tf.V1_TYPE not in r["resource"]
    assert {i["to"] for i in r["import"]} >= {f"{tf.V2_TYPE}.{n}" for n in names}
    assert {x["from"] for x in r["removed"]} == {f"{tf.V1_TYPE}.{n}" for n in names}
    assert all(x["lifecycle"] == {"destroy": False} for x in r["removed"])


def test_render_v1_is_the_exact_reverse(built):
    r = tf.render(built, "v1")
    names = {tf._tf_name(b["uid"]) for b in built}
    assert set(r["resource"][tf.V1_TYPE]) == names and tf.V2_TYPE not in r["resource"]
    assert {i["to"] for i in r["import"]} >= {f"{tf.V1_TYPE}.{n}" for n in names}
    assert {x["from"] for x in r["removed"]} == {f"{tf.V2_TYPE}.{n}" for n in names}


def test_board_files_follow_the_schema(built):
    b = built[0]
    assert "elements" in tf.board_file(b, "v2") and "panels" not in tf.board_file(b, "v2")
    assert "panels" in tf.board_file(b, "v1")


def test_a_tabbed_board_emits_its_tabs(built):
    for uid, spec in boards.TABS.items():
        b = next(x for x in built if x["uid"] == uid)
        lay = tf.board_file(b, "v2")["layout"]
        assert lay["kind"] == "TabsLayout"
        assert [t["spec"]["title"] for t in lay["spec"]["tabs"]] == [tab for tab, _ in spec]
        rows = [
            [r["spec"]["title"] for r in t["spec"]["layout"]["spec"]["rows"]] for t in lay["spec"]["tabs"]
        ]
        assert rows == [[title for title, _ in sections] for _, sections in spec]


def test_tabbing_loses_no_panel(built):
    raw = {b["uid"]: b for b in boards._merged(boards._fold([f() for f in boards.BOARDS]))}
    for b in built:
        n = sum(1 for p in model._walk_panels(b["panels"]) if p.get("type") != "row")
        assert n == sum(1 for p in model._walk_panels(raw[b["uid"]]["panels"]) if p.get("type") != "row"), b[
            "uid"
        ]
