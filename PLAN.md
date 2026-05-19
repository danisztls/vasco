# Vasco — design notes & roadmap

## Status

- **v0.1 — shipped** (commit `3a762ca`). CLI implemented with search, fetch, extract, map, normalize, and cache. 64 pytest cases pass. See `README.md` for usage and `CLAUDE.md` for architecture notes.
- **v0.2 — to be planned.** Tracks sketched at the bottom of this file; details TBD.

## Vision

LLM agents are bad at web research out of the box: they have no native browser, search APIs cost money or hit anti-bot walls, and raw HTML is the wrong shape for a context window. Vasco exposes the primitives an agent actually needs:

1. **search** — query the web, get title/URL/snippet (pluggable backend)
2. **fetch** — turn a URL into clean Markdown plus a rich envelope, even on JS-heavy or bot-protected sites
3. **extract** — fetch + return only passages matching a query
4. **map** — discover URLs on a site (sitemap → feeds → light spider)

CLI first because the text contract forces a clean core before adding MCP as a second transport.

## v0.1 — shipped reference

### Stack

| Concern | Pick | Why |
|---|---|---|
| Runtime | Python 3.12+, `uv` | Matches default toolchain |
| CLI | `typer` | Type-hint driven, sub-commands |
| Search | `ddgs` behind a pluggable `Searcher` protocol | Free, multi-backend; future backends (Tavily, Brave, Kagi) drop into the same interface |
| HTTP client | `httpx` | Async + sync, HTTP/2 |
| Stealth browser | `camoufox` | Patched-Firefox anti-fingerprint, drives via Playwright |
| Extraction / Markdown | `trafilatura` | Boilerplate stripping + Markdown + sitemap/feed/spider |
| PDF | `pdftotext`, `pdfinfo` (shelled out) | No Python PDF dep |
| Ranking (extract) | `rank-bm25` | Deterministic, no model download |
| Cache | stdlib `sqlite3` | Easy to inspect, TTL columns |
| Config | `tomllib` + env vars | Stdlib |

### Fetch envelope (single source of truth)

```json
{
  "url_requested": "...", "url_final": "...", "url_canonical": "...",
  "http_status": 200, "mode_used": "http", "fetched_at": 1747584000,
  "from_cache": false, "cache_age_seconds": 0, "content_type": "text/html",
  "title": "...", "byline": "...", "published": "...", "modified": "...",
  "language": "en", "site_name": "...",
  "word_count": 1842, "token_count_estimate": 2310,
  "quality": { "trafilatura_confidence": 0.86, "boilerplate_ratio": 0.12 },
  "links": [{"url": "...", "anchor": "...", "rel": null}],
  "markdown": "...", "warnings": []
}
```

Failure envelope adds a `failure` object with a typed `reason` (closed enum in `vasco/errors.py`). `fetch_one` never raises; partial content is returned when available.

### Auto-mode escalation (`vasco/fetch.py`)

```
domain   = registered_domain(url)
strategy = cache.get_domain_strategy(domain)
deadline = monotonic() + cli.deadline_seconds

if strategy == 'browser':
    html, status, headers = browser_fetch(url, deadline)
else:
    html, status, headers = http_fetch(url, deadline)
    reason = bot_detect.classify(...)
    if reason != 'ok':
        if time_remaining(deadline) < BROWSER_MIN_BUDGET:  # 3.0s
            return failure('deadline_exceeded', partial=html)
        html, status, headers = browser_fetch(url, deadline)
        reason = bot_detect.classify(...)

cache.bump(domain, mode=mode_actually_used, success=(reason == 'ok'))
```

`cache.bump` tracks consecutive failures of the *preferred* mode and flips after 3.

### Cache (`$XDG_CACHE_HOME/vasco/cache.db`)

Two tables: `fetch_cache` (envelope columns + `failure_reason` + `failure_json` + TTL) and `domain_strategy` (per-domain preferred mode + counters). Default TTLs: 24h success, 15min failure. `--refresh` skips reads but still writes; `--no-cache` skips both. URL normalization (`vasco/cache.py:normalize_url`) is the cache key.

### Out of scope for v0.1

Authenticated fetches; JS interaction beyond "wait for load"; YouTube transcripts; semantic ranking; image OCR; proxy rotation; paid search backends.

## v0.2 — open tracks (to be planned)

These were sketched during v0.1 design. Treat as starting points; details and priority will be set in our next planning round.

- **MCP server** — `vasco/mcp.py` using the official `mcp` Python SDK, sharing v0.1 core modules. Same primitives over MCP transport.
- **Semantic extract** — opt-in mode for `extract` using sentence-transformers; complements (doesn't replace) BM25.
- **Additional search backends** — Tavily, Brave, Kagi, etc. The `Searcher` protocol in `vasco/search.py` is already pluggable.
- **YouTube transcripts** — auto-detected via URL pattern. Port the working pipeline from `~/Dev/cli/claudinho/process/summarize.py` (`yt_dlp` + VTT parsing with transition-block dedup + SponsorBlock filtering).
- **Daemon mode** — keeps the Camoufox browser warm between CLI calls; meaningful speedup for repeated browser-tier fetches.

### Known v0.1 limitations worth revisiting

- `registered_domain` is a heuristic, not a PSL lookup. Edge cases under multi-label ccTLDs may misattribute the eTLD+1.
- Bot detection is body-marker-based; false negatives possible for novel challenge designs.
- BM25 is deterministic and cheap but doesn't capture semantic similarity — addressed by the semantic-extract track above.
- Camoufox + Cloudflare Turnstile is not always sufficient against the most aggressive bot protection (e.g. G2). Out of scope for v0.2 unless a paid-bypass track is added explicitly.
