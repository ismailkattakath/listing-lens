# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version is the one in `pyproject.toml`.

## [Unreleased]

### Changed

- `export-db@24`: `observations` keeps the first sighting per
  `(site, ad_id, crawl_id)`. A contiguous list walk (ADR-0017 slice 2) can
  see one ad twice in one crawl, because a bump during the walk pushes its card
  a page down. The `UNIQUE` rule stays, the repeats are counted in
  `merge.observations_repeat_dropped`, and `observations.jsonl` is never
  rewritten. Before this, the repeat made the load fail with a unique violation.
- The scrape-harness pin moves to f23f117, which ships ADR-0017 slice 1
  (frontier-and-ledger sync): exit 14 for a parse or yield stop, the liveness
  ledger, the fresh-sighting gate with `select_ads` lanes (revisit share hard
  capped at rho = 0.2), and a signed `SyncPolicy` digest in every fetch-ads
  plan. Plans and refresh approvals signed under the old pin no longer match:
  plan them again. No golden in this repository moved. The DATA site plug-in's
  e2e digest moves with the new run.v1 `list{}` and `ads{}` blocks and the
  fetch-ads run card's selection line, so it needs re-recording.
- `export-db@23`: `ad_observation_span` is read from the harness's liveness
  ledger, not from a second copy of the sighting and gone rules. Only a good
  crawl's cards count as sightings, a gone joins its ad by the ledger's rule,
  and an OK fetch after a gone clears `gone_at`. New columns: `liveness`,
  `gone_run`, `readmitted`, `held_since`. `gone_source` and the run-log gone
  attribution were removed.

### Fixed

- `llens geocode --dry-run` now prints its `pending` count. The refresh's paid gate reads
  that count from the dry run; without it every refresh stopped at geocode with "its
  --dry-run printed no pending" (seen on a refresh with 10 addresses queued, 2026-11-18).

## [0.1.0] - 2026-11-13

### Added

- Initial public release: offline analysis of classified-listing crawls, as a
  scrape-harness consumer. The `llens` CLI and its pipeline (`pipeline.toml`),
  `llens refresh`, the record schemas, the derived Postgres export, the
  Grafana board tooling, and a synthetic test site (`tests/site`). Every site
  specific lives outside the repository.
- Community files (licence, code of conduct, contributing guide, security
  policy, issue and pull request templates) and CI: `gitleaks` over the whole
  history and `nix flake check`, behind one required `gate` check.

[Unreleased]: https://github.com/ismailkattakath/listing-lens/commits/main
[0.1.0]: https://github.com/ismailkattakath/listing-lens/commit/3943304
