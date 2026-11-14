"""Every board, built from model.py. SQL follows the derived copy's schema contract agreed with
`llens export-db`: timestamptz without a _utc suffix, pin_/text_hint_/geocode_/city_centre_
prefixes, jsonb lists, list_page/list_page_count/list_position."""

from __future__ import annotations

import json
import re

from listing_lens import site

from . import model
from .model import (
    DS_UID,
    PFX,
    custom_var,
    dashboard,
    explorer_link,
    in_filter,
    markdown,
    out_of_region_var,
    panel,
    pin_flagged,
    pin_in,
    pin_ok,
    query_var,
    row,
    subcategory_var,
    target,
    text_flagged,
    text_in,
    text_ok,
)

# THE depth sample (analyst, 2026-11-08): the run whose design is the plain depth stratification. The
# recruit run is ALSO depth-stratified (with its own decile + weight) and differs only by
# sample.select = 'recruit', so neither "has a weight", "has a decile", fetch_reason nor stratify alone
# identifies it. Selected by design, never by id; the earliest such run, so two are never pooled.
# The SITE's (site config [boards], DATA, read at build time): its two lists as (slug, short label), A then B (a
# node's B arc is 1 - A), the service-mode concepts (the ad page's service_mode values and the services
# vocabulary's notations), and the place whose clock li parses the ad page's times in.
(LIST_A, SHORT_A), (LIST_B, SHORT_B) = site.get("boards", "lists")
INCALL, OUTCALL = site.get("boards", "mode_concepts")
LOCAL_TIME_PLACE = site.get("boards", "local_time_place")
DEPTH_RUN = (
    "(SELECT r.run_id FROM runs r WHERE r.sample ->> 'stratify' = 'depth' "
    "AND r.sample ->> 'select' IS NULL ORDER BY r.created_at LIMIT 1)"
)
DEPTH_SAMPLE = f"(SELECT s.site, s.ad_id, s.weight, s.decile FROM samples s WHERE s.run_id = {DEPTH_RUN})"

# Explorer's advertiser filter takes a v1 advertiser id, a v2 ad-cluster id (v2:…) or a v2 persona id (p2:…)
ADVERTISER_FILTER = (
    "('__all' IN (${advertiser:sqlstring}) OR l.advertiser_id IN (${advertiser:sqlstring}) "
    "OR (l.site, l.ad_id) IN (SELECT site, ad_id FROM advertisers_v2 "
    "WHERE operator_id IN (${advertiser:sqlstring}) OR persona_id IN (${advertiser:sqlstring})))"
)
# the explorer board's service filters: an ad passes when it OFFERS (services_ad_concepts.offered, the step's own
# polarity) any selected concept / any concept in a selected group. All is the custom all-value '__all', so
# the OR folds to true at plan time and the EXISTS never runs; ads with no services stay under All.
_SVC_OFFERED = (
    "EXISTS (SELECT 1 FROM services_ad_concepts ac JOIN services_concepts c USING (concept) "
    "LEFT JOIN services_concepts b ON b.concept = c.broader "
    "WHERE ac.site = l.site AND ac.ad_id = l.ad_id AND ac.offered AND {test})"
)
SERVICE_FILTER = (
    "('__all' IN (${service:sqlstring}) OR "
    + _SVC_OFFERED.format(test="c.pref_label IN (${service:sqlstring})")
    + ")"
)
# every concept -> itself and ALL its ancestors over `broader` (the vocabulary is multi-level: bareback blow job
# -> blow job -> oral sex); `path` is the cycle guard. A group then matches its whole subtree (operator, 2026-11-13)
_ANCESTORS = (
    "WITH RECURSIVE anc(concept, ancestor, path) AS ("
    "SELECT concept, concept, ARRAY[concept] FROM services_concepts "
    "UNION ALL SELECT a.concept, c.broader, a.path || c.broader FROM anc a "
    "JOIN services_concepts c ON c.concept = a.ancestor "
    "WHERE c.broader IS NOT NULL AND NOT c.broader = ANY (a.path)) "
)
# uncorrelated IN (…): the recursive CTE runs once per query, not once per ad
SERVICE_GROUP_FILTER = (
    "('__all' IN (${service_group:sqlstring}) OR (l.site, l.ad_id) IN ("
    + _ANCESTORS
    + "SELECT ac.site, ac.ad_id FROM services_ad_concepts ac JOIN anc a USING (concept) "
    "JOIN services_concepts g ON g.concept = a.ancestor "
    "WHERE ac.offered AND g.pref_label IN (${service_group:sqlstring})))"
)
_OFFERED_CONCEPT = (
    "EXISTS (SELECT 1 FROM services_ad_concepts ac WHERE ac.concept = c.concept AND ac.offered)"
)
SERVICE_VAR_SQL = (
    f"SELECT c.pref_label FROM services_concepts c WHERE {_OFFERED_CONCEPT} ORDER BY c.sort_order"
)
# the immediate groups present (a top-level concept is its own group) plus every higher ancestor of an
# offered concept, so "oral sex" is selectable though no ad lists it as an immediate group
SERVICE_GROUP_VAR_SQL = (
    _ANCESTORS + "SELECT DISTINCT g.pref_label FROM services_concepts c JOIN anc a ON a.concept = c.concept "
    "JOIN services_concepts g ON g.concept = a.ancestor "
    f"WHERE {_OFFERED_CONCEPT} AND (a.ancestor <> c.concept OR c.broader IS NULL) ORDER BY 1"
)
PERSONA_SELECTED = (
    "EXISTS (SELECT 1 FROM unnest(ARRAY[${advertiser:sqlstring}]::text[]) x WHERE x LIKE 'p2:%')"
)
# G9 "One ad cluster": the ad-level link graph of the selected ad cluster or persona (encodings § 3):
# nodes = ads (opaque id, count of link kinds, FE/TS arc), edges = kinds only; never a title, city,
# phone, age, date or key value (C21). Empty until ONE ad cluster or persona is selected.
_AD_NODES = """
WITH sel AS (SELECT DISTINCT a.site, a.ad_id FROM advertisers_v2 a
             WHERE NOT '__all' IN (${advertiser:sqlstring})
               AND (a.operator_id IN (${advertiser:sqlstring}) OR a.persona_id IN (${advertiser:sqlstring}))),
k AS (SELECT v.site, v.ad_a AS ad_id, v.link_kind FROM advertiser_v2_links v JOIN sel ON sel.site = v.site AND sel.ad_id = v.ad_a
      UNION SELECT v.site, v.ad_b, v.link_kind FROM advertiser_v2_links v JOIN sel ON sel.site = v.site AND sel.ad_id = v.ad_b)"""
AD_GRAPH_NODES = f"""{_AD_NODES}
SELECT l.ad_id AS id, l.ad_id AS title, count(DISTINCT k.link_kind)::int AS mainstat,
       (l.subcategory = '{LIST_A}')::int AS "arc__fe", (l.subcategory = '{LIST_B}')::int AS "arc__ts"
FROM sel JOIN listings_masked l USING (site, ad_id) LEFT JOIN k USING (site, ad_id)
GROUP BY l.ad_id, l.subcategory"""
# the edge set of the ad-level graph, once: the panel draws it, and the hidden edges_drawn variable counts
# it for the title (one number never gets a panel of its own: operator rule)
_AD_EDGE_SET = """
WITH sel AS (SELECT DISTINCT a.site, a.ad_id FROM advertisers_v2 a
             WHERE NOT '__all' IN (${advertiser:sqlstring})
               AND (a.operator_id IN (${advertiser:sqlstring}) OR a.persona_id IN (${advertiser:sqlstring}))),
e AS (SELECT k.ad_a, k.ad_b, array_agg(DISTINCT k.link_kind ORDER BY k.link_kind) AS kinds
      FROM advertiser_v2_links k JOIN sel x ON x.site = k.site AND x.ad_id = k.ad_a
      JOIN sel y ON y.site = k.site AND y.ad_id = k.ad_b GROUP BY 1, 2
      HAVING count(DISTINCT k.link_kind) >= ${min_kinds:raw})"""
AD_GRAPH_EDGES = (
    _AD_EDGE_SET
    + """
SELECT ad_a || '>' || ad_b AS id, ad_a AS source, ad_b AS target,
       replace(array_to_string(kinds, '+'), 'image', 'shared images') AS mainstat, cardinality(kinds) AS thickness
FROM e"""
)
# the graph's title variable: an edge count, or what to pick while advertiser = All (the graph is
# empty by design then, and a bare "0 edges" read as broken; listy's check 2026-11-13)
AD_GRAPH_EDGE_COUNT = (
    _AD_EDGE_SET
    + """
SELECT CASE WHEN '__all' IN (${advertiser:sqlstring}) THEN 'pick one advertiser above'
            ELSE count(*) || ' edges' END FROM e"""
)

# G3.Q5 (GQM: NEW): which point li uses per ad. Gold ads are one named bucket, never merged into "none"
# (C19); the decider tier follows the precedence in place_apply (operator > machine > derive > bare pin).
_GOLD_FIRST = "CASE WHEN l.gold_masked THEN 'gold (masked)' "
POINT_ROUTE_SQL = f"""
SELECT {_GOLD_FIRST}WHEN l.geocode_decision IS NULL THEN 'no precise text point' ELSE l.geocode_decision END AS decision,
       count(*) FILTER (WHERE l.subcategory = '{LIST_A}')::int AS "{SHORT_A}",
       count(*) FILTER (WHERE l.subcategory = '{LIST_B}')::int AS "{SHORT_B}"
FROM listings_masked l WHERE {model.text_ok()} GROUP BY 1 ORDER BY count(*) DESC"""
POINT_LEDGER_SQL = f"""
SELECT {_GOLD_FIRST}WHEN l.place_decided_by = 'operator' THEN '1 manual (operator)'
            WHEN l.place_decided_by LIKE 'place-check@%' THEN '2 machine (place-check)'
            WHEN l.place_decided_by IS NOT NULL THEN '3 derive'
            WHEN l.pin_lat IS NOT NULL THEN '4 bare pin' ELSE '5 none' END AS decider,
       coalesce(l.place_decision, '–') AS decision, coalesce(l.place_decision_status, '–') AS status,
       CASE WHEN l.gold_masked THEN '–' WHEN l.place_point_source IS NULL THEN 'none'
            WHEN l.place_lat IS NULL THEN 'expired (TTL)' ELSE l.place_point_source END AS point,
       count(*)::int AS ads
FROM listings_masked l WHERE {model.pin_ok()} AND {model.text_ok()} GROUP BY 1, 2, 3, 4 ORDER BY 1, ads DESC"""

# D2 (2026-11-13): persona nodes are SHOWN as an upper bound until the B3 blind read passes 0.9
PERSONA_GATE = f"{model.PERSONA_DRAFT}: a persona is an upper bound until the B3 blind read passes 0.9."

# Services, pre-freeze (analyst, 2026-11-08): the eval half is ABSENT from services_ads, so the depth
# weights are renormalised within each subcategory × decile over the depth ads that ARE there:
# w = N_h / n_h(non-eval), N_h = the run's own strata.population. A cell under 5 non-eval ads pools with
# the adjacent decile of the same subcategory (d0 → d1, else d → d−1; N and n summed). After the freeze
# services_ads holds every ad and this falls back to the original weights — no code change.
SERVICES_W = f"""
WITH dr AS (SELECT r.run_id, r.sample::jsonb AS sample FROM runs r WHERE r.run_id = {DEPTH_RUN}),
design AS (SELECT x ->> 'subcategory' AS subcategory, (x ->> 'decile')::int AS decile,
                  (x ->> 'population')::numeric AS n_pop
           FROM dr, jsonb_array_elements(dr.sample -> 'strata') x),
ne AS (SELECT s.subcategory, s.decile, count(*) AS n_ne
       FROM samples s JOIN dr USING (run_id) JOIN services_ads a USING (site, ad_id) GROUP BY 1, 2),
m AS (SELECT d.subcategory, d.decile, d.n_pop, coalesce(ne.n_ne, 0) AS n_ne,
             CASE WHEN coalesce(ne.n_ne, 0) < 5 THEN CASE WHEN d.decile = 0 THEN 1 ELSE d.decile - 1 END
                  ELSE d.decile END AS k
      FROM design d LEFT JOIN ne USING (subcategory, decile)),
g AS (SELECT subcategory, k, sum(n_pop) AS n_pop, sum(n_ne) AS n_ne FROM m GROUP BY 1, 2),
sw AS (SELECT s.site, s.ad_id, s.subcategory, (g.n_pop / g.n_ne)::float AS w
       FROM samples s JOIN dr USING (run_id) JOIN services_ads a USING (site, ad_id)
       JOIN m USING (subcategory, decile) JOIN g USING (subcategory, k))"""
SERVICES_NOTE = (
    "Until the eval labels are frozen, the ${svc_eval_n} eval-half ads and the ${svc_mates_n} ads sharing a "
    "current operator with one are excluded, and depth weights are renormalised within each subcategory × "
    "decile over the remaining ${svc_depth_n} of ${svc_depth_total} depth ads (wider intervals). "
    "Neither exclusion shows an association with depth (permutation tests): persona-permutation χ² "
    "p = 0.38 FE, 0.45 TS for the eval half; eval half plus operator-mates, permuted by operator, "
    "p = 0.63 FE, 0.59 TS. "
    "Vocab v${svc_vocab} (${svc_method}), provisional until the review revises it. "
    "Page-declared online-service codes are not used: every ad that has any carries the same three."
)

ABOUT = f"""\
### What this is
- **One listing-lens snapshot**, not a time series: the list cards of the crawls in `runs` ({PFX}-process),
  each card seen **once**. Counts live in the panels, never in this text: they move with each export.
- **listing-lens-fetched ads**: a depth-stratified sample (half FE, half TS, with design weights) plus the
  ads opened by both lanes, joined onto the cards by `ad_id`. A separate **phone-session import**
  overlaps on few ads and is badged wherever it appears.
- Read-only copy in Postgres (`{DS_UID}`), loaded by `llens export-db`; the loader version and build
  time are in its `meta` table. Refresh is **OFF** (as of 2026-11-13).

### What is NOT in it
- **No per-ad calendar series.** A fetched ad has at most two live sightings
  (`ad_observation_span`): enough for first/last seen and gone, not a series (as of 2026-11-13).
  Age, list depth and `ad_id` stay the main time axes; the time panels are {PFX}-content's G6 tab.
- **Price:** the list-card rate (`observations.rate`, where the card shows one) feeds {PFX}-content's
  G7 Content tab, Price row; the ad page's own price is not mapped: no other price column is in this
  export (as of 2026-11-13). **Services** are matched against a vocabulary ({PFX}-content, Services row): provisional, and the eval half is held out until its
  labels are frozen (as of 2026-11-13).
- `bumped_at` is empty on every list **card** (lists carry no absolute bump time); **ad pages**
  carry it. Coverage per field: the "Field coverage" table (tab G2 Sample and coverage).

### No photos on these boards
- Photos stay **plain-text URLs**. The stock image routes (Geomap Photos layer, Table Image cell)
  load images with the page's default referrer policy, so each request would send this Grafana's
  origin to the site as a Referer. No stock setting changes that, so no image is fetched at all.
- Ad **links** open the ad in a new tab with `rel=noreferrer` (no Referer is sent).

### Privacy
- This folder is **Admin-only**. No public dashboard, snapshot or share link — ever.
"""


# the ad page's own price is not parsed; the list-card rate (observations.rate) is the price li has
PRICE_AD_PAGE, PRICE_AD_PAGE_NOTE = "price (ad page)", "not mapped; card price = rate"

NOTES = {
    "bumped_at": "'NULL by design: list pages carry no absolute bump time'",
    "rate": f"'the list-card rate (bare number, no unit on the site); feeds {PFX}-content G7 Price'",
    "geocode (precise)": "'lat/lon NULL past the 30-day cache rule; attempted = geocode_status'",
}


# ---------------------------------------------------------------- out-of-region toggle (operator rule, 2026-11-08)
# One live line per board, counted by derive's own enum (location_invalid_reason), never recomputed here:
# in the board's KPI strip when it has one, else in the ⓘ of its first location panel (via the hidden
# variable OOR_LINE — a description is static text, Grafana interpolates ${var} in it). No new stat panel.
OOR_LINE = "oor_line"
_REASONS = (
    ("pin", "out_of_region_pin"),
    ("text", "out_of_region_text"),
    ("both", "both"),
    ("unresolvable", "unresolvable"),
)


def _oor_counts(table: str) -> str:
    return " || ' · ' || ".join(
        f"'{k} ' || (SELECT count(*) FROM {table} WHERE location_invalid_reason = '{r}')" for k, r in _REASONS
    )


def oor_line_sql(*, phone: bool = False) -> str:
    """'excluded: out-of-region — pin N · text N · both N · unresolvable N' over the li ads (+ the phone import)."""
    head = "CASE WHEN '${out_of_region:raw}' = 'show' THEN 'shown (grey on maps)' ELSE 'excluded' END"
    tail = f" || ' (phone import: ' || {_oor_counts('import_phone_ads')} || ')'" if phone else ""
    return f"SELECT {head} || ': out-of-region — ' || {_oor_counts('listings_masked')}{tail} AS \"out-of-region\""


def oor_line_var(*, phone: bool = False) -> dict:
    # skipUrlSync: a derived count has no business in a shared link
    return query_var(OOR_LINE, oor_line_sql(phone=phone), multi=False, include_all=False) | {
        "hide": 2,
        "skipUrlSync": True,
    }


def oor_strip_sql() -> str:
    """The same counts as NUMBERS for a KPI strip: a stat sizes every value in it with ONE font, so the long
    line shrank the whole strip to unreadable (drawn-content sweep 2026-11-08, the overview board)."""
    cols = ", ".join(
        f"count(*) FILTER (WHERE location_invalid_reason = '{r}')::int AS \"out-of-region · {k}\""
        for k, r in _REASONS
    )
    return f"SELECT {cols} FROM listings_masked"


OOR_NOTE = (
    "Out-of-region points (derive's region rule: > 60 km from the ad's own label centre) are excluded by "
    "default; the out-of-region variable shows them (maps: a separate grey layer, the reason in the tooltip). "
    "${oor_line}."
)
GREY = "#8E8E8E"


def grey_layer(ref: str) -> dict:
    """Flagged points on their own grey layer; its query returns rows only when out_of_region = show."""
    lyr = _markers("Out of region (grey)", ref, size=6)
    lyr["config"]["style"]["color"] = {"fixed": GREY}
    return lyr


def overview() -> dict:
    coverage_fields = [
        ("observations", "rate", "rate"),
        ("observations", "bumped_at", "bumped_at"),
        ("observations", "card_age", "card_age"),
        ("observations", "card_ethnicity", "card_ethnicity"),
        # a flagged (out-of-region) point counts as absent unless the board shows it: the ad stays a row
        ("listings_masked", "pin", f"CASE WHEN {pin_ok(None)} THEN pin_lat END"),
        ("listings_masked", "text_hint", f"CASE WHEN {text_ok(None)} THEN text_hint_raw END"),
        ("listings_masked", "geocode (attempted)", "geocode_status"),
        (
            "listings_masked",
            "geocode (precise)",
            f"CASE WHEN {text_ok(None)} THEN NULLIF(geocode_precise, false) END",
        ),
        ("listings_masked", "whatsapp", "whatsapp"),
        ("listings_masked", "hair", "hair"),
        ("listings_masked", "eye", "eye"),
        ("listings_masked", "tagline", "tagline"),
        ("listings_masked", "services", "services"),
        ("listings_masked", "first_published", "first_published"),
        ("listings_masked", "bumped_at", "bumped_at"),
        ("listings_masked", "phone_masked", "phone_masked"),
        ("listings_masked", "email_masked", "email_masked"),
        ("listings_masked", "website", "website"),
        ("listings_masked", "age", "age"),
        ("listings_masked", "ethnicity", "ethnicity"),
        ("listings_masked", "languages", "languages"),
        ("listings_masked", "height", "height"),
        ("listings_masked", "body_stats", "body_stats"),
        ("listings_masked", "media_urls (≥1)", "NULLIF(n_media, 0)"),
        ("listings_masked", "advertiser_id", "advertiser_id"),
    ]
    cov = "\nUNION ALL ".join(
        f"SELECT '{t}' AS tbl, '{f}' AS field, avg((({e}) IS NOT NULL)::int)::float AS coverage, "
        f"count(DISTINCT {e})::int AS distinct_n, {NOTES.get(f, 'NULL')}::text AS note FROM {t}"
        for t, f, e in coverage_fields
    )
    parser = "\nUNION ALL ".join(
        f"SELECT 'listings_masked', '{f}', 0.0, 0, '{PRICE_AD_PAGE_NOTE}'" for f in (PRICE_AD_PAGE,)
    )
    cov_sql = f"SELECT * FROM (\n{cov}\nUNION ALL {parser}\n) c ORDER BY coverage, tbl, field"

    pop_sql = f"""
WITH d AS (
  SELECT o.site, o.ad_id, o.list_subcategory AS subcategory, o.list_sample_weight AS w, o.depth_decile
  FROM observations o WHERE {in_filter("o.list_subcategory", "subcategory")})
SELECT d.depth_decile::text AS decile, d.subcategory,
       sum(d.w) AS est_population,
       count(ds.ad_id) AS sampled,
       sum(d.w) / nullif(count(ds.ad_id), 0) AS design_weight
FROM d LEFT JOIN {DEPTH_SAMPLE} ds USING (site, ad_id)
GROUP BY 1, 2 ORDER BY 2, 1"""

    wvr_sql = f"""
WITH s AS (
  SELECT CASE WHEN {pin_ok()} THEN l.pin_lat END AS pin_lat,
         CASE WHEN {text_ok()} THEN l.text_hint_raw END AS text_hint_raw,
         o.list_subcategory, o.depth_decile, ds.weight AS dw
  FROM listings_masked l
  JOIN observations o USING (site, ad_id)
  LEFT JOIN {DEPTH_SAMPLE} ds USING (site, ad_id)
  WHERE {in_filter("o.list_subcategory", "subcategory")})
SELECT avg((pin_lat IS NOT NULL)::int)::float AS "pin · raw",
       (sum(dw * (pin_lat IS NOT NULL)::int) / sum(dw))::float AS "pin · weighted",
       avg((text_hint_raw IS NOT NULL)::int)::float AS "text · raw",
       (sum(dw * (text_hint_raw IS NOT NULL)::int) / sum(dw))::float AS "text · weighted"
FROM s"""

    # the geocode cache itself (query → point) is not granted to the board role (export-db@21, D8): its
    # one-row aggregate view is
    geo_sql = """
SELECT queries, places, precise_share,
       (extract(epoch FROM (min_expires_at - now())) / 86400)::float AS days_to_expiry
FROM geocode_cache_summary"""

    pct = {
        "defaults": {"unit": "percentunit", "decimals": 1, "min": 0, "max": 1},
        # the out-of-region counts in the same strip are counts, not shares
        "overrides": [
            {
                "matcher": {"id": "byRegexp", "options": "/^out-of-region/"},
                "properties": [
                    {"id": "unit", "value": "none"},
                    {"id": "decimals", "value": 0},
                ],
            }
        ],
    }
    return dashboard(
        f"{PFX}-overview",
        "Overview and About",
        [
            markdown("About this data (read first)", ABOUT, h=16),
            markdown("Candidate hypotheses H7–H14", "(filled at build time)", h=9),
            row("Findings H1–H6"),
            panel(
                "table",
                "Findings register — every version, latest first",
                """SELECT id, version, status, tested_at, n, scope, result, weakest_assumption, next_test
FROM findings ORDER BY id, version DESC""",
                w=24,
                h=10,
                description=(
                    "Every version of every finding (H1–H6), newest first within each id; the first row of "
                    "an id is its current status. A status timeline was dropped: its versions span hours, "
                    "so on the board's 2020→now range it drew nothing (2026-11-08)."
                ),
                options={"cellHeight": "md"},
            ),
            row("Coverage and sampling"),
            panel(
                "table",
                "Field coverage (holes first)",
                cov_sql,
                w=12,
                h=14,
                fieldConfig={
                    "defaults": {},
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "coverage"},
                            "properties": [
                                {"id": "unit", "value": "percentunit"},
                                {"id": "min", "value": 0},
                                {"id": "max", "value": 1},
                                {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "lcd"}},
                                {
                                    "id": "thresholds",
                                    "value": {
                                        "mode": "absolute",
                                        "steps": [
                                            {"color": "red", "value": None},
                                            {"color": "orange", "value": 0.2},
                                            {"color": "green", "value": 0.5},
                                        ],
                                    },
                                },
                            ],
                        }
                    ],
                },
            ),
            panel(
                "barchart",
                "Population vs sample by depth decile — [depth sample]; depth ≈ days since last bump; FE stride vs TS census.",
                pop_sql,
                w=12,
                h=14,
                description="est_population = Σ list_sample_weight (all cards); sampled = [depth sample] ads in the decile. "
                "Every weighted panel elsewhere is read against this.",
                options={"xField": "decile", "groupWidth": 0.7, "stacking": "none"},
            ),
            panel(
                "stat",
                "Pin and text-location rates — raw = every listing-lens-fetched ad; weighted = [depth sample] design weights",
                [wvr_sql, oor_strip_sql()],
                w=12,
                h=6,
                description=OOR_NOTE.replace(" ${oor_line}.", ""),
                fieldConfig=pct,
                options={"colorMode": "none", "graphMode": "none", "textMode": "value_and_name"},
            ),
            panel(
                "stat",
                "Geocode cache — re-resolve countdown (30-day ToS)",
                geo_sql,
                w=12,
                h=6,
                description="days_to_expiry is computed with now() on load: no refresh, no alert rule.",
                options={"colorMode": "background", "graphMode": "none", "textMode": "value_and_name"},
                fieldConfig={
                    "defaults": {},
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "precise_share"},
                            "properties": [{"id": "unit", "value": "percentunit"}],
                        },
                        {
                            "matcher": {"id": "byName", "options": "days_to_expiry"},
                            "properties": [
                                {"id": "unit", "value": "d"},
                                {"id": "decimals", "value": 1},
                                {
                                    "id": "thresholds",
                                    "value": {
                                        "mode": "absolute",
                                        "steps": [
                                            {"color": "red", "value": None},
                                            {"color": "orange", "value": 0},
                                            {"color": "green", "value": 7},
                                        ],
                                    },
                                },
                            ],
                        },
                    ],
                },
            ),
        ],
        variables=[subcategory_var(), out_of_region_var()],
        links=[f"{PFX}-explorer", f"{PFX}-geo", f"{PFX}-process", f"{PFX}-content", f"{PFX}-advertiser"],
        description="Entry board: what the listing-lens snapshot is, what it is not, where H1–H6 stand.",
    )


