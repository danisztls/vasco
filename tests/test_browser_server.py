"""Tests for the persistent browser server: wedge-recovery, page-driving
(`fetch_page` tracker interception), and launch-kwargs assembly."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vasco.fetch import browser_server as bs


class _FakeSupervisor:
    """Stand-in for `_BrowserSupervisor` that records relaunch + probe calls.

    `wedged` controls the liveness-probe verdict: True (default) keeps the
    relaunch-path tests valid; set False to simulate a healthy browser whose
    timeouts are the *site*, not a wedge.
    """

    is_persistent = False

    def __init__(self, wedged: bool = True) -> None:
        self._consecutive_timeouts = 0
        self.relaunches = 0
        self.wedged = wedged
        self.probes = 0

    async def get_browser(self):  # pragma: no cover - trivial
        return object()

    async def mark_dead(self) -> None:
        self.relaunches += 1

    def note_timeout(self) -> int:
        self._consecutive_timeouts += 1
        return self._consecutive_timeouts

    def reset_timeouts(self) -> None:
        self._consecutive_timeouts = 0

    async def is_wedged(self) -> bool:
        self.probes += 1
        return self.wedged


async def test_timeout_streak_wedged_probe_relaunches_then_retries(monkeypatch):
    """A timeout streak whose liveness probe also fails (a real wedge) forces one
    relaunch+retry."""
    calls = {"n": 0}

    async def fake_fetch_page(browser, url, **kw):
        calls["n"] += 1
        # Time out until the relaunch retry, then succeed.
        if calls["n"] <= bs._TIMEOUT_RELAUNCH_THRESHOLD:
            raise RuntimeError("Page.goto: Timeout 8000ms exceeded.")
        return ("<html>ok</html>", 200, {})

    monkeypatch.setattr(bs, "_fetch_page", fake_fetch_page)
    sup = _FakeSupervisor(wedged=True)  # probe says wedged → relaunch

    # First N-1 requests time out and re-raise without probing or relaunching.
    for _ in range(bs._TIMEOUT_RELAUNCH_THRESHOLD - 1):
        with pytest.raises(RuntimeError):
            await bs._serve_fetch(sup, url="https://x", mobile=False, timeout=8.0)
        assert sup.relaunches == 0

    # The request that crosses the threshold probes, finds a wedge, and retries.
    html, status, _ = await bs._serve_fetch(
        sup, url="https://x", mobile=False, timeout=8.0
    )
    assert (html, status) == ("<html>ok</html>", 200)
    assert sup.probes == 1
    assert sup.relaunches == 1
    assert sup._consecutive_timeouts == 0  # reset after success


async def test_timeout_streak_healthy_probe_does_not_relaunch(monkeypatch):
    """The regression for the watchdog cascade: a burst of slow-site timeouts whose
    liveness probe SUCCEEDS must NOT relaunch — the slow-fetch timeout just
    propagates, leaving the browser (and its healthy sibling pages) untouched."""

    async def fake_fetch_page(browser, url, **kw):
        raise RuntimeError("Page.goto: Timeout 8000ms exceeded.")

    monkeypatch.setattr(bs, "_fetch_page", fake_fetch_page)
    sup = _FakeSupervisor(wedged=False)  # probe says healthy → site is just slow

    # Three same-site timeouts: the third crosses the threshold, probes, and —
    # because the browser is healthy — re-raises the timeout without relaunching.
    for _ in range(bs._TIMEOUT_RELAUNCH_THRESHOLD):
        with pytest.raises(RuntimeError):
            await bs._serve_fetch(sup, url="https://slow", mobile=False, timeout=8.0)

    assert sup.relaunches == 0  # browser preserved, siblings not torn down
    assert sup.probes == 1  # probed exactly once, at the threshold crossing
    assert sup._consecutive_timeouts == 0  # streak consumed by the probe


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


# ── Supervisor lifecycle: recycle, de-storm, force-kill ───────────────────────


class _FakeBrowserObj:
    """Minimal stand-in for a Camoufox Browser: only `is_connected()` is probed
    by `_BrowserSupervisor._alive`."""

    def __init__(self) -> None:
        self._connected = True

    def is_connected(self) -> bool:
        return self._connected


def _stub_supervisor(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, int]]:
    """A real `_BrowserSupervisor` whose launch/close are stubbed (no camoufox)."""
    sup = bs._BrowserSupervisor({}, is_persistent=False)
    counts = {"launch": 0, "close": 0}

    async def fake_launch() -> None:
        counts["launch"] += 1
        sup._cm = object()
        sup._browser = _FakeBrowserObj()

    async def fake_close() -> None:
        counts["close"] += 1
        sup._cm = None
        sup._browser = None

    monkeypatch.setattr(sup, "_launch_locked", fake_launch)
    monkeypatch.setattr(sup, "_close_locked", fake_close)
    return sup, counts


async def test_supervisor_recycles_after_page_threshold(monkeypatch) -> None:
    monkeypatch.setattr(bs, "_RECYCLE_AFTER_PAGES", 3)
    sup, counts = _stub_supervisor(monkeypatch)
    await sup.start()
    assert counts["launch"] == 1

    # 3 handouts are allowed before recycle (fast path: handouts < threshold).
    for _ in range(3):
        await sup.get_browser()
    assert counts["launch"] == 1

    # The handout that crosses the threshold relaunches and resets the counter.
    await sup.get_browser()
    assert counts["launch"] == 2
    assert counts["close"] == 1
    assert sup._handouts == 1


async def test_supervisor_mark_dead_destorms_concurrent_calls(monkeypatch) -> None:
    """A burst of concurrent mark_dead calls collapses to a single close — and a
    late one can't tear down a browser another coroutine already relaunched."""
    sup, counts = _stub_supervisor(monkeypatch)
    await sup.start()
    await asyncio.gather(sup.mark_dead(), sup.mark_dead(), sup.mark_dead())
    assert counts["close"] == 1


