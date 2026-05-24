# Vasco

CLI for AI web research. Search, fetch, extract, and map — built as primitives for LLM agents.

LLM agents don't have a native browser. Search APIs cost money or hit anti-bot walls. Raw HTML is the wrong shape for a context window. Vasco bundles the primitives an agent actually needs behind a stable text/JSON contract.

## Install

```bash
git clone git@github.com:danisztls/vasco.git
cd vasco
uv sync                       # runtime deps
uv run python -m camoufox fetch   # one-time: download patched Firefox
```

PDF support shells out to `pdftotext` and `pdfinfo` — install via `poppler` (Linux/macOS).

## Commands

```
vasco search    <query>  [--max 10] [--region us-en] [--time d|w|m|y] [--site DOMAIN] [--json]
vasco fetch     <url...> [--mode auto|http|browser|mobile|wayback] [--workers 4]
                          [--no-cache] [--refresh] [--deadline 15s] [--raw]
                          [--json | --concat]
vasco extract   <url>     --query "..." [--top 5] [--context-chars 400]
                          [--rank bm25|semantic] [--mode auto|http|browser|mobile|wayback]
vasco map       <url>     [--source sitemap|feeds|spider|all] [--limit 1000]
                          [--exclude SUBSTR]...
vasco normalize <url>
vasco cache     list | purge [--older-than 7d] | stats
vasco logs      stats [--days 1]
vasco mcp
```

### Quick tour

```bash
# Search returns title/url/snippet records
uv run vasco search "rust async runtimes" --max 5 --json | jq .

# Fetch: TTY gets markdown, pipe gets a JSON envelope
uv run vasco fetch https://example.com
uv run vasco fetch https://example.com | jq '.title, .word_count, .from_cache'

# Bot-protected sites auto-escalate: http → browser → browser+mobile → wayback
uv run vasco fetch https://www.g2.com/products/notion/reviews | jq '.mode_used, .escalated_from, .failure'

# Per-fetch phase timing on the envelope
uv run vasco fetch https://example.com | jq '.duration_ms, .network_ms, .parse_ms, .attempts'

# Batch streams NDJSON as pages complete
uv run vasco fetch https://example.com https://news.ycombinator.com

# PDF works via pdftotext
uv run vasco fetch https://arxiv.org/pdf/2410.10934.pdf | jq '.mode_used, .word_count'

# Extract returns the top BM25 passages for a query
uv run vasco extract https://en.wikipedia.org/wiki/Device_fingerprint \
  --query "canvas font fingerprinting" --top 3

# Map discovers URLs on a site; --exclude drops noise paths
uv run vasco map https://adrien.barbaresi.eu --source sitemap --limit 50 \
  --exclude /tag/ --exclude /author/

# URL normalization is exposed (and used as the cache key)
uv run vasco normalize "https://Example.COM:443/foo/?utm_source=x&b=2&a=1#frag"
# → https://example.com/foo?a=1&b=2
```

## Output contract

Every successful `fetch` (cache hit or miss) returns the same envelope:

```json
{
  "url_requested":  "...",
  "url_final":      "...",
  "url_canonical":  "...",
  "http_status":    200,
  "mode_used":      "http",
  "fetched_at":     1747584000,
  "from_cache":     false,
  "cache_age_seconds": 0,
  "content_type":   "text/html",
  "title": "...", "byline": "...", "published": "...", "modified": "...",
  "language": "en", "site_name": "...",
  "word_count": 1842, "token_count_estimate": 2310,
  "quality": { "trafilatura_confidence": 0.86, "boilerplate_ratio": 0.12 },
  "links":    [{"url": "...", "anchor": "...", "rel": null}],
  "markdown": "...",
  "duration_ms":     842,
  "network_ms":      610,
  "parse_ms":         95,
  "cache_write_ms":   12,
  "attempts":          1,
  "escalated_from":  null,
  "warnings": []
}
```

Phase fields (`network_ms`, `parse_ms`, `cache_write_ms`, `attempts`, `escalated_from`) are populated on real fetches. Cache hits and short-circuit paths stamp only `duration_ms`. `escalated_from` is set when auto-mode started in one tier and finished in another (e.g. `"http"` → finished in `browser`).

Failures replace the success-only fields with a typed `failure` object:

```json
{
  "url_requested": "...", "http_status": 403, "mode_used": "browser",
  "failure": {
    "reason": "blocked_cloudflare",
    "retry_after_seconds": null,
    "message": "blocked_cloudflare after browser tier"
  },
  "markdown": "...",
  "warnings": []
}
```