TABLE_CAP = 5000  # rows a list table fetches (the stock table virtualises rows); tf plan refuses when a /* cap */ query returns it


def explorer() -> dict:
    flt = " AND ".join(
        [
            in_filter("o.list_subcategory", "subcategory"),
            in_filter("l.city_label", "city_label"),
            in_filter("coalesce(l.ethnicity, o.card_ethnicity)", "ethnicity"),
            in_filter("o.verified", "verified"),
            ADVERTISER_FILTER,
            SERVICE_FILTER,
            SERVICE_GROUP_FILTER,
        ]
    )
    # F2/F2b (domain review): filtered to a PERSONA, the table drops every coordinate and place column and
    # shows dates to the month only, so no route can be rebuilt as a sorted table (C17, C20)
    lp = lambda expr: f"CASE WHEN {PERSONA_SELECTED} THEN NULL ELSE {expr} END"  # noqa: E731
    table_sql = f"""
SELECT l.ad_id, l.advertiser_id, o.list_subcategory AS subcategory, l.city_label, l.title,
       sv.services, sv.service_groups, sv.refused_services,
       -- the ad page's own service_mode (2891 of 2944 ads set it, 2026-11-13) first; the incall/outcall
       -- service concepts (788 ads) only where service_mode is empty
       CASE WHEN jsonb_array_length(coalesce(l.service_mode, '[]'::jsonb)) > 0 THEN
              CASE WHEN l.service_mode @> '["{INCALL}", "{OUTCALL}"]' THEN 'both'
                   WHEN l.service_mode @> '["{INCALL}"]' THEN '{INCALL}'
                   WHEN l.service_mode @> '["{OUTCALL}"]' THEN '{OUTCALL}' END
            WHEN sv.incall AND sv.outcall THEN 'both' WHEN sv.incall THEN '{INCALL}' WHEN sv.outcall THEN '{OUTCALL}'
       END AS mode,
       (SELECT string_agg(x, ', ' ORDER BY x) FROM jsonb_array_elements_text(l.languages) AS x) AS languages,
       -- the SAME point as pin_lat/pin_lon (persona blanking, the region rule): NULL wherever they are NULL
       {lp(f"CASE WHEN {pin_ok()} AND l.pin_lat IS NOT NULL THEN round((ST_Distance(vpt.geog, l.pin_geog) / 1000.0)::numeric, 1) END")}
         AS km_from_viewpoint,
       l.source_url AS ad_url,
       o.rate, coalesce(l.age, o.card_age) AS stated_age, coalesce(l.ethnicity, o.card_ethnicity) AS ethnicity,
       o.verified,
       CASE WHEN {PERSONA_SELECTED} THEN date_trunc('month', l.first_published) ELSE l.first_published END AS first_published,
       {lp("(extract(epoch FROM (l.fetched_at - l.first_published)) / 86400)::int")} AS age_days,
       o.list_page, o.list_page_count, o.list_position,
       {lp(f"CASE WHEN {pin_ok()} THEN l.pin_lat END")} AS pin_lat, {lp(f"CASE WHEN {pin_ok()} THEN l.pin_lon END")} AS pin_lon,
       l.text_hint_kind, {lp("l.text_hint_raw")} AS text_hint_raw,
       {lp(f"CASE WHEN {text_ok()} THEN l.geocode_formatted END")} AS geocode_formatted,
       CASE WHEN {text_ok()} THEN l.geocode_precise END AS geocode_precise, l.location_invalid_reason,
       l.phone_masked, l.email_masked, l.website, l.n_media, l.media_urls::text AS media_urls,
       left(l.description, 160) AS description
FROM listings_masked l JOIN observations o USING (site, ad_id)
-- the services step's OWN polarity (services_ad_concepts.offered: a negated mention, "no greek", is refused,
-- not offered) and its own eval rule (eval ads are absent from services_*), so no row = empty, never 0
LEFT JOIN LATERAL (
  SELECT string_agg(x.label, ', ' ORDER BY x.so) FILTER (WHERE x.offered) AS services,
         string_agg(DISTINCT x.grp, ', ') FILTER (WHERE x.offered) AS service_groups,
         string_agg(x.label, ', ' ORDER BY x.so) FILTER (WHERE x.refused) AS refused_services,
         bool_or(x.offered AND x.concept = '{INCALL}') AS incall, bool_or(x.offered AND x.concept = '{OUTCALL}') AS outcall
  FROM (SELECT ac.concept, ac.offered, ac.refused, c.pref_label AS label, c.sort_order AS so,
               coalesce(b.pref_label, c.pref_label) AS grp
        FROM services_ad_concepts ac JOIN services_concepts c USING (concept)
        LEFT JOIN services_concepts b ON b.concept = c.broader
        WHERE ac.site = l.site AND ac.ad_id = l.ad_id AND (ac.offered OR ac.refused)) x) sv ON true
LEFT JOIN {VP} vpt ON true
WHERE {flt}
ORDER BY l.ad_id DESC
LIMIT {TABLE_CAP} /* cap */"""  # one fixed cap; sort and filter in the stock table header (reuse audit #2–#3)
    one = "l.ad_id = ${ad_id:sqlstring}"
    dossier_sql = f"""
SELECT f.key AS field, f.value
FROM listings_masked l
JOIN observations o USING (site, ad_id),
LATERAL jsonb_each_text(
  to_jsonb(l) - 'media_urls'
  || jsonb_build_object(
       'age_days', round((extract(epoch FROM (l.fetched_at - l.first_published)) / 86400)::numeric, 1),
       'card.rate', o.rate, 'card.verified', o.verified, 'card.list_page', o.list_page,
       'card.list_page_count', o.list_page_count, 'card.list_position', o.list_position,
       'card.card_hash', o.card_hash, 'card.sponsored', o.sponsored,
       '{PRICE_AD_PAGE}', '{PRICE_AD_PAGE_NOTE}')
) AS f
WHERE {one}
ORDER BY f.key"""

    media_sql = f"""
SELECT u.n AS "#", u.url
FROM listings_masked l, LATERAL jsonb_array_elements_text(l.media_urls) WITH ORDINALITY AS u(url, n)
WHERE {one}
ORDER BY u.n"""

    shared_sql = f"""
WITH m AS (
  SELECT l.site, l.ad_id, l.advertiser_id, l.age, l.ethnicity, l.city_label, u.url
  FROM listings_masked l
  JOIN observations o USING (site, ad_id)
  CROSS JOIN LATERAL jsonb_array_elements_text(l.media_urls) AS u(url)
  WHERE {in_filter("o.list_subcategory", "subcategory")})
SELECT url,
       count(DISTINCT ad_id)::int AS n_ads,
       count(DISTINCT advertiser_id)::int AS n_advertisers,
       string_agg(DISTINCT age::text, ', ') AS ages,
       string_agg(DISTINCT ethnicity, ', ') AS ethnicities,
       string_agg(DISTINCT city_label, ', ') AS cities,
       string_agg(DISTINCT ad_id, ', ') AS ad_ids
FROM m GROUP BY url HAVING count(DISTINCT ad_id) >= 2
ORDER BY n_advertisers DESC, n_ads DESC LIMIT {TABLE_CAP} /* cap */"""

    self_link = {
        "title": "Open this ad's dossier",
        "url": f"/d/{PFX}-explorer?var-ad_id=${{__data.fields.ad_id}}&${{subcategory:queryparam}}",
    }
    gate = (
        "Photos are shown as **plain-text URLs** until the browser media gate passes "
        "(every image straight from the media host, exact URL, no Referer, nothing via Grafana)."
    )
    return dashboard(
        f"{PFX}-explorer",
        "Explorer (ad and advertiser drill-down)",
        [
            markdown(
                "How to use",
                f"Pick an ad in the table (row link) or set **ad_id** above. {gate} The table holds the ads listing-lens "
                "fetched (`listings_masked`); the phone-session-only ads in `ads` have no listing-lens page and are not here.",
                w=16,
                h=3,
            ),
            panel(
                "table",
                "Ads — every field (sort by any column header; filter per column with the funnel icon)",
                table_sql,
                w=24,
                h=12,
                options={
                    "cellHeight": "sm",
                    "showHeader": True,
                    "sortBy": [{"displayName": "ad_id", "desc": True}],
                },
                description=(
                    "The footer counts the rows shown: the listing-lens-fetched ads (listings_masked), up to the "
                    "query cap of "
                    + str(TABLE_CAP)
                    + ". A footer below the cap means nothing is cut. Phone-only imports in `ads` have no "
                    "listing-lens page, so they are not rows here. A concept the ad both offers and refuses is in services AND "
                    "refused_services. mode = the ad's service_mode, else its incall/outcall services. "
                    "km_from_viewpoint is empty wherever pin_lat is. The group filter includes all sub-groups (the service_groups "
                    "column shows the immediate group). services empty = none offered in the text, or the ad is "
                    "held out for the services evaluation (333 ads as of 2026-11-13). An out-of-region point is "
                    "blanked, never its ad: " + OOR_NOTE
                ),
                fieldConfig={
                    "defaults": {"custom": {"filterable": True, "footer": {"reducers": ["countAll"]}}},
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "ad_id"},
                            "properties": [{"id": "links", "value": [self_link]}],
                        },
                        ad_link("ad_url", "ad_url"),
                        {  # SQL rounds to 1 decimal; the table's auto-decimals padded 1.1 to "1.10"
                            "matcher": {"id": "byName", "options": "km_from_viewpoint"},
                            "properties": [{"id": "decimals", "value": 1}],
                        },
                        {
                            "matcher": {"id": "byName", "options": "advertiser_id"},
                            "properties": [
                                {
                                    "id": "links",
                                    "value": [
                                        {
                                            "title": "Filter to this advertiser",
                                            "url": f"/d/{PFX}-explorer?var-advertiser=${{__data.fields.advertiser_id}}",
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                },
            ),
            row("Dossier — one ad (${ad_id})"),
            panel(
                "table",
                "Every field of the selected ad",
                dossier_sql,
                expect_rows=("A",),
                w=14,
                h=18,
                options={"cellHeight": "sm"},
                fieldConfig={
                    "defaults": {
                        "custom": {"inspect": True, "cellOptions": {"type": "auto", "wrapText": True}}
                    },
                    "overrides": [],
                },
            ),
            panel(
                "table",
                "Photo URLs of the selected ad (text only — not fetched)",
                media_sql,
                w=10,
                h=18,
                options={"cellHeight": "sm"},
            ),
            row("Shared photos (exact-URL reuse)"),
            panel(
                "table",
                "Media URLs on ≥ 2 ads — A lower bound: re-uploads need hashing, needs=second-crawl.",
                shared_sql,
                w=24,
                h=10,
                options={"cellHeight": "sm"},
                transformations=min_filter("n_ads", 2),
            ),
            panel(
                "nodeGraph",
                "Ad-level graph: ${edges_drawn}, ≥ ${min_kinds} kinds",
                [AD_GRAPH_NODES, AD_GRAPH_EDGES],
                w=24,
                h=14,
                description="The ads of ONE ad cluster or persona (pick it in advertiser); min link kinds hides edges "
                "joined by fewer kinds, and the title counts the edges drawn. Nodes are the selected ad cluster's (or persona's) ads: an opaque ad id, the number of "
                'shared-key kinds that touch it, FE/TS. Edges are the kinds that joined two ads ("shared images" '
                "for image); never a key value, title, city, phone or date (C21). Empty while advertiser = All.",
                fieldConfig={"defaults": {"links": [explorer_link("id")]}, "overrides": []},
            ),
        ],
        variables=[
            custom_var("min_kinds", ["1", "2", "3"], label="min link kinds per edge"),
            query_var("edges_drawn", AD_GRAPH_EDGE_COUNT, multi=False, include_all=False) | {"hide": 2},
            query_var(
                "ad_id",
                "SELECT ad_id FROM listings_masked ORDER BY ad_id DESC",
                multi=False,
                include_all=False,
            ),
            query_var(
                "advertiser",
                # v1 advertiser ids, v2 ad-cluster ids (v2:…) and v2 persona ids (p2:…): the network's
                # node clicks land here with either v2 id
                "SELECT DISTINCT advertiser_id FROM listings_masked WHERE advertiser_id IS NOT NULL "
                "UNION SELECT DISTINCT operator_id FROM advertisers_v2 UNION SELECT DISTINCT persona_id FROM advertisers_v2 "
                "ORDER BY 1",
            ),
            subcategory_var(),
            query_var(
                "city_label",
                "SELECT DISTINCT city_label FROM listings_masked WHERE city_label IS NOT NULL ORDER BY 1",
            ),
            query_var("viewpoint", "SELECT label FROM viewpoint ORDER BY 1", multi=False, include_all=False),
            query_var("service", SERVICE_VAR_SQL, label="service (offered)"),
            query_var("service_group", SERVICE_GROUP_VAR_SQL, label="service group"),
            query_var(
                "ethnicity",
                "SELECT DISTINCT coalesce(l.ethnicity, o.card_ethnicity) FROM listings_masked l JOIN observations o USING (site, ad_id) WHERE coalesce(l.ethnicity, o.card_ethnicity) IS NOT NULL ORDER BY 1",
            ),
            out_of_region_var(),
            oor_line_var(),
            custom_var("verified", ["true", "false"])
            | {
                "multi": True,
                "includeAll": True,
                "allValue": model.ALL,
                "current": {"text": "All", "value": "$__all"},
            },
        ],
        links=[f"{PFX}-overview", f"{PFX}-geo", f"{PFX}-advertiser", f"{PFX}-content"],
        description="The pivot board: every lens links here with var-ad_id / var-advertiser.",
    )


BOARDS = [overview, explorer]


# candidate hypotheses from the analyst's vetting: (id, claim, board uid, title prefix of the evidence panel)
CANDIDATES = [
    (
        "H7",
        "A photo / upload id is an account key: the same media URL links ads the clustering splits",
        f"{PFX}-advertiser",
        "Single-kind direct edges",
    ),
    (
        "H8",
        "List depth IS bump staleness (Spearman ρ 0.988 FE / 0.987 TS; ≈ 0.65 with age)",
        f"{PFX}-content",
        "H8 candidate",
    ),
    (
        "H9",
        "A far pin (> 60 km from the label centre) is the account's location, not the work place",
        f"{PFX}-geo",
        "Far pins per advertiser",
    ),
    (
        "H10",
        "Shared venues: several advertisers name the same place",
        f"{PFX}-advertiser",
        "Venues named by advertisers",
    ),
    (
        "H12",
        "Scheduled day-1 bumps at whole-hour lags (61% aligned at 0–24 h vs ≈ 7% chance after 7 d)",
        f"{PFX}-content",
        "H12 candidate: whole-hour",
    ),
    (
        "H13",
        "FE's $160 mode is the hourly figure of an '80 per half-hour' tier (mostly incall-only)",
        f"{PFX}-content",
        "H13 candidate",
    ),
    (
        "H14",
        "Stale copy: 'new / just arrived' ads are OLDER (median 218 d vs 167 d)",
        f"{PFX}-content",
        "H14 candidate",
    ),
]


def _panel_id(board: dict, prefix: str) -> int:
    hits = [p["id"] for p in board["panels"] if p.get("title", "").startswith(prefix)]
    if len(hits) != 1:
        raise ValueError(f"{board['uid']}: {len(hits)} panels titled {prefix!r}")
    return hits[0]


def _with_candidates(built: list[dict]) -> list[dict]:
    by_uid = {b["uid"]: b for b in built}
    lines = [
        "Vetted by the analyst; each links to the panel that suggests it. **Candidates, not findings.**",
        "",
    ]
    for hid, claim, uid, prefix in CANDIDATES:
        lines.append(
            f"- **{hid}** — {claim} → [{uid}](/d/{uid}?viewPanel=panel-{_panel_id(by_uid[uid], prefix)})"
        )
    for p in [q for top in by_uid[f"{PFX}-overview"]["panels"] for q in [top, *top.get("panels", [])]]:
        if p.get("title") == "Candidate hypotheses H7–H14":
            p["options"]["content"] = "\n".join(lines)
    return built


# A panel earns its area (operator rule, 2026-11-08): one or two numbers never get their own panel. Long text
# goes into ONE collapsed Notes row; small stats fold into a companion chart's description (ⓘ) or into at
# most ONE compact KPI strip per board. Titles are matched by prefix.
FOLDS = {
    f"{PFX}-overview": {
        "kpi": ["Pin and text-location rates"],
        "move_to": {"Geocode cache": f"{PFX}-process"},
    },
    f"{PFX}-explorer": {"describe": {"How to use": "Ads"}},
    f"{PFX}-geo": {
        "widen": ["H3: pin-to-centre"],
        # GQM verdict DELETE: the box rule is superseded by the region test; the node graph's question
        # (G4.Q2) is served by "Hints by kind"
        "drop": ["Outside the region box", "Hint kind → geocode precision"],
    },
    f"{PFX}-process": {
        "describe": {"What the ladder cannot see": "Recorded request counters"},
        "widen": ["Recorded request counters"],
    },
    f"{PFX}-time": {"notes": ["Read first"], "kpi": ["Clock calibration"], "widen": ["T02: ad_id"]},
    f"{PFX}-content": {
        "notes": ["Read first"],
        "widen": ["T2"],
        # GQM verdict DELETE, operator decision D4 (2026-11-13): a price series from a cross-section (P3),
        # a label-centre rate map (P5, the label is not a location: H2 refuted), two discovery panels with
        # no question (T1, T5)
        "drop": [
            "P3 — rate vs publish date",
            "P5: median rate",
            "T1: distinctive bigrams",
            "T5 — top card-title",
        ],
    },
    f"{PFX}-advertiser": {
        "notes": ["Read first", "How to read advertisers@2"],
        "kpi": [
            "Pin pairs —",
            "H4 power",
            "Advertiser pairs joined",
            "Operators and",
            "Pin pairs within a persona",
            "Masked contact phone",
        ],
        "kpi_title": "Key counts (v2, personas: draft)",
        # 21 values in one h = 4 strip auto-sized every name below readable (operator, 2026-11-13): two
        # rows' height and fixed sizes instead of Grafana's auto-fit
        "kpi_h": 8,
        "kpi_text": {"titleSize": 14, "valueSize": 28},
        # the strip already holds 21 values: 4 more shrank every name below readable (sweep 2026-11-08), so
        # the out-of-region line rides in the strip's own ⓘ
        "kpi_note": (
            "Labels: v1.1 / v2 = the advertiser method; max = an upper bound (personas are a draft); "
            "phone4 = the last 4 digits of the masked contact phone; m = advertiser × phone4 combinations "
            "(the null model C(m,2)/10⁴ gives null pairs (chance)).\n\n" + OOR_NOTE
        ),
        # GQM verdict DELETE: the per-persona pin route is a movement trace of one person (C20, a BLOCK);
        # card-hash families answer no question (D4, operator decision 2026-11-13)
        "drop": ["Pin route in publish order", "Card-hash families"],
        "widen": ["Pin pairs within an advertiser", "Card-hash families"],
    },
}


def _match(panels: list[dict], prefix: str) -> list[dict]:
    return [p for p in panels if p.get("raw_title", p.get("title", "")).startswith(prefix)]


def _kpi_strip(stats: list[dict], title: str) -> dict:
    targets, overrides = [], []
    for n, st in enumerate(stats, start=1):
        for t in st["targets"]:
            targets.append({**t, "refId": f"K{n}{t['refId']}"})
        overrides += (st.get("fieldConfig") or {}).get("overrides", [])
    desc = " · ".join(x for x in (st.get("description", "") for st in stats) if x)
    return {
        "type": "stat",
        "title": title,
        "description": desc,
        "datasource": model.DS,
        "targets": targets,
        "gridPos": {"w": 24, "h": 4},
        "options": {
            "colorMode": "none",
            "graphMode": "none",
            "textMode": "value_and_name",
            "justifyMode": "auto",
            "reduceOptions": {"values": False, "calcs": ["lastNotNull"], "fields": ""},
        },
        "fieldConfig": {"defaults": {}, "overrides": overrides},
    }


def _fold(boards: list[dict]) -> list[dict]:
    by_uid = {b["uid"]: b for b in boards}
    moved: dict[str, list[dict]] = {}
    for uid, rule in FOLDS.items():
        b = by_uid[uid]
        ps = b["panels"]
        for src, dst in rule.get("describe", {}).items():
            s_p, d_p = _match(ps, src)[0], _match(ps, dst)[0]
            text = (s_p.get("options") or {}).get("content") or s_p.get("description") or s_p.get("title", "")
            d_p["description"] = "\n\n".join(x for x in (d_p.get("description", ""), text) if x)
            if s_p["type"] == "text":
                ps.remove(s_p)
        for prefix in rule.get("drop", []):
            for x in _match(ps, prefix):
                ps.remove(x)
        for prefix, target_uid in rule.get("move_to", {}).items():
            for x in _match(ps, prefix):
                ps.remove(x)
                moved.setdefault(target_uid, []).append(x)
        notes = [x for prefix in rule.get("notes", []) for x in _match(ps, prefix)]
        for x in notes:
            ps.remove(x)
            x["gridPos"]["w"] = 24
        kpis = [x for prefix in rule.get("kpi", []) for x in _match(ps, prefix)]
        for x in kpis:
            ps.remove(x)
        for prefix in rule.get("widen", []):
            for x in _match(ps, prefix):
                x["gridPos"]["w"] = 24
        head = []
        if kpis:
            head.append(
                kpis[0]
                if len(kpis) == 1 and "kpi_title" not in rule
                else _kpi_strip(kpis, rule.get("kpi_title", "Key counts"))
            )
            head[-1]["gridPos"].update(w=24, h=rule.get("kpi_h", 4))
            if "kpi_text" in rule:
                head[-1]["options"]["text"] = dict(rule["kpi_text"])
            if "kpi_note" in rule:
                head[-1]["description"] = "\n\n".join(
                    x for x in (head[-1].get("description"), rule["kpi_note"]) if x
                )
        # the collapsed Notes row goes LAST: Grafana gives every top-level panel after a row to that
        # row, so a collapsed row at the top swallowed the board (measured 2026-11-08, ship 3a635e9)
        b["panels"] = head + ps + ([model.notes_row(*notes)] if notes else [])
    for uid, extra in moved.items():
        b = by_uid[uid]
        strip = _kpi_strip(extra, "Key counts") if len(extra) > 1 else extra[0]
        strip["gridPos"].update(w=24, h=4)
        b["panels"] = [strip] + b["panels"]
    for b in boards:
        model.layout(b["panels"])
        for p in [q for top in b["panels"] for q in [top, *top.get("panels", [])]]:
            p.pop("raw_title", None)
    return boards


# ad-set labels name a SET, never a count (the analyst's wording, 2026-11-08); each label carries its
# definition into the ⓘ of every panel that shows it. [pre-recruitment ads, clustered alone] is reserved
# for a stand-alone clustering — none is shown today.
AD_SETS = {
    "[all fetched]": "[all fetched]: every listing-lens-fetched ad as of the last export; grows as recruits are processed.",
    "[pre-recruitment ads, clustered with recruits]": (
        "[pre-recruitment ads, clustered with recruits]: the depth sample plus 2 pilot fetches; advertiser "
        "groups computed on all fetched ads, then restricted."
    ),
    "[depth sample]": "[depth sample]: the design-weighted depth sample only.",
}


def _with_ad_set_notes(boards_: list[dict]) -> list[dict]:
    def walk(ps):
        for p_ in ps:
            yield p_
            yield from walk(p_.get("panels", []))

    for b in boards_:
        for p_ in walk(b["panels"]):
            text = p_.get("title", "") + " " + p_.get("description", "")
            sql = " ".join(t.get("rawSql", "") for t in p_.get("targets", []))
            notes = [d for label, d in AD_SETS.items() if label in text or label in sql]
            if notes:
                p_["description"] = "\n\n".join(x for x in (p_.get("description", ""), *notes) if x)
    return boards_


MASKED_NOTE = (
    "Gold (eval) ads: their text-hint, geocode and place columns read NULL here (the listings_masked view), "
    "so the listing-lens answer for them is never shown; in counts they fall with the no-answer ads."
)


def _with_mask_notes(boards_: list[dict]) -> list[dict]:
    """Every panel that reads a gold-answer column says how gold ads appear in it (C19)."""
    for b in boards_:
        for p_ in model._walk_panels(b["panels"]):
            sql = " ".join(t.get("rawSql", "") for t in p_.get("targets", []))
            if (
                model.MASKED_SRC in sql
                and model._MASKED_COL.search(sql)
                and MASKED_NOTE not in p_.get("description", "")
            ):
                p_["description"] = "\n\n".join(x for x in (p_.get("description", ""), MASKED_NOTE) if x)
    return boards_


# The rework's tabs (grafana/v2.py TabsLayout): per board, [(tab, [(section row, [panel title prefixes])])],
# from DATA notes/li-grafana-encodings.md § 1a. Applied AFTER _fold; a content panel no section claims
# raises, so a board cannot lose a panel to the regroup. A board absent here keeps its rows as they are.
TABS: dict[str, list[tuple[str, list[tuple[str, list[str]]]]]] = {
    f"{PFX}-overview": [
        (
            "G1 Read first",
            [
                ("Legend", ["About this data", "Candidate hypotheses"]),
                ("Findings register", ["Findings register"]),
            ],
        ),
        (
            "G2 Sample and coverage",
            [
                ("Key numbers", ["Pin and text-location rates"]),
                ("Sample vs population", ["Population vs sample"]),
                ("Field holes", ["Field coverage"]),
            ],
        ),
    ],
    f"{PFX}-geo": [
        (
            "G3 Location",
            [
                ("Viewpoint map", ["Viewpoint rings", "Pin atlas by label"]),
                (
                    "Which point listing-lens uses",
                    ["Which route listing-lens takes", "Point decision ledger"],
                ),
                (
                    "H1: written place or label centre?",
                    ["H1: pin-to-centre", "Pin → text pairs"],
                ),
                (
                    "Labels",
                    [
                        "Shared pins (H2)",
                        "Shared pins (table)",
                        "H3: pin-to-centre",
                        "Distance fingerprint",
                        "Coverage per label centre",
                    ],
                ),
                ("Out of region", ["Far-pin rate per list", "Far pins per advertiser"]),
                (
                    "Cross-source (listing-lens vs the phone session)",
                    ["listing-lens pins vs phone-session pins", "Ads in BOTH sources"],
                ),
            ],
        ),
        ("G4 Text-location extractor", [("Hint funnel", ["Hints by kind"])]),
    ],
    f"{PFX}-process": [
        (
            "G8 Process",
            [
                ("Runs", ["Runs — start", "Events ("]),
                ("A/B", ["H5 — A/B"]),
                ("Pace and latency", ["Crawl sweep", "Request latency"]),
                ("Request ledger", ["Recorded request counters"]),
                ("Geocode expiry", ["Geocode cache"]),
            ],
        ),
    ],
    f"{PFX}-explorer": [
        (
            "G9 Ads",
            [
                ("Ads", ["Ads — every field"]),
                ("One-ad dossier", ["Every field of the selected ad", "Photo URLs of the selected ad"]),
                ("Media reuse", ["Media URLs on"]),
            ],
        ),
        ("G9 One ad cluster", [("Ad-level link graph", ["Ad-level graph"])]),
    ],
    f"{PFX}-content": [
        (
            "G6 Time",
            [
                (
                    "Clock calibration",
                    ["Clock calibration", "T02: ad_id as a publish clock", "T02 — residuals"],
                ),
                (
                    "Depth = bump staleness",
                    ["H8 candidate", "T01 — age bucket", "T05: list rank", "T12: phone-session bump"],
                ),
                ("Whole-hour bumps", ["H12 candidate: scheduled", "H12 candidate: whole-hour"]),
                ("Listing survival", ["T04: share of ads older", "T07 — publish-month"]),
                ("Notes", ["G6 Read first"]),
            ],
        ),
        (
            "G7 Content",
            [
                ("Price", ["P1: price points", "P2: rate by stated age", "P4: disclosure", "H13 candidate"]),
                ("Services", ["Services offered", "Services: offered"]),
                ("Language", ["T4: written language", "T4 — declared"]),
                ('"New in town"', ["H14 candidate"]),
                ("Text and templates", ["T2: description length", "T3: within-advertiser"]),
                ("Notes", ["Read first"]),
            ],
        ),
    ],
    f"{PFX}-advertiser": [
        (
            "G5 Network",
            [
                ("Key counts", ["Key counts"]),
                ("Ad cluster / persona network", ["Ad cluster network", "Single-kind direct edges"]),
            ],
        ),
        (
            "G5 Size and method",
            [
                ("Concentration", ["Leaderboard", "Cluster sizes"]),
                ("v1.1 vs v2", ["Ablation: clusters per variant"]),
            ],
        ),
        (
            "G5 Behaviour (aggregate)",
            [
                ("Multi-label posting", ["Ads vs city labels", "City labels in multi-label posting"]),
                (
                    "Pin movement",
                    [
                        "Pin pairs within an advertiser",
                        "H4: age vs pin-to-text",
                        "Far pairs per persona",
                        "Far pins per persona",
                    ],
                ),
                ("Posting gaps", ["Posting gaps between"]),
            ],
        ),
        (
            "G5 Shared venues and templates",
            [
                ("Venues", ["Venues named by advertisers", "Venues (table)", "Venues across ad clusters"]),
                ("Templates", ["Template families across −desc", "Template families across personas"]),
                ("Notes", ["Read first", "How to read advertisers@2"]),
            ],
        ),
    ],
}

# GQM question ids per panel (DATA notes/li-grafana-gqm.md, the "Panel verdicts" tables); the GQM test
# checks every id is a live question and the ratchet tracks what is still untagged.
QUESTIONS: dict[tuple[str, str], tuple[str, ...]] = {
    (f"{PFX}-overview", "Pin and text-location rates"): ("G2.Q3", "G2.Q4"),
    (f"{PFX}-overview", "Findings register"): ("G1.Q1",),
    (f"{PFX}-overview", "Field coverage"): ("G2.Q2",),
    (f"{PFX}-overview", "Population vs sample"): ("G2.Q1",),
    (f"{PFX}-overview", "About this data"): ("G1.Q3",),
    (f"{PFX}-overview", "Candidate hypotheses"): ("G1.Q2",),
    (f"{PFX}-geo", "Viewpoint rings"): ("G3.Q6",),
    (f"{PFX}-geo", "Pin atlas by label"): ("G3.Q6",),
    (f"{PFX}-geo", "Shared pins (H2)"): ("G3.Q2",),
    (f"{PFX}-geo", "Shared pins (table)"): ("G3.Q2",),
    (f"{PFX}-geo", "Far-pin rate per list"): ("G3.Q4",),
    (f"{PFX}-geo", "Far pins per advertiser"): ("G3.Q4",),
    (f"{PFX}-geo", "H1: pin-to-centre"): ("G3.Q1",),
    (f"{PFX}-geo", "Pin → text pairs"): ("G3.Q1",),
    (f"{PFX}-geo", "Distance fingerprint"): ("G3.Q8",),
    (f"{PFX}-geo", "Hints by kind"): ("G4.Q2",),
    (f"{PFX}-geo", "Coverage per label centre"): ("G2.Q3",),
    (f"{PFX}-geo", "listing-lens pins vs phone-session pins"): ("G3.Q7",),
    (f"{PFX}-geo", "Ads in BOTH sources"): ("G3.Q7",),
    (f"{PFX}-geo", "H3: pin-to-centre"): ("G3.Q3",),
    (f"{PFX}-geo", "Which route listing-lens takes"): ("G3.Q5",),
    (f"{PFX}-geo", "Point decision ledger"): ("G3.Q5",),
    (f"{PFX}-process", "Geocode cache"): ("G8.Q5",),
    (f"{PFX}-process", "Runs — start"): ("G8.Q1",),
    (f"{PFX}-process", "Events ("): ("G8.Q1",),
    (f"{PFX}-process", "H5 — A/B"): ("G8.Q2",),
    (f"{PFX}-process", "Crawl sweep"): ("G8.Q3",),
    (f"{PFX}-process", "Request latency"): ("G8.Q3",),
    (f"{PFX}-process", "Recorded request counters"): ("G8.Q4",),
    (f"{PFX}-explorer", "Ads — every field"): ("G9.Q1", "G7.Q3"),
    (f"{PFX}-explorer", "Every field of the selected ad"): ("G9.Q1",),
    (f"{PFX}-explorer", "Photo URLs of the selected ad"): ("G9.Q1",),
    (f"{PFX}-explorer", "Media URLs on"): ("G9.Q1",),
    (f"{PFX}-explorer", "Ad-level graph"): ("G9.Q1",),
    (f"{PFX}-content", "Clock calibration"): ("G6.Q1",),
    (f"{PFX}-content", "T02: ad_id as a publish clock"): ("G6.Q1",),
    (f"{PFX}-content", "T02 — residuals"): ("G6.Q1",),
    (f"{PFX}-content", "T01 — age bucket"): ("G6.Q2",),
    (f"{PFX}-content", "T05: list rank"): ("G6.Q2",),
    (f"{PFX}-content", "T04: share of ads older"): ("G6.Q4",),
    (f"{PFX}-content", "T07 — publish-month"): ("G6.Q4",),
    (f"{PFX}-content", "H12 candidate: scheduled"): ("G6.Q3",),
    (f"{PFX}-content", "T12: phone-session bump"): ("G6.Q2",),
    (f"{PFX}-content", "H8 candidate"): ("G6.Q2",),
    (f"{PFX}-content", "H12 candidate: whole-hour"): ("G6.Q3",),
    (f"{PFX}-content", "G6 Read first"): ("G1.Q3",),
    (f"{PFX}-content", "P1: price points"): ("G7.Q1",),
    (f"{PFX}-content", "P2: rate by stated age"): ("G7.Q2",),
    (f"{PFX}-content", "P4: disclosure"): ("G7.Q2",),
    (f"{PFX}-content", "H13 candidate"): ("G7.Q1",),
    (f"{PFX}-content", "Services offered"): ("G7.Q3",),
    (f"{PFX}-content", "Services: offered"): ("G7.Q3",),
    (f"{PFX}-content", "T2: description length"): ("G1.Q3",),
    (f"{PFX}-content", "T3: within-advertiser"): ("G5.Q11",),
    (f"{PFX}-content", "T4: written language"): ("G7.Q4",),
    (f"{PFX}-content", "T4 — declared"): ("G7.Q4",),
    (f"{PFX}-content", "H14 candidate"): ("G7.Q5",),
    (f"{PFX}-content", "Read first"): ("G1.Q3",),
    (f"{PFX}-advertiser", "Key counts"): ("G5.Q1", "G5.Q2", "G5.Q3", "G5.Q9"),
    (f"{PFX}-advertiser", "Leaderboard"): ("G5.Q6",),
    (f"{PFX}-advertiser", "Cluster sizes"): ("G5.Q6",),
    (f"{PFX}-advertiser", "Single-kind direct edges"): ("G5.Q5",),
    (f"{PFX}-advertiser", "Ad cluster network"): ("G5.Q1", "G5.Q2", "G5.Q3", "G5.Q4", "G5.Q5"),
    (f"{PFX}-advertiser", "Ads vs city labels"): ("G5.Q8",),
    (f"{PFX}-advertiser", "City labels in multi-label posting"): ("G5.Q8",),
    (f"{PFX}-advertiser", "Posting gaps between"): ("G5.Q12",),
    (f"{PFX}-advertiser", "Pin pairs within an advertiser"): ("G5.Q9",),
    (f"{PFX}-advertiser", "H4: age vs pin-to-text"): ("G5.Q9",),
    (f"{PFX}-advertiser", "Ablation: clusters per variant"): ("G5.Q7",),
    (f"{PFX}-advertiser", "Template families across −desc"): ("G5.Q11",),
    (f"{PFX}-advertiser", "Template families across personas"): ("G5.Q11",),
    (f"{PFX}-advertiser", "Far pairs per persona"): ("G5.Q9",),
    (f"{PFX}-advertiser", "Far pins per persona"): ("G3.Q4",),
    (f"{PFX}-advertiser", "Venues across ad clusters"): ("G5.Q10",),
    (f"{PFX}-advertiser", "Venues named by advertisers"): ("G5.Q10",),
    (f"{PFX}-advertiser", "Venues (table)"): ("G5.Q10",),
    (f"{PFX}-advertiser", "Read first"): ("G1.Q3",),
    (f"{PFX}-advertiser", "How to read advertisers@2"): ("G1.Q3",),
}


def _with_questions(boards_: list[dict]) -> list[dict]:
    for b in boards_:
        for p_ in model._walk_panels(b["panels"]):
            hits = [
                q
                for (uid, prefix), q in QUESTIONS.items()
                if uid == b["uid"] and p_.get("title", "").startswith(prefix)
            ]
            if len(hits) > 1:
                raise ValueError(f"{b['uid']}: {p_.get('title')!r} matches {len(hits)} QUESTIONS entries")
            if hits and p_.get("type") != "row":
                model.answers(p_, *hits[0])
    return boards_


def _tabbed(boards_: list[dict]) -> list[dict]:
    for b in boards_:
        spec = TABS.get(b["uid"])
        if not spec:
            continue
        content = [p_ for p_ in model._walk_panels(b["panels"]) if p_.get("type") != "row"]
        claimed: list[dict] = []
        out: list[dict] = []
        for tab, sections in spec:
            for i, (title, prefixes) in enumerate(sections):
                r = model.row(title, tab=tab if i == 0 else None)
                # a "Notes" section stays collapsed: long text costs one grid line until opened (as the
                # fold's Notes row did); it comes LAST in its tab, so no panel follows it under the fold
                if title.startswith("Notes"):
                    if i != len(sections) - 1:
                        raise ValueError(f"{b['uid']} tab {tab!r}: the Notes section must come last")
                    r["collapsed"] = True
                out.append(r)
                for prefix in prefixes:
                    hits = [
                        p_ for p_ in content if p_.get("title", "").startswith(prefix) and p_ not in claimed
                    ]
                    if len(hits) != 1:
                        raise ValueError(f"{b['uid']} tab {tab!r}: {prefix!r} matches {len(hits)} panels")
                    claimed.append(hits[0])
                    (r["panels"] if r["collapsed"] else out).append(hits[0])
        left = [p_.get("title") for p_ in content if p_ not in claimed]
        if left:
            raise ValueError(f"{b['uid']}: panels in no tab section: {left}")
        b["panels"] = out
        model.layout(b["panels"])
    return boards_


def _with_persona_gate(boards_: list[dict]) -> list[dict]:
    """Every panel that reads persona_id carries the D2 draft stamp in its ⓘ (model.check enforces it)."""
    for b in boards_:
        for p_ in model._walk_panels(b["panels"]):
            sql = " ".join(t.get("rawSql", "") for t in p_.get("targets", []))
            if "persona_id" in sql and model.PERSONA_DRAFT not in p_.get("description", ""):
                p_["description"] = "\n\n".join(x for x in (p_.get("description", ""), PERSONA_GATE) if x)
    return boards_


# G6 moved into the content board (encodings § 1a). the time board was a one-panel redirect stub until the operator
# confirmed no bookmark used it (2026-11-13); its source board is still built so the merge has its panels,
# and the merged-away board is then dropped, so tofu destroys that one dashboard.
MERGE_INTO = {f"{PFX}-time": f"{PFX}-content"}
# panels the GQM test accepts without a question id, each with its reason (a waiver, never a silent skip)
GQM_WAIVERS: dict[tuple[str, str], str] = {}


def _merged(boards_: list[dict]) -> list[dict]:
    by = {b["uid"]: b for b in boards_}
    for src, dst in MERGE_INTO.items():
        s_, d_ = by[src], by[dst]
        moved = [p_ for p_ in model._walk_panels(s_["panels"]) if p_.get("type") != "row"]
        for p_ in moved:
            if p_.get("title", "").startswith("Read first"):
                p_["title"] = "G6 Read first"  # the content board has its own Read first
        titles = {p_.get("title") for p_ in model._walk_panels(d_["panels"])}
        clash = sorted(t for t in (p_.get("title") for p_ in moved) if t in titles)
        if clash:
            raise ValueError(f"{src} → {dst}: titles already on {dst}: {clash}")
        d_["panels"] += moved
        have = {v["name"]: v for v in d_["templating"]["list"]}
        for v in s_["templating"]["list"]:
            if v["name"] in have and have[v["name"]] != v:
                raise ValueError(f"{src} → {dst}: variable {v['name']} differs")
            if v["name"] not in have:
                d_["templating"]["list"].append(v)
    return [b_ for b_ in boards_ if b_["uid"] not in MERGE_INTO]


def _var_refs(text: str, name: str) -> bool:
    return re.search(r"(\$\{?|\[\[)" + re.escape(name) + r"(?![A-Za-z0-9_])", text) is not None


def _without_unused_vars(boards: list[dict]) -> list[dict]:
    """Drop each variable no panel, link or used variable references: merges and tab regroups left
    dropdowns that change nothing (the content board's viewpoint/field/min_advertisers, the advertiser board's
    advertiser; listy's check 2026-11-13), which reads as a broken filter."""
    for b in boards:
        rest = json.dumps({k: v for k, v in b.items() if k != "templating"})
        vs = b["templating"]["list"]
        used = {v["name"] for v in vs if _var_refs(rest, v["name"])}
        grew = True
        while grew:  # a used variable's own query can need another (oor_line reads out_of_region)
            text = json.dumps([v for v in vs if v["name"] in used])
            more = {v["name"] for v in vs if v["name"] not in used and _var_refs(text, v["name"])}
            used |= more
            grew = bool(more)
        b["templating"]["list"] = [v for v in vs if v["name"] in used]
    return boards


def build_all() -> list[dict]:
    # _with_candidates links each candidate to its evidence panel by id (viewPanel=panel-<id>), so it runs
    # AFTER _tabbed: the regroup renumbers panels, and before it 5 of the 7 links pointed at the wrong panel
    # (measured on live 2026-11-13)
    return _without_unused_vars(
        _with_questions(
            _with_candidates(
                _tabbed(
                    _with_persona_gate(
                        _with_mask_notes(_with_ad_set_notes(_merged(_fold([b() for b in BOARDS]))))
                    )
                )
            )
        )
    )


# ---------------------------------------------------------------- geography

# the canonical box lives in the `region` table (loaded from config): one source of truth
BOX = "(SELECT south, west, north, east FROM region WHERE site = l.site LIMIT 1)"


def km(a: str, b: str) -> str:
    """Geodesic km between two geography(Point, 4326) values — PostGIS ST_Distance on the WGS84
    spheroid (export-db@9 geography columns), replacing the hand-written spherical haversine."""
    return f"(ST_Distance({a}, {b}) / 1000.0)"


def geog(lat: str, lon: str) -> str:
    """A computed lat/lon as geography, for points that have no stored column (e.g. a clamped one)."""
    return f"ST_SetSRID(ST_MakePoint({lon}, {lat}), 4326)::geography"


# In Geomap's fit view, `zoom` is the MAX zoom of the fit (grafana v13.0.1 GeomapPanel.tsx initViewExtent:
# `const maxZoom = config.zoom ?? config.maxZoom`). Left unset, Grafana stores zoom: 1 and every fit is
# capped near world view (measured 2026-11-08: all the geo board maps at zoom 1.85, ~29,000 km across).
FIT_MAX_ZOOM = 18


# Every map opens on the viewpoint with a 20 km radius in view (operator rule, no exceptions): each geomap fits
# an invisible layer that is the 20 km circle around li.viewpoint — the centre is DATA, never in this JSON.
VIEW_RADIUS_KM = 20
VIEW_LAYER = model.VIEW_LAYER


# Stock data links in the stock tooltip: title → the ad itself (external, rendered target=_blank rel=noreferrer by
# grafana-ui TextLink, so no Referer reaches the site), ad_id → the Explorer dossier.
def ad_link(url_field: str, on: str) -> dict:
    """The ONE ad-link convention: field `on` links to the ad at `url_field`, external, new tab; grafana-ui
    TextLink renders it target=_blank rel=noreferrer, so no Referer reaches the site."""
    return {
        "matcher": {"id": "byName", "options": on},
        "properties": [
            {
                "id": "links",
                "value": [
                    {
                        "title": "Open the ad (new tab, no referrer)",
                        "url": "${__data.fields." + url_field + "}",
                        "targetBlank": True,
                    }
                ],
            }
        ],
    }


AD_LINK_OVERRIDES = [
    ad_link("source_url", "title"),
    {
        "matcher": {"id": "byName", "options": "ad_id"},
        "properties": [
            {
                "id": "links",
                "value": [
                    {
                        "title": "Open in Explorer",
                        "url": f"/d/{PFX}-explorer?var-ad_id=${{__data.fields.ad_id}}",
                    }
                ],
            }
        ],
    },
]


def view_sql() -> str:
    lat, lon = _dest(str(VIEW_RADIUS_KM), "radians(j * 10)")
    return f"""
SELECT j, {lat} AS lat, {lon} AS lon FROM {VP} v, generate_series(0, 36) AS j ORDER BY j"""


def view_layer(ref: str = "Z") -> dict:
    return {
        "type": "route",
        "name": VIEW_LAYER,
        "config": {
            "style": {"color": {"fixed": "transparent"}, "opacity": 0, "size": {"fixed": 1}},
            "arrow": 0,
        },
        "location": {"mode": "coords", "latitude": "lat", "longitude": "lon"},
        "filterData": {"id": "byRefId", "options": ref},
        "tooltip": False,
    }


def _with_links(field_config: dict | None) -> dict:
    fc = field_config or {"defaults": {}, "overrides": []}
    return {**fc, "overrides": list(fc.get("overrides", [])) + AD_LINK_OVERRIDES}


def to_matrix(row_f: str, col_f: str, value_f: str) -> list[dict]:
    """Stock 'Grouping to matrix' (the reuse audit's swap for hand-written FILTER pivots): the first output
    field is named `row\\col` (grafana-data groupingToMatrix.ts), missing cells become 0."""
    return [
        {
            "id": "groupingToMatrix",
            "options": {"rowField": row_f, "columnField": col_f, "valueField": value_f, "emptyValue": "zero"},
        }
    ]


def colour_cells(*plain: str, scheme: str = "continuous-YlRd", **defaults) -> dict:
    """A label × bucket matrix as a stock table with colour-background cells — the heatmap panel needs a
    numeric/time x field and drew NOTHING over a string one (T01, the distance fingerprint: drawn-content
    sweep 2026-11-08). `plain` columns (labels, a median) keep plain cells."""
    return {
        "defaults": {
            "color": {"mode": scheme},
            "custom": {"cellOptions": {"type": "color-background", "mode": "gradient"}},
            **defaults,
        },
        "overrides": [
            {
                "matcher": {"id": "byName", "options": name},
                "properties": [{"id": "custom.cellOptions", "value": {"type": "auto"}}],
            }
            for name in plain
        ],
    }


def min_filter(field: str, value: int, ref: str = "A") -> list[dict]:
    """Stock 'Filter data by values' (field >= value), scoped to one query — the reuse audit's swap for the
    `HAVING >= ${min_*:raw}` threshold variables. Raise the value in the panel editor."""
    return [
        {
            "id": "filterByValue",
            "filter": {"id": "byRefId", "options": ref},
            "options": {
                "type": "include",
                "match": "all",
                "filters": [
                    {"fieldName": field, "config": {"id": "greaterOrEqual", "options": {"value": value}}}
                ],
            },
        }
    ]


def _geomap(title: str, sqls: list[str], layers: list[dict], *, w=24, h=14, description="", **extra) -> dict:
    fc = _with_links(extra.pop("fieldConfig", None))
    p = panel(
        "geomap",
        title,
        list(sqls) + [view_sql()],
        w=w,
        h=h,
        description=description,
        options={
            "view": {
                "id": "fit",
                "allLayers": False,
                "layer": VIEW_LAYER,
                "padding": 0,
                "lastOnly": False,
                "zoom": FIT_MAX_ZOOM,
            },
            "controls": {"showZoom": True, "mouseWheelZoom": True, "showScale": True, "showMeasure": True},
            "basemap": {"type": "default", "name": "Basemap"},
            "layers": [view_layer("Z")] + list(layers),
            "tooltip": {"mode": "details"},
        },
        fieldConfig=fc,
        **extra,
    )
    p["targets"][-1]["refId"] = "Z"
    return p


def _markers(name: str, ref: str, *, color: str | None = None, size=5, symbol="circle", text=None) -> dict:
    style = {
        "size": {"fixed": size},
        "color": {"field": color} if color else {"fixed": "dark-blue"},
        "opacity": 0.7,
        "symbol": {"mode": "fixed", "fixed": f"img/icons/marker/{symbol}.svg"},
    }
    if text:
        style["text"] = {"mode": "field", "field": text}
        # above the dot, not on it: with no offset the label sits under the WebGL symbol (measured
        # 2026-11-08: 41 text styles set, none visible); same shape as the edge labels, which render
        style["textConfig"] = {"fontSize": 12, "offsetY": -9, "textAlign": "center", "textBaseline": "bottom"}
    return {
        "type": "markers",
        "name": name,
        "config": {"style": style, "showLegend": True},
        "location": {"mode": "coords", "latitude": "lat", "longitude": "lon"},
        "filterData": {"id": "byRefId", "options": ref},
        "tooltip": True,
    }


# H3: the phone import's own distances (km_to_target is THAT session's target point, not li's)
H3_GEO = f"""SELECT d_city, km_to_target, city_filter, pin_class, ad_id
FROM import_phone_ads
WHERE d_city IS NOT NULL AND km_to_target IS NOT NULL AND {pin_ok(None)}"""
H3_SQL = f"""
SELECT * FROM ({H3_GEO}) g
WHERE {in_filter("g.city_filter", "city_filter")} AND g.d_city < 60 AND g.km_to_target < 60"""
# a far pin IS a flagged pin (measured 2026-11-08: km_pin_city > 60 and location_valid_pin = false are the same
# 24 pins), so derive's flag is the ONE definition. The far-pin panels are the out-of-region view: they show
# the flagged set in BOTH modes, by design (team-lead, 2026-11-08) — hiding them would erase H9's evidence.
FAR = "l.location_valid_pin IS FALSE"
FAR_NOTE = (
    "Shows the out-of-region (flagged) pins in both modes of the out-of-region variable, by design: a far pin "
    "IS a flagged pin (derive's region rule, > 60 km from the label centre)."
)
FAR_SUB = f"""
SELECT o.list_subcategory AS list, count(*) FILTER (WHERE l.pin_lat IS NOT NULL)::int AS pinned,
       count(*) FILTER (WHERE {FAR})::int AS far, count(DISTINCT l.advertiser_id) FILTER (WHERE {FAR})::int AS far_advertisers,
       (count(*) FILTER (WHERE {FAR})::float / nullif(count(*) FILTER (WHERE l.pin_lat IS NOT NULL), 0)) AS far_rate
FROM listings_masked l JOIN observations o USING (site, ad_id)
GROUP BY 1 ORDER BY 1"""
FAR_ADV = f"""
SELECT l.advertiser_id, min(o.list_subcategory) AS list, count(*) FILTER (WHERE {FAR})::int AS far_pins,
       count(*) FILTER (WHERE l.pin_lat IS NOT NULL)::int AS pinned,
       round(max(l.km_pin_city)::numeric)::int AS max_km, string_agg(DISTINCT l.city_label, ', ') AS labels
FROM listings_masked l JOIN observations o USING (site, ad_id)
GROUP BY l.advertiser_id HAVING count(*) FILTER (WHERE {FAR}) > 0
ORDER BY far_pins DESC"""


# ---- viewpoint radius rings (operator request 2026-11-08). The centre is DATA: it comes only from the
# li.viewpoint table (loaded by export-db from the operator's DATA config), never from this repo.
MAX_RINGS = 6
# band fill grid, in km at the fixed 20 km fit (zoom ≈ 10.6, ≈ 73 m/px measured 2026-11-08):
FILL_KM = 0.2  # ≈ 2.7 px apart, under the kernel radius, so the fill reads as solid
FILL_RADIUS_PX, FILL_BLUR_PX = 3, 2  # the kernel reaches ≈ 5 px ≈ 0.37 km past each point
FILL_MARGIN_KM = 0.4  # so the grid stops this far inside the outer edge
FILL_BOX_KM = 26  # half the view box (the 20 km fit is ≈ 51 × 41 km): points beyond are never seen
# the selected viewpoint, or the first one while the variable is still empty: on load the dashboard sent
# viewpoint = '' (its saved default is empty), so every ring, the viewpoint mark and the 20 km fit layer came
# back empty and the map drew blank (2026-11-13). The label is data, so it is never written as a default here.
VP = (
    "(SELECT label, lat, lon, geog FROM viewpoint WHERE label = coalesce(nullif(${viewpoint:sqlstring}, ''), "
    "(SELECT min(label) FROM viewpoint)))"
)
STEP, COUNT = "${ring_step:raw}", "${ring_count:raw}"


def _dest(r_km: str, theta: str) -> tuple[str, str]:
    """Destination from the viewpoint v: r_km along bearing theta (radians), PostGIS ST_Project on the
    spheroid — the same geodesic ST_DWithin counts with, so a drawn edge and its count agree."""
    pt = f"ST_Project(v.geog, ({r_km}) * 1000.0, {theta})::geometry"
    return f"ST_Y({pt})", f"ST_X({pt})"


# ONE pin set for the ring counts and the drawn pins: the board's own filters (bug 2026-11-08: the counts used
# every pinned ad, the map only the filtered ones, so "16 ads within 2 km" sat over ~3 visible pins)
PIN_WHERE_BASE = (
    "l.pin_lat IS NOT NULL AND "
    + in_filter("o.list_subcategory", "subcategory")
    + " AND "
    + in_filter("l.city_label", "city_label")
    + " AND "
    + in_filter("l.pin_verdict", "verdict")
)
# the counts take every pin the board shows: the main pins layer plus, in show mode, the grey one
PIN_WHERE = f"{PIN_WHERE_BASE} AND {pin_ok()}"
# ONE observation per ad (the latest): a plain join would double-count an ad once a revisit or a
# re-crawl adds a second row — 1:1 at export-db@8, so no count changed
PIN_FROM = (
    "FROM listings_masked l JOIN LATERAL (SELECT * FROM observations o1 WHERE o1.site = l.site "
    "AND o1.ad_id = l.ad_id ORDER BY o1.seen_at DESC, o1.seq DESC LIMIT 1) o ON true"
)


def _within(r_km: str) -> str:
    return (
        f"(SELECT count(DISTINCT (l.site, l.ad_id)) {PIN_FROM} WHERE {PIN_WHERE} AND "
        f"ST_DWithin(v.geog, l.pin_geog, ({r_km}) * 1000.0))"
    )


# the rings map's pins: the same set, each row carrying how many ads share its point (drawn on the pin)
RING_PINS = f"""
SELECT l.ad_id, l.title, l.source_url, o.list_subcategory AS subcategory, l.city_label,
       l.pin_lat AS lat, l.pin_lon AS lon, l.pin_verdict,
       count(*) OVER (PARTITION BY round(l.pin_lat::numeric, 5), round(l.pin_lon::numeric, 5))::int AS ads_here,
       CASE WHEN count(*) OVER (PARTITION BY round(l.pin_lat::numeric, 5), round(l.pin_lon::numeric, 5)) > 1
            THEN (count(*) OVER (PARTITION BY round(l.pin_lat::numeric, 5), round(l.pin_lon::numeric, 5)))::text
       END AS stack
{PIN_FROM} WHERE {PIN_WHERE_BASE} AND {pin_in()}"""
RING_GREY = f"""
SELECT l.ad_id, l.title, l.source_url, o.list_subcategory AS subcategory, l.city_label,
       l.pin_lat AS lat, l.pin_lon AS lon, l.location_invalid_reason
{PIN_FROM} WHERE {PIN_WHERE_BASE} AND {pin_flagged()}"""


def ring_edge_sql(k: int) -> str:
    """ONE clean circle at k*step km (band k's outer edge) for k < count; the last edge is RING_OUTER."""
    lat, lon = _dest(f"{k} * {STEP}", "radians(j * 5)")
    # the count is per RING: a MATERIALIZED CTE runs it once. In the select list (or a LATERAL, which the
    # planner pulled back up) it ran as a SubPlan once per vertex: 73 loops x ~0.18 s, the outer ring 7-13 s
    # (EXPLAIN ANALYZE, listy's check 2026-11-13)
    return f"""
WITH v AS MATERIALIZED {VP},
c AS MATERIALIZED (SELECT {_within(f"{k} * {STEP}")} AS n FROM v)
SELECT j, {lat} AS lat, {lon} AS lon,
       '≤ ' || ({k} * {STEP}) || ' km: ' || c.n || ' ads' AS ring
FROM v, c, generate_series(0, 72) AS j
WHERE {k} < {COUNT}
ORDER BY j"""


def band_fill_sql(k: int) -> str:
    """Band k's fill: grid points every FILL_KM whose TRUE distance falls in band k (position =
    ST_Project(bearing, distance)); full weight — the fade is the layer's opacity, because OpenLayers'
    heatmap maps a lower weight off the visible end of the gradient instead of lighter.
    The heatmap kernel (radius + blur px) paints PAST its point, so the grid stops FILL_MARGIN_KM inside
    the outer edge: the fill ends at the ring, not a blur's width beyond it (2026-11-08: radius 9 + blur
    12 = 21 px ≈ 1.5 km of bleed at the fit). The grid is clipped to the view box, so 6 × 10 km stays
    bounded."""
    n = f"ceil(least({COUNT} * {STEP}, {FILL_BOX_KM}) / {FILL_KM})::int"
    r = f"({FILL_KM} * sqrt(power(gx, 2) + power(gy, 2)))"
    lat, lon = _dest("g.r", "g.th")
    return f"""
SELECT 1.0::float AS shade, {lat} AS lat, {lon} AS lon, g.band
FROM {VP} v,
LATERAL (SELECT {r} AS r, atan2(gx, gy) AS th, greatest(1, ceil({r} / {STEP}))::int AS band
         FROM generate_series(-{n}, {n}) gx, generate_series(-{n}, {n}) gy) g
WHERE g.r <= {COUNT} * {STEP} - {FILL_MARGIN_KM} AND g.band = {k} AND {k} <= {COUNT}"""


def band_opacity(k: int) -> float:
    """Inner strongest, fading outward: 0.60, 0.37, 0.28, 0.23, 0.19, 0.17."""
    return round(0.6 / k**0.7, 2)


RING_OUTER = (
    lambda ll: (
        f"""
WITH v AS MATERIALIZED {VP},
c AS MATERIALIZED (SELECT {_within(f"{COUNT} * {STEP}")} AS n FROM v)
SELECT j, {ll[0]} AS lat, {ll[1]} AS lon,
       '≤ ' || ({COUNT} * {STEP}) || ' km: ' || c.n || ' ads' AS ring
FROM v, c, generate_series(0, 72) AS j
ORDER BY j"""
    )
)(_dest(f"{COUNT} * {STEP}", "radians(j * 5)"))
RING_LABELS = (
    lambda ll: (
        f"""
SELECT {ll[0]} AS lat, {ll[1]} AS lon,
       '≤ ' || (k * {STEP}) || ' km · ' || {_within(f"k * {STEP}")} || ' ads' AS label
FROM {VP} v, generate_series(1, {COUNT}) AS k"""
    )
)(_dest(f"k * {STEP}", "radians(0)"))
VIEWPOINT_MARK = f"SELECT v.lat, v.lon, v.label AS viewpoint FROM {VP} v"


def viewpoint_panels(pins_sql: str) -> list[dict]:
    edge_refs = [f"R{k}" for k in range(1, MAX_RINGS)]
    coords = {"mode": "coords", "latitude": "lat", "longitude": "lon"}
    layers = [  # bottom: one filled band per ring (heatmap over a true-distance grid), fading outward
        {
            "type": "heatmap",
            "name": f"Fill {k}",
            "config": {
                "blur": FILL_BLUR_PX,
                "radius": FILL_RADIUS_PX,
                # FIXED weight: a field whose values are all equal scales to 0 (min = max) and draws nothing
                "weight": {"fixed": 1, "min": 0, "max": 1},
            },
            "location": coords,
            "filterData": {"id": "byRefId", "options": f"F{k}"},
            "opacity": band_opacity(k),
            "tooltip": False,
        }
        for k in range(1, MAX_RINGS + 1)
    ]
    for k in range(1, MAX_RINGS):  # inner band edges; the outermost edge is the "Outer edge" layer
        layers.append(
            {
                "type": "route",
                "name": f"Edge {k}",
                "config": {
                    "style": {"color": {"fixed": "#1F60C4"}, "opacity": 0.9, "size": {"fixed": 1}},
                    "arrow": 0,
                },
                "location": coords,
                "filterData": {"id": "byRefId", "options": edge_refs[k - 1]},
                "tooltip": True,
            }
        )
    layers.append(
        {
            "type": "route",
            "name": "Outer edge",
            "config": {
                "style": {"color": {"fixed": "#1F60C4"}, "opacity": 0.9, "size": {"fixed": 1}},
                "arrow": 0,
            },
            "location": coords,
            "filterData": {"id": "byRefId", "options": "O"},
            "tooltip": True,
        }
    )
    layers.append(
        {
            **_markers("Edge labels", "L", size=1, text="label"),
            "config": {
                "style": {
                    "size": {"fixed": 1},
                    "color": {"fixed": "#B4CCFF"},  # light: dark blue text vanished on the dark basemap
                    "opacity": 1,
                    "symbol": {"mode": "fixed", "fixed": "img/icons/marker/circle.svg"},
                    "text": {"mode": "field", "field": "label"},
                    "textConfig": {
                        "fontSize": 12,
                        "offsetY": -4,
                        "textAlign": "center",
                        "textBaseline": "bottom",
                    },
                },
                "showLegend": False,
            },
        }
    )
    vp_layer = _markers("Viewpoint", "V", size=14, symbol="star", text="viewpoint")
    # the name sits BELOW the star; ring labels sit ABOVE their north edge point, so at 2 km × 2 the
    # first ring's label (27 px north) and the name no longer stack (2026-11-08 screenshot)
    vp_layer["config"]["style"]["textConfig"] = {
        "fontSize": 12,
        "offsetY": 16,
        "textAlign": "center",
        "textBaseline": "top",
    }
    layers.append(vp_layer)
    layers.insert(0, view_layer("Z"))  # invisible 20 km circle the view fits
    # ONE fixed colour, no legend: coloured by city_label (text) with no scheme the legend showed Grafana's
    # default thresholds "< 80 / 80+"; a by-name scheme recoloured the pins but kept that legend (measured
    # live 2026-11-13). The rings and their labels already carry the geography.
    pins = _markers("Ad pins", "P", text="stack")  # label = ads stacked there
    pins["config"]["style"]["color"] = {"fixed": "green"}
    pins["config"]["showLegend"] = False
    layers.append(pins)
    layers.append(grey_layer("G"))  # flagged pins, show mode only
    p = panel(
        "geomap",
        "Viewpoint rings: ${ring_count} × ${ring_step} km — True-distance filled bands from the viewpoint table; pins on top.",
        [band_fill_sql(k) for k in range(1, MAX_RINGS + 1)]
        + [ring_edge_sql(k) for k in range(1, MAX_RINGS)]
        + [RING_OUTER, RING_LABELS, VIEWPOINT_MARK, RING_PINS, RING_GREY, view_sql()],
        expect_rows=("O", "V", "Z"),  # the outer ring, the viewpoint and the 20 km fit: empty = a blank map
        w=24,
        h=16,
        description="Exactly `rings` bands from the viewpoint table: each a filled annulus (strongest inside), one "
        "thin outline per band edge, a label at each edge. All distances are true km (spherical formulas in SQL). "
        "The fill is core Geomap's heatmap over a grid whose band comes from each point's true distance — core "
        "Geomap cannot fill a polygon from a query. The view fits the outermost edge, so it follows step × rings.\n\n"
        + OOR_NOTE,
        options={
            "view": {
                "id": "fit",
                "allLayers": False,
                "layer": VIEW_LAYER,  # the 20 km view, whatever step × rings (bug 2026-11-08: it fitted the rings)
                "padding": 2,
                "lastOnly": False,
                "zoom": FIT_MAX_ZOOM,
            },
            "controls": {"showZoom": True, "mouseWheelZoom": True, "showScale": True, "showMeasure": True},
            "basemap": {"type": "default", "name": "Basemap"},
            "layers": layers,
            "tooltip": {"mode": "details"},
        },
        fieldConfig={
            "defaults": {},
            # the heatmap colours by the FIRST numeric field (grafana v13.0.1 heatMap.tsx: frame.fields.find(number)),
            # so `shade` comes first and alone gets the single-hue scheme; pins keep their own colours
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "shade"},
                    "properties": [{"id": "color", "value": {"mode": "continuous-blues"}}],
                }
            ],
        },
    )
    p["fieldConfig"]["overrides"] += AD_LINK_OVERRIDES
    refs = [f"F{k}" for k in range(1, MAX_RINGS + 1)] + edge_refs + ["O", "L", "V", "P", "G", "Z"]
    for t, ref in zip(p["targets"], refs, strict=True):
        t["refId"] = ref
    return [row("Viewpoint"), p]


