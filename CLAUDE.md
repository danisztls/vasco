# CLAUDE.md

Vasco — CLI for AI web research. Python 3.12+, managed with `uv`.

## Run

| Task | Command |
|---|---|
| Install | `uv sync` (`--group dev` for tests); add `--extra browser` on the host that runs `vasco browser-server` (camoufox lives in the `[browser]` extra, server-only) |
| CLI | `uv run vasco <subcommand>` |
| Daemon | `uv run vasco serve` (resident `vascod`; systemd units in `contrib/systemd/`) |
| Tests | `uv run --group dev pytest -q` |
| Lint / format | `uv run ruff check .` / `uv run ruff format .` |

## Module map

| File | Role |
|---|---|
| `vasco/interface/cli.py` | Typer app, TTY-aware output, parses `--deadline`/`--older-than`; `search`/`fetch`/`extract`/`answer`/`map`/`normalize` commands + `cache` and `logs` sub-apps + `serve` (run vascod) and `browser-server`. CLI commands run **in-process** (not routed through vascod) so the CLI stays the daemon-free ground-truth/debug path |
| `vasco/interface/mcp.py` | MCP server (stdio); tools `search`/`fetch`/`fetch_many`/`extract`/`answer`/`map`; opt-in browser prewarm; `fetch_many` defaults to `metadata_only=true`; llms.txt taint warning on `map` results. Every tool routes through `service.client.request_or` (vascod when reachable, else in-process fallback); telemetry stays at the tool layer |
| `vasco/service/protocol.py` | Wire protocol for vascod — single home for the socket contract: `PROTOCOL_VERSION`, length-prefixed JSON framing (`read_msg`/`write_msg`), `socket_path()` (`$XDG_RUNTIME_DIR/vasco/vascod.sock`, `VASCO_SERVICE_SOCKET` override), op constants. The *payload* model is the envelope itself (already JSON) |
| `vasco/service/daemon.py` | `run_daemon(cfg)` = vascod: loads one `Config`+`Cache`, serves the full API over a UNIX socket (chmod 0600, 10 MiB frame cap). `Dispatcher` routes ops to existing entry points; `fetch`/`fetch_many` go through the coordinator (fetch_many = coordinated gather over fetch). A fetch failure crosses the wire as `ok=true` + failure envelope; `ok=false` is reserved for transport/unexpected errors |
| `vasco/service/coordinator.py` | Cross-consumer coordination for the fetch path: single-flight dedup (task-keyed by `normalize_url`+mode/raw/refresh/use_cache) + per-domain min-interval rate limit (`registered_domain`, skipped on cache hit). Config via `ServiceCfg` (`single_flight` default on, `rate_limit_rps` default 0=off) |
| `vasco/service/client.py` | `DaemonClient` (one-shot request; fast-fail on no daemon, one reconnect on mid-request drop, bounded read timeout) + `request_or(op, params, local=)` — daemon when reachable else in-process. `DaemonError` (ok=false) propagates; only `DaemonUnavailable` falls back |
| `vasco/fetch/__init__.py` | `fetch_one` / `fetch_many`, auto chain `http → browser → browser+mobile → wayback`, phase timing, envelope assembly |
| `vasco/fetch/browser.py` | **Browser-tier client (no camoufox dep).** `get_browser(cfg)` → `BrowserPool`, a thin proxy that connects to the `browser_server` UNIX socket and forwards `fetch(url, mobile=)` requests. No in-process browser fallback: if the server isn't reachable it raises `BrowserServerUnavailable` (→ `BROWSER_UNAVAILABLE` in the chain). `cfg` is ignored on the client — the server owns all browser config |
| `vasco/fetch/browser_server.py` | **Browser-tier server (`vasco browser-server`).** Owns the Camoufox process (imported lazily; needs the `[browser]` extra), serves fetch requests over the UNIX socket with a wedge/disconnect watchdog (`_BrowserSupervisor`). Holds the page-driving code (`fetch_page` + `_install_netblock_route`/`_extract_headers`, mobile UA/viewport) and `_build_launch_kwargs` (locale/persistent-context). Applies its own config's tracker blocking |
| `vasco/fetch/netblock.py` | Network request blocklist for the browser tier: `should_block(request_url, page_domain, blocklist)` (pure — first-party guard via `cache.registered_domain` + `quality.blocklist.is_blocked` membership) + `get_netblock(cfg)`/`load_netblock` lazy singleton. On by default; bundled conservative list `data/netblock_default.txt` (Peter Lowe), overridable via `browser.network_blocklist_paths` through the shared blocklist loader (separate `netblock.txt` consolidation file) |
| `vasco/fetch/bot_detect.py` | `classify(status, html, headers) -> FailureReason` (pure) |
| `vasco/telemetry/__init__.py` | JSONL event log; `record_success` / `record_failure` / `record_exception` with `outcome` discriminator |
| `vasco/telemetry/logstats.py` | `summarize(cfg, days=)` — per-tool counts, cache-hit ratio, mode mix, failure histogram, duration + phase percentiles, escalation rate |
| `vasco/search.py` | `Searcher` protocol, `SearchResult`, `get_searcher()` factory, `--site` operator |
| `vasco/adapters/ddgs.py` | DuckDuckGo search backend (`DdgsBackend`) |
| `vasco/adapters/tavily.py` | Tavily search backend (`TavilyBackend`) |
| `vasco/adapters/wayback.py` | Wayback Availability API + `if_` modifier; trailing-slash retry |
| `vasco/adapters/youtube.py` | Transcript fetch — own envelope shape (`mode_used="youtube"`, `content_type="text/youtube"`) |
| `vasco/adapters/wikimedia.py` | Wikimedia article fetch via Enterprise On-demand API — Structured Contents (Wikipedia, 9 beta langs) + standard articles (all projects/langs); own envelope shape (`mode_used="wikimedia"`, `content_type="text/wikimedia"`) |
| `vasco/adapters/realestate.py` | Brazilian real-estate portals (vivareal) — HTML fetched via the shared escalation chain (injected `fetch_html`) then parsed per-provider into normalized listings (`url`, `title`, `type`, `price`, `condo_fee`, `area`, `bedrooms`, `bathrooms`, `parking`, `neighborhood`, `city`, `street`, `description`, `amenities`, `image`, `images`; `title`/`description` carry free-text when a provider lacks clean structured fields) in `quality.listings`; `list` pages (many, thumbnail) vs `detail` pages (one, full gallery); own envelope shape (`mode_used="realestate"`, `content_type="application/x-realestate"`) |
| `vasco/adapters/google_shopping.py` | Google Shopping BR results (search + homepage) — HTML fetched via the shared escalation chain (injected `fetch_html`; routes seeded to the browser tier in `vasco/strategy.py` since the http tier only serves an empty JS shell) then parsed via `<product-viewer-entrypoint>` aria-labels into structured products (title, price, store, rating, discount, badges) in `quality.products`; filters used/refurb + international sellers + IQR outliers; own envelope shape (`mode_used="google_shopping"`, `content_type="application/x-google-shopping"`) |
| `vasco/adapters/olx.py` | OLX.com.br classifieds — two verticals only: real estate (`/imoveis/`) + vehicles (`/autos-e-pecas/`); other categories aren't matched by `is_olx_url` and fall through to normal fetch. HTML via the shared escalation chain (injected `fetch_html`); Cloudflare-protected so `olx.com.br` is seeded to the browser tier in `vasco/strategy.py`. Embedded JSON, not HTML scraping: **list** pages parse `<script id="__NEXT_DATA__">` → `props.pageProps.ads[]`; **detail** pages parse `<script id="initial-data" data-json>` → `.ad` (schema.org JSON-LD `Offer`/`Car` as fallback). Each ad's category-agnostic `properties[]` (name/value) is lifted into a per-vertical typed `attributes` bag (RE: type/area/bedrooms/bathrooms/parking/condo_fee/iptu/amenities; vehicles: brand/model/year/mileage/fuel/gearbox/cartype/color/doors/motorpower/features) on a common listing (`url`, `title`, `price`, `old_price`, `category`, `vertical`, `neighborhood`, `municipality`, `uf`, `image`, `images`, `description`, `date`) in `quality.listings`; own envelope shape (`mode_used="olx"`, `content_type="application/x-olx"`) |
| `vasco/adapters/mercadolivre.py` | MercadoLivre BR marketplace (`mercadolivre.com.br`; Spanish-country MercadoLibre out of scope, falls through to normal fetch). HTML via the shared escalation chain (injected `fetch_html`); bot-challenged on the http tier so `mercadolivre.com.br` is seeded to the browser tier in `vasco/strategy.py` (bare-domain key covers www./lista./produto.). **schema.org JSON-LD is the robust spine** (survives ML's CSS class rotation): **search** pages parse the `@graph` of `Product` objects → many products (title, price, currency, url, brand, aggregateRating, image); **product** pages parse the single rich `Product` (offers.shippingDetails→`free_shipping`, itemCondition→`condition`, aggregateRating, brand, sku, color, description) + best-effort `ui-pdp-*`/`andes-*` HTML extras (`seller`, `sold_quantity`, `installments`, struck `original_price`, spec-table `attributes`) that never fail the parse. Products in `quality.products` (`page_type` search/product, `currency`, `result_count`); own envelope shape (`mode_used="mercadolivre"`, `content_type="application/x-mercadolivre"`) |
| `vasco/quality/__init__.py` | `score(markdown, url, cfg)` → composite quality dict merged into envelope; orchestrates both layers |
| `vasco/quality/heuristics.py` | Text-level slop detection: vocab ratio, phrase count, sentence CV, em-dash density, transition starts, TTR |
| `vasco/quality/blocklist.py` | Domain blocklist loader (plain + uBlacklist + `0.0.0.0`/`127.0.0.1` hosts formats); lazy singleton; `is_blocked(url)`; `load_blocklist(..., consolidated_name=)` so independent lists (quality vs. netblock) don't share a cache file |
| `vasco/quality/wordlists.py` | Slop vocabulary data (tier-1 words, phrases, transition starters) |
| `vasco/extract.py` | Passage segmentation + BM25 / semantic ranking |
| `vasco/semantic.py` | Lazy sentence-transformers ranker; raises `SemanticRankerUnavailable` if extra is missing |
| `vasco/summarize.py` | Powers the `answer` command: `answer(url, question=)` fetches then asks an LLM (DeepSeek default); `summarize()` does the LLM call. Returns `None`/`error` on failure, never raises. Page is cached via `fetch_one`; the answer is not cached |
| `vasco/adapters/deepseek.py` | Async OpenAI-compatible chat client (`DeepSeekClient`) for the `answer` command; raises on HTTP error |
| `vasco/map.py` | sitemap / feeds / spider via trafilatura, `--exclude` filters; llms.txt discovery (disk-cached 24h) |
| `vasco/cache.py` | SQLite cache + URL normalization (incl. AMP folding, YouTube collapse, Wikimedia mobile→desktop) + `route_key(url)` (registered_domain + first path segment) + learned per-route strategy (`fetch_strategy` table; starting tier only) |
| `vasco/strategy.py` | Declarative `SEED_STRATEGIES` (route_key → starting tier) — the single config-like home for known per-route tier knowledge (e.g. Google Shopping + vivareal detail + OLX → browser). Bare-domain keys (e.g. `olx.com.br`) prefix-match every route under a Cloudflare-protected site. Seeds only; learning overrides once a row exists |
| `vasco/converters/convert.py` | `html_to_markdown` (trafilatura wrapper + link extraction) |
| `vasco/converters/pdf.py` | `pdftotext` / `pdfinfo` shell adapter |
| `vasco/converters/pandoc.py` | Pandoc shell adapter (DOCX, EPUB, ODT, RTF → Markdown) |
| `vasco/errors.py` | `FailureReason` enum |
| `vasco/envelope.py` | Single source of truth for the fetch envelope: `FetchEnvelope` TypedDict + `base_envelope`/`success_envelope`/`failure_envelope`/`now_epoch`. Core fetch **and** every adapter build through these, so the shape lives in one place |
| `vasco/config.py` | `load_config()` → YAML + `VASCO_*` env vars |
| `vasco/io.py` | TTY detection, NDJSON/JSON/markdown writers, token estimator |

## Invariants

- **Fetch envelope is the contract.** Same shape across `fetch_one`, `extract`, `cache.get`. The shape is built in exactly one place — `vasco/envelope.py` (`base_envelope`/`success_envelope`/`failure_envelope`); core fetch and all four adapters call these (adapters keep only thin delegators passing their own `mode_used`/`content_type`). Adding/renaming a field means editing `vasco/envelope.py` **and** the `cache.py` columns (incl. `_FETCH_CACHE_ADDED_COLUMNS` for the ALTER back-fill) + `_hydrate_cache_hit`; `tests/test_cache_roundtrip.py` fails CI if a builder field has no cache column (this caught `image`/`modified` being silently dropped).
- **vascod is the shared coordinator; the envelope is also a wire contract.** `vasco serve` runs a resident daemon (`vasco/service/`) that owns one `Config`+`Cache` and serves the full API (`search`/`fetch`/`fetch_many`/`extract`/`answer`/`map`) over a UNIX socket, adding cross-consumer **single-flight dedup + per-domain rate-limiting** (only meaningful because every consumer funnels through one process). It sits **in front of** `browser_server` as a client (two independent systemd peers — neither owns the other; preserves browser crash isolation). MCP and claudinho are clients; the **CLI stays in-process** (debuggable ground-truth path, and the cache is already shared via the SQLite *file*). The envelope crosses the socket as JSON unchanged — `protocol.PROTOCOL_VERSION` versions the *transport*, the envelope stays the single source of truth. A *fetch failure* is still a `failure` envelope over the wire (`ok=true`); `ok=false` means a transport/daemon error. The wire shape lives in exactly one place: `vasco/service/protocol.py` (claudinho vendors a copy of `PROTOCOL_VERSION` + framing and guards against drift at runtime). vascod is **stateless between deadline-bounded requests** — it survives host suspend/resume with no special handling; a wedged browser is healed by `browser_server`'s existing watchdog. **Key-dependent ops resolve keys from config when routed.** All six tools route through vascod, so the LLM/search-API calls in `answer` (DeepSeek) and `search` (Tavily) execute in the *daemon's* process, which does **not** inherit the MCP/shell env — set `answer.api_key` / `tavily.api_key` in `~/.config/vasco/config.yaml` (env vars like `DEEPSEEK_API_KEY` only reach a tool when it runs in-process, e.g. the CLI). Restart `vascod.service` after editing config.
- **`fetch_one` never raises.** Failures are first-class output via a `failure` object whose `reason` is a `FailureReason` enum value. New failure modes go in `errors.py` first, then `fetch.bot_detect.classify` learns to produce them. The exception is `PARSE_FAILED` — it is **adapter-produced** (a 200 OK whose source-specific parser lost its structural anchor), so `classify` never emits it; the content adapters raise `errors.AdapterParseError` at the anchor and turn it into a `PARSE_FAILED` failure envelope.
- **Content adapters distinguish scraper-rot from empty results.** Each adapter (olx, mercadolivre, realestate, google_shopping) parses an embedded structural anchor (OLX `__NEXT_DATA__`, ML/vivareal schema.org JSON-LD, Google Shopping `<product-viewer-entrypoint>`). **Anchor absent/unparseable → raise `AdapterParseError` → `PARSE_FAILED` failure** with a short, agent-readable message (never raw HTML). **Anchor present but zero items → `success` + `"no_results"` in `warnings`** (a genuinely empty search). A *detail/product* page parsing to zero items is always rot (it should yield exactly one). This is what stops a site markup change from silently caching a `result_count: 0` success forever.
- **URL normalization is the cache key.** `cache.normalize_url` is load-bearing — changing it invalidates every cached entry. Besides lowering, sorting params, and stripping tracking params, it folds AMP variants (`?amp=1`, `?output=amp`, `/amp/` path segments) and collapses all YouTube URL forms (`youtu.be`, `m.youtube.com`, local TLDs, `/embed/`, `/shorts/`) to bare `youtube.com/watch?v=<id>`. Tests in `tests/test_normalize.py` are table-driven.
- **Auto-mode escalation lives in `fetch._do_fetch_html`.** Chain is `http → browser → browser+mobile → wayback`. Per-tier wall-clock caps (`HTTP_MAX_BUDGET` 5s, `BROWSER_MAX_BUDGET` 8s, `MOBILE_MAX_BUDGET` 5s, `WAYBACK_MAX_BUDGET` 6s) are the *primary* budget — the chain naturally takes up to ~24s for a full run. The caller-supplied `deadline` (default 30s) is a kill-switch hard upper bound, not the timing users feel in practice. Each tier's effective deadline is `min(global_deadline, now + tier_cap)`. `cache.bump`'s `failure_count` tracks consecutive failures of the *preferred* mode only; non-preferred-mode bumps don't move the counter. Strategy is keyed per-**route** (`cache.route_key(url)`: registered_domain + first path segment, e.g. `vivareal.com.br/aluguel/*` vs `vivareal.com.br/imovel/*`), so page-types within a domain learn independent starting tiers. The starting tier is `cache.get_strategy(route)` (learned) falling back to `strategy.seed_strategy(route)` (declarative seed in `vasco/strategy.py`); it picks the *starting* tier only — it never decides whether the chain runs to completion. Content adapters (real-estate, google-shopping, olx) route their HTML fetch through this same chain via an injected `fetch_html`, so they share the strategy/seed system instead of hardcoding a browser-only fetch.
- **Phase timing is part of the envelope contract.** `duration_ms` is always stamped; `network_ms`, `parse_ms`, `cache_write_ms`, `attempts`, and `escalated_from` are populated on real fetches via the `_Phases` accumulator threaded through `_do_fetch_html` / `_fetch_pdf`. Cache hits and short-circuit paths stamp only `duration_ms`; `_hydrate_cache_hit` strips phase fields defensively. `telemetry.fetch_success_fields` surfaces them through the success event so `vasco logs stats` can roll up `phase_percentiles` and `escalation_rate`.
- **Telemetry has four outcomes.** `ok` / `fail` / `exception` / `empty` (the last for `extract` returning zero passages). Successes log too — disable the whole stream with `logging.enabled: false` or `VASCO_LOGGING_ENABLED=false`. Writes never block tool calls; I/O errors are swallowed. Content-adapter successes additionally carry `provider`/`page_type`/`result_count` (via `telemetry.fetch_success_fields` reading the envelope's `quality` block), so `logs stats` rolls up a per-provider `adapters` block with `zero_result_rate` — a high rate is the fingerprint of a silently-rotted adapter (still 200ing, parsing to nothing). `parse_failed` shows up in the failure histogram for free.
- **Negative-cache TTL is per-reason.** `fetch._FAILURE_TTL_MULTIPLIER` scales the base `failure_ttl_seconds` (default 900s) by failure reason. Permanent failures (`NOT_FOUND`, `ROBOTS_DISALLOW`, `INVALID_URL`) get ~24h; transient ones (`TIMEOUT`, `SERVER_ERROR`, `DNS_FAIL`, `PARSE_FAILED`) get ~5min. This keeps retries fast for flaky upstreams without hammering pages that genuinely don't exist. `PARSE_FAILED` is transient on purpose: scraper-rot is fixed by a code change (or the site reverting), so a redeployed adapter should heal fast instead of being pinned to a stale failure for a day.
- **HTTP tier sends modern-Chrome headers.** `_http_fetch` sends `Sec-Fetch-*`, `Accept-Language`, `Accept-Encoding`, and `Upgrade-Insecure-Requests` alongside the configurable `User-Agent` to avoid WAF short-circuits that reject bare-UA requests before the browser tier can help.
- **The browser tier is a separate peer service; the client only proxies.** Camoufox is **not** a dependency of core `vasco` — it lives in the `[browser]` optional extra and is imported only by `browser_server.py` (`vasco browser-server`). The fetch client (`vasco/fetch/browser.py`) never launches a browser: `get_browser(cfg)` returns a process-singleton `BrowserPool` (reset in tests via `browser._reset_for_tests()`) that forwards fetches to the server's UNIX socket. There is **no in-process fallback** — when the server isn't running, `BrowserPool` raises `BrowserServerUnavailable`, which `_browser_fetch` turns into the `BROWSER_UNAVAILABLE` failure reason (transient 0.33× TTL, not emitted by `bot_detect`) and the auto chain escalates to wayback instead of crashing. All browser config (locale, `user_data_dir` persistent_context for Cloudflare/login cookies, tracker blocking) is resolved **server-side** in `browser_server._build_launch_kwargs`; the client passes only URL + deadline + mobile flag, so `cfg` is ignored on the client. MCP's `VASCO_BROWSER_PREWARM=true` lifespan prewarm now just establishes the socket connection early; failures (server not running) are swallowed.
- **Browser-tier tracker blocking is third-party-only and on by default.** `browser.block_trackers` (default true) installs a `page.route("**/*")` handler in `fetch_page` that aborts requests whose `registered_domain` differs from the page's **and** match a tracker/ad hostlist (`vasco/fetch/netblock.py`). First-party requests (incl. same-site CDNs) always load, so a page's own resources are never dropped; blocking third-party trackers makes the headless browser look like a real adblock user, aiding rather than hurting the tier's anti-bot purpose. The list is resolved once off the event loop (`load_netblock` via `asyncio.to_thread`) at server startup — the handler itself is an O(1) frozenset membership test, and interception errors are swallowed so they can never kill a fetch. Default list is bundled (`data/netblock_default.txt`); `browser.network_blocklist_paths` points at local files or remote URLs (hosts/plain/uBlacklist formats) through the shared consolidate + 7-day-refresh loader. **Blocking happens entirely in `browser_server`** (which owns `fetch_page` and opens the page); the client just proxies, so there's no double-filtering and no new wire field — it's governed by the *server's* config. Decided against the uBlock *extension* (fingerprint surface, defeats stealth) and against EasyList ABP syntax (cosmetic rules N/A to request interception; network-rule subset needs a heavy parser).
- **Config precedence**: CLI flag > `VASCO_*` env var > `~/.config/vasco/config.yaml` > dataclass default. `cfg=None` is allowed everywhere internal — code falls back to defaults.
- **Quality scoring is two layers.** (1) Domain blocklist from community-curated files (uBlacklist + plain-domain formats); (2) text heuristics (slop vocab, sentence CV, em-dash density, transition starts, TTR) plus envelope metadata signals (boilerplate ratio, missing byline/date, thin content). Both produce signals merged into the envelope's `quality` dict alongside existing `trafilatura_confidence` and `boilerplate_ratio`. The composite `slop_score` (0-1, higher=worse) uses calibrated weights (15% text heuristics, 85% metadata — see `scripts/calibrate_quality.py`); the raw `signals` dict lets consumers apply their own thresholds. Blocklist is a lazy singleton (`quality.blocklist.get_blocklist`); reset in tests via `blocklist.reset()`. Quality scoring is enabled when `cfg.quality` is not None (the default); disable via `quality: false` in YAML or `VASCO_QUALITY_ENABLED=false`. Blocklist paths configured via `quality.blocklist_paths` (YAML list) or `VASCO_QUALITY_BLOCKLIST_PATHS` (colon-separated); accepts both local files and HTTP(S) URLs, consolidated to `$XDG_CACHE_HOME/vasco/blocklist.txt` with 7-day refresh.

## Testing

- Per-module unit tests in `tests/test_<module>.py`.
- **Cross-module integration tests** in `tests/test_fetch_integration.py` use a real `Cache` (sqlite in `tmp_path`) and stub only `_http_fetch` at the network seam. Add to that file when fixing bugs that span modules — those are exactly the bugs unit tests with mocks per layer miss.
- Bot-detect tests are fixture-driven from `tests/fixtures/*.html`. Add a new fixture for any new failure signature.
- `asyncio_mode = "auto"` is configured in `pyproject.toml`, so `async def test_*` works without decorators.

## Conventions

- `from __future__ import annotations` at the top of every file.
- Type hints on public functions; docstrings only where the *why* is non-obvious.
- Failure handling: return failure envelopes, don't raise.
- Cache writes go through `Cache.put`; never INSERT/UPDATE the table directly.
- Don't add Claude/Anthropic co-author trailers to commits.
- When adding or renaming fields in any `*Cfg` dataclass, update `config.yaml.template` to match.

## Verification recipes

```bash
uv run vasco normalize "https://Example.COM:443/foo/?utm_source=x&b=2&a=1#frag"
# → https://example.com/foo?a=1&b=2

uv run vasco fetch https://example.com | jq '.from_cache, .word_count'
uv run vasco fetch https://example.com | jq '.from_cache, .cache_age_seconds'   # second call hits cache

uv run vasco fetch https://httpbin.org/status/404 | jq '.failure.reason'        # → "not_found"

uv run vasco fetch https://example.com | jq '.duration_ms, .network_ms, .parse_ms, .attempts'
# phase timing on a live fetch; cache hits report only duration_ms

uv run vasco logs stats --days 7 | jq '.by_tool, .escalation_rate, .phase_percentiles'
# rollup over the last 7 days of telemetry JSONL

VASCO_FETCH_WORKERS=7 uv run python -c "from vasco.config import load_config; print(load_config().fetch.workers)"
# → 7

uv run vasco serve &     # vascod on $XDG_RUNTIME_DIR/vasco/vascod.sock (or: systemctl --user start vascod)
uv run python -c "import asyncio; from vasco.service.client import DaemonClient; \
print(asyncio.run(DaemonClient().request('fetch', url='https://example.com'))['mode_used'])"
# full fetch over the socket; single-flight collapses concurrent identical fetches to one upstream GET

uv run vasco fetch https://example.com | jq '.quality.slop_score, .quality.domain_flagged, .quality.signals'
# quality scoring: slop_score 0-1 (15% text heuristics, 85% metadata), domain blocklist check, raw signal breakdown

uv run vasco fetch "https://www.google.com/search?udm=28&q=kindle+paperwhite" \
  | jq '.mode_used, .quality.result_count, .quality.filtered, .quality.products[0:3]'
# Google Shopping adapter: structured products in quality.products, drops by reason in quality.filtered

uv run vasco fetch "https://www.vivareal.com.br/aluguel/sp/sao-carlos/" \
  | jq '.mode_used, .quality.provider, .quality.page_type, .quality.result_count, .quality.listings[0]'
# Real-estate adapter: normalized listings in quality.listings; routes vivareal by domain,
# list vs detail by URL (detail pages add the full photo gallery)

uv run vasco fetch "https://www.olx.com.br/autos-e-pecas/carros-vans-e-utilitarios/estado-sp" \
  | jq '.mode_used, .quality.vertical, .quality.page_type, .quality.result_count, .quality.listings[0].attributes'
# OLX adapter: real-estate (/imoveis/) + vehicle (/autos-e-pecas/) verticals only; per-vertical typed
# fields in quality.listings[].attributes. Cloudflare-protected → starts at the browser tier (seeded).

uv run vasco fetch "https://lista.mercadolivre.com.br/notebook" \
  | jq '.mode_used, .quality.page_type, .quality.result_count, .quality.products[0]'
uv run vasco fetch "https://www.mercadolivre.com.br/<slug>/p/MLB43417665" \
  | jq '.mode_used, .quality.page_type, .quality.products[0] | {title,price,condition,seller,sold_quantity,attributes}'
# MercadoLivre BR adapter: JSON-LD spine (search @graph → many products; product page → one rich product),
# best-effort PDP extras (seller/sold_quantity/installments/original_price/attributes). Bot-challenged → browser tier (seeded).
```
