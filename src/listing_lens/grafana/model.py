"""Grafana dashboard-as-code primitives for the PRIVATE exploration boards.

One generator, no hand-edited JSON: every board is built from these helpers, and `check()`
enforces the hard rules on the built JSON (the same function runs in pytest and before every
apply), so a rule cannot hold in review and drift in the stack.

Hard rules enforced by `check()`:
- refresh OFF, and the picker offers nothing under 10 minutes;
- every SQL target uses the one datasource uid, and template variables only as
  ${var:sqlstring} (text) or ${var:raw} (a whitelisted custom variable);
- no image-renderer panel and no `<img` literal in any template: no media is shown until a browser
  check proves an image can be fetched with no Referer (stock Photos/Table Image cells cannot set one);
- every query that reads an ad's pin columns carries location_valid_pin, and every one that reads its
  text-geocode columns carries location_valid_text (the out-of-region toggle, pin_ok / text_ok);
- every colour mode id is one of Grafana's FieldColorModeId values (an unknown one silently falls back to
  thresholds);
- no query reads a gold-answer column (gold_mask.MASKED) from listings_latest or derived, only from the
  listings_masked view (C19).
"""

from __future__ import annotations

import json
import os
import re

from .. import gold_mask

# The stack objects' names: GENERIC defaults, each env-overridable (another stack, another site). A live
# stack's own uids/titles are bound outside the repo (.envrc.local), so changing a default never renames
# a live object. The tofu resource names derive from these uids (tf._tf_name), so they follow too.
PFX = os.environ.get("LLENS_GRAFANA_UID_PREFIX", "llens")  # every board uid is <PFX>-<board>
DS_UID = os.environ.get("LLENS_GRAFANA_DS_UID", f"{PFX}-db")
DS = {"type": "grafana-postgresql-datasource", "uid": DS_UID}
FOLDER_UID = os.environ.get("LLENS_GRAFANA_FOLDER_UID", f"{PFX}-private")
TITLE_PREFIX = os.environ.get("LLENS_GRAFANA_TITLE_PREFIX", "Listing Lens")  # "<TITLE_PREFIX> - <board>"
FOLDER_TITLE = os.environ.get("LLENS_GRAFANA_FOLDER_TITLE", f"{TITLE_PREFIX} (private)")
TAG = os.environ.get("LLENS_GRAFANA_TAG", "listing-lens")  # every board's first tag
# every geomap fits this ONE layer: a 20 km circle around li.viewpoint (boards.view_sql)
VIEW_LAYER = "View 20 km"
REFRESH_INTERVALS = ["10m", "30m", "1h", "2h"]

# The All value of every multi-value variable. Grafana inserts a CUSTOM allValue VERBATIM — it
# skips the :sqlstring formatter for it (grafana v13.0.1 public/app/features/templating/
# template_srv.ts: "skip formatting of custom all values unless format set to text or
# percentencode"). So the sentinel must already be a quoted SQL literal; a bare __all renders as
# an identifier (ERROR: column "__all" does not exist — the 2026-11-08 outage).
ALL_TOKEN = "__all"
ALL = f"'{ALL_TOKEN}'"

BOARD_UIDS = {
    f"{PFX}-overview": "Overview",
    f"{PFX}-explorer": "Explorer",
    f"{PFX}-geo": "Geography",
    f"{PFX}-process": "Process",
    f"{PFX}-time": "Time",
    f"{PFX}-content": "Content",
    f"{PFX}-advertiser": "Advertisers",
}


# ---------------------------------------------------------------- variables


def query_var(name: str, sql: str, *, label: str | None = None, multi=True, include_all=True) -> dict:
    return {
        "type": "query",
        "name": name,
        "label": label or name,
        "datasource": DS,
        "query": sql,
        "definition": sql,
        "refresh": 1,  # on dashboard load
        "multi": multi,
        "includeAll": include_all,
        "current": {"text": "All", "value": "$__all"} if include_all else {},
        # All → the literal '__all' (not every value inlined), so in_filter() can short-circuit
        # and rows whose column is NULL are not silently dropped by IN (...)
        "allValue": ALL if include_all else None,
        "sort": 1,
    }