`reason` is a closed enum: `ok`, `blocked_cloudflare`, `blocked_captcha`, `blocked_bot`, `paywall_hard`, `paywall_soft_with_partial`, `login_required`, `not_found`, `server_error`, `timeout`, `deadline_exceeded`, `js_app_needs_interaction`, `dns_fail`, `robots_disallow`, `unsupported_content_type`, `invalid_url`.

`blocked_bot` covers anti-bot tear-downs at the browser tier — the page killed the Playwright session before content could be read (e.g. *"Page.content: Connection closed while reading from the driver"*). Distinct from `blocked_cloudflare`, which requires a visible CF challenge body.

## Config

`~/.config/vasco/config.toml` (all optional):

```toml
[search]
default_backend = "ddg"
region          = "us-en"
max_results     = 10

[fetch]
workers              = 4
deadline_seconds     = 15
ttl_seconds          = 86400
failure_ttl_seconds  = 900
user_agent           = "Mozilla/5.0 (...)"

[browser]
headless = true
locale   = "en-US"

[logging]
enabled = true
path    = ""        # empty → $XDG_DATA_HOME/vasco/logs
```

Precedence: CLI flag > `VASCO_*` env var > config file > default. Example: `VASCO_FETCH_WORKERS=8` overrides `fetch.workers`.

Cache lives at `$XDG_CACHE_HOME/vasco/cache.db` (default `~/.cache/vasco/cache.db`).

## Telemetry

Both the CLI and the MCP server write structured JSONL events to `$XDG_DATA_HOME/vasco/logs/YYYY-MM-DD.jsonl` (default `~/.local/share/vasco/logs/`) — one file per day, append-only. Every tool call emits a single record with an `outcome` discriminator:

- `ok` — success; carries `mode_used`, `http_status`, `from_cache`, and the phase fields (`duration_ms`, `network_ms`, `parse_ms`, `cache_write_ms`, `attempts`, `escalated_from`)
- `fail` — typed failure; carries `failure_reason` + `message` (per URL for `fetch_many`)
- `empty` — `extract` returning zero passages (separates "query was off" from "fetch silently broke")
- `exception` — uncaught tool-level exception

Common fields: `tool`, `url`, `ts` (UTC ISO 8601). Disable with `[logging] enabled = false` or `VASCO_LOGGING_ENABLED=false`. Writes never block tool calls — any I/O error is swallowed silently.

`vasco logs stats [--days N]` rolls the JSONL into a JSON summary: per-tool outcome counts, cache-hit ratio, mode mix, failure histogram, p50/p95/p99 of `duration_ms`, per-phase percentiles, and `escalation_rate` (fraction of successful fetches where auto-mode started in `http` and finished in `browser`).

```bash
tail -f ~/.local/share/vasco/logs/$(date -u +%F).jsonl | jq .
uv run vasco logs stats --days 7 | jq '.by_tool, .escalation_rate, .phase_percentiles'
jq -r 'select(.outcome=="fail") | .failure_reason' ~/.local/share/vasco/logs/*.jsonl | sort | uniq -c | sort -rn
```

## MCP server

`vasco mcp` runs the server on stdio, exposing `search`, `fetch`, `fetch_many`, `extract`, and `map` to MCP clients (Claude Desktop, Claude Code). The `BrowserPool` and any loaded semantic model stay warm for the server's lifetime.

- **`fetch_many` defaults to `metadata_only=true`.** Batch fan-outs return triage envelopes (no `markdown`) so an agent can pick what to read instead of dumping N pages of markdown into context. Refetching a chosen URL afterwards is near-free — it's a cache hit. Pass `metadata_only=false` to override.
- **`fetch` accepts `metadata_only=true`** for the same reason on large pages that would otherwise blow per-tool-output caps.
- **Browser prewarm is opt-in.** Set `VASCO_BROWSER_PREWARM=true` (or `[browser] prewarm = true`) and the server launches Camoufox during lifespan startup, so the first browser-tier fetch doesn't pay Firefox cold-start cost. Prewarm failures (e.g. camoufox not installed) are swallowed — HTTP-tier users are unaffected.

## Known limitations

- **Tables rendered via MathJax / CSS come out hollow.** Pages like the arXiv HTML view encode numeric cells through scripts that trafilatura (and most plain-HTML extractors) strip. Prose around the table survives intact; the table itself becomes a markdown skeleton with empty data cells. Workaround for now: read the surrounding paragraphs, or fetch the PDF version (`https://arxiv.org/pdf/...`) which preserves tabular data via `pdftotext`.
- **Large pages may overflow downstream context windows.** A 10k-word article can yield ~80 KB of markdown. Consumers running into per-tool-output caps should call `extract` for query-targeted passages, or pass `metadata_only=true` to the MCP `fetch` / `fetch_many` tools to get the envelope without the `markdown` field.

## License

MIT.
