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
        self.store[envelope.get("url_canonical") or envelope["url_requested"]] = (
            envelope
        )

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
        url: str,
        *,
        deadline_monotonic: float,
        cfg: Any | None = None,
        mobile: bool = False,
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


def _disable_wayback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the wayback recovery tier to find no snapshot.

    Tests that exercise a fully-blocked auto chain need this so the final
    recovery attempt doesn't hit the real archive.org.
    """
    from vasco import wayback as wayback_mod

    async def _no_snapshot(
        url: str, *, deadline_monotonic: float, cfg: Any | None = None
    ) -> str | None:
        return None

    monkeypatch.setattr(wayback_mod, "find_snapshot", _no_snapshot)


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
    assert cache.bumps == [{"domain": "example.com", "mode": "http", "success": True}]


def test_http_cloudflare_escalates_to_browser_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 2: http → CF block → browser succeeds. mode_used=browser, bump browser/True."""
    cache = FakeCache()
    monkeypatch.setattr(fetch_mod, "_http_fetch", _make_http(CF_HTML, 200))
    monkeypatch.setattr(fetch_mod, "_browser_fetch", _make_browser(CLEAN_HTML, 200))
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
    monkeypatch.setattr(fetch_mod, "_browser_fetch", _make_browser(CLEAN_HTML, 200))
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


def test_browser_disconnect_classified_as_blocked_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Playwright-style disconnect during browser fetch → BLOCKED_BOT (not SERVER_ERROR)."""
    cache = FakeCache(strategy="browser")

    class _DisconnectingPool:
        async def fetch(self, *a: Any, **kw: Any) -> tuple[str, int, dict[str, str]]:
            raise RuntimeError(
                "Page.content: Connection closed while reading from the driver"
            )

        async def close(self) -> None:
            return None

    from vasco import browser as browser_mod

    monkeypatch.setattr(browser_mod, "_pool", None, raising=False)
    monkeypatch.setattr(
        browser_mod, "get_browser", lambda cfg=None: _DisconnectingPool()
    )

    env = run(
        fetch_mod.fetch_one(
            "https://anti-bot.example.com/x",
            cache=cache,
            use_cache=False,
            deadline=10.0,
        )
    )
    assert "failure" in env
    assert env["failure"]["reason"] == FailureReason.BLOCKED_BOT.value
    assert env["mode_used"] == "browser"


def test_playwright_timeout_classified_as_timeout_not_bot_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Playwright TimeoutError → TIMEOUT, not BLOCKED_BOT (regression: slow
    loads on w3.org / turing.ac.uk used to be mislabeled as bot blocks)."""

    class _PWTimeout(Exception):
        pass

    _PWTimeout.__name__ = "TimeoutError"  # mirror playwright's class name

    class _Pool:
        async def fetch(self, *a: Any, **kw: Any) -> tuple[str, int, dict[str, str]]:
            raise _PWTimeout("Page.goto: Timeout 15000ms exceeded.")

        async def close(self) -> None: ...

    from vasco import browser as browser_mod

    monkeypatch.setattr(browser_mod, "_pool", None, raising=False)
    monkeypatch.setattr(browser_mod, "get_browser", lambda cfg=None: _Pool())

    env = run(
        fetch_mod.fetch_one(
            "https://slow.example.com/x",
            cache=FakeCache(strategy="browser"),
            use_cache=False,
            deadline=10.0,
        )
    )
    assert env["failure"]["reason"] == FailureReason.TIMEOUT.value
    assert env["mode_used"] == "browser"