def custom_var(
    name: str, options: list[str], *, label: str | None = None, default: str | None = None
) -> dict:
    default = default if default is not None else options[0]
    return {
        "type": "custom",
        "name": name,
        "label": label or name,
        "query": ",".join(options),
        "multi": False,
        "includeAll": False,
        "current": {"text": default, "value": default},
        "options": [{"text": o, "value": o, "selected": o == default} for o in options],
    }


def text_var(name: str, *, label: str | None = None, default: str = "") -> dict:
    return {
        "type": "textbox",
        "name": name,
        "label": label or name,
        "query": default,
        "current": {"text": default, "value": default},
    }


def subcategory_var(name: str = "subcategory") -> dict:
    return query_var(
        name, "SELECT DISTINCT list_subcategory FROM observations ORDER BY 1", label="subcategory"
    )


def in_filter(col: str, var: str) -> str:
    """col IN the multi-value variable, with All matching everything (NULLs included)."""
    return f"({ALL} IN (${{{var}:sqlstring}}) OR ({col})::text IN (${{{var}:sqlstring}}))"


# ---------------------------------------------------------------- out-of-region toggle (operator rule, 2026-11-08)
# derive.region_validity flags a pin / text point > REGION_KM from its own label centre (export-db@15/16:
# location_valid_pin / location_valid_text on derived, listings_latest and import_phone_ads). Every location
# panel EXCLUDES flagged points unless the board variable says show. A flag is NULL only when its point is
# missing (measured 2026-11-08: every pinned row carries a non-NULL flag), so the test is IS NOT FALSE — a bare
# `flag OR …` is NULL for a pinless ad and the WHERE would silently drop it from every mixed panel.
OOR_VAR = "out_of_region"
_SHOW = f"'${{{OOR_VAR}:raw}}' = 'show'"


def out_of_region_var() -> dict:
    return custom_var(OOR_VAR, ["hide", "show"], label="out-of-region")


def _flag(kind: str, alias: str | None) -> str:
    return f"{alias + '.' if alias else ''}location_valid_{kind}"


def pin_ok(alias: str | None = "l") -> str:
    """THE pin helper: in-region (or no pin), or the board shows flagged points."""
    return f"({_flag('pin', alias)} IS NOT FALSE OR {_SHOW})"


def text_ok(alias: str | None = "l") -> str:
    """THE text helper: the same rule for the text-geocode point."""
    return f"({_flag('text', alias)} IS NOT FALSE OR {_SHOW})"


def pin_in(alias: str | None = "l") -> str:
    """A map's main pin layer: never a flagged pin (in show mode they get their own grey layer)."""
    return f"{_flag('pin', alias)} IS NOT FALSE"


def text_in(alias: str | None = "l") -> str:
    return f"{_flag('text', alias)} IS NOT FALSE"


def pin_flagged(alias: str | None = "l") -> str:
    """The grey layer: flagged pins, only in show mode (0 rows under hide)."""
    return f"({_flag('pin', alias)} IS FALSE AND {_SHOW})"


def text_flagged(alias: str | None = "l") -> str:
    return f"({_flag('text', alias)} IS FALSE AND {_SHOW})"


# columns that place an AD on the map or measure from its point (city_centre_* is the label centre, not the ad).
# km_pin_text needs both flags. A name after AS (an alias) is not a read and is stripped before matching.
PIN_COLS = re.compile(r"\b(?:pin_(?:lat|lon|geog|verdict|class)|km_pin_(?:city|text)|d_city|km_to_target)\b")
TEXT_COLS = re.compile(
    r"\b(?:geocode_(?:lat|lon|geog|formatted|precise|place_id|decision|location_type)|km_pin_text|km_text_city|d_text)\b"
)
_ALIAS = re.compile(r'\bAS\s+(?:"[^"]*"|\w+)', re.I)


def region_violations(sql: str) -> list[str]:
    """Which flag a query is missing: pin columns without location_valid_pin, text/geocode columns without
    location_valid_text."""
    reads = _ALIAS.sub("", sql)
    out = []
    if PIN_COLS.search(reads) and "location_valid_pin" not in sql:
        out.append("reads pin columns without location_valid_pin")
    if TEXT_COLS.search(reads) and "location_valid_text" not in sql:
        out.append("reads text/geocode columns without location_valid_text")
    return out


# ---------------------------------------------------------------- panels


