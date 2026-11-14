-- `llens services` (derived/services.latest.jsonl, services.summary.json) and the operator's vocabulary labels.
-- Until the analyst freezes the blind-eval labels, the eval-half ads are ABSENT from every services table:
-- services_run says so (eval_excluded, eval_ads_excluded), services_excluded_strata says where.

-- One row per ad the stage processed, INCLUDING ads with no mention (the denominator of every share).
CREATE TABLE services_ads (
  site                    text NOT NULL,
  ad_id                   text NOT NULL,
  fetched_at              timestamptz NOT NULL,  -- the listing version read: join listings on (site, ad_id, fetched_at)
  n_concepts_offered      integer NOT NULL,      -- distinct concepts with an offered mention
  n_concepts_refused      integer NOT NULL,      -- distinct concepts with a refused mention
  online_services_offered text[] NOT NULL,       -- the page's own services list, as codes (services_codes)
  eval_adjacent           boolean,               -- analyst R1: an eval ad or a v2-operator mate; filter it out of discovery/tuning
  vocab_version           text NOT NULL,
  method                  text NOT NULL,
  PRIMARY KEY (site, ad_id)
);

-- One row per matched mention. start/end: Unicode code-point offsets, 0-based, end-exclusive, into that
-- version's title or description; the span is substring(<field> FROM start + 1 FOR "end" - start).
CREATE TABLE services_mentions (
  site          text NOT NULL,
  ad_id         text NOT NULL,
  fetched_at    timestamptz NOT NULL,
  concept       text NOT NULL,         -- skos:notation (services_concepts.concept)
  field         text NOT NULL,         -- title | description
  start         integer NOT NULL,
  "end"         integer NOT NULL,
  stance        text NOT NULL,         -- offered | refused
  label_matched text NOT NULL
);
CREATE INDEX services_mentions_ad ON services_mentions (site, ad_id);
CREATE INDEX services_mentions_concept ON services_mentions (concept);

-- "Does this ad offer / refuse X": one row per (ad, concept), over the mention rows (one source).
CREATE VIEW services_ad_concepts AS
  SELECT site, ad_id, concept,
         bool_or(stance = 'offered') AS offered,
         bool_or(stance = 'refused') AS refused,
         count(*)::integer AS n_mentions
  FROM services_mentions
  GROUP BY site, ad_id, concept;

-- The stage's last run (one row; none before the stage has run).
CREATE TABLE services_run (
  ran_at            timestamptz,
  eval_gate         text NOT NULL,     -- empty-vocab | frozen | unfrozen
  eval_excluded     boolean NOT NULL,
  eval_ads_excluded integer NOT NULL,
  eval_excluded_by_advertiser integer NOT NULL,  -- non-eval ads sharing a v2 operator with an eval ad
  concepts          integer NOT NULL,  -- concepts in the vocabulary the run used
  ads               integer NOT NULL,
  vocab_version     text NOT NULL,
  method            text NOT NULL
);

-- Where the excluded ads (eval + their v2-operator mates) sit in THE depth sample (the boards' DEPTH_RUN; recruit rows excluded): counts
-- only, never ad ids. Empty when nothing is excluded.
CREATE TABLE services_excluded_strata (
  subcategory         text,
  decile              integer,
  n_eval_ads_excluded integer NOT NULL,
  n_advertiser_excluded integer NOT NULL  -- their v2-operator mates, also absent from services_ads
);

-- The operator's vocabulary labels (DATA): panels show pref_label, never the concept key.
CREATE TABLE services_concepts (
  concept       text PRIMARY KEY,      -- skos:notation
  pref_label    text NOT NULL,
  broader       text,                  -- the broader concept's notation, if any
  sort_order    integer NOT NULL,      -- by (broader, pref_label), case-insensitive
  vocab_version text NOT NULL
);

-- The page's own service codes and their displayed label (the most frequent one per code).
CREATE TABLE services_codes (
  code  text PRIMARY KEY,
  label text
);
