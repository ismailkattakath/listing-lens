"""The gold mask: ONE list of the columns that carry li's answer for an eval-gold ad, and the SQL that
hides them (GQM constraint C19; DATA notes/li-grafana-encodings.md § 1c).

Two gold sets, two flags:
- place_gold_v2 (exported): the operator's manual place answer is gold, so every place_* column and the
  validity/reason computed from it read NULL;
- hint_gold_v3 (exported since export-db@19, D6): the text-hint labels are gold, so the hint, its geocode
  and every derivative (a distance or a decision built from li's answer leaks it as surely as the column)
  read NULL, plus every place_* column.

ONE source: export-db's listings_masked view (D7) is generated from view_select() here, and the boards read
that view (model.check refuses an ad-grain read of a masked column from listings_latest or derived).
Pin-only columns (pin_*, km_pin_city, location_valid_pin) and the label-derived city_centre_* are not
masked.
"""

from __future__ import annotations

import re
from importlib import resources

PLACE = (
    "place_decision",
    "place_decided_by",
    "place_decision_status",
    "place_review_ref",
    "place_point_source",
    "place_lat",
    "place_lon",
    "place_geog",
    "location_valid_place",
)
HINT = (
    "text_hint_raw",
    "text_hint_kind",
    "text_hint_query",
    "text_hint_extractor",
    "geocode_provider",
    "geocode_status",
    "geocode_formatted",
    "geocode_lat",
    "geocode_lon",
    "geocode_precise",
    "geocode_place_id",
    "geocode_types",
    "geocode_expires_at",
    "geocode_geog",
    "geocode_decision",
    "geocode_location_type",
    "km_pin_text",
    "km_text_city",
    "location_valid_text",
)
BOTH = ("location_invalid_reason",)  # its reason can name the text or the place answer
# columns masked when the ad is v2 gold / v3 gold
MASKED_V2 = frozenset(PLACE + BOTH)
MASKED_V3 = frozenset(PLACE + HINT + BOTH)
MASKED = MASKED_V2 | MASKED_V3

V2 = "x.place_gold_v2 IS TRUE"
V3 = "x.hint_gold_v3 IS TRUE"  # a boolean column since export-db@19 (D6)


def _ddl() -> str:
    return (resources.files("listing_lens.db.schema") / "0001_li.sql").read_text()


def latest_columns() -> list[str]:
    """listings_latest's columns, in order, read from its DDL: listings' own, then the derived list."""
    ddl = _ddl()
    table = re.search(r"^CREATE TABLE listings \((.*?)^\);", ddl, re.S | re.M)[1]
    own = [
        m[1]
        for ln in table.splitlines()
        if (m := re.match(r"\s*([a-z_][a-z0-9_]*)\s+\S", ln)) and m[1].upper() not in ("PRIMARY", "UNIQUE")
    ]
    view = re.search(r"CREATE VIEW listings_latest AS(.*?);", ddl, re.S)[1]
    select = view[view.index("SELECT l.*") + len("SELECT l.*") : view.index("FROM latest")]
    extra = re.findall(r"\b[ad]\.([a-z_][a-z0-9_]*)", select)
    return own + extra


def view_select(src: str = "listings_latest") -> str:
    """SELECT every listings_latest column with the gold columns set to NULL, plus gold_masked and
    mask_reason ('gold_v2' | 'gold_v3' | NULL). A view body or a CTE body."""
    cols = []
    for c in latest_columns():
        if c in MASKED:
            cond = (
                " OR ".join(x for x, s in ((V2, MASKED_V2), (V3, MASKED_V3)) if c in s and x != "FALSE")
                or "FALSE"
            )
            cols.append(f"CASE WHEN {cond} THEN NULL ELSE x.{c} END AS {c}")
        else:
            cols.append(f"x.{c}")
    cols.append(f"({V2} OR {V3}) AS gold_masked")
    cols.append(f"CASE WHEN {V3} THEN 'gold_v3' WHEN {V2} THEN 'gold_v2' END AS mask_reason")
    return "SELECT " + ", ".join(cols) + f" FROM {src} x"
