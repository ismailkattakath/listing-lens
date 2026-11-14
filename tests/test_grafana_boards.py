"""The private Grafana boards obey the hard rules, and the rule check itself bites."""

import copy
from pathlib import Path

import pytest

from listing_lens.grafana import boards, model
from listing_lens.grafana.model import PFX


def _last(b):
    """The last panel that carries a query (a board may end with a collapsed Notes row)."""
    return next(p for p in reversed(b["panels"]) if p.get("targets"))


@pytest.fixture(scope="module")
def built():
    return boards.build_all()


def test_every_board_is_clean(built):
    assert built
    assert [e for b in built for e in model.check(b)] == []


def test_uids_are_known_and_unique(built):
    uids = [b["uid"] for b in built]
    assert len(uids) == len(set(uids))
    assert set(uids) <= set(model.BOARD_UIDS)


def test_refresh_off_and_floor(built):
    for b in built:
        assert b["refresh"] == ""
        assert all(model._minutes(i) >= 10 for i in b["timepicker"]["refresh_intervals"])


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda b: b.update(refresh="5s"), "refresh must be OFF"),
        (lambda b: b["timepicker"].update(refresh_intervals=["5m"]), "under 10m"),
        (lambda b: _last(b)["targets"][0].update(rawSql="SELECT ${subcategory}"), "sqlstring"),
        (
            lambda b: _last(b)["targets"][0].update(rawSql="SELECT ${subcategory:raw}"),
            "non-whitelisted",
        ),
        (
            lambda b: _last(b)["targets"][0].update(rawSql="SELECT ${nope:sqlstring}"),
            "unknown variable",
        ),
        (lambda b: _last(b)["options"].update(content='<img src="x">'), "literal <img"),
        (lambda b: _last(b)["targets"][0]["datasource"].update(uid="grafanacloud-prom"), "not on"),
        (
            lambda b: (
                _last(b)
                .setdefault("fieldConfig", {})
                .setdefault("defaults", {})
                .update(color={"mode": "continuous-oranges"})
            ),
            "not a Grafana FieldColorModeId",
        ),
        (
            lambda b: (
                _last(b)
                .setdefault("fieldConfig", {})
                .setdefault("overrides", [])
                .append(
                    {"matcher": {}, "properties": [{"id": "color", "value": {"mode": "continuous-Blues"}}]}
                )
            ),
            "not a Grafana FieldColorModeId",
        ),
    ],
)
def test_check_bites(built, mutate, needle):
    b = copy.deepcopy(built[0])
    mutate(b)
    assert any(needle in e for e in model.check(b))


# ---- the All-variable outage (2026-11-08): a custom allValue is inserted VERBATIM by Grafana


def test_render_follows_grafana_custom_allvalue_rule():
    v = {"s": {"type": "query", "name": "s", "includeAll": True, "multi": True, "allValue": "'__all'"}}
    sql = "SELECT 1 WHERE ('__all' IN (${s:sqlstring}) OR c IN (${s:sqlstring}))"
    assert model.render(sql, v, "all") == "SELECT 1 WHERE ('__all' IN ('__all') OR c IN ('__all'))"
    assert model.render(sql, v, "multi").count("'x1','x2'") == 2
    assert model.render(sql, {"s": {**v["s"], "allValue": "__all"}}, "all").count("(__all)") == 2


def test_every_board_renders_clean_under_all_single_multi(built):
    for b in built:
        names = {v["name"]: v for v in b["templating"]["list"]}
        for p in b["panels"]:
            for t in p.get("targets", []):
                for state in ("all", "single", "multi"):
                    assert not model._BARE_SENTINEL.search(model.render(t["rawSql"], names, state)), (
                        b["uid"],
                        p["id"],
                    )


def test_bare_allvalue_turns_the_check_red(built):
    b = copy.deepcopy(next(x for x in built if x["uid"] == f"{PFX}-explorer"))
    for v in b["templating"]["list"]:
        if v.get("allValue"):
            v["allValue"] = "__all"  # the outage
    errs = model.check(b)
    assert any("not a quoted SQL literal" in e for e in errs)
    assert any("unquoted __all under all" in e for e in errs)


def test_persona_panels_carry_the_draft_stamp(built):
    """D2: a panel reading persona_id says "draft — unchecked" in its ⓘ until the B3 read passes 0.9."""
    assert not [e for b in built for e in model.check(b) if model.PERSONA_DRAFT in e]
    b = copy.deepcopy(built[0])
    p = _last(b)
    p["targets"][0]["rawSql"] = "SELECT count(DISTINCT persona_id) FROM advertisers_v2"
    p["description"] = "no stamp"
    assert any(model.PERSONA_DRAFT in e for e in model.check(b))
    p["description"] = boards.PERSONA_GATE
    assert not [e for e in model.check(b) if model.PERSONA_DRAFT in e]