def geography() -> dict:
    sub = in_filter("o.list_subcategory", "subcategory")
    base = f"FROM listings_masked l JOIN observations o USING (site, ad_id) WHERE {sub}"
    outside = "NOT (l.pin_lat BETWEEN b.south AND b.north AND l.pin_lon BETWEEN b.west AND b.east)"
    boxj = f"CROSS JOIN LATERAL {BOX} b"

    pins = f"""
SELECT l.ad_id, l.title, l.source_url, o.list_subcategory AS subcategory, l.city_label,
       l.pin_lat AS lat, l.pin_lon AS lon,
       l.pin_verdict, round(l.km_pin_city::numeric, 1) AS km_pin_city, {outside} AS outside_box
FROM listings_masked l JOIN observations o USING (site, ad_id) {boxj}
WHERE l.pin_lat IS NOT NULL AND {sub}
  AND {in_filter("l.city_label", "city_label")} AND {in_filter("l.pin_verdict", "verdict")}"""
    pins_main = f"{pins}\n  AND {pin_in()}"

    def grey(where: str) -> str:
        """The flagged pins under a panel's own filters, for its grey layer (rows only in show mode)."""
        return f"""
SELECT l.ad_id, l.title, l.source_url, l.city_label, l.pin_lat AS lat, l.pin_lon AS lon,
       round(l.km_pin_city::numeric, 1) AS km_pin_city, l.location_invalid_reason
FROM listings_masked l JOIN observations o USING (site, ad_id)
WHERE l.pin_lat IS NOT NULL AND {where} AND {pin_flagged()}"""

    pins_grey = grey(
        f"{sub} AND {in_filter('l.city_label', 'city_label')} AND {in_filter('l.pin_verdict', 'verdict')}"
    )
    centres = """
SELECT city_centre_label AS city_label, min(city_centre_lat) AS lat, min(city_centre_lon) AS lon, count(*)::int AS ads
FROM listings_masked WHERE city_centre_lat IS NOT NULL GROUP BY 1"""
    ring = """
SELECT r.seq, r.lat, r.lon FROM region g,
LATERAL (VALUES (1, g.south, g.west), (2, g.south, g.east), (3, g.north, g.east),
                (4, g.north, g.west), (5, g.south, g.west)) r(seq, lat, lon)
ORDER BY r.seq"""

    def shared(keep: str) -> str:
        return f"""
SELECT round(l.pin_lat::numeric, 5)::float AS lat, round(l.pin_lon::numeric, 5)::float AS lon,
       count(*)::int AS ads, count(DISTINCT l.city_label)::int AS labels,
       count(DISTINCT l.advertiser_id)::int AS advertisers,
       string_agg(DISTINCT l.city_label, ', ') AS label_list, string_agg(l.ad_id, ', ') AS ad_ids
{base} AND l.pin_lat IS NOT NULL AND {keep}
GROUP BY 1, 2 HAVING count(*) >= 2
ORDER BY ads DESC, labels DESC"""

    clamp_lat, clamp_lon = (
        "greatest(b.south, least(b.north, l.pin_lat))",
        "greatest(b.west, least(b.east, l.pin_lon))",
    )
    oor = f"""
SELECT round(l.pin_lat::numeric, 4)::float AS lat, round(l.pin_lon::numeric, 4)::float AS lon,
       count(*)::int AS ads, string_agg(DISTINCT l.city_label, ', ') AS labels,
       round(max({km("l.pin_geog", geog(clamp_lat, clamp_lon))})::numeric, 1)::float AS km_to_box,
       count(DISTINCT l.advertiser_id)::int AS advertisers, string_agg(l.ad_id, ', ') AS ad_ids
FROM listings_masked l JOIN observations o USING (site, ad_id) {boxj}
WHERE l.pin_lat IS NOT NULL AND {outside} AND {sub} AND {pin_ok()}
GROUP BY 1, 2 ORDER BY km_to_box DESC"""

    flows = f"""
SELECT l.ad_id, l.pin_verdict, l.text_hint_kind, l.text_hint_raw, l.geocode_precise,
       round(l.km_pin_text::numeric, 2)::float AS km_pin_text, round(l.km_pin_city::numeric, 2)::float AS km_pin_city,
       l.pin_lat, l.pin_lon, l.geocode_lat, l.geocode_lon
{base} AND l.km_pin_text IS NOT NULL AND {pin_ok()} AND {text_ok()}
  AND ('${{precise_only:raw}}' = 'false' OR l.geocode_precise)
  AND {in_filter("l.pin_verdict", "verdict")}
ORDER BY l.km_pin_text DESC"""

    diag = f"""
SELECT greatest(l.km_pin_city, 0.1)::float AS km_pin_city, greatest(l.km_pin_text, 0.1)::float AS km_pin_text,
       l.pin_verdict, l.geocode_precise,
       (extract(epoch FROM (l.fetched_at - l.first_published)) / 86400)::int AS age_days, l.ad_id
{base} AND l.km_pin_text IS NOT NULL AND l.km_pin_city IS NOT NULL AND {pin_ok()} AND {text_ok()}"""
    diag_ref = "SELECT x::float AS km_pin_city, x::float AS km_pin_text FROM (VALUES (0.1), (4000.0)) d(x)"

    fp = f"""
WITH d AS (
  SELECT l.city_label,
         CASE '${{distance_field:raw}}' WHEN 'pin' THEN CASE WHEN {pin_ok()} THEN l.km_pin_city END
              ELSE CASE WHEN {text_ok()} THEN l.km_text_city END END AS km
  {base})
SELECT city_label,
       count(*) FILTER (WHERE km < 0.5)::int AS "0-0.5", count(*) FILTER (WHERE km >= 0.5 AND km < 1)::int AS "0.5-1",
       count(*) FILTER (WHERE km >= 1 AND km < 2)::int AS "1-2", count(*) FILTER (WHERE km >= 2 AND km < 5)::int AS "2-5",
       count(*) FILTER (WHERE km >= 5 AND km < 10)::int AS "5-10", count(*) FILTER (WHERE km >= 10 AND km < 25)::int AS "10-25",
       count(*) FILTER (WHERE km >= 25 AND km < 100)::int AS "25-100", count(*) FILTER (WHERE km >= 100)::int AS "100+",
       percentile_cont(0.5) WITHIN GROUP (ORDER BY km)::float AS median_km
FROM d WHERE km IS NOT NULL AND city_label IS NOT NULL GROUP BY 1 ORDER BY median_km"""

    stage = "CASE WHEN l.geocode_precise THEN 'precise' WHEN l.geocode_status IS NULL THEN 'not geocoded' ELSE 'coarse' END"
    funnel_src = f"""SELECT l.text_hint_kind AS kind, {stage} AS g, coalesce(l.pin_verdict, 'none') AS v
  {base} AND l.text_hint_kind IS NOT NULL AND {pin_ok()} AND {text_ok()}"""
    funnel_nodes = f"""
WITH s AS ({funnel_src})
SELECT 'k:' || kind AS id, kind AS title, 'hint kind' AS subtitle, count(*)::int AS mainstat FROM s GROUP BY kind
UNION ALL SELECT 'g:' || g, g, 'geocode', count(*)::int FROM s GROUP BY g
UNION ALL SELECT 'v:' || v, v, 'pin verdict', count(*)::int FROM s GROUP BY v"""
    funnel_edges = f"""
WITH s AS ({funnel_src})
SELECT 'k:' || kind || '>' || g AS id, 'k:' || kind AS source, 'g:' || g AS target,
       count(*)::int AS mainstat, count(*)::int AS thickness FROM s GROUP BY kind, g
UNION ALL
SELECT 'g:' || g || '>' || v, 'g:' || g, 'v:' || v, count(*)::int, count(*)::int FROM s GROUP BY g, v"""

    h6 = f"""
SELECT l.text_hint_kind AS kind,
       CASE WHEN l.geocode_precise THEN 'precise' WHEN l.geocode_status IS NULL THEN 'not geocoded' ELSE 'coarse' END AS status,
       count(*)::int AS n
{base} AND l.text_hint_raw IS NOT NULL AND {text_ok()}
GROUP BY 1, 2 ORDER BY sum(count(*)) OVER (PARTITION BY l.text_hint_kind) DESC, 1, 2"""

    w = "CASE '${weighting:raw}' WHEN 'weighted' THEN coalesce(o.list_sample_weight, 1) ELSE 1 END"
    cov = f"""
WITH cards AS (
  SELECT o.card_city_label AS city_label, sum({w})::float AS cards
  FROM observations o WHERE {sub} GROUP BY 1),
per_label AS (
  SELECT l.city_centre_label AS city_label, min(l.city_centre_lat) AS lat, min(l.city_centre_lon) AS lon,
         (sum(sm.weight * (l.pin_lat IS NOT NULL AND {pin_ok()})::int) / sum(sm.weight))::float AS pin_rate_w,
         avg((l.pin_lat IS NOT NULL AND {pin_ok()})::int)::float AS pin_rate_raw,
         avg((l.text_hint_raw IS NOT NULL AND {text_ok()})::int)::float AS text_rate_raw,
         count(*)::int AS fetched
  {
        base.replace(
            "WHERE",
            f"LEFT JOIN {DEPTH_SAMPLE} sm USING (site, ad_id) WHERE",
        )
    } AND l.city_centre_lat IS NOT NULL
  GROUP BY 1)
SELECT a.city_label, a.lat, a.lon, a.fetched, c.cards,
       CASE '${{weighting:raw}}' WHEN 'weighted' THEN a.pin_rate_w ELSE a.pin_rate_raw END AS pin_rate,
       a.text_rate_raw
FROM per_label a LEFT JOIN cards c USING (city_label) ORDER BY c.cards DESC NULLS LAST"""

    li_pins = f"SELECT ad_id, title, source_url, pin_lat AS lat, pin_lon AS lon, 'li' AS source FROM listings_masked WHERE pin_lat IS NOT NULL AND {pin_in(None)}"
    ph_pins = f"SELECT ad_id, pin_lat AS lat, pin_lon AS lon, 'phone-session' AS source FROM import_phone_ads WHERE pin_lat IS NOT NULL AND {pin_in(None)}"
    both_grey = f"""
SELECT ad_id, pin_lat AS lat, pin_lon AS lon, 'li' AS source, location_invalid_reason
FROM listings_masked WHERE pin_lat IS NOT NULL AND {pin_flagged(None)}
UNION ALL
SELECT ad_id, pin_lat, pin_lon, 'phone-session', location_invalid_reason
FROM import_phone_ads WHERE pin_lat IS NOT NULL AND {pin_flagged(None)}"""
    both = f"""
SELECT i.ad_id, round(({km("i.pin_geog", "l.pin_geog")})::numeric, 3)::float AS km_moved,
       l.city_label AS li_label, i.city_label AS phone_label
FROM import_phone_ads i JOIN listings_masked l USING (site, ad_id)
WHERE i.pin_lat IS NOT NULL AND l.pin_lat IS NOT NULL AND {pin_ok("i")} AND {pin_ok("l")}
ORDER BY km_moved DESC"""

    ad_link = {"defaults": {"links": [explorer_link("ad_id")]}, "overrides": []}
    log_axis = {"custom": {"scaleDistribution": {"type": "log", "log": 10}}}
    return dashboard(
        f"{PFX}-geo",
        "Geography",
        [
            *viewpoint_panels(pins),
            row("Atlas"),
            _geomap(
                "Pin atlas by label — Label centres as stars; the region box comes from the region table.",
                [pins_main, centres, ring, pins_grey],
                [
                    _markers("Pins", "A", color="city_label"),
                    grey_layer("D"),
                    _markers("Label centres", "B", size=12, symbol="star", text="city_label"),
                    {
                        "type": "route",
                        "name": "Region box",
                        "config": {
                            "style": {"color": {"fixed": "gray"}, "size": {"fixed": 2}, "opacity": 0.8}
                        },
                        "location": {"mode": "coords", "latitude": "lat", "longitude": "lon"},
                        "filterData": {"id": "byRefId", "options": "C"},
                    },
                ],
                h=16,
                description="Anchor map (H1/H2/H6). Box = the canonical region from config (region table).",
                fieldConfig=ad_link,
            ),
            row("H2 — one point, several labels"),
            _geomap(
                "Shared pins (H2) — Size = ads on the point; colour = distinct labels.",
                [shared(pin_in()), grey(sub)],
                [
                    grey_layer("B"),
                    {
                        **_markers("Shared points", "A", color="labels"),
                        "config": {
                            "style": {
                                "size": {"field": "ads", "min": 4, "max": 30, "fixed": 5},
                                "color": {"field": "labels"},
                                "opacity": 0.75,
                                "symbol": {"mode": "fixed", "fixed": "img/icons/marker/circle.svg"},
                            },
                            "showLegend": True,
                        },
                    },
                ],
                transformations=min_filter("ads", 2),
                w=12,
                h=12,
                fieldConfig={
                    "defaults": {
                        "color": {"mode": "thresholds"},
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [
                                {"color": "gray", "value": None},
                                {"color": "orange", "value": 2},
                                {"color": "red", "value": 3},
                            ],
                        },
                    },
                    "overrides": [],
                },
            ),
            panel(
                "table",
                "Shared pins (table)",
                shared(pin_ok()),
                w=12,
                h=12,
                options={"cellHeight": "sm"},
                transformations=min_filter("ads", 2),
            ),
            row("Far pins: > 60 km from label centre"),
            panel(
                "table",
                "Far-pin rate per list",
                FAR_SUB,
                w=8,
                h=8,
                description=FAR_NOTE,
                options={"cellHeight": "sm"},
                fieldConfig={
                    "defaults": {},
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "far_rate"},
                            "properties": [{"id": "unit", "value": "percentunit"}],
                        }
                    ],
                },
            ),
            panel(
                "table",
                "Far pins per advertiser (one TS advertiser holds 14 far pins)",
                FAR_ADV,
                w=16,
                h=8,
                description=FAR_NOTE,
                options={"cellHeight": "sm"},
            ),
            row("Outside the region box"),
            # the map twin is gone: every map opens on the 20 km view, where pins OUTSIDE the box are by
            # construction off-screen — it drew 0 features at open (drawn-content sweep, 2026-11-08)
            panel(
                "table",
                "Outside the region box — labels, km to the box",
                oor,
                w=24,
                h=12,
                options={"cellHeight": "sm"},
            ),
            row("H1: pin at the written place or centre?"),
            panel(
                "xychart",
                "H1: pin-to-centre vs pin-to-text km — Log-log; below y = x the pin is nearer the written place.",
                [diag, diag_ref],
                w=12,
                h=12,
                options={
                    "mapping": "manual",
                    "series": [
                        {
                            "frame": {"matcher": {"id": "byIndex", "options": 0}},
                            "x": {"matcher": {"id": "byName", "options": "km_pin_city"}},
                            "y": {"matcher": {"id": "byName", "options": "km_pin_text"}},
                            "color": {"matcher": {"id": "byName", "options": "pin_verdict"}},
                        },
                        {
                            "frame": {"matcher": {"id": "byIndex", "options": 1}},
                            "x": {"matcher": {"id": "byName", "options": "km_pin_city"}},
                            "y": {"matcher": {"id": "byName", "options": "km_pin_text"}},
                        },
                    ],
                },
                fieldConfig={"defaults": {**log_axis, "unit": "lengthkm"}, "overrides": []},
            ),
            panel(
                "table",
                "Pin → text pairs (sorted by distance)",
                flows,
                w=12,
                h=12,
                options={"cellHeight": "sm"},
                fieldConfig=ad_link,
            ),
            # a stock table with colour-background cells: the heatmap drew NOTHING, its x field was the
            # string city_label (it needs time/number), and median_km fell in as a bucket row
            panel(
                "table",
                "Distance fingerprint per label (pre-bucketed km)",
                fp,
                w=24,
                h=10,
                description="Ads per km bucket (pin → label centre), one row per label; darker = more ads.",
                options={"cellHeight": "sm"},
                fieldConfig=colour_cells("city_label", "median_km"),
            ),
            row("H6 — the text-hint extractor"),
            panel(
                "nodeGraph",
                "Hint kind → geocode precision → pin verdict",
                [funnel_nodes, funnel_edges],
                w=14,
                h=14,
            ),
            panel(
                "barchart",
                "Hints by kind — precise / coarse / not geocoded (IN-SAMPLE ONLY: no held-out labels)",
                h6,
                w=10,
                h=14,
                options={
                    "orientation": "horizontal",
                    "stacking": "normal",
                    "xField": "kind\\status",
                    "showValue": "auto",
                },
                transformations=to_matrix("kind", "status", "n"),
            ),
            row("Coverage and sources"),
            _geomap(
                "Coverage per label centre — size = cards, colour = pin rate; weighted = [depth sample] only",
                [cov],
                [_markers("Label centres", "A", color="pin_rate", size=10, text="city_label")],
                w=12,
                h=12,
                fieldConfig={
                    "defaults": {"unit": "percentunit", "color": {"mode": "continuous-blues"}},
                    "overrides": [],
                },
            ),
            _geomap(
                "listing-lens pins vs phone-session pins (badged: separate import)",
                [li_pins, ph_pins, both_grey],
                [
                    grey_layer("C"),
                    _markers("listing-lens", "A", size=5),
                    {
                        **_markers("phone-session", "B", size=6, symbol="triangle"),
                        "config": {
                            "style": {
                                "size": {"fixed": 6},
                                "color": {"fixed": "orange"},
                                "opacity": 0.7,
                                "symbol": {"mode": "fixed", "fixed": "img/icons/marker/triangle.svg"},
                            },
                            "showLegend": True,
                        },
                    },
                ],
                w=12,
                h=12,
            ),
            panel(
                "table",
                "Ads in BOTH sources — km between the two pins",
                both,
                w=24,
                h=6,
                options={"cellHeight": "sm"},
            ),
            panel(
                "barchart",
                "Which route listing-lens takes for a text point — ads per automatic geocode decision, FE vs TS",
                POINT_ROUTE_SQL,
                w=12,
                h=8,
                description="The listing-lens AUTOMATIC route for a precise text point (derive's pre-registered 5 km rule, read from "
                'geocode_decision, never re-implemented here). "no precise text point" = no decision; '
                '"gold (masked)" = eval-gold ads, whose answer the board never shows (C19).',
                options={"xField": "decision", "orientation": "horizontal", "stacking": "none"},
                fieldConfig={"defaults": {"min": 0}, "overrides": []},
            ),
            panel(
                "table",
                "Point decision ledger — who decided, by precedence",
                POINT_LEDGER_SQL,
                w=12,
                h=8,
                description="The APPLIED point per ad, by precedence manual (operator) > machine (place-check) > "
                "derive > bare pin; the review queue and stale decisions are listed beside the applied ones. "
                '"expired (TTL)" = a Google point dropped after the 30-day coordinate TTL; "none" = no point.',
                options={"cellHeight": "sm"},
                fieldConfig={
                    "defaults": {},
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "ads"},
                            "properties": [
                                {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "basic"}},
                                {"id": "min", "value": 0},
                                {"id": "color", "value": {"mode": "continuous-blues"}},
                            ],
                        }
                    ],
                },
            ),
            row("H3 — phone-session import (pre-listing-lens, separate source)"),
            panel(
                "xychart",
                "H3: pin-to-centre vs pin-to-target km — PHONE-SESSION import; a rising band would support H3. km_to_target is that session's target point; points beyond 60 km on either axis are clipped.",
                [H3_SQL, "SELECT x::float AS d_city, x::float AS km_to_target FROM (VALUES (0), (60)) d(x)"],
                w=16,
                h=10,
                options={
                    "mapping": "manual",
                    "series": [
                        {
                            "frame": {"matcher": {"id": "byIndex", "options": 0}},
                            "x": {"matcher": {"id": "byName", "options": "d_city"}},
                            "y": {"matcher": {"id": "byName", "options": "km_to_target"}},
                            "color": {"matcher": {"id": "byName", "options": "city_filter"}},
                        },
                        {
                            "frame": {"matcher": {"id": "byIndex", "options": 1}},
                            "x": {"matcher": {"id": "byName", "options": "d_city"}},
                            "y": {"matcher": {"id": "byName", "options": "km_to_target"}},
                        },
                    ],
                },
                fieldConfig={"defaults": {"unit": "lengthkm"}, "overrides": []},
            ),
        ],
        variables=[
            subcategory_var(),
            query_var(
                "city_label",
                "SELECT DISTINCT city_label FROM listings_masked WHERE city_label IS NOT NULL ORDER BY 1",
            ),
            query_var(
                "verdict",
                "SELECT DISTINCT pin_verdict FROM listings_masked WHERE pin_verdict IS NOT NULL ORDER BY 1",
            ),
            out_of_region_var(),
            oor_line_var(phone=True),
            custom_var("precise_only", ["false", "true"]),
            custom_var("distance_field", ["pin", "text"]),
            custom_var("weighting", ["weighted", "raw"]),
            query_var("viewpoint", "SELECT label FROM viewpoint ORDER BY 1", multi=False, include_all=False),
            custom_var("ring_step", ["5", "1", "2", "10"], label="ring step (km)"),
            custom_var("ring_count", ["4", "1", "2", "3", "5", "6"], label="rings"),
            query_var(
                "city_filter",
                "SELECT DISTINCT city_filter FROM import_phone_ads WHERE city_filter IS NOT NULL ORDER BY 1",
            ),
        ],
        links=[f"{PFX}-explorer", f"{PFX}-advertiser", f"{PFX}-overview"],
        description="Where pins sit vs labels, written places and the region box: evidence for H1, H2, H6.",
    )


