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
import struct
import time
from pathlib import Path
from typing import Any

from ..cache import registered_domain
from .netblock import load_netblock, should_block

log = logging.getLogger(__name__)

_HEADER = struct.Struct("!I")

_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 "
    "Mobile/15E148 Safari/604.1"
)
_MOBILE_VIEWPORT = {"width": 393, "height": 852}


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
# After this many *consecutive* goto timeouts we treat the browser as wedged
# and force a relaunch. One slow page is normal; a streak across requests is not.
_TIMEOUT_RELAUNCH_THRESHOLD = 3


async def _serve_fetch(
    supervisor: _BrowserSupervisor,
    *,
    url: str,
    mobile: bool,
    timeout: float,
    netblock: frozenset[str] | None = None,
) -> tuple[str, int, dict[str, str]]:
    """Fetch a page, relaunching the browser once if the driver connection drops.

    The persistent browser is a single point of failure: a renderer crash or a
    suspend/resume cycle leaves the driver pipe dead, after which every fetch
    fails until the process restarts. We detect that here and relaunch in-place.
    """
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
                    log.warning(
                        "browser timed out %d× consecutively — wedged, relaunching",
                        streak,
                    )
                    await supervisor.mark_dead()
                    supervisor.reset_timeouts()
                    continue
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


async def _install_netblock_route(
    page: Any, url: str, netblock: frozenset[str]
) -> None:
    """Install a `page.route` handler that aborts third-party tracker requests.

    First-party requests (same registered domain as `url`) always pass, so a
    page's own resources are never blocked. The handler is an O(1) set membership
    test; interception errors are swallowed so they can never kill a fetch.
    """
    page_domain = registered_domain(url)

    async def _route(route: Any) -> None:
        try:
            if should_block(route.request.url, page_domain, netblock):
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    await page.route("**/*", _route)


async def fetch_page(
    browser_or_context: Any,
    url: str,
    *,
    deadline_monotonic: float,
    mobile: bool = False,
    is_persistent: bool = False,
    netblock: frozenset[str] | None = None,
) -> tuple[str, int, dict[str, str]]:
    """Open a page, navigate to `url`, and return (html, status, headers).

    Honours `deadline_monotonic` (an absolute ``time.monotonic()`` value) for
    both the navigation and the networkidle settle. When `netblock` is
    non-empty, third-party tracker/ad requests are aborted via a `page.route`
    handler.
    """
    context = None
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
        if netblock:
            await _install_netblock_route(page, url, netblock)
        remaining_ms = int(max(0.0, deadline_monotonic - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            raise asyncio.TimeoutError("deadline elapsed before page.goto could start")
        response = await page.goto(
            url, wait_until="domcontentloaded", timeout=remaining_ms
        )

        remaining_ms = int(max(0.0, deadline_monotonic - time.monotonic()) * 1000)
        if remaining_ms > 0:
            try:
                await page.wait_for_load_state("networkidle", timeout=remaining_ms)
            except Exception:
                pass

        html = await page.content()
        status = int(response.status) if response is not None else 0
        headers = await _extract_headers(response)
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
    )


def _build_launch_kwargs(cfg: Any | None) -> tuple[dict[str, Any], bool]:
    """Resolve Camoufox launch kwargs and whether we run a persistent context."""
    headless = True
    locale = "en-US"
    user_data_dir = ""
    if cfg is not None:
        try:
            headless = bool(cfg.browser.headless)
            locale = str(cfg.browser.locale)
            user_data_dir = str(cfg.browser.user_data_dir or "")
        except Exception:
            pass

    if user_data_dir:
        if "XDG_DATA_HOME" not in os.environ:
            xdg = str(Path.home() / ".local" / "share")
            user_data_dir = user_data_dir.replace("${XDG_DATA_HOME}", xdg).replace(
                "$XDG_DATA_HOME", xdg
            )
        user_data_dir = os.path.abspath(
            os.path.expanduser(os.path.expandvars(user_data_dir))
        )

    kwargs: dict[str, Any] = {"headless": headless, "locale": (locale,)}
    is_persistent = bool(user_data_dir)
    if is_persistent:
        os.makedirs(user_data_dir, exist_ok=True)
        kwargs["persistent_context"] = True
        kwargs["user_data_dir"] = user_data_dir
    return kwargs, is_persistent


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
                await self._cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._cm = None
        self._browser = None

    async def start(self) -> None:
        async with self._lock:
            await self._launch_locked()

    async def get_browser(self) -> Any:
        if self._alive():
            return self._browser
        async with self._lock:
            if self._alive():
                return self._browser
            log.warning("browser dead/disconnected — relaunching")
            await self._close_locked()
            await self._launch_locked()
            return self._browser

    async def mark_dead(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()


async def run_server(cfg: Any | None = None) -> None:
    try:
        import camoufox.async_api  # noqa: F401
    except ImportError:
        log.error("camoufox is not installed")
        return

    _patch_playwright_driver()
    kwargs, is_persistent = _build_launch_kwargs(cfg)

    # Resolve the tracker blocklist once at startup; the handler then only does
    # an O(1) set membership test per request.
    block_trackers = True
    network_blocklist_paths: tuple[str, ...] = ()
    if cfg is not None:
        try:
            block_trackers = bool(cfg.browser.block_trackers)
            network_blocklist_paths = tuple(cfg.browser.network_blocklist_paths)
        except Exception:
            pass
    netblock = await asyncio.to_thread(
        load_netblock, block_trackers, network_blocklist_paths
    )
    if netblock:
        log.info("tracker blocking enabled (%d domains)", len(netblock))

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
            lambda r, w: _handle_client(r, w, supervisor, netblock),
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
        if sock.exists():
            sock.unlink()
        log.info("browser server stopped")
