# listing-lens

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
llens refresh                                    # no approval yet: prints the bounds + plan_hash, exits 10
llens approve <plan_hash>                        # ONE Touch ID signs the bounds (single use, 12 h; --ttl, max 24 h)
secret exec GEOCODE_KEY="${LLENS_GEOCODE_KEYCHAIN_ITEM:-llens:geocode:apikey}" LLENS_DB_URL=<keychain item> -- \
  llens refresh --approve-network                # runs; stops before geocode if it has paid queries:
secret exec ... -- llens refresh --approve-network --geocode-count N   # N = the printed count, exactly; 0 needs none
```

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
