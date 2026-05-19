"""Tests for the fetch-mode escalation state machine in `vasco.fetch.fetch_one`.

We monkeypatch the module-level helpers `_http_fetch` and `_browser_fetch`
so no network or browser is required. A minimal fake Cache stub records
`bump` calls and can preconfigure a domain strategy.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from vasco import fetch as fetch_mod
from vasco.errors import FailureReason


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


CLEAN_HTML = ""  # filled below by a fixture-loader at module import
CF_HTML = ""


def setup_module(module: Any) -> None:  # noqa: ARG001
    global CLEAN_HTML, CF_HTML
    CLEAN_HTML = _load("article_clean.html")
    CF_HTML = _load("cloudflare_challenge.html")


class FakeCache:
    """Minimal Cache stub: get, put, get_domain_strategy, bump, normalize_url."""

    def __init__(self, *, strategy: str | None = None) -> None:
        self.store: dict[str, dict] = {}
        self.bumps: list[dict] = []
        self.preferred = strategy

    def normalize_url(self, url: str) -> str:
        return url

    def registered_domain(self, url: str) -> str:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host

    def get(self, url: str) -> dict | None:
        return self.store.get(url)

    def put(self, envelope: dict, *, ttl_seconds: int) -> None:
        self.store[envelope.get("url_canonical") or envelope["url_requested"]] = envelope

    def get_domain_strategy(self, domain: str) -> str | None:
        return self.preferred

    def bump(self, domain: str, *, mode: str, success: bool) -> None:
        self.bumps.append({"domain": domain, "mode": mode, "success": success})


def _make_http(html: str, status: int = 200, headers: dict | None = None):
    async def _fake_http(
        url: str, *, deadline_monotonic: float, cfg: Any | None = None
    ) -> tuple[str, int, dict[str, str]]:
        return html, status, dict(headers or {})

    return _fake_http


def _make_browser(html: str, status: int = 200, headers: dict | None = None):
    async def _fake_browser(
        url: str, *, deadline_monotonic: float, cfg: Any | None = None
    ) -> tuple[str, int, dict[str, str]]:
        return html, status, dict(headers or {})

    return _fake_browser


def _disable_browser_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the real Camoufox singleton close in `fetch_one` finally blocks."""
    from vasco import browser as browser_mod

    class _NopPool:
        async def fetch(self, *a: Any, **kw: Any) -> tuple[str, int, dict[str, str]]:
            return "", 0, {}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(browser_mod, "_pool", None, raising=False)
    monkeypatch.setattr(browser_mod, "get_browser", lambda cfg=None: _NopPool())


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# -----------------------------------------------------------------------------
# Test cases
# -----------------------------------------------------------------------------


def test_unknown_domain_http_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case 1: Unknown domain, http returns OK → mode_used=http, bump http/True."""
    cache = FakeCache()
    monkeypatch.setattr(fetch_mod, "_http_fetch", _make_http(CLEAN_HTML, 200))
    monkeypatch.setattr(
        fetch_mod, "_browser_fetch", _make_browser("should not be called", 0)
    )
    _disable_browser_close(monkeypatch)

    env = run(
        fetch_mod.fetch_one(
            "https://example.com/article",
            cache=cache,
            use_cache=False,
            deadline=10.0,
        )
    )
    assert env["mode_used"] == "http"
    assert "failure" not in env
    assert cache.bumps == [
        {"domain": "example.com", "mode": "http", "success": True}
    ]


def test_http_cloudflare_escalates_to_browser_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 2: http → CF block → browser succeeds. mode_used=browser, bump browser/True."""
    cache = FakeCache()
    monkeypatch.setattr(fetch_mod, "_http_fetch", _make_http(CF_HTML, 200))
    monkeypatch.setattr(
        fetch_mod, "_browser_fetch", _make_browser(CLEAN_HTML, 200)
    )
    _disable_browser_close(monkeypatch)

    env = run(
        fetch_mod.fetch_one(
            "https://blocked.example.com/page",
            cache=cache,
            use_cache=False,
            deadline=30.0,
        )
    )
    assert env["mode_used"] == "browser"
    assert "failure" not in env
    # `registered_domain` returns the eTLD+1; here that's example.com.
    assert len(cache.bumps) == 1
    assert cache.bumps[0]["mode"] == "browser"
    assert cache.bumps[0]["success"] is True