BOARDS.append(geography)


# ---------------------------------------------------------------- process

HOTLINK_NOTE = (
    "**Not in these counters:** any request made outside listing-lens. These boards add none: they fetch "
    "no photo or page from the site (media closed, enforced by the no-images guard), and an ad "
    "link opens only on a click, with no Referer. The two `run_correction` rows (reconciled "
    "counters) are in the events table: pick kind = run_correction; `record` holds the fix."
)


def process() -> dict:
    runf = " AND ".join(
        [
            in_filter("r.run_id", "run_id"),
            in_filter("r.variant", "variant"),
            in_filter("r.command", "command"),
        ]
    )
    runs = f"""
SELECT r.run_id, r.command, r.variant, r.status, r.created_at AS started,
       e.ended, round((extract(epoch FROM (e.ended - r.created_at)) / 60)::numeric, 1)::float AS minutes,
       coalesce(r.requests_list_pages, 0) + coalesce(r.requests_ad_pages, 0) AS pages,
       e.challenges
FROM runs r
LEFT JOIN LATERAL (
  SELECT greatest(max(l.at), max(ev.at)) AS ended,
         count(*) FILTER (WHERE ev.kind = 'challenge')::int AS challenges
  FROM run_logs l FULL JOIN events ev ON false
  WHERE l.run_id = r.run_id OR ev.run_id = r.run_id) e ON true
WHERE {runf}
ORDER BY r.created_at"""
    events = f"""
SELECT ev.at AS time, ev.kind, ev.run_id, ev.reason, ev.url, ev.record::text AS record
FROM events ev LEFT JOIN runs r USING (run_id)
WHERE {runf} AND {in_filter("ev.kind", "kind")}
ORDER BY ev.at"""
    ab = """
SELECT a.subcategory, a.variant, a.cards, a.pins_on_pages, a.stopped #>> '{}' AS stopped,
       a.ab_at, (SELECT round(avg(value::float)::numeric, 2) FROM jsonb_each_text(a.coverage)
                 WHERE value ~ '^[0-9.]+$')::float AS mean_field_coverage
FROM ab_variants a
ORDER BY a.subcategory, a.variant, a.ab_at"""
    sweep = f"""
SELECT round((extract(epoch FROM (l.at - min(l.at) OVER (PARTITION BY l.run_id))) / 60)::numeric, 2)::float
         AS minutes_since_start,
       row_number() OVER (PARTITION BY l.run_id ORDER BY l.at)::int AS request_n,
       l.ms, l.status, l.run_id
FROM run_logs l LEFT JOIN runs r USING (run_id)
WHERE l.kind = 'list' AND {runf}
ORDER BY l.run_id, l.at"""
    latency = f"""
SELECT l.at AS time, l.ms
FROM run_logs l LEFT JOIN runs r USING (run_id)
WHERE l.ms IS NOT NULL AND $__timeFilter(l.at) AND {runf} AND {in_filter("l.kind", "log_kind")}
ORDER BY l.at"""
    ladder = f"""
SELECT r.run_id,
       coalesce(r.requests_list_pages, 0) AS list_pages, coalesce(r.requests_ad_pages, 0) AS ad_pages,
       coalesce(r.requests_robots, 0) AS robots, coalesce(r.requests_redirects, 0) AS redirects,
       coalesce(r.requests_site_subresources, 0) AS site_subresources,
       coalesce(r.requests_third_party, 0) AS third_party, coalesce(r.requests_other, 0) AS other,
       coalesce(r.requests_site_subresources_blocked, 0) AS subresources_blocked,
       coalesce(r.requests_third_party_blocked, 0) AS third_party_blocked,
       coalesce(r.requests_failed_before_request, 0) AS failed_before_request
FROM runs r WHERE {runf} ORDER BY r.created_at"""
    board = dashboard(
        f"{PFX}-process",
        "Process (runs, A/B, requests)",
        [
            panel(
                "table",
                "Runs — start, end, minutes, pages, challenges",
                runs,
                w=24,
                h=9,
                options={"cellHeight": "sm"},
            ),
            panel(
                "table",
                "Events (challenge / cooldown / correction …)",
                events,
                w=12,
                h=9,
                options={"cellHeight": "sm"},
            ),
            panel(
                "table",
                "H5 — A/B variant × subcategory (cards, pins, stop reason)",
                ab,
                w=12,
                h=9,
                options={"cellHeight": "sm"},
                fieldConfig={
                    "defaults": {},
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "cards"},
                            "properties": [
                                {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                                {"id": "color", "value": {"mode": "continuous-GrYlRd"}},
                            ],
                        }
                    ],
                },
            ),
            row("Requests over a run"),
            panel(
                "xychart",
                "Crawl sweep by minutes since start — List request n vs minutes since the run started; never blank under any time range.",
                sweep,
                w=12,
                h=10,
                options={
                    "mapping": "auto",
                    "series": [
                        {
                            "x": {"matcher": {"id": "byName", "options": "minutes_since_start"}},
                            "y": {"matcher": {"id": "byName", "options": "request_n"}},
                        }
                    ],
                },
            ),
            panel(
                "heatmap",
                "Request latency over time (ms, log buckets) — ${log_kind}",
                latency,
                w=12,
                h=10,
                options={
                    "calculate": True,
                    "calculation": {"yBuckets": {"scale": {"type": "log", "log": 2}}},
                    "color": {"mode": "scheme", "scheme": "Oranges", "scale": "exponential", "exponent": 0.5},
                },
                targets=[{**target(latency), "format": "time_series"}],
            ),
            row("Request ladder"),
            panel(
                "barchart",
                "Recorded request counters per run (stacked)",
                ladder,
                w=16,
                h=10,
                options={
                    "xField": "run_id",
                    "stacking": "normal",
                    "orientation": "horizontal",
                    "legend": {"showLegend": True},
                },
            ),
            markdown("What the ladder cannot see", HOTLINK_NOTE, w=8, h=5),
        ],
        variables=[
            query_var("run_id", "SELECT run_id FROM runs ORDER BY created_at DESC"),
            query_var("variant", "SELECT DISTINCT variant FROM runs WHERE variant IS NOT NULL ORDER BY 1"),
            query_var("command", "SELECT DISTINCT command FROM runs ORDER BY 1"),
            query_var("kind", "SELECT DISTINCT kind FROM events ORDER BY 1"),
            query_var("log_kind", "SELECT DISTINCT kind FROM run_logs WHERE kind IS NOT NULL ORDER BY 1"),
        ],
        links=[f"{PFX}-overview", f"{PFX}-content"],
        description="How the data was collected: runs, the H5 A/B, request timing and the counters.",
    )
    board["time"] = {"from": "now-30d", "to": "now"}
    board["annotations"]["list"].append(
        {
            "name": "challenges / cooldowns",
            "datasource": model.DS,
            "enable": True,
            "iconColor": "red",
            "target": {
                "rawSql": "SELECT at AS time, kind || ': ' || coalesce(reason, '') AS text, kind AS tags "
                "FROM events WHERE kind IN ('challenge', 'cooldown', 'cooldown_cleared') "
                "AND $__timeFilter(at)",
                "format": "table",
                "rawQuery": True,
                "refId": "Anno",
            },
        }
    )
    return board


