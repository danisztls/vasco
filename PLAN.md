# Vasco — CLI for AI web research

## Context

LLM agents are bad at web research out of the box: they have no native browser, search APIs cost money or hit anti-bot walls, and raw HTML is the wrong shape for a context window. Vasco is a small Python CLI (and a parallel MCP server starting in v0.2) that exposes the primitives an agent actually needs:

1. **search** — query the web, get title/URL/snippet (pluggable backend; ddg in v0.1)
2. **fetch** — turn a URL into clean Markdown plus a rich envelope, even on JS-heavy or bot-protected sites; handles HTML and PDF in v0.1
3. **extract** — fetch + return only passages matching a query (BM25 in v0.1; semantic in v0.2). Tavily-extract equivalent
4. **map** — discover URLs on a site (sitemap → feeds → light spider)

CLI first because the text contract forces a clean core before adding MCP as a second transport. The repo is currently empty (`main`, no commits); this plan creates the project from scratch.

## Stack

| Concern | Pick | Why |
|---|---|---|
| Runtime | Python 3.12+, `uv` | Matches default toolchain |
| CLI | `typer` | Type-hint driven, sub-commands |
| Search (v0.1) | `ddgs` behind a pluggable `Searcher` protocol | Free, multi-backend (DDG/Bing/Brave/Mojeek); future backends (Tavily, Brave API, Kagi) drop into the same interface |
| HTTP client | `httpx` | Async + sync, HTTP/2 |
| Stealth browser | `camoufox` | Patched-Firefox anti-fingerprint, drives via Playwright |
| Extraction / Markdown | `trafilatura` | Boilerplate stripping + Markdown + sitemap/feed/spider |
| PDF | `pdftotext`, `pdfgrep` (shelled out, already on PATH) | No Python PDF dep needed |
| Ranking (extract) | `rank-bm25` | ~200 LoC pure-Python BM25; deterministic, no model download |
| Cache | stdlib `sqlite3` | Easy to inspect, TTL columns |
| Config | `tomllib` + env vars | Stdlib |

No `readability-lxml`, no `markdownify`, no `pandoc` dep — trafilatura covers all of it. No embedding model in v0.1.

## Subcommands

```
vasco search    <query>  [--max 10] [--region us-en] [--time d|w|m|y]
                          [--site DOMAIN] [--backend ddg|auto] [--json]

vasco fetch     <url...>  [--mode auto|http|browser] [--workers 4]
                          [--no-cache] [--refresh] [--deadline 15s] [--raw]
                          [--json | --concat]
                          # single url, TTY:   Markdown to stdout
                          # single url, pipe:  JSON envelope
                          # multiple urls:     NDJSON (one envelope per line)

vasco extract   <url>     --query "..." [--top 5] [--context-chars 400]
                          [--mode auto|http|browser]
                          # passages ranked by BM25; works on HTML and PDF

vasco map       <url>     [--source sitemap|feeds|spider|all]
                          [--limit 1000] [--depth 2]
                          # NDJSON: {url, source, lastmod?}

vasco normalize <url>     # prints the canonical form used by the cache key
vasco cache     list | purge [--older-than 7d] | stats
```

## Fetch envelope (v0.1 contract)

Single source of truth for `fetch` and `extract` output. Every successful fetch (cache hit or miss) returns this:

```json
{
  "url_requested":  "https://example.com/path?b=2&a=1#frag",
  "url_final":      "https://example.com/path?a=1&b=2",
  "url_canonical":  "https://example.com/path",
  "http_status":    200,
  "mode_used":      "http",
  "fetched_at":     1747584000,
  "from_cache":     true,
  "cache_age_seconds": 3812,
  "content_type":   "text/html",

  "title":          "...",
  "byline":         "...",
  "published":      "2025-11-01",
  "modified":       "2025-11-03",
  "language":       "en",
  "site_name":      "Example Corp",

  "word_count":     1842,
  "token_count_estimate": 2310,        # cl100k tiktoken approximation
  "quality": {
    "trafilatura_confidence": 0.86,
    "boilerplate_ratio":      0.12
  },

  "links": [
    {"url": "https://...", "anchor": "the thing", "rel": null},
    ...
  ],

  "markdown":       "...",
  "warnings":       ["short_content"]    # see failure model below
}
```

**TTY default for single fetch is `markdown` only** (line is short, human reads it). **Non-TTY default is the full JSON envelope.** Batch is always NDJSON. `--json` and `--raw` force the choice.

## Failure model

Failures are first-class output, not exceptions. The envelope adds a `failure` object and omits successful fields it couldn't populate. Partial content is still returned when available — the agent decides whether to use it.

```json
{
  "url_requested": "...",
  "http_status":   403,
  "mode_used":     "browser",
  "failure": {
    "reason":             "blocked_cloudflare",
    "retry_after_seconds": null,
    "message":            "Cloudflare challenge page detected after browser tier"
  },
  "warnings": ["paywall_soft_with_partial"],
  "markdown": "<first 3 paragraphs that did extract>"
}
```

