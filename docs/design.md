# ADR-0001 — The harness drives; the model assists

**Status:** accepted (first version). **Scope:** the whole repo.

## Context

The operator's brief: the Claude Code setup must be *deterministic and not depend on the model's
intelligence too much*; scripts, hooks and settings drive; standard plugins, connectors and skills
contribute intelligence; agents interface through standardised, typed expectations; LEAN, DRY,
modular, plug and play, reuse over reinvent.

## Decision — five testable principles

| # | Principle | Test that proves it holds | Mechanism |
|---|---|---|---|
| P1 | **Programs run the pipeline.** Every step that can be code is code; the model never sequences steps. | `llens` runs the whole pipeline with no model involved (`test_pipeline_e2e`). | `src/listing_lens/cli.py`, Nix `packages.default` |
| P2 | **Contracts at every boundary.** Records, agent input and agent output are JSON-Schema documents; nothing is parsed by luck. | Each example validates; each `examples/invalid/*` must FAIL (`checks.schemas`, `test_contracts`). | `schemas/*.v1.schema.json`, `contracts.validate` on every read/write |
| P3 | **Invariants are mechanical and fail closed.** Network politeness, no data in the repo, secret egress, agent output shape. | Each gate has an allow, a deny and a garbage-input test (`test_hooks`). | `.claude/hooks/guard.py`, `.claude/settings.json`, CLI refusal (exit 3) |
| P4 | **Plug and play.** Site, browser, geocoder and extractor are modules behind four `Protocol`s, loaded by entry-point name. | A second site = one module + one `pyproject.toml` line; the core names no implementation. | `interfaces.py`, `[project.entry-points."listing_lens.*"]` |
| P5 | **Intelligence plugs in at named seams only.** The model writes a new extractor rule or source module (code, then tests gate it) and interprets a summary (`finding-analyst`, output = `finding.v1`). | The agent has no write tools; its output is schema-checked by a hook, then again by `llens findings append`. | `.claude/agents/finding-analyst.md`, SubagentStop hook |

Data model (agreed before this repo): `observation.v1` = one sighting on a list page (cheap);
`listing.v1` = one version of an ad page (append-only; refetch only when new, card changed, or
>7 days old and still listed); `finding.v1` = one hypothesis test; `geocode.v1` = one paid answer,
cached forever. Raw facts are append-only under `$LLENS_DATA`; `derived/` is rebuilt.
Sampling: systematic every k-th page from a random start (depth spread; Cochran 1977 ch. 8), with
`sample_weight = page_count / pages_sampled`; equal allocation between strata for between-stratum
comparison (Cochran 1977 ch. 5). `stride 1` is a census.

## Choices, their evidence, and what they beat

| Choice | Evidence | Beat |
|---|---|---|
| `jsonschema` (+ `rfc3339-validator`, `rfc3986-validator`) at runtime; `check-jsonschema` in CI | Precedent: the 2020-12 reference implementation; both enforce `date-time`/`uri`, proven by `test_date_time_format_is_enforced` and the invalid-example gate | pydantic models (a second copy of every field list) |
| BeautifulSoup CSS selectors for site-specific cards, selectors copied from the userscript | Precedent: the userscript measured them on captured pages (counts in its comments) | re-deriving selectors; regexes over HTML |
| Entry points as the plug-in registry | Precedent: PEP 621 / `importlib.metadata`, the mechanism pytest plugins use | a hand-kept dict (would need a core edit per site) |
| Stdlib `json` for JSONL; stdlib `argparse` for the CLI | 15 lines of I/O; no feature of `jsonlines`/`click` is used | `jsonlines`, `typer` (extra deps for no behaviour) |
| `pytestCheckHook` inside the package build | Precedent: nixpkgs' standard; a red test means no package | a separate test derivation duplicating the build |
| Hooks in one stdlib-only script behind a `sh` wrapper that exits 2 when `python3` is absent | Data: a hook that errors with a non-2 code lets the tool call through, so the wrapper and a top-level `except` both exit 2 | hooks that import the package (would fail open when the dev shell is absent) |
| `mkPythonEditablePackage` for the dev shell, reading scripts/entry points from `pyproject.toml` | Precedent: nixpkgs' PEP 660 helper; one declaration of the entry points | `pip install -e` into an impure venv |
| Approved network runs still **ask** (hook) | Math: an agent that can add `--approve-network` itself would otherwise approve itself | a flag-only gate |

**Weakest assumption (ADR-0001), resolved 2026-11-07 (ADR-0005):** the first real ad fetch confirmed both. Originally: the ad-page selectors for the *City* row and the pin (`meta[itemprop]`) come
from the scratchpad harvest script, not from a captured HTML file; the fixtures follow those shapes.
The first approved real fetch should be checked against one saved page before a full run.

## ADR-0002 — Reuse study adoptions, dedicated browser, challenge stops (2026-11-07)

*Partly superseded by ADR-0003 the same day; superseded rows are marked.*

Inputs: the oss-scout reuse study and the operator's decisions relayed by the team lead.

### Adopted, rejected, pending

| Item | Decision | Evidence | Where |
|---|---|---|---|
| **Concurrency 1 for every page GET** | Adopted; fixes prototype defect (6 parallel ad fetches) | Since ADR-0003: crawlee `ConcurrencySettings(max_concurrency=1, max_tasks_per_minute=7)`. A ceiling of concurrency 1 / 60 per minute (~1 req/s) is enforced in `crawl.Politeness`; exceeding it needs `--approve-politeness-override`, and the hook asks the human. Test: the fixture client records max in-flight = 1 | `crawl.py`, `test_crawl.py` |
| **Geocode cache keyed by place_id, lat/lng TTL 30 days** | Adopted; fixes a compliance defect | Precedent: Google Maps Platform Service Specific Terms (lat/lng ≤ 30 days, place_id indefinitely). `geocode.v1` now requires `place_id` + `expires_at`. The cache is rewritten on every `llens geocode`, stripping expired points. A stale entry is re-resolved **by place_id**, and `derive` ignores expired points | `geocache.py`, `test_geocache.py` |
| **geopy** (GoogleV3 + RateLimiter) | Adopted | In nixpkgs (2.4.1 in our pin, not 2.5; `place_id=` and `RateLimiter` exist there). The geocoder seam stays ours, so Nominatim is one module | `geocoders/google.py` |
| **extruct** (schema.org microdata / JSON-LD) | Adopted | In nixpkgs 0.18. It replaces our hand-written `meta[itemprop]` and JSON-LD readers. `schemaorg.pins()` walks any GeoCoordinates node, so it is site-agnostic | `schemaorg.py`, the site's source |
| robots.txt | Adopted | Since ADR-0003: crawlee `respect_robots_txt_file=True` (it parses with protego, nixpkgs 0.6.2); a Disallow skips the request (event `robots_disallowed`); `Crawl-delay` lowers the rate | `crawl.py` |
| ~~Playwright `connect_over_cdp`~~ | **Superseded by ADR-0003** | crawlee cannot attach over CDP, so crawlee's PlaywrightCrawler LAUNCHES the pinned Chromium into the persistent profile instead | `drivers/browser.py` |
| **Nix-pinned Chromium = `playwright-driver.browsers-chromium`** | Adopted | nixpkgs `chromium` does not build on aarch64-darwin (measured: `meta.platforms` excludes it). This is the hash-pinned Chrome-for-Testing build matching the pinned Playwright | `flake.nix` `browsersFor` |
| **crawlee-python** | **Adopted, ADR-0003** (operator decision 2026-11-07) | Hard-dependency evidence kept there | `crawl.py`, `nix/python-packages.nix` |
| **mcpc** / any MCP runner | Not needed (operator accepted) | No MCP hop is left: crawlee drives Playwright. The hand-written `mcp-run.mjs` and the official-SDK client that replaced it are both gone | — |
| Crawlee PlaywrightCrawler + fingerprints, impit | **Adopted, ADR-0003.** `desktop` (fingerprints on) is the primary variant | — | — |
| fingerprint-suite (standalone), proxy-chain | Not used | `desktop` uses crawlee's own fingerprint generator | — |
| Crawlee JS, libpostal, usaddress, DVC/MLflow | Rejected for now | Remote CDP only in unreleased v4 and forces incognito; parsers do not *find* phrases; too heavy for JSONL | — |

### Dedicated browser
- The Nix-pinned Chromium runs with its own profile at `$LLENS_DATA/browser-profile` and `--disable-extensions`. Since ADR-0003 crawlee's PlaywrightCrawler launches it over Playwright's pipe, so no CDP port is opened at all (stricter than a localhost port).
- It never touches the operator's daily browser or the phone. Crawls run it **headless**. Only `llens browser open` runs it headed.
- The binary is configuration: `LLENS_CHROMIUM` overrides it. crawlee has no attach-over-CDP option, so `LLENS_BROWSER_CDP_URL` was removed with ADR-0003.
- A profile already in use (`SingletonLock`) is refused, not shared.
- Since ADR-0003 the UA depends on the variant: `desktop` sends crawlee's generated fingerprint, `desktop-nofp` sends Chromium's own `HeadlessChrome`, `mobile` sends the configured mobile UA.