def test_browser_unknown_exception_propagates_to_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognized browser-tier exception falls through to SERVER_ERROR, not BLOCKED_BOT."""
    cache = FakeCache(strategy="browser")

    class _BoomPool:
        async def fetch(self, *a: Any, **kw: Any) -> tuple[str, int, dict[str, str]]:
            raise RuntimeError("totally unexpected internal error")

        async def close(self) -> None:
            return None

    from vasco import browser as browser_mod

    monkeypatch.setattr(browser_mod, "_pool", None, raising=False)
    monkeypatch.setattr(browser_mod, "get_browser", lambda cfg=None: _BoomPool())

    env = run(
        fetch_mod.fetch_one(
            "https://chaos.example.com/x",
            cache=cache,
            use_cache=False,
            deadline=10.0,
        )
    )
    assert "failure" in env
    assert env["failure"]["reason"] == FailureReason.SERVER_ERROR.value


def test_http_not_found_does_not_escalate(monkeypatch: pytest.MonkeyPatch) -> None:
    """410/404 from http tier → NOT_FOUND, no browser call, no domain bump."""
    cache = FakeCache()
    monkeypatch.setattr(fetch_mod, "_http_fetch", _make_http("", 410))

    browser_called: list[str] = []

    async def _browser_should_not_be_called(
        url: str, *, deadline_monotonic: float, cfg: Any | None = None
    ) -> tuple[str, int, dict[str, str]]:
        browser_called.append(url)
        return "", 0, {}

    monkeypatch.setattr(fetch_mod, "_browser_fetch", _browser_should_not_be_called)
    _disable_browser_close(monkeypatch)

    env = run(
        fetch_mod.fetch_one(
            "https://hiteck.example.com/produtos/versa-evo/",
            cache=cache,
            use_cache=False,
            deadline=30.0,
        )
    )
    assert env["failure"]["reason"] == FailureReason.NOT_FOUND.value
    assert env["mode_used"] == "http"
    assert env["http_status"] == 410
    assert browser_called == []
    # Domain strategy should not be moved by a per-URL 404/410.
    assert cache.bumps == []


def test_both_tiers_fail_returns_browser_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 5: Both tiers (plus the post-browser recovery tiers) fail →
    failure envelope reflects browser-tier reason and mode_used="browser".

    The recovery chain still runs (mobile + wayback), but with the same
    blocked response and no wayback snapshot, the original browser failure
    is what's reported.
    """
    cache = FakeCache()
    # http returns CF challenge, browser (incl. mobile retry) also returns CF.
    monkeypatch.setattr(fetch_mod, "_http_fetch", _make_http(CF_HTML, 200))
    monkeypatch.setattr(fetch_mod, "_browser_fetch", _make_browser(CF_HTML, 200))
    _disable_browser_close(monkeypatch)
    _disable_wayback(monkeypatch)

    env = run(
        fetch_mod.fetch_one(
            "https://blocked.example.com/x",
            cache=cache,
            use_cache=False,
            deadline=30.0,
        )
    )
    assert "failure" in env
    assert env["failure"]["reason"] == FailureReason.BLOCKED_CLOUDFLARE.value
    assert env["mode_used"] == "browser"
    # cache.bump should only reflect http + browser tiers, never mobile/wayback.
    assert {b["mode"] for b in cache.bumps} <= {"http", "browser"}
    assert all(b["success"] is False for b in cache.bumps)


# -----------------------------------------------------------------------------
# Phase timing assertions (duration_ms / network_ms / parse_ms / attempts /
# escalated_from). The fake http/browser helpers return immediately so the
# wall-clock ms are tiny — we assert presence and structural relationships,
# not absolute values.
# -----------------------------------------------------------------------------


def test_phase_timings_stamped_on_plain_http_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = FakeCache()
    monkeypatch.setattr(fetch_mod, "_http_fetch", _make_http(CLEAN_HTML, 200))
    _disable_browser_close(monkeypatch)

    env = run(
        fetch_mod.fetch_one(
            "https://example.com/article",
            cache=cache,
            use_cache=False,
            deadline=30.0,
        )
    )

    assert "failure" not in env
    assert env["mode_used"] == "http"
    # Phase fields are always stamped on a live fetch (zero ms means "fast",
    # not "skipped"). They're omitted only on short-circuit paths.
    assert isinstance(env["duration_ms"], int) and env["duration_ms"] >= 0
    assert env["attempts"] == 1
    assert isinstance(env["network_ms"], int)
    assert isinstance(env["parse_ms"], int)
    assert isinstance(env["cache_write_ms"], int)
    # No escalation on a clean http success.
    assert "escalated_from" not in env


def test_phase_timings_record_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = FakeCache()
    monkeypatch.setattr(fetch_mod, "_http_fetch", _make_http(CF_HTML, 200))
    monkeypatch.setattr(fetch_mod, "_browser_fetch", _make_browser(CLEAN_HTML, 200))
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
    assert env["attempts"] == 2
    assert env["escalated_from"] == "http"


def test_cache_hit_envelope_omits_phase_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = FakeCache()
    monkeypatch.setattr(fetch_mod, "_http_fetch", _make_http(CLEAN_HTML, 200))
    _disable_browser_close(monkeypatch)

    # First call: live fetch, populates cache.store.
    first = run(
        fetch_mod.fetch_one(
            "https://example.com/article",
            cache=cache,
            use_cache=True,
            deadline=30.0,
        )
    )
    assert first["attempts"] == 1

    # Second call: cache hit. Should carry duration_ms but none of the live-
    # fetch phase fields, which describe how the entry was *originally*
    # obtained and would be misleading on a cache hit. The real Cache.put
    # doesn't persist phase columns at all; `_hydrate_cache_hit` also strips
    # them defensively so this holds even with a dumb dict cache.
    second = run(
        fetch_mod.fetch_one(
            "https://example.com/article",
            cache=cache,
            use_cache=True,
            deadline=30.0,
        )
    )
    assert second["from_cache"] is True
    assert "duration_ms" in second
    for absent in (
        "network_ms",
        "parse_ms",
        "cache_write_ms",
        "attempts",
        "escalated_from",
    ):
        assert absent not in second, absent


