# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

_MAX_BODY_BYTES = 512 * 1024
_DISK_TTL_SECONDS = 86400  # 24 h


def _warn(message: str) -> None:
    sys.stderr.write(f"vasco map: warning: {message}\n")


def _llmstxt_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "vasco" / "llms.txt"


def _fetch_llmstxt(url: str) -> tuple[str | None, str | None]:
    """Fetch /llms.txt for the given URL's origin.

    Returns (content, llmstxt_url) or (None, None) on failure.
    Serves from on-disk cache when fresh (< 24h).
    """
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    llmstxt_url = f"{origin}/llms.txt"
    domain = parts.hostname or parts.netloc

    cache_path = _llmstxt_dir() / f"{domain}.txt"
    if cache_path.exists():
        try:
            age = time.time() - cache_path.stat().st_mtime
            if age < _DISK_TTL_SECONDS:
                return cache_path.read_text(encoding="utf-8"), llmstxt_url
        except OSError:
            pass

    try:
        resp = httpx.get(llmstxt_url, timeout=10, follow_redirects=True)
    except (httpx.HTTPError, OSError) as exc:
        _warn(f"llms.txt fetch failed for {llmstxt_url}: {exc}")
        return None, None

    if resp.status_code != 200:
        _warn(f"llms.txt returned {resp.status_code} for {llmstxt_url}")
        return None, None

    if len(resp.content) > _MAX_BODY_BYTES:
        _warn(f"llms.txt too large ({len(resp.content)} bytes) for {llmstxt_url}")
        return None, None

    content = resp.text
    if not content.strip():
        _warn(f"llms.txt is empty for {llmstxt_url}")
        return None, None

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(content, encoding="utf-8")
    except OSError:
        pass

    return content, llmstxt_url


def _iter_llmstxt(url: str) -> Iterator[dict[str, Any]]:
    content, llmstxt_url = _fetch_llmstxt(url)
    if content is not None:
        yield {
            "url": llmstxt_url,
            "source": "llmstxt",
            "content": content,
            "lastmod": None,
        }


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
    known = result[1] if isinstance(result, tuple) and len(result) >= 2 else result

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

    sources = {"llmstxt", "sitemap", "feeds", "spider"} if source == "all" else {source}
    seen: set[str] = set()
    emitted = 0
    patterns = tuple(p for p in (exclude or ()) if p)

    streams: list[Iterator[dict[str, Any]]] = []
    if "llmstxt" in sources:
        streams.append(_iter_llmstxt(url))
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
