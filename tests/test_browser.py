"""Tests for the Camoufox kwargs assembled by BrowserPool.

These stub `AsyncCamoufox` to a recording mock so we don't actually launch
Firefox — what we care about is which kwargs are passed.
"""

from __future__ import annotations

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