BOARDS.append(process)


# ---------------------------------------------------------------- time

SAMPLE_W = DEPTH_SAMPLE
AGE_D = "(extract(epoch FROM (l.fetched_at - l.first_published)) / 86400)"


H8_SQL = f"""
SELECT ds.decile::text AS decile,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM (l.fetched_at - l.bumped_at)) / 86400)::float AS median_days_since_bump,
       count(*)::int AS ads
FROM listings_masked l JOIN observations o USING (site, ad_id) JOIN {DEPTH_SAMPLE} ds USING (site, ad_id)
WHERE l.bumped_at IS NOT NULL AND {in_filter("o.list_subcategory", "subcategory")}
GROUP BY ds.decile ORDER BY ds.decile"""
H12_SQL = f"""
WITH d AS (
  SELECT extract(epoch FROM (l.bumped_at - l.first_published)) AS s
  FROM listings_masked l JOIN observations o USING (site, ad_id)
  WHERE l.bumped_at IS NOT NULL AND l.first_published IS NOT NULL AND {in_filter("o.list_subcategory", "subcategory")})
SELECT band AS lag_band, (avg((abs(s - 3600 * round(s / 3600)) <= 120)::int))::float AS aligned_share, count(*)::int AS bumps
FROM (SELECT s, CASE WHEN s < 86400 THEN '1: 0-24 h' WHEN s < 7 * 86400 THEN '2: 1-7 d' ELSE '3: > 7 d' END AS band FROM d) b
GROUP BY band ORDER BY band"""