async def test_close_timeout_force_kills_browser_tree(monkeypatch) -> None:
    monkeypatch.setattr(bs, "_CLOSE_TIMEOUT", 0.05)
    killed = {"n": 0}
    monkeypatch.setattr(
        bs, "_kill_browser_processes", lambda: killed.__setitem__("n", killed["n"] + 1)
    )

    class _HangingCM:
        async def __aexit__(self, *_: Any) -> None:
            await asyncio.sleep(10)  # never returns within _CLOSE_TIMEOUT

    sup = bs._BrowserSupervisor({}, is_persistent=False)
    sup._cm = _HangingCM()
    sup._browser = _FakeBrowserObj()

    await sup._close_locked()
    assert killed["n"] == 1
    assert sup._cm is None and sup._browser is None


async def test_is_wedged_probe_distinguishes_healthy_from_hung(monkeypatch) -> None:
    """The about:blank liveness probe: fast load → not wedged; a hung goto or no
    browser → wedged."""
    monkeypatch.setattr(bs, "_PROBE_TIMEOUT", 0.05)

    class _ProbePage:
        def __init__(self, delay: float) -> None:
            self._delay = delay
            self.closed = False

        async def goto(self, url: str, **_: Any) -> None:
            if self._delay:
                await asyncio.sleep(self._delay)

        async def close(self) -> None:
            self.closed = True

    class _ProbeBrowser:
        def __init__(self, delay: float) -> None:
            self.page = _ProbePage(delay)

        async def new_page(self) -> _ProbePage:
            return self.page

    # Healthy: about:blank returns well within the probe timeout.
    sup = bs._BrowserSupervisor({}, is_persistent=False)
    sup._browser = _ProbeBrowser(delay=0.0)
    assert await sup.is_wedged() is False
    assert sup._browser.page.closed  # probe cleans up its page

    # Wedged: goto hangs past the probe's outer guard.
    sup._browser = _ProbeBrowser(delay=1.0)
    assert await sup.is_wedged() is True

    # No browser at all is treated as wedged.
    sup._browser = None
    assert await sup.is_wedged() is True


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


# ── Turnstile-solving launch kwargs ───────────────────────────────────────────


def test_build_launch_kwargs_omits_solve_kwargs_by_default() -> None:
    """A cfg with none of the new fields set must not add any solve kwargs —
    headless stays a bool, no humanize/disable_coop/window/block_images leak in."""
    kwargs, _ = bs._build_launch_kwargs(_cfg())
    assert kwargs == {"headless": True, "locale": ("en-US",)}


def test_build_launch_kwargs_virtual_display_overrides_headless() -> None:
    kwargs, _ = bs._build_launch_kwargs(_cfg(virtual_display=True))
    assert kwargs["headless"] == "virtual"


