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

from vasco import browser as browser_mod
from vasco import fetch as fetch_mod
from vasco.cache import Cache


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
            fetch_mod.fetch_one(
                "https://example.com/y", cache=cache, deadline=10.0
            )
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
            fetch_mod.fetch_one(
                "https://example.com/y", cache=cache, deadline=10.0
            )
        )
        assert third["from_cache"] is True
        assert "second" in (third.get("markdown") or "").lower()
    finally:
        cache.close()
