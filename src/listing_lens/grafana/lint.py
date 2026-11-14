"""Chart-choice lint: known-bad pairings of chart type and data, checked on the built board JSON.

Partial by design. A chart is chosen per question by the viz-designer; this catches only the
pairings with a published case against them, mechanically. A panel that breaks a rule on purpose
carries a WAIVER in boards.py with its reason, and a waiver that no longer fires fails too, so
waivers cannot rot. model.check stays the hard-rule gate (it also runs before every apply); this
lint runs in pytest only.

Prior art checked (2026-11-13): grafana/dashboard-linter's 18 rules cover datasources, PromQL/LogQL,
templating, units and titles. None covers chart choice, so this is not a re-implementation.

Rules, each with its source:
- pie-slices: a piechart must bound its slices to <= 5 statically (reduceOptions.limit or a SQL
  LIMIT). Angle and area are judged less accurately than position or length
  (Cleveland & McGill 1984, "Graphical Perception", JASA 79:531-554).
- dual-axis: no field on a right-hand y axis next to a left-hand one. Two scales invite a false
  comparison of slopes and crossings (Few 2008, "Dual-Scaled Axes in Graphs: Are They Ever the Best
  Solution?").
- bar-baseline: a bar encodes its value as LENGTH, so the value axis starts at zero. Grafana's
  barchart already sets softMin = 0 when neither min nor axisSoftMin is set
  (public/app/plugins/panel/barchart/utils.ts), so only an explicit min > 0, axisSoftMin > 0 or a log
  scale is flagged (Few 2012, "Show Me the Numbers", 2nd ed.; Tufte 1983, the lie factor).
- diverging-palette: a diverging or multi-hue rainbow scheme implies a meaningful midpoint, so on a
  plain magnitude it invents a boundary that is not in the data (Harrower & Brewer 2003, ColorBrewer;
  Borland & Taylor 2007, "Rainbow Color Map (Still) Considered Harmful"). A waiver must name the
  midpoint.
- categorical-on-ordinal: a categorical palette loses the order of an ordered field, here a field
  whose name says it is ordered (decile, bucket, band, bin, rank, age, page) that a barchart colours
  by (same sources).
- gauge-unbounded: a gauge or bar gauge draws a value as a share of its range, so it needs an
  explicit max (Few 2006, "Information Dashboard Design", on gauges).
"""

from __future__ import annotations

import re

from .model import _walk_panels

# Grafana's FieldColorModeId values (see model.FIELD_COLOR_MODES) that run through a midpoint
DIVERGING_MODES = frozenset({"continuous-GrYlRd", "continuous-RdYlGr", "continuous-BlYlRd"})
# d3 scheme names the heatmap offers that are diverging or rainbow (the heatmap's own registry)
DIVERGING_SCHEMES = frozenset(
    {
        "BrBG",
        "PiYG",
        "PRGn",
        "PuOr",
        "RdBu",
        "RdGy",
        "RdYlBu",
        "RdYlGn",
        "Spectral",
        "Rainbow",
        "Sinebow",
        "Turbo",
    }
)
ORDINAL_FIELD = re.compile(r"(^|_)(decile|bucket|band|bin|rank|age|page)(_|$)", re.I)
_LIMIT = re.compile(r"\bLIMIT\s+(\d+)\b", re.I)
RULES = (
    "pie-slices",
    "dual-axis",
    "bar-baseline",
    "diverging-palette",
    "categorical-on-ordinal",
    "gauge-unbounded",
)


def _props(fc: dict) -> list[tuple[str, dict]]:
    """(field name or '*', the field-config dict) for the defaults and every override."""
    out = [("*", fc.get("defaults") or {})]
    for o in fc.get("overrides", []):
        name = str((o.get("matcher") or {}).get("options", "?"))
        flat: dict = {}
        for pr in o.get("properties", []):
            key, val = pr.get("id", ""), pr.get("value")
            if key.startswith("custom."):
                flat.setdefault("custom", {})[key[len("custom.") :]] = val
            else:
                flat[key] = val
        out.append((name, flat))
    return out


