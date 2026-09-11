# Vasco

Web research primitives for LLM agents — search, fetch, extract, answer, map — behind one stable JSON contract.

LLM agents don't have a native browser. Search APIs cost money or hit anti-bot walls. Raw HTML is the wrong shape for a context window. Vasco bundles the primitives an agent actually needs: one envelope shape for every page, typed failures instead of exceptions, a persistent cache, and an escalation chain that quietly upgrades from plain HTTP to a stealth browser only when a site demands it.

Ships three ways: a **CLI**, an **MCP server**, and **vascod** — a resident daemon that lets every consumer on the machine share one cache, one browser, and one rate limiter.

## Architecture

Three processes, each independently useful:

```
  CLI  ──────────────────────────┐  (in-process: daemon-free ground truth)
                                 │
  MCP server  ──┐                ├──► fetch pipeline ──► browser-server
  other clients ┴──► vascod ─────┘      cache (sqlite)     (Camoufox)
                    UNIX socket
                    single-flight
                    rate limiting
```

- **`vasco <cmd>`** — the CLI runs the pipeline **in-process**, so it stays the daemon-free debug path. It still shares the cache (same SQLite file).
- **`vasco serve`** (_vascod_) — resident daemon owning one `Config` + one `Cache`, serving the whole API over `$XDG_RUNTIME_DIR/vasco/vascod.sock`. Adds cross-consumer **single-flight dedup** and **per-domain rate limiting** — only meaningful because every consumer funnels through one process. MCP routes through it, falling back in-process when it isn't running.
- **`vasco browser-server`** — owns the Camoufox process. A **peer** service, not a child: vascod is just a client of it, so a browser crash can't take the daemon down. Camoufox lives in the `[browser]` extra and is imported nowhere else; when the server is down, browser-tier fetches return a typed `browser_unavailable` and the chain escalates past it.

## Install

```bash
git clone git@github.com:danisztls/vasco.git
cd vasco
uv sync                                # core: CLI + MCP + daemon
uv sync --extra browser                # only on the host running `vasco browser-server`
uv run python -m camoufox fetch        # one-time: download the patched Firefox
```

Optional extras: `browser` (Camoufox stealth browser tier), `semantic` (sentence-transformers ranking for `extract --rank semantic`).

External binaries, all optional and feature-scoped:

| Binary                           | Provides                                                     |
| -------------------------------- | ------------------------------------------------------------ |
| `pdftotext`, `pdfinfo` (poppler) | PDF text + metadata                                          |
| `pdftoppm` + `tesseract`         | OCR fallback for image-only (scanned) PDF pages              |
| `pandoc`                         | DOCX / EPUB / ODT / RTF → Markdown                           |
| `Xvfb`, `x11vnc`                 | opt-in headful Turnstile solving / human-in-the-loop captcha |

For a persistent deployment, see [`contrib/systemd/`](contrib/systemd/README.md) — two user units (`vascod.service`, `vasco-browser.service`).

## Commands

```
vasco search    <query>  [--max 10] [--region us-en] [--time d|w|m|y] [--site DOMAIN]
                          [--backend ddg]
vasco fetch     <url...> [--mode auto|http|browser|mobile|wayback] [--workers 4]
                          [--no-cache] [--refresh] [--deadline 30s] [--raw] [--concat]
vasco extract   <url>     --query "..." [--top 5] [--context-chars 400]
                          [--rank bm25|semantic] [--mode ...] [--deadline ...]
vasco answer    <url>    [--question "..."] [--mode ...] [--refresh] [--deadline ...]
vasco map       <url>    [--source llmstxt|sitemap|feeds|spider|all] [--limit 1000]
                          [--exclude SUBSTR]...
vasco normalize <url>
vasco cache     list | purge [--older-than 7d] [--domain DOMAIN] | stats
vasco logs      stats [--days 1]
vasco config    show
vasco serve             # vascod
vasco mcp               # MCP server on stdio
vasco browser-server    # Camoufox browser tier
vasco browser-solve <url>   # hold a page open for a manual captcha solve over VNC
```

