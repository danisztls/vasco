# CLAUDE.md

Vasco — CLI for AI web research. Python 3.12+, managed with `uv`.

## Run

| Task | Command |
|---|---|
| Install | `uv sync` (`--group dev` for tests) |
| CLI | `uv run vasco <subcommand>` |
| Tests | `uv run --group dev pytest -q` |
| Lint / format | `uv run ruff check .` / `uv run ruff format .` |

## Module map

| File | Role |
|---|---|
| `vasco/interface/cli.py` | Typer app, TTY-aware output, parses `--deadline`/`--older-than`; `search`/`fetch`/`extract`/`answer`/`map`/`normalize` commands + `cache` and `logs` sub-apps |
| `vasco/interface/mcp.py` | MCP server (stdio); tools `search`/`fetch`/`fetch_many`/`extract`/`answer`/`map`; opt-in browser prewarm; `fetch_many` defaults to `metadata_only=true`; llms.txt taint warning on `map` results |
| `vasco/fetch/__init__.py` | `fetch_one` / `fetch_many`, auto chain `http → browser → browser+mobile → wayback`, phase timing, envelope assembly |
| `vasco/fetch/browser.py` | Camoufox singleton (`get_browser(cfg)` → `BrowserPool`); `fetch(mobile=)` for iOS UA/viewport |
| `vasco/fetch/bot_detect.py` | `classify(status, html, headers) -> FailureReason` (pure) |
| `vasco/telemetry/__init__.py` | JSONL event log; `record_success` / `record_failure` / `record_exception` with `outcome` discriminator |
| `vasco/telemetry/logstats.py` | `summarize(cfg, days=)` — per-tool counts, cache-hit ratio, mode mix, failure histogram, duration + phase percentiles, escalation rate |
| `vasco/search.py` | `Searcher` protocol, `SearchResult`, `get_searcher()` factory, `--site` operator |
| `vasco/adapters/ddgs.py` | DuckDuckGo search backend (`DdgsBackend`) |
| `vasco/adapters/tavily.py` | Tavily search backend (`TavilyBackend`) |
| `vasco/adapters/wayback.py` | Wayback Availability API + `if_` modifier; trailing-slash retry |
| `vasco/adapters/youtube.py` | Transcript fetch — own envelope shape (`mode_used="youtube"`, `content_type="text/youtube"`) |
| `vasco/adapters/wikimedia.py` | Wikimedia article fetch via Enterprise On-demand API — Structured Contents (Wikipedia, 9 beta langs) + standard articles (all projects/langs); own envelope shape (`mode_used="wikimedia"`, `content_type="text/wikimedia"`) |
| `vasco/adapters/google_shopping.py` | Google Shopping BR results (search + homepage) — browser-rendered HTML parsed via `<product-viewer-entrypoint>` aria-labels into structured products (title, price, store, rating, discount, badges) in `quality.products`; filters used/refurb + international sellers + IQR outliers; own envelope shape (`mode_used="google_shopping"`, `content_type="application/x-google-shopping"`) |
| `vasco/quality/__init__.py` | `score(markdown, url, cfg)` → composite quality dict merged into envelope; orchestrates both layers |
| `vasco/quality/heuristics.py` | Text-level slop detection: vocab ratio, phrase count, sentence CV, em-dash density, transition starts, TTR |
| `vasco/quality/blocklist.py` | Domain blocklist loader (plain + uBlacklist formats); lazy singleton; `is_blocked(url)` |
| `vasco/quality/wordlists.py` | Slop vocabulary data (tier-1 words, phrases, transition starters) |
| `vasco/extract.py` | Passage segmentation + BM25 / semantic ranking |
| `vasco/semantic.py` | Lazy sentence-transformers ranker; raises `SemanticRankerUnavailable` if extra is missing |
| `vasco/summarize.py` | Powers the `answer` command: `answer(url, question=)` fetches then asks an LLM (DeepSeek default); `summarize()` does the LLM call. Returns `None`/`error` on failure, never raises. Page is cached via `fetch_one`; the answer is not cached |
| `vasco/adapters/deepseek.py` | Async OpenAI-compatible chat client (`DeepSeekClient`) for the `answer` command; raises on HTTP error |
| `vasco/map.py` | sitemap / feeds / spider via trafilatura, `--exclude` filters; llms.txt discovery (disk-cached 24h) |
| `vasco/cache.py` | SQLite cache + URL normalization (incl. AMP folding, YouTube collapse, Wikimedia mobile→desktop) + per-domain strategy (starting tier only) |
| `vasco/converters/convert.py` | `html_to_markdown` (trafilatura wrapper + link extraction) |
| `vasco/converters/pdf.py` | `pdftotext` / `pdfinfo` shell adapter |
| `vasco/converters/pandoc.py` | Pandoc shell adapter (DOCX, EPUB, ODT, RTF → Markdown) |
| `vasco/errors.py` | `FailureReason` enum |
| `vasco/config.py` | `load_config()` → YAML + `VASCO_*` env vars |
| `vasco/io.py` | TTY detection, NDJSON/JSON/markdown writers, token estimator |