# -----------------------------------------------------------------------------
# Recovery chain: mobile and wayback tiers triggered after browser blocks
# -----------------------------------------------------------------------------


def _make_browser_then_clean(blocked_html: str, clean_html: str, status: int = 200):
    """Browser stub: returns blocked_html on first call, clean_html when mobile=True."""

    async def _fake_browser(
        url: str,
        *,
        deadline_monotonic: float,
        cfg: Any | None = None,
        mobile: bool = False,
    ) -> tuple[str, int, dict[str, str]]:
        return (clean_html if mobile else blocked_html), status, {}

    return _fake_browser


def _patch_wayback_snapshot(
    monkeypatch: pytest.MonkeyPatch, snapshot_url: str | None
) -> list[str]:
    """Patch wayback.find_snapshot to return `snapshot_url` for any input.

    Returns a list that records each call's input URL so tests can assert
    that wayback was (or wasn't) consulted.
    """
    from vasco import wayback as wayback_mod

    calls: list[str] = []

    async def _fake(
        url: str, *, deadline_monotonic: float, cfg: Any | None = None
    ) -> str | None:
        calls.append(url)
        return snapshot_url

    monkeypatch.setattr(wayback_mod, "find_snapshot", _fake)
    return calls


def test_mobile_recovers_after_browser_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto chain: http CF block → browser CF block → mobile recovers."""
    cache = FakeCache()
    monkeypatch.setattr(fetch_mod, "_http_fetch", _make_http(CF_HTML, 200))
    monkeypatch.setattr(
        fetch_mod, "_browser_fetch", _make_browser_then_clean(CF_HTML, CLEAN_HTML)
    )
    wb_calls = _patch_wayback_snapshot(monkeypatch, None)
    _disable_browser_close(monkeypatch)

    env = run(
        fetch_mod.fetch_one(
            "https://hard.example.com/x",
            cache=cache,
            use_cache=False,
            deadline=30.0,
        )
    )
    assert "failure" not in env
    assert env["mode_used"] == "browser+mobile"
    # Wayback should not have been consulted since mobile succeeded.
    assert wb_calls == []
    # Mobile is a recovery tier; it must not be recorded as a domain strategy.
    assert {b["mode"] for b in cache.bumps} <= {"http", "browser"}


def test_wayback_recovers_after_mobile_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto chain: every tier blocked except wayback, which returns clean HTML."""
    cache = FakeCache()
    monkeypatch.setattr(fetch_mod, "_http_fetch", _make_http(CF_HTML, 200))
    monkeypatch.setattr(fetch_mod, "_browser_fetch", _make_browser(CF_HTML, 200))

    snapshot_url = (
        "https://web.archive.org/web/20240501123045if_/https://hard.example.com/x"
    )
    wb_calls = _patch_wayback_snapshot(monkeypatch, snapshot_url)

    # Once wayback gives us a snapshot URL, fetch.py calls _http_fetch again
    # against that URL. Swap the stub mid-flight by URL-matching.
    real_http_stub = _make_http(CF_HTML, 200)
    clean_for_snapshot = _make_http(CLEAN_HTML, 200)

    async def _dispatching_http(
        url: str, *, deadline_monotonic: float, cfg: Any | None = None
    ) -> tuple[str, int, dict[str, str]]:
        if url.startswith("https://web.archive.org/"):
            return await clean_for_snapshot(
                url, deadline_monotonic=deadline_monotonic, cfg=cfg
            )
        return await real_http_stub(url, deadline_monotonic=deadline_monotonic, cfg=cfg)

    monkeypatch.setattr(fetch_mod, "_http_fetch", _dispatching_http)
    _disable_browser_close(monkeypatch)

    env = run(
        fetch_mod.fetch_one(
            "https://hard.example.com/x",
            cache=cache,
            use_cache=False,
            deadline=30.0,
        )
    )
    assert "failure" not in env, env.get("failure")
    assert env["mode_used"] == "wayback"
    assert wb_calls == ["https://hard.example.com/x"]