def test_build_launch_kwargs_solve_knobs_flow_through() -> None:
    kwargs, _ = bs._build_launch_kwargs(
        _cfg(
            virtual_display=True,
            humanize=True,
            disable_coop=True,
            block_images=True,
            window=[1280, 720],
        )
    )
    assert kwargs["headless"] == "virtual"
    assert kwargs["humanize"] is True
    assert kwargs["disable_coop"] is True
    assert kwargs["block_images"] is True
    assert kwargs["window"] == (1280, 720)


def test_force_x11_scrubs_wayland_so_browser_stays_off_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression for windows leaking onto the real desktop: a --user service
    inherits WAYLAND_DISPLAY, which makes a virtual-display Firefox render to the
    real compositor. The scrub must remove it (and DISPLAY) and pin X11."""
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    bs._force_x11_for_virtual_display()
    assert "WAYLAND_DISPLAY" not in os.environ
    assert "DISPLAY" not in os.environ  # Camoufox sets its own :N for the Xvfb
    assert os.environ["MOZ_ENABLE_WAYLAND"] == "0"


def test_build_launch_kwargs_window_coerces_and_guards() -> None:
    # Env-var path yields strings; they must coerce to ints.
    kwargs, _ = bs._build_launch_kwargs(_cfg(window=("800", "600")))
    assert kwargs["window"] == (800, 600)
    # A malformed window is dropped, not fatal.
    kwargs, _ = bs._build_launch_kwargs(_cfg(window=["oops"]))
    assert "window" not in kwargs


# ── Turnstile challenge detection + solve (_maybe_solve_turnstile) ─────────────

_CF_CHALLENGE_HTML = (
    "<html><head><title>Just a moment...</title></head><body>"
    "<div class='cf-turnstile'></div>"
    "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script>"
    "</body></html>"
)
_CLEARED_HTML = (
    "<html><body><article>" + ("real content " * 200) + "</article></body></html>"
)


def test_looks_challenged_detects_cf_and_passes_real_content() -> None:
    assert bs._looks_challenged(200, _CF_CHALLENGE_HTML, {}) is True
    assert bs._looks_challenged(200, _CLEARED_HTML, {}) is False


class _CFFrame:
    """frame_locator(...).locator(...) stand-in whose click clears the page."""

    def __init__(self, page: "_CFChallengePage") -> None:
        self._page = page

    def locator(self, _sel: str) -> "_CFFrame":
        return self

    async def click(self, timeout: int | None = None) -> None:
        self._page.clicks += 1
        self._page.cleared = True


class _CFChallengePage:
    """A page that serves a CF challenge until its Turnstile checkbox is clicked."""

    def __init__(self) -> None:
        self.cleared = False
        self.clicks = 0
        self.frame_locator_calls = 0

    def frame_locator(self, _sel: str) -> _CFFrame:
        self.frame_locator_calls += 1
        return _CFFrame(self)

    async def content(self) -> str:
        return _CLEARED_HTML if self.cleared else _CF_CHALLENGE_HTML


async def test_maybe_solve_turnstile_clicks_and_clears() -> None:
    page = _CFChallengePage()
    cleared = await bs._maybe_solve_turnstile(
        page,
        status=200,
        html=_CF_CHALLENGE_HTML,
        headers={},
        deadline_monotonic=time.monotonic() + 5,
    )
    assert cleared is True
    assert page.clicks == 1


async def test_maybe_solve_turnstile_noop_when_not_challenged() -> None:
    page = _CFChallengePage()
    cleared = await bs._maybe_solve_turnstile(
        page,
        status=200,
        html=_CLEARED_HTML,  # already real content
        headers={},
        deadline_monotonic=time.monotonic() + 5,
    )
    assert cleared is False
    assert page.frame_locator_calls == 0  # never touched the page


async def test_maybe_solve_turnstile_returns_false_when_no_budget() -> None:
    """Past the deadline: no click, no clearance — degrades to a failed solve."""
    page = _CFChallengePage()
    cleared = await bs._maybe_solve_turnstile(
        page,
        status=403,
        html=_CF_CHALLENGE_HTML,
        headers={},
        deadline_monotonic=time.monotonic() - 1,  # already elapsed
    )
    assert cleared is False
    assert page.clicks == 0


# ── fetch_page end-to-end with solving ────────────────────────────────────────


class _CFResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    async def all_headers(self) -> dict[str, str]:
        return {}


class _CFFetchPage(_CFChallengePage):
    """Adds the page-driving surface fetch_page needs on top of the challenge page."""

    def __init__(self, status: int = 200) -> None:
        super().__init__()
        self._status = status
        self.closed = False

    async def route(self, _pattern: str, _handler: Any) -> None:  # pragma: no cover
        return None

    async def goto(self, _url: str, **_: Any) -> _CFResponse:
        return _CFResponse(self._status)

    async def wait_for_load_state(self, *_: Any, **__: Any) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _CFBrowser:
    def __init__(self, page: _CFFetchPage) -> None:
        self._page = page

    async def new_page(self) -> _CFFetchPage:
        return self._page


async def test_fetch_page_solves_challenge_when_enabled() -> None:
    page = _CFFetchPage(status=403)
    html, status, _ = await bs.fetch_page(
        _CFBrowser(page),
        "https://poder360.com.br",
        deadline_monotonic=time.monotonic() + 5,
        solve_turnstile=True,
    )
    assert page.clicks == 1
    assert "real content" in html
    assert status == 200  # cleared content reported as 200, not the 403 challenge


async def test_fetch_page_skips_solve_when_disabled() -> None:
    page = _CFFetchPage(status=200)
    html, status, _ = await bs.fetch_page(
        _CFBrowser(page),
        "https://poder360.com.br",
        deadline_monotonic=time.monotonic() + 5,
        # solve_turnstile defaults False
    )
    assert page.clicks == 0
    assert "challenges.cloudflare.com" in html  # untouched challenge HTML


# ── Manual (human-in-the-loop) solve: notify + budget-suspended hold ───────────


def test_pick_free_display_returns_unused_display() -> None:
    d = bs._pick_free_display(":71")
    assert d.startswith(":")
    n = d.lstrip(":")
    assert not os.path.exists(f"/tmp/.X11-unix/X{n}")  # genuinely free


async def test_manual_solve_hold_notifies_and_clears(monkeypatch) -> None:
    notified: list[str] = []

    async def fake_notify(url: str) -> None:
        notified.append(url)

    monkeypatch.setattr(bs, "_notify_manual_solve", fake_notify)
    monkeypatch.setattr(bs, "_manual_in_progress", False)
    page = _CFChallengePage()
    page.cleared = True  # human "solved it" → clearance detected on first poll
    ok = await bs._manual_solve_hold(page, "https://x", timeout=5)
    assert ok is True
    assert notified == ["https://x"]


async def test_manual_solve_hold_skips_when_already_in_progress(monkeypatch) -> None:
    """Single-in-progress guard: a concurrent hold returns immediately, no notify."""
    notified: list[str] = []

    async def fake_notify(url: str) -> None:  # pragma: no cover - must not run
        notified.append(url)

    monkeypatch.setattr(bs, "_notify_manual_solve", fake_notify)
    monkeypatch.setattr(bs, "_manual_in_progress", True)  # one already running
    page = _CFChallengePage()
    page.cleared = True
    ok = await bs._manual_solve_hold(page, "https://x", timeout=5)
    assert ok is False
    assert notified == []  # never notified, never held


async def test_maybe_solve_turnstile_manual_fallback(monkeypatch) -> None:
    """When auto is skipped/failed and manual_solve is on, hold for the human."""
    monkeypatch.setattr(bs, "_notify_manual_solve", lambda url: _noop())
    monkeypatch.setattr(bs, "_manual_in_progress", False)
    page = _CFChallengePage()
    page.cleared = True  # human solves during the hold
    ok = await bs._maybe_solve_turnstile(
        page,
        status=403,
        html=_CF_CHALLENGE_HTML,
        headers={},
        deadline_monotonic=time.monotonic() + 5,
        url="https://x",
        solve_turnstile=False,  # skip auto-click; go straight to manual
        manual_solve=True,
        manual_solve_timeout=5,
    )
    assert ok is True


async def test_maybe_solve_turnstile_no_manual_when_disabled() -> None:
    """Auto fails to clear and manual_solve is off → no hold, returns False."""
    page = _CFChallengePage()  # never clears (click is a no-op via _click fallback)
    ok = await bs._maybe_solve_turnstile(
        page,
        status=403,
        html=_CF_CHALLENGE_HTML,
        headers={},
        deadline_monotonic=time.monotonic() - 1,  # no budget → auto can't clear
        url="https://x",
        solve_turnstile=True,
        manual_solve=False,
    )
    assert ok is False


async def _noop() -> None:
    return None
