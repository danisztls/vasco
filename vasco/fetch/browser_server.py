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


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    browser: Any,
    is_persistent: bool,
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
                html, status, headers = await _fetch_page(
                    browser,
                    url,
                    mobile=mobile,
                    timeout=timeout,
                    is_persistent=is_persistent,
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


async def run_server(cfg: Any | None = None) -> None:
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError:
        log.error("camoufox is not installed")
        return

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
        user_data_dir = os.path.abspath(
            os.path.expanduser(os.path.expandvars(user_data_dir))
        )

    kwargs: dict[str, Any] = {"headless": headless, "locale": (locale,)}
    is_persistent = bool(user_data_dir)
    if is_persistent:
        os.makedirs(user_data_dir, exist_ok=True)
        kwargs["persistent_context"] = True
        kwargs["user_data_dir"] = user_data_dir

    sock = _socket_path()
    sock.parent.mkdir(parents=True, exist_ok=True)
    if sock.exists():
        sock.unlink()

    log.info("launching camoufox (locale=%s, persistent=%s)", locale, is_persistent)
    async with AsyncCamoufox(**kwargs) as browser:
        server = await asyncio.start_unix_server(
            lambda r, w: _handle_client(r, w, browser, is_persistent),
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
            if sock.exists():
                sock.unlink()
            log.info("browser server stopped")