## Invariants

- **Fetch envelope is the contract.** Same shape across `fetch_one`, `extract`, `cache.get`. Adding/renaming a field requires updating `cache.py` columns + `_hydrate_cache_hit` + the integration test.
- **`fetch_one` never raises.** Failures are first-class output via a `failure` object whose `reason` is a `FailureReason` enum value. New failure modes go in `errors.py` first, then `fetch.bot_detect.classify` learns to produce them.
- **URL normalization is the cache key.** `cache.normalize_url` is load-bearing — changing it invalidates every cached entry. Besides lowering, sorting params, and stripping tracking params, it folds AMP variants (`?amp=1`, `?output=amp`, `/amp/` path segments) and collapses all YouTube URL forms (`youtu.be`, `m.youtube.com`, local TLDs, `/embed/`, `/shorts/`) to bare `youtube.com/watch?v=<id>`. Tests in `tests/test_normalize.py` are table-driven.
- **Auto-mode escalation lives in `fetch._do_fetch_html`.** Chain is `http → browser → browser+mobile → wayback`. Per-tier wall-clock caps (`HTTP_MAX_BUDGET` 5s, `BROWSER_MAX_BUDGET` 8s, `MOBILE_MAX_BUDGET` 5s, `WAYBACK_MAX_BUDGET` 6s) are the *primary* budget — the chain naturally takes up to ~24s for a full run. The caller-supplied `deadline` (default 30s) is a kill-switch hard upper bound, not the timing users feel in practice. Each tier's effective deadline is `min(global_deadline, now + tier_cap)`. `cache.bump`'s `failure_count` tracks consecutive failures of the *preferred* mode only; non-preferred-mode bumps don't move the counter. Per-domain strategy picks the *starting* tier only — it never decides whether the chain runs to completion.
- **Phase timing is part of the envelope contract.** `duration_ms` is always stamped; `network_ms`, `parse_ms`, `cache_write_ms`, `attempts`, and `escalated_from` are populated on real fetches via the `_Phases` accumulator threaded through `_do_fetch_html` / `_fetch_pdf`. Cache hits and short-circuit paths stamp only `duration_ms`; `_hydrate_cache_hit` strips phase fields defensively. `telemetry.fetch_success_fields` surfaces them through the success event so `vasco logs stats` can roll up `phase_percentiles` and `escalation_rate`.
- **Telemetry has four outcomes.** `ok` / `fail` / `exception` / `empty` (the last for `extract` returning zero passages). Successes log too — disable the whole stream with `logging.enabled: false` or `VASCO_LOGGING_ENABLED=false`. Writes never block tool calls; I/O errors are swallowed.
- **Negative-cache TTL is per-reason.** `fetch._FAILURE_TTL_MULTIPLIER` scales the base `failure_ttl_seconds` (default 900s) by failure reason. Permanent failures (`NOT_FOUND`, `ROBOTS_DISALLOW`, `INVALID_URL`) get ~24h; transient ones (`TIMEOUT`, `SERVER_ERROR`, `DNS_FAIL`) get ~5min. This keeps retries fast for flaky upstreams without hammering pages that genuinely don't exist.
- **HTTP tier sends modern-Chrome headers.** `_http_fetch` sends `Sec-Fetch-*`, `Accept-Language`, `Accept-Encoding`, and `Upgrade-Insecure-Requests` alongside the configurable `User-Agent` to avoid WAF short-circuits that reject bare-UA requests before the browser tier can help.
- **Browser is a process singleton.** `get_browser(cfg)` reads `BrowserCfg` only on first construction; subsequent calls return the same instance regardless of cfg. Reset in tests via `browser._reset_for_tests()`. MCP can opt into a lifespan prewarm with `VASCO_BROWSER_PREWARM=true` so the first browser-tier fetch isn't the one paying Firefox cold-start cost — prewarm failures (e.g. camoufox missing) are swallowed. Setting `browser.user_data_dir` (opt-in, empty by default) flips Camoufox into persistent_context mode so Cloudflare/login cookies survive across runs; in that mode `AsyncCamoufox` yields a `BrowserContext` (no `.new_context()`), so the mobile recovery tier falls back to per-page UA + viewport overrides.
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

uv run vasco fetch https://example.com | jq '.quality.slop_score, .quality.domain_flagged, .quality.signals'
# quality scoring: slop_score 0-1 (15% text heuristics, 85% metadata), domain blocklist check, raw signal breakdown

uv run vasco fetch "https://www.google.com/search?udm=28&q=kindle+paperwhite" \
  | jq '.mode_used, .quality.result_count, .quality.filtered, .quality.products[0:3]'
# Google Shopping adapter: structured products in quality.products, drops by reason in quality.filtered
```