Every content command takes `--human/-H` (force pretty output even when piped) and `--json` (force machine output even on a terminal). By default output **auto-detects the TTY**: rich rendering on a terminal, JSON/NDJSON when piped. Machine output is byte-for-byte identical either way.

### Quick tour

```bash
# Search returns title/url/snippet records
uv run vasco search "rust async runtimes" --max 5 --json | jq .

# Fetch: TTY gets rendered markdown, pipe gets the JSON envelope
uv run vasco fetch https://example.com
uv run vasco fetch https://example.com | jq '.title, .word_count, .from_cache'

# Bot-protected sites auto-escalate: http → browser → browser+mobile → wayback
uv run vasco fetch https://www.g2.com/products/notion/reviews | jq '.mode_used, .escalated_from, .failure'

# Per-fetch phase timing rides on the envelope
uv run vasco fetch https://example.com | jq '.duration_ms, .network_ms, .parse_ms, .attempts'

# Batch streams NDJSON as pages complete
uv run vasco fetch https://example.com https://news.ycombinator.com

# PDFs go through pdftotext; scanned pages fall back to OCR
uv run vasco fetch https://arxiv.org/pdf/2410.10934.pdf | jq '.mode_used, .word_count, .warnings'

# Extract returns the top BM25 passages for a query — read a slice, not a page
uv run vasco extract https://en.wikipedia.org/wiki/Device_fingerprint \
  --query "canvas font fingerprinting" --top 3

# Answer fetches, then asks an LLM about the page (needs an answer provider configured)
uv run vasco answer https://example.com -q "what is this domain for?"

# Map discovers URLs on a site; --exclude drops noise paths
uv run vasco map https://adrien.barbaresi.eu --source sitemap --limit 50 \
  --exclude /tag/ --exclude /author/

# URL normalization is exposed (and is the cache key)
uv run vasco normalize "https://Example.COM:443/foo/?utm_source=x&b=2&a=1#frag"
# → https://example.com/foo?a=1&b=2
uv run vasco normalize "https://l.facebook.com/l.php?u=https%3A%2F%2Fyoutu.be%2FdQw4w9WgXcQ"
# → https://youtube.com/watch?v=dQw4w9WgXcQ   (wrapper unwrapped, then canonicalized)
```

## Output contract

Every successful `fetch` — cache hit or miss, HTTP tier or browser tier or adapter — returns the same envelope:

```json
{
  "url_requested": "https://example.com",
  "url_final": "https://example.com",
  "url_canonical": "https://example.com",
  "http_status": 200,
  "mode_used": "http",
  "fetched_at": 1788924329,
  "from_cache": false,
  "cache_age_seconds": 0,
  "content_type": "text/html",
  "title": "Example Domain",
  "byline": null,
  "published": null,
  "modified": null,
  "language": null,
  "site_name": null,
  "image": null,
  "word_count": 28,
  "token_count_estimate": 44,
  "quality": {
    "trafilatura_confidence": 0.035,
    "boilerplate_ratio": 0.0,
    "domain_flagged": false,
    "paywalled": false,
    "paywall_vendor": null,
    "slop_score": 0.5,
    "signals": {
      "slop_vocab_ratio": 0.0,
      "sentence_length_cv": 0.649,
      "...": "..."
    }
  },
  "markdown": "...",
  "duration_ms": 3208,
  "network_ms": 2534,
  "parse_ms": 445,
  "cache_write_ms": 4,
  "attempts": 1,
  "escalated_from": null,
  "warnings": []
}
```

The shape lives in exactly one place (`vasco/envelope.py`) — core fetch and every adapter build through it.

Phase fields (`network_ms`, `parse_ms`, `cache_write_ms`, `attempts`, `escalated_from`) are populated on real fetches; cache hits and short-circuits stamp only `duration_ms`. `escalated_from` is set when auto-mode started in one tier and finished in another.