def test_no_coordinate_literal_in_any_query(built):
    """Places are DATA (viewpoint, region tables): a coordinate literal in SQL turns the check red."""
    b = copy.deepcopy(next(x for x in built if x["uid"] == f"{PFX}-geo"))
    assert not [e for e in model.check(b) if "coordinate-like" in e]
    b["panels"][1]["targets"][0]["rawSql"] = "SELECT 45.0001 AS lat, -75.0001 AS lon"  # synthetic point
    assert any("coordinate-like" in e for e in model.check(b))


def test_rings_read_the_centre_only_from_the_viewpoint_table():
    from listing_lens.grafana import boards as bd

    for sql in [bd.ring_edge_sql(k) for k in range(1, bd.MAX_RINGS)] + [
        bd.RING_OUTER,
        *[bd.band_fill_sql(k) for k in range(1, bd.MAX_RINGS + 1)],
        bd.RING_LABELS,
        bd.VIEWPOINT_MARK,
        bd.RING_LABELS,
    ]:
        assert bd.VP in sql  # one viewpoint subquery, with the empty-variable fallback (2026-11-13)
    assert "coalesce(nullif(${viewpoint:sqlstring}, '')" in bd.VP


def test_fit_views_are_not_capped_at_world_zoom(built):
    b = copy.deepcopy(next(x for x in built if x["uid"] == f"{PFX}-geo"))
    assert not [e for e in model.check(b) if "max zoom" in e]
    next(p for p in b["panels"] if p["type"] == "geomap")["options"]["view"].pop(
        "zoom"
    )  # Grafana then stores 1
    assert any("max zoom" in e for e in model.check(b))


def test_field_regex_with_templated_display_name_is_refused(built):
    b = copy.deepcopy(built[0])
    stat = _last(b)
    stat.setdefault("options", {})["reduceOptions"] = {"values": True, "calcs": [], "fields": ""}
    stat.setdefault("fieldConfig", {"defaults": {}, "overrides": []})["defaults"]["displayName"] = (
        "${__data.fields.x}"
    )
    assert not [e for e in model.check(b) if "No data" in e]
    stat["options"]["reduceOptions"]["fields"] = "/^ads$/"  # the 2026-11-08 bug
    assert any("No data" in e for e in model.check(b))


def test_every_geomap_opens_on_the_viewpoint(built):
    maps = [(b["uid"], p) for b in built for p in model._walk_panels(b["panels"]) if p["type"] == "geomap"]
    assert len(maps) >= 6  # 8 until 2026-11-13: D4 deleted the content board P5 and C20 the pin-route map
    assert not [
        e for b in built for e in model.check(b) if "does not open on the viewpoint" in e or "measure" in e
    ]
    b = copy.deepcopy(next(x for x in built if x["uid"] == f"{PFX}-geo"))
    g = next(
        p
        for p in model._walk_panels(b["panels"])
        if p["type"] == "geomap" and p["title"].startswith("Pin atlas")
    )
    g["options"]["view"] = {"id": "fit", "allLayers": True, "zoom": 18}  # a wide view: no exceptions allowed
    assert any("does not open on the viewpoint" in e for e in model.check(b))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["options"].setdefault("layers", []).append({"type": "photos", "name": "thumbs"}),
        lambda p: p.setdefault("fieldConfig", {"defaults": {}, "overrides": []})["defaults"].update(
            custom={"cellOptions": {"type": "image"}}
        ),
        lambda p: p["options"].update(content="![x](https://imx.example/a.jpg)"),
        lambda p: p["options"].update(content="<img src=x>"),
    ],
)
def test_no_images_anywhere(built, mutate):
    assert not [e for b in built for e in model.check(b) if "no images" in e or "no hotlinks" in e]
    b = copy.deepcopy(built[0])
    mutate(next(p for p in b["panels"] if p["type"] != "row"))  # rows carry no options
    assert any("no images" in e or "no hotlinks" in e for e in model.check(b))


def test_rings_map_fits_the_20km_view_not_the_rings(built):
    g = next(x for x in built if x["uid"] == f"{PFX}-geo")
    rings = next(p for p in g["panels"] if p["type"] == "geomap" and p["title"].startswith("Viewpoint"))
    assert rings["options"]["view"]["layer"] == model.VIEW_LAYER
    b = copy.deepcopy(g)
    r2 = next(p for p in b["panels"] if p["type"] == "geomap" and p["title"].startswith("Viewpoint"))
    r2["options"]["view"]["layer"] = "Outer edge"  # the 2026-11-08 bug: fits step × rings
    assert any("does not open on the viewpoint" in e for e in model.check(b))


