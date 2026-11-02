# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from vasco import telemetry
from vasco.quality import smuggling

_MAX_BODY_BYTES = 512 * 1024
_DISK_TTL_SECONDS = 86400  # 24 h
_SPIDER_STEP_PAGES = 5
_SITEMAP_GUESSES = ("sitemap.xml", "sitemap_index.xml", "wp-sitemap.xml")
NOTICE_SOURCE = "notice"


@dataclass(frozen=True)
class _Size:
    limit: int
    budget: float  # seconds of wall clock across all sources
    sources: tuple[str, ...]  # what source="all" covers
    spider_pages: int
    max_sitemaps: int
    max_content_chars: int | None  # llms.txt body


_ALL_SOURCES = ("llmstxt", "sitemap", "feeds", "spider")
_SIZES = {
    "quick": _Size(50, 15.0, ("llmstxt", "sitemap"), 3, 3, 4_000),
    "standard": _Size(200, 60.0, _ALL_SOURCES, 10, 20, 20_000),
    "full": _Size(1000, 300.0, _ALL_SOURCES, 1000, 10_000, None),
}


def _size(name: str) -> _Size:
    try:
        return _SIZES[name]
    except KeyError:
        raise ValueError(f"unknown map size: {name!r}") from None


def effective_budget(size: str, budget: float | None) -> float:
    """Seconds a `map_site` call may run: `budget`, or the size's default."""
    return _size(size).budget if budget is None else budget


class _BudgetExceeded(Exception):
    pass


def _until(
    stream: Iterator[dict[str, Any]], deadline: float
) -> Iterator[dict[str, Any]]:
    """Records from `stream` until `deadline`, then raises `_BudgetExceeded`.

    The discovery libraries do blocking HTTP, so the stream runs in a thread that
    can only be abandoned. It stays bounded on its own: the sitemap walk by
    `max_sitemaps`, the spider by its deadline.
    """
    items: queue.SimpleQueue[tuple[dict[str, Any] | None, Exception | None]] = (
        queue.SimpleQueue()
    )

    def pump() -> None:
        try:
            for record in stream:
                items.put((record, None))
            items.put((None, None))
        except Exception as exc:
            items.put((None, exc))

    threading.Thread(target=pump, daemon=True).start()
    while True:
        try:
            record, exc = items.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            raise _BudgetExceeded from None
        if exc is not None:
            raise exc
        if record is None:
            return
        yield record


def _notice(url: str, note: str) -> dict[str, Any]:
    return {"url": url, "source": NOTICE_SOURCE, "lastmod": None, "note": note}


def _looks_like_html(text: str) -> bool:
    return text.lstrip()[:15].lower().startswith(("<!doctype html", "<html"))


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
                cached = cache_path.read_text(encoding="utf-8")
                if not _looks_like_html(cached):
                    return cached, llmstxt_url
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
    # A site that serves its homepage for unknown paths isn't offering an llms.txt.
    if _looks_like_html(content):
        _warn(f"llms.txt is an HTML page for {llmstxt_url}")
        return None, None

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(content, encoding="utf-8")
    except OSError:
        pass

    return content, llmstxt_url


def _guard_llmstxt(content: str, url: str | None, cfg: Any | None) -> str:
    """Strip an encoded prompt payload out of an llms.txt body.

    llms.txt is *written for* LLMs and is the one map source whose full text —
    not just a URL — is handed to the agent, which makes it the highest-value
    place on this path to smuggle instructions. On a hit the body is replaced
    outright rather than partially redacted: unlike a search snippet there is no
    residual value in a mangled llms.txt, and the caller still gets the record's
    URL. The decoded payload goes only to the log.
    """
    if not smuggling.enabled(cfg):
        return content
    hits = smuggling.scan_text(content, field="content")
    if not hits:
        return content
    telemetry.record_smuggling(
        cfg,
        "map",
        url=url,
        payloads=smuggling.log_records(hits),
        action="withheld",
    )
    kinds = ", ".join(sorted({hit.kind for hit in hits}))
    return f"[llms.txt withheld: hidden-text payload detected ({kinds})]"


def _iter_llmstxt(url: str) -> Iterator[dict[str, Any]]:
    content, llmstxt_url = _fetch_llmstxt(url)
    if content is not None:
        yield {
            "url": llmstxt_url,
            "source": "llmstxt",
            "content": content,
            "lastmod": None,
        }


def _subdirectory_sitemap_urls(
    url: str, sitemap_search: Callable[..., list[str]], max_sitemaps: int
) -> list[str]:
    """A site installed under a path keeps its robots.txt and sitemaps there, but
    trafilatura only looks at the host's root."""
    from trafilatura.sitemaps import find_robots_sitemaps

    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if not path or path.endswith((".xml", ".gz", "sitemap")):
        return []
    base = f"{parts.scheme}://{parts.netloc}{path}"
    for candidate in find_robots_sitemaps(base) or [
        f"{base}/{name}" for name in _SITEMAP_GUESSES
    ]:
        urls = sitemap_search(candidate, target_lang=None, max_sitemaps=max_sitemaps)
        if urls:
            return urls
    return []