**Failures are values, not exceptions.** `fetch_one` never raises; the success-only fields are replaced by a typed `failure` object:

```json
{
  "url_requested": "...",
  "http_status": 403,
  "mode_used": "browser",
  "failure": {
    "reason": "blocked_cloudflare",
    "retry_after_seconds": null,
    "message": "blocked_cloudflare after browser tier"
  },
  "markdown": "",
  "warnings": []
}
```

`reason` is a closed enum:

| Reason                                                          | Meaning                                                                                                        |
| --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `blocked_cloudflare` / `blocked_captcha` / `blocked_bot`        | anti-bot walls; `blocked_bot` is a tear-down that killed the browser session                                   |
| `paywall_hard` / `paywall_soft_with_partial` / `login_required` | access walls; the soft variant still carries partial `markdown`                                                |
| `not_found` / `server_error` / `dns_fail` / `invalid_url`       | plain upstream/input errors                                                                                    |
| `timeout` / `deadline_exceeded`                                 | tier cap vs. the global kill-switch                                                                            |
| `robots_disallow` / `unsupported_content_type`                  | refused by policy / by content class (images, media, archives)                                                 |
| `js_app_needs_interaction` / `empty_body`                       | rendered to nothing — `empty_body` is the post-conversion verdict (`word_count == 0`) after every content tier |
| `parse_failed` / `category_landing`                             | adapter-produced: scraper-rot vs. a nav hub with nothing to extract                                            |
| `browser_unavailable`                                           | the browser server wasn't reachable                                                                            |

Negative caching is **per reason**: permanent failures (`not_found`, `robots_disallow`, `invalid_url`) cache for ~24h; transient ones (`timeout`, `server_error`, `parse_failed`, `browser_unavailable`) for ~5min, so a fixed adapter or a restarted service heals fast.

## The escalation chain

Auto mode walks `http → browser → browser+mobile → wayback`, stopping at the first tier that yields content.

Each tier has its own **wall-clock cap** — http 5s, browser 12s, mobile 5s, wayback 6s — and those are the primary budget (~28s for a full run). The `--deadline` (default 30s) is a hard kill-switch, not the timing you feel in practice.

What makes it cheap in the common case:

- **Learned starting tier, per route.** Strategy is keyed by `registered_domain + first path segment`, so `vivareal.com.br/aluguel/*` and `/imovel/*` learn independently. Declarative seeds in `vasco/strategy.py` cover known browser-only sites; learning overrides them once a row exists.
- **Adaptive header profile.** The http tier defaults to a full modern-Chrome header shape, and falls back **once** to a minimal "honest" client set when a WAF rejects the half-fingerprint (a "Chrome" UA without the rest of the suite reads as a headless bot). The working profile is persisted per route.
- **Escalation is decided post-conversion.** Whether a page rendered is judged on trafilatura's `word_count`, not on raw-HTML heuristics — those both over- and under-fire. Plain text, PDFs, office docs and binary blobs each take their own path _before_ that, so a `.md` file never pointlessly escalates and an image fails fast instead of timing out in the browser.
- **Wayback is opt-out.** Content adapters skip the archive tail: they parse live structured data, so a stale snapshot breaks their anchor. They return the honest block instead.

Cache lives at `$XDG_CACHE_HOME/vasco/cache.db`, keyed by `urls.normalize_url` — which lowercases, sorts params, strips a curated tracking-param denylist, folds AMP variants, collapses every YouTube URL form to `youtube.com/watch?v=<id>`, and unwraps known redirect wrappers (Facebook `l.php`, Google `/url?q=`, …) _before_ the rest, so a wrapped link is fetched and cached under its true key.

## Content adapters

Some pages aren't articles. For those, Vasco routes to a source-specific adapter that parses a structural anchor into normalized data in `quality.*` instead of prose:

