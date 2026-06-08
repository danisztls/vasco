"""Persistent Camoufox browser server over a UNIX socket.

Owns one Camoufox browser, serves fetch requests from any local consumer
(MCP server, claudinho, CLI). The browser stays warm between requests.

Protocol: length-prefixed JSON over UNIX socket.
  - 4-byte big-endian uint32 length prefix
  - JSON payload

Request:  {"url": "...", "mobile": false, "timeout": 30.0}
Response: {"html": "...", "status": 200, "headers": {...}}
Error:    {"error": "message"}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import struct
import subprocess
import time
from pathlib import Path
from typing import Any

from ..cache import registered_domain
from ..errors import FailureReason
from . import bot_detect
from .netblock import load_netblock, should_block

log = logging.getLogger(__name__)

# A page whose markers classify to one of these is a live Cloudflare challenge
# we should try to solve (interstitial *or* Turnstile widget).
_CHALLENGE_REASONS = frozenset(
    {FailureReason.BLOCKED_CLOUDFLARE, FailureReason.BLOCKED_CAPTCHA}
)

_HEADER = struct.Struct("!I")

_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 "
    "Mobile/15E148 Safari/604.1"
)
_MOBILE_VIEWPORT = {"width": 393, "height": 852}

# --- Concurrency + lifecycle limits ------------------------------------------
# Camoufox's patched Firefox deadlocks when `new_page()`/`new_context()` run
# concurrently on one shared browser (daijro/camoufox#279, #553): the loser's
# `goto` never resolves while the process stays connected — the silent wedge.
# `_CREATE_LOCK` serializes *creation only* (navigation stays concurrent) and is
# the root-cause fix; `_PAGE_SEMAPHORE` bounds how many pages are open at once so
# a burst can't spike Firefox memory (which it never reclaims — camoufox#245).
_MAX_CONCURRENT_PAGES = 3
_CREATE_LOCK = asyncio.Lock()
_PAGE_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_PAGES)

# Graceful browser close is wrapped in this timeout; a wedged browser's close can
# hang forever and (held under the supervisor lock) deadlock the whole server, so
# on timeout we SIGKILL the process tree instead.
_CLOSE_TIMEOUT = 10.0

# Recycle the browser after this many page handouts. Firefox doesn't GC like
# Chromium under a long-lived Playwright session, so memory creeps; a periodic
# relaunch bounds it. An in-flight page torn down by a recycle is retried by
# `_serve_fetch`'s disconnect path, same as any mid-flight browser death.
_RECYCLE_AFTER_PAGES = 150

# Cap on the post-navigation `networkidle` settle. On ad/beacon-heavy pages
# networkidle never goes quiet, so an uncapped wait would burn the entire
# browser budget even though `page.content()` was ready at domcontentloaded —
# a throughput killer under bursts. 3s still covers typical SPA hydration (the
# JSON-LD the structured adapters depend on lands well inside it); a page that
# settles sooner returns sooner, one that never settles returns its
# domcontentloaded HTML at the cap instead of at the full budget.
_NETWORKIDLE_SETTLE_CAP = 3.0


# --- Playwright Firefox driver patch -------------------------------------
# Playwright's Firefox PageError dispatcher reads `pageError.location.url`
# (and .lineNumber/.columnNumber) with no null guard. Firefox can report an
# uncaught page error whose `location` is undefined; the deref then throws a
# TypeError *inside the Node driver process*, killing the driver connection.
# Because our browser is long-lived and shared, that one crash takes down every
# subsequent fetch until restart. The bug is generic — any page that emits a
# locationless uncaught error triggers it — so we patch the bundled driver with
# optional-chaining + protocol-valid fallbacks. Idempotent; failures are
# swallowed (the supervisor still recovers from any crash that slips through).
_PATCH_REPLACEMENTS = (
    ("url: pageError.location.url,", 'url: pageError.location?.url ?? "",'),
    (
        "line: pageError.location.lineNumber,",
        "line: pageError.location?.lineNumber ?? 0,",
    ),
    (
        "column: pageError.location.columnNumber",
        "column: pageError.location?.columnNumber ?? 0",
    ),
)


def _patch_playwright_driver() -> None:
    try:
        import playwright

        bundle = (
            Path(playwright.__file__).parent
            / "driver"
            / "package"
            / "lib"
            / "coreBundle.js"
        )
        if not bundle.is_file():
            return
        text = bundle.read_text(encoding="utf-8")
        if "pageError.location?.url" in text:
            return  # already patched
        if "pageError.location.url" not in text:
            return  # upstream changed shape — don't guess
        for old, new in _PATCH_REPLACEMENTS:
            text = text.replace(old, new)
        bundle.write_text(text, encoding="utf-8")
        log.info("patched Playwright Firefox driver pageError null-deref (%s)", bundle)
    except Exception as exc:  # never block server startup
        log.warning("could not patch Playwright driver: %s", exc)


def _socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(runtime) / "vasco" / "browser.sock"


async def _read_msg(reader: asyncio.StreamReader) -> dict | None:
    header = await reader.readexactly(_HEADER.size)
    (length,) = _HEADER.unpack(header)
    if length > 10 * 1024 * 1024:
        return None
    data = await reader.readexactly(length)
    return json.loads(data)


async def _write_msg(writer: asyncio.StreamWriter, msg: dict) -> None:
    payload = json.dumps(msg, ensure_ascii=False).encode()
    writer.write(_HEADER.pack(len(payload)) + payload)
    await writer.drain()


# Substrings that mean the browser process died / the driver pipe dropped.
# A request hitting one of these is retried once against a freshly relaunched
# browser (see `_serve_fetch`).
_DISCONNECT_MARKERS = (
    "connection closed",
    "target closed",
    "target page, context or browser has been closed",
    "browser has been closed",
    "browser closed",
    "disconnected",
)


def _is_disconnect(exc: BaseException) -> bool:
    s = str(exc).lower()
    return any(m in s for m in _DISCONNECT_MARKERS)


def _is_timeout(exc: BaseException) -> bool:
    return type(exc).__name__ == "TimeoutError" or "timeout" in str(exc).lower()


# A wedged browser (renderer hung after suspend/resume or a bad tab) stays
# `is_connected()`-alive but every `page.goto` times out — so `_alive()` never
# trips and the server serves nothing but timeouts until manually restarted.
# After this many *consecutive* goto timeouts we run a liveness probe to decide
# whether to relaunch. A raw timeout count alone can't tell a wedged browser
# (every URL hangs) from a merely-slow site (these URLs hang): a burst of
# concurrent fetches at one heavy/Cloudflare site would all time out and falsely
# trip a relaunch, tearing down the healthy sibling pages. One slow page is
# normal; a streak is the cue to *check*, not to assume the worst.
_TIMEOUT_RELAUNCH_THRESHOLD = 3

# `about:blank` loads in <100ms on a healthy browser and has no network/site/
# captcha to be slow on, so it's a clean "is this browser process responsive?"
# probe: success means the timeouts are the site (don't relaunch), a hang means
# the browser is genuinely wedged (relaunch). The whole probe is bounded so a
# `new_page` that itself hangs can't exceed this ceiling.
_PROBE_TIMEOUT = 2.0


async def _serve_fetch(
    supervisor: _BrowserSupervisor,
    *,
    url: str,
    mobile: bool,
    timeout: float,
    netblock: frozenset[str] | None = None,
    solve_turnstile: bool = False,
    manual_solve: bool = False,
    manual_solve_timeout: float = 60.0,
    block_images: bool = False,
    clear_cookies_on_wall: bool = False,
) -> tuple[str, int, dict[str, str]]:
    """Fetch a page, relaunching the browser once if the driver connection drops.

    The persistent browser is a single point of failure: a renderer crash or a
    suspend/resume cycle leaves the driver pipe dead, after which every fetch
    fails until the process restarts. We detect that here and relaunch in-place.
    """
    # Bound concurrent open pages: with the creation lock this is defense-in-depth
    # against memory spikes, not the deadlock fix. Acquired around the whole
    # attempt loop so a relaunch+retry still counts as one in-flight page.
    async with _PAGE_SEMAPHORE:
        last_exc: Exception | None = None
        for attempt in (0, 1):
            browser = await supervisor.get_browser()
            try:
                result = await _fetch_page(
                    browser,
                    url,
                    mobile=mobile,
                    timeout=timeout,
                    is_persistent=supervisor.is_persistent,
                    netblock=netblock,
                    solve_turnstile=solve_turnstile,
                    manual_solve=manual_solve,
                    manual_solve_timeout=manual_solve_timeout,
                    block_images=block_images,
                    clear_cookies_on_wall=clear_cookies_on_wall,
                )
            except Exception as exc:
                last_exc = exc
                if attempt == 0 and _is_disconnect(exc):
                    log.warning("browser fetch failed (%s) — relaunching browser", exc)
                    await supervisor.mark_dead()
                    continue
                if attempt == 0 and _is_timeout(exc):
                    streak = supervisor.note_timeout()
                    if streak >= _TIMEOUT_RELAUNCH_THRESHOLD:
                        # Consume the streak either way; the probe decides whether
                        # this is a real wedge or just a run of slow-site fetches.
                        supervisor.reset_timeouts()
                        if await supervisor.is_wedged():
                            log.warning(
                                "browser wedged (liveness probe failed after %d× "
                                "timeouts) — relaunching",
                                streak,
                            )
                            await supervisor.mark_dead()
                            continue
                        log.info(
                            "browser healthy (about:blank ok) after %d× site "
                            "timeouts — not relaunching",
                            streak,
                        )
                raise
            else:
                supervisor.reset_timeouts()
                return result
        assert last_exc is not None
        raise last_exc


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    supervisor: _BrowserSupervisor,
    netblock: frozenset[str] | None = None,
    solve_turnstile: bool = False,
    manual_solve: bool = False,
    manual_solve_timeout: float = 60.0,
    block_images: bool = False,
    clear_cookies_on_wall: bool = False,
) -> None:
    try:
        while True:
            try:
                req = await _read_msg(reader)
            except (asyncio.IncompleteReadError, ConnectionResetError):
                break
            if req is None:
                break

            url = req.get("url", "")
            mobile = req.get("mobile", False)
            timeout = req.get("timeout", 30.0)

            try:
                html, status, headers = await _serve_fetch(
                    supervisor,
                    url=url,
                    mobile=mobile,
                    timeout=timeout,
                    netblock=netblock,
                    solve_turnstile=solve_turnstile,
                    manual_solve=manual_solve,
                    manual_solve_timeout=manual_solve_timeout,
                    block_images=block_images,
                    clear_cookies_on_wall=clear_cookies_on_wall,
                )
                await _write_msg(
                    writer, {"html": html, "status": status, "headers": headers}
                )
            except Exception as exc:
                await _write_msg(writer, {"error": str(exc)})
    finally:
        writer.close()
        await writer.wait_closed()


async def _extract_headers(response: Any) -> dict[str, str]:
    """Best-effort response header extraction, with a fallback path."""
    if response is None:
        return {}
    try:
        raw = await response.all_headers()
        return {str(k): str(v) for k, v in raw.items()}
    except Exception:
        try:
            return {str(k): str(v) for k, v in (response.headers or {}).items()}
        except Exception:
            return {}


async def _install_route(
    page: Any,
    url: str,
    netblock: frozenset[str] | None,
    block_images: bool,
) -> None:
    """Install a `page.route` handler that aborts image requests (when
    `block_images`) and/or third-party tracker requests (when `netblock`).

    `block_images` lives here rather than as a Camoufox launch pref so it can be
    *suspended at runtime* (`page.unroute`) — a human solving an image-based
    captcha needs the puzzle image to render (see `_enable_images_for_solve`). For
    netblock, first-party requests (same registered domain as `url`) always pass,
    so a page's own resources are never blocked. Both checks are O(1); interception
    errors are swallowed so they can never kill a fetch.
    """
    page_domain = registered_domain(url)
    nb = netblock or frozenset()

    async def _route(route: Any) -> None:
        try:
            req = route.request
            if block_images and req.resource_type == "image":
                await route.abort()
                return
            if nb and should_block(req.url, page_domain, nb):
                await route.abort()
                return
            await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    await page.route("**/*", _route)


async def _enable_images_for_solve(
    page: Any, url: str, deadline_monotonic: float
) -> None:
    """Drop the image-block route and reload so a human can solve an image-based
    captcha (the puzzle image is aborted on the image-blocked first load). Reuses
    `block_images`'s route-handler design — engine-level blocking couldn't be
    undone at runtime. Best-effort; never raises (a failed reload just leaves the
    image-less page for the human, no worse than before)."""
    try:
        await page.unroute("**/*")
    except Exception:
        pass
    try:
        # Generous floor: this is the human-solve path (budget-suspended), and a
        # near-exhausted caller deadline shouldn't starve the reload.
        timeout = max(8000, _remaining_ms(deadline_monotonic))
        await page.reload(wait_until="domcontentloaded", timeout=timeout)
    except Exception as exc:
        log.info("image-enable reload for manual solve failed: %s", exc)


# --- Cloudflare Turnstile solve -----------------------------------------------
# Selectors for the challenge iframe (host is contractually stable across all
# Cloudflare customers) and the checkbox inside it. Loose on purpose: the inner
# class names rotate, the iframe host and the checkbox role don't.
_CF_IFRAME_SELECTOR = "iframe[src*='challenges.cloudflare.com']"
_CF_CHECKBOX_SELECTOR = "input[type='checkbox']"
_CF_CLICK_TIMEOUT_MS = 5000  # per click attempt, also clamped to the deadline
_CF_POLL_INTERVAL = 0.5  # clearance re-check cadence


def _remaining_ms(deadline_monotonic: float) -> int:
    return int(max(0.0, deadline_monotonic - time.monotonic()) * 1000)


def _looks_challenged(status: int, html: str, headers: dict[str, str]) -> bool:
    """True when the response classifies as a live Cloudflare/captcha challenge.

    Reuses the maintained `bot_detect.classify` so detection tracks the same
    markers the fetch chain already trusts — no second copy of the signatures.
    """
    return bot_detect.classify(status, html, headers) in _CHALLENGE_REASONS


async def _click_turnstile(page: Any, deadline_monotonic: float) -> None:
    """Best-effort click of the Turnstile checkbox. Never raises.

    Tries the checkbox inside the Cloudflare iframe first (needs disable_coop for
    the cross-origin reach), then falls back to a humanized click near the
    widget's left edge (where the checkbox sits) via the iframe bounding box.
    """
    timeout = min(_CF_CLICK_TIMEOUT_MS, _remaining_ms(deadline_monotonic))
    if timeout <= 0:
        return
    try:
        checkbox = page.frame_locator(_CF_IFRAME_SELECTOR).locator(
            _CF_CHECKBOX_SELECTOR
        )
        await checkbox.click(timeout=timeout)
        return
    except Exception:
        pass
    try:
        el = await page.query_selector(_CF_IFRAME_SELECTOR)
        box = await el.bounding_box() if el is not None else None
        if box:
            # The checkbox sits ~30px from the widget's left, vertically centered.
            await page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
    except Exception:
        pass


async def _wait_for_clearance(page: Any, deadline_monotonic: float) -> bool:
    """Poll until the challenge markers are gone (clearance), bounded by deadline.

    Returns True once the page no longer classifies as challenged — i.e. the real
    origin content rendered (and, with a persistent profile, cf_clearance landed).
    """
    while _remaining_ms(deadline_monotonic) > 0:
        try:
            html = await page.content()
        except Exception:
            return False
        if not _looks_challenged(200, html, {}):
            return True
        await asyncio.sleep(_CF_POLL_INTERVAL)
    return False


async def _maybe_solve_turnstile(
    page: Any,
    *,
    status: int,
    html: str,
    headers: dict[str, str],
    deadline_monotonic: float,
    url: str = "",
    solve_turnstile: bool = True,
    manual_solve: bool = False,
    manual_solve_timeout: float = 60.0,
    block_images: bool = False,
) -> bool:
    """If the current page is a Cloudflare challenge, try to clear it. Returns
    True only when the challenge cleared. Never raises — a failed solve leaves the
    challenge HTML in place so the chain still reports BLOCKED_CAPTCHA, exactly as
    before this feature existed.

    Order: auto-click (when `solve_turnstile`), then — if that didn't clear and
    `manual_solve` is on — notify the user and hold the page for a human to solve
    via VNC (budget-suspended, up to `manual_solve_timeout`). When `block_images`
    is on, images are re-enabled (and the page reloaded) just before the human
    hold, so an image-based captcha (e.g. a slider's puzzle) actually renders.
    """
    if not _looks_challenged(status, html, headers):
        return False
    try:
        if solve_turnstile:
            # Managed challenges often pass non-interactively on a good
            # fingerprint; the click is the fallback for the interactive checkbox
            # a human also has to tick.
            await _click_turnstile(page, deadline_monotonic)
            if await _wait_for_clearance(page, deadline_monotonic):
                return True
        if manual_solve:
            if block_images:
                await _enable_images_for_solve(page, url, deadline_monotonic)
            return await _manual_solve_hold(page, url, manual_solve_timeout)
        return False
    except Exception as exc:  # defensive: a solve must never kill the fetch
        log.info("turnstile solve attempt failed: %s", exc)
        return False


# --- Persistent-profile login-wall recovery -----------------------------------
# A shared persistent profile can accumulate session state that flips a site into
# a login/account wall (e.g. MercadoLivre's `/gz/account-verification`
# interstitial). Clearing *that domain's* cookies resets it to a fresh anonymous
# session, which usually clears the wall — and heals the profile for later fetches
# too. Scoped to the page's registered domain so sibling sites' clearances (e.g.
# AliExpress x5secdata) are preserved.
_COOKIE_CLEAR_COOLDOWN = (
    120.0  # s: don't re-clear a domain's cookies within this window
)
_last_cookie_clear: dict[str, float] = {}  # registered_domain -> monotonic ts


async def _maybe_recover_login_wall(
    page: Any,
    url: str,
    *,
    status: int,
    html: str,
    headers: dict[str, str],
    deadline_monotonic: float,
) -> tuple[str, int, dict[str, str]] | None:
    """If the current page is a login wall, clear this domain's cookies and
    re-fetch the URL once.

    Returns the recovered ``(html, status, headers)`` only when the retry no
    longer classifies as ``LOGIN_REQUIRED``; otherwise ``None`` (the wall is left
    in place, so the chain still reports LOGIN_REQUIRED). Never raises.

    Two guards make a ``wall → clear → wall`` thrash impossible:
      * **single-shot** — clears once and re-fetches once, no recursion/loop, so it
        runs at most once per ``fetch_page``;
      * a **per-domain cooldown** (``_COOKIE_CLEAR_COOLDOWN``) so a *persistent*
        wall (one cookie-clearing can't fix — e.g. IP/fingerprint-gated) can't make
        every later fetch re-clear; within the window the recovery is skipped.
    """
    if bot_detect.classify(status, html, headers) != FailureReason.LOGIN_REQUIRED:
        return None
    rd = registered_domain(url)
    now = time.monotonic()
    if now - _last_cookie_clear.get(rd, 0.0) < _COOKIE_CLEAR_COOLDOWN:
        log.info("login-wall recovery skipped for %s: cookies cleared recently", rd)
        return None  # cross-fetch loop guard — clearing didn't help last time
    _last_cookie_clear[rd] = now
    try:
        # Domain-scoped: the regex matches `rd`, `www.rd`, and `.rd` cookie
        # domains via search; never a context-wide wipe, so other sites keep theirs.
        await page.context.clear_cookies(domain=re.compile(re.escape(rd)))
    except Exception as exc:
        log.info("login-wall cookie clear failed for %s: %s", rd, exc)
        return None
    log.warning("login wall on %s — cleared %s cookies, retrying once", url, rd)
    if _remaining_ms(deadline_monotonic) <= 0:
        return None
    try:
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=_remaining_ms(deadline_monotonic),
        )
        settle_ms = min(
            _remaining_ms(deadline_monotonic), int(_NETWORKIDLE_SETTLE_CAP * 1000)
        )
        if settle_ms > 0:
            try:
                await page.wait_for_load_state("networkidle", timeout=settle_ms)
            except Exception:
                pass
        new_html = await page.content()
        new_status = int(response.status) if response is not None else status
        new_headers = await _extract_headers(response)
    except Exception as exc:
        log.info("login-wall retry navigation failed for %s: %s", url, exc)
        return None
    if (
        bot_detect.classify(new_status, new_html, new_headers)
        == FailureReason.LOGIN_REQUIRED
    ):
        return None  # still walled — give up; chain surfaces LOGIN_REQUIRED
    log.info("login wall cleared for %s", url)
    return new_html, new_status, new_headers


async def fetch_page(
    browser_or_context: Any,
    url: str,
    *,
    deadline_monotonic: float,
    mobile: bool = False,
    is_persistent: bool = False,
    netblock: frozenset[str] | None = None,
    solve_turnstile: bool = False,
    manual_solve: bool = False,
    manual_solve_timeout: float = 60.0,
    block_images: bool = False,
    clear_cookies_on_wall: bool = False,
) -> tuple[str, int, dict[str, str]]:
    """Open a page, navigate to `url`, and return (html, status, headers).

    Honours `deadline_monotonic` (an absolute ``time.monotonic()`` value) for
    both the navigation and the networkidle settle. When `netblock` is non-empty
    or `block_images` is set, a `page.route` handler aborts the matching requests
    (third-party trackers / images respectively).
    """
    context = None
    # Serialize page/context creation: concurrent `new_page`/`new_context` on one
    # shared Camoufox deadlock it. The lock is held only for creation — navigation
    # below runs concurrently, so throughput is unaffected.
    async with _CREATE_LOCK:
        if mobile and not is_persistent:
            context = await browser_or_context.new_context(
                user_agent=_MOBILE_USER_AGENT,
                viewport=_MOBILE_VIEWPORT,
                device_scale_factor=3,
            )
            page = await context.new_page()
        else:
            page = await browser_or_context.new_page()
            if mobile:
                await page.set_extra_http_headers({"User-Agent": _MOBILE_USER_AGENT})
                await page.set_viewport_size(_MOBILE_VIEWPORT)
    try:
        if netblock or block_images:
            await _install_route(page, url, netblock, block_images)
        remaining_ms = int(max(0.0, deadline_monotonic - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            raise asyncio.TimeoutError("deadline elapsed before page.goto could start")
        response = await page.goto(
            url, wait_until="domcontentloaded", timeout=remaining_ms
        )

        remaining_ms = int(max(0.0, deadline_monotonic - time.monotonic()) * 1000)
        if remaining_ms > 0:
            # networkidle is load-bearing for the JS-heavy structured adapters
            # (e.g. MercadoLivre injects its JSON-LD @graph after hydration), but
            # an uncapped wait lets a never-settling ad/beacon-heavy page eat the
            # whole browser budget. Cap it: typical hydration lands inside the cap,
            # and `page.content()` below still captures whatever rendered even if
            # the settle times out — so a non-settling page returns its
            # domcontentloaded HTML at the cap instead of at the tier deadline.
            settle_ms = min(remaining_ms, int(_NETWORKIDLE_SETTLE_CAP * 1000))
            try:
                await page.wait_for_load_state("networkidle", timeout=settle_ms)
            except Exception:
                pass

        html = await page.content()
        status = int(response.status) if response is not None else 0
        headers = await _extract_headers(response)
        if (solve_turnstile or manual_solve) and await _maybe_solve_turnstile(
            page,
            status=status,
            html=html,
            headers=headers,
            deadline_monotonic=deadline_monotonic,
            url=url,
            solve_turnstile=solve_turnstile,
            manual_solve=manual_solve,
            manual_solve_timeout=manual_solve_timeout,
            block_images=block_images,
        ):
            # Cleared: re-read the now-rendered origin content. Report 200 — the
            # challenge response's status/markers no longer describe the page we
            # hold, and a stale 403 would re-trip the chain's bot classifier.
            html = await page.content()
            status = 200
        # A persistent profile can accumulate state that flips a site into a login
        # wall; clear that domain's cookies and retry once (single-shot, cooldown-
        # guarded). Only meaningful on the shared persistent context.
        if clear_cookies_on_wall and is_persistent:
            recovered = await _maybe_recover_login_wall(
                page,
                url,
                status=status,
                html=html,
                headers=headers,
                deadline_monotonic=deadline_monotonic,
            )
            if recovered is not None:
                html, status, headers = recovered
        return html, status, headers
    finally:
        try:
            await page.close()
        except Exception:
            pass
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


async def _fetch_page(
    browser: Any,
    url: str,
    *,
    mobile: bool = False,
    timeout: float = 30.0,
    is_persistent: bool = False,
    netblock: frozenset[str] | None = None,
    solve_turnstile: bool = False,
    manual_solve: bool = False,
    manual_solve_timeout: float = 60.0,
    block_images: bool = False,
    clear_cookies_on_wall: bool = False,
) -> tuple[str, int, dict[str, str]]:
    """Thin wrapper over `fetch_page`. Kept as a stable seam the request handler
    calls and the server tests monkeypatch."""
    return await fetch_page(
        browser,
        url,
        deadline_monotonic=time.monotonic() + timeout,
        mobile=mobile,
        is_persistent=is_persistent,
        netblock=netblock,
        solve_turnstile=solve_turnstile,
        manual_solve=manual_solve,
        manual_solve_timeout=manual_solve_timeout,
        block_images=block_images,
        clear_cookies_on_wall=clear_cookies_on_wall,
    )


def _build_launch_kwargs(cfg: Any | None) -> tuple[dict[str, Any], bool]:
    """Resolve Camoufox launch kwargs and whether we run a persistent context."""
    headless: bool | str = True
    locale = "en-US"
    user_data_dir = ""
    # Turnstile-solving knobs; read with getattr so a partial cfg/namespace (e.g.
    # a test SimpleNamespace) falls back per-field instead of dropping the lot.
    virtual_display = False
    humanize = False
    disable_coop = False
    window: tuple[int, ...] = ()
    if cfg is not None:
        try:
            headless = bool(cfg.browser.headless)
            locale = str(cfg.browser.locale)
            user_data_dir = str(cfg.browser.user_data_dir or "")
        except Exception:
            pass
        b = getattr(cfg, "browser", None)
        if b is not None:
            virtual_display = bool(getattr(b, "virtual_display", False))
            humanize = bool(getattr(b, "humanize", False))
            disable_coop = bool(getattr(b, "disable_coop", False))
            try:
                window = tuple(int(x) for x in (getattr(b, "window", ()) or ()))[:2]
            except (TypeError, ValueError):
                window = ()

    if user_data_dir:
        if "XDG_DATA_HOME" not in os.environ:
            xdg = str(Path.home() / ".local" / "share")
            user_data_dir = user_data_dir.replace("${XDG_DATA_HOME}", xdg).replace(
                "$XDG_DATA_HOME", xdg
            )
        user_data_dir = os.path.abspath(
            os.path.expanduser(os.path.expandvars(user_data_dir))
        )

    # virtual_display launches a real (non-headless) Firefox inside Xvfb; it wins
    # over the bool `headless` because clicking the Turnstile checkbox needs a
    # genuine (not headless) browser, and Xvfb keeps that headless-server-safe.
    kwargs: dict[str, Any] = {
        "headless": "virtual" if virtual_display else headless,
        "locale": (locale,),
    }
    if humanize:
        kwargs["humanize"] = True
    if disable_coop:
        kwargs["disable_coop"] = True
    # NB: block_images is intentionally NOT a launch pref. It's applied per-page in
    # `_install_route` so a manual captcha solve can re-enable images at runtime
    # (an engine-level pref can't be undone mid-session). See `fetch_page`.
    if len(window) == 2:
        kwargs["window"] = window
    is_persistent = bool(user_data_dir)
    if is_persistent:
        os.makedirs(user_data_dir, exist_ok=True)
        kwargs["persistent_context"] = True
        kwargs["user_data_dir"] = user_data_dir
    return kwargs, is_persistent


def _force_x11_for_virtual_display() -> None:
    """Pin the browser to X11 so a virtual-display launch actually uses Xvfb.

    Firefox prefers the Wayland backend whenever ``WAYLAND_DISPLAY`` is set — and a
    ``systemctl --user`` service inherits it (plus ``DISPLAY``) from the graphical
    session. With it set, ``headless="virtual"`` still starts an Xvfb, but Firefox
    renders to the *real* Wayland compositor instead, popping visible browser
    windows on the user's desktop (Xvfb only isolates the X11 path, not Wayland).
    The server itself never needs a display, so scrub both display vars (Camoufox
    sets its own ``DISPLAY=:N`` for the Xvfb it creates) and force the X11 backend.
    Bonus: if the Xvfb ever fails to start, the browser then can't fall back to the
    real desktop either — it just fails to launch.
    """
    for var in ("WAYLAND_DISPLAY", "DISPLAY"):
        os.environ.pop(var, None)
    os.environ["MOZ_ENABLE_WAYLAND"] = "0"


# --- Managed display for manual (VNC) solving ---------------------------------
# When manual_solve is on, vasco runs its OWN sized Xvfb (Camoufox's built-in
# headless="virtual" display is 1x1 — fine for headless scraping, useless for
# VNC) plus an x11vnc server on loopback, so a human can connect and solve a
# challenge the auto-solver can't. The port is stashed module-side so the notify
# message can name it without threading it through the whole fetch path.
_VNC_PORT = 5900


def _pick_free_display(preferred: str) -> str:
    """Return a free X display string (e.g. ':99'), starting from `preferred`.

    Probes the abstract/unix socket path Xvfb creates; an X server that owns a
    display leaves `/tmp/.X11-unix/X<n>`. Falls back to scanning upward so we
    never collide with an existing server (incl. the user's real :0)."""
    try:
        start = int(preferred.lstrip(":")) if preferred else 99
    except ValueError:
        start = 99
    for n in range(max(start, 1), max(start, 1) + 64):
        if not os.path.exists(f"/tmp/.X11-unix/X{n}"):
            return f":{n}"
    return f":{start}"


def _start_managed_display(
    preferred_display: str, size: tuple[int, int], port: int
) -> tuple[str, subprocess.Popen, subprocess.Popen | None]:
    """Start a sized Xvfb + a loopback x11vnc on it. Returns (display, xvfb, vnc).

    Synchronous (called once at startup via ``asyncio.to_thread``). The x11vnc
    handle may be None if it fails to start — VNC is best-effort; the sized Xvfb
    is what the browser needs, VNC is only how the human views it.
    """
    display = _pick_free_display(preferred_display)
    w, h = size
    xvfb = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", f"{w}x{h}x24", "-ac", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait for the display socket so the browser/x11vnc don't race a cold Xvfb.
    sock = f"/tmp/.X11-unix/X{display.lstrip(':')}"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not os.path.exists(sock):
        time.sleep(0.1)

    vnc: subprocess.Popen | None = None
    try:
        vnc = subprocess.Popen(
            [
                "x11vnc",
                "-display",
                display,
                "-localhost",
                "-forever",
                "-shared",
                "-nopw",
                "-rfbport",
                str(port),
                "-quiet",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # x11vnc missing / failed — keep the Xvfb anyway
        log.warning("x11vnc failed to start (%s); manual solve has no viewer", exc)
    return display, xvfb, vnc


async def _notify_manual_solve(url: str) -> None:
    """Fire a desktop notification that a captcha needs a human. Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "notify-send",
            "-u",
            "critical",
            "vasco: solve captcha",
            f"{url}\nConnect a VNC viewer to localhost:{_VNC_PORT} to solve.",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception:
        pass


# Only one manual hold at a time: a burst of challenged fetches must not each pop
# a notification or each pin a page-semaphore slot for a minute. Guarded by the
# event loop's single-threadedness (no await between the check and the set).
_manual_in_progress = False


async def _manual_solve_hold(page: Any, url: str, timeout: float) -> bool:
    """Notify the user and hold the page open for a human to solve, budget-suspended.

    Returns True only if the challenge clears within `timeout`. Concurrent calls
    while one hold is active return False immediately (resume as normal)."""
    global _manual_in_progress
    if _manual_in_progress:
        log.info("manual solve already in progress; not holding for %s", url)
        return False
    _manual_in_progress = True
    try:
        log.warning(
            "manual solve: holding %s up to %.0fs for a human (VNC localhost:%d)",
            url,
            timeout,
            _VNC_PORT,
        )
        await _notify_manual_solve(url)
        # Own clock, independent of the caller's fetch deadline — this is the
        # "budget suspension": the page stays open for the human window.
        return await _wait_for_clearance(page, time.monotonic() + timeout)
    finally:
        _manual_in_progress = False


def _kill_browser_processes() -> None:
    """SIGKILL the browser subprocess tree when a graceful close hangs/errors.

    Best-effort and dependency-free (Linux ``/proc``, no psutil): walk ``/proc``
    to find every descendant of this process and SIGKILL it. The browser server
    is the only thing here that spawns subprocesses, so its descendants are
    exactly the Playwright node driver + ``camoufox-bin`` + content-process tree.
    All errors are swallowed — this is a last-resort reclaim after a wedged close.
    """
    import signal

    self_pid = os.getpid()
    try:
        pids = [int(e) for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return

    children: dict[int, list[int]] = {}
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                data = fh.read()
            # comm (field 2) is parenthesized and may contain spaces/')'; the ppid
            # is the 2nd whitespace token after the final ')'.
            ppid = int(data[data.rfind(b")") + 2 :].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(pid)

    descendants: list[int] = []
    queue = list(children.get(self_pid, []))
    seen: set[int] = set()
    while queue:
        pid = queue.pop()
        if pid in seen or pid == self_pid:
            continue
        seen.add(pid)
        descendants.append(pid)
        queue.extend(children.get(pid, []))

    for pid in descendants:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if descendants:
        log.warning("force-killed %d wedged browser process(es)", len(descendants))


class _BrowserSupervisor:
    """Owns the long-lived Camoufox browser and relaunches it when it dies.

    A single browser serving every consumer is a single point of failure: a
    renderer crash (some sites OOM or crash the tab) or a suspend/resume cycle
    leaves the driver pipe dead, after which `is_connected()` reports False and
    every fetch fails until restart. `get_browser` lazily relaunches on demand;
    `mark_dead` lets a caller force a relaunch after a disconnect error.
    """

    def __init__(self, kwargs: dict[str, Any], is_persistent: bool) -> None:
        self._kwargs = kwargs
        self.is_persistent = is_persistent
        self._cm: Any | None = None
        self._browser: Any | None = None
        self._lock = asyncio.Lock()
        self._consecutive_timeouts = 0
        self._handouts = 0  # pages served since the last (re)launch; drives recycle

    def note_timeout(self) -> int:
        """Record a goto timeout; return the current consecutive-timeout streak."""
        self._consecutive_timeouts += 1
        return self._consecutive_timeouts

    def reset_timeouts(self) -> None:
        self._consecutive_timeouts = 0

    def _alive(self) -> bool:
        b = self._browser
        if b is None:
            return False
        # Browser exposes is_connected(); a persistent BrowserContext does not,
        # so probe its underlying .browser, and assume alive if neither is known.
        for obj in (b, getattr(b, "browser", None)):
            probe = getattr(obj, "is_connected", None)
            if callable(probe):
                try:
                    return bool(probe())
                except Exception:
                    return False
        return True

    async def _launch_locked(self) -> None:
        from camoufox.async_api import AsyncCamoufox

        try:
            self._cm = AsyncCamoufox(**self._kwargs)
            self._browser = await self._cm.__aenter__()
        except Exception as exc:
            # Persistent profile still locked by the dying process — fall back to
            # an ephemeral browser so the server keeps serving.
            if self.is_persistent and "already running" in str(exc).lower():
                log.warning("persistent profile locked — relaunching ephemeral")
                fallback = {
                    k: v
                    for k, v in self._kwargs.items()
                    if k not in ("persistent_context", "user_data_dir")
                }
                self.is_persistent = False
                self._cm = AsyncCamoufox(**fallback)
                self._browser = await self._cm.__aenter__()
            else:
                self._cm = None
                self._browser = None
                raise

    async def _close_locked(self) -> None:
        if self._cm is not None:
            try:
                await asyncio.wait_for(
                    self._cm.__aexit__(None, None, None), timeout=_CLOSE_TIMEOUT
                )
            except Exception:
                # Graceful close hung or errored — force-kill the browser tree so a
                # wedged close can't deadlock the supervisor lock or leak content
                # processes (~930MB each).
                _kill_browser_processes()
        self._cm = None
        self._browser = None

    async def start(self) -> None:
        async with self._lock:
            await self._launch_locked()

    async def get_browser(self) -> Any:
        # Fast path: alive and not due for recycle. Fully synchronous (no await),
        # so concurrent callers can't interleave the handout increment.
        if self._alive() and self._handouts < _RECYCLE_AFTER_PAGES:
            self._handouts += 1
            return self._browser
        async with self._lock:
            if self._alive() and self._handouts < _RECYCLE_AFTER_PAGES:
                self._handouts += 1
                return self._browser
            if self._alive() and self._handouts >= _RECYCLE_AFTER_PAGES:
                log.info("recycling browser after %d pages", self._handouts)
            else:
                log.warning("browser dead/disconnected — relaunching")
            await self._close_locked()
            await self._launch_locked()
            self._handouts = 1
            return self._browser

    async def mark_dead(self) -> None:
        # De-storm: a burst of concurrent failures all call mark_dead, but only
        # the first should relaunch. Capture the browser we observed; if another
        # coroutine already swapped it (cm changed or cleared) by the time we hold
        # the lock, this is a no-op — which also stops a *late* mark_dead from
        # tearing down a freshly relaunched browser (the re-wedge loop).
        stale = self._cm
        async with self._lock:
            if self._cm is None or self._cm is not stale:
                return
            await self._close_locked()

    async def is_wedged(self) -> bool:
        """Probe browser responsiveness with an `about:blank` navigation.

        Returns True only when the probe fails (no browser, hang, or error) —
        a genuine wedge that warrants a relaunch. A fast success means the
        browser is fine and the caller's timeouts are the *site*, so the slow
        fetch's timeout should just propagate without tearing the browser (and
        its healthy sibling pages) down.

        Deliberately does NOT take `self._lock` (held by mark_dead/get_browser/
        _close_locked — holding it here would block the very relaunch we might
        ask for). It only needs `_CREATE_LOCK` to serialize page creation, the
        same invariant `fetch_page` honours, and reads `self._browser` directly.
        """
        browser = self._browser
        if browser is None:
            return True

        async def _probe() -> bool:
            async with _CREATE_LOCK:
                page = await browser.new_page()
            try:
                await page.goto("about:blank", timeout=int(_PROBE_TIMEOUT * 1000))
                return True
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

        try:
            # Outer guard slightly larger than the goto timeout, in case the
            # `new_page` itself hangs before goto can start.
            return not await asyncio.wait_for(_probe(), timeout=_PROBE_TIMEOUT + 0.5)
        except Exception:
            return True

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()


async def run_server(cfg: Any | None = None) -> None:
    try:
        import camoufox.async_api  # noqa: F401
    except ImportError:
        log.error("camoufox is not installed")
        return

    global _VNC_PORT
    _patch_playwright_driver()
    kwargs, is_persistent = _build_launch_kwargs(cfg)

    # Resolve config once at startup.
    block_trackers = True
    network_blocklist_paths: tuple[str, ...] = ()
    solve_turnstile = False
    manual_solve = False
    manual_solve_timeout = 60.0
    block_images = False
    clear_cookies_on_wall = True  # only acts on a persistent profile + login wall
    vnc_display = ":99"
    vnc_size = (1280, 720)
    if cfg is not None:
        try:
            block_trackers = bool(cfg.browser.block_trackers)
            network_blocklist_paths = tuple(cfg.browser.network_blocklist_paths)
        except Exception:
            pass
        b = getattr(cfg, "browser", None)
        solve_turnstile = bool(getattr(b, "solve_turnstile", False))
        manual_solve = bool(getattr(b, "manual_solve", False))
        block_images = bool(getattr(b, "block_images", False))
        clear_cookies_on_wall = bool(getattr(b, "clear_cookies_on_wall", True))
        try:
            manual_solve_timeout = float(getattr(b, "manual_solve_timeout", 60.0))
            _VNC_PORT = int(getattr(b, "vnc_port", 5900))
            vnc_display = str(getattr(b, "vnc_display", ":99") or ":99")
            vnc_size = tuple(
                int(x) for x in (getattr(b, "vnc_display_size", ()) or ())
            )[:2] or (1280, 720)
        except (TypeError, ValueError):
            pass

    # Display setup. manual_solve needs a *viewable* display (Camoufox's built-in
    # virtual display is 1x1), so vasco runs its own sized Xvfb + x11vnc and points
    # the browser at it; this also keeps it off the real (Wayland) desktop.
    managed: tuple[subprocess.Popen, subprocess.Popen | None] | None = None
    if manual_solve:
        # Scrub WAYLAND_DISPLAY *before* spawning x11vnc — x11vnc refuses to run if
        # it sees a Wayland session env ("only supported via -rawfb"), even when its
        # -display target is a real X server. Then point the browser at our Xvfb.
        _force_x11_for_virtual_display()  # pops WAYLAND_DISPLAY/DISPLAY, MOZ=0
        display, xvfb, vnc = await asyncio.to_thread(
            _start_managed_display, vnc_display, vnc_size, _VNC_PORT
        )
        os.environ["DISPLAY"] = display  # ...then point at our sized Xvfb
        kwargs["headless"] = False  # headful so the page renders into the Xvfb
        kwargs.pop("virtual_display", None)
        managed = (xvfb, vnc)
        log.info(
            "manual-solve mode: Xvfb %s (%dx%d) + x11vnc on localhost:%d",
            display,
            vnc_size[0],
            vnc_size[1],
            _VNC_PORT,
        )
    elif kwargs.get("headless") == "virtual":
        # Must run before Camoufox launches: keep the headful Xvfb browser off the
        # real (Wayland) desktop. See _force_x11_for_virtual_display.
        _force_x11_for_virtual_display()

    netblock = await asyncio.to_thread(
        load_netblock, block_trackers, network_blocklist_paths
    )
    if netblock:
        log.info("tracker blocking enabled (%d domains)", len(netblock))
    if block_images:
        log.info("image blocking enabled (per-page; re-enabled during a manual solve)")
    if solve_turnstile:
        log.info("cloudflare turnstile solving enabled")
    if clear_cookies_on_wall and is_persistent:
        log.info("login-wall cookie-clear recovery enabled")

    sock = _socket_path()
    sock.parent.mkdir(parents=True, exist_ok=True)
    if sock.exists():
        sock.unlink()

    locale = kwargs.get("locale", ("en-US",))
    log.info("launching camoufox (locale=%s, persistent=%s)", locale, is_persistent)
    supervisor = _BrowserSupervisor(kwargs, is_persistent)
    await supervisor.start()
    try:
        server = await asyncio.start_unix_server(
            lambda r, w: _handle_client(
                r,
                w,
                supervisor,
                netblock,
                solve_turnstile,
                manual_solve,
                manual_solve_timeout,
                block_images,
                clear_cookies_on_wall,
            ),
            path=str(sock),
        )
        os.chmod(str(sock), 0o600)
        log.info("browser server listening on %s", sock)
        try:
            await server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            server.close()
            await server.wait_closed()
    finally:
        await supervisor.close()
        if managed:
            for proc in managed:
                if proc is not None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
        if sock.exists():
            sock.unlink()
        log.info("browser server stopped")