def panel_hits(p: dict) -> list[tuple[str, str]]:
    """(rule, detail) for every rule this panel breaks."""
    hits: list[tuple[str, str]] = []
    kind = p.get("type")
    fc = p.get("fieldConfig") or {}
    props = _props(fc)
    opts = p.get("options") or {}

    if kind == "piechart":
        limit = (opts.get("reduceOptions") or {}).get("limit")
        sql_limits = [
            int(m[1]) for t in p.get("targets", []) for m in [_LIMIT.search(t.get("rawSql", ""))] if m
        ]
        bound = limit if limit is not None else (max(sql_limits) if sql_limits else None)
        if bound is None or bound > 5:
            hits.append(("pie-slices", f"slices bounded at {bound!r}, need <= 5"))

    # dual = some field on the right while the defaults (every other field) are not
    right = [n for n, c in props[1:] if (c.get("custom") or {}).get("axisPlacement") == "right"]
    if right and (props[0][1].get("custom") or {}).get("axisPlacement") != "right":
        hits.append(("dual-axis", f"right-hand axis on {', '.join(right)}"))

    if kind == "barchart":
        for name, c in props:
            cu = c.get("custom") or {}
            low = [v for v in (c.get("min"), cu.get("axisSoftMin")) if isinstance(v, (int, float)) and v > 0]
            if low:
                hits.append(("bar-baseline", f"{name}: value axis starts at {low[0]}"))
            if (cu.get("scaleDistribution") or {}).get("type") == "log":
                hits.append(("bar-baseline", f"{name}: log value axis"))

    for name, c in props:
        mode = (c.get("color") or {}).get("mode")
        if mode in DIVERGING_MODES:
            hits.append(("diverging-palette", f"{name}: {mode}"))
    if kind == "heatmap":
        scheme = (opts.get("color") or {}).get("scheme")
        if scheme in DIVERGING_SCHEMES:
            hits.append(("diverging-palette", f"heatmap scheme {scheme}"))

    by = opts.get("colorByField")
    if kind == "barchart" and by and ORDINAL_FIELD.search(str(by)):
        mode = ((fc.get("defaults") or {}).get("color") or {}).get("mode", "palette-classic")
        if str(mode).startswith("palette-"):
            hits.append(("categorical-on-ordinal", f"colour by {by} with {mode}"))

    if kind in ("gauge", "bargauge") and not isinstance((fc.get("defaults") or {}).get("max"), (int, float)):
        hits.append(("gauge-unbounded", "no explicit max"))
    return hits


def lint(board: dict, waivers: dict) -> list[str]:
    """Every unwaived hit, and every waiver that no longer matches a hit ([] = clean).

    waivers: {(board uid, panel title): {rule: reason}}; a reason must be a sentence, not a tag."""
    errs: list[str] = []
    uid = board["uid"]
    used: set[tuple[str, str, str]] = set()
    for p in _walk_panels(board["panels"]):
        title = p.get("title", "")
        mine = waivers.get((uid, title), {})
        for rule, detail in panel_hits(p):
            if rule in mine:
                used.add((uid, title, rule))
            else:
                errs.append(f"{uid}#{p.get('id')} {title!r}: {rule}: {detail}")
    for (w_uid, title), rules in waivers.items():
        if w_uid != uid:
            continue
        for rule, reason in rules.items():
            if rule not in RULES:
                errs.append(f"{uid} {title!r}: waiver names unknown rule {rule!r}")
            elif len(str(reason).split()) < 6:
                errs.append(f"{uid} {title!r}: waiver for {rule} needs a reason (a sentence), got {reason!r}")
            elif (uid, title, rule) not in used:
                errs.append(f"{uid} {title!r}: stale waiver for {rule} (the rule no longer fires)")
    return errs
