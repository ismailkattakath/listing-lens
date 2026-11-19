-- The DERIVED, read-only Postgres copy of listing-lens' DATA (ADR-0014).
-- Applied to a FRESH schema (li_new) on every `llens export-db`; the copy is rebuilt, never migrated in
-- place, so this file is the whole truth. Bump the number (0002_…) for a breaking change.
-- Contract with the dashboard (grafana-engineer, 2026-11-08): instants are timestamptz with NO suffix and
-- the stored string beside them as *_raw; nested li fields are flattened with their path as the prefix
-- (pin_, text_hint_, geocode_, city_centre_); lists are jsonb; list positions are list_*; key (site, ad_id).
-- Points: every lat/lon pair also has a *_geog geography(Point, 4326) column, GENERATED from it (PostGIS, in
-- public): distances are ST_Distance / ST_DWithin on the WGS84 spheroid, in metres.

CREATE TABLE meta (
  key   text PRIMARY KEY,
  value jsonb NOT NULL
);

-- The source's region box (from its own config; never a second hand-typed box).
CREATE TABLE region (
  site   text NOT NULL,
  name   text NOT NULL,
  south  double precision NOT NULL,
  west   double precision NOT NULL,
  north  double precision NOT NULL,
  east   double precision NOT NULL,
  PRIMARY KEY (site, name)
);

-- The operator's own points for the map's radius rings (DATA/config/viewpoint.json, never in git).
CREATE TABLE viewpoint (
  label text PRIMARY KEY,
  lat   double precision NOT NULL,
  lon   double precision NOT NULL,
  geog  geography(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography) STORED
);

CREATE TABLE observations (
  seq                integer PRIMARY KEY,     -- line order in observations.jsonl
  site               text NOT NULL,
  ad_id              text NOT NULL,
  crawl_id           text NOT NULL,
  seen_at            timestamptz NOT NULL,
  seen_at_raw        text NOT NULL,
  ad_url             text,
  list_url           text,
  list_subcategory   text,
  list_page          integer,
  list_page_count    integer,
  list_position      integer,
  depth_decile       integer,                -- min(9, floor(10*(list_page-1)/list_page_count))
  sponsored          boolean,                -- UNVERIFIED: false on every card so far
  list_sample_weight double precision,
  card_title         text,
  card_city_label    text,
  card_name          text,
  card_age           integer,
  card_ethnicity     text,
  verified           boolean,
  available          boolean,
  rate               integer,                -- a bare number; the site shows no unit
  bumped_at          timestamptz,            -- always NULL: list pages carry no absolute bump time
  card_hash          text,
  card               jsonb NOT NULL,
  UNIQUE (site, ad_id, crawl_id)
);

-- Every version of every ad (append-only DATA, line order kept for the tie-break).
CREATE TABLE listings (
  seq                 integer PRIMARY KEY,
  site                text NOT NULL,
  ad_id               text NOT NULL,
  fetched_at          timestamptz NOT NULL,
  fetched_at_raw      text NOT NULL,
  fetch_reason        text NOT NULL,
  parser              text,                  -- derived.method of a reparse version
  source_url          text,
  subcategory         text,
  region              text,
  title               text,
  description         text,
  phone_masked        text,
  email_masked        text,
  website             text,
  whatsapp            boolean,
  media_urls          jsonb,                 -- exactly as the site gave them
  n_media             integer,
  first_published     timestamptz,
  first_published_raw text,
  bumped_at           timestamptz,
  bumped_at_raw       text,
  city_label          text,
  pin_lat             double precision,
  pin_lon             double precision,
  pin_approximate     boolean,
  pin_geog            geography(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(pin_lon, pin_lat), 4326)::geography) STORED,
  raw_path            text,
  raw_sha256          text,
  raw_bytes           integer,
  city_code           text,
  name                text,
  age                 integer,
  ethnicity           text,
  service_mode        jsonb,
  languages           jsonb,
  height              text,
  weight              text,
  body_stats          text,
  orientation         text,
  hair                text,
  eye                 text,
  tagline             text,
  services            jsonb,                 -- [{code, label}]
  page_schema_type    text,
  site_fields         jsonb
);
CREATE INDEX listings_ad ON listings (site, ad_id);
CREATE INDEX listings_pin_geog ON listings USING gist (pin_geog);

