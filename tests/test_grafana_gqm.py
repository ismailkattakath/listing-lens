"""Every shipped panel answers a GQM question, and every question has a panel: a RATCHET until the rework.

The GQM tree is the analyst's file in DATA (notes/li-grafana-gqm.md), never in git. Where DATA is absent
(the Nix sandbox, CI) the DATA-reading tests SKIP and say why, unless LLENS_REQUIRE_GQM=1, which makes a
missing tree a FAILURE: ship's host-side check sets it, so on the operator's path a skip cannot read as
green.

Ratchet: tests/gqm_gap_baseline.json lists today's gaps by board uid + panel title (ids are renumbered
by layout). A gap that is NOT in the baseline fails (a new untagged panel, a question that lost its
panel), and so does a baseline entry that is no longer a gap, so every rework commit must lower it. The
commit that reaches zero deletes the baseline and the test becomes strict.
"""

import json
import os
import warnings
from pathlib import Path

import pytest

from listing_lens.grafana import boards, gqm, model, tf

BASELINE = Path(__file__).with_name("gqm_gap_baseline.json")
REQUIRE = os.environ.get("LLENS_REQUIRE_GQM") == "1"
# read at import, before any fixture swaps LLENS_DATA for a tmp dir
GQM_PATH = gqm.path()


def _content_panels(built):
    for b in built:
        for top in b["panels"]:
            for p in [top, *top.get("panels", [])]:
                if p.get("type") != "row":
                    yield b["uid"], p


def _key(uid, p):
    return f"{uid}/{p.get('title', '')}"


@pytest.fixture(scope="module")
def built():
    return boards.build_all()


@pytest.fixture(scope="module")
def tree():
    if GQM_PATH is None or not GQM_PATH.is_file():
        why = f"the GQM tree is not readable ({GQM_PATH or 'LLENS_DATA unset'})"
        if REQUIRE:
            pytest.fail(f"LLENS_REQUIRE_GQM=1 and {why}")
        pytest.skip(f"{why}; set LLENS_REQUIRE_GQM=1 to make this a failure")
    return gqm.parse(GQM_PATH.read_text())


def gaps(built, tree) -> dict[str, list[str]]:
    used = {q for _, p in _content_panels(built) for q in p.get("q", ())}
    return {
        "panels_without_question": sorted(
            _key(u, p)
            for u, p in _content_panels(built)
            if not p.get("q") and (u, p.get("title")) not in boards.GQM_WAIVERS
        ),
        "questions_without_panel": sorted(tree.active - tree.no_panel - used),
    }


# ---- needs no DATA: the tag mechanism itself


def test_panel_keys_are_unique_per_board(built):
    keys = [_key(u, p) for u, p in _content_panels(built)]
    assert len(keys) == len(set(keys)), "two content panels on one board share a title"


def test_q_and_the_description_agree(built):
    for u, p in _content_panels(built):
        lines = [ln for ln in p.get("description", "").split("\n") if ln.startswith(model.ANSWERS)]
        if p.get("q"):
            assert lines == [model.ANSWERS + ", ".join(p["q"])], _key(u, p)
            assert all(gqm.QID.match(q) for q in p["q"]), _key(u, p)
        else:
            assert not lines, f"{_key(u, p)}: an Answers line without q"


def test_q_never_reaches_grafana(built):
    b = json.loads(json.dumps(built[0]))
    model.answers(b["panels"][0], "G1.Q1")
    out = json.dumps(tf._board_json(b))
    assert '"q":' not in out and "Answers: G1.Q1" in out


def test_answers_replaces_rather_than_stacks():
    p = model.answers({"description": "Why."}, "G1.Q1")
    assert model.answers(p, "G2.Q1", "G2.Q2")["description"] == "Why.\n\nAnswers: G2.Q1, G2.Q2"


def test_the_parser_reads_the_id_grammar():
    t = gqm.parse(
        "### G1.Q1 — a\n### G1.Q2 — b — no panel: served by G1.Q1\n### G1.Q3 (retired) — c\n#### G9.Q9 — not a q\n"
    )
    assert t.active == {"G1.Q1", "G1.Q2"} and t.no_panel == {"G1.Q2"} and t.retired == {"G1.Q3"}


# ---- needs DATA: the ratchet


def test_every_tag_names_a_live_question(built, tree):
    bad = [(_key(u, p), q) for u, p in _content_panels(built) for q in p.get("q", ()) if q not in tree.active]
    assert not bad, f"tags naming an unknown or retired question: {bad}"


def test_gaps_only_shrink(built, tree):
    now = gaps(built, tree)
    warnings.warn(
        "GQM gaps: "
        + ", ".join(f"{k} {len(v)}" for k, v in now.items())
        + (" (strict: no baseline)" if not BASELINE.exists() else f" (baseline {BASELINE.name})"),
        stacklevel=1,
    )
    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else {k: [] for k in now}
    for k, cur in now.items():
        new = sorted(set(cur) - set(base.get(k, [])))
        closed = sorted(set(base.get(k, [])) - set(cur))
        assert not new, f"{k}: NEW gaps (tag the panel or build the question's panel): {new}"
        assert not closed, f"{k}: closed gaps still in {BASELINE.name}; lower the baseline: {closed}"


def test_the_baseline_holds_ids_only():
    if not BASELINE.exists():
        return
    base = json.loads(BASELINE.read_text())
    assert set(base) == {"panels_without_question", "questions_without_panel"}
    assert all(gqm.QID.match(q) for q in base["questions_without_panel"])


def test_a_gqm_waiver_is_reasoned_and_live(built):
    present = {(u, p.get("title")) for u, p in _content_panels(built)}
    for key, reason in boards.GQM_WAIVERS.items():
        assert key in present, f"stale GQM waiver: {key}"
        assert len(reason.split()) >= 6, f"a waiver needs a reason: {key}"
