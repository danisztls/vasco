"""MCP server for Vasco — exposes search, fetch, fetch_many, extract, map,
normalize over stdio.

Designed for long-lived processes (Claude Desktop, Claude Code): the
``BrowserPool`` singleton stays warm across calls and the semantic ranker
model loads once on first use.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from vasco import browser as _browser
from vasco import cache as _cache_mod
from vasco import config as _config
from vasco import extract as _extract_mod
from vasco import fetch as _fetch
from vasco import map as _map_mod
from vasco import search as _search

log = logging.getLogger("vasco.mcp")

# Process-singleton state populated by the lifespan handler. Tools read these
# directly — stdio MCP is single-process, so plain module globals are sufficient
# and clearer than threading context through every tool.
_cache: _cache_mod.Cache | None = None
_cfg: _config.Config | None = None


@asynccontextmanager
async def _lifespan(_server: FastMCP):  # type: ignore[no-untyped-def]
    """Open the cache at server start; close the browser pool and cache on shutdown."""
    global _cache, _cfg
    _cfg = _config.load_config()
    _cache = _cache_mod.Cache(_cfg.cache.path or None)
    log.info("vasco MCP server ready")
    try:
        yield {"cfg": _cfg, "cache": _cache}
    finally:
        log.info("vasco MCP server stopping")
        try:
            await _browser.get_browser(_cfg).close()
        except Exception:
            pass
        if _cache is not None:
            _cache.close()
        _cache = None
        _cfg = None


server = FastMCP(
    "vasco",
    instructions=(
        "Vasco: web research primitives for agents. "
        "search the web, fetch URLs (with Markdown + metadata envelope), "
        "extract passages by query, and map URLs on a site."
    ),
    lifespan=_lifespan,
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "Search the web. Returns a list of {title, url, snippet} objects. "
        "Supports --site filter, time range (d|w|m|y), and pluggable backends "
        "(ddg by default, tavily if TAVILY_API_KEY is configured)."
    ),
)
async def search(
    query: str,
    max_results: int = 10,
    region: str = "us-en",
    time: str | None = None,
    site: str | None = None,
    backend: str | None = None,
) -> list[dict[str, str]]:
    assert _cfg is not None  # populated by lifespan before tools run
    searcher = _search.get_searcher(backend or _cfg.search.default_backend, cfg=_cfg)
    return [
        {"title": r.title, "url": r.url, "snippet": r.snippet}
        for r in searcher.search(
            query, max_results=max_results, region=region, time=time, site=site
        )
    ]


@server.tool(
    description=(
        "Fetch a single URL and return its envelope: clean Markdown plus "
        "metadata (title, byline, published, word_count, links, etc.) or a "
        "typed failure object. YouTube URLs return a transcript; PDFs are "
        "rendered to text."
    ),
)
async def fetch(
    url: str,
    mode: str = "auto",
    deadline: float = 15.0,
    refresh: bool = False,
    raw: bool = False,
) -> dict[str, Any]:
    return await _fetch.fetch_one(
        url,
        mode=mode,
        deadline=deadline,
        refresh=refresh,
        raw=raw,
        cache=_cache,
        cfg=_cfg,
    )


@server.tool(
    description=(
        "Fetch many URLs concurrently. Returns a list of envelopes (one per "
        "URL, NOT necessarily in input order). Reuses one browser instance "
        "across the batch."
    ),
)
async def fetch_many(
    urls: list[str],
    workers: int = 4,
    mode: str = "auto",
    deadline: float = 15.0,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    async for env in _fetch.fetch_many(
        urls,
        workers=workers,
        mode=mode,
        deadline=deadline,
        refresh=refresh,
        cache=_cache,
        cfg=_cfg,
    ):
        results.append(env)
    return results


@server.tool(
    description=(
        "Fetch a URL and return the top-K passages matching a query. "
        "rank='bm25' (default) is fast and pure-Python; rank='semantic' uses "
        "sentence-transformers (requires the 'semantic' extra) and is better "
        "for paraphrased queries."
    ),
)
async def extract(
    url: str,
    query: str,
    top: int = 5,
    context_chars: int = 400,
    mode: str = "auto",
    rank: str = "bm25",
    deadline: float = 15.0,
) -> dict[str, Any]:
    return await _extract_mod.extract(
        url,
        query=query,
        top=top,
        context_chars=context_chars,
        mode=mode,
        rank=rank,
        deadline=deadline,
        cache=_cache,
        cfg=_cfg,
    )


@server.tool(
    name="map",
    description=(
        "Discover URLs on a site via sitemap, feeds, or a shallow spider. "
        "Returns a list of {url, source, lastmod} records."
    ),
)
async def map_site(
    url: str,
    source: str = "all",
    limit: int = 1000,
) -> list[dict[str, Any]]:
    # trafilatura does synchronous HTTP; offload to a thread so a slow
    # sitemap fetch doesn't block other in-flight MCP tool calls.
    return await asyncio.to_thread(
        lambda: list(_map_mod.map_site(url, source=source, limit=limit))
    )


@server.tool(
    description=(
        "Return the canonical (cache-key) form of a URL: lowercase host, "
        "sorted query params, tracking params dropped, youtu.be upgraded to "
        "youtube.com."
    ),
)
def normalize(url: str) -> str:
    return _cache_mod.normalize_url(url)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run() -> None:
    """Run the MCP server on stdio. Blocks until the client disconnects."""
    asyncio.run(server.run_stdio_async())