- **Media / reference**: YouTube (transcripts, plus channel/playlist/search listings), Wikimedia (Enterprise API), Scholar (DOI / PII / PubMed / arXiv via open scholarly APIs, with OA full-text pull)
- **Marketplaces**: MercadoLivre, Amazon BR, AliExpress, Shopee BR, Petlove, OLX, Google Shopping, generic **Shopify** (any store, via platform JSON endpoints)
- **Real estate**: vivareal
- **Dev trackers**: GitLab (public `/api/v4`), Phabricator/Phorge (public tasks)
- **Enrichment**: Steam store pages gain IsThereAnyDeal historical pricing when a key is configured

The contract that keeps them honest: **anchor missing → `parse_failed`** (scraper-rot, short TTL, shows up in telemetry); **anchor present but empty → `success` + a `no_results` warning** (a genuinely empty search). A detail page parsing to zero items is always rot. That's what stops a site markup change from silently caching `result_count: 0` forever.

Unknown-domain adapters (Shopify, self-hosted GitLab) **probe once** and persist the verdict in the cache, and a miss falls through to a normal fetch — a lookalike must never become a failure.

Full per-adapter detail: **[docs/adapters.md](docs/adapters.md)**.

## Quality scoring

Every converted page gets a `quality` block, in two layers:

1. **Domain blocklist** — community-curated lists (uBlacklist, plain-domain, and `0.0.0.0` hosts formats), local files or remote URLs, consolidated with a 7-day refresh.
2. **Text heuristics + metadata signals** — slop vocabulary, sentence-length CV, em-dash density, transition-word starts, type-token ratio, plus boilerplate ratio and missing byline/date.

They compose into `slop_score` (0–1, higher is worse; 15% text / 85% metadata, calibrated in `research/calibrate_quality.py`), and the raw `signals` dict is exposed so consumers can apply their own thresholds. Paywall-vendor fingerprinting sets `paywalled` / `paywall_vendor`. Disable the whole thing with `quality: false` or `VASCO_QUALITY_ENABLED=false`.

## MCP server

`vasco mcp` runs on stdio, exposing `search`, `fetch`, `fetch_many`, `extract`, `answer`, and `map`. Every tool routes through vascod when it's reachable and falls back in-process otherwise.

Two things it does specifically for agents:

- **`fetch_many` defaults to `metadata_only=true`.** Batch fan-outs return triage envelopes with no `markdown`, so an agent picks what to read instead of dumping N pages into context. Re-fetching a chosen URL is a cache hit — near-free. `fetch` accepts the same flag for large pages.
- **`answer` returns a short LLM answer over a page** instead of its full text, for when you only need what a page _says_ about something.

Browser prewarm is opt-in (`VASCO_BROWSER_PREWARM=true` or `browser.prewarm: true`); it just establishes the socket connection early, and failures are swallowed.

> **Keys must be in config, not the environment.** Because tools route through vascod, key-dependent ops (the `answer` LLM call, the ITAD enrichment) run in the _daemon's_ process, which doesn't inherit your shell's env. Set `answer.providers[].api_key` / `adapters.steam.itad_api_key` in `config.yaml` and restart `vascod.service`. `VASCO_*` env vars only reach in-process runs (i.e. the CLI).

## Config

`~/.config/vasco/config.yaml` — every section is optional. [`config.yaml.template`](config.yaml.template) documents every key with its default:

```yaml
fetch:
  workers: 4
  deadline_seconds: 30
  ttl_seconds: 86400
  failure_ttl_seconds: 900

browser: # all resolved server-side by `vasco browser-server`
  headless: true
  user_data_dir: "" # non-empty path → persistent Camoufox profile
  block_ads: true # abort third-party ad/tracker requests
  clear_cookies_on_wall: true # on a login wall, clear that domain's cookies + retry

service: # vascod coordination
  single_flight: true
  rate_limit_rps: 1.0
  max_concurrent_per_domain: 2

answer: # ordered chain: first = primary, rest are fallbacks
  providers:
    - { provider: claude_cli, model: sonnet, effort: low }
    - { provider: deepseek, model: deepseek-flash, api_key: sk-... }

domains: # per-host fetch overrides
  gitlab.example.com: honest # minimal client headers, for WAFs that 403 the full shape
```