-- derive's per-ad output (derived/listings.latest.jsonl). Google coordinates are NULL once past the
-- 30-day cache limit: use geocode_status, not a NULL lat, for "was it geocoded".
CREATE TABLE derived (
  site                 text NOT NULL,
  ad_id                text NOT NULL,
  text_hint_raw        text,
  text_hint_kind       text,
  text_hint_query      text,
  text_hint_extractor  text,
  geocode_provider     text,
  geocode_status       text,
  geocode_formatted    text,
  geocode_lat          double precision,
  geocode_lon          double precision,
  geocode_precise      boolean,
  geocode_place_id     text,
  geocode_types        jsonb,
  geocode_expires_at   timestamptz,
  city_centre_label    text,
  city_centre_lat      double precision,
  city_centre_lon      double precision,
  geocode_geog         geography(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(geocode_lon, geocode_lat), 4326)::geography) STORED,
  city_centre_geog     geography(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(city_centre_lon, city_centre_lat), 4326)::geography) STORED,
  km_pin_text          double precision,
  km_pin_city          double precision,
  km_text_city         double precision,
  pin_verdict          text,
  confidence           text,
  location_valid_pin   boolean,  -- false: pin > 60 km from the label centre (pre-registered); raw pin kept
  location_valid_text  boolean,  -- false: text geocode outside the label's region (see location_region_test)
  location_invalid_reason text,  -- out_of_region_pin | out_of_region_text | both | out_of_region_place | unresolvable
  location_region_test text,     -- boundary (inside the label's 2021 census divisions + 3 km) | radius (<= 60 km from the label centre)
  geocode_decision     text,     -- auto_close | no_pin | city_default_pin | far | out_of_region
  geocode_location_type text,    -- Google's ROOFTOP | RANGE_INTERPOLATED | GEOMETRIC_CENTER | APPROXIMATE
  derive_method        text,
  place_decision       text,     -- accept_google | pick_alternative | other_place | text_only_verified | auto_alternative | review | auto_close
  place_decided_by     text,     -- operator | place-check@N | derive's method (auto_close)
  place_decision_status text,    -- applied | stale_group | stale_point | geocode_pending | geocode_failed | … (no point)
  place_review_ref     text,     -- <batch>#<task> of the operator decision
  place_point_source   text,     -- google | alternative | operator_geocode | operator_osm | machine_verified | pin
  place_lat            double precision,  -- the final point; a Google one only while within the coordinate TTL
  place_lon            double precision,
  place_geog           geography(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(place_lon, place_lat), 4326)::geography) STORED,
  location_valid_place boolean,  -- false: the final point > 60 km from the label centre (flagged, kept)
  place_gold_v2        boolean,  -- the operator decision is on an address-gold-v2 ad: never a gold label source
  hint_gold_v3         boolean NOT NULL,  -- the ad is in an address-gold-v3 frame (tranche 1 or the tranche-2 frame): masked
  PRIMARY KEY (site, ad_id)
);

-- advertiser.v1 in LONG form: one row per (advertiser, ad).
CREATE TABLE advertisers (
  advertiser_id text NOT NULL,
  site          text NOT NULL,
  ad_id         text NOT NULL,
  n_ads         integer,
  link_kinds    jsonb,
  venue_only    boolean,
  method        text,
  PRIMARY KEY (advertiser_id, site, ad_id)
);

-- The frozen advertisers@1 record (derive-advertisers --method v1), same long form; `advertisers` is v1.1.
CREATE TABLE advertisers_v1 (LIKE advertisers INCLUDING ALL);