def target(sql: str, ref: str = "A", fmt: str = "table") -> dict:
    return {
        "refId": ref,
        "datasource": DS,
        "editorMode": "code",
        "format": fmt,
        "rawQuery": True,
        "rawSql": sql,
    }


MAX_TITLE_WORDS = 8


def short_title(title: str) -> tuple[str, str]:
    """A panel title is <= 8 words; the rest moves to the panel description (Grafana's (i) tooltip).
    'Title — method note' splits at the dash when the head is a real title (>= 3 words); otherwise the
    title keeps its first 8 words and the description gets the full text."""
    words = title.split()
    if " — " in title:
        head, tail = title.split(" — ", 1)
        # a dash inside "( … )" is part of the title, not the method note ("(text only — not fetched)")
        balanced = head.count("(") == head.count(")")
        if balanced and len(head.split()) >= 3 and len(head.split()) <= MAX_TITLE_WORDS:
            return head.strip(), tail.strip()
    if len(words) <= MAX_TITLE_WORDS:
        return title, ""
    if title.endswith(")") and " (" in title:
        head, tail = title.rsplit(" (", 1)
        if 3 <= len(head.split()) <= MAX_TITLE_WORDS:
            return head.strip(), tail[:-1].strip()
    return title, ""  # still too long: the title guard flags it, and the source gets a better title


ANSWERS = "Answers: "  # the description line that shows a panel's GQM question ids to a reader


# ---- the gold mask (C19): every read of an answer-bearing column goes through the listings_masked view

MASKED_SRC = "listings_masked"
PERSONA_DRAFT = "draft — unchecked"
# tables the board role (board_ro) is NOT granted from export-db@21 (D8): gold answers and the geocode cache
NOT_GRANTED = frozenset({"derived", "listings_latest", "ads", "geocodes"})
_MASKED_COL = re.compile(r"\b(" + "|".join(sorted(gold_mask.MASKED)) + r")\b")
_RAW_SRC = re.compile(r"\b(listings_latest|derived)\b")
_WILDCARD = re.compile(r"\b(?:SELECT\s+\*|\w+\.\*)", re.I)


def mask_violations(sql: str) -> list[str]:
    """Why this query can read a gold answer: it names a masked column, or takes every column with *, from
    listings_latest or derived instead of listings_masked. [] = safe."""
    if not _RAW_SRC.search(sql):
        return []
    named = sorted(set(_MASKED_COL.findall(sql)))
    out = [f"reads {', '.join(named)} unmasked"] if named else []
    if _WILDCARD.search(sql):
        out.append("takes * from a raw source")
    return out


def answers(p: dict, *ids: str) -> dict:
    """Tag a panel with the GQM question ids it serves (DATA notes/li-grafana-gqm.md). `q` is source-only
    (tf strips it before deploy, so Grafana never sees an unknown key); the same ids are appended to the
    description so a reader sees them, and the GQM test checks that the two agree."""
    p["q"] = tuple(ids)
    lines = [ln for ln in p.get("description", "").split("\n") if not ln.startswith(ANSWERS)]
    body = "\n".join(lines).rstrip()
    p["description"] = (body + "\n\n" if body else "") + ANSWERS + ", ".join(ids)
    return p


def panel(
    kind: str,
    title: str,
    sqls: list[str] | str,
    *,
    w=12,
    h=8,
    description="",
    q: tuple | None = None,
    **extra,
) -> dict:
    sqls = [sqls] if isinstance(sqls, str) else sqls
    raw_title = title
    title, tail = short_title(title)
    description = "\n\n".join(x for x in (tail[:1].upper() + tail[1:] if tail else "", description) if x)
    p = {
        "type": kind,
        "title": title,
        "raw_title": raw_title,  # build-time only (fold matching); stripped before deploy
        "description": description,
        "datasource": DS,
        "targets": [target(s, chr(65 + i)) for i, s in enumerate(sqls)],
        "gridPos": {"w": w, "h": h},
        "options": {},
        "fieldConfig": {"defaults": {}, "overrides": []},
    }
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(p.get(k), dict):
            p[k].update(v)
        else:
            p[k] = v
    return answers(p, *q) if q else p


def markdown(title: str, md: str, *, w=24, h=10) -> dict:
    return {
        "type": "text",
        "title": title,
        "gridPos": {"w": w, "h": h},
        "options": {"mode": "markdown", "content": md},
    }