### Challenge stop (fixed detectors, `challenge.py`)
| Detector | Code |
|---|---|
| HTTP 403 / 429 / 503 | `http_403` / `http_429` / `http_503` |
| redirect to a /login-like path | `login_redirect` |
| reCAPTCHA / hCaptcha / Turnstile iframe, **only when corroborated** by status ≥ 400 or the expected element being absent (ADR-0004) | `captcha_iframe` |
| `cf-mitigated` response header (Cloudflare served a mitigation) | `cf_mitigated` |
| Cloudflare challenge script markers (`cf-chl`, `/cdn-cgi/challenge-platform`), **only when corroborated** by status ≥ 400 or the expected element being absent (ADR-0004) | `challenge_marker` |
| challenge page title ("Just a moment…") | `challenge_title` |
| expected content missing + password field | `signin_wall` |
| expected content missing + age/terms text | `consent_gate` |
| expected content missing, nothing recognised | `unexpected_page` (fail closed) |

Any hit stops the run. The request stays pending in `runs/<run_id>.json` (`run.v1`, rewritten atomically after every request), and an `event.v1` `challenge` records the URL and reason. The run then exits 5.
- The operator may run `llens browser open`, clear the challenge once by hand, then run `llens crawl resume`. Resume continues the same run, and done requests are never re-fetched.
- The counter is per SITE within 1 hour, across runs and A/B variants (ADR-0004). The second challenge for a site in that window aborts the run and writes a site `cooldown` event (24 h); every crawl command for that site refuses to start before it ends. The event log is the marker; there is no second state file.