Precedence: **CLI flag > `VASCO_*` env var > config file > default**. Env names are `VASCO_<SECTION>_<FIELD>` (`VASCO_FETCH_WORKERS=8`); adapter sections nest, so `VASCO_ADAPTERS_STEAM_COUNTRY=BR`. `vasco config show` prints the effective merge.

Setting `browser.user_data_dir` enables a **persistent profile** — Cloudflare clearance and login sessions survive across runs, which several adapters depend on. Vasco keeps the profile healthy: a login wall triggers a **domain-scoped** cookie clear and one retry (single-shot, cooldown-gated), so a poisoned session can't wedge a site permanently and sibling domains keep their clearances.

The `answer` command supports **`claude_cli`**, which shells out to `claude -p` and runs on your Claude Code subscription rather than an API key.

## Telemetry

CLI, MCP and daemon all append structured JSONL to `$XDG_DATA_HOME/vasco/logs/YYYY-MM-DD.jsonl` — one file per day. Each tool call emits one record with an `outcome` discriminator:

- `ok` — carries `mode_used`, `http_status`, `from_cache`, and the phase fields; content-adapter successes also carry `provider`, `page_type`, `result_count`
- `fail` — typed `failure_reason` + `message` (per URL for `fetch_many`)
- `empty` — `extract` returned zero passages (separates "bad query" from "fetch silently broke")
- `exception` — uncaught tool-level error

Writes never block a tool call; I/O errors are swallowed. Disable with `logging.enabled: false` or `VASCO_LOGGING_ENABLED=false`.

```bash
uv run vasco logs stats --days 7 | jq '.by_tool, .escalation_rate, .phase_percentiles'
uv run vasco logs stats --days 7 | jq '.adapters'   # per-provider zero_result_rate
tail -f ~/.local/share/vasco/logs/$(date -u +%F).jsonl | jq .
```

`zero_result_rate` is the fingerprint of a silently rotted adapter — still 200ing, parsing to nothing.

## Known limitations

- **Tables rendered via MathJax / CSS come out hollow.** Pages like the arXiv HTML view encode numeric cells through scripts that trafilatura (and most plain-HTML extractors) strip. Surrounding prose survives; the table becomes a skeleton with empty cells. Workaround: fetch the PDF version, which preserves tabular data via `pdftotext`.
- **Large pages can overflow downstream context windows.** A 10k-word article yields ~80 KB of markdown. Use `extract` for query-targeted passages, `answer` for a summary, or `metadata_only=true` for triage.
- **The stealth tier is best-effort.** Some sites (AliExpress's `baxia` stack, notably) only work with a warm persistent profile holding an earned clearance token. Vasco reports the honest block rather than pretending otherwise.
- **Several marketplace adapters are Brazil-scoped.** Amazon, Shopee, MercadoLivre, Petlove and OLX parse the pt-BR storefronts; other-country hosts deliberately fall through to a normal fetch instead of mis-parsing.

## Development

```bash
uv sync --group dev
uv run --group dev pytest -q
uv run ruff check . && uv run ruff format .
```

`.git/hooks/pre-commit` runs `ruff format` + `ruff check --fix` on staged files and the full test suite, and aborts the commit on failure. Architecture notes, module map and invariants live in [`CLAUDE.md`](CLAUDE.md).

## License

Copyright (C) 2026 Daniel de Souza.

Vasco is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. See [LICENSE](LICENSE).

The AGPL's §13 network clause applies: if you run a modified version as a service others interact with over a network, those users must be offered its source.