def row(title: str, *, tab: str | None = None) -> dict:
    """A section header. `tab` starts a new TAB at this row (v2 TabsLayout; grafana/v2.py): every row after it,
    up to the next `tab`, sits in that tab. Source-only on the v1 rollback path (v1 has no tabs)."""
    r = {"type": "row", "title": title, "collapsed": False, "gridPos": {"w": 24, "h": 1}, "panels": []}
    if tab:
        r["tab"] = tab
    return r


def notes_row(*text_panels: dict, title: str = "Notes (click to open)") -> dict:
    """Long text lives in ONE collapsed row at the END of a board: it costs one grid line until opened."""
    return {
        "type": "row",
        "title": title,
        "collapsed": True,
        "gridPos": {"w": 24, "h": 1},
        "panels": list(text_panels),
    }


def link_to(uid: str, title: str | None = None, *, keep_vars=False) -> dict:
    return {
        "title": title or BOARD_UIDS.get(uid, uid),
        "type": "link",
        "url": f"/d/{uid}",
        "keepTime": False,
        "includeVars": keep_vars,
        "targetBlank": False,
        "icon": "dashboard",
    }


def explorer_link(field: str = "ad_id", var: str | None = None) -> dict:
    """A data link that opens one ad (or advertiser) on the Explorer board."""
    var = var or field
    return {
        "title": f"Open {field} in Explorer",
        "url": f"/d/{PFX}-explorer?var-{var}=${{__data.fields.{field}}}",
    }


# ---------------------------------------------------------------- dashboard


def layout(panels: list[dict]) -> list[dict]:
    """Flow panels left-to-right, top-to-bottom on the 24-column grid; assign ids (collapsed rows'
    children too, laid out just below their row)."""
    x = y = rowh = 0
    next_id = 1
    for p in panels:
        p["id"] = next_id
        next_id += 1
        g = p["gridPos"]
        if p["type"] == "row" or x + g["w"] > 24:
            x, y = 0, y + rowh
            rowh = 0
        g["x"], g["y"] = x, y
        x += g["w"]
        rowh = max(rowh, g["h"])
        if p["type"] == "row":
            cx = cy = crow = 0
            for c in p.get("panels", []):
                c["id"] = next_id
                next_id += 1
                cg = c["gridPos"]
                if cx + cg["w"] > 24:
                    cx, cy, crow = 0, cy + crow, 0
                cg["x"], cg["y"] = cx, y + 1 + cy
                cx += cg["w"]
                crow = max(crow, cg["h"])
            x, y, rowh = 0, y + 1, 0
    return panels


def dashboard(
    uid: str, title: str, panels: list[dict], *, variables=(), links=(), tags=(), description=""
) -> dict:
    return {
        "uid": uid,
        "title": f"{TITLE_PREFIX} - {title}",
        "description": description,
        "tags": [TAG, "private", *tags],
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 1,
        "refresh": "",
        "liveNow": False,
        # load every panel on open, not lazily on scroll: a lazily-skipped panel can hide a broken
        # query from the browser check (and a human), and these boards are small with refresh OFF
        "preload": True,
        "time": {"from": "2020-01-01T00:00:00.000Z", "to": "now"},
        "timepicker": {"refresh_intervals": REFRESH_INTERVALS, "hidden": False},
        "templating": {"list": list(variables)},
        "annotations": {"list": []},
        "links": [link_to(u) for u in links],
        "panels": layout(list(panels)),
        "schemaVersion": 41,
    }


# ---------------------------------------------------------------- rules


_VAR_USE = re.compile(r"\$\{(\w+)(?::(\w+))?\}|\$(\w+)|\[\[(\w+)\]\]")
_ALLOWED_BARE = {"__timeFilter", "__timeFrom", "__timeTo", "__from", "__to", "__interval"}
_TIME_FILTER = "$__timeFilter("
_TIME_BOUNDS = {"$__timeFrom()": "'-infinity'::timestamptz", "$__timeTo()": "'infinity'::timestamptz"}


