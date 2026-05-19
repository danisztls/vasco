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
| `vasco/cli.py` | Typer app, TTY-aware output, parses `--deadline`/`--older-than` |
| `vasco/fetch.py` | `fetch_one` / `fetch_many`, auto-mode escalation, envelope assembly |
| `vasco/search.py` | `Searcher` protocol + `DdgsBackend`, `--site` operator |
| `vasco/extract.py` | Passage segmentation + BM25 ranking |
| `vasco/map.py` | sitemap / feeds / spider via trafilatura |
| `vasco/cache.py` | SQLite cache + URL normalization + per-domain strategy |
| `vasco/browser.py` | Camoufox singleton (`get_browser(cfg)` → `BrowserPool`) |
| `vasco/convert.py` | `html_to_markdown` (trafilatura wrapper + link extraction) |
| `vasco/pdf.py` | `pdftotext` / `pdfinfo` shell adapter |
| `vasco/bot_detect.py` | `classify(status, html, headers) -> FailureReason` (pure) |
| `vasco/errors.py` | `FailureReason` enum |
| `vasco/config.py` | `load_config()` → TOML + `VASCO_*` env vars |
| `vasco/io.py` | TTY detection, NDJSON/JSON/markdown writers, token estimator |

## Invariants

- **Fetch envelope is the contract.** Same shape across `fetch_one`, `extract`, `cache.get`. Adding/renaming a field requires updating `cache.py` columns + `_hydrate_cache_hit` + the integration test.
- **`fetch_one` never raises.** Failures are first-class output via a `failure` object whose `reason` is a `FailureReason` enum value. New failure modes go in `errors.py` first, then `bot_detect.classify` learns to produce them.
- **URL normalization is the cache key.** `cache.normalize_url` is load-bearing — changing it invalidates every cached entry. Tests in `tests/test_normalize.py` are table-driven.
- **Auto-mode escalation lives in `fetch._do_fetch_html`.** `cache.bump`'s `failure_count` tracks consecutive failures of the *preferred* mode only; non-preferred-mode bumps don't move the counter.
- **Browser is a process singleton.** `get_browser(cfg)` reads `BrowserCfg` only on first construction; subsequent calls return the same instance regardless of cfg. Reset in tests via `browser._reset_for_tests()`.
- **Config precedence**: CLI flag > `VASCO_*` env var > `~/.config/vasco/config.toml` > dataclass default. `cfg=None` is allowed everywhere internal — code falls back to defaults.

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

## Verification recipes

```bash
uv run vasco normalize "https://Example.COM:443/foo/?utm_source=x&b=2&a=1#frag"
# → https://example.com/foo?a=1&b=2

uv run vasco fetch https://example.com | jq '.from_cache, .word_count'
uv run vasco fetch https://example.com | jq '.from_cache, .cache_age_seconds'   # second call hits cache

uv run vasco fetch https://httpbin.org/status/404 | jq '.failure.reason'        # → "not_found"

VASCO_FETCH_WORKERS=7 uv run python -c "from vasco.config import load_config; print(load_config().fetch.workers)"
# → 7
```