def time_board() -> dict:
    sub = in_filter("o.list_subcategory", "subcategory")
    # weighted = the depth sample's design weights only; other ads carry NULL, which sum() skips
    w = "CASE '${weighted:raw}' WHEN 'yes' THEN sm.weight ELSE 1 END"
    fetched = f"""FROM listings_masked l JOIN observations o USING (site, ad_id)
LEFT JOIN {SAMPLE_W} sm USING (site, ad_id)
WHERE {sub} AND l.first_published IS NOT NULL"""

    clock = f"""
WITH k AS (
  SELECT l.ad_id::bigint AS id, (extract(epoch FROM l.first_published) / 86400)::float AS pub_day,
         o.list_subcategory AS subcategory
  {fetched}),
fit AS (SELECT regr_slope(pub_day, id) AS b, regr_intercept(pub_day, id) AS a, regr_r2(pub_day, id) AS r2 FROM k)
SELECT k.id AS ad_id, k.pub_day, (fit.a + fit.b * k.id)::float AS fitted,
       (k.pub_day - (fit.a + fit.b * k.id))::float AS resid_days, k.subcategory, fit.r2::float AS r2
FROM k, fit ORDER BY k.id"""
    clock_stats = f"""
WITH k AS (SELECT l.ad_id::bigint AS id, extract(epoch FROM l.first_published) / 86400 AS d {fetched})
SELECT regr_r2(d, id)::float AS r2, (regr_slope(d, id) * 1e6)::float AS days_per_million_ids,
       count(*)::int AS n,
       (SELECT count(*) FROM (SELECT d - lag(d) OVER (ORDER BY id) AS step FROM k) s WHERE step < 0)::int AS inversions
FROM k"""

    age_buckets = [
        ("0-7 d", 0, 7),
        ("7-30 d", 7, 30),
        ("30-90 d", 30, 90),
        ("90-180 d", 90, 180),
        ("180-365 d", 180, 365),
        ("1-2 y", 365, 730),
        ("2+ y", 730, 100000),
    ]
    heat_cols = ", ".join(
        f'sum({w}) FILTER (WHERE {AGE_D} >= {lo} AND {AGE_D} < {hi})::float AS "{name}"'
        for name, lo, hi in age_buckets
    )
    heat = f"""
SELECT o.depth_decile::text AS decile, {heat_cols}
{fetched}
GROUP BY o.depth_decile ORDER BY o.depth_decile"""

    decay = f"""
SELECT ((o.list_page - 1) * pp.per_page + o.list_position)::int AS global_rank,
       o.ad_id::bigint AS ad_id, o.list_subcategory AS subcategory, o.list_sample_weight
FROM observations o
JOIN (SELECT crawl_id, list_subcategory, max(list_position) AS per_page
      FROM observations GROUP BY 1, 2) pp USING (crawl_id, list_subcategory)
WHERE {sub}
ORDER BY global_rank"""

    surv = f"""
WITH a AS (SELECT o.list_subcategory AS subcategory, {AGE_D} AS age_d, {w} AS w {fetched})
SELECT round(age_d::numeric, 1)::float AS age_days, subcategory,
       (1 - sum(w) OVER (PARTITION BY subcategory ORDER BY age_d)
            / sum(w) OVER (PARTITION BY subcategory))::float AS share_older
FROM a ORDER BY subcategory, age_d"""

    cohort = f"""
SELECT to_char(date_trunc('month', l.first_published), 'YYYY-MM') AS cohort, o.list_subcategory AS list,
       sum({w})::float AS ads
{fetched}
GROUP BY 1, 2 ORDER BY 1, 2"""

    bump = f"""
SELECT greatest({AGE_D}, 0.01)::float AS age_days,
       greatest(extract(epoch FROM (l.fetched_at - l.bumped_at)) / 86400, 0.01)::float AS days_since_bump,
       o.depth_decile, l.ad_id, l.advertiser_id, l.title
{fetched} AND l.bumped_at IS NOT NULL"""
    diag = "SELECT x::float AS age_days, x::float AS days_since_bump FROM (VALUES (0.01), (3000.0)) d(x)"

    phone = """
WITH p AS (
  SELECT (record->'card'->>'page')::int AS page,
         extract(epoch FROM (max(bumped_at) OVER () - bumped_at)) / 60 AS mins
  FROM import_phone_ads WHERE bumped_at IS NOT NULL)
SELECT page::text AS page,
       count(*) FILTER (WHERE mins < 60)::int AS "<1 h", count(*) FILTER (WHERE mins >= 60 AND mins < 180)::int AS "1-3 h",
       count(*) FILTER (WHERE mins >= 180 AND mins < 360)::int AS "3-6 h",
       count(*) FILTER (WHERE mins >= 360 AND mins < 720)::int AS "6-12 h",
       count(*) FILTER (WHERE mins >= 720)::int AS "12 h+"
FROM p GROUP BY page ORDER BY page::int"""

    log = {"custom": {"scaleDistribution": {"type": "log", "log": 10}}}
    xy = lambda x, y, c=None, frame=0: {  # noqa: E731
        "frame": {"matcher": {"id": "byIndex", "options": frame}},
        "x": {"matcher": {"id": "byName", "options": x}},
        "y": {"matcher": {"id": "byName", "options": y}},
        **({"color": {"matcher": {"id": "byName", "options": c}}} if c else {}),
    }
    return dashboard(
        f"{PFX}-time",
        "Time (age, bump staleness, bumps — a cross-section, not a series)",
        [
            markdown(
                "Read first",
                "One snapshot: **no panel here is a per-ad calendar series** (needs=second-crawl). The time "
                "axes are ad **age**, list **depth = days since last bump** (H8: Spearman ρ 0.99 per list, vs ≈0.65 with age), **ad_id** (a publish clock — calibrated below first) "
                "and the ad page's **bumped_at**. first_published is the site's Pacific value and bumped_at "
                f"the listing-lens {LOCAL_TIME_PLACE} parse; both are stored as instants, and both cross the 2026-11-02 DST switch.",
                h=4,
            ),
            panel(
                "xychart",
                "T02: ad_id as a publish clock — With the linear fit.",
                [clock],
                w=16,
                h=10,
                options={
                    "mapping": "manual",
                    "series": [xy("ad_id", "pub_day", "subcategory"), xy("ad_id", "fitted")],
                },
            ),
            panel(
                "stat",
                "Clock calibration",
                clock_stats,
                w=8,
                h=10,
                options={"colorMode": "none", "graphMode": "none", "textMode": "value_and_name"},
            ),
            panel(
                "xychart",
                "T02 — residuals (days) vs ad_id",
                clock,
                w=24,
                h=6,
                options={"mapping": "manual", "series": [xy("ad_id", "resid_days", "subcategory")]},
            ),
            row("Age × list depth (≈ bump staleness)"),
            panel(
                "table",
                "T01 — age bucket × list-depth decile (depth ≈ days since last bump; weighting: ${weighted}; weighted = [depth sample] only)",
                heat,
                w=12,
                h=10,
                description="Cards per age bucket (columns) and list-depth decile (rows); darker = more.",
                options={"cellHeight": "sm"},
                fieldConfig=colour_cells("decile", scheme="continuous-blues", decimals=0),
            ),
            panel(
                "xychart",
                "T05: list rank vs ad_id — All cards; sorted by age would be a falling diagonal.",
                decay,
                w=12,
                h=10,
                options={"mapping": "manual", "series": [xy("global_rank", "ad_id", "subcategory")]},
            ),
            row("Survival and cohorts (cross-sections)"),
            panel(
                "xychart",
                "T04: share of ads older than X — NOT true survival until a second crawl.",
                surv,
                w=12,
                h=10,
                # one ascending frame per list (Partition by values), one line per frame: the trend panel
                # takes ONE frame ("Only one frame is supported") and in one frame x restarts at TS
                # ("Values must be in ascending order") — drawn-content sweep 2026-11-08
                options={
                    "mapping": "manual",
                    "series": [
                        {
                            "frame": {"matcher": {"id": "byIndex", "options": i}},
                            "x": {"matcher": {"id": "byName", "options": "age_days"}},
                            "y": {"matcher": {"id": "byName", "options": "share_older"}},
                        }
                        for i in (0, 1)
                    ],
                },
                fieldConfig={
                    "defaults": {
                        **log,
                        "unit": "percentunit",
                        "custom": {**log.get("custom", {}), "show": "lines"},
                    },
                    "overrides": [],
                },
                transformations=[
                    {"id": "partitionByValues", "options": {"fields": ["subcategory"], "keepFields": False}}
                ],
            ),
            panel(
                "barchart",
                "T07 — publish-month cohorts still listed today",
                cohort,
                w=12,
                h=10,
                options={"xField": "cohort\\list", "stacking": "normal", "xTickLabelRotation": -45},
                transformations=to_matrix("cohort", "list", "ads"),
            ),
            row("Bumps"),
            panel(
                "xychart",
                "H12 candidate: scheduled day-1 bumps at whole-hour lags — T06 age vs days since last bump, log-log",
                [bump, diag],
                w=12,
                h=10,
                options={
                    "mapping": "manual",
                    "series": [
                        xy("age_days", "days_since_bump", "depth_decile"),
                        xy("age_days", "days_since_bump", frame=1),
                    ],
                },
                fieldConfig={"defaults": {**log, "links": [explorer_link("ad_id")]}, "overrides": []},
            ),
            panel(
                "heatmap",
                "T12: phone-session bump decay by page — Separate import (pre-listing-lens, FE only): import page × minutes since bump.",
                phone,
                w=12,
                h=10,
                options={"calculate": False, "color": {"mode": "scheme", "scheme": "Purples"}},
            ),
            row("H8 / H12 candidates"),
            panel(
                "barchart",
                "H8 candidate: list depth IS bump staleness — [depth sample]: median days since last bump by its decile",
                H8_SQL,
                w=12,
                h=9,
                options={"xField": "decile", "showValue": "always"},
                fieldConfig={"defaults": {"unit": "d", "decimals": 1}, "overrides": []},
            ),
            panel(
                "barchart",
                "H12 candidate: whole-hour bump share by lag — Share of bumps within ±2 min of a whole-hour lag after publish, by lag band; chance ≈ 6.7%.",
                H12_SQL,
                w=12,
                h=9,
                options={"xField": "lag_band", "showValue": "always"},
                fieldConfig={"defaults": {"unit": "percentunit", "min": 0, "max": 1}, "overrides": []},
            ),
        ],
        variables=[subcategory_var(), custom_var("weighted", ["yes", "no"])],
        links=[f"{PFX}-overview", f"{PFX}-explorer", f"{PFX}-process"],
        description="Age, bump staleness (list depth), the ad_id publish clock and bumps — cross-sections of one snapshot.",
    )


BOARDS.append(time_board)


# ---------------------------------------------------------------- content


H13_SQL = """
SELECT o.list_subcategory AS list, coalesce(l.service_mode::text, '(none)') AS service_mode, count(*)::int AS ads,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY o.rate)::float AS median_rate,
       mode() WITHIN GROUP (ORDER BY o.rate)::int AS modal_rate,
       (count(*) FILTER (WHERE o.rate = 160)::float / nullif(count(o.rate), 0)) AS share_at_160
FROM listings_masked l JOIN observations o USING (site, ad_id)
WHERE o.rate IS NOT NULL
GROUP BY 1, 2 HAVING count(*) >= 5 ORDER BY 1, ads DESC"""


def content() -> dict:
    sub_o = in_filter("o.list_subcategory", "subcategory")
    w = "CASE '${weighted:raw}' WHEN 'yes' THEN coalesce(o.list_sample_weight, 1) ELSE 1 END"
    lo = "FROM listings_masked l JOIN observations o USING (site, ad_id)"

    p1 = f"""
WITH t AS (SELECT o.rate, o.list_subcategory AS subcategory, {w} AS w
           FROM observations o WHERE o.rate IS NOT NULL AND {sub_o})
SELECT rate::text AS price_point, subcategory AS list,
       (sum(w) / sum(sum(w)) OVER (PARTITION BY subcategory))::float AS share
FROM t GROUP BY rate, subcategory ORDER BY rate, subcategory"""

    p2 = f"""
SELECT o.card_age AS stated_age,
       percentile_cont(0.10) WITHIN GROUP (ORDER BY o.rate)::float AS p10,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY o.rate)::float AS p50,
       percentile_cont(0.90) WITHIN GROUP (ORDER BY o.rate)::float AS p90,
       count(*)::int AS n
FROM observations o
WHERE o.rate IS NOT NULL AND o.card_age < 99 AND {in_filter("o.list_subcategory", "subcategory")}
GROUP BY o.card_age ORDER BY o.card_age"""

    p3 = f"""
SELECT l.first_published AS time, o.rate::float AS rate
{lo} WHERE o.rate IS NOT NULL AND $__timeFilter(l.first_published) AND {sub_o}
ORDER BY 1"""

    p4 = f"""
SELECT ${{dimension:raw}} AS grp, o.verified, o.list_subcategory AS list,
       round(sum({w})::numeric)::float AS n,
       (sum({w}) FILTER (WHERE o.rate IS NOT NULL) / sum({w}))::float AS share_rate_stated,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY o.rate)::float AS median_rate,
       (percentile_cont(0.75) WITHIN GROUP (ORDER BY o.rate) - percentile_cont(0.25) WITHIN GROUP (ORDER BY o.rate))::float AS iqr
FROM observations o WHERE {sub_o}
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"""

    p5a = f"""
SELECT o.card_city_label AS city_label, c.lat, c.lon, count(o.rate)::int AS n_rate,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY o.rate)::float AS median_rate,
       (count(o.rate)::float / count(*)) AS share_stated
FROM observations o
JOIN (SELECT city_centre_label AS city_label, min(city_centre_lat) AS lat, min(city_centre_lon) AS lon
      FROM listings_masked WHERE city_centre_lat IS NOT NULL GROUP BY 1) c ON c.city_label = o.card_city_label
WHERE {sub_o} GROUP BY 1, 2, 3"""
    p5b = f"""
SELECT l.ad_id, l.title, l.source_url, l.pin_lat AS lat, l.pin_lon AS lon, o.rate::float AS median_rate, l.city_label
{lo} WHERE l.pin_lat IS NOT NULL AND o.rate IS NOT NULL AND {sub_o} AND {pin_in()}"""
    p5_grey = f"""
SELECT l.ad_id, l.title, l.source_url, l.pin_lat AS lat, l.pin_lon AS lon, o.rate::float AS rate, l.city_label,
       l.location_invalid_reason
{lo} WHERE l.pin_lat IS NOT NULL AND o.rate IS NOT NULL AND {sub_o} AND {pin_flagged()}"""

    t2 = f"""
SELECT length(l.description) AS desc_len, l.n_media AS n_photos, o.verified::int AS verified,
       coalesce(o.rate, 0) AS rate, o.list_subcategory AS subcategory, l.ad_id
{lo} WHERE {sub_o}"""
    t3 = f"""
WITH f AS (
  SELECT l.*, md5(regexp_replace(lower(left(l.description, 160)), '\\s+', ' ', 'g')) AS family
  {lo} WHERE {sub_o})
SELECT left(family, 10) AS family, count(*)::int AS n_ads, count(DISTINCT advertiser_id)::int AS n_advertisers,
       count(DISTINCT city_label)::int AS n_cities, string_agg(DISTINCT city_label, ', ') AS cities,
       string_agg(DISTINCT age::text, ', ') AS ages, string_agg(DISTINCT ethnicity, ', ') AS ethnicities,
       min(first_published) AS first_pub, max(first_published) AS last_pub,
       min(left(description, 160)) AS excerpt, min(ad_id) AS ad_id
FROM f GROUP BY family HAVING count(*) >= 2
ORDER BY n_advertisers DESC, n_ads DESC"""

    t1 = f"""
WITH tok AS (
  SELECT l.ad_id, o.list_subcategory AS subcategory, coalesce(l.advertiser_id, l.ad_id) AS who, t.w, t.i
  {lo}
  CROSS JOIN LATERAL regexp_split_to_table(lower(coalesce(${{field:raw}}, '')), '[^[:alpha:]'']+') WITH ORDINALITY t(w, i)
  WHERE t.w <> '' AND {sub_o}),
bi AS (SELECT subcategory, who, w || ' ' || lead(w) OVER (PARTITION BY ad_id ORDER BY i) AS bg FROM tok),
c AS (
  SELECT bg, count(DISTINCT who) FILTER (WHERE subcategory = '{LIST_A}') AS fe,
             count(DISTINCT who) FILTER (WHERE subcategory = '{LIST_B}') AS ts
  FROM bi WHERE bg IS NOT NULL GROUP BY bg HAVING count(DISTINCT who) >= ${{min_advertisers:raw}}),
s AS (SELECT bg, fe, ts, sum(fe) OVER () AS nfe, sum(ts) OVER () AS nts, count(*) OVER () AS v FROM c),
d AS (
  SELECT bg, fe::int, ts::int,
         (ln((ts + 0.5) / (nts + 0.5 * v - ts - 0.5)) - ln((fe + 0.5) / (nfe + 0.5 * v - fe - 0.5)))
           / sqrt(1.0 / (ts + 0.5) + 1.0 / (fe + 0.5)) AS z
  FROM s)
SELECT bg AS bigram, z::float AS "z ({SHORT_B} > 0 > {SHORT_A})", fe AS fe_advertisers, ts AS ts_advertisers
FROM (SELECT * FROM d ORDER BY z DESC LIMIT 20) a
UNION ALL SELECT * FROM (SELECT bg, z::float, fe, ts FROM d ORDER BY z ASC LIMIT 20) b
ORDER BY 2 DESC"""

    t4 = f"""
WITH w AS (
  SELECT coalesce(l.ethnicity, '(none)') AS ethnicity,
    (SELECT count(*) FROM regexp_matches(lower(l.description), '\\m(and|the|with|you)\\M', 'g')) AS en,
    (SELECT count(*) FROM regexp_matches(lower(l.description), '\\m(y|con|para|muy|soy)\\M', 'g')) AS es,
    (SELECT count(*) FROM regexp_matches(lower(l.description), '\\m(et|avec|pour|très|je)\\M', 'g')) AS fr,
    (SELECT count(*) FROM regexp_matches(lower(l.description), '\\m(com|muito|sou)\\M', 'g')) AS pt
  {lo} WHERE {sub_o}),
c AS (
  SELECT ethnicity, CASE WHEN es >= 3 AND en >= 3 THEN 'mixed'
                         WHEN greatest(es, fr, pt) > en THEN CASE greatest(es, fr, pt) WHEN es THEN 'spanish'
                                                                WHEN fr THEN 'french' ELSE 'portuguese' END
                         ELSE 'english' END AS written FROM w)
SELECT ethnicity,
       count(*) FILTER (WHERE written = 'english')::int AS english, count(*) FILTER (WHERE written = 'spanish')::int AS spanish,
       count(*) FILTER (WHERE written = 'french')::int AS french, count(*) FILTER (WHERE written = 'portuguese')::int AS portuguese,
       count(*) FILTER (WHERE written = 'mixed')::int AS mixed
FROM c GROUP BY 1 ORDER BY count(*) DESC"""
    t4_decl = f"""
SELECT coalesce(lang, '(none declared)') AS declared_language, count(*)::int AS ads
{lo} LEFT JOIN LATERAL jsonb_array_elements_text(coalesce(l.languages, '[]'::jsonb)) lang ON true
WHERE {sub_o} GROUP BY 1 ORDER BY 2 DESC"""

    deciles = ", ".join(
        f'(sum(c.w) FILTER (WHERE top.tok = ANY(c.toks) AND c.decile = {d}) / nullif(sum(c.w) FILTER (WHERE c.decile = {d}), 0))::float AS "d{d}"'
        for d in range(10)
    )
    t5 = f"""
WITH c AS (
  SELECT {w} AS w, o.depth_decile AS decile, regexp_split_to_array(lower(coalesce(o.card_title, '')), '[^[:alnum:]]+') AS toks
  FROM observations o WHERE {sub_o}),
top AS (SELECT t AS tok FROM c, unnest(toks) t WHERE length(t) >= 3 GROUP BY t ORDER BY sum(w) DESC LIMIT 25)
SELECT top.tok AS token, {deciles}
FROM top CROSS JOIN c GROUP BY top.tok ORDER BY sum(c.w) FILTER (WHERE top.tok = ANY(c.toks)) DESC"""

    t6 = f"""
SELECT {AGE_D}::float AS age_days,
       CASE WHEN (coalesce(l.title, '') || ' ' || coalesce(l.description, ''))
                 ~* '\\m(new|just arrived|first time|brand new|nueva|nouvelle)\\M'
            THEN 'claims new' ELSE 'other' END AS series
{lo} WHERE l.first_published IS NOT NULL AND {sub_o}"""
    pct = {"defaults": {"unit": "percentunit"}, "overrides": []}
    board = dashboard(
        f"{PFX}-content",
        "Content (price, text, language, templates)",
        [
            markdown(
                "Read first",
                "Rates are a bare number (no unit on the site); 59% of cards state none. Weighted = the FE "
                "stride weight undone. Text panels group by **advertiser** where one exists, so one "
                "prolific poster cannot dominate a bigram. Photos are **not** on this board until the "
                "browser media gate passes.",
                h=3,
            ),
            panel(
                "barchart",
                "P1: price points by list — Share of rate-stating cards at each exact rate.",
                p1,
                w=24,
                h=9,
                options={"xField": "price_point\\list", "xTickLabelRotation": -60, "groupWidth": 0.8},
                transformations=to_matrix("price_point", "list", "share"),
                fieldConfig=pct,
            ),
            panel(
                "trend",
                "P2: rate by stated age, ${subcategory} — p10 / p50 / p90; ages with n < 5 hidden "
                "(Filter data by values: lower it in the panel editor). One copy per list (panel repeat).",
                p2,
                w=12,
                h=9,
                options={"xField": "stated_age"},
                transformations=min_filter("n", 5),
                repeat="subcategory",
                repeatDirection="h",
                maxPerRow=2,
            ),
            panel(
                "heatmap",
                "P3 — rate vs publish date ($25 buckets; time range 2020→now)",
                p3,
                w=12,
                h=10,
                options={
                    "calculate": True,
                    "calculation": {"yBuckets": {"mode": "size", "value": "25"}},
                    "color": {"mode": "scheme", "scheme": "Oranges", "scale": "exponential", "exponent": 0.5},
                },
                targets=[{**target(p3), "format": "time_series"}],
            ),
            panel(
                "table",
                "P4: disclosure and price by ${dimension} — Crossed with verified.",
                p4,
                w=12,
                h=10,
                options={"cellHeight": "sm"},
                fieldConfig={
                    "defaults": {},
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "share_rate_stated"},
                            "properties": [
                                {"id": "unit", "value": "percentunit"},
                                {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "gradient"}},
                                {"id": "min", "value": 0},
                                {"id": "max", "value": 1},
                            ],
                        }
                    ],
                },
            ),
            _geomap(
                "P5: median rate per label centre — And priced exact pins.",
                [p5a, p5b, p5_grey],
                [
                    _markers("Label medians", "A", color="median_rate", size=14, text="city_label"),
                    _markers("Priced pins", "B", color="median_rate", size=4),
                    grey_layer("C"),
                ],
                w=24,
                h=12,
                description=OOR_NOTE,
                fieldConfig={"defaults": {"color": {"mode": "continuous-YlRd"}}, "overrides": []},
            ),
            panel(
                "table",
                "H13 candidate: rate by service_mode — FE's $160 mode is the hourly figure of an '80 per half-hour' tier",
                H13_SQL,
                w=24,
                h=8,
                options={"cellHeight": "sm"},
            ),
            row("Services (provisional vocab)"),
            panel(
                "barchart",
                "Services offered: share of ads, FE vs TS — [depth sample], renormalised over non-eval ads.",
                f"""{SERVICES_W}
SELECT c.pref_label AS service, sw.subcategory,
       (sum(sw.w * coalesce(ac.offered, false)::int) / sum(sw.w))::float AS share
FROM sw CROSS JOIN (SELECT concept, pref_label FROM services_concepts) c
LEFT JOIN services_ad_concepts ac ON ac.site = sw.site AND ac.ad_id = sw.ad_id AND ac.concept = c.concept
WHERE c.concept IN (SELECT concept FROM services_ad_concepts WHERE offered GROUP BY 1 HAVING count(*) >= 5)
  AND {in_filter("sw.subcategory", "subcategory")}
GROUP BY 1, 2 ORDER BY sum(sum(sw.w * coalesce(ac.offered, false)::int)) OVER (PARTITION BY c.pref_label) DESC""",
                w=12,
                h=14,
                description=SERVICES_NOTE,
                options={"orientation": "horizontal", "xField": "service\\subcategory", "groupWidth": 0.8},
                transformations=to_matrix("service", "subcategory", "share"),
                # the matrix names its columns by the site's list slugs; every board says FE / TS
                fieldConfig={
                    "defaults": {"unit": "percentunit", "min": 0},
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": slug},
                            "properties": [{"id": "displayName", "value": short}],
                        }
                        for slug, short in ((LIST_A, SHORT_A), (LIST_B, SHORT_B))
                    ],
                },
            ),
            panel(
                "table",
                "Services: offered, refused, rate — Raw ad counts; the shares per list are in the bar chart beside it.",
                f"""
SELECT c.pref_label AS service, coalesce(c.broader, '') AS "group",
       count(DISTINCT (ac.site, ac.ad_id)) FILTER (WHERE ac.offered)::int AS ads_offering,
       count(DISTINCT (ac.site, ac.ad_id)) FILTER (WHERE ac.refused)::int AS ads_refusing,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY o.rate) FILTER (WHERE ac.offered AND o.rate IS NOT NULL)::float AS median_rate_when_offered,
       count(o.rate) FILTER (WHERE ac.offered)::int AS n_rate
FROM services_ad_concepts ac
JOIN services_concepts c USING (concept)
JOIN observations o USING (site, ad_id)
WHERE {in_filter("o.list_subcategory", "subcategory")}
GROUP BY 1, 2 ORDER BY ads_offering DESC""",
                w=12,
                h=14,
                description=SERVICES_NOTE,
                options={"cellHeight": "sm", "sortBy": [{"displayName": "ads_offering", "desc": True}]},
            ),
            row("Text"),
            panel(
                "xychart",
                "T2: description length vs photos per ad — The site limit is ≈ 1,000 chars (not a parser cap): watch the wall at the right edge.",
                t2,
                w=16,
                h=10,
                options={
                    "mapping": "manual",
                    "series": [
                        {
                            "frame": {"matcher": {"id": "byIndex", "options": 0}},
                            "x": {"matcher": {"id": "byName", "options": "desc_len"}},
                            "y": {"matcher": {"id": "byName", "options": "n_photos"}},
                            "color": {"matcher": {"id": "byName", "options": "verified"}},
                        }
                    ],
                },
                fieldConfig={
                    "defaults": {"links": [explorer_link("ad_id")]},
                    # the colour dimension reads the field's own colour: without one every point is grey
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "verified"},
                            "properties": [
                                {"id": "color", "value": {"mode": "thresholds"}},
                                {
                                    "id": "thresholds",
                                    "value": {
                                        "mode": "absolute",
                                        "steps": [
                                            {"color": "blue", "value": None},
                                            {"color": "orange", "value": 1},
                                        ],
                                    },
                                },
                            ],
                        }
                    ],
                },
            ),
            panel(
                "table",
                "T3: within-advertiser description templates — Same first 160 chars, normalised; never crosses advertisers because union-find links on desc.",
                t3,
                w=24,
                h=10,
                options={"cellHeight": "sm"},
                transformations=min_filter("n_ads", 2),
                fieldConfig={
                    "defaults": {},
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "ad_id"},
                            "properties": [{"id": "links", "value": [explorer_link("ad_id")]}],
                        },
                        {
                            "matcher": {"id": "byName", "options": "n_advertisers"},
                            "properties": [
                                {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                                {
                                    "id": "thresholds",
                                    "value": {
                                        "mode": "absolute",
                                        "steps": [
                                            {"color": "transparent", "value": None},
                                            {"color": "red", "value": 2},
                                        ],
                                    },
                                },
                            ],
                        },
                    ],
                },
            ),
            panel(
                "barchart",
                "T1: distinctive bigrams in ${field} — FE (z < 0) vs TS (z > 0), Fightin' Words, by advertiser.",
                t1,
                w=12,
                h=14,
                options={"xField": "bigram", "orientation": "horizontal"},
            ),
            panel(
                "barchart",
                "T4: written language by stated ethnicity — Connector-word guess.",
                t4,
                w=12,
                h=8,
                options={"xField": "ethnicity", "orientation": "horizontal", "stacking": "percent"},
            ),
            panel("table", "T4 — declared languages[]", t4_decl, w=12, h=6, options={"cellHeight": "sm"}),
            panel(
                "table",
                "T5 — top card-title tokens per list-depth decile (≈ days since last bump; all cards)",
                t5,
                w=24,
                h=12,
                options={"cellHeight": "sm"},
                fieldConfig={
                    "defaults": {
                        "unit": "percentunit",
                        "decimals": 0,
                        "color": {"mode": "continuous-BlPu"},
                        "custom": {"cellOptions": {"type": "color-background"}},
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "token"},
                            "properties": [{"id": "custom.cellOptions", "value": {"type": "auto"}}],
                        }
                    ],
                },
            ),
            panel(
                "histogram",
                "H14 candidate: stale copy — ads claiming 'new / just arrived' are OLDER than the rest (unweighted)",
                t6,
                w=24,
                h=9,
                options={
                    "bucketSize": 30,
                    "combine": False,
                    "legend": {
                        "showLegend": True,
                        "displayMode": "table",
                        "placement": "right",
                        "calcs": ["median", "count"],
                    },
                },
                transformations=[
                    {"id": "partitionByValues", "options": {"fields": ["series"], "keepFields": False}}
                ],
            ),
        ],
        variables=[
            subcategory_var(),
            query_var("viewpoint", "SELECT label FROM viewpoint ORDER BY 1", multi=False, include_all=False),
            out_of_region_var(),
            oor_line_var(),
            custom_var("weighted", ["yes", "no"]),
            custom_var(
                "dimension", ["o.card_ethnicity", "o.card_city_label", "o.card_age::text"], label="dimension"
            ),
            custom_var("field", ["l.description", "l.title", "l.tagline"]),
            custom_var("min_advertisers", ["3", "2", "5"]),
            *[
                {**query_var(n, q_, multi=False, include_all=False), "hide": 2}
                for n, q_ in (
                    ("svc_vocab", "SELECT vocab_version FROM services_run"),
                    ("svc_method", "SELECT method FROM services_run"),
                    ("svc_eval_n", "SELECT eval_ads_excluded::text FROM services_run"),
                    ("svc_mates_n", "SELECT eval_excluded_by_advertiser::text FROM services_run"),
                    (
                        "svc_depth_n",
                        f"SELECT count(*)::text FROM samples s JOIN services_ads a USING (site, ad_id) "
                        f"WHERE s.run_id = {DEPTH_RUN}",
                    ),
                    ("svc_depth_total", f"SELECT count(*)::text FROM samples s WHERE s.run_id = {DEPTH_RUN}"),
                )
            ],
        ],
        links=[f"{PFX}-overview", f"{PFX}-explorer", f"{PFX}-advertiser"],
        description="Price points, rate by age and place, text templates, distinctive language.",
    )
    return board