def strip_time_macros(sql: str) -> str:
    """Grafana's time macros for a query run outside Grafana, over ALL time: `$__timeFilter(expr)` becomes
    TRUE (parentheses inside expr balanced), `$__timeFrom()`/`$__timeTo()` the infinite bounds. The ONE
    copy of this rule: `db check` and the export test's EXPLAIN pass both use it."""
    for macro, bound in _TIME_BOUNDS.items():
        sql = sql.replace(macro, bound)
    out, i = [], 0
    while (j := sql.find(_TIME_FILTER, i)) >= 0:
        k, depth = j + len(_TIME_FILTER), 1
        while k < len(sql) and depth:
            depth += {"(": 1, ")": -1}.get(sql[k], 0)
            k += 1
        if depth:  # unbalanced: leave the rest as is, so the database names the error
            break
        out += [sql[i:j], "TRUE"]
        i = k
    return "".join(out) + sql[i:]


# Grafana's FieldColorModeId values, copied from packages/grafana-data/src/types/fieldColor.ts at the
# stack's own build (13.4.0, /api/health commit ed8f13c9a91faa2828f5459a65a1c0d3b2b0cdc6). The match is
# case-sensitive. Re-copy when the stack moves to a new minor version.
FIELD_COLOR_MODES = frozenset(
    {
        "thresholds",
        "palette-classic",
        "palette-classic-by-name",
        "palette-saturated",
        "palette-colorblind",
        "palette-categorical-next",
        "palette-categorical-next-2",
        "palette-categorical-next-3",
        "continuous-GrYlRd",
        "continuous-RdYlGr",
        "continuous-BlYlRd",
        "continuous-YlRd",
        "continuous-BlPu",
        "continuous-YlBl",
        "continuous-blues",
        "continuous-reds",
        "continuous-greens",
        "continuous-purples",
        "continuous-viridis",
        "continuous-magma",
        "continuous-plasma",
        "continuous-inferno",
        "continuous-cividis",
        "fixed",
        "shades",
        "gradient",
    }
)


def _minutes(interval: str) -> float:
    m = re.fullmatch(r"(\d+)([smhd])", interval)
    if not m:
        raise ValueError(interval)
    return int(m[1]) * {"s": 1 / 60, "m": 1, "h": 60, "d": 1440}[m[2]]


def _walk_panels(panels):
    for p in panels:
        yield p
        yield from _walk_panels(p.get("panels", []))


_FMT_VAR = re.compile(r"\$\{(\w+)(?::(\w+))?\}")


