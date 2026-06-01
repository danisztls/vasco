"""End-to-end fetch test using a real Cache (sqlite in tmp_path) but a stubbed
HTTP tier. Covers the seam between fetch_one, Cache, and url_requested
preservation — the exact gap that let the dead-CLI-cache bug slip through
unit testing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from vasco.fetch import browser as browser_mod
from vasco import fetch as fetch_mod
from vasco.adapters import youtube as youtube_mod
from vasco.cache import Cache


# A year-2100 epoch second — used as fetched_at in faked envelopes so the
# cache's TTL (current_time + 86400) is always far in the future, making
# tests independent of the wall clock at run time.
_FAR_FUTURE_FETCHED_AT = 4_102_444_800

FIXTURES = Path(__file__).parent / "fixtures"


def _disable_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NopPool:
        async def fetch(self, *a: Any, **kw: Any) -> tuple[str, int, dict[str, str]]:
            return "", 0, {}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(browser_mod, "_pool", None, raising=False)
    monkeypatch.setattr(browser_mod, "get_browser", lambda cfg=None: _NopPool())


def _stub_http(html: str, status: int = 200, headers: dict | None = None):
    async def _fake(
        url: str, *, deadline_monotonic: float, cfg: Any | None = None
    ) -> tuple[str, int, dict[str, str]]:
        hdrs = dict(headers or {})
        hdrs.setdefault("_url_final", url)
        return html, status, hdrs

    return _fake


def test_fetch_one_round_trips_through_real_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two fetches of the same URL: first miss, second hit, with markdown preserved."""
    html = (FIXTURES / "article_clean.html").read_text(encoding="utf-8")
    monkeypatch.setattr(fetch_mod, "_http_fetch", _stub_http(html, 200))
    _disable_browser(monkeypatch)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        first = asyncio.run(
            fetch_mod.fetch_one(
                "https://Example.COM/article",
                cache=cache,
                deadline=10.0,
            )
        )
        assert first["from_cache"] is False
        assert first["http_status"] == 200
        assert first["markdown"], "expected extracted markdown"
        assert "failure" not in first

        second = asyncio.run(
            fetch_mod.fetch_one(
                "https://Example.COM/article",
                cache=cache,
                deadline=10.0,
            )
        )
        assert second["from_cache"] is True
        assert second["markdown"] == first["markdown"]
        assert second["title"] == first["title"]
    finally:
        cache.close()




def test_cache_hit_preserves_caller_url_casing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cached envelope's url_requested should reflect what the caller passed,
    not the normalized cache key.
    """
    html = (FIXTURES / "article_clean.html").read_text(encoding="utf-8")
    monkeypatch.setattr(fetch_mod, "_http_fetch", _stub_http(html, 200))
    _disable_browser(monkeypatch)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        # Seed: normalized form gets written.
        asyncio.run(
            fetch_mod.fetch_one(
                "https://Example.COM/foo?b=2&a=1",
                cache=cache,
                deadline=10.0,
            )
        )
        # Hit: a differently-cased equivalent URL should still get a hit,
        # but the envelope must echo the caller's exact input.
        hit = asyncio.run(
            fetch_mod.fetch_one(
                "HTTPS://example.com/foo?a=1&b=2",
                cache=cache,
                deadline=10.0,
            )
        )
        assert hit["from_cache"] is True
        assert hit["url_requested"] == "HTTPS://example.com/foo?a=1&b=2"
    finally:
        cache.close()


def test_no_cache_flag_bypasses_reads_and_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = (FIXTURES / "article_clean.html").read_text(encoding="utf-8")
    monkeypatch.setattr(fetch_mod, "_http_fetch", _stub_http(html, 200))
    _disable_browser(monkeypatch)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        asyncio.run(
            fetch_mod.fetch_one(
                "https://example.com/x",
                cache=cache,
                use_cache=False,
                deadline=10.0,
            )
        )
        assert cache.stats()["entries"] == 0
    finally:
        cache.close()


def test_youtube_url_routes_to_youtube_fetcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A youtube.com URL bypasses HTTP/browser tier and yields a transcript envelope.
    Both youtu.be and youtube.com forms should hit the same cache row.
    """
    calls: list[str] = []

    async def fake_fetch_youtube(url: str, *, deadline: float, cfg: Any = None) -> dict:
        calls.append(url)
        return {
            "url_requested": url,
            "url_final": url,
            "url_canonical": "https://youtube.com/watch?v=abc123",
            "http_status": 200,
            "mode_used": "youtube",
            "fetched_at": _FAR_FUTURE_FETCHED_AT,
            "from_cache": False,
            "cache_age_seconds": 0,
            "content_type": "text/youtube",
            "title": "T",
            "byline": "B",
            "published": None,
            "modified": None,
            "language": "en",
            "site_name": "YouTube",
            "word_count": 2,
            "token_count_estimate": 2,
            "quality": {},
            "links": [],
            "markdown": "hello world",
            "warnings": [],
        }

    # HTTP tier must NOT be hit for YouTube URLs.
    def explode(*a: Any, **kw: Any) -> Any:
        raise AssertionError("HTTP tier should not run for YouTube URLs")

    monkeypatch.setattr(youtube_mod, "fetch_youtube", fake_fetch_youtube)
    monkeypatch.setattr(fetch_mod, "_http_fetch", explode)
    _disable_browser(monkeypatch)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        first = asyncio.run(
            fetch_mod.fetch_one("https://youtu.be/abc123", cache=cache, deadline=10.0)
        )
        assert first["mode_used"] == "youtube"
        assert first["markdown"] == "hello world"
        assert first["from_cache"] is False
        assert len(calls) == 1

        # Canonical youtube.com form should hit the same cache row (youtu.be
        # was upgraded to this exact shape by normalize_url).
        second = asyncio.run(
            fetch_mod.fetch_one(
                "https://youtube.com/watch?v=abc123",
                cache=cache,
                deadline=10.0,
            )
        )
        assert second["from_cache"] is True
        assert second["mode_used"] == "youtube"
        # youtube.fetch_youtube should NOT have been called again.
        assert len(calls) == 1
    finally:
        cache.close()