BOARDS.append(content)


# ---------------------------------------------------------------- advertisers


PHOTO_PAIRS = """
WITH pr AS (SELECT DISTINCT least(advertiser_id, advertiser_b) AS x, greatest(advertiser_id, advertiser_b) AS y
            FROM advertiser_links WHERE link_kind = 'photo' AND advertiser_id <> advertiser_b),
sz AS (SELECT advertiser_id, count(*) AS n FROM advertisers GROUP BY 1)
SELECT count(*)::int AS "photo pairs (v1.1)",
       count(*) FILTER (WHERE (SELECT n FROM sz WHERE sz.advertiser_id = pr.x) = 1
                           OR (SELECT n FROM sz WHERE sz.advertiser_id = pr.y) = 1)::int AS "photo, 1-ad (v1.1)"
FROM pr"""


# the 32 recruited ads (fetched with fetch_reason = recruit) — [pre-recruitment ads, clustered with recruits] excludes them. Clustering is
# global, so [pre-recruitment ads, clustered with recruits] is NOT [pre-recruitment ads, clustered alone] (recruited ads bridge old groups).
RECRUITED = "EXISTS (SELECT 1 FROM listings r WHERE r.site = a.site AND r.ad_id = a.ad_id AND r.fetch_reason = 'recruit')"
ABLATION = f"""
WITH b AS (SELECT site, ad_id, n_ads FROM advertisers_ablation WHERE variant = 'baseline'),
v AS (SELECT a.*, {RECRUITED} AS recruited FROM advertisers_ablation a)
SELECT s.ad_set, v.variant, count(DISTINCT v.advertiser_id)::int AS clusters,
       count(DISTINCT v.advertiser_id) FILTER (WHERE v.n_ads > 1)::int AS multi_ad_clusters,
       count(*) FILTER (WHERE v.n_ads <> b.n_ads)::int AS ads_whose_cluster_size_changes
FROM v JOIN b USING (site, ad_id)
CROSS JOIN (VALUES ('[all fetched]', true), ('[pre-recruitment ads, clustered with recruits]', false)) s(ad_set, with_recruited)
WHERE s.with_recruited OR NOT v.recruited
GROUP BY s.ad_set, v.variant
ORDER BY s.ad_set DESC, (v.variant = 'baseline') DESC, ads_whose_cluster_size_changes DESC"""
T3_NODESC = """
WITH f AS (
  SELECT md5(regexp_replace(lower(left(l.description, 160)), '\\s+', ' ', 'g')) AS family, x.advertiser_id, l.ad_id
  FROM listings_masked l JOIN advertisers_ablation x USING (site, ad_id) WHERE x.variant = '-desc')
SELECT left(family, 10) AS family, count(DISTINCT ad_id)::int AS ads, count(DISTINCT advertiser_id)::int AS advertisers_without_desc
FROM f GROUP BY family HAVING count(DISTINCT advertiser_id) > 1 ORDER BY 3 DESC, 2 DESC"""
T3_V2 = """
WITH f AS (
  SELECT md5(regexp_replace(lower(left(l.description, 160)), '\\s+', ' ', 'g')) AS family, l.site, l.ad_id,
         v.persona_id, v.operator_id
  FROM listings_masked l JOIN advertisers_v2 v USING (site, ad_id)),
fam AS (SELECT family FROM f GROUP BY family HAVING count(DISTINCT persona_id) > 1),
pp AS (  -- persona pairs inside a family that share a key directly (one link between their ads)
  SELECT fa.family, k.link_kind, least(fa.persona_id, fb.persona_id) AS pa, greatest(fa.persona_id, fb.persona_id) AS pb
  FROM advertiser_links k
  JOIN f fa ON fa.site = k.site AND fa.ad_id = k.ad_a
  JOIN f fb ON fb.site = k.site AND fb.ad_id = k.ad_b AND fb.family = fa.family
  WHERE fa.family IN (SELECT family FROM fam) AND fa.persona_id <> fb.persona_id
    AND k.link_kind IN ('phone4', 'photo', 'venue'))
SELECT left(f.family, 10) AS family, count(DISTINCT f.ad_id)::int AS ads,
       count(DISTINCT f.persona_id)::int AS personas_upper_bound, count(DISTINCT f.operator_id)::int AS operators,
       (SELECT count(DISTINCT k.link_key) FROM advertiser_links k JOIN f g ON g.site = k.site AND g.ad_id IN (k.ad_a, k.ad_b)
        WHERE g.family = f.family AND k.link_kind = 'phone4')::int AS shared_phone4_keys,
       (count(DISTINCT f.persona_id) * (count(DISTINCT f.persona_id) - 1) / 2)::int AS persona_pairs,
       (SELECT count(DISTINCT (pa, pb)) FROM pp WHERE pp.family = f.family AND pp.link_kind = 'phone4')::int AS pairs_sharing_phone4,
       (SELECT count(DISTINCT (pa, pb)) FROM pp WHERE pp.family = f.family AND pp.link_kind = 'photo')::int AS pairs_sharing_photo,
       (SELECT count(DISTINCT (pa, pb)) FROM pp WHERE pp.family = f.family AND pp.link_kind = 'venue')::int AS pairs_sharing_venue
FROM f WHERE f.family IN (SELECT family FROM fam)
GROUP BY f.family ORDER BY ads DESC"""
V2_SUMMARY = """
SELECT count(DISTINCT operator_id)::int AS "operators (v2)",
       count(DISTINCT persona_id)::int AS "personas (max, v2)",
       count(DISTINCT persona_id) FILTER (WHERE n_ads > 1)::int AS "multi-ad (max, v2)",
       count(DISTINCT operator_id) FILTER (WHERE level = 'operator')::int AS "multi-persona ops"
FROM advertisers_v2"""
V2_PIN_PAIRS = f"""
WITH p AS (SELECT DISTINCT ON (v.persona_id, l.ad_id) v.persona_id, l.ad_id, l.pin_lat, l.pin_lon, l.pin_geog
           FROM advertisers_v2 v JOIN listings_masked l USING (site, ad_id) WHERE l.pin_lat IS NOT NULL AND {pin_ok()}),
d AS (SELECT x.persona_id, {km("x.pin_geog", "y.pin_geog")} AS km
      FROM p x JOIN p y ON x.persona_id = y.persona_id AND x.ad_id < y.ad_id)
SELECT count(*)::int AS "pin pairs (v2)", count(*) FILTER (WHERE km < 0.05)::int AS "tied < 50 m (v2)",
       count(*) FILTER (WHERE km >= 0.05 AND km < 5)::int AS "50 m–5 km (v2)",
       count(*) FILTER (WHERE km >= 5)::int AS "over 5 km (v2)",
       count(DISTINCT persona_id)::int AS "pinned (max, v2)"
FROM d"""
V2_FAR_PAIRS_TOP = f"""
WITH p AS (SELECT DISTINCT ON (v.persona_id, v.operator_id, l.ad_id) v.persona_id, v.operator_id, l.ad_id, l.pin_lat, l.pin_lon, l.city_label, l.pin_geog
           FROM advertisers_v2 v JOIN listings_masked l USING (site, ad_id) WHERE l.pin_lat IS NOT NULL AND {pin_ok()}),
d AS (SELECT x.persona_id, x.operator_id, {km("x.pin_geog", "y.pin_geog")} AS km
      FROM p x JOIN p y ON x.persona_id = y.persona_id AND x.ad_id < y.ad_id)
SELECT d.persona_id, min(d.operator_id) AS operator_id, count(*) FILTER (WHERE d.km >= 5)::int AS pairs_over_5km,
       count(*)::int AS pairs, (SELECT count(*) FROM p WHERE p.persona_id = d.persona_id)::int AS pinned_ads,
       (SELECT count(DISTINCT (round(p.pin_lat::numeric, 4), round(p.pin_lon::numeric, 4))) FROM p WHERE p.persona_id = d.persona_id)::int AS pin_sites,
       (SELECT string_agg(DISTINCT p.city_label, ', ') FROM p WHERE p.persona_id = d.persona_id) AS labels
FROM d GROUP BY d.persona_id HAVING count(*) FILTER (WHERE d.km >= 5) > 0
ORDER BY pairs_over_5km DESC LIMIT 10"""
V2_FAR = f"""
SELECT v.persona_id, v.operator_id, count(*) FILTER (WHERE {FAR})::int AS far_pins,
       count(*) FILTER (WHERE l.pin_lat IS NOT NULL)::int AS pinned, round(max(l.km_pin_city)::numeric)::int AS max_km,
       string_agg(DISTINCT l.city_label, ', ') AS labels
FROM advertisers_v2 v JOIN listings_masked l USING (site, ad_id)
GROUP BY 1, 2 HAVING count(*) FILTER (WHERE {FAR}) > 0 ORDER BY far_pins DESC"""
V2_VENUES = f"""
SELECT l.geocode_place_id, max(l.geocode_formatted) AS venue, count(DISTINCT v.operator_id)::int AS operators,
       count(DISTINCT l.ad_id)::int AS ads, string_agg(DISTINCT l.text_hint_raw, ' | ') AS phrases
FROM advertisers_v2 v JOIN listings_masked l USING (site, ad_id)
WHERE l.geocode_place_id IS NOT NULL AND l.geocode_precise AND {text_ok()}
GROUP BY 1 HAVING count(DISTINCT v.operator_id) > 1 ORDER BY operators DESC, ads DESC"""


