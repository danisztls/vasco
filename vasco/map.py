from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import Any


def _warn(message: str) -> None:
    sys.stderr.write(f"vasco map: warning: {message}\n")


def _iter_sitemap(url: str) -> Iterator[dict[str, Any]]:
    try:
        from trafilatura.sitemaps import sitemap_search
    except Exception as exc:  # pragma: no cover - import guard
        _warn(f"sitemap import failed: {exc}")
        return
    try:
        urls = sitemap_search(url, target_lang=None) or []
    except Exception as exc:
        _warn(f"sitemap discovery failed for {url}: {exc}")
        return
    for u in urls:
        if u:
            yield {"url": u, "source": "sitemap", "lastmod": None}


def _iter_feeds(url: str) -> Iterator[dict[str, Any]]:
    try:
        from trafilatura.feeds import find_feed_urls
    except Exception as exc:  # pragma: no cover - import guard
        _warn(f"feeds import failed: {exc}")
        return
    try:
        urls = find_feed_urls(url) or []
    except Exception as exc:
        _warn(f"feed discovery failed for {url}: {exc}")
        return
    for u in urls:
        if u:
            yield {"url": u, "source": "feed", "lastmod": None}


def _iter_spider(url: str, *, limit: int) -> Iterator[dict[str, Any]]:
    try:
        from trafilatura.spider import focused_crawler
    except Exception as exc:  # pragma: no cover - import guard
        _warn(f"spider import failed: {exc}")
        return
    try:
        result = focused_crawler(
            url,
            max_seen_urls=limit,
            max_known_urls=limit * 5,
        )
    except Exception as exc:
        _warn(f"spider crawl failed for {url}: {exc}")
        return

    # focused_crawler returns (to_visit, known). Be defensive against shape drift.
    known: Any
    if isinstance(result, tuple) and len(result) >= 2:
        known = result[1]
    else:
        known = result

    if known is None:
        return
    try:
        iterable = list(known)
    except TypeError:
        return
    for u in iterable:
        if u:
            yield {"url": u, "source": "spider", "lastmod": None}


def map_site(
    url: str,
    *,
    source: str = "all",
    limit: int = 1000,
    exclude: list[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Discover URLs on a site via sitemap, feeds, and/or a light spider.

    Yields ``{"url": str, "source": str, "lastmod": str | None}``. When
    ``source="all"`` the three discovery paths are merged and deduplicated by
    URL (first-seen source wins). At most ``limit`` records are yielded.

    ``exclude`` is a list of substring patterns; any URL containing one of
    them is filtered out. Matching is case-sensitive against the full URL.
    """
    if limit <= 0:
        return

    sources = {"sitemap", "feeds", "spider"} if source == "all" else {source}
    seen: set[str] = set()
    emitted = 0
    patterns = tuple(p for p in (exclude or ()) if p)

    streams: list[Iterator[dict[str, Any]]] = []
    if "sitemap" in sources:
        streams.append(_iter_sitemap(url))
    if "feeds" in sources:
        streams.append(_iter_feeds(url))
    if "spider" in sources:
        streams.append(_iter_spider(url, limit=limit))

    for stream in streams:
        for record in stream:
            u = record.get("url")
            if not u or u in seen:
                continue
            seen.add(u)
            if patterns and any(p in u for p in patterns):
                continue
            yield record
            emitted += 1
            if emitted >= limit:
                return
