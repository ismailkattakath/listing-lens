"""The gold mask hides li's answer for eval-gold ads (C19), from one column list, and no board reads that
answer from anything but the listings_masked view."""

import copy

import pytest

from listing_lens import gold_mask
from listing_lens.grafana import boards, model


def _content_panels(built):
    for b in built:
        for top in b["panels"]:
            for p in [top, *top.get("panels", [])]:
                if p.get("type") != "row":
                    yield b["uid"], p


def unmasked_panels(built) -> list[str]:
    return sorted(
        f"{u}/{p.get('title', '')}"
        for u, p in _content_panels(built)
        if any(model.mask_violations(t.get("rawSql", "")) for t in p.get("targets", []))
    )


def test_the_columns_come_from_the_ddl_and_cover_the_mask():
    cols = gold_mask.latest_columns()
    assert len(cols) == len(set(cols))
    assert set(cols) >= gold_mask.MASKED
    assert {"pin_lat", "pin_lon", "km_pin_city", "location_valid_pin"}.isdisjoint(
        gold_mask.MASKED
    )  # pin-only stays
    assert gold_mask.MASKED_V2 <= gold_mask.MASKED_V3


def test_postgres_nulls_exactly_the_gold_columns(pg):
    """A table with every listings_latest column, one gold-v2 ad, one gold-v3 ad and one plain ad, all values
    set: through the mask each gold ad's MASKED_V2 / MASKED_V3 columns read NULL and nothing else changes."""
    psycopg = pytest.importorskip("psycopg")
    cols = gold_mask.latest_columns()
    flags = ("place_gold_v2", "hint_gold_v3")
    typed = ", ".join(f"{c} {'boolean' if c in flags else 'text'}" for c in cols)
    vals = lambda v2, v3: ", ".join(  # noqa: E731
        str({"place_gold_v2": v2, "hint_gold_v3": v3}[c]).lower() if c in flags else f"'{c}'" for c in cols
    )
    with psycopg.connect(pg, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS fake_latest")
        c.execute(f"CREATE TEMP TABLE fake_latest ({typed})")
        rows = ", ".join(f"({vals(*f)})" for f in ((True, False), (False, True), (False, False)))
        c.execute(f"INSERT INTO fake_latest ({', '.join(cols)}) VALUES {rows}")
        cur = c.execute(
            gold_mask.view_select("fake_latest") + " ORDER BY place_gold_v2 DESC, hint_gold_v3 DESC"
        )
        names = [d.name for d in cur.description]
        gold, gold3, plain = (dict(zip(names, r, strict=True)) for r in cur.fetchall())
    assert names[: len(cols)] == cols  # same columns, same order
    assert gold["gold_masked"] is True and gold["mask_reason"] == "gold_v2"
    assert gold3["gold_masked"] is True and gold3["mask_reason"] == "gold_v3"
    assert plain["gold_masked"] is False and plain["mask_reason"] is None
    assert {c for c in cols if gold[c] is None} == set(gold_mask.MASKED_V2)
    assert {c for c in cols if gold3[c] is None} == set(gold_mask.MASKED_V3)
    assert all(plain[c] is not None for c in cols)


@pytest.mark.parametrize(
    "sql, bad",
    [
        ("SELECT l.place_lat FROM listings_latest l", True),
        ("SELECT d.km_pin_text FROM derived d", True),
        ("SELECT l.* FROM listings_latest l", True),
        ("SELECT l.pin_lat, l.km_pin_city FROM listings_latest l", False),
        ("SELECT m.place_lat FROM listings_masked m", False),
    ],
)
def test_the_leak_guard(sql, bad):
    assert bool(model.mask_violations(sql)) is bad


def test_no_board_reads_a_gold_answer_raw():
    assert unmasked_panels(boards.build_all()) == []


def test_model_check_refuses_a_raw_read():
    b = copy.deepcopy(boards.build_all()[0])
    p = next(x for x in model._walk_panels(b["panels"]) if x.get("targets"))
    p["targets"][0]["rawSql"] = "SELECT l.place_lat FROM listings_latest l"
    assert any("read listings_masked" in e for e in model.check(b))


def test_no_board_reads_a_table_the_role_is_not_granted():
    built = boards.build_all()
    assert not [e for b in built for e in model.check(b) if "not granted" in e]
    b = copy.deepcopy(built[0])
    p = next(x for x in model._walk_panels(b["panels"]) if x.get("targets"))
    p["targets"][0]["rawSql"] = "SELECT count(*) FROM geocodes"
    assert any("not granted" in e for e in model.check(b))
    p["targets"][0]["rawSql"] = "WITH ads AS (SELECT 1) SELECT * FROM ads"  # a CTE of the same name is fine
    assert not [e for e in model.check(b) if "not granted" in e]