def test_one_kpi_strip_per_board(built):
    """The 'showing N of M' stat beside the Explorer table (2026-11-08) is the case this refuses."""
    assert not [e for b in built for e in model.check(b) if "KPI strip" in e]
    b = copy.deepcopy(next(x for x in built if x["uid"] == f"{PFX}-overview"))
    extra = copy.deepcopy(next(p for p in b["panels"] if p["type"] == "stat"))
    extra.update(id=999, title="Ads table: showing N of M")
    b["panels"].append(extra)
    assert any("at most ONE KPI strip" in e for e in model.check(b))


def test_explorer_counts_rows_in_the_table_footer(built):
    ex = next(x for x in built if x["uid"] == f"{PFX}-explorer")
    t = next(
        p for p in model._walk_panels(ex["panels"]) if p["type"] == "table" and p["title"].startswith("Ads")
    )
    assert t["fieldConfig"]["defaults"]["custom"]["footer"] == {"reducers": ["countAll"]}
    assert "nothing is cut" in t["description"]


def test_no_panel_hides_under_a_collapsed_row(built):
    """The 2026-11-08 bug: a collapsed Notes row at the top swallowed every panel up to the next row."""
    assert not [e for b in built for e in model.check(b) if "is hidden" in e]
    b = copy.deepcopy(next(x for x in built if x["uid"] == f"{PFX}-content"))
    notes = next(p for p in b["panels"] if p["type"] == "row" and p.get("collapsed"))
    b["panels"].remove(notes)
    loose = next(
        p for p in b["panels"] if p["type"] != "row"
    )  # a panel left at top level after a collapsed row
    b["panels"].remove(loose)
    b["panels"][0:0] = [notes, loose]
    assert any("follows collapsed row" in e for e in model.check(b))


def test_distances_are_postgis_not_hand_haversine(built):
    assert not [e for b in built for e in model.check(b) if "haversine" in e]
    b = copy.deepcopy(next(x for x in built if x["uid"] == f"{PFX}-geo"))
    _last(b)["targets"][0]["rawSql"] = "SELECT 6371 * 2 * asin(sqrt(0.1)) AS km"
    assert any("hand-written haversine" in e for e in model.check(b))


def test_manual_xychart_series_bind_a_frame(built):
    """T2 said "No data" over 657 rows: a manual xychart series without a frame matcher maps nothing."""
    assert not [e for b in built for e in model.check(b) if "frame matcher" in e]
    b = copy.deepcopy(next(x for x in built if x["uid"] == f"{PFX}-content"))
    xy = next(p for p in b["panels"] if p["type"] == "xychart")
    xy["options"]["series"][0].pop("frame")
    assert any("no frame matcher" in e for e in model.check(b))


# ---------------------------------------------------------------- out-of-region toggle (operator rule, 2026-11-08)


def _targets(b):
    return [(p, t) for p in model._walk_panels(b["panels"]) for t in p.get("targets", [])]


def _with_state(b, value):
    names = {v["name"]: copy.deepcopy(v) for v in b["templating"]["list"]}
    names[model.OOR_VAR]["current"] = {"text": value, "value": value}
    return names


@pytest.mark.parametrize("kind", ["pin", "text"])
def test_region_guard_bites(built, kind):
    """Perturbation: drop the flag from ONE target that reads that kind's columns, and check() refuses."""
    cols = model.PIN_COLS if kind == "pin" else model.TEXT_COLS
    flag = f"location_valid_{kind}"
    for b in built:
        for p, t in _targets(b):
            if flag in t["rawSql"] and cols.search(model._ALIAS.sub("", t["rawSql"])):
                bad = copy.deepcopy(b)
                tgt = next(
                    x
                    for q in model._walk_panels(bad["panels"])
                    if q.get("id") == p["id"]
                    for x in q["targets"]
                    if x["refId"] == t["refId"]
                )
                tgt["rawSql"] = tgt["rawSql"].replace(flag, "some_other_column")
                assert any(f"without {flag}" in e for e in model.check(bad))
                return
    pytest.fail(f"no target reads {kind} columns with its flag")


def test_alias_names_are_not_reads():
    assert (
        model.region_violations("SELECT x::float AS km_pin_city, x AS geocode_lat FROM (VALUES (1)) d(x)")
        == []
    )
    assert model.region_violations("SELECT l.pin_lat AS lat FROM listings_latest l") != []