-- advertiser_link.v1: one edge per (ad pair, key kind) inside an advertiser (the node graph).
CREATE TABLE advertiser_links (
  advertiser_id       text NOT NULL,       -- ad_a's advertiser
  advertiser_b        text NOT NULL,       -- ad_b's advertiser (differs only for photo links)
  site                text NOT NULL,
  ad_a                text NOT NULL,
  ad_b                text NOT NULL,
  link_kind           text NOT NULL,       -- phone4 | handle | name | title | desc | venue | photo
  link_key            text NOT NULL,       -- title/desc/photo: a 12-hex fingerprint, never the text
  used_for_clustering boolean NOT NULL,    -- false for photo: recorded, never merges advertisers
  PRIMARY KEY (site, ad_a, ad_b, link_kind, link_key)
);

-- advertiser_ablation.v1: the advertiser each ad lands in with one key kind dropped (or photos added).
CREATE TABLE advertisers_ablation (
  variant       text NOT NULL,             -- baseline | -desc | -name | -title | -phone4 | -handle | -venue | +photo
  site          text NOT NULL,
  ad_id         text NOT NULL,
  advertiser_id text NOT NULL,             -- a hash of the cluster's ad set: = baseline's means the same cluster
  n_ads         integer NOT NULL,
  link_kinds    jsonb,
  PRIMARY KEY (variant, site, ad_id)
);

-- advertiser.v2 (advertisers@2, pairwise): ONE row per ad, next to v1. advertiser_id is "v2:"+hash of
-- the component's ad set, so it changes when membership does: join on (site, ad_id).
CREATE TABLE advertisers_v2 (
  site                 text NOT NULL,
  ad_id                text NOT NULL,
  operator_id          text NOT NULL,      -- "v2:"…: the v2 component
  persona_id           text NOT NULL,      -- "p2:"…: persona edges only, always inside one operator
  n_ads                integer NOT NULL,
  level                text NOT NULL,      -- persona | operator (a judgement threshold; inputs below)
  n_names              integer NOT NULL,
  top_shared_key_share double precision NOT NULL,
  edge_evidence        jsonb NOT NULL,     -- the kinds of the v2 edges that pulled this ad in
  persona_edge_evidence jsonb NOT NULL,
  method               text NOT NULL,
  PRIMARY KEY (site, ad_id)
);

