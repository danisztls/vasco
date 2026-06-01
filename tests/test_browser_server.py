"""Tests for the persistent browser server: wedge-recovery, page-driving
(`fetch_page` tracker interception), and launch-kwargs assembly."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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


# ── Page driving / tracker-blocking request interception (fetch_page) ──────────


class _FakeResponse:
    status = 200

    async def all_headers(self) -> dict[str, str]:
        return {}


class _FakeRoute:
    """Records the action the route handler takes for a given request URL."""

    def __init__(self, url: str) -> None:
        self.request = type("Req", (), {"url": url})()
        self.action: str | None = None

    async def abort(self) -> None:
        self.action = "abort"

    async def continue_(self) -> None:
        self.action = "continue"


class _FakePage:
    def __init__(self) -> None:
        self.routes: list[tuple[str, Any]] = []
        self.closed = False

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    async def goto(self, url: str, **_: Any) -> _FakeResponse:
        return _FakeResponse()

    async def wait_for_load_state(self, *_: Any, **__: Any) -> None:
        return None

    async def content(self) -> str:
        return "<html>ok</html>"

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self._page = page

    async def new_page(self) -> _FakePage:
        return self._page


async def test_fetch_page_installs_route_and_blocks_third_party_tracker() -> None:
    page = _FakePage()
    html, status, _ = await bs.fetch_page(
        _FakeBrowser(page),
        "https://example.com",
        deadline_monotonic=time.monotonic() + 5,
        netblock=frozenset({"tracker.com"}),
    )
    assert (html, status) == ("<html>ok</html>", 200)
    assert len(page.routes) == 1
    pattern, handler = page.routes[0]
    assert pattern == "**/*"

    # Drive the captured handler: third-party tracker aborted, first-party passes.
    tracker = _FakeRoute("https://tracker.com/t.js")
    await handler(tracker)
    assert tracker.action == "abort"

    first_party = _FakeRoute("https://example.com/app.js")
    await handler(first_party)
    assert first_party.action == "continue"


async def test_fetch_page_no_route_when_netblock_empty_or_none() -> None:
    for netblock in (frozenset(), None):
        page = _FakePage()
        await bs.fetch_page(
            _FakeBrowser(page),
            "https://example.com",
            deadline_monotonic=time.monotonic() + 5,
            netblock=netblock,
        )
        assert page.routes == []


# ── Launch-kwargs assembly (_build_launch_kwargs) ─────────────────────────────


def _cfg(**browser: Any) -> SimpleNamespace:
    # _build_launch_kwargs reads headless/locale/user_data_dir in one try-block,
    # so a partial namespace would AttributeError into the defaults — supply all.
    fields = {"headless": True, "locale": "en-US", "user_data_dir": ""}
    fields.update(browser)
    return SimpleNamespace(browser=SimpleNamespace(**fields))


def test_build_launch_kwargs_default_omits_persistent_context() -> None:
    kwargs, is_persistent = bs._build_launch_kwargs(None)
    assert kwargs == {"headless": True, "locale": ("en-US",)}
    assert is_persistent is False


def test_build_launch_kwargs_user_data_dir_enables_persistent_context(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "camoufox-profile"
    kwargs, is_persistent = bs._build_launch_kwargs(_cfg(user_data_dir=str(profile)))
    assert is_persistent is True
    assert kwargs["persistent_context"] is True
    assert kwargs["user_data_dir"] == str(profile)
    assert profile.is_dir()  # created


def test_build_launch_kwargs_expands_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VASCO_TEST_PROFILE", str(tmp_path / "p"))
    kwargs, _ = bs._build_launch_kwargs(_cfg(user_data_dir="$VASCO_TEST_PROFILE"))
    assert kwargs["user_data_dir"] == str(tmp_path / "p")


def test_build_launch_kwargs_expands_xdg_data_home_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """$XDG_DATA_HOME must expand even when the env var is absent (subprocess env)."""
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    kwargs, _ = bs._build_launch_kwargs(
        _cfg(user_data_dir="$XDG_DATA_HOME/vasco/profile")
    )
    expected = str(Path.home() / ".local" / "share" / "vasco" / "profile")
    assert kwargs["user_data_dir"] == expected