def test_every_location_board_has_the_toggle_and_its_live_line(built):
    for b in built:
        reads = any(model.PIN_COLS.search(model._ALIAS.sub("", t["rawSql"])) for _, t in _targets(b))
        names = {v["name"] for v in b["templating"]["list"]}
        if not reads:
            continue
        assert model.OOR_VAR in names, b["uid"]
        in_strip = any(t["rawSql"] == boards.oor_strip_sql() for _, t in _targets(b))
        in_info = any("${oor_line}" in (p.get("description") or "") for p in model._walk_panels(b["panels"]))
        assert in_strip or (in_info and boards.OOR_LINE in names), b["uid"]
        assert [v for v in b["templating"]["list"] if v["name"] == model.OOR_VAR][0]["current"][
            "value"
        ] == "hide"


def test_toggle_changes_the_rendered_sql(built):
    geo = next(b for b in built if b["uid"] == f"{PFX}-geo")
    _, t = next((p, t) for p, t in _targets(geo) if model.pin_ok() in t["rawSql"])
    hide = model.render(t["rawSql"], _with_state(geo, "hide"), "all")
    show = model.render(t["rawSql"], _with_state(geo, "show"), "all")
    assert "'hide' = 'show'" in hide and "'show' = 'show'" in show


def test_far_pins_are_the_flag_not_a_second_threshold():
    for sql in (boards.FAR_SUB, boards.FAR_ADV, boards.V2_FAR):
        assert "location_valid_pin IS FALSE" in sql and "> 60" not in sql


def test_pinless_ad_survives_the_default_filter(pg, built):
    """team-lead's guard (2): under hide, an in-region and a PINLESS ad (flag NULL) pass and only the flagged
    one goes; under show all three pass. Postgres evaluates the rendered helper itself: NULL logic is the point
    (a bare `flag OR ...` is NULL for the pinless ad, and WHERE drops it)."""
    psycopg = pytest.importorskip("psycopg")
    geo = next(b for b in built if b["uid"] == f"{PFX}-geo")
    rows = "(VALUES ('in', true, true), ('pinless', NULL::boolean, NULL::boolean), ('flagged', false, false))"
    sql = (
        f"SELECT array_agg(l.ad_id ORDER BY l.ad_id) FROM {rows} l(ad_id, location_valid_pin, location_valid_text) "
        f"WHERE {model.pin_ok()} AND {model.text_ok()}"
    )
    with psycopg.connect(pg, autocommit=True) as c:
        got = {
            state: c.execute(model.render(sql, _with_state(geo, state), "all")).fetchone()[0]
            for state in ("hide", "show")
        }
        bare = c.execute(
            f"SELECT count(*) FROM {rows} l(ad_id, location_valid_pin, t) WHERE (l.location_valid_pin OR false)"
        ).fetchone()[0]
    assert got == {"hide": ["in", "pinless"], "show": ["flagged", "in", "pinless"]}
    assert bare == 1  # the brief's literal form would have dropped the pinless ad


def test_no_frame_carries_a_shared_key_value(built):
    """C21: the network graph counts link_key in a CTE and never selects it; the check bites on a select."""
    assert not [e for b in built for e in model.check(b) if "link_key" in e]
    b = copy.deepcopy(next(x for x in built if x["uid"] == f"{PFX}-advertiser"))
    p = next(
        x for x in model._walk_panels(b["panels"]) if x.get("title", "").startswith("Ad cluster network")
    )
    assert "link_key" in p["targets"][1]["rawSql"]  # counted inside the CTE
    p["targets"][1]["rawSql"] += "\nUNION ALL SELECT k.link_key AS id FROM advertiser_v2_links k"
    assert any("selects link_key" in e for e in model.check(b))


def test_every_viewpanel_link_lands_on_its_panel(built):
    """A text or ⓘ link /d/<uid>?viewPanel=panel-<n> must name a panel that exists on that board, and each
    candidate's link must land on its evidence panel (the regroup renumbers ids: 2026-11-13 bug)."""
    import re

    by = {b["uid"]: {p["id"]: p for p in model._walk_panels(b["panels"])} for b in built}
    link = re.compile(rf"/d/({PFX}-[a-z]+)\?viewPanel=panel-(\d+)")
    seen = 0
    for b in built:
        for p in model._walk_panels(b["panels"]):
            text = (p.get("options") or {}).get("content", "") + p.get("description", "")
            for uid, n in link.findall(text):
                seen += 1
                assert int(n) in by[uid], (b["uid"], p.get("title"), uid, n)
    assert seen >= len(boards.CANDIDATES)
    overview = next(b for b in built if b["uid"] == f"{PFX}-overview")
    text = next(
        p for p in model._walk_panels(overview["panels"]) if p.get("title", "").startswith("Candidate")
    )
    for hid, _claim, uid, prefix in boards.CANDIDATES:
        m = re.search(rf"\*\*{hid}\*\*.*?/d/{uid}\?viewPanel=panel-(\d+)", text["options"]["content"])
        assert m and by[uid][int(m[1])]["title"].startswith(prefix), hid