def test_explicit_wayback_mode_skips_other_tiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mode="wayback" goes straight to wayback; http/browser stubs are never called."""
    cache = FakeCache()

    http_calls: list[str] = []

    async def _http_should_not_be_called(
        url: str, *, deadline_monotonic: float, cfg: Any | None = None
    ) -> tuple[str, int, dict[str, str]]:
        http_calls.append(url)
        # Wayback's snapshot fetch uses _http_fetch, so allow archive.org URLs through.
        if url.startswith("https://web.archive.org/"):
            return CLEAN_HTML, 200, {"_url_final": url}
        return "", 0, {}

    monkeypatch.setattr(fetch_mod, "_http_fetch", _http_should_not_be_called)
    snapshot = "https://web.archive.org/web/20240501123045if_/https://wb.example.com/x"
    _patch_wayback_snapshot(monkeypatch, snapshot)
    _disable_browser_close(monkeypatch)

    env = run(
        fetch_mod.fetch_one(
            "https://wb.example.com/x",
            mode="wayback",
            cache=cache,
            use_cache=False,
            deadline=30.0,
        )
    )
    assert env["mode_used"] == "wayback"
    assert "failure" not in env
    # Only the snapshot fetch should hit _http_fetch — never the origin URL.
    assert http_calls == [snapshot]


def test_explicit_wayback_returns_not_found_when_no_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mode="wayback" with no available snapshot → failure with NOT_FOUND."""
    cache = FakeCache()
    _patch_wayback_snapshot(monkeypatch, None)
    _disable_browser_close(monkeypatch)

    env = run(
        fetch_mod.fetch_one(
            "https://nothing.example.com/x",
            mode="wayback",
            cache=cache,
            use_cache=False,
            deadline=10.0,
        )
    )
    assert "failure" in env
    assert env["failure"]["reason"] == FailureReason.NOT_FOUND.value
    assert env["mode_used"] == "wayback"


def test_explicit_mobile_mode_calls_browser_with_mobile_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mode="mobile" forces browser tier with mobile=True; no http preamble."""
    cache = FakeCache()
    calls: list[dict] = []

    async def _fake_browser(
        url: str,
        *,
        deadline_monotonic: float,
        cfg: Any | None = None,
        mobile: bool = False,
    ) -> tuple[str, int, dict[str, str]]:
        calls.append({"url": url, "mobile": mobile})
        return CLEAN_HTML, 200, {}

    http_calls: list[str] = []

    async def _http_should_not_be_called(
        url: str, *, deadline_monotonic: float, cfg: Any | None = None
    ) -> tuple[str, int, dict[str, str]]:
        http_calls.append(url)
        return "", 0, {}

    monkeypatch.setattr(fetch_mod, "_http_fetch", _http_should_not_be_called)
    monkeypatch.setattr(fetch_mod, "_browser_fetch", _fake_browser)
    _disable_browser_close(monkeypatch)

    env = run(
        fetch_mod.fetch_one(
            "https://m.example.com/x",
            mode="mobile",
            cache=cache,
            use_cache=False,
            deadline=30.0,
        )
    )
    assert env["mode_used"] == "mobile"
    assert "failure" not in env
    assert http_calls == []
    assert calls == [{"url": "https://m.example.com/x", "mobile": True}]
    # Explicit mobile mode should not bump domain strategy.
    assert cache.bumps == []


def test_recovery_skipped_when_budget_too_tight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If only ~1s remains after browser fails, neither mobile nor wayback should run."""
    cache = FakeCache()
    monkeypatch.setattr(fetch_mod, "_http_fetch", _make_http(CF_HTML, 200))

    async def _slow_browser(
        url: str,
        *,
        deadline_monotonic: float,
        cfg: Any | None = None,
        mobile: bool = False,
    ) -> tuple[str, int, dict[str, str]]:
        # Burn most of the remaining budget so mobile/wayback can't run.
        remaining = max(0.0, deadline_monotonic - time.monotonic())
        await asyncio.sleep(max(0.0, remaining - 1.0))
        return CF_HTML, 200, {}

    monkeypatch.setattr(fetch_mod, "_browser_fetch", _slow_browser)
    wb_calls = _patch_wayback_snapshot(monkeypatch, None)
    _disable_browser_close(monkeypatch)

    env = run(
        fetch_mod.fetch_one(
            "https://tight.example.com/x",
            cache=cache,
            use_cache=False,
            deadline=4.0,
        )
    )
    assert "failure" in env
    assert env["mode_used"] == "browser"
    assert wb_calls == []  # wayback skipped due to budget
