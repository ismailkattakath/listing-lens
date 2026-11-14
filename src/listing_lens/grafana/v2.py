"""The v1 board model → Grafana's v2 dashboard spec (dashboard.grafana.app/v2), so the stack can carry tabs.

One generator: boards.py / model.py build the v1-shaped dict, and model.check, the lint and both ratchets
run on THAT; this module only re-serialises it for the grafana_apps_dashboard_dashboard_v2 resource. It
reproduces what Grafana's own server-side conversion returns for our v1 boards (measured against all 7,
tests/test_grafana_v2.py), and it RAISES on any shape it does not know: no silent fallback.

Sections: a row with `tab` (model.row(..., tab=...)) starts a tab; rows under a tab become that tab's
RowsLayout. With no tab anywhere, the body is a plain RowsLayout, exactly as the server converts today.
"""

from __future__ import annotations

from . import model

DS = {"name": model.DS_UID}  # the v2 datasource ref carries the UID in `name`
PG = "grafana-postgresql-datasource"
_SORT = {0: "disabled", 1: "alphabeticalAsc", 2: "alphabeticalDesc", 3: "numericalAsc", 4: "numericalDesc"}
_REFRESH = {0: "never", 1: "onDashboardLoad", 2: "onTimeRangeChanged"}
_HIDE = {0: "dontHide", 1: "hideLabel", 2: "hideVariable"}
_TOOLTIP = {0: "Off", 1: "Crosshair", 2: "Tooltip"}


class Unsupported(ValueError):
    pass


def _ds_ok(ds: dict | None) -> None:
    if ds and (ds.get("uid") != DS["name"] or ds.get("type") != PG):
        raise Unsupported(f"datasource {ds!r}")


def _query(t: dict) -> dict:
    _ds_ok(t.get("datasource"))
    spec = {k: t[k] for k in ("editorMode", "format", "rawQuery", "rawSql") if k in t}
    extra = set(t) - {"refId", "datasource", "editorMode", "format", "rawQuery", "rawSql"}
    if extra:
        raise Unsupported(f"target keys {sorted(extra)}")
    return {
        "kind": "PanelQuery",
        "spec": {
            "query": {"kind": "DataQuery", "group": PG, "version": "v0", "datasource": DS, "spec": spec},
            "refId": t["refId"],
            "hidden": False,
        },
    }


_PANEL_KEYS = {
    "type", "title", "description", "datasource", "targets", "gridPos", "options", "fieldConfig", "id",
    "links", "transformations", "repeat", "repeatDirection", "maxPerRow",
}  # fmt: skip


def _element(p: dict) -> dict:
    extra = set(p) - _PANEL_KEYS
    if extra:
        raise Unsupported(f"panel {p.get('title')!r} keys {sorted(extra)}")
    _ds_ok(p.get("datasource"))
    fc = p.get("fieldConfig", {})
    if set(fc) - {"defaults", "overrides"}:
        raise Unsupported(f"panel {p.get('title')!r} fieldConfig keys {sorted(fc)}")
    spec = {"id": p["id"], "title": p.get("title", "")}
    # always present: the server's GET omits an empty one, but the provider's imported state carries "",
    # so emitting it keeps the first plan at whitespace-only (measured 2026-11-13)
    spec["description"] = p.get("description", "")
    spec["links"] = p.get("links", [])
    spec["data"] = {
        "kind": "QueryGroup",
        "spec": {
            "queries": [_query(t) for t in p.get("targets", [])],
            "transformations": [
                {
                    "kind": "Transformation",
                    "group": t["id"],
                    "spec": {k: v for k, v in t.items() if k != "id"},
                }
                for t in p.get("transformations", [])
            ],
            "queryOptions": {},
        },
    }
    spec["vizConfig"] = {
        "kind": "VizConfig",
        "group": p["type"],
        "version": "",
        "spec": {
            "options": p.get("options", {}),
            "fieldConfig": {
                "defaults": p.get("fieldConfig", {}).get("defaults", {}),
                # the server drops a threshold step's `value: null` inside an override, not in defaults
                "overrides": _no_null_steps(p.get("fieldConfig", {}).get("overrides", [])),
            },
        },
    }
    return {"kind": "Panel", "spec": spec}