def test_a_persona_filtered_explorer_drops_coordinates(built):
    """F2b: under a persona selection the ads table blanks every coordinate/place column and its exact
    dates, so a sorted table cannot rebuild a route (C17, C20)."""
    ex = next(b for b in built if b["uid"] == f"{PFX}-explorer")
    t = next(
        p for p in model._walk_panels(ex["panels"]) if p.get("title", "").startswith("Ads — every field")
    )
    sql = t["targets"][0]["rawSql"]
    for col in ("pin_lat", "pin_lon", "geocode_formatted", "text_hint_raw", "age_days", "first_published"):
        expr = sql[: sql.index(f"AS {col}")]
        expr = expr[expr.rfind(",\n") :] if ",\n" in expr else expr
        assert boards.PERSONA_SELECTED in expr, col
    assert "place_lat" not in sql and "geocode_lat" not in sql  # never selected at all


def test_key_counts_strip_labels_are_unique_and_short():
    """21 values in one strip: duplicate raw column names ("pairs" x3) made counts unreadable as WHICH
    count, and long names overprinted their neighbours at 1280 px (2026-11-13)."""
    import re

    from listing_lens.grafana import boards, model

    b = next(b for b in boards.build_all() if b["uid"] == f"{PFX}-advertiser")
    strip = next(
        p
        for p in model._walk_panels(b["panels"])
        if p.get("type") == "stat" and p.get("title", "").startswith("Key counts")
    )
    names = [n for t_ in strip["targets"] for n in re.findall(r'AS\s+"([^"]+)"', t_["rawSql"])]
    assert len(names) >= 20, names
    assert len(names) == len(set(names)), names
    assert all(len(n) <= 20 for n in names), [n for n in names if len(n) > 20]


def test_no_title_is_cut_inside_a_bracket():
    """short_title split "…ad (text only — not fetched)" at the dash, deploying "…ad (text only" (2026-11-13)."""
    from listing_lens.grafana import boards, model

    title, note = model.short_title("Photo URLs of the selected ad (text only — not fetched)")
    assert (title, note) == ("Photo URLs of the selected ad", "text only — not fetched")
    for b in boards.build_all():
        for p in model._walk_panels(b["panels"]):
            t = p.get("title", "")
            assert t.count("(") == t.count(")"), (b["uid"], t)


def test_every_variable_changes_something():
    """A dropdown no panel reads looks like a broken filter (listy's check, 2026-11-13)."""
    import json

    from listing_lens.grafana import boards

    for b in boards.build_all():
        rest = json.dumps({k: v for k, v in b.items() if k != "templating"})
        vs = json.dumps(b["templating"]["list"])
        for v in b["templating"]["list"]:
            others = vs.replace(json.dumps(v), "")
            assert boards._var_refs(rest, v["name"]) or boards._var_refs(others, v["name"]), (
                b["uid"],
                v["name"],
            )


def test_the_sites_lists_and_concepts_come_from_the_site_config(built):
    """The board build reads the site's lists, service-mode concepts and local-time place from the site config
    (the synthetic site here); the code names none of them."""
    import json

    from listing_lens import site

    text = json.dumps(built)
    (a, sa), (b, sb) = site.get("boards", "lists")
    assert f"'{a}'" in text and f"'{b}'" in text and f'\\"{sa}\\"' in text
    assert site.get("boards", "local_time_place") in text
    src = (Path(boards.__file__)).read_text()
    assert not any(f"'{s}'" in src for s in (a, b))


def test_the_stack_uids_are_env_overridable(monkeypatch):
    import importlib

    monkeypatch.setenv("LLENS_GRAFANA_DS_UID", "ds-x")
    monkeypatch.setenv("LLENS_GRAFANA_FOLDER_UID", "folder-x")
    try:
        m = importlib.reload(model)
        assert (m.DS_UID, m.FOLDER_UID, m.DS["uid"]) == ("ds-x", "folder-x", "ds-x")
    finally:
        monkeypatch.delenv("LLENS_GRAFANA_DS_UID")
        monkeypatch.delenv("LLENS_GRAFANA_FOLDER_UID")
        importlib.reload(model)
    assert (model.PFX, model.DS_UID, model.FOLDER_UID) == ("llens", "llens-db", "llens-private")