def test_youtube_raw_flag_adds_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--raw is meaningless for YouTube (no HTML); we surface a warning so
    callers can detect the mismatch instead of silently dropping the flag.
    """

    async def fake_fetch_youtube(url: str, *, deadline: float, cfg: Any = None) -> dict:
        return {
            "url_requested": url,
            "url_final": url,
            "url_canonical": url,
            "http_status": 200,
            "mode_used": "youtube",
            "fetched_at": _FAR_FUTURE_FETCHED_AT,
            "from_cache": False,
            "cache_age_seconds": 0,
            "content_type": "text/youtube",
            "title": "T",
            "byline": None,
            "published": None,
            "modified": None,
            "language": "en",
            "site_name": "YouTube",
            "word_count": 1,
            "token_count_estimate": 1,
            "quality": {},
            "links": [],
            "markdown": "hi",
            "warnings": [],
        }

    monkeypatch.setattr(youtube_mod, "fetch_youtube", fake_fetch_youtube)
    _disable_browser(monkeypatch)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        env = asyncio.run(
            fetch_mod.fetch_one(
                "https://youtu.be/abc123", cache=cache, deadline=10.0, raw=True
            )
        )
        assert "raw_unsupported_for_youtube" in env["warnings"]
    finally:
        cache.close()


def test_refresh_flag_writes_but_ignores_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html_first = "<html><body>" + "first " * 200 + "</body></html>"
    html_second = "<html><body>" + "second " * 200 + "</body></html>"

    monkeypatch.setattr(fetch_mod, "_http_fetch", _stub_http(html_first, 200))
    _disable_browser(monkeypatch)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        first = asyncio.run(
            fetch_mod.fetch_one("https://example.com/y", cache=cache, deadline=10.0)
        )
        assert first["from_cache"] is False

        # Swap the upstream response and refetch with --refresh.
        monkeypatch.setattr(fetch_mod, "_http_fetch", _stub_http(html_second, 200))
        second = asyncio.run(
            fetch_mod.fetch_one(
                "https://example.com/y",
                cache=cache,
                refresh=True,
                deadline=10.0,
            )
        )
        # refresh ignores cached read → we should see the new content.
        assert second["from_cache"] is False
        assert "second" in (second.get("markdown") or "").lower()

        # And the cache should now hold the refreshed body.
        third = asyncio.run(
            fetch_mod.fetch_one("https://example.com/y", cache=cache, deadline=10.0)
        )
        assert third["from_cache"] is True
        assert "second" in (third.get("markdown") or "").lower()
    finally:
        cache.close()


def test_browser_tier_unavailable_degrades_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no browser server running, a browser-tier fetch fails as
    BROWSER_UNAVAILABLE rather than raising — the real BrowserPool is used
    (not the NopPool stub), pointed at a socket that doesn't exist."""
    monkeypatch.setattr(
        browser_mod, "_socket_path", lambda: str(tmp_path / "no-server.sock")
    )
    monkeypatch.setattr(browser_mod, "_pool", None, raising=False)

    env = asyncio.run(
        fetch_mod.fetch_one(
            "https://example.com", mode="browser", use_cache=False, deadline=10.0
        )
    )
    assert "failure" in env
    assert env["failure"]["reason"] == "browser_unavailable"
