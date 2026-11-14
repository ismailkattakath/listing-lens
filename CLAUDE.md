# listing-lens

A typed, append-only pipeline for studying public listing pages (the site's source is a plug-in kept in DATA).
**The harness drives; the model assists** — read [`docs/design.md`](docs/design.md) first.

## Hard rules (enforced by hooks and `settings.json`, not by good intentions)

- **No data in this repo.** Two roots (ADR-0009): `$LLENS_DATA` (Google Drive, synced:
  observations, listings, raw/, findings, events, runs/ + logs, derived/, import/) and
  `$LLENS_STATE` (local, default `$XDG_STATE_HOME/listing-insights`, NEVER a sync folder:
  browser profile, crawlee queues, plans, approvals). Never write either path into the repo. Moving an
  old single root: `llens migrate --to-data D --to-state S --dry-run` (copies; the operator deletes the old).
- **No network without the operator, mechanically.** Every networked `llens` command (`crawl-lists`,
  `fetch-ads`, `ab`, `crawl resume`) refuses (exit 10) unless the operator approved that exact plan:
  1. the agent runs the command with `--plan` (or `llens plan <command> …`) and shows the operator the
     plan, its `plan_hash` and the run card's Scale/ETA, and tells him a Touch ID prompt is coming;
  2. the agent runs `llens approve <plan_hash>` (needs `LLENS_APPROVAL_SIGNERS` = the Drive allowed_signers):
     a notification shows the plan summary, then Secretive asks for Touch ID. The operator's FINGERPRINT
     is the approval (his Secure Enclave key signs it); a declined prompt approves nothing — never retry.
     Single use, expires (default 30 min); `llens approvals verify` audits the hash-chained ledger;
  3. the agent runs the command with the same arguments plus `--approve-network`.
  Ledger lines from before the switch (Keychain Ed25519) are kept but never honoured. Each ad
GET adds a view to a real person's ad. Plan with `--dry-run`/`--plan`. Never `curl` the site.
- **One request at a time, robots.txt obeyed.** A challenge stops the run (exit 5; a second one for the
  same site within an hour, across runs and A/B variants, writes a 24 h site cooldown, exit 6). The
  operator clears a gate in `llens browser open`. **`desktop` is the primary variant** for `crawl-lists`
  and `fetch-ads`: crawlee's fingerprint generator is on. `LLENS_VARIANT` or `--variant` names another.
- **Dedicated browser only.** crawlee launches the Nix-pinned Chromium into its own persistent
  profile under the data folder, over Playwright's pipe: no TCP debugging port, no CDP attach. Never drive the operator's daily browser or phone for crawling.
- **Egress preflight.** Every networked crawl first checks Cloudflare's trace endpoint and refuses (exit 8, no override) unless the egress is `$LLENS_HOME_COUNTRY` (default CA). A VPN must be off; never work around it.
- **Politeness ceiling.** Concurrency 1, ≤ 60 requests/min (default 7). Exceeding it needs
  `--approve-politeness-override`, which only the operator gives.
- **Secrets by name only.** The geocoding key reaches `llens` only as
  `secret exec GEOCODE_KEY="${LLENS_GEOCODE_KEYCHAIN_ITEM:-llens:geocode:apikey}" -- llens geocode --approve-network`.
- **`$LLENS_DATA/import/` is not pipeline input.** It holds foreign data with its own README; nothing may write it. The ONE reader is `llens export-db`, which loads `import/2026-11-07-phone-session/` read-only into the derived Postgres copy (operator decision 2026-11-08, ADR-0014); no pipeline stage reads it.
- **Synthetic fixtures only.** No real ad text, names, phones, URLs or coordinates in tests.
- **Push only with `nix run .#ship`** (the harness's ship): clean `main`, every commit signed, `leak-gate` (needs `$LLENS_FORBIDDEN_LITERALS`, a colon-separated list of files; fails closed), `nix flake check`, the darwin tests rebuilt, the aarch64-linux build and a live Claude Code smoke of the harness plugin; it stops at the first failure before `git push`. Never chain gates and a push in an ad-hoc shell line.

## Commands

| Do | Run |
|---|---|
| Shell with everything | `nix develop` (or `direnv allow`) |
| The whole update, on demand | `llens refresh` (one bounded Touch ID; see README.md) |
| Full gate | `git add -A && nix flake check` |
| Tests only | `pytest -q` |
| Which env vars are set | `nix run .#env-doctor` |
| Pipeline | `llens crawl-lists` → `llens fetch-ads` → `llens geocode` → `llens derive` → `llens qa-page` |
| After a challenge stop (exit 5) | operator: `llens browser open`, clear it by hand, close; then `llens crawl resume` |
| Depth-stratified ad sample (H4) | `llens fetch-ads --sample N --seed S [--subcategories a,b] [--allocation equal] --dry-run`, then `--plan` |
| New selector, no new views | `llens reparse [--site S] [--since T]` re-extracts from saved snapshots (`raw/`), offline |
| Run card | printed to stderr at every run start (stage N/7, I/O, scale, who/what, live-log tail line, copied to the clipboard; `LLENS_NO_CLIPBOARD=1` opts out) |
| Watch a run live | `tail -f "$LLENS_DATA"/runs/<run_id>.log.jsonl \| jq -c .` (path in `run.v1.log_ref`) |
| Site cooldowns | `llens cooldown status`; clearing is the operator's: `llens cooldown clear --site S --reason "..." --approve-operator` (appends, never deletes) |
| Primary variant | `desktop` (fingerprints on) for list and ad fetches. `LLENS_VARIANT` or `--variant` names another |
| Compare variants | `llens ab --subcategory <s> --pages 2` (list pages only; http, desktop, desktop-nofp, mobile) |
| Findings | `llens findings validate|append|current`; any contract: `llens validate <name> FILE` |
| Plug-ins registered | `llens plugins` |

## Where things live

| Path | Owns |
|---|---|
| `pipeline.toml` | THE declaration: stages, I/O, network classes, data files, listing-lens' names (`[consumer]`), its plug-ins and commands (`[plugins]`), its allow rules (`[claude]`). Read by the harness CLI, the plugin's hooks and the generated settings. |
| scrape-harness (flake input) | The generic core: CLI, crawl/challenge/browser/drivers, approvals, store, contracts, run card, and the Claude Code plugin (hooks, agents, skills, monitor, `/scrape-harness:plan-pipeline`). |
| `schemas/` | listing-lens' OWN contracts (`listing`, `observation`, `geocode` v1; `agents/` inputs). The harness owns `run`, `event`, `finding`, `ab`, `sample`, `approval`, `status`. |
| `tests/site/plugins/` | The tests' SYNTHETIC source. The real site's source (module, record sub-schemas, its pages and tests) is a DATA plug-in, loaded through the site file's `[pipeline.plugins]` (`python_path`, `schema_dirs`, `sources`); the core reads it through `site.source()`. |
| `src/listing_lens/{geocoders,extractors}/` | Geocoders and the text-hint extractor (`[plugins.geocoders]`, `[plugins.extractors]`). |
| `src/listing_lens/site.py` | The SITE config: the hint extractor's vocabulary (cities, areas, landmarks, aliases, street words, highways, ad words), the region radius, the places box/seeds/context, the GeoNames country, the boards' two lists + service-mode concepts + local-time place (`[boards]`, read at board build). Read at runtime from `$LLENS_SITE_CONFIG`, else `$LLENS_DATA/config/site.toml` (+ `vocab/hints.json`); absent = refuse. Never in git: tests use the synthetic `tests/site/`. |
| `src/listing_lens/commands.py` | The analysis stages `extract`, `geocode`, `derive`, `qa-page` (`[plugins].commands`). |
| `src/listing_lens/{derive,qa,geocache}.py` | Pin measurement, the QA page, the 30-day coordinate cache. |
| `.claude/settings.json`, `.claude/statusline.py` | GENERATED by `llens claude-settings --write` from pipeline.toml (deny rules, allow rules, Task tools, status line, the plugin); a drift check is `llens claude-settings`. |
| `tests/test_switch_goldens.py` | The phase-3 proof: run-card rows, file table, hook verdicts and the full offline e2e digest equal the pre-switch capture. |

## Adding a source (the common extension)

New module under `sources/` implementing the harness's `Source`, one line under
`[plugins.sources]` in pipeline.toml, a synthetic fixture page, a parse test. Its
site-specific fields go under `site.<name>` in the schemas. Nothing in the core changes.

## Expected plugins and skills (from user-level settings; none enabled here)

- `page-lab` — `page-lab:devtools` for inspecting a SAVED page before changing selectors. It is
  not the crawl driver (the dedicated browser is).
- `nix-dev-toolkit` — the flake's shape (dev shell, env catalogue, checks).
- `superpowers` (TDD, verification-before-completion), `pr-review-toolkit` / `code-review` — reviews.
