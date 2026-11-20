# listing-lens

[![ci](https://github.com/ismailkattakath/listing-lens/actions/workflows/ci.yml/badge.svg)](https://github.com/ismailkattakath/listing-lens/actions/workflows/ci.yml)
[![licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![Nix flake](https://img.shields.io/badge/Nix-flake-5277C3.svg?logo=nixos&logoColor=white)](flake.nix)

Offline analysis of classified-listing crawls, built on
[scrape-harness](https://github.com/ismailkattakath/scrape-harness). Every site specific lives outside this
repository; the tests run on a synthetic site (`tests/site`).

The operator's guide is [`CLAUDE.md`](CLAUDE.md); the design and every decision are in [`docs/design.md`](docs/design.md).

## `llens refresh`: the one on-demand update

One command runs the whole chain, in this order, and only when the operator starts it (no schedule):
crawl-lists (one per site list, in the configured order) → fetch-ads → reparse → geocode → derive →
derive-advertisers (v1.1, then v2) → places-resolve → services → export-db (`--grant-role board_ro`).
The steps are declared in `pipeline.toml` `[crawl.refresh]`; scrape-harness runs them by doit, each through
its own command, so every safety stays on (egress preflight, robots.txt, cooldown and circuit breaker, stop on
any challenge, politeness).

```sh
llens refresh --dry-run                          # the run card + the bounds; nothing is sent or saved
secret exec ... -- llens preflight refresh       # offline: each secret present or MISSING (by name), signers, cooldown
secret exec ... -- llens refresh                 # no approval yet: prints the bounds + plan_hash, exits 10
llens approve <plan_hash>                        # ONE Touch ID signs the bounds (single use, 12 h; --ttl, max 24 h)
secret exec GEOCODE_KEY="${LLENS_GEOCODE_KEYCHAIN_ITEM:-llens:geocode:apikey}" LLENS_DB_URL=<keychain item> -- \
  llens refresh --approve-network                # runs; stops before geocode if it has paid queries:
secret exec ... -- llens refresh --approve-network --geocode-count N   # N = the printed count, exactly; 0 needs none
```

- **Preflight first:** `geocode` needs `GEOCODE_KEY` and `export-db` needs `LLENS_DB_URL` (`secrets_env` in
  `pipeline.toml`). `refresh` refuses with exit 2 before any plan when one is unset, so plan it under the same `secret exec`
  as the run (`...` above: the two names and their Keychain items). `preflight` says which is missing.
- **Pick the tunnel:** `--proxy-only <text>` or `--proxy-exclude <text>` (repeatable) choose the `[crawl.proxy]`
  url for a new plan. Do not edit the site file's list for it.
- **Check the database:** `llens db check --approve-network` runs every board target read-only and prints
  counts only. `llens db api-check --approve-network` proves the Data API's access rules by status codes.
  `db export` and `db token` are `export-db` and `api-token` by another name.
- **From any shell:** `nix run .#llens -- refresh --dry-run` loads this checkout's `.env*` and `.envrc.local`
  (once direnv allows it), so no `nix develop -c` is needed. `nix run .#preflight -- refresh` is the same check.
- **The approval is bounded:** site, egress, proxy, rate, both list crawls' exact arguments, at most so many
  list pages and ad pages, the fetch reasons (new / card_changed / stale_7d), plus one robots.txt check per site
  step. A step outside them stops the chain (exit 3) and spends the approval.
- **Size it with headroom:** `--max-ad-requests` ≈ the ads you expect × 1.5. A resume after a crash re-charges
  that step's ceiling, and a list crawl that crashed after it started cannot be resumed (its list counts as
  crawled), so a tight bound turns a resume into a breach.
- **Resume:** rerun the same command; finished steps are skipped (STATE/refresh/<run_id>.json). An egress
  refusal (VPN in the wrong place) refunds and resumes. A challenge or cooldown (exit 5/6) ends it.
- **The geocode count is confirmed, not signed:** it is only known after fetch-ads, so `--geocode-count N` must
  equal it exactly.
- **places-resolve lag:** it reads derive's output and writes `vocab/places.ttl`, which the extractor reads
  in geocode and derive, so a newly resolved place reaches hints on the NEXT refresh (accepted).
- **Timing (a hint, not a schedule):** ad publish times suggest ~1–2 am site-local time catches the evening peak.
  Each run adds a revisit, which feeds the price-trend, survival and sponsored questions.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md). Report security
issues privately, as [SECURITY.md](SECURITY.md) describes. Changes are listed in [CHANGELOG.md](CHANGELOG.md).

## Licence

[MIT](LICENSE) © 2026 Ismail Kattakath