`reason` is a closed enum:

```
ok | blocked_cloudflare | blocked_captcha | paywall_hard |
paywall_soft_with_partial | login_required | not_found | server_error |
timeout | deadline_exceeded | js_app_needs_interaction | dns_fail |
robots_disallow | unsupported_content_type | invalid_url
```

`bot_detect.py` produces the `reason`; never throws. Cache stores failures with a short TTL (15min) so we don't hammer dead URLs but also don't poison the cache forever.

## Module layout

```
vasco/
  __init__.py
  cli.py            # Typer app, TTY-aware output selection
  search.py         # Searcher protocol + DdgsBackend, --site, normalization
  fetch.py          # auto-mode logic, envelope assembly, deadline handling
  extract.py        # passage segmentation + BM25 ranking (rank-bm25)
  pdf.py            # pdftotext / pdfgrep adapters; same envelope shape
  browser.py        # Camoufox singleton, page pool, lifecycle
  convert.py        # trafilatura.extract() wrapper, --raw path
  map.py            # trafilatura.sitemaps / feeds / spider wrapper
  cache.py          # SQLite schema, URL normalization, domain strategy
  bot_detect.py     # signature list → FailureReason
  errors.py         # FailureReason enum
  config.py         # config.toml + env var resolution
  io.py             # NDJSON writer, envelope serializer, TTY detection
tests/
  test_normalize.py     # URL canonicalization, table-driven
  test_bot_detect.py    # fixture HTML files (CF challenge, paywalls, …)
  test_cache_ttl.py     # TTL math, expiry, refresh semantics
  test_escalation.py    # mode-escalation state machine
  test_extract.py       # BM25 on known fixtures, --top, --context-chars
  fixtures/
    cloudflare_challenge.html
    paywall_soft.html
    paywall_hard.html
    article_clean.html
    sample.pdf
pyproject.toml
```

## Cache schema (`$XDG_CACHE_HOME/vasco/cache.db`)

```sql
CREATE TABLE fetch_cache (
  url            TEXT PRIMARY KEY,    -- normalized
  final_url      TEXT,
  canonical_url  TEXT,
  title          TEXT,
  byline         TEXT,
  published      TEXT,                -- ISO-8601
  language       TEXT,
  site_name      TEXT,
  word_count     INTEGER,
  token_count    INTEGER,
  quality_json   TEXT,
  links_json     TEXT,                -- separate column; agents query it
  markdown       TEXT,
  warnings_json  TEXT,
  status         INTEGER,
  failure_reason TEXT,                -- null on success
  mode_used      TEXT,
  fetched_at     INTEGER,
  ttl_expires    INTEGER,
  content_type   TEXT,
  html_gz        BLOB                 -- optional, --keep-html
);

CREATE TABLE domain_strategy (
  domain          TEXT PRIMARY KEY,
  preferred_mode  TEXT,
  success_count   INTEGER DEFAULT 0,
  failure_count   INTEGER DEFAULT 0,
  last_updated    INTEGER
);
```

Default TTL: 24h success, 15min failure. `--refresh` ignores cache for reads but writes; `--no-cache` skips both.

## URL normalization (cache key — tested)

Lowercase scheme + host, drop fragment, drop default ports, drop trailing slash from non-root paths, sort query params, drop tracking params (`utm_*`, `fbclid`, `gclid`, `mc_eid`). `vasco normalize <url>` is the exposed implementation. Tests cover ~30 table-driven cases.

## Fetch mode selection (auto with per-domain learning)

```
domain = registered_domain(url)
strategy = cache.get_domain_strategy(domain)
deadline = time.monotonic() + cli.deadline_seconds

if strategy.preferred_mode == 'browser':
    html, status = browser_fetch(url, deadline)
else:
    html, status = http_fetch(url, deadline)
    reason = bot_detect.classify(status, html, response_headers)
    if reason != 'ok':
        if time_remaining(deadline) < BROWSER_MIN_BUDGET:
            return failure_envelope('deadline_exceeded', partial=html)
        html, status, reason = browser_fetch(url, deadline)
        cache.bump(domain, mode='browser' if reason == 'ok' else strategy.preferred_mode)
    else:
        cache.bump(domain, mode='http')

if reason != 'ok':
    return failure_envelope(reason, partial=html, retry_after=parse_retry_after(headers))

markdown, meta = trafilatura.extract(html, output_format='markdown', with_metadata=True)
return success_envelope(...)
```

`bot_detect.classify` returns a `FailureReason`. Switches strategy after 3 consecutive failures of the preferred mode.

## Concurrency

`vasco fetch` accepts N URLs and runs `asyncio.Semaphore(workers)` (default 4). One Camoufox browser is started lazily on first browser-tier need and reused across the invocation; pages are per-URL and torn down post-extract. Browser shutdown is in `finally`. Single-URL invocations skip the pool.

## Configuration (`$XDG_CONFIG_HOME/vasco/config.toml`)

