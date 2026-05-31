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

log = logging.getLogger(__name__)

_HEADER = struct.Struct("!I")


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


async def _serve_fetch(
    supervisor: _BrowserSupervisor,
    *,
    url: str,
    mobile: bool,
    timeout: float,
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
            return await _fetch_page(
                browser,
                url,
                mobile=mobile,
                timeout=timeout,
                is_persistent=supervisor.is_persistent,
            )
        except Exception as exc:
            last_exc = exc
            if attempt == 0 and _is_disconnect(exc):
                log.warning("browser fetch failed (%s) — relaunching browser", exc)
                await supervisor.mark_dead()
                continue
            raise
    assert last_exc is not None
    raise last_exc


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    supervisor: _BrowserSupervisor,
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
                    supervisor, url=url, mobile=mobile, timeout=timeout
                )
                await _write_msg(
                    writer, {"html": html, "status": status, "headers": headers}
                )
            except Exception as exc:
                await _write_msg(writer, {"error": str(exc)})
    finally:
        writer.close()
        await writer.wait_closed()


_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 "
    "Mobile/15E148 Safari/604.1"
)
_MOBILE_VIEWPORT = {"width": 393, "height": 852}


async def _fetch_page(
    browser: Any,
    url: str,
    *,
    mobile: bool = False,
    timeout: float = 30.0,
    is_persistent: bool = False,
) -> tuple[str, int, dict[str, str]]:
    deadline = time.monotonic() + timeout

    context = None
    if mobile and not is_persistent:
        context = await browser.new_context(
            user_agent=_MOBILE_USER_AGENT,
            viewport=_MOBILE_VIEWPORT,
            device_scale_factor=3,
        )
        page = await context.new_page()
    else:
        page = await browser.new_page()
        if mobile:
            await page.set_extra_http_headers({"User-Agent": _MOBILE_USER_AGENT})
            await page.set_viewport_size(_MOBILE_VIEWPORT)
    try:
        remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
        response = await page.goto(
            url, wait_until="domcontentloaded", timeout=remaining_ms
        )

        remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
        if remaining_ms > 0:
            try:
                await page.wait_for_load_state("networkidle", timeout=remaining_ms)
            except Exception:
                pass

        html = await page.content()
        status = int(response.status) if response is not None else 0
        headers: dict[str, str] = {}
        if response is not None:
            try:
                raw = await response.all_headers()
                headers = {str(k): str(v) for k, v in raw.items()}
            except Exception:
                pass
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

    kwargs, is_persistent = _build_launch_kwargs(cfg)

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
            lambda r, w: _handle_client(r, w, supervisor),
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