-- advertiser_v2_link.v1: WHY v2 merged two ads (the node graph's edges), one row per (v2 edge, key kind).
CREATE TABLE advertiser_v2_links (
  operator_id       text NOT NULL,         -- both ads' v2 operator (advertisers_v2.operator_id)
  site              text NOT NULL,
  ad_a              text NOT NULL,
  ad_b              text NOT NULL,
  link_kind         text NOT NULL,         -- phone4 | handle | name | title | desc | venue | image
  link_key          text NOT NULL,         -- title/desc/image: a 12-hex fingerprint, never the text
  n_keys            integer NOT NULL,      -- distinct keys of this kind the pair shares (link_key = the smallest)
  edge_kinds        jsonb NOT NULL,        -- every kind on the edge: the pair rule's input
  cross_subcategory boolean NOT NULL,      -- across FE/TS the rule needs >= 2 kinds
  persona_edge      boolean NOT NULL,      -- the edge also links personas
  method            text NOT NULL,
  PRIMARY KEY (site, ad_a, ad_b, link_kind)
);

-- The latest version per ad (greatest fetched_at; a later line wins a tie), with derive's output.
CREATE VIEW listings_latest AS
  WITH latest AS (
    SELECT DISTINCT ON (site, ad_id) *
    FROM listings
    ORDER BY site, ad_id, fetched_at DESC, seq DESC
  ),
  adv AS (
    SELECT DISTINCT ON (site, ad_id) site, ad_id, advertiser_id
    FROM advertisers
    ORDER BY site, ad_id, advertiser_id
  )
  SELECT l.*,
         d.text_hint_raw, d.text_hint_kind, d.text_hint_query, d.text_hint_extractor,
         d.geocode_provider, d.geocode_status, d.geocode_formatted, d.geocode_lat, d.geocode_lon,
         d.geocode_precise, d.geocode_place_id, d.geocode_types, d.geocode_expires_at,
         d.city_centre_label, d.city_centre_lat, d.city_centre_lon, d.geocode_geog, d.city_centre_geog,
         d.km_pin_text, d.km_pin_city, d.km_text_city, d.pin_verdict, d.confidence, d.derive_method,
         d.location_valid_pin, d.location_valid_text, d.location_invalid_reason, d.location_region_test, d.geocode_decision,
         d.geocode_location_type,
         d.place_decision, d.place_decided_by, d.place_decision_status, d.place_review_ref,
         d.place_point_source, d.place_lat, d.place_lon, d.place_geog, d.location_valid_place, d.place_gold_v2,
         d.hint_gold_v3,
         a.advertiser_id
  FROM latest l
  LEFT JOIN derived d USING (site, ad_id)
  LEFT JOIN adv a USING (site, ad_id);

-- Survival (GQM G6.Q4): ONE row per ad with a live sighting, read from the harness's liveness ledger
-- (scrape_harness.liveness, export-db@23). A sighting = a GOOD crawl's list card (completed, list.status ok) or
-- an OK ad fetch (never a reparse). gone = the ledger's tombstone (events kind=gone, joined by its adkey rule).
CREATE TABLE ad_observation_span (
  site           text NOT NULL,
  ad_id          text NOT NULL,
  first_seen     timestamptz NOT NULL,
  last_seen      timestamptz NOT NULL,     -- the last LIVE sighting (a gone_at may follow it)
  n_observations integer NOT NULL,         -- live sightings: a GOOD crawl's list cards + OK fetches
  gone_at        timestamptz,              -- the tombstone (or the quarantined gone); NULL if none stands
  gone_reason    text,                     -- expired_notice | http_410 | …
  gone_run       text,                     -- the run that wrote that gone event
  liveness       text NOT NULL,            -- the harness ledger's state: alive | tombstoned | quarantined | held
  readmitted     integer NOT NULL,         -- list re-admissions after a tombstone (0 or 1)
  held_since     timestamptz,              -- the parse_failed event that holds it
  status         text NOT NULL,            -- gone (tombstoned) | still_listed (seen live >= 4 h after first_seen) | unknown
  PRIMARY KEY (site, ad_id)
);

CREATE TABLE geocodes (
  provider     text NOT NULL,
  query        text NOT NULL,
  place_id     text,
  status       text,
  lat          double precision,           -- only while within the coordinate TTL
  lon          double precision,
  precise      boolean,
  types        jsonb,
  formatted    text,
  resolved_at  timestamptz,
  expires_at   timestamptz,
  PRIMARY KEY (provider, query)
);

-- export-db@21: what the boards may know about the geocode cache WITHOUT reading it (the table holds the
-- query text and the points; board_ro is denied on it). Counts and expiry only.
CREATE VIEW geocode_cache_summary AS  -- ONE row; the column contract is pinned by a test (grafaner's panel)
SELECT count(*)::integer                    AS queries,
       count(DISTINCT place_id)::integer    AS places,
       avg(precise::int)::double precision  AS precise_share,
       min(expires_at)                      AS min_expires_at
FROM geocodes;

CREATE TABLE runs (
  run_id                         text PRIMARY KEY,
  command                        text,
  variant                        text,
  site                           text,
  status                         text,
  created_at                     timestamptz,
  requests_list_pages            integer,
  requests_ad_pages              integer,
  requests_robots                integer,
  requests_other                 integer,
  requests_egress_trace          integer,
  requests_site_subresources     integer,
  requests_third_party           integer,
  requests_redirects             integer,
  requests_failed_before_request integer,
  requests_site_subresources_blocked integer,
  requests_third_party_blocked   integer,
  sample                         jsonb,
  approval_ref                   jsonb,
  record                         jsonb NOT NULL
);

CREATE TABLE events (
  seq     integer PRIMARY KEY,
  at      timestamptz NOT NULL,
  run_id  text,
  kind    text NOT NULL,
  site    text,
  reason  text,
  url     text,
  record  jsonb NOT NULL
);

-- runs/<run>.log.jsonl, one row per line.
CREATE TABLE run_logs (
  run_id  text NOT NULL,
  seq     integer NOT NULL,
  at      timestamptz,
  level   text,
  logger  text,
  event   text,
  kind    text,
  status  integer,
  path    text,
  ms      double precision,
  msg     text,
  PRIMARY KEY (run_id, seq)
);

-- derived/ab-*.json, one row per variant.
CREATE TABLE ab_variants (
  ab_at         timestamptz NOT NULL,
  ab_at_raw     text NOT NULL,
  site          text,
  subcategory   text,
  variant       text NOT NULL,
  cards         integer,
  pins_on_pages integer,
  stopped       jsonb,
  requests      jsonb,
  coverage      jsonb,
  PRIMARY KEY (ab_at_raw, variant)
);

CREATE TABLE samples (
  run_id      text NOT NULL,
  site        text NOT NULL,
  ad_id       text NOT NULL,
  stratum     text,                          -- "<subcategory>/d<decile>"
  subcategory text,
  decile      integer,
  weight      double precision,
  reason      text,
  seed        bigint,
  allocation  text,
  PRIMARY KEY (run_id, site, ad_id)
);

CREATE TABLE findings (
  seq                integer PRIMARY KEY,
  id                 text,
  version            integer,                -- 1.. per id, in file order
  tested_at          timestamptz,
  status             text,
  question           text,
  method             text,
  result             text,
  n                  integer,
  scope              text,
  weakest_assumption text,
  next_test          text,
  record             jsonb NOT NULL
);

-- The phone-session import (2026-11-07, pre-li), one row per ad, joined from its own files.
CREATE TABLE import_phone_ads (
  site            text NOT NULL,
  ad_id           text NOT NULL,
  path            text NOT NULL,
  title           text,
  body            text,
  city_label      text,
  bumped_at       timestamptz,
  bumped_at_raw   text,
  pin_lat         double precision,
  pin_lon         double precision,
  text_hint_raw   text,
  text_hint_kind  text,
  geocode_lat     double precision,          -- NULL once past Google's 30-day cache limit
  geocode_lon     double precision,
  geocode_precise boolean,
  pin_geog        geography(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(pin_lon, pin_lat), 4326)::geography) STORED,
  geocode_geog    geography(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(geocode_lon, geocode_lat), 4326)::geography) STORED,
  city_filter     text,                      -- the import's own derived fields (its H1/H3 inputs)
  km_to_target    double precision,          -- distance to the import's target point (that session's argument)
  d_text          double precision,
  d_city          double precision,
  pin_class       text,
  confidence      text,
  location_valid_pin      boolean,  -- li's region rule (derive.region_validity): the label's CDs, else <= 60 km
  location_valid_text     boolean,
  location_invalid_reason text,     -- out_of_region_pin | out_of_region_text | both | unresolvable
  location_region_test    text,     -- boundary | radius
  record          jsonb NOT NULL,
  PRIMARY KEY (site, ad_id)
);

-- ONE row per ad across li and the phone import, merged field by field with provenance (*_src):
-- llens | phone | both-agree | both-differ (the kept value follows export_db's merge rules).
CREATE TABLE ads (
  site            text NOT NULL,
  ad_id           text NOT NULL,
  in_li_listings  boolean NOT NULL,
  in_li_cards     boolean NOT NULL,
  in_phone        boolean NOT NULL,
  title           text,
  title_src       text,
  description     text,
  description_src text,
  city_label      text,
  city_label_src  text,
  bumped_at       timestamptz,
  bumped_at_src   text,
  pin_lat         double precision,
  pin_lon         double precision,
  pin_src         text,
  text_hint_raw   text,
  text_hint_kind  text,
  text_hint_src   text,
  geocode_lat     double precision,
  geocode_lon     double precision,
  geocode_precise boolean,
  geocode_src     text,
  in_box          boolean,                   -- pin inside the region box (region table)
  pin_geog        geography(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(pin_lon, pin_lat), 4326)::geography) STORED,
  geocode_geog    geography(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(geocode_lon, geocode_lat), 4326)::geography) STORED,
  PRIMARY KEY (site, ad_id)
);
