"""The chart-choice lint holds on every board, and each of its rules bites."""

import copy

import pytest

from listing_lens.grafana import boards, lint
from listing_lens.grafana.model import PFX


@pytest.fixture(scope="module")
def built():
    return boards.build_all()


def test_every_board_passes_the_chart_lint(built):
    assert [e for b in built for e in lint.lint(b, boards.LINT_WAIVERS)] == []


def _board(*panels):
    return {"uid": "t", "panels": list(panels)}


def _bar(**fc):
    return {"id": 1, "type": "barchart", "title": "bars", "fieldConfig": {"defaults": fc, "overrides": []}}


@pytest.mark.parametrize(
    "panel, rule",
    [
        (
            {"id": 1, "type": "piechart", "title": "pie", "targets": [{"rawSql": "SELECT a, n FROM t"}]},
            "pie-slices",
        ),
        (
            {"id": 1, "type": "piechart", "title": "pie", "options": {"reduceOptions": {"limit": 8}}},
            "pie-slices",
        ),
        (
            {
                "id": 1,
                "type": "timeseries",
                "title": "two axes",
                "fieldConfig": {
                    "defaults": {},
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "b"},
                            "properties": [{"id": "custom.axisPlacement", "value": "right"}],
                        }
                    ],
                },
            },
            "dual-axis",
        ),
        (_bar(min=10), "bar-baseline"),
        (_bar(custom={"axisSoftMin": 5}), "bar-baseline"),
        (_bar(custom={"scaleDistribution": {"type": "log", "log": 10}}), "bar-baseline"),
        (_bar(color={"mode": "continuous-RdYlGr"}), "diverging-palette"),
        (
            {"id": 1, "type": "heatmap", "title": "h", "options": {"color": {"scheme": "Spectral"}}},
            "diverging-palette",
        ),
        (
            {"id": 1, "type": "barchart", "title": "b", "options": {"colorByField": "depth_decile"}},
            "categorical-on-ordinal",
        ),
        ({"id": 1, "type": "gauge", "title": "g", "fieldConfig": {"defaults": {}}}, "gauge-unbounded"),
    ],
)
def test_each_rule_bites(panel, rule):
    errs = lint.lint(_board(panel), {})
    assert any(f": {rule}: " in e for e in errs), errs


@pytest.mark.parametrize(
    "panel",
    [
        {"id": 1, "type": "piechart", "title": "pie", "options": {"reduceOptions": {"limit": 5}}},
        {"id": 1, "type": "piechart", "title": "pie", "targets": [{"rawSql": "SELECT a FROM t LIMIT 4"}]},
        _bar(),  # Grafana anchors bars at 0 when min and axisSoftMin are unset
        _bar(min=0),
        _bar(color={"mode": "continuous-blues"}),
        {"id": 1, "type": "gauge", "title": "g", "fieldConfig": {"defaults": {"min": 0, "max": 1}}},
        {
            "id": 1,
            "type": "timeseries",
            "title": "one axis, moved",
            "fieldConfig": {"defaults": {"custom": {"axisPlacement": "right"}}, "overrides": []},
        },
    ],
)
def test_good_choices_pass(panel):
    assert lint.lint(_board(panel), {}) == []


def test_a_waiver_needs_a_reason_and_must_still_fire():
    p = _bar(min=10)
    reason = "A deliberate zoom on a narrow band, with the baseline stated in the title."
    assert lint.lint(_board(p), {("t", "bars"): {"bar-baseline": reason}}) == []
    assert any("needs a reason" in e for e in lint.lint(_board(p), {("t", "bars"): {"bar-baseline": "ok"}}))
    assert any(
        "stale waiver" in e for e in lint.lint(_board(_bar()), {("t", "bars"): {"bar-baseline": reason}})
    )
    assert any("unknown rule" in e for e in lint.lint(_board(p), {("t", "bars"): {"nope": reason}}))


def test_removing_a_live_waiver_fails_the_lint(built):
    waivers = copy.deepcopy(boards.LINT_WAIVERS)
    waivers.pop((f"{PFX}-advertiser", "Cluster sizes and cumulative share of ads"))
    errs = [e for b in built for e in lint.lint(b, waivers)]
    assert any(f"{PFX}-advertiser" in e and ": dual-axis: " in e for e in errs), errs