def advertisers_board() -> dict:
    sub = in_filter("o.list_subcategory", "subcategory")
    la = f"""FROM advertisers a
JOIN listings_masked l USING (site, ad_id)
JOIN observations o USING (site, ad_id)
WHERE {sub}"""
    one = "a.advertiser_id = ${advertiser:sqlstring}"
    km_xy = km("x.pin_geog", "y.pin_geog")

    leader = f"""
SELECT a.advertiser_id, count(DISTINCT a.ad_id)::int AS ads,
       count(DISTINCT l.city_label)::int AS city_labels, string_agg(DISTINCT l.city_label, ', ') AS cities,
       count(DISTINCT (round(l.pin_lat::numeric, 4), round(l.pin_lon::numeric, 4))) FILTER (WHERE l.pin_lat IS NOT NULL AND {pin_ok()})::int AS pin_sites,
       count(DISTINCT l.phone_masked)::int AS phones4, string_agg(DISTINCT l.name, ' / ') AS names,
       min(l.first_published) AS first_ad, max(l.first_published) AS last_ad,
       (extract(epoch FROM (max(l.first_published) - min(l.first_published))) / 86400)::int AS span_days,
       string_agg(DISTINCT o.list_subcategory, ', ') AS lists, min(a.link_kinds::text) AS link_kinds
{la} GROUP BY a.advertiser_id HAVING count(DISTINCT a.ad_id) >= 2
ORDER BY ads DESC, city_labels DESC"""

    size = f"""
WITH c AS (SELECT a.advertiser_id, min(o.list_subcategory) AS subcat, count(DISTINCT a.ad_id) AS n {la} GROUP BY 1)
SELECT n AS ads_per_advertiser, subcat AS list, count(*)::int AS clusters
FROM c GROUP BY n, subcat ORDER BY n"""
    size_cum = f"""
WITH c AS (SELECT a.advertiser_id, count(DISTINCT a.ad_id) AS n {la} GROUP BY 1)
SELECT n AS "ads_per_advertiser\\list", (sum(sum(n)) OVER (ORDER BY n) / sum(sum(n)) OVER ())::float AS cum_share_of_ads
FROM c GROUP BY n ORDER BY n"""

    sens = """
WITH k AS (SELECT advertiser_id, ad_a, ad_b, link_kind, used_for_clustering, advertiser_id <> advertiser_b AS cross_cluster
           FROM advertiser_links),
sole AS (SELECT ad_a, ad_b, min(link_kind) AS kind FROM k WHERE NOT cross_cluster AND used_for_clustering
         GROUP BY ad_a, ad_b HAVING count(DISTINCT link_kind) = 1)
SELECT k.link_kind, bool_or(k.used_for_clustering) AS clusters_on_it,
       count(*)::int AS edges,
       count(DISTINCT k.advertiser_id)::int AS clusters_touched,
       count(*) FILTER (WHERE k.cross_cluster)::int AS edges_between_clusters,
       (SELECT count(*) FROM sole WHERE sole.kind = k.link_kind)::int AS pairs_held_only_by_this_kind
FROM k GROUP BY k.link_kind ORDER BY pairs_held_only_by_this_kind DESC, edges DESC"""

    # The ad cluster / persona network (GQM G5.Q1–Q5; encodings § 3; C21). Every field is a count, a kind
    # name or an opaque id: link_key is COUNTED in a CTE but never selected (Inspect shows the whole frame).
    # Persona nodes stay a draft (D2) until the B3 blind read passes 0.9.
    # filter to the SHOWN ad clusters first: joining all ~60k link rows timed out on Neon (measured 2026-11-13)
    net_personas = f"""
WITH shown AS (SELECT operator_id FROM advertisers_v2 GROUP BY 1 HAVING count(DISTINCT persona_id) >= ${{min_personas:raw}}),
ap AS (SELECT a.site, a.ad_id, a.persona_id, a.operator_id, a.level, a.n_names, a.top_shared_key_share, l.subcategory
       FROM advertisers_v2 a JOIN shown USING (operator_id) JOIN listings_masked l USING (site, ad_id)),
p AS (SELECT persona_id, operator_id, count(DISTINCT ad_id)::int AS n_ads, max(n_names)::int AS n_names,
             max(level) AS level, max(top_shared_key_share)::float AS tsk,
             avg((subcategory = '{LIST_A}')::int)::float AS fe
      FROM ap GROUP BY 1, 2),
mixed AS (SELECT operator_id, max(fe) > 0 AND min(fe) < 1 AS both_lists FROM p GROUP BY 1)"""
    net_nodes = f"""{net_personas}
SELECT p.persona_id AS id, p.persona_id AS title, 'ad cluster ' || p.operator_id AS subtitle,
       -- text, not numbers: nodeGraph ignores fieldConfig (decimals, units), so a bare 38.00 / 6.00 said
       -- nothing; the frame itself carries the words (listy's check 2026-11-13, measured live)
       p.n_ads || CASE WHEN p.n_ads = 1 THEN ' ad' ELSE ' ads' END AS mainstat,
       p.n_names || CASE WHEN p.n_names = 1 THEN ' name' ELSE ' names' END AS secondarystat,
       p.fe AS "arc__fe", (1 - p.fe)::float AS "arc__ts", m.both_lists AS highlighted,
       CASE p.level WHEN 'operator' THEN 'ad cluster' ELSE p.level END AS detail__level, p.n_names AS detail__n_names, p.tsk AS detail__top_shared_key_share,
       '{PERSONA_GATE}' AS detail__gate, p.operator_id AS detail__ad_cluster
FROM p JOIN mixed m USING (operator_id)"""
    net_edges = f"""{net_personas},
e AS (SELECT least(x.persona_id, y.persona_id) AS s, greatest(x.persona_id, y.persona_id) AS t,
             k.link_kind, k.link_key, k.cross_subcategory
      FROM advertiser_v2_links k JOIN shown USING (operator_id)
      JOIN ap x ON x.site = k.site AND x.ad_id = k.ad_a JOIN ap y ON y.site = k.site AND y.ad_id = k.ad_b
      WHERE x.persona_id <> y.persona_id AND {in_filter("k.link_kind", "link_kind")}),
pe AS (SELECT s, t, count(DISTINCT link_kind || ':' || link_key)::int AS n_keys, bool_or(cross_subcategory) AS cross_list,
              array_agg(DISTINCT link_kind ORDER BY link_kind) FILTER (
                WHERE '${{photo_edges:raw}}' = 'include' OR link_kind <> 'image') AS kinds
       FROM e GROUP BY 1, 2),
ok AS (SELECT *, cardinality(kinds) AS n_kinds,
              kinds && ARRAY['desc', 'handle', 'image'] AS strong FROM pe WHERE kinds IS NOT NULL)
SELECT s || '>' || t AS id, s AS source, t AS target,
       replace(array_to_string(kinds, '+'), 'image', 'shared images') AS mainstat, n_keys AS secondarystat,
       n_kinds AS thickness,
       CASE WHEN kinds = ARRAY['image'] THEN 'images-only' WHEN strong THEN 'strong' ELSE 'weak pair' END AS detail__class,
       CASE WHEN kinds = ARRAY['image'] THEN '#8F8F8F' WHEN strong THEN '#3274D9' ELSE '#FF9830' END AS color,
       CASE WHEN strong AND kinds <> ARRAY['image'] THEN '' ELSE '4 4' END AS "strokeDasharray",
       'v2 edges (advertiser_v2_links)' AS detail__source
FROM ok
-- photo_edges = exclude re-applies the v2 rule without images: >= 2 kinds across FE/TS, else a strong kind or >= 2
WHERE '${{photo_edges:raw}}' = 'include' OR (CASE WHEN cross_list THEN n_kinds >= 2 ELSE strong OR n_kinds >= 2 END)"""

    # C17 (2026-11-13): persona-grain views are AGGREGATE-only: distinct counts, a label name only where a
    # cell holds >= 5 personas, gaps pooled with each persona weighing 1; no per-persona series, no click.
    persona_labels = """
WITH pl AS (SELECT a.persona_id, count(DISTINCT a.ad_id)::int AS n_ads, count(DISTINCT l.city_label)::int AS n_labels
            FROM advertisers_v2 a JOIN listings_masked l USING (site, ad_id) GROUP BY 1 HAVING count(DISTINCT a.ad_id) >= 2)"""
    multi_label = f"""{persona_labels}
SELECT CASE WHEN n_ads = 2 THEN '2 ads' WHEN n_ads <= 4 THEN '3–4 ads' WHEN n_ads <= 9 THEN '5–9 ads' ELSE '10+ ads' END AS ads,
       CASE WHEN n_labels >= 5 THEN '5+' ELSE n_labels::text END AS city_labels, count(*)::int AS personas
FROM pl GROUP BY 1, 2 ORDER BY min(n_ads), 2"""
    multi_label_names = f"""{persona_labels},
named AS (SELECT DISTINCT a.persona_id, l.city_label
          FROM advertisers_v2 a JOIN listings_masked l USING (site, ad_id) JOIN pl USING (persona_id)
          WHERE pl.n_labels >= 2 AND l.city_label IS NOT NULL)
SELECT city_label, count(*)::int AS personas FROM named GROUP BY 1 HAVING count(*) >= 5 ORDER BY 2 DESC, 1"""
    gap_buckets = """
WITH g AS (SELECT a.persona_id,
                  extract(epoch FROM (l.first_published - lag(l.first_published) OVER (
                      PARTITION BY a.persona_id ORDER BY l.first_published))) / 86400 AS days
           FROM advertisers_v2 a JOIN listings_masked l USING (site, ad_id) WHERE l.first_published IS NOT NULL),
w AS (SELECT persona_id, days, 1.0 / count(*) OVER (PARTITION BY persona_id) AS wt FROM g WHERE days IS NOT NULL)
SELECT CASE WHEN days < 1 THEN 'a: < 1 d' WHEN days < 7 THEN 'b: 1–6 d' WHEN days < 30 THEN 'c: 7–29 d'
            WHEN days < 90 THEN 'd: 30–89 d' WHEN days < 365 THEN 'e: 90–364 d' ELSE 'f: ≥ 1 y' END AS gap,
       (sum(wt) / (SELECT count(DISTINCT persona_id) FROM w))::float AS share_of_personas,
       count(DISTINCT persona_id)::int AS personas
FROM w GROUP BY 1 ORDER BY 1"""

    routes = f"""
SELECT l.pin_lat AS lat, l.pin_lon AS lon, l.first_published AS time, l.city_label, l.ad_id,
       count(*) OVER (PARTITION BY round(l.pin_lat::numeric, 4), round(l.pin_lon::numeric, 4))::int AS ads_on_pin
FROM advertisers a JOIN listings_masked l USING (site, ad_id)
WHERE {one} AND l.pin_lat IS NOT NULL AND {pin_in()}
ORDER BY l.first_published"""
    routes_grey = f"""
SELECT l.pin_lat AS lat, l.pin_lon AS lon, l.first_published AS time, l.city_label, l.ad_id, l.location_invalid_reason
FROM advertisers a JOIN listings_masked l USING (site, ad_id)
WHERE {one} AND l.pin_lat IS NOT NULL AND {pin_flagged()}"""

    PAIR_BIN = 0.25  # log10 km per bin
    pairs = f"""
WITH p AS (SELECT DISTINCT ON (a.advertiser_id, l.ad_id) a.advertiser_id, l.ad_id, l.pin_lat, l.pin_lon, l.pin_geog, l.first_published {la} AND l.pin_lat IS NOT NULL AND {pin_ok()}),
big AS (SELECT advertiser_id FROM p GROUP BY 1 ORDER BY count(*) DESC LIMIT 1),
d AS (SELECT floor(log(greatest(0.01, {km_xy})) / {PAIR_BIN}) * {PAIR_BIN} AS b,
             x.advertiser_id IN (SELECT advertiser_id FROM big) AS largest
      FROM p x JOIN p y ON x.advertiser_id = y.advertiser_id AND x.ad_id < y.ad_id
      WHERE x.first_published <> y.first_published)
-- pre-binned in SQL (xMin/xMax = Grafana's own histogram frame shape): ~20 rows, not one row per pair
-- (14,644 raw rows before; listy's check 2026-11-13); same bins as the panel's old bucketSize, offset 0
SELECT b::float AS "xMin", (b + {PAIR_BIN})::float AS "xMax",
       count(*) FILTER (WHERE largest)::int AS "largest cluster",
       count(*) FILTER (WHERE NOT largest)::int AS "all others"
FROM d GROUP BY b ORDER BY b"""
    pairs_stat = f"""
WITH p AS (SELECT DISTINCT ON (a.advertiser_id, l.ad_id) a.advertiser_id, l.ad_id, l.pin_lat, l.pin_lon, l.pin_geog {la} AND l.pin_lat IS NOT NULL AND {pin_ok()}),
d AS (SELECT x.advertiser_id, {km_xy} AS km FROM p x JOIN p y ON x.advertiser_id = y.advertiser_id AND x.ad_id < y.ad_id)
SELECT count(*)::int AS "pin pairs (v1.1)", count(*) FILTER (WHERE km < 0.05)::int AS "tied < 50 m (v1.1)",
       count(*) FILTER (WHERE km >= 0.05)::int AS "moved (v1.1)", count(DISTINCT advertiser_id)::int AS "advertisers (v1.1)"
FROM d"""

    h4 = f"""
SELECT {AGE_D}::float AS age_days, l.km_pin_text::float AS km_pin_text, a.advertiser_id AS advertiser, l.ad_id
{la} AND l.pin_lat IS NOT NULL AND l.geocode_precise AND l.km_pin_text IS NOT NULL AND {pin_ok()} AND {text_ok()}"""
    h4_stat = f"""
SELECT count(*)::int AS "H4 pin/text pairs", count(DISTINCT a.advertiser_id)::int AS "H4 independent advs"
{la} AND l.pin_lat IS NOT NULL AND l.geocode_precise AND l.km_pin_text IS NOT NULL AND {pin_ok()} AND {text_ok()}"""

    venues = f"""
SELECT l.geocode_place_id, max(l.geocode_formatted) AS venue, avg(l.geocode_lat)::float AS lat, avg(l.geocode_lon)::float AS lon,
       max(l.text_hint_kind) AS kind, count(DISTINCT a.advertiser_id)::int AS advertisers, count(DISTINCT a.ad_id)::int AS ads,
       string_agg(DISTINCT l.text_hint_raw, ' | ') AS phrases, bool_or(l.geocode_precise) AS precise
{la} AND l.geocode_place_id IS NOT NULL AND ('${{precise:raw}}' = 'all' OR l.geocode_precise)
  AND {{keep}}
GROUP BY 1 ORDER BY advertisers DESC, ads DESC"""
    venues_grey = f"""
SELECT l.ad_id, l.geocode_formatted AS venue, l.geocode_lat AS lat, l.geocode_lon AS lon, l.text_hint_kind AS kind,
       a.advertiser_id, l.location_invalid_reason
{la} AND l.geocode_lat IS NOT NULL AND {text_flagged()}"""

    phone4 = f"""
WITH c AS (SELECT DISTINCT a.advertiser_id, l.phone_masked AS p4 {la} AND l.phone_masked IS NOT NULL),
v AS (SELECT p4, count(*) AS advs FROM c GROUP BY 1)
-- observed = colliding ADVERTISER PAIRS; null = C(m,2)/10^4 over m advertiser-phone combos (analyst, 2026-11-08)
SELECT (SELECT count(*) FROM v WHERE advs > 1)::int AS "shared phone4",
       (SELECT sum(advs * (advs - 1) / 2) FROM v)::int AS "colliding adv pairs",
       (SELECT count(*) FROM c)::int AS "adv × phone4 (m)",
       round(((SELECT count(*) FROM c) * ((SELECT count(*) FROM c) - 1) / 2.0 / 10000)::numeric, 2)::float AS "null pairs (chance)"
"""

    fam = f"""
WITH f AS (SELECT card_hash, count(*) AS n FROM observations GROUP BY 1)
SELECT CASE WHEN f.n = 1 THEN '1' WHEN f.n = 2 THEN '2' WHEN f.n <= 5 THEN '3-5' WHEN f.n <= 10 THEN '6-10' ELSE '11+' END AS family_size,
       {", ".join(f'sum(coalesce(o.list_sample_weight, 1)) FILTER (WHERE o.depth_decile = {d})::float AS "d{d}"' for d in range(10))}
FROM observations o JOIN f USING (card_hash) WHERE {sub}
GROUP BY 1 ORDER BY min(f.n)"""

    return dashboard(
        f"{PFX}-advertiser",
        "Advertisers (the union-find entity)",
        [
            markdown(
                "Read first",
                "An advertiser is **derived**, not stored by the site: union-find over phone last-4, handle, "
                "name, title, description and venue keys (`used_for_clustering`). **Photo** edges are recorded "
                "but do NOT cluster — where they join two clusters they are a merge candidate, not a fact. "
                "Pick one advertiser above for the network and route panels.",
                h=3,
            ),
            panel(
                "table",
                "Leaderboard — advertisers with ≥ 2 ads (raise it in the panel's Filter data by values)",
                leader,
                w=24,
                h=10,
                options={"cellHeight": "sm"},
                transformations=min_filter("ads", 2),
                fieldConfig={
                    "defaults": {},
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "advertiser_id"},
                            "properties": [
                                {
                                    "id": "links",
                                    "value": [explorer_link("advertiser_id", "advertiser")],
                                }
                            ],
                        },
                        {
                            "matcher": {"id": "byName", "options": "ads"},
                            "properties": [
                                {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "basic"}}
                            ],
                        },
                    ],
                },
            ),
            panel(
                "barchart",
                "Cluster sizes and cumulative share of ads",
                [size, size_cum],
                w=12,
                h=9,
                options={"xField": "ads_per_advertiser\\list"},
                # stock transforms, no hand pivot: A → one column per list (Grouping to matrix), joined
                # with B's cumulative share on the size, then the size becomes a category for the x axis
                transformations=[
                    {
                        **to_matrix("ads_per_advertiser", "list", "clusters")[0],
                        "filter": {"id": "byRefId", "options": "A"},
                    },
                    {
                        "id": "joinByField",
                        "options": {"byField": "ads_per_advertiser\\list", "mode": "outer"},
                    },
                    {
                        "id": "convertFieldType",
                        "options": {
                            "conversions": [
                                {"targetField": "ads_per_advertiser\\list", "destinationType": "string"}
                            ]
                        },
                    },
                ],
                fieldConfig={
                    "defaults": {},
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "cum_share_of_ads"},
                            "properties": [
                                {"id": "custom.axisPlacement", "value": "right"},
                                {"id": "unit", "value": "percentunit"},
                                {"id": "custom.drawStyle", "value": "line"},
                            ],
                        }
                    ],
                },
            ),
            panel(
                "table",
                "Single-kind direct edges (v1.1 [all fetched]) — Direct edges inside an advertiser whose ONLY shared key kind is X; transitive pairs are not counted.",
                sens,
                w=12,
                h=9,
                options={"cellHeight": "sm"},
            ),
            row("One advertiser — ${advertiser}"),
            panel(
                "nodeGraph",
                "Ad cluster network (v2, personas: draft)",
                [net_nodes, net_edges],
                expect_rows=("A", "B"),
                w=24,
                h=16,
                description=(
                    "Nodes are personas (an ad group under one stage identity, not a person) of ad clusters "
                    "with at least ${min_personas} personas; edges are the shared-key KINDS that joined them "
                    "(counts only, never a key value). Blue = strong (desc, handle or shared images), orange "
                    "dashed = a weak pair, grey dashed = shared images only. photo_edges=exclude re-applies "
                    "the v2 rule without shared images: the joins that survive. Grafana shows ≤ 200 nodes; "
                    "use 'show more' or raise min personas. The legend's 'mainstat / secondarystat' = ads / names "
                    "(Grafana's fixed field names). " + PERSONA_GATE
                ),
                # force: Grafana's default "layered" layout leaves a 200+-node graph off-screen on open
                options={"layoutAlgorithm": "force"},
                fieldConfig={
                    "defaults": {
                        "links": [
                            {
                                "title": "This persona's ads in Explorer (no coordinates, dates to the month)",
                                "url": f"/d/{PFX}-explorer?var-advertiser=${{__data.fields.id}}",
                            },
                            {
                                "title": "This ad cluster's ads in Explorer",
                                "url": f"/d/{PFX}-explorer?var-advertiser=${{__data.fields.detail__ad_cluster}}",
                            },
                        ]
                    },
                    "overrides": [],
                },
            ),
            _geomap(
                "Pin route in publish order (empty if this advertiser has no pins; the default is the largest advertiser WITH pins)",
                [routes, routes_grey],
                [
                    grey_layer("B"),
                    _markers("Pins", "A", size=8, color="city_label"),
                    {
                        "type": "route",
                        "name": "Route",
                        "config": {"style": {"color": {"fixed": "purple"}, "size": {"fixed": 2}}, "arrow": 1},
                        "location": {"mode": "coords", "latitude": "lat", "longitude": "lon"},
                        "filterData": {"id": "byRefId", "options": "A"},
                    },
                ],
                w=10,
                h=14,
            ),
            row("Cluster shape and H2 label shopping"),
            panel(
                "table",
                "Ads vs city labels (v2, personas: draft)",
                multi_label,
                description="Personas with >= 2 ads, counted by their number of ads (rows) and of distinct city "
                "labels (columns). On the diagonal (2 ads × 2 labels, …) every ad is posted under a new label.",
                w=12,
                h=8,
                options={"cellHeight": "sm"},
                transformations=[
                    {
                        "id": "groupingToMatrix",
                        "options": {
                            "columnField": "city_labels",
                            "rowField": "ads",
                            "valueField": "personas",
                        },
                    }
                ],
                fieldConfig={
                    "defaults": {
                        "custom": {"cellOptions": {"type": "color-background"}},
                        "color": {"mode": "continuous-blues"},
                    },
                    "overrides": [],
                },
            ),
            panel(
                "table",
                "City labels in multi-label posting — labels used by >= 5 multi-label personas (counts only; no drill-down)",
                multi_label_names,
                w=12,
                h=8,
                options={"cellHeight": "sm"},
                description="Multi-label posting is a visibility tactic (reaching clients who search by city), not an "
                "indicator. A label appears only where >= 5 personas used it alongside another label.",
            ),
            panel(
                "barchart",
                "Posting gaps between a persona's ads — pooled over personas with >= 2 ads; each persona weighs 1",
                gap_buckets,
                w=24,
                h=8,
                description="Days between consecutive first_published dates within one persona (v2), bucketed. "
                "Bars are the share of personas (each persona's gaps split its weight of 1), so a prolific persona "
                "cannot dominate; no per-persona series and no dates (C17). Rate input for panel sizing (λ).",
                options={"xField": "gap", "orientation": "vertical", "stacking": "none"},
                fieldConfig={
                    "defaults": {"min": 0},
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "share_of_personas"},
                            "properties": [{"id": "unit", "value": "percentunit"}],
                        },
                        {
                            "matcher": {"id": "byName", "options": "personas"},
                            "properties": [
                                {
                                    "id": "custom.hideFrom",
                                    "value": {"viz": True, "legend": True, "tooltip": False},
                                }
                            ],
                        },
                    ],
                },
            ),
            row("H4 — does the pin move?"),
            panel(
                "histogram",
                "Pin pairs within an advertiser — log10 km, tied < 50 m at the left (advertisers v1.1 [all fetched])",
                pairs,
                w=12,
                h=9,
                description="Drops pairs whose two ads share an identical first_published; the tied/moved stat beside "
                "it does not, so the two counts differ by those pairs.",
                options={"combine": False},
            ),
            panel(
                "stat",
                "Pin pairs — tied vs moved (advertisers v1.1 [all fetched]; the persona-level twin is below)",
                pairs_stat,
                w=12,
                h=4,
                options={"colorMode": "none", "graphMode": "none", "textMode": "value_and_name"},
            ),
            panel(
                "stat",
                "H4 power — precise pin/text pairs vs independent advertisers",
                h4_stat,
                w=12,
                h=5,
                options={"colorMode": "background", "graphMode": "none", "textMode": "value_and_name"},
            ),
            panel(
                "xychart",
                "H4: age vs pin-to-text km — Precise pairs, coloured by advertiser (clumps = pseudo-replication).",
                h4,
                w=24,
                h=9,
                options={
                    "mapping": "manual",
                    "series": [
                        {
                            "frame": {"matcher": {"id": "byIndex", "options": 0}},
                            "x": {"matcher": {"id": "byName", "options": "age_days"}},
                            "y": {"matcher": {"id": "byName", "options": "km_pin_text"}},
                            "color": {"matcher": {"id": "byName", "options": "advertiser"}},
                        }
                    ],
                },
            ),
            panel(
                "stat",
                "Advertiser pairs joined by a shared photo upload (advertisers v1.1 [all fetched]; photo is NOT a clustering key)",
                PHOTO_PAIRS,
                w=24,
                h=4,
                options={"colorMode": "none", "graphMode": "none", "textMode": "value_and_name"},
            ),
            row("Ablation (v1.1): what holds clusters together"),
            panel(
                "table",
                "Ablation: clusters per variant — Ads whose cluster SIZE changes vs baseline; ad sets [all fetched] and [pre-recruitment ads, clustered with recruits] (not a sample-alone run).",
                ABLATION,
                w=12,
                h=8,
                options={"cellHeight": "sm"},
                description="An ad counts as changed when the SIZE of its cluster (all [all fetched] members, recruited "
                "included) differs from baseline. The analyst's definition — an old ad moves when its set of OLD "
                "co-members differs — can give one fewer on [pre-recruitment ads, clustered with recruits]; both are correct for their definition.",
            ),
            panel(
                "table",
                "Template families across −desc clusters [all fetched] — The non-circular T3: v1.1 re-run without the desc key.",
                T3_NODESC,
                w=12,
                h=8,
                options={"cellHeight": "sm"},
            ),
            panel(
                "table",
                "Template families across personas (v2, personas: draft) — Each inside ONE ad cluster? Kit vs agency: persona pairs sharing phone4 / photo / venue. Persona counts are an upper bound.",
                T3_V2,
                w=24,
                h=8,
                options={"cellHeight": "sm"},
            ),
            row("advertisers@2 (v2)"),
            markdown(
                "How to read advertisers@2",
                "A **persona** sits inside one **operator**. Persona precision in the analyst's blind read: "
                "**0.97 [0.87–1.00]**. Every persona COUNT is an **upper bound**: about half of same-operator "
                "splits are one person under a rotating name (**0.46 [0.23–0.71]**). Ids are member-set hashes, "
                "rebuilt every export — joined on (site, ad_id) only.",
                h=4,
            ),
            panel(
                "stat",
                "Operators and ≤ N personas (upper bound) — candidate (advertisers@2)",
                V2_SUMMARY,
                w=24,
                h=4,
                options={"colorMode": "none", "graphMode": "none", "textMode": "value_and_name"},
            ),
            panel(
                "stat",
                "Pin pairs within a persona [all fetched] — tied vs moved (persona counts are an upper bound) — candidate (advertisers@2)",
                V2_PIN_PAIRS,
                w=12,
                h=5,
                options={"colorMode": "none", "graphMode": "none", "textMode": "value_and_name"},
            ),
            panel(
                "table",
                "Far pairs per persona (v2, personas: draft) — Pairs > 5 km apart per persona, top 10 [all fetched]; one persona can carry most. Persona counts are an upper bound.",
                V2_FAR_PAIRS_TOP,
                w=12,
                h=8,
                options={"cellHeight": "sm"},
            ),
            panel(
                "table",
                "Far pins per persona (v2, personas: draft) — > 60 km from the label centre. Persona counts are an upper bound.",
                V2_FAR,
                w=12,
                h=8,
                description=FAR_NOTE,
                options={"cellHeight": "sm"},
            ),
            panel(
                "table",
                "Venues across ad clusters (v2)",
                V2_VENUES,
                w=12,
                h=8,
                options={"cellHeight": "sm"},
            ),
            row("Shared keys"),
            _geomap(
                "Venues named by advertisers — size = distinct advertisers",
                [venues.replace("{keep}", text_in()), venues_grey],
                [
                    grey_layer("B"),
                    {
                        **_markers("Venues", "A", color="kind"),
                        "config": {
                            "style": {
                                "size": {"field": "advertisers", "min": 4, "max": 24, "fixed": 6},
                                "color": {"field": "kind"},
                                "opacity": 0.8,
                                "symbol": {"mode": "fixed", "fixed": "img/icons/marker/circle.svg"},
                            },
                            "showLegend": True,
                        },
                    },
                ],
                w=12,
                h=10,
            ),
            panel(
                "table",
                "Venues (table)",
                venues.replace("{keep}", text_ok()),
                w=12,
                h=10,
                options={"cellHeight": "sm"},
            ),
            panel(
                "stat",
                "Masked contact phone (last 4) appearing under > 1 advertiser — NOT a clustering key (pairs vs a C(m,2)/10⁴ null)",
                phone4,
                w=12,
                h=6,
                options={"colorMode": "none", "graphMode": "none", "textMode": "value_and_name"},
            ),
            panel(
                "table",
                "Card-hash families × list-depth decile (≈ days since last bump; all cards, weighted)",
                fam,
                w=12,
                h=6,
                options={"cellHeight": "sm"},
                fieldConfig={
                    "defaults": {
                        "decimals": 0,
                        "custom": {"cellOptions": {"type": "color-background"}},
                        "color": {"mode": "continuous-YlRd"},
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "family_size"},
                            "properties": [{"id": "custom.cellOptions", "value": {"type": "auto"}}],
                        }
                    ],
                },
            ),
        ],
        variables=[
            subcategory_var(),
            query_var("viewpoint", "SELECT label FROM viewpoint ORDER BY 1", multi=False, include_all=False),
            query_var(
                "advertiser",
                "SELECT a.advertiser_id FROM advertisers a JOIN listings_masked l USING (site, ad_id) "
                "GROUP BY 1 ORDER BY bool_or(l.pin_lat IS NOT NULL) DESC, count(*) DESC, 1",
                multi=False,
                include_all=False,
            )
            | {"sort": 0},
            query_var("link_kind", "SELECT DISTINCT link_kind FROM advertiser_links ORDER BY 1"),
            custom_var("precise", ["precise", "all"]),
            custom_var("photo_edges", ["include", "exclude"]),
            custom_var("min_personas", ["2", "3", "5"], label="min personas per ad cluster"),
            out_of_region_var(),
            oor_line_var(),
        ],
        links=[f"{PFX}-explorer", f"{PFX}-geo", f"{PFX}-overview"],
        description="The derived advertiser: size, how firmly it is held, label shopping, pin moves, shared keys.",
    )


BOARDS.append(advertisers_board)


# Chart-choice lint waivers (lint.py): {(board uid, panel title): {rule: reason}}. A waiver is a
# reasoned exception, never a silent skip; one that stops firing fails the lint test.
LINT_WAIVERS: dict[tuple[str, str], dict[str, str]] = {
    (f"{PFX}-advertiser", "Cluster sizes and cumulative share of ads"): {
        "dual-axis": "PROVISIONAL until the GQM rework: a Pareto chart, bars of cluster counts with the "
        "cumulative share of ads on a right-hand 0-100 % axis; viz-designer decides keep or split.",
    },
    (f"{PFX}-process", "H5 — A/B variant × subcategory"): {
        "diverging-palette": "PROVISIONAL until the GQM rework: continuous-GrYlRd on a card count has "
        "no meaningful midpoint; viz-designer decides the sequential replacement.",
    },
}
