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
vasco fetch     <url...> [--mode auto|http|browser] [--workers 4]
                          [--no-cache] [--refresh] [--deadline 15s] [--raw]
                          [--json | --concat]
vasco extract   <url>     --query "..." [--top 5] [--context-chars 400]
vasco map       <url>     [--source sitemap|feeds|spider|all] [--limit 1000]
                          [--exclude SUBSTR]...
vasco normalize <url>
vasco cache     list | purge [--older-than 7d] | stats
```

### Quick tour

```bash
# Search returns title/url/snippet records
uv run vasco search "rust async runtimes" --max 5 --json | jq .

# Fetch: TTY gets markdown, pipe gets a JSON envelope
uv run vasco fetch https://example.com
uv run vasco fetch https://example.com | jq '.title, .word_count, .from_cache'

# Bot-protected sites auto-escalate to a stealth browser
uv run vasco fetch https://www.g2.com/products/notion/reviews | jq '.mode_used, .failure'

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
  "warnings": []
}
```

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
```

Precedence: CLI flag > `VASCO_*` env var > config file > default. Example: `VASCO_FETCH_WORKERS=8` overrides `fetch.workers`.

Cache lives at `$XDG_CACHE_HOME/vasco/cache.db` (default `~/.cache/vasco/cache.db`).

## Known limitations

- **Tables rendered via MathJax / CSS come out hollow.** Pages like the arXiv HTML view encode numeric cells through scripts that trafilatura (and most plain-HTML extractors) strip. Prose around the table survives intact; the table itself becomes a markdown skeleton with empty data cells. Workaround for now: read the surrounding paragraphs, or fetch the PDF version (`https://arxiv.org/pdf/...`) which preserves tabular data via `pdftotext`.
- **Large pages may overflow downstream context windows.** A 10k-word article can yield ~80 KB of markdown. Consumers running into per-tool-output caps should call `extract` for query-targeted passages, or pass `metadata_only=true` to the MCP `fetch` / `fetch_many` tools to get the envelope without the `markdown` field.

## Status

- **v0.1** — shipped. Search (ddg), fetch (HTTP + Camoufox auto-escalation with per-domain learning), extract (BM25), map, cache, PDF.
- **v0.2** — planned. MCP server, semantic extract, paid search backends (Tavily/Brave/Kagi), YouTube transcripts, daemon mode. See [PLAN.md](PLAN.md).

## License

MIT.