def _sqlstring(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def render(sql: str, variables: dict, state: str) -> str:
    """Interpolate ${var:fmt} the way Grafana's templateSrv does, for one variable state:
    'all' (the dashboard default), 'single' (one value) or 'multi' (two values).
    - All with a custom allValue: the allValue VERBATIM for every format but text/percentencode
      (template_srv.ts, v13.0.1); All without one: every option, formatted.
    - :sqlstring quotes each value ('' escapes) and joins with ','; :raw inserts as is.
    Values for 'single'/'multi' are placeholders: this checks SYNTAX, not results."""

    def one(m: re.Match) -> str:
        name, fmt = m[1], m[2]
        v = variables.get(name)
        if v is None or name.startswith("__"):
            return m[0]
        if v["type"] in ("custom", "textbox") and not v.get("includeAll"):
            val = v["current"].get("value", "")
            return val if fmt == "raw" else _sqlstring(val)
        if state == "all" and not v.get("includeAll") and (v.get("current") or {}).get("value"):
            val = v["current"]["value"]  # a saved default (e.g. tf's live viewpoint): what the board opens on
            return val if fmt == "raw" else _sqlstring(val)
        if state == "all" and v.get("includeAll"):
            if v.get("allValue"):
                return v["allValue"] if fmt not in ("text", "percentencode") else v["allValue"]
            vals = ["a", "b"]  # the full option list; shape is what matters
        elif state == "multi" and v.get("multi"):
            vals = ["x1", "x2"]
        else:
            vals = ["x1"]
        return ",".join(vals) if fmt == "raw" else ",".join(_sqlstring(x) for x in vals)

    return _FMT_VAR.sub(one, sql)


# a sentinel that reached the SQL outside a string literal
_BARE_SENTINEL = re.compile(r"(?<!')\b" + ALL_TOKEN + r"\b(?!')")


_COORD_LITERAL = re.compile(r"(?<![\w.])-?\d{1,3}\.\d{4,}")


def _site_url() -> str:
    """A URL on the crawled site (pipeline site_hosts, from the site file): never in panel text."""
    from scrape_harness import pipeline

    hosts = "|".join(map(re.escape, pipeline.consumer().site_hosts)) or r"(?!)"
    return rf"https?://[^\"']*(?:{hosts})"


def check(board: dict) -> list[str]:
    """Return every hard-rule violation in a built board ([] = clean)."""
    errs: list[str] = []
    uid = board["uid"]
    if board.get("refresh"):
        errs.append(f"{uid}: refresh must be OFF, got {board['refresh']!r}")
    for iv in board["timepicker"]["refresh_intervals"]:
        if _minutes(iv) < 10:
            errs.append(f"{uid}: refresh interval {iv} is under 10m")
    names = {v["name"]: v for v in board["templating"]["list"]}
    for p in _walk_panels(board["panels"]):
        where = f"{uid}#{p.get('id')} {p.get('title')!r}"
        if "renderer" in p["type"] or p["type"] == "image":
            errs.append(f"{where}: renderer/image panel")
        blob = json.dumps(p.get("options", {}))
        if re.search(r"<img", blob, re.I):
            errs.append(f"{where}: literal <img in a template (no media until the no-Referer check passes)")
        for t in p.get("targets", []):
            if t.get("datasource", {}).get("uid") != DS_UID:
                errs.append(f"{where}: target not on {DS_UID}")
            for m in _VAR_USE.finditer(t.get("rawSql", "")):
                name, fmt = m[1] or m[3] or m[4], m[2]
                if name in _ALLOWED_BARE or name.startswith("__"):
                    continue
                if name not in names:
                    errs.append(f"{where}: unknown variable {name}")
                elif fmt == "raw" and names[name]["type"] != "custom":
                    errs.append(f"{where}: ${{{name}:raw}} on a non-custom (non-whitelisted) variable")
                elif fmt not in ("sqlstring", "raw"):
                    errs.append(f"{where}: variable {name} must be ${{{name}:sqlstring}} or :raw")
    for v in board["templating"]["list"]:
        if v.get("includeAll") and v.get("allValue") and not re.fullmatch(r"'[^']*'", v["allValue"]):
            errs.append(f"{uid}: variable {v['name']} allValue {v['allValue']!r} is not a quoted SQL literal")
    for p in _walk_panels(board["panels"]):
        for t in p.get("targets", []):
            for state in ("all", "single", "multi"):
                if _BARE_SENTINEL.search(render(t.get("rawSql", ""), names, state)):
                    errs.append(f"{uid}#{p.get('id')}: unquoted {ALL_TOKEN} under {state}")
    # Grafana gives every top-level panel after a row to that row: after a COLLAPSED row they vanish
    # until it is opened (2026-11-08: 15 panels on 4 boards hid under a top "Notes" row)
    top = board["panels"]
    for i, p in enumerate(top[:-1]):
        if p.get("type") == "row" and p.get("collapsed") and top[i + 1].get("type") != "row":
            errs.append(
                f"{uid}: '{top[i + 1].get('title')}' follows collapsed row '{p.get('title')}' and is hidden"
            )
    # distances are PostGIS geography (export-db@9): a hand haversine would drift from ST_DWithin's
    # spheroid (0.28 % measured) and split a ring's drawn edge from its count
    for p in _walk_panels(board["panels"]):
        for t in p.get("targets", []):
            if re.search(r"\b6371(\.0+)?\b|asin\(\s*sqrt\(", t.get("rawSql", "")):
                errs.append(f"{uid}#{p.get('id')}: hand-written haversine; use ST_Distance/ST_DWithin")
    # xychart v2 manual mapping binds a series to a FRAME first: without `frame` it maps nothing and
    # the panel says "No data" over a full result (T2, drawn-content sweep 2026-11-08)
    for p in _walk_panels(board["panels"]):
        o = p.get("options") or {}
        if p.get("type") == "xychart" and o.get("mapping") == "manual":
            for i, s_ in enumerate(o.get("series", [])):
                if "frame" not in s_:
                    errs.append(
                        f"{uid}#{p.get('id')}: xychart series {i} has no frame matcher (draws nothing)"
                    )
    # a colour mode id Grafana does not know is NOT an error there: getFieldColorModeForField falls back
    # to thresholds (green/red at 80) and the panel draws in the wrong scheme, silently (4 panels did,
    # found 2026-11-13: continuous-oranges/-Oranges/-RdYlBu/-YlOrRd)
    for p in _walk_panels(board["panels"]):
        fc = p.get("fieldConfig") or {}
        modes = [(fc.get("defaults") or {}).get("color", {}).get("mode")]
        modes += [
            (pr.get("value") or {}).get("mode")
            for o in fc.get("overrides", [])
            for pr in o.get("properties", [])
            if pr.get("id") == "color"
        ]
        for mode in modes:
            if mode is not None and mode not in FIELD_COLOR_MODES:
                errs.append(f"{uid}#{p.get('id')}: colour mode {mode!r} is not a Grafana FieldColorModeId")
    # C21: a shared-key VALUE never reaches a frame (Inspect → Data shows every field): a query may count
    # link_key inside a CTE, but its final SELECT list may not name it
    for p in _walk_panels(board["panels"]):
        for t in p.get("targets", []):
            sql = t.get("rawSql", "")
            final = sql[sql.rfind("SELECT") :]
            if re.search(r"\blink_key\b", final.split("FROM", 1)[0]):
                errs.append(f"{uid}#{p.get('id')}: selects link_key (a shared-key value; C21)")
    # D8 (export-db@21): the board role is granted neither the gold-bearing tables nor the geocode cache, so
    # a query naming one would fail live; refuse it here first (a CTE of the same name is fine)
    for p in _walk_panels(board["panels"]):
        for t in p.get("targets", []):
            sql = t.get("rawSql", "")
            ctes = set(re.findall(r"\b([a-z_][a-z0-9_]*)\s+AS\s*\(", sql, re.I))
            for tbl in re.findall(r"\b(?:FROM|JOIN)\s+(?:li\.)?([a-z_][a-z0-9_]*)", sql, re.I):
                if tbl in NOT_GRANTED and tbl not in ctes:
                    errs.append(f"{uid}#{p.get('id')}: reads {tbl}, which the board role is not granted (D8)")
    # the gold mask (C19): an answer-bearing column is read only through the listings_masked view
    for p in _walk_panels(board["panels"]):
        for t in p.get("targets", []):
            for why in mask_violations(t.get("rawSql", "")):
                errs.append(f"{uid}#{p.get('id')}: {why} (read listings_masked)")
    for v in board["templating"]["list"]:
        for why in mask_violations(str(v.get("query", ""))):
            errs.append(f"{uid}: variable {v['name']} {why} (read listings_masked)")
    # a panel earns its area (operator rule, 2026-11-08): one or two numbers never get a panel of
    # their own — they fold into labels, legends, a table footer or descriptions, and a board keeps
    # at most ONE compact KPI strip
    strips = [p for p in board["panels"] if p.get("type") in ("stat", "gauge", "bargauge")]
    if len(strips) > 1:
        errs.append(
            f"{uid}: {len(strips)} number panels ({', '.join(p.get('title', '?') for p in strips)}); "
            "a board keeps at most ONE KPI strip"
        )
    # D2 (2026-11-13): persona-grain v2 output is a DRAFT until the B3 blind read passes 0.9, so every panel
    # that reads persona_id says so in its ⓘ. (Replaces the 2026-11-08 "v2 candidate in the title" rule:
    # D3 made v2 the default method.)
    for p in _walk_panels(board["panels"]):
        sql = " ".join(t.get("rawSql", "") for t in p.get("targets", []))
        if "persona_id" in sql and PERSONA_DRAFT not in p.get("description", ""):
            errs.append(f"{uid}#{p.get('id')}: reads persona_id without the '{PERSONA_DRAFT}' stamp in its ⓘ")
    # a fit view's `zoom` is its MAX zoom: unset (Grafana stores 1) it pins every map at world view
    for p in _walk_panels(board["panels"]):
        view = (p.get("options") or {}).get("view") or {}
        if p["type"] == "geomap" and view.get("id") == "fit" and (view.get("zoom") or 0) < 10:
            errs.append(
                f"{uid}#{p.get('id')}: geomap fit view without a max zoom >= 10 (capped at world view)"
            )
    # a stat/gauge's reduceOptions.fields regex matches the field's DISPLAY name; with a templated displayName
    # it matches nothing and the panel says "No data" (measured 2026-11-08 on the geo board "Ads within each ring")
    for p in _walk_panels(board["panels"]):
        rf = ((p.get("options") or {}).get("reduceOptions") or {}).get("fields") or ""
        dn = ((p.get("fieldConfig") or {}).get("defaults") or {}).get("displayName") or ""
        if rf and "${" in dn:
            errs.append(
                f"{uid}#{p.get('id')}: reduceOptions.fields regex with a templated displayName shows No data"
            )
    # NO images, ever (the operator, 2026-11-08: the media track is closed). media_urls stay data — counts and a
    # text list — but nothing renders them: no Photos layer, no image cell, no <img> / markdown image.
    for p in _walk_panels(board["panels"]):
        where = f"{uid}#{p.get('id')}"
        layers = (p.get("options") or {}).get("layers") or []
        if any(ly.get("type") == "photos" for ly in layers):
            errs.append(f"{where}: Photos layer (no images)")
        if '"image"' in json.dumps(p.get("fieldConfig") or {}) and '"cellOptions"' in json.dumps(
            p.get("fieldConfig") or {}
        ):
            for blob in re.findall(r'"cellOptions": \{[^}]*\}', json.dumps(p.get("fieldConfig"))):
                if '"type": "image"' in blob:
                    errs.append(f"{where}: image cell (no images)")
        text = json.dumps(p.get("options") or {})
        if re.search(r"<img|!\[[^\]]*\]\(", text, re.I) or re.search(_site_url(), text, re.I):
            errs.append(f"{where}: an image or a site URL in panel text (no images, no hotlinks)")
    seen_titles: dict[str, int] = {}
    for p in _walk_panels(board["panels"]):
        if p["type"] != "row" and p.get("title"):
            seen_titles[p["title"]] = seen_titles.get(p["title"], 0) + 1
    for ttl, n in seen_titles.items():
        if n > 1:
            errs.append(f"{uid}: {n} panels share the title {ttl!r}")
    # a panel earns its area: titles <= 8 words (method notes go to the description)
    for p in _walk_panels(board["panels"]):
        if len((p.get("title") or "").split()) > MAX_TITLE_WORDS:
            errs.append(f"{uid}#{p.get('id')}: title over {MAX_TITLE_WORDS} words: {p.get('title')!r}")
    # EVERY geomap opens on the viewpoint (operator rule, no allow-list): its view fits ONE named layer whose
    # query reads the li.viewpoint table — so the centre is data, never a coordinate in this JSON
    for p in _walk_panels(board["panels"]):
        if p["type"] != "geomap":
            continue
        view = (p.get("options") or {}).get("view") or {}
        name = view.get("layer")
        lyr = next((ly for ly in (p.get("options") or {}).get("layers", []) if ly.get("name") == name), None)
        ref = ((lyr or {}).get("filterData") or {}).get("options")
        sql = next((tg.get("rawSql", "") for tg in p.get("targets", []) if tg.get("refId") == ref), "")
        if (
            view.get("id") != "fit"
            or view.get("allLayers")
            or name != VIEW_LAYER
            or "FROM viewpoint" not in sql
        ):
            errs.append(
                f"{uid}#{p.get('id')}: geomap does not open on the viewpoint (fit to a viewpoint-derived layer)"
            )
        if not ((p.get("options") or {}).get("controls") or {}).get("showMeasure"):
            errs.append(f"{uid}#{p.get('id')}: geomap without the stock measure tools")
    # the out-of-region toggle (operator rule, 2026-11-08): every query that reads an ad's pin or text point
    # carries that point's region flag (an undeclared ${out_of_region} is caught above as an unknown variable)
    for p in _walk_panels(board["panels"]):
        for tg in p.get("targets", []):
            for e in region_violations(tg.get("rawSql", "")):
                errs.append(f"{uid}#{p.get('id')} {tg.get('refId')}: {e}")
    # no coordinate literal in any query: places (the viewpoint, the region box) are DATA, read from tables
    for p in _walk_panels(board["panels"]):
        for tg in p.get("targets", []):
            if _COORD_LITERAL.search(tg.get("rawSql", "")):
                errs.append(
                    f"{uid}#{p.get('id')}: a coordinate-like literal in SQL (places come from tables)"
                )
    return errs
