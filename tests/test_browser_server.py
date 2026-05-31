"""Tests for the persistent browser server's wedge-recovery logic."""

from __future__ import annotations

import pytest

from vasco.fetch import browser_server as bs


class _FakeSupervisor:
    """Stand-in for `_BrowserSupervisor` that records relaunch calls."""

    is_persistent = False

    def __init__(self) -> None:
        self._consecutive_timeouts = 0
        self.relaunches = 0

    async def get_browser(self):  # pragma: no cover - trivial
        return object()

    async def mark_dead(self) -> None:
        self.relaunches += 1

    def note_timeout(self) -> int:
        self._consecutive_timeouts += 1
        return self._consecutive_timeouts

    def reset_timeouts(self) -> None:
        self._consecutive_timeouts = 0


async def test_timeout_streak_relaunches_then_retries(monkeypatch):
    """A consecutive-timeout streak past the threshold forces one relaunch+retry."""
    calls = {"n": 0}

    async def fake_fetch_page(browser, url, **kw):
        calls["n"] += 1
        # Time out until the relaunch retry, then succeed.
        if calls["n"] <= bs._TIMEOUT_RELAUNCH_THRESHOLD:
            raise RuntimeError("Page.goto: Timeout 8000ms exceeded.")
        return ("<html>ok</html>", 200, {})

    monkeypatch.setattr(bs, "_fetch_page", fake_fetch_page)
    sup = _FakeSupervisor()

    # First N-1 requests time out and re-raise without relaunching.
    for _ in range(bs._TIMEOUT_RELAUNCH_THRESHOLD - 1):
        with pytest.raises(RuntimeError):
            await bs._serve_fetch(sup, url="https://x", mobile=False, timeout=8.0)
        assert sup.relaunches == 0

    # The request that crosses the threshold relaunches and the retry succeeds.
    html, status, _ = await bs._serve_fetch(
        sup, url="https://x", mobile=False, timeout=8.0
    )
    assert (html, status) == ("<html>ok</html>", 200)
    assert sup.relaunches == 1
    assert sup._consecutive_timeouts == 0  # reset after success


async def test_success_resets_timeout_streak(monkeypatch):
    """A success between timeouts clears the streak so we don't relaunch early."""
    outcomes = iter([True, True, False])  # timeout, timeout, success

    async def fake_fetch_page(browser, url, **kw):
        if next(outcomes):
            raise RuntimeError("Page.goto: Timeout exceeded")
        return ("<html>ok</html>", 200, {})

    monkeypatch.setattr(bs, "_fetch_page", fake_fetch_page)
    sup = _FakeSupervisor()

    with pytest.raises(RuntimeError):
        await bs._serve_fetch(sup, url="https://a", mobile=False, timeout=8.0)
    assert sup._consecutive_timeouts == 1

    with pytest.raises(RuntimeError):
        await bs._serve_fetch(sup, url="https://b", mobile=False, timeout=8.0)
    assert sup._consecutive_timeouts == 2

    await bs._serve_fetch(sup, url="https://c", mobile=False, timeout=8.0)
    assert sup._consecutive_timeouts == 0
    assert sup.relaunches == 0


async def test_disconnect_still_relaunches_immediately(monkeypatch):
    """A driver disconnect relaunches on the first failure, independent of timeouts."""

    calls = {"n": 0}

    async def fake_fetch_page(browser, url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Target page, context or browser has been closed")
        return ("<html>ok</html>", 200, {})

    monkeypatch.setattr(bs, "_fetch_page", fake_fetch_page)
    sup = _FakeSupervisor()

    html, status, _ = await bs._serve_fetch(
        sup, url="https://x", mobile=False, timeout=8.0
    )
    assert (html, status) == ("<html>ok</html>", 200)
    assert sup.relaunches == 1