### Page variants and the A/B comparison
- `variants.py`: `desktop` is the primary variant (crawlee's fingerprint generator on). `LLENS_VARIANT` or `--variant` names another. `mobile` uses a mobile UA built from the real browser version, 412×915, touch.
- Neither variant logs in or stores an account. Both use the profile's own context, so a gate the operator cleared stays cleared. `isolated=True` gives a fresh cookie-free context. **Open question:** the operator said "desktop cookieless"; flip `isolated` if that means a fresh context per run.
- `llens ab --pages N` (default 2) fetches the SAME list pages in each variant and never an ad page. It writes only `derived/ab-*.json` (`ab.v1`), with per-field card coverage and schema.org pins found. A challenge stops that variant only. A cooldown stops the rest.

## ADR-0003 — crawlee, impit, and desktop fingerprints (operator decision, 2026-11-07)

**Decision (2026-11-07).** `desktop` is the primary page variant for list and ad fetches. The browser path is crawlee **PlaywrightCrawler** with crawlee's **fingerprint generator on**. The HTTP variant `http` is crawlee `HttpCrawler` with its default **ImpitHttpClient**.

Kept with that choice:
- robots.txt respected;
- concurrency 1, about 1 request per second;
- challenge stop, the persisted queue, the headed handoff, resume and the 24 h cooldown.

### Evidence that forced the question (measured on the crawlee 1.11.1 sdist, sha256 7b504f8c…)
- `impit>=0.13.2` and `proclimits>=0.2.0` are core `requires_dist`, not extras.
- `impit` is imported unconditionally: `http_clients/__init__.py`, `crawlers/_basic/_basic_crawler.py:69` (default `ImpitHttpClient()`), and `_utils/public_suffix.py:9`.
- Neither package is in nixpkgs. `nix/python-packages.nix` packages:
  - impit 0.15.1 **from source** (maturin + pyo3). `fetchCargoVendor` pins its five git-rev forks under one hash. It needs `RUSTFLAGS="--cfg reqwest_unstable"`, upstream's own `build.rustflags`, which the Nix hook's `RUSTFLAGS` would otherwise override (measured: reqwest refused to compile).
  - proclimits 0.2.0 and crawlee 1.11.1 as pinned pure-Python wheels. crawlee's sdist needs the `uv_build` backend for no behaviour difference.

### How crawlee is wired, and where our rules override its defaults
| crawlee default | Our setting | Why (evidence) |
|---|---|---|
| retry on blocked status/captcha (session rotation) | `retry_on_blocked=False`, block statuses 401/403/404/429/503 in `ignore_http_error_status_codes` | A blocked response goes to `challenge.detect`, which stops the run. The handler calls `crawler.stop()` and raises, so crawlee reclaims the request and it stays pending for `llens crawl resume` (test: 429 → exit 5, resume fetches only that page) |
| `ThrottlingRequestManager` (suggested for robots Crawl-delay) | **not used** | Source: its 429 path raises `RequestThrottledError` before the handler and defers/retries the request (`_basic_crawler.py`, `_defer_throttled_request`). `Crawl-delay` is still honoured: `RobotsTxtFile.get_crawl_delay()` lowers `max_tasks_per_minute` |
| `max_concurrency=100`, unlimited rate | `max_concurrency=1`, `max_tasks_per_minute=7`; ceiling 1 / 60 unless `--approve-politeness-override` | The politeness rule. The hook asks the human for the override |
| `max_request_retries=3` | 1 | One retry for transient errors |
| incognito off, temp profile | `user_data_dir=$LLENS_DATA/browser-profile`, `use_incognito_pages=False` | Handoff cookies persist. **Persistent + fingerprints is compatible**: the generator applies via `AsyncNewContext(browser=PlaywrightPersistentBrowser)`, whose `new_context` calls `launch_persistent_context(user_data_dir)` (source read) |
| headers generated even with `fingerprint_generator=None` | `extra_http_headers={}` for the fingerprint-OFF variants | Measured 2026-11-07 on a localhost server: `desktop-nofp` sent a generated Windows-Chrome UA until this; now it sends Chromium's own `HeadlessChrome`. `mobile` sends the configured Android UA |
| Playwright's `chrome-headless-shell` for headless | `channel="chromium"` | The pin (`playwright-driver.browsers-chromium`) carries the full Chromium only. Measured: without it the launch fails "Executable doesn't exist … chrome-headless-shell" |
| process-wide storage cache | `storage_instance_manager.clear_cache()` per run | Measured: a 2nd run in one process hit "Lock … bound to a different event loop" |
| queue in `./storage` | `$LLENS_DATA/crawlee`, `purge_on_start=False`, named queue per run (`run.v1.queue`) | No data in the repo; resume reopens the same queue (dedup by `unique_key`) |

### Variants (`variants.py`; the backend is per variant, and a source sets per-kind defaults)
| variant | backend | fingerprints | measured on localhost 2026-11-07 |
|---|---|---|---|
| `http` | impit-http | impit's own (Firefox) | 200; UA `… Firefox/128.0` |
| `desktop` (primary) | browser | ON | 200; generated Chrome UA (Mac one run, Windows the next) |
| `desktop-nofp` | browser | OFF | 200; `HeadlessChrome` |
| `mobile` | browser | OFF | 200; Android UA |

`llens ab` compares the same N=2 list pages across variants (no ad pages). It does not choose the primary variant. A challenge stops that variant only; the next variant still runs, and the report records `stopped`. A cooldown stops all of them.

### Production decision: fingerprints ON (operator, 2026-11-07)
**Decision.** The production crawl variant is **`desktop` with crawlee's fingerprint generator ON**, for both list and ad fetches. The operator chose it knowingly, after the A/B showed that only the honest headless browser was challenged.

This replaces the earlier "learning exercise for the A/B comparison only" framing. Plainly: **fingerprints reduce how often Cloudflare's bot scoring challenges us.**

**Evidence** (`llens ab`, 2026-11-07T21:50:49Z, the usual egress, the same 2 list pages, no ad pages; `derived/ab-2026-11-07T21_50_49Z.json`, finding H5):

| variant | cards | challenged? | coverage (title / city / name / age / ethnicity · verified · rate · available) |
|---|---|---|---|
| `http` (impit) | 200 | no | 100% · 59% · 20.5% · 2% |
| `desktop` (fingerprints ON) | 200 | no | 100% · 59% · 20.5% · 2% |
| `desktop-nofp` (`HeadlessChrome`) | 0 | **yes**: 403, `cf-mitigated: challenge`, "Just a moment…" | — |
| `mobile` | 200 | no | 100% · 59.5% · 20.5% · 2% |

`bumped_at` and pins were 0% on every variant: they live on ad pages, not list pages. `mobile` ran after the challenged variant and still passed, which weakens (but with n=1 does not exclude) an ordering explanation.

**Unchanged and absolute.** The fingerprint choice changes how often a challenge happens, not what happens when one does:
- **Any detected challenge stops the run** (exit 5). The second for a site within an hour writes a 24 h site cooldown (exit 6). No variant retries or backs off past a block (`retry_on_blocked=False`; no `ThrottlingRequestManager`). The operator clears a gate in `llens browser open`, then `llens crawl resume`.
- **Concurrency 1**, at most ~1 request per second (default 7 per minute), unless the operator passes `--approve-politeness-override`.
- **robots.txt respected**; Crawl-delay lowers the rate.
- **Egress preflight**: home country only (exit 8, no override).

### Pipe only: the browser opens no TCP port (operator, 2026-11-07)
- Every launch goes through Playwright's own `launch_persistent_context`, which drives Chromium over `--remote-debugging-pipe`. That covers crawlee's PlaywrightCrawler and the headed `llens browser open`.
- No path passes a debugging-port flag, and no code attaches over CDP.
- Tests: `tests/test_browser_pipe.py` asserts the launch args of every path, and a source scan fails on any port flag or `connect_over_cdp`.
- Measured 2026-11-07 on aarch64-darwin, launching the pinned Chromium exactly as crawlee does: 7 Chromium processes, `--remote-debugging-pipe` present, no port flag, **0 listening TCP sockets** (`lsof -iTCP -sTCP:LISTEN` on those PIDs).

### Notes
- `llens browser open` launches the SAME pinned executable on the SAME `user_data_dir` headed, via Playwright's persistent launch (pipe). Without `LLENS_CHROMIUM`, Playwright resolves the pin from `PLAYWRIGHT_BROWSERS_PATH`, the path `browser.executable()` returns.
- Google geocode terms (ADR-0002): **re-check the live Google Maps Platform Service Specific Terms page** before relying on the 30-day figure. It is from the reuse study's citation, not re-fetched here.
- geopy stays 2.4.1 (operator accepted).

## ADR-0004 — Site-scoped cooldown and challenge evidence (after the first real run, 2026-11-07)

**What happened.** In the first operator-approved `llens ab`, all four variants got HTTP 403 on list page 1 within 30 s (http, desktop, desktop-nofp, mobile). Each variant stopped correctly: exit 5, no retries, 0 ad pages.

**Defect 1, the cooldown hole.** Each A/B variant is its own run_id, and the counter was per run, so "two challenges → 24 h cooldown" never fired. That allowed 4 refused requests in 30 s with no cooldown.
- Fix: `crawl.recent_challenges(site)` counts `challenge` events for the SAME SITE within `CHALLENGE_WINDOW` (1 h), across runs and variants.
- The second one writes a site-scoped `cooldown`. `cooldown_until(site)` makes every later run for that site refuse to start (exit 6) before it sends a request. A cooldown without a site (older events) applies to every site: fail closed.
- In `llens ab`, a cooldown ends the experiment: the remaining variants never start, and the exit code is the most severe one seen (6 outranks 5).
- Test: a 4-variant A/B where every page is 403 makes exactly 2 page requests (variants 1 and 2), writes one cooldown and exits 6. A rerun makes 0 requests.

**Defect 2, no evidence.** `event.v1` held only `http_403`, which cannot tell a Cloudflare/WAF challenge from a plain deny or an IP block.
- Fix: every `challenge` event now carries an optional `evidence` object, taken from the blocked response itself with **no extra request**:
  - `status`;
  - the allowlisted headers `server`, `cf-ray` and `cf-mitigated`;
  - the page `title`;
  - a ≤500-char visible-text `excerpt`.
- No other header is read, so cookies (`set-cookie`) never enter it. Long opaque strings (≥32 token-like chars) in the title or excerpt are replaced with `[redacted]`.
- Tests: Cloudflare-shaped headers are captured, a `set-cookie` token never reaches `events.jsonl`, and tokens in page text are redacted.

### Root cause, and the egress preflight (operator decision, 2026-11-07)
**Cause.** The 403s were a **Cloudflare managed challenge triggered from a VPN egress**. The evidence run (http variant, robots plus 1 list page) recorded:
- `server: cloudflare`, `cf-mitigated: challenge`, title "Just a moment…", cf-ray colo FRA.
- At the same time, Cloudflare's trace for this Mac reported `loc=AT`, `colo=FRA`, `warp=off`: a WireGuard VPN exiting in Vienna, not the operator's home connection.

**Preflight** (`egress.py`). Before any networked crawl (`crawl-lists`, `fetch-ads`, `crawl resume`, `ab`):
- `llens` GETs Cloudflare's own diagnostic endpoint (`LLENS_EGRESS_TRACE_URL`, default `https://www.cloudflare.com/cdn-cgi/trace`), never the target site.
- It keeps only `loc`, `colo`, `warp` and `gateway`. The trace's public `ip` is deliberately **not** stored.
- It records them in `run.v1.egress` and in every challenge event's `evidence.egress`.
- It **refuses to start, exit 8**, unless `loc` equals `LLENS_HOME_COUNTRY` (default `CA`). Exit 7 was already the politeness ceiling.
- There is **no override flag**: the operator fixes the network (disconnects the VPN) and re-runs. The run is left `stopped`, so `llens crawl resume` works afterwards.
- It fails closed: an unreachable or unparsable trace refuses exactly like a foreign country.
- In `llens ab`, a refusal stops the remaining variants.
- Offline backends send nothing, so they skip it.

**Tests** (`test_egress.py`, file:// fixture traces):
- CA passes, and the IP is never kept.
- AT is refused.
- The home country is configurable.
- An unreachable trace refuses, and so does a garbage one.
- A run on a VPN makes 0 requests, not even robots.txt.
- A challenge event carries the egress.

This prevents accidental VPN crawling, which made every first run look like a bot from Austria.

### False positive: the Cloudflare beacon on a normal page (2026-11-07)
**What happened.**
- After the preflight went in, a single http request at 21:41:48Z (the usual egress, VPN off) got the REAL page: status 200, `server: cloudflare`, no `cf-mitigated`, and the site's own list-page title.
- `challenge.detect` still returned `challenge_marker`. `/cdn-cgi/challenge-platform` matched Cloudflare's background bot-detection **beacon** (`/cdn-cgi/challenge-platform/scripts/jsd/main.js`), which Cloudflare injects into normal pages.
- That false hit was the site's second challenge within the hour, after the real 21:37 VPN one. So it wrote a **false 24 h cooldown**.

**Fix.** A marker alone no longer counts. It is a challenge only when corroborated:
- by status ≥ 400;
- or by a `cf-mitigated` header (now its own detector, `cf_mitigated`);
- or by a challenge title (`challenge_title`);
- or by the expected page element being absent.

Regression tests:
- A real-shaped 200 list page with the beacon script and its cards → `None`.
- A true managed challenge (403 + `cf-mitigated: challenge` + "Just a moment…") is still a challenge, with that evidence captured.
- Every corroboration path has a case.

**Not touched.** The operator's `events.jsonl` and the false cooldown in it stay as they are. Clearing them is the operator's decision. Until it is cleared or expires, every crawl for the site exits 6.

**The same rule for `captcha_iframe`** (team lead's request, applied before any real page tripped it):
- A captcha iframe counts only when corroborated: status ≥ 400, `cf-mitigated`, a challenge title, or the expected element absent. The header and title cases already return their own codes first.
- Fixture: a 200 ad page with its `#preview-description` and an embedded reCAPTCHA contact form passes.
- The same iframe on a 403 page, beside a `cf-mitigated` header, or on a page missing its content still stops the run.

### Audited cooldown clear (operator decision, 2026-11-07)
- **`llens cooldown status [--site S]`** lists the cooldowns still blocking: site, until, reason, the run_id that wrote it, and when.
- **`llens cooldown clear --site S --reason "<text>" --approve-operator`** **never deletes anything**. It APPENDS an `event.v1` of kind `cooldown_cleared`, carrying the site, the recorded reason, the clear time (`at`) and `clears` (the cleared cooldown's run_id, at and until).
- Without `--approve-operator` it refuses (exit 9) and appends nothing. A blank reason is refused (exit 4). With nothing active, it appends nothing. The Bash hook asks the human for any `cooldown clear`.
- **Semantics:** the log's ORDER, not second-resolution timestamps, decides what a clear covers.
  - A cleared cooldown no longer blocks.
  - Challenges logged before the clear no longer count toward the 1 h window, so the next one counts from zero and the one after it writes a new cooldown.
- Tests (`test_cooldown_clear.py`):
  - clear → status empty;
  - the earlier log lines are byte-identical afterwards (append-only);
  - a new challenge after the clear exits 5, and the next exits 6;
  - clearing without `--approve-operator` fails with nothing appended.

### Found while testing it: run_id collision
- Two runs created in the same second got the same run_id. The run_id names crawlee's persisted queue, so the second run silently shared the first one's queue and fetched nothing.
- Back-to-back `llens ab` variants could hit this.
- Fix: run_ids carry a random 6-hex suffix (`secrets.token_hex(3)`), with a regression test.

**Not changed:** the detectors, exit codes 5/6, the human handoff and resume, the politeness ceiling, and the pipe-only browser.

## ADR-0005 — Raw ad snapshots and offline reparse (operator decision, 2026-11-07)

**Context.** The first real ad fetch (egress CA, no challenge) verified the two guessed selectors:
- the City row matched the card's city and the URL slug;
- the pin read a plausible in-region coordinate;
- `bumped_at` (-05:00), `first_published` (-07:00), a 418-char description and 11 media URLs also parsed.

It also showed gaps: `contact.*` and `service_mode` have no selectors yet. Every GET adds a view to a real person's ad, so a new selector must not cost new views.

**Decision.**
- Every `fetch-ads` version writes a gzipped snapshot of the fetched HTML to `$LLENS_DATA/raw/<site>/<ad_id>/<fetched_at>.html.gz`, before the listing is appended.
- `listing.v1.raw_ref` = {`path` relative to the data dir, `sha256` and `bytes` of the **uncompressed** HTML}. gzip `mtime=0`, so the archive is deterministic.
- Snapshots never enter the repo: the data-dir guard, `.gitignore` (`*.html.gz`, `/raw/`) and the write hook all apply.
- **`llens reparse [--site S] [--since T]`** re-extracts from those snapshots **offline**. It loads no fetch backend and opens no socket (both asserted in tests).
  - It checks each snapshot's sha256 and bytes first; a mismatch refuses, exit 2.
  - It appends a `reparse` version (new `fetch_reason`) only where the current parser's fields differ from the latest version parsed from that snapshot. It tags that version `derived.method = <source.parser>` (e.g. `<source>-ad@1`; bump it with each new selector).
  - A reparse keeps the page's ORIGINAL `fetched_at`, so the refetch rule (new / card_changed / stale_7d) is untouched. Being appended later, it wins the latest-version tie.
- Tests (`test_reparse.py`):
  - every fetched version has a snapshot whose sha256 matches;
  - an unchanged reparse appends nothing (byte-identical file);
  - a new selector yields 3 `reparse` versions that become current, keep `fetched_at`/`raw_ref`, and plan no refetch;
  - a tampered snapshot is refused;
  - `--since` filters.

**Beat:** re-fetching the ad (costs a view per ad per selector change); storing parsed DOM fragments only (a future selector may need markup we did not keep).

### The site source's parser versions (@2 to @5)
The site's selectors, their measured counts and apply steps live with the source in DATA (the site source
plug-in's NOTES.md). The site-agnostic rules they established:
- A contact value is stored exactly as the page shows it BEFORE "show"; the full value is never requested.
- A row or section the page hides (`display: none`) is absent, never parsed; a facts row is kept as displayed.
- A version bump of `source.parser` plus `llens reparse --site <site>` applies a new selector offline, with no views.
- **Note:** the first fetched ad predates snapshots and has none. The next approved fetch provided the first one.

**History rewrite (2026-11-07).** History was rewritten on 2026-11-07 to remove real ad data from 9ffb14e onward (operator-approved); a backup bundle is kept outside the repo. The tip tree was unchanged by the rewrite, and every rewritten commit is re-signed. Rule this enforces: docs describe what a real page showed by its shape or kind, never by its values.

### Per-run request accounting (2026-11-07)
`run.v1.requests` (and each `ab.v1` variant) records `list_pages`, `ad_pages`, `robots` and `other`, the GETs to the target site, plus `egress_trace`, which never goes to the site.
- Counting is at the backend layer. Pages come from crawlee's `pre_navigation_hook`: every attempt, HTTP or browser, retries included. robots.txt and anything else come from a wrapper on the backend HTTP client's `send_request`.
- A resume adds to the earlier counts.
- Tests: a fixture crawl/fetch reports exact counts, and their sum equals the fixture client's own log of served requests (an independent tally). A preflight refusal reports 0 site requests and 1 trace.
- **Found by measuring:** each run sent **two** robots.txt GETs, ours (to read Crawl-delay) plus crawlee's own. The run now seeds crawlee's robots cache with the file it already loaded, so it is 1. That cache is a private attribute of crawlee 1.11.1; the `robots == 1` test is the tripwire on an upgrade.

## ADR-0006 — Network approval is mechanical, inside `llens` (operator decision, 2026-11-07)

**Context.** The repo's Claude hooks fire only for sessions rooted in this repo, so no real run so far was actually gated by them; `--approve-network` was a flag anyone could type. Approval has to live in `llens` itself, so it holds whichever agent, session or script calls it.

**Decision.**
1. **Plan.** Every networked command (`crawl-lists`, `fetch-ads`, `ab`, `crawl resume`) takes `--plan`, and `llens plan <command> <args>` is the same thing. It prints a canonical run plan and its `plan_hash`, sends nothing, and saves the plan under `$LLENS_DATA/plans/`. The plan holds everything that decides what is sent and to whom:
   - command, site, variants and backends;
   - subcategory, region, page count, stride, start, seed, max pages, or the exact ad URLs and reasons, or the run_id;
   - concurrency, rate, any politeness override, and the egress country.
   The hash is sha256 of sorted-key JSON. A networked sample with stride > 1 needs `--start` or `--seed`, so the approved plan is the run that happens.
2. **Approve.** `llens approve <plan_hash> [--ttl 30m]` (max 24 h) refuses unless stdin is an **interactive TTY**. It shows the plan and requires the human to type the first 6 characters of the hash. Then it appends an `approval.v1` `approved` line to `approvals/ledger.jsonl` (mode 0600, directory 0700).
3. **Run.** The command recomputes its plan_hash and **refuses with exit 10, before any request**, unless a matching, unexpired, unused approval exists. It then appends a `used` line, so the approval is single-use, and records `approval_ref` in `run.v1`. One approval covers all variants of one `llens ab`.
   - `--approve-network` is still required as well; alone it no longer suffices.
   - Offline backends (fixtures) need no approval.

**Tests** (`test_approvals.py`):
- no approval → exit 10, 0 requests, no run record;
- `llens plan` and `--plan` give the same, stable hash;
- approved → runs once and records `approval_ref`; the same approval reused → 10;
- ledger mode 0600;
- a different plan (another start page) → 10;
- expired → refused;
- a non-TTY `llens approve` → 10; a wrong confirmation → refused, nothing written.

**Agent procedure (corrected 2026-11-07).**
1. The agent runs the command with `--plan` and shows the operator the plan and its hash.
2. The agent prints the single-line approve command and copies it to the clipboard (`pbcopy`): `cd <listing-lens checkout> && LLENS_DATA=<data dir> nix run .#default -- approve <plan_hash>`.
3. The operator pastes it into **his own terminal tab** and types the 6-character confirmation there.
4. The agent then runs the command.

*Measured:* Claude Code's `!` prompt prefix provides **no interactive TTY**, so `llens approve` refuses there ("llens approve needs an interactive terminal"). The gate behaved correctly; the earlier `! llens approve` procedure was wrong. The TTY requirement stays.

**Known limits (stated, not hidden).**
- A process that can write the data dir could forge a ledger line, and a pseudo-terminal wrapper (`script`, `expect`, `unbuffer`) could fake a TTY.
- The repo hook therefore BLOCKS any agent `llens approve`, wrappers included (tested). The TTY rule covers agents in every other repo. The ledger forgery gap is closed by ADR-0008 (Ed25519-signed approvals).
- `llens geocode` (Google, not the target site) still uses `--approve-network` only.

## ADR-0007 — Live per-run log (operator decision, 2026-11-07)

- **What.** `runlog.attached` puts a stdlib `logging.FileHandler` with a JSON-lines formatter on the **root** logger for each run (`crawl.run`, cooldown and egress refusals included), and removes it at the end. The file is `$LLENS_DATA/runs/<run_id>.log.jsonl`, referenced from `run.v1.log_ref`.
  - crawlee's module loggers propagate to root, so its own lines are captured (tested).
  - crawlee installs its console handler only on a handler-less root, so `llens` installs crawlee's own console setup first, and the console output is not lost.
  - `StreamHandler.emit` flushes every record, so `tail -f runs/<run_id>.log.jsonl | jq -c .` follows a run live (tested mid-run).
- **Lines.**
  - `start`;
  - `preflight` (egress, or the refusal);
  - one `request` line per GET attempt: `kind` list|ad|robots|other, URL **path only**, `page` for list pages, `status`, `ms`, running `counts`;
  - `challenge` (reason and the allowlisted evidence);
  - `stop` (exit code and final counts);
  - every crawlee line.
- **Never logged.** Cookies, any header beyond the evidence allowlist, query strings. Messages pass a redactor: a URL query becomes `?[query]`, cookie/authorization/token/key values become `[redacted]`, and long opaque strings are redacted.
- **Tests.**
  - The request lines per kind equal `run.v1.requests` exactly, and every path is query-free.
  - A preflight refusal logs `preflight egress_refused` and `stop` with code 8, with no request lines.
  - The handler is detached after the run, and redaction works.

**`import/` boundary.** `$LLENS_DATA/import/` holds a non-`llens` scrape. No `llens` command reads it:
- every store path is fixed (`store.FILES`), and none is under `import/`;
- `raw_ref` paths must match `^raw/…` (schema);
- the approvals and plans directories are fixed.
- Test: `reparse`, `derive`, `findings current`, `cooldown status`, `fetch-ads --dry-run` and `qa-page` all succeed with li-looking files planted in `import/`, and leave them byte-identical.
- The data-dir guards cover it like the rest of the data dir: `llens` refuses a data dir inside a git tree, and the write hook blocks agent Write/Edit anywhere under `$LLENS_DATA`.

## ADR-0008 — Signed approval ledger: Ed25519, key in the Keychain (operator decision, 2026-11-07)

**Problem.** Any process that could write `approvals/ledger.jsonl` could forge an `approved` line (ADR-0006 limit).

**Decision: an asymmetric signature.**
- `llens approve` signs each `approved` record with **Ed25519** over its canonical JSON (everything except `sig`: plan_hash, approval_id, at, expires_at, key_id, …).
- The private key (a random 32-byte seed) lives in the macOS login Keychain, item `$LLENS_APPROVAL_KEY_ITEM` (default `llens:approval:ed25519`).
  - It is created on first use and read **only inside `llens approve`**, through `keyring`'s in-process macOS backend (verified: `keyring.backends.macOS`). So it never appears on a command line, in a log or on stdout.
- The public key is published to `approvals/approve.pub`. Networked commands **verify with the public key alone**: no runner holds a secret.
- An `approved` line counts only if its `sig` verifies and its `key_id` (sha256(pub)[:16]) matches. Unsigned, mis-signed, retargeted and foreign-key lines are refused, exit 10, 0 requests.
- A `used` line is bound to the approved line's `approval_id` and `key_id`. It needs no signature: forging one can only burn an approval, never grant one.

**Beat HMAC-SHA256** (the first proposal): with a symmetric key, every runner that verifies must hold the same key that signs. Any runner could then mint approvals. The asymmetric scheme splits signing (Keychain, `llens approve` only) from verifying (public file).

**Migration.** Lines written before this have no `sig` and are kept in the ledger, but never honoured. None were pending.

**Tests** (`test_approvals.py`; a temporary in-memory key behind the `KEY_PROVIDER` seam, no Keychain in the sandbox):
- a forged unsigned line → 10 with 0 requests and no run record;
- a line signed by another key → 10;
- a validly signed line retargeted to another plan_hash → 10;
- a valid signature → runs once, a second use → 10, and the `used` line is bound to (approval_id, key_id);
- the private key (raw hex and base64) appears in no output, log, ledger, plan or public-key file (every file under the data dir scanned, the run log included).

**Remaining limits (named).**
- **Anyone who can read the operator's login Keychain can approve.** The Keychain is the trust root.
- `approve.pub` is in the data dir: a process that can write it AND sign with its own key could substitute both. Pinning the public key outside the data dir (e.g. in the Keychain as a separate item, compared at verify time) would close that. Not built.
- Above all: a process running as the operator can bypass `llens` entirely and send HTTP itself. The gate binds well-behaved callers of `llens`; it is not a sandbox.

## ADR-0009 — Two roots: DATA (Google Drive, synced) and STATE (local) (operator decision, 2026-11-07)

**Decision.** Following the XDG data-vs-state split:

| Root | Env (default) | Holds | Synced? |
|---|---|---|---|
| DATA | `LLENS_DATA` (required) | `observations.jsonl`, `listings.jsonl`, `raw/`, `findings.jsonl`, `events.jsonl`, `geocode-cache.jsonl`, `runs/` (+ live logs), `derived/`, `import/` | yes: the operator's Google Drive |
| STATE | `LLENS_STATE` (`$XDG_STATE_HOME/listing-insights`, i.e. `~/.local/state/listing-insights`) | `browser-profile/`, `crawlee/` queues, `plans/`, `approvals/` (ledger + `approve.pub`) | **never** |

- **Guards** (`store.check_state_root`):
  - `llens` refuses a STATE root inside a cloud-sync folder: `~/Library/CloudStorage/**`, or any path part containing "Google Drive", "GoogleDrive", "Dropbox", "OneDrive", "iCloud" or "Mobile Documents". iCloud was added beyond the brief, as the same failure. So the Chromium profile and the approval ledger can never sync.
  - It refuses STATE == DATA and STATE inside DATA, and STATE inside a git tree.
  - DATA inside Drive is fine (tested).
  - The write hook blocks agent Write/Edit under the STATE root, the default XDG one included, as it already did under DATA.
- **Backward compatibility.** With `LLENS_STATE` unset and an old single root that still holds `browser-profile/`, `crawlee/`, `plans/` or `approvals/`, `llens` prints a one-line `llens migrate` hint. It moves nothing.
- **`llens migrate --to-data D --to-state S [--from OLD] [--dry-run]`** COPIES, never moves, never deletes, never overwrites.
  - It copies every regular file (mode and mtime kept: the ledger stays 0600) into the right root, verifies its sha256 against the source, and prints a JSON-lines manifest.
  - An identical destination file is skipped. A different one is a CONFLICT that copies nothing and exits 2.
  - A running Chromium's `Singleton*` locks and non-regular files are skipped (`"action": "skip", "reason": "not_regular"`). macOS Finder litter (`.DS_Store`, `Icon\r`, `._*`) is skipped as `"reason": "os_junk"` and never copied: the first real dry-run had planned 8 `.DS_Store` copies into Drive.
  - stdout is pure JSONL: the manifest lines, then ONE `"summary": true` line, on the same stream. The summary used to go to stderr, where `2>&1` into a pipe could splice it into the middle of a block-buffered manifest line (tested through a real pipe with more than a buffer of output).
  - The operator deletes the old copy himself after checking.
- **Tests** (`test_roots.py`):
  - the split puts queues in STATE and data plus run logs in DATA;
  - five cloud-folder shapes are refused, and DATA in Drive is accepted;
  - same/inside is refused, and the XDG default is used;
  - `--dry-run` leaves the source byte-identical and creates nothing;
  - a copy keeps the source byte-identical, matches every hash and keeps modes, and a re-run copies nothing;
  - a cloud `--to-state` is refused; a conflict copies nothing and overwrites nothing;
  - the old-root hint moves nothing.
  - Every other suite runs against split roots (the test fixture always sets both).

## ADR-0010 — Depth-stratified ad sampling for H4 (2026-11-07)

**Why.** H4 asks whether pin reliability varies with ad age / list depth. `fetch-ads --max-fetches N` takes the first N candidates in refetch-rule order, which favours the freshest bumps, so it would confound depth with recency.

**Decision.** `fetch-ads --sample N --stratify depth --seed S [--subcategories a,b] [--allocation equal|proportional]` (`plan.stratified_sample`, pure):
- **Candidates:** exactly the refetch rule's (new / card_changed / stale_7d), unchanged.
- **Strata:** (subcategory, depth decile). Decile = ⌊10·(page−1)/page_count⌋ of the latest sighting; `null` when the page count is unknown.
- **Across subcategories:** `equal` by default, the comparison design (Cochran 1977, ch. 5). A subcategory short of its share gives the rest back, so the sample still reaches N when the pool allows. `proportional` is the alternative.
- **Within a subcategory:** proportional across deciles by the largest-remainder method.
- **Inside a stratum:** a simple random sample from `random.Random(seed)`. Candidates are sorted first, so input order never changes the draw. Same seed + same candidates → same ads (tested).
- **Weights:** each pick carries the design weight N_h / n_h (candidates in its stratum / sampled from it), inside the candidate population of that run.
- **Recorded:**
  - `run.v1.sample` = {n, seed, stratify, allocation, subcategories, strata[{subcategory, decile, population, sampled, weight}], manifest};
  - one `sample.v1` line per ad in `DATA/derived/samples/<run_id>.jsonl` (ad, URL, reason, stratum, weight, seed).
- **Approval:** the plan, and so the `plan_hash`, carries the sample design AND the exact ad URL list. A different seed is a different approval (tested). `--sample` requires `--seed`, so the approved sample is the one fetched.
- `--dry-run` prints the picks with stratum and weight plus a summary line. It sends nothing and creates no run (tested).

**Tests** (`test_sampling.py`):
- decile edges;
- determinism (input order irrelevant, another seed differs);
- equal allocation (20/20 of 40) with every decile of the first list covered;
- the shortfall is given back (35/5);
- proportional allocation (30/4 of 300:40), and the subcategory filter;
- dry-run sends nothing;
- the plan's URLs equal the dry-run's;
- a seed is required;
- a fixture run records the design, the manifest and 2 ad pages.

**Beat:** simple random sampling over all candidates (the fresh pages dominate the pool, so deep deciles would be thin); hand-picking pages (unreproducible).

## ADR-0011 — The run card (operator request, 2026-11-07)

- **What.** Every run prints a card to **stderr**, so stdout stays clean JSON (tested): crawl, fetch-ads, resume, each A/B variant, the offline stages (reparse, extract, geocode, derive, qa-page), migrate, cooldown, `--plan` and `--dry-run`.
  - **Run:** run_id, command and a one-line purpose.
  - **Stage:** N/M from ONE ordered list, `runcard.STAGES` = crawl-lists → fetch-ads → reparse → extract → geocode → derive → qa-page. `ab`, `migrate`, `cooldown`, `crawl resume`, `approve` and `browser open` are "utility".
  - **Inputs / Outputs:** from ONE table, `runcard.IO`, with paths relative to their root (DATA/STATE/OLD).
  - **Scale:** planned list/ad/robots requests, the rate, and the ETA at that rate.
  - **Triggered by:**
    - user@host and the parent process: "claude" when `CLAUDECODE`/`CLAUDE_CODE_ENTRYPOINT` is set or a parent is named claude, else the nearest parent via psutil;
    - the llens build's git rev (`LLENS_BUILD_REV`, baked into the Nix wrapper from `self.shortRev`/`dirtyShortRev`, or the dev checkout's rev);
    - plan_hash and approval id;
    - variant/backend;
    - the egress, so a crawl's card is written after the preflight.
  - **Live log:** the absolute path and a ready `tail -f '<path>' | jq -c .` line.
- **Rendering** (operator style, 2026-11-07): a `rich` **Table** titled "RUN CARD" on a TTY. It has two columns, **Field** (bold) | **Value**, `box.SQUARE`, `show_lines=True` (a rule between every row), and values folded INSIDE their cell (`overflow="fold"`), so long paths stay in their column. Plain `key: value` lines when stderr is not a TTY or `NO_COLOR` is set.
  - Rows: Run, What, Stage, Roots, Inputs, Outputs, Scale, ETA, Triggered by, Approval, Egress, Live log, Follow it (+ "(copied to clipboard)").
- **No placeholders** (operator rule). Every value is the run's real one:
  - the real run_id and file names (`safe_name(run_id)`, as on disk);
  - the real crawlee queue directory (`STATE:crawlee/request_queues/` plus the run's queue name);
  - the real site, and a count for many-file outputs ("`raw/<site>/ (600 ad snapshots)`");
  - the absolute live-log path, and the full `tail -f <shlex.quote(absolute path)> | jq -c .` line (the exact clipboard string). `shlex.quote` because the DATA root sits under "My Drive/…" (spaces) and a path may hold `'` or `$`; tested by running the copied line through `sh -c` with `tail` swapped for `ls`.
  - DATA and STATE are shown once, as absolute paths in the Roots row; every other path is relative to its root.
  - An output that this invocation does not have (no run at `--plan`, no sample without `--sample`) is left out, never templated.
- **Clipboard.** The tail line goes to `pbcopy` when present, and the card says "(copied to clipboard)". It is skipped silently elsewhere, and opted out with `LLENS_NO_CLIPBOARD=1`. The test suite sets that globally: the darwin build sandbox is off and `/usr/bin/pbcopy` exists, so tests would otherwise overwrite the operator's clipboard.
- **Recorded.** The same card is the FIRST line of the run log (`event: run_card`; it replaces the old `start` line) and `run.v1.card`.
- **Note.** The card holds the OS user name and host. It goes to stderr and to the run record in DATA (the operator's private Drive), never to the repo.
- **Tests** (`test_runcard.py`):
  - the grid rendered at widths 80 and 120 has every line at the same display width, measured with `wcwidth` (independent of rich's own cell math);
  - no angle-bracket placeholder appears in the rendered card (TTY or plain) or its JSON, including `--plan` and `--dry-run` cards;
  - on a real fixture run, every output path the card names exists on disk;
  - stage numbers equal positions in the one list;
  - TTY output is a rich panel; non-TTY and NO_COLOR output is plain;
  - pbcopy is called with exactly the tail line (mocked), and LLENS_NO_CLIPBOARD suppresses it;
  - the parent is "claude" under Claude Code;
  - a run records its card (stage 1/7, log path = log_ref), stdout stays JSONL, and the card is on stderr;
  - offline stages print a card (derive 6/7);
  - the run log's first line is the card.

## ADR-0012 — Extractor gta-hints@5 and the source's parser @3 (analyst recall study, 2026-11-07)

**Context.** An offline recall study (150 ads, gold labels read blind; all 44 @4 hits read in context) measured gta-hints@4 at **recall 0.43** (point locations) and **0.95 precision**. The misses had seven causes, listed below by shape, never by value.

**Decision.** Fixed in this order, each pinned by a synthetic test in `tests/test_gta_hints.py` / the site source's tests (now in DATA):

| # | Miss (shape) | Fix |
|---|---|---|
| 1 | The stored description had its line breaks flattened, gluing neighbour words onto a phrase ("&lt;menu word&gt; &lt;Street&gt; and &lt;Street&gt; Text…") | **The source's parser @3** keeps `<br>`/block breaks as `\n` (spaces collapse per line, blank lines dropped). The extractor matches per line: a line break is a phrase boundary. The title joins as its own line. |
| 2 | "&lt;n&gt; &lt;NAME&gt; STAR" read as "… ST"; a phone fragment "view-&lt;4 digits&gt; … st" read as a house number | The suffix must not be followed by a letter (`(?![A-Za-z])`, not `\b`, which would break "St."). A digit run right after `-` is never a house number. |
| 3 | "&lt;Street&gt; e/y &lt;Street&gt;", "&lt;STREET&gt; E &lt;STREET&gt; UNIT &lt;n&gt;", "&lt;Street&gt; street east and &lt;Street&gt; road" | Connectors `e`/`y` (any case) and a direction letter N/S/E/W between two streets; a direction word may end a side; "unit/suite/apt" never extends a street name. |
| 4 | "&lt;Hotel brand&gt; inn &lt;n&gt; &lt;lower&gt; &lt;lower&gt; drive" | A hotel-anchored address accepts lowercase street words, with the same ad-vocabulary filters as a street address. |
| 5 | "in Midmere", "DOWNTOWN &lt;adjective&gt;", "near airport area &lt;road&gt;", "&lt;Street&gt; area" | New last-resort kind **`area`**. Skipped on an outcall-only line or a line naming two or more areas (a coverage list); dropped when it only repeats the city label. A bare "&lt;city&gt; area" is not an area. |

- Precedence is global by kind (postal > address > hotel address > label > intersection > landmark > FSA > area): a weak hit on line 1 never beats a strong one on line 5.
- **An area is never a point.** `derive.enrich` records the geocode of an `area` hint with `precise: false` whatever the geocoder answered (Google types an airport as precise), so it never reaches `confidence: high` or the precise-only summary.
- Version tags: `text_hint.extractor` and `derived.method` say `gta-hints@5`; reparsed listings say `derived.method: <source>-ad@3`.
- **Apply (offline):** `llens reparse --site <site>` (every snapshot whose description had a line break gets a `reparse` version), then `llens extract`, `llens geocode --dry-run`, `llens geocode` (networked; the new phrases only), `llens derive`.

**Beat:** a larger sample through @4 (measures the same bias more tightly); a model-based extractor (no measured need yet, and it breaks P1's determinism).
**Weakest assumption:** the study tuned on the same 150 ads (~14 template clusters). Re-measure on a held-out, advertiser-level gold set before quoting @5 recall.



## ADR-0013 — Extractor gta-hints@6: the held-out misses (2026-11-08)

**Context.** On a held-out, advertiser-level gold set (182 ads, 4 venue leaks dropped), gta-hints@5 measured tier-A recall **17/22** and A+B **19/27**, at precision 19/19. Eight misses remained, listed here by shape, never by value.

**Decision.** One fix per shape, each with negative tests in `tests/test_gta_hints.py`:

| # | Shape | Fix | Kind / precise |
|---|---|---|---|
| 1-2 | a mall or transit-centre abbreviation ("SQ1"-, "VMC"-style) | `ALIASES`: three public abbreviations, each with the full place name. The hint keeps `raw` as written and adds `query`, which `geocode` sends instead (`commands.query_phrase`). Upper-case aliases match case-sensitively. | landmark / the geocoder's answer |
| 3 | "&lt;Street&gt; corner &lt;Street&gt;" | `corner` / `corner of` is a connector | intersection |
| 4 | a one-letter misspelling of a street | **Bounded edit distance**: one edit (OSA) against a known street word, both 5+ letters, a Title-case single word, and ONLY when the other side is a real street | intersection |
| 5 | "&lt;n&gt;A &lt;Street&gt;" | One unit letter A-G after the house number (H is left out: "24H" means hours) | address |
| 6 | decorative Unicode letters | NFKC normalisation before matching (`raw` is the normalised text) | - |
| 7 | "&lt;city&gt; -&lt;hwy&gt;" | `CITY_HWY` (a GTA city list), not followed by "-" or a digit (a phone). `query` = "Highway N, &lt;City&gt;" | **street / false** |
| 8 | one street, no cross street | `SINGLE_STREET`: a street word + a direction ("Dundas e"), or a Title-case name + a street suffix. Noun-like suffixes (way, lane, place, court…) and street words that are ordinary ad words (`GENERIC`: queen, king, park, main…) are excluded | **street / false** |

- `street` sits after FSA and before area in the precedence. derive treats `street` like `area`: its geocode is recorded `precise: false` (`derive.IMPRECISE_KINDS`), because a road is a line, not a point.
- An outcall line keeps its area when the ad offers incall elsewhere and the line does not say "only".
- "Near" never starts a street name (NFKC turns a non-breaking space before a street into a plain one).

**Evidence for the edit-distance choice (data).** Over all 602 distinct ads on disk, the fuzzy rule fired 3 times on 2 words. One word was the real misspelling, seen twice in one advertiser's template. The other was a person's name in "&lt;Name&gt; &amp; &lt;Name&gt;", which the other-side-is-a-street rule rejected. Rejected alternative: skip misspellings, which loses a tier-A point for a rule with zero measured false positives. Rejected alternative: a general fuzzy matcher (rapidfuzz), which adds a dependency and a threshold for one word-level check that 15 lines already cover.

**Measured (offline, analyst corpora):**

| Set | @5 recall A / A+B | @6 recall A / A+B | @6 precision A+B |
|---|---|---|---|
| gold-150 | 14/14 / 19/22 | 14/14 / 19/22 | 19/19 |
| held-out 182 | 17/22 / 19/27 | 22/22 / 27/27 | 27/27 |

- Across the 602 unlabelled ads: 11 new hits and 2 changed phrases, every one a real stated location; no @5 hit lost.
- **Caveat:** @6 was written against the held-out set, so its numbers are now upper bounds. The next honest number needs a third, unseen set.
- Tag `gta-hints@6`. `llens reparse` is not needed (the parser is unchanged); rerun `extract`, `geocode`, `derive`.

## ADR-0014 — `llens export-db`: a derived Postgres copy for a private Grafana dashboard (operator decision, 2026-11-08)

**Context.** The operator wants a private Grafana dashboard over all listing-lens data. Grafana reads SQL, and DATA is JSONL on Drive. Decisions: the operator's existing Grafana stack (a private folder), a new private Neon Postgres project with a neutral name, the built-in PostgreSQL datasource, NO field filtering (titles and descriptions go in; contacts as stored, i.e. masked), and the phone-session import INCLUDED with zero redundancy.

**Decision.**
- `llens export-db` rebuilds schema `li` from DATA. It reads DATA only and never writes it. The DDL is `src/listing_lens/db/schema/*.sql`, applied to a FRESH schema each time: the copy holds no fact of its own, so there are no migrations.
- **Atomic:** load into `li_new`, then ONE transaction renames li → li_old, li_new → li, drops li_old, and re-grants the read-only role (`--grant-role`). A failed load leaves the previous copy untouched (tested).
- **Idempotent:** `meta.digest` = SHA-256 over the loader version, the DDL and every row. The same inputs give the same digest, and an unchanged digest is a no-op unless `--force` (tested: export, no-op, forced rebuild with identical table hashes).
- **Semantics in one place:** `listings_latest` (greatest fetched_at; a later line wins a tie). Every `*_at` is UTC `timestamptz` next to `*_raw`, the exact stored string; a zone-less site wall time is the site's own zone (the source's `tz`). The region box comes from the source's own region config (`region` table), never a second hand-typed box.
- **Google's coordinate rule holds in the copy:** a geocode point is exported only while its cache entry is usable (`geocache.usable`, 30 days). The phone import's Google points are exported only before 2026-12-07 (its README). Site pins are not Google's and are kept.
- **The phone import, merged:** `import_phone_ads` holds the import's own rows (842). `ads` holds ONE row per (site, ad_id) across llens's latest listings and the import. Each field is taken from either side with its provenance (`*_src`: li | phone | both-agree | both-differ):
  - Text (title, description, city): the richer (longer, whitespace-normalised) value.
  - Structured values (pin, bump time, hint, geocode): llens when both differ, because llens's come from a saved snapshot and a newer parser/extractor.
  - `observations` stays its own table.
- Media URLs are stored exactly as the site gave them (the dashboard hotlinks from the operator's browser).
- **Networked, gated:** network class `paid` (our own metered database). `--dry-run` builds every row offline. A real run needs `--approve-network` and the DSN in `LLENS_DB_URL`, set only by `secret exec LLENS_DB_URL=<keychain item> -- llens export-db --approve-network`. The DSN is never printed: a database error is reported by class and SQLSTATE only (tested with a marker password).
- `import/` stays write-protected; export-db is its one reader (CLAUDE.md).

- **The dashboard contract (with grafana-engineer, loader `export-db@2`):** instants are `timestamptz` with no suffix, next to `*_raw`; nested fields are flattened with their record path as prefix (`pin_`, `text_hint_`, `geocode_`, `city_centre_`); lists are `jsonb` (Grafana's Postgres driver returns `text[]` as an unparsed string), plus `n_media`; list positions are `list_page` / `list_page_count` / `list_position` with `depth_decile` (the sampler's own formula). `listings_latest` is a view joining derive's output and `advertiser_id`; advertisers are in LONG form; `run_logs` and `ab_variants` flatten the run logs and A/B reports.

**Measured on DATA (dry run, 2026-11-08):** observations 7,133 · listings 1,143 (602 latest) · derived 602 · geocodes 73 · runs 16 · run_logs 1,486 · ab_variants 10 · events 35 · samples 600 · findings 9 · advertisers 0 (derive-advertisers not yet run on DATA) · import_phone_ads 842 · **ads 1,434** = 592 li-only + 10 opened by both + 832 phone-only (110 of them llens saw only as list cards, 722 disjoint).
- `count(DISTINCT (site, ad_id)) == rows` holds in `ads` (tested).

**Neon Free fit (Neon docs, checked 2026-11-08):** 1 GB/project storage (the copy is tens of MB), 100 CU-hours/project/month with scale-to-zero after 5 min that cannot be disabled. So dashboards must not auto-refresh more often than every 10 min, and no Grafana alert rule may query this datasource. Any-IP access is the default (IP Allow is Scale-only), so use TLS plus a read-only role.

- **Advertiser links (loader `export-db@3`, team-lead GO 2026-11-08):** `derive-advertisers` also writes `derived/advertiser_links.jsonl` (`advertiser_link.v1`). Inside each advertiser there is ONE row per (ad pair, key kind) for every key two ads share DIRECTLY: the union-find's edges, not its closure. `link_kind` ∈ phone4 | handle | name | title | desc | venue. `link_key` is the last 4 digits / handle / normalised name / venue, while title and description become a 12-hex fingerprint, never the text. On DATA: 272 advertisers (88 multi-ad), 3,631 links (name 1,139 · desc 1,009 · title 637 · handle 418 · phone4 298 · venue 130); the densest advertiser has 736.
- **Photo links (recorded, never clustering):** `link_kind = photo` marks two ads carrying the same media URL exactly as the site gave it, one row per (pair, URL), with `used_for_clustering = false` and `advertiser_b`, since these edges may cross advertisers. Clusters are unchanged. On DATA: 790 URLs on 2+ ads, 54 spanning more than one advertiser, at most 19 ads on one URL; 8,187 photo links, 412 crossing advertisers. This is the input to the analyst's photo ablation (agency/stock photos vs. one advertiser).
- **Ablation (loader `export-db@5`, team-lead GO):** `derive-advertisers` also writes `derived/advertisers_ablation.jsonl` (`advertiser_ablation.v1`): the advertiser each ad lands in when the SAME union-find runs with one key kind dropped (-desc, -name, -title, -phone4, -handle, -venue) or photos added (+photo); `baseline` equals the real clusters (tested), which are never changed by it. This makes "families across advertisers" non-circular: desc is itself a clustering key, so under baseline a description family cannot span two advertisers. On DATA (602 ads): baseline 272 advertisers; -name 339 (247 ads move), -desc 285 (47), -venue 282 (46), -handle 281 (13), -title 275 (25), -phone4 275 (14); +photo 262 (66 ads move).
- **search_path:** the grant step sets `ALTER ROLE <ro> IN DATABASE <db> SET search_path = li`, so unqualified panel SQL resolves. Measured: it applies on the DIRECT endpoint only; Neon's PgBouncer (transaction mode) ignores role settings, so Grafana uses the direct host (few connections).
- **Viewpoint (loader `export-db@8`, operator request):** table `viewpoint(label, lat, lon)` is loaded from `DATA/config/viewpoint.json`, which the operator writes himself; the repo and its tests hold no real point. No file = 0 rows; a malformed file stops export-db with exit 2 before anything is sent. PostGIS is NOT used: the boards compute rings (spherical destination formula) and distances (haversine) in plain SQL, so no extension is installed and the read-only role's search_path stays `llens`.
- **PostGIS geography (loader `export-db@9`, operator approval 2026-11-08):** export-db runs `CREATE EXTENSION IF NOT EXISTS postgis SCHEMA public` in its transaction. It must sit in `public`, never in `li`, which every rebuild drops with CASCADE. Every lat/lon pair also gets a `*_geog geography(Point, 4326)` column GENERATED from it: listings.pin_geog (GiST-indexed), derived.geocode_geog / city_centre_geog, import_phone_ads and ads pin_geog / geocode_geog, and viewpoint.geog. The read-only role's search_path is `li, public`, applied on the direct endpoint. The earlier "no PostGIS" (226b32e) had no blocker: the boards had not needed it. Measured on DATA, haversine (R 6371 km) vs `ST_Distance` on the WGS84 spheroid: viewpoint→pin max 0.278 % (263 pairs), pin→geocode max 0.272 % (56); ring counts identical at all 16 selectable radii (1–60 km). The ~0.28 % is the sphere-vs-spheroid gap at this latitude.
- **The phone import's `maps_url`** embeds the Google point, so it is dropped with lat/lon after 2026-12-07.
- **export-db@19 (team lead, 2026-11-13):**
  - `advertiser_v2_links`: WHY v2 merged two ads, for the Node Graph. `derive-advertisers --method v2` also writes `derived/advertiser_v2_links.jsonl` (`advertiser_v2_link.v1`): one row per (accepted v2 edge, key kind), from the SAME pair pass as the clusters (one function, `_v2_edges`). Keys follow advertiser_link.v1; the photo upload id (`image`) is fingerprinted too. Beat: export-db recomputing edges from the current listings, which could disagree with the stored partition. export-db refuses (exit 2) an edge file that is missing, older than `advertisers_v2.jsonl`, or does not explain it (an edge across operators, an ad whose edge kinds ≠ its edge_evidence, components ≠ the multi-ad operators). On DATA that means re-running `llens derive-advertisers --method v2` once before the next export.
  - `ad_observation_span` (survival, GQM G6.Q4): one row per ad llens ever saw. A sighting is a list card or an ad fetch (a reparse copies `fetched_at` and is no visit). `gone` comes from `events.jsonl` kind=gone (the ad id from its url), else from a run log's `gone` line, which has no ad id, attributed to the ad `request` line right before it in the same run; the earliest gone record wins. On DATA (2026-11-13) the two sources agree exactly: 54/54 (run, ad) pairs, 51 ads, 0 unattributable lines. status = gone (a gone record at or after the last live sighting) | still_listed (seen live ≥ `REVISIT_H` = 4 h after first_seen) | unknown (never revisited). The 4 h is FITTED: consecutive sighting gaps fall ≤ 2 h (a card, then its own ad fetch) or ≥ 8 h (a later re-fetch), none in between. On DATA: 51 gone, 1,786 still_listed, 5,296 unknown, 0 seen live after a gone.
  - **export-db@23 (ADR-0017 slice 1, 2026-11-19):** the span is read from the harness's liveness ledger (`scrape_harness.liveness.build(...).spans()`, the default `SyncPolicy`'s rules), so the planner's gate and the boards share ONE rule for sighting, gone and state. Changes: a sighting counts only from a GOOD crawl (completed with `list.status` ok, or a legacy crawl that cannot be judged); a gone joins its ad by the ledger's `adkey` rule (`(site, ad_id)`, else the exact url); an OK fetch after a gone clears it (`gone_at` NULL); an ad with no live sighting has no row. New columns `liveness` (alive | tombstoned | quarantined | held), `gone_run`, `readmitted`, `held_since`; `gone_source` and the run-log gone attribution are dropped. `status` stays the survival label: gone = tombstoned, else the `REVISIT_H` split.
  - `hint_gold_v3` (boolean, never NULL) on `derived` and `listings_latest`, beside `place_gold_v2`: true for every ad of the address-gold-v3 frames. They are read ONLY through `eval/address-gold-v3/MANIFEST.json`: each `sample.tranche<N>.json` key names a tranche file beside it (value = its sha256), and `tranche2_frame` names the tranche-2 frame with its sha256. A missing manifest or file, or a sha that moved, stops export-db with exit 2: an empty set would silently unmask gold ads. On DATA: 93 tranche-1 ads (all in derived) + a 1,650-ad frame (none fetched yet). `gold_mask.V3` now reads the flag.
  - **export-db@20 (analyst, the gold owner, 2026-11-13):** `hint_gold_v3` is the MANIFEST's `d6_mask`, not the whole frame: true iff the ad is LABELLED (in the union of every `sample.tranche<N>.json` the manifest lists) OR its current advertisers@2 operator holds a labelled ad (operator-mates share templates). A frame ad never drawn stays unmasked. Still fail-closed (exit 2): a moved sha, a labelled set that disagrees with `labelled_ids` (n, sha256 of the sorted ids), or labelled ads with no advertisers_v2.jsonl. On DATA: 528 labelled → 692 masked (was 1,669).
  - **export-db@22 (operator decision, 2026-11-13):** the BOARD flag narrows to the labelled ads only (528 at writing): operator-mates are no longer masked, so their llens answers and pins return. The wider @20 set (labelled + mates, 692) survives only as gta-hints@8's pinned development deny-list (STATE). Fail-closed checks unchanged; advertisers_v2.jsonl is no longer needed for the mask.
- **Read grants (loader `export-db@21`, grafaner's Assistant plan, team-lead GO 2026-11-13):** the read-only role no longer gets `SELECT ON ALL TABLES`. `export_db.READABLE` is an explicit allowlist; `WITHHELD` = `derived`, `listings_latest`, `ads`, `geocodes` (gold-bearing location answers, or the geocode queries and points). `grant()` revokes everything first, so re-running on an existing database narrows it. `listings_masked` stays readable (a view runs with its owner's rights), and the new view `geocode_cache_summary` is ONE row (`queries`, `places`, `precise_share`, `min_expires_at`; names and types pinned by a test for grafaner's panel), with no query text and no points. A test fails on any relation in neither set, so a new table is denied until it is classified.
  - View `listings_masked`: `listings_latest` with the gold answer columns NULL on every `place_gold_v2` OR `hint_gold_v3` ad, plus `gold_masked` and `mask_reason`. export-db GENERATES it from `gold_mask.view_select()` (the one column list, which the boards' interim CTE uses too) and appends it to the DDL, so a mask change moves the digest. The read-only role's blanket `GRANT SELECT ON ALL TABLES` covers it; raw-table grants are unchanged. A test fails if `listings_latest` gains a place_/geocode_/text_hint_ (or listed derived-location) column the mask does not cover.

**Test:** `tests/test_export_db.py` starts a throwaway Postgres (`initdb` + `pg_ctl` on a unix socket, no TCP) inside the build. `LLENS_REQUIRE_PG=1` in the nix build makes a missing server FAIL there, never skip.

**Beat:** Grafana's JSON/Infinity datasource straight on Drive files (no joins, no SQL, Drive auth inside Grafana). Google Sheets as the store: the operator's rule is that Sheets is never the visualiser's database, and the row counts exceed comfortable Sheets use. An in-place migrating schema: there's nothing to migrate in a derived copy.

## ADR-0015 — advertisers@2: pairwise edges, a NEW method next to v1 (analyst spec, 2026-11-08)

**Context.** v1 (advertisers@1) unions every ad that shares ANY single key, so one common first name chains unrelated personas. The ablation (ADR-0014) measured it: dropping `name` moves 247 of 602 ads. The analyst's spec (scratchpad h7/advertiser-v2-spec.md) keeps v1's keys and changes only the edge rule.

**Decision.**
- `llens derive-advertisers --method v2` writes `derived/advertisers_v2.jsonl` (`advertiser.v2`, ONE record per ad), and `export-db@6` loads it as `advertisers_v2`. v1 is the default, untouched, and its files are never written by v2 (tested).
- **One key function:** v1's `keys()` unchanged, plus `img:<upload id>` (the first path segment of the site's image-host URL, the source's `upload_re`, shared by every size of one upload).
- **Pair rule, then connected components:** strong kinds are d (desc200), h (handle) and img; weak kinds are n, t, ph4 and v.
  - Same subcategory: an edge if the pair shares ≥1 strong kind or ≥2 distinct kinds.
  - Across FE/TS: an edge needs ≥2 distinct kinds.
  - Hub guard: a key on >60 ads is ignored, for every kind.
- **Level:** `operator` iff size ≥5 AND names ≥3 AND (one d:/img: key on ≥50% of the ads OR names ≥5); otherwise `persona`. This is a judgement threshold (analyst, measured on the 602), so each record also carries `n_names` and `top_shared_key_share` for a panel to re-cut.
- **Per ad:** `edge_evidence` = the kinds of the v2 edges incident to it ([] for a singleton). `advertiser_id` = "v2:" + sha256(sorted ad ids)[:12]; it is NOT stable across crawls, so join on (site, ad_id).

**Parity (offline, the 602 old ads):** identical partition to the analyst's map (h7/v2_on_li_keys.json): 332 advertisers, max 27, 9 clusters of 10+ (137 ads), 8 mixed FE/TS, 6 operators.

- **Email-domain handles (analyst blind read, v2 only):** a handle captured through a bare "@" that is domain-shaped (`^[a-z0-9-]+(\.[a-z0-9-]+)*\.(com|ca|net|org|me|co|io)$`) is dropped, e.g. "…view@gmail.com". It caused all 7 wrong strong-only links in the read. Prefix captures (onlyfans.com/, linktr.ee/, instagram) are untouched, and there is NO lookbehind, which would also kill real "telegram...@Name" handles. On the 602: distinct handles 121 → 118.
- **Two ids per ad:** `operator_id` ("v2:") = the v2 component above. `persona_id` ("p2:") = components over PERSONA edges only: a v2 edge whose pair shares a non-email handle, or a name plus at least one other kind. Requiring the v2 edge too means a handle alone across FE/TS links nothing, so a persona can never span two operators (tested; 0 on DATA).
- **On the 602 after the fix:** 340 operators (max 27), 5 operator-level; 378 personas (78 multi-ad, max 19). The pre-fix 332-group partition matched the analyst's map exactly; the 12 groups that differ now are the email-domain splits.
- **advertisers@1 known defect (recorded, NOT changed):** v1 uses the same HANDLE regex, so the email domain "gmail.com" becomes a handle and merges 12 unrelated ads into one v1 advertiser (tested as a known defect). Whether to cut a v1.1 is the operator's decision.

- **advertisers@1.1, the DEFAULT (operator decision, 2026-11-08):** v1 EXCEPT email domains are no longer handles (the same `handles_v2` fix, one function). `derive-advertisers` (no flag, or `--method v1.1`) writes the canonical `advertisers.jsonl` with `v1.1:` ids, plus links and ablation on v1.1's keys. `--method v1` writes the frozen advertisers@1 record to `advertisers_v1.jsonl` (`adv-` ids), reproducible as the historical record and exported as `advertisers_v1` (`export-db@7`). advertiser.v1 and panel.v1 accept both id prefixes. On the 602: v1 272 advertisers (88 multi-ad), v1.1 281 (89); v1's email cluster had 13 ads. Tested: two ads sharing only "x@gmail.com"-style emails link under v1, not under v1.1 or v2.
- `panel build` reads `advertisers.jsonl`, so after the next reprocess a new panel is built on v1.1 clusters.

**Not decided here:** switching the default. That needs the analyst's blind reads (strong-only edges precision ≥0.9, 2-weak ≥0.8, v1-only merges ≥70% truly different advertisers) and the operator.

**Beat:** editing v1 in place (it would silently change every published v1 number); a weighted similarity score (it needs a threshold with no ground truth yet, whereas the pair rule is auditable edge by edge).

## Deliberately NOT built (and why)

| Not built | Why |
|---|---|
| A database (the template's Postgres) | Append-only JSONL on Drive is the agreed store; a DB would be a second copy. |
| A project-local skill | Nothing here needs prose know-how that a CLI subcommand cannot encode; the installed skills cover browser driving and Nix. |
| Phone/contact parsing, `service_mode` | No measured selector yet; fields stay null rather than guessed. |
| Scheduling (cron, routines) | Every networked run needs a human's approval by design. |
| A request-queue / retry / rate-limit framework of our own | crawlee owns them (ADR-0003). |
| Ports of `analyze*.py` beyond the H1 numbers | Only the statistics a current finding uses are in `derive.summarise`; more when a finding needs them. |
| The operator's reference point in the QA page | It is a run argument (`llens qa-page --target lat,lon`), never a constant in the repo. |
| Offline-proof on darwin by default | darwin's sandbox is off by default. Data: `checks.aarch64-linux.default` built in the Linux sandbox (no network) on 2026-11-07 — 56 tests passed. A CI that uses the nixos/nix image would repeat the build but NOT the proof: that image sets `sandbox = false`. |