def _no_null_steps(o):
    """Drop a threshold step's `value: null` (the base step), as the server does inside overrides."""
    if isinstance(o, dict):
        out = {k: _no_null_steps(v) for k, v in o.items()}
        if "steps" in out and isinstance(out["steps"], list):
            out["steps"] = [
                {k: v for k, v in st.items() if not (k == "value" and v is None)} for st in out["steps"]
            ]
        return out
    if isinstance(o, list):
        return [_no_null_steps(x) for x in o]
    return o


def _item(p: dict, y0: int) -> dict:
    g = p["gridPos"]
    spec = {"x": g["x"], "y": g["y"] - y0, "width": g["w"], "height": g["h"]}
    spec["element"] = {"kind": "ElementReference", "name": f"panel-{p['id']}"}
    if p.get("repeat"):
        spec["repeat"] = {"mode": "variable", "value": p["repeat"]}
        if p.get("repeatDirection"):
            spec["repeat"]["direction"] = p["repeatDirection"]
        if p.get("maxPerRow"):
            spec["repeat"]["maxPerRow"] = p["maxPerRow"]
    return {"kind": "GridLayoutItem", "spec": spec}


_ROW_KEYS = {"type", "title", "collapsed", "gridPos", "panels", "id", "tab"}


def _row(title: str, collapsed: bool, panels: list[dict], y0: int, header: bool) -> dict:
    spec = {"title": title, "collapse": collapsed}
    if not header:
        spec["hideHeader"] = True
    spec["layout"] = {"kind": "GridLayout", "spec": {"items": [_item(p, y0) for p in panels]}}
    return {"kind": "RowsLayoutRow", "spec": spec}


def _sections(panels: list[dict]) -> list[tuple[str | None, dict]]:
    """[(tab or None, RowsLayoutRow)] in order: top-level panels before the first row form a header-less row."""
    out, head = [], []
    i = 0
    while i < len(panels) and panels[i]["type"] != "row":
        head.append(panels[i])
        i += 1
    if head:
        out.append((None, _row("", False, head, 0, header=False)))
    while i < len(panels):
        r = panels[i]
        if r["type"] != "row":
            raise Unsupported("a panel after a row that is not inside it")
        if set(r) - _ROW_KEYS:
            raise Unsupported(f"row {r.get('title')!r} keys {sorted(set(r) - _ROW_KEYS)}")
        y0 = r["gridPos"]["y"] + 1
        if r.get("collapsed"):
            kids, i = r.get("panels", []), i + 1
        else:
            kids, i = [], i + 1
            while i < len(panels) and panels[i]["type"] != "row":
                kids.append(panels[i])
                i += 1
        out.append((r.get("tab"), _row(r.get("title", ""), bool(r.get("collapsed")), kids, y0, header=True)))
    return out


def _layout(panels: list[dict]) -> dict:
    secs = _sections(panels)
    if all(t is None for t, _ in secs):
        return {"kind": "RowsLayout", "spec": {"rows": [r for _, r in secs]}}
    tabs: list[dict] = []
    for tab, r in secs:
        if tab is not None or not tabs:
            tabs.append(
                {
                    "kind": "TabsLayoutTab",
                    "spec": {"title": tab or "", "layout": {"kind": "RowsLayout", "spec": {"rows": []}}},
                }
            )
        tabs[-1]["spec"]["layout"]["spec"]["rows"].append(r)
    return {"kind": "TabsLayout", "spec": {"tabs": tabs}}


