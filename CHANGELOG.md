# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version is the one in `pyproject.toml`.

## [Unreleased]

- Pin scrape-harness d03c24f: an approved networked run (refresh included) keeps the Mac awake until it exits (`caffeinate -i -w <pid>`); `LLENS_NO_CAFFEINATE=1` opts out.

### Added

- `llens db`: the derived database's commands under one name.
  - `db export` is `export-db` and `db token` is `api-token`: the same
    argument definition, the same function, the same gate and exit codes.
    `cli spec` lists them as their own rows with the target's options (cli.v1
    aliases are same-level only), and their help starts `alias of`.
    `pipeline.toml` declares each like its target; a test fails on a difference.
  - `db check`: runs every Grafana board target read-only against the export
    DB. All is executed, single and multi are EXPLAINed, and a query variable
    with no default is resolved live. It prints counts only: rows per target,
    empties, errors (class and SQLSTATE, never the message). `--json` prints one
    `db_check.v1` object. `--dry-run` counts the targets offline. Exit 1 on an
    error; an empty result is reported but does not fail.
  - `db api-check`: proves the Data API's access rules by status codes. The
    operator reads rows (2xx and rows > 0); an anonymous token gets 403; no
    token gets 401; schema li, a withheld table and a public RPC are
    unreachable (404 or 406). `--json` prints one `api_check.v1` object. It
    needs the site file's new `[api] rest_url` (the Data API endpoint).
  - Both need `--approve-network` (`paid`, like export-db) and their secret by
    name: `db check` `LLENS_DB_URL`, `db api-check` `LLENS_API_PASSWORD`.

- `llens grafana`: the boards' commands in the one CLI (it was `llens-grafana`).
  - `check`, `build [uid]`, `tf plan|apply`, `perms`, `ds-password` and
    `bootstrap`; `plan` and `apply` are short for `tf plan` and `tf apply`.
  - One parser definition serves both spellings. `llens-grafana <sub> --help`
    equals `llens grafana <sub> --help` but for the name, and both reach the
    same function. A test fails on a difference.
  - `pipeline.toml` declares each one. `check` and `build` are offline. The
    ones that call Grafana's API are `paid`, with their secrets by name
    (`GRAFANA_TOKEN`, plus `LLENS_PG_DSN`, `LLENS_TOFU_PASSPHRASE` where read).
    `bootstrap` is operator-only. Under `llens grafana` they need
    `--approve-network`, and the guard blocks them without it.

- `nix run .#llens -- <args>`, `nix run .#preflight -- <command>` and
  `nix run .#cli-spec`: the harness's apps, from `consumerTools` `cliPackage`.
  They load this checkout's `.env*` and `.envrc.local`, only once direnv
  allows it, so no `nix develop -c` is needed.
- `checks.claude-md-drift`: the route table in `CLAUDE.md` (between the
  `scrape-harness:routes` markers) must equal `llens claude-md`. Regenerate it
  with `llens claude-md --write`. The block is repo-only: no site file value
  enters it, and a test renders it with and without the site file.
- `secrets_env` in `pipeline.toml`, names only: `geocode` needs `GEOCODE_KEY`,
  `export-db` needs `LLENS_DB_URL`, `api-token` needs `LLENS_API_PASSWORD`.
  `llens preflight refresh` names the first two, `preflight api-token` the
  third, as present or missing. No value is ever printed.
- `llens api-token`: signs in to Neon Auth as the operator and prints a
  short-lived (~15 min) Neon Data API JWT, or copies it with `--copy`. It needs
  `--approve-network` and the site file's `[api] auth_url, email`. The
  password arrives only as `$LLENS_API_PASSWORD`, through
  `secret exec LLENS_API_PASSWORD=<keychain item> -- llens api-token --approve-network`,
  and is never printed. A failed sign-in is exit 11 and reports the HTTP status
  only.

### Changed

- `llens-grafana` is deprecated; it goes in the next release. It prints one
  line to stderr naming the new spelling, then runs the same function. Its gate
  is unchanged (no new flag). It now takes `--help`. Its old grammar still
  works: `--check`, a leading `apply`, and words anywhere in their old order.
- `nix run .#grafana-tf` and `.#grafana-admin` now run `llens grafana tf`
  and `llens grafana <sub>`, with `--approve-network` (running the app is the
  approval). They no longer call `llens-grafana`, so its removal leaves them
  working and they print no deprecation note. They take the documented forms
  (`plan|apply`, `perms|ds-password|bootstrap`); for the offline `check` use
  `llens grafana check`.
- Grafana's time macros have one rule, `model.strip_time_macros`: `db check`
  and the export test's EXPLAIN pass both use it. `$__timeFilter(...)` becomes
  TRUE even with nested parentheses; `$__timeFrom()` and `$__timeTo()` become
  the infinite bounds.
- The scrape-harness pin moves to 49f9cf5 (plugin 1.3.0): `cli spec`,
  `claude-md`, `preflight`, `refresh --proxy-only/--proxy-exclude`, and the
  run_report.v1 and diagnosis.v1 agent contracts. The switch goldens did not move.
- `llens refresh` now refuses with exit 2, before it plans, claims or sends
  anything, when `GEOCODE_KEY` or `LLENS_DB_URL` is unset. Before this, it
  stopped after the Touch ID. `refresh --dry-run` warns and goes on.
- `export_db.SITE_TZ` and `REGIONS` are read on first use, not at import. So
  `llens cli spec` and `llens claude-md` work with no site file (they read
  the repo's `pipeline.toml` only). The values are unchanged.
- `export-db@26`: every `api` view is gated on `auth.user_id()` (the request
  JWT's subject, from Neon's `pg_session_jwt`). A view answers only the user
  ids given as `--api-user`, which is required with `--api-role`. A token Neon
  Auth issues to anyone else, or no token at all, reads 0 rows, whatever the
  auth provider's sign-up settings let through. The views are
  `security_barrier`.
- `export-db@25`: `--api-role ROLE` rebuilds schema `api` on every load and on
  an unchanged export. It holds one owner-rights view per relation in the
  boards' allowlist (`READABLE`), for the Neon Data API. ROLE gets `SELECT` on
  `api` and loses every right on `li`. Without the flag, `api` is left empty
  (fail closed): the old views go with `li_old`.
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
