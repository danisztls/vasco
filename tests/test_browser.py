"""Tests for the Camoufox kwargs assembled by BrowserPool.

These stub `AsyncCamoufox` to a recording mock so we don't actually launch
Firefox — what we care about is which kwargs are passed.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from vasco.fetch import browser as browser_mod
from vasco.fetch.browser import BrowserPool


class _RecordingCM:
    """Async context manager that records the kwargs it was constructed with."""

    instances: list[_RecordingCM] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        type(self).instances.append(self)

    async def __aenter__(self) -> object:
        # Return a stand-in that has `.new_page()` / `.new_context()` so the
        # rest of BrowserPool doesn't blow up if anything calls them. None of
        # these tests do, but it's free insurance.
        class _Stub:
            async def new_page(self) -> object:
                raise AssertionError("page creation not exercised in this test")

            async def new_context(self, **_: Any) -> object:
                raise AssertionError("context creation not exercised in this test")

        return _Stub()

    async def __aexit__(self, *_: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_recordings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _RecordingCM.instances = []
    monkeypatch.setattr(browser_mod, "AsyncCamoufox", _RecordingCM)
    # Point the socket path at a guaranteed-missing file so a real browser
    # server running on the dev machine doesn't divert `_ensure_started`
    # away from the recording mock.
    monkeypatch.setattr(
        browser_mod, "_socket_path", lambda: str(tmp_path / "nonexistent.sock")
    )
    browser_mod._reset_for_tests()


@pytest.mark.asyncio
async def test_default_kwargs_omit_persistent_context() -> None:
    pool = BrowserPool()
    await pool._ensure_started()
    [cm] = _RecordingCM.instances
    assert cm.kwargs == {"headless": True, "locale": ("en-US",)}
    assert "persistent_context" not in cm.kwargs
    assert "user_data_dir" not in cm.kwargs


@pytest.mark.asyncio
async def test_user_data_dir_enables_persistent_context(tmp_path: Path) -> None:
    profile = tmp_path / "camoufox-profile"
    pool = BrowserPool(user_data_dir=str(profile))
    await pool._ensure_started()
    [cm] = _RecordingCM.instances
    assert cm.kwargs["persistent_context"] is True
    # Path is absolutized + expanded; tmp_path is already absolute, so equality
    # holds. The dir must also have been created.
    assert cm.kwargs["user_data_dir"] == str(profile)
    assert profile.is_dir()


@pytest.mark.asyncio
async def test_empty_user_data_dir_is_not_persistent() -> None:
    pool = BrowserPool(user_data_dir="   ")  # whitespace also disables
    await pool._ensure_started()
    [cm] = _RecordingCM.instances
    assert "persistent_context" not in cm.kwargs
    assert "user_data_dir" not in cm.kwargs


@pytest.mark.asyncio
async def test_user_data_dir_expands_env_and_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VASCO_TEST_PROFILE", str(tmp_path / "p"))
    pool = BrowserPool(user_data_dir="$VASCO_TEST_PROFILE")
    await pool._ensure_started()
    [cm] = _RecordingCM.instances
    assert cm.kwargs["user_data_dir"] == str(tmp_path / "p")


@pytest.mark.asyncio
async def test_user_data_dir_expands_xdg_data_home_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """$XDG_DATA_HOME must expand even when the env var is absent (MCP subprocess env)."""
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    pool = BrowserPool(user_data_dir="$XDG_DATA_HOME/vasco/profile")
    await pool._ensure_started()
    [cm] = _RecordingCM.instances
    expected = str(Path.home() / ".local" / "share" / "vasco" / "profile")
    assert cm.kwargs["user_data_dir"] == expected


# ── Tracker-blocking request interception (fetch_page) ──────────────


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


@pytest.mark.asyncio
async def test_fetch_page_installs_route_and_blocks_third_party_tracker() -> None:
    page = _FakePage()
    html, status, _ = await browser_mod.fetch_page(
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


@pytest.mark.asyncio
async def test_fetch_page_no_route_when_netblock_empty_or_none() -> None:
    for netblock in (frozenset(), None):
        page = _FakePage()
        await browser_mod.fetch_page(
            _FakeBrowser(page),
            "https://example.com",
            deadline_monotonic=time.monotonic() + 5,
            netblock=netblock,
        )
        assert page.routes == []