def test_preferred_browser_skips_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case 3: Domain strategy = browser → skip http entirely."""
    cache = FakeCache(strategy="browser")

    http_calls: list[str] = []

    async def _http_should_not_be_called(
        url: str, *, deadline_monotonic: float, cfg: Any | None = None
    ) -> tuple[str, int, dict[str, str]]:
        http_calls.append(url)
        return "", 0, {}

    monkeypatch.setattr(fetch_mod, "_http_fetch", _http_should_not_be_called)
    monkeypatch.setattr(
        fetch_mod, "_browser_fetch", _make_browser(CLEAN_HTML, 200)
    )
    _disable_browser_close(monkeypatch)

    env = run(
        fetch_mod.fetch_one(
            "https://heavy.example.com/spa",
            cache=cache,
            use_cache=False,
            deadline=20.0,
        )
    )
    assert env["mode_used"] == "browser"
    assert http_calls == []
    assert len(cache.bumps) == 1
    assert cache.bumps[0]["mode"] == "browser"
    assert cache.bumps[0]["success"] is True


def test_deadline_exceeded_before_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 4: http blocked, but <BROWSER_MIN_BUDGET remaining → DEADLINE_EXCEEDED."""
    cache = FakeCache()

    async def _slow_http(
        url: str, *, deadline_monotonic: float, cfg: Any | None = None
    ) -> tuple[str, int, dict[str, str]]:
        # Burn the budget so the remaining time falls below BROWSER_MIN_BUDGET.
        burn = max(
            0.0,
            (deadline_monotonic - time.monotonic())
            - (fetch_mod.BROWSER_MIN_BUDGET / 2.0),
        )
        await asyncio.sleep(burn)
        return CF_HTML, 200, {}

    browser_called: list[str] = []

    async def _browser_should_not_be_called(
        url: str, *, deadline_monotonic: float, cfg: Any | None = None
    ) -> tuple[str, int, dict[str, str]]:
        browser_called.append(url)
        return "", 0, {}

    monkeypatch.setattr(fetch_mod, "_http_fetch", _slow_http)
    monkeypatch.setattr(fetch_mod, "_browser_fetch", _browser_should_not_be_called)
    _disable_browser_close(monkeypatch)

    env = run(
        fetch_mod.fetch_one(
            "https://laggy.example.com/x",
            cache=cache,
            use_cache=False,
            deadline=4.0,
        )
    )
    assert "failure" in env
    assert env["failure"]["reason"] == FailureReason.DEADLINE_EXCEEDED.value
    assert browser_called == []


def test_both_tiers_fail_returns_browser_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 5: Both tiers fail → failure envelope reflects browser-tier reason."""
    cache = FakeCache()
    # http returns CF challenge, browser also returns CF challenge.
    monkeypatch.setattr(fetch_mod, "_http_fetch", _make_http(CF_HTML, 200))
    monkeypatch.setattr(fetch_mod, "_browser_fetch", _make_browser(CF_HTML, 200))
    _disable_browser_close(monkeypatch)

    env = run(
        fetch_mod.fetch_one(
            "https://blocked.example.com/x",
            cache=cache,
            use_cache=False,
            deadline=30.0,
        )
    )
    assert "failure" in env
    assert (
        env["failure"]["reason"] == FailureReason.BLOCKED_CLOUDFLARE.value
    )
    assert env["mode_used"] == "browser"
    # cache.bump was called twice: once after http (success=False), once after
    # browser (success=False).
    assert {b["mode"] for b in cache.bumps} <= {"http", "browser"}
    assert all(b["success"] is False for b in cache.bumps)