def _variable(v: dict) -> dict:
    common = {"name": v["name"]}
    kind = v["type"]
    if kind == "query":
        _ds_ok(v.get("datasource"))
        spec = {
            **common,
            "current": v.get("current") or {"text": "", "value": ""},
            "label": v.get("label", v["name"]),
            "hide": _HIDE[v.get("hide", 0)],
            "refresh": _REFRESH[v.get("refresh", 1)],
            "skipUrlSync": v.get("skipUrlSync", False),
            "query": {
                "kind": "DataQuery",
                "group": PG,
                "version": "v0",
                "datasource": DS,
                "spec": {"__legacyStringValue": v["query"]},
            },
            "regex": v.get("regex", ""),
            "sort": _SORT[v.get("sort", 0)],
            "definition": v.get("definition", ""),
            "options": [],
            "multi": v.get("multi", False),
            "includeAll": v.get("includeAll", False),
        }
        if v.get("allValue"):
            spec["allValue"] = v["allValue"]
        spec["allowCustomValue"] = True
        return {"kind": "QueryVariable", "spec": spec}
    if kind == "custom":
        spec = {
            **common,
            "query": v["query"],
            "current": v.get("current") or {"text": "", "value": ""},
            "options": [
                {"selected": o.get("selected", False), "text": o["text"], "value": o["value"]}
                for o in v.get("options", [])
            ],
            "multi": v.get("multi", False),
            "includeAll": v.get("includeAll", False),
        }
        if v.get("allValue"):
            spec["allValue"] = v["allValue"]
        spec |= {
            "label": v.get("label", v["name"]),
            "hide": _HIDE[v.get("hide", 0)],
            "skipUrlSync": v.get("skipUrlSync", False),
            "allowCustomValue": True,
        }
        return {"kind": "CustomVariable", "spec": spec}
    raise Unsupported(f"variable type {kind!r}")


_BUILTIN = {
    "kind": "AnnotationQuery",
    "spec": {
        "query": {
            "kind": "DataQuery",
            "group": "grafana",
            "version": "v0",
            "datasource": {"name": "-- Grafana --"},
            "spec": {},
        },
        "enable": True,
        "hide": True,
        "iconColor": "rgba(0, 211, 255, 1)",
        "name": "Annotations & Alerts",
        "builtIn": True,
        "legacyOptions": {"type": "dashboard"},
    },
}


def _annotation(a: dict) -> dict:
    _ds_ok(a.get("datasource"))
    return {
        "kind": "AnnotationQuery",
        "spec": {
            "query": {
                "kind": "DataQuery",
                "group": PG,
                "version": "v0",
                "datasource": DS,
                "spec": a["target"],
            },
            "enable": a.get("enable", True),
            "hide": a.get("hide", False),
            "iconColor": a.get("iconColor", "red"),
            "name": a["name"],
        },
    }


def _link(lk: dict) -> dict:
    return {
        "title": lk["title"],
        "type": lk.get("type", "link"),
        "icon": lk.get("icon", "external link"),
        "tooltip": lk.get("tooltip", ""),
        "url": lk.get("url", ""),
        "tags": lk.get("tags", []),
        "asDropdown": lk.get("asDropdown", False),
        "targetBlank": lk.get("targetBlank", False),
        "includeVars": lk.get("includeVars", False),
        "keepTime": lk.get("keepTime", False),
    }


def spec(b: dict) -> dict:
    """The v2 spec (the resource's spec.json) for one built v1 board."""
    elements = {}

    def walk(ps):
        for p in ps:
            if p["type"] == "row":
                walk(p.get("panels", []))
            else:
                elements[f"panel-{p['id']}"] = _element(p)

    walk(b["panels"])
    tp = b.get("timepicker", {})
    return {
        "annotations": [_BUILTIN, *(_annotation(a) for a in b.get("annotations", {}).get("list", []))],
        "cursorSync": _TOOLTIP[b.get("graphTooltip", 0)],
        "description": b.get("description", ""),
        "editable": b.get("editable", True),
        "elements": elements,
        "layout": _layout(b["panels"]),
        "links": [_link(lk) for lk in b.get("links", [])],
        "liveNow": b.get("liveNow", False),
        "preferences": {"layout": {"kind": "GridLayout", "spec": {"items": []}}},
        "preload": b.get("preload", False),
        "tags": b.get("tags", []),
        "timeSettings": {
            "timezone": b.get("timezone", "browser"),
            "from": b["time"]["from"],
            "to": b["time"]["to"],
            "autoRefresh": b.get("refresh", ""),
            "autoRefreshIntervals": tp.get("refresh_intervals", []),
            "hideTimepicker": tp.get("hidden", False),
            "fiscalYearStartMonth": 0,
        },
        "title": b["title"],
        "variables": [_variable(v) for v in b.get("templating", {}).get("list", [])],
    }