def _iter_sitemap(url: str, *, max_sitemaps: int) -> Iterator[dict[str, Any]]:
    try:
        from trafilatura.sitemaps import sitemap_search
    except Exception as exc:  # pragma: no cover - import guard
        _warn(f"sitemap import failed: {exc}")
        return
    try:
        urls = sitemap_search(url, target_lang=None, max_sitemaps=max_sitemaps) or []
        if not urls:
            urls = _subdirectory_sitemap_urls(url, sitemap_search, max_sitemaps)
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


def _iter_spider(
    url: str, *, limit: int, pages: int, deadline: float
) -> Iterator[dict[str, Any]]:
    try:
        from trafilatura.spider import focused_crawler
    except Exception as exc:  # pragma: no cover - import guard
        _warn(f"spider import failed: {exc}")
        return
    # In steps, because the crawler sleeps between pages: this is what makes a
    # budget reachable, and results so far survive running out of it.
    todo: list[str] | None = None
    known: list[str] | None = None
    emitted: set[str] = set()
    visited = 0
    while visited < pages and time.monotonic() < deadline:
        try:
            result = focused_crawler(
                url,
                max_seen_urls=min(_SPIDER_STEP_PAGES, pages - visited),
                max_known_urls=limit * 5,
                todo=todo,
                known_links=known,
            )
        except Exception as exc:
            _warn(f"spider crawl failed for {url}: {exc}")
            return
        visited += _SPIDER_STEP_PAGES
        # focused_crawler returns (to_visit, known). Be defensive against shape drift.
        if not (isinstance(result, tuple) and len(result) >= 2):
            return
        todo, known = result[0], result[1]
        try:
            fresh = [u for u in known if u and u not in emitted]
        except TypeError:
            return
        emitted.update(fresh)
        for u in fresh:
            yield {"url": u, "source": "spider", "lastmod": None}
        if not todo or len(known) >= limit * 5:
            return


def map_site(
    url: str,
    *,
    source: str = "all",
    limit: int | None = None,
    exclude: list[str] | None = None,
    size: str = "standard",
    budget: float | None = None,
    cfg: Any | None = None,
) -> Iterator[dict[str, Any]]:
    """Discover URLs on a site via sitemap, feeds, and/or a light spider.

    Yields ``{"url": str, "source": str, "lastmod": str | None}``. When
    ``source="all"`` the three discovery paths are merged and deduplicated by
    URL (first-seen source wins). At most ``limit`` URLs are yielded, within
    ``budget`` seconds.

    ``size`` is how much to spend: ``quick``, ``standard`` or ``full``. It sets the
    default ``limit`` and ``budget``, what ``source="all"`` covers, how far the
    sitemap walk and the spider go, and how much of an llms.txt body is kept.

    When the limit or budget cuts discovery short, a final ``source="notice"``
    record says so in its ``note``.

    ``exclude`` is a list of substring patterns; any URL containing one of
    them is filtered out. Matching is case-sensitive against the full URL.

    ``cfg`` is read only for the ASCII-smuggling gate applied to body-bearing
    records (llms.txt); ``None`` means the default (enabled).
    """
    profile = _size(size)
    limit = profile.limit if limit is None else limit
    if limit <= 0:
        return
    budget = effective_budget(size, budget)
    deadline = time.monotonic() + budget

    sources = set(profile.sources) if source == "all" else {source}
    seen: set[str] = set()
    emitted = 0
    patterns = tuple(p for p in (exclude or ()) if p)

    streams: list[Iterator[dict[str, Any]]] = []
    if "llmstxt" in sources:
        streams.append(_iter_llmstxt(url))
    if "sitemap" in sources:
        streams.append(_iter_sitemap(url, max_sitemaps=profile.max_sitemaps))
    if "feeds" in sources:
        streams.append(_iter_feeds(url))
    if "spider" in sources:
        streams.append(
            _iter_spider(
                url, limit=limit, pages=profile.spider_pages, deadline=deadline
            )
        )

    for stream in streams:
        try:
            for record in _until(stream, deadline):
                u = record.get("url")
                if not u or u in seen:
                    continue
                seen.add(u)
                if patterns and any(p in u for p in patterns):
                    continue
                if emitted >= limit:
                    yield _notice(
                        url, f"stopped at the limit of {limit} URLs; more exist"
                    )
                    return
                # llms.txt is the only map source that hands the agent a *body*
                # rather than just a URL — and it is written for LLMs, which makes
                # it the highest-value place on this path to smuggle instructions.
                # Screened here, at the merge point, so any future body-bearing
                # source is covered by construction.
                if isinstance(record.get("content"), str):
                    content = _guard_llmstxt(record["content"], u, cfg)
                    record = {**record, "content": content}
                    max_chars = profile.max_content_chars
                    if max_chars is not None and len(content) > max_chars:
                        record["content"] = content[:max_chars]
                        record["content_truncated"] = True
                yield record
                emitted += 1
        except _BudgetExceeded:
            yield _notice(
                url, f"stopped at the budget of {budget:g}s; results may be partial"
            )
            return
        if emitted >= limit:
            return