```toml
[search]
default_backend = "ddg"
region          = "us-en"
max_results     = 10

[fetch]
default_mode    = "auto"
workers         = 4
ttl_seconds     = 86400
failure_ttl_seconds = 900
deadline_seconds = 15
user_agent      = "Mozilla/5.0 (...)"

[browser]
headless = true
locale   = "en-US"

[cache]
path = "$XDG_CACHE_HOME/vasco/cache.db"
```

Env vars override (`VASCO_*`); CLI flags override env.

## Phase split

**v0.1 (this plan):**
- `pyproject.toml`, `uv` setup, `vasco` entry point
- `search` (ddg backend behind `Searcher` protocol, `--site` operator), `fetch` (single + batch + envelope + failure model + PDF via pdftotext), `extract` (BM25), `map`, `normalize`, `cache`
- Auto-mode fetch with per-domain learning + bot detection
- Camoufox singleton + reuse
- pytest covering URL norm, bot-detect, cache TTL, mode escalation, BM25

**v0.2 (parallel tracks):**
- MCP server (`vasco/mcp.py`) using official `mcp` Python SDK, shares the v0.1 core modules
- Semantic mode for `extract` (sentence-transformers, opt-in)
- Additional `Searcher` backends (Tavily, Brave, Kagi)
- YouTube transcripts (auto-detected): port the working pattern from `~/Dev/cli/claudinho/process/summarize.py` — `yt_dlp` Python lib + VTT parsing with transition-block dedup + SponsorBlock filtering. Don't reinvent
- Daemon mode that keeps the browser warm between CLI calls

## Verification

```bash
# 1. Search returns structured results
uv run vasco search "rust async runtimes" --max 5 --json | jq .
uv run vasco search "async runtime" --site doc.rust-lang.org

# 2. Plain HTTP fetch returns clean Markdown to TTY
uv run vasco fetch https://adrien.barbaresi.eu/blog/

# 3. Same URL piped emits JSON envelope (TTY-aware default)
uv run vasco fetch https://adrien.barbaresi.eu/blog/ | jq '.title, .word_count'

# 4. Bot-protected site escalates to browser tier; envelope reflects mode_used
uv run vasco fetch https://www.g2.com/products/notion/reviews | jq '.mode_used, .failure'

# 5. Batch streams NDJSON as pages complete
uv run vasco fetch https://example.com https://news.ycombinator.com | mlr --j2t cat

# 6. PDF fetch via pdftotext path
uv run vasco fetch https://arxiv.org/pdf/2410.10934.pdf | head -40

# 7. Extract: query-relevant passages from a long page
uv run vasco extract https://en.wikipedia.org/wiki/Camoufox --query "anti-fingerprinting techniques" --top 3

# 8. Failure envelope is well-shaped
uv run vasco fetch https://httpbin.org/status/404 | jq '.failure.reason'
# expect: "not_found"

# 9. Deadline returns typed failure rather than blocking
uv run vasco fetch https://example.com --deadline 0.001s | jq '.failure.reason'
# expect: "deadline_exceeded"

# 10. Cache hit is near-instant
hyperfine 'uv run vasco fetch https://example.com'

# 11. Per-domain learning persists
sqlite3 ~/.cache/vasco/cache.db 'SELECT * FROM domain_strategy;'

# 12. Map a site
uv run vasco map https://adrien.barbaresi.eu --source sitemap --limit 50

# 13. URL normalization is exposed and stable
uv run vasco normalize "https://Example.COM:443/foo/?b=2&utm_source=x&a=1#frag"
# expect: "https://example.com/foo?a=1&b=2"

# 14. Unit tests pass
uv run pytest -q
```

## Critical files to create (none exist yet)

- `pyproject.toml` — entry point `vasco = "vasco.cli:app"`, deps pinned
- `vasco/cli.py` — Typer app, TTY-aware output selection
- `vasco/fetch.py` — envelope assembly + auto-mode + deadline. Most load-bearing file
- `vasco/bot_detect.py` + `vasco/errors.py` — typed failure-reason classification
- `vasco/cache.py` — schema + URL normalization. Correctness primitive: cache key bugs poison every subsequent lookup
- `vasco/browser.py` — Camoufox lifecycle. Leaks Firefox processes if wrong
- `vasco/extract.py` — passage segmentation + BM25
- `tests/test_normalize.py`, `tests/test_bot_detect.py`, `tests/test_cache_ttl.py`, `tests/test_escalation.py`, `tests/test_extract.py`

## Out of scope for v0.1

- Authenticated fetches (cookies, login flows)
- JS interaction beyond "wait for load"
- YouTube transcripts (v0.2 — port from `~/Dev/cli/claudinho/process/summarize.py`)
- Semantic ranking in `extract` (v0.2, sentence-transformers)
- Image OCR (use `tesseract` on PATH ad-hoc)
- Proxy rotation / paid bypass services
- Paid search backends (v0.2 once `Searcher` protocol is proven)
