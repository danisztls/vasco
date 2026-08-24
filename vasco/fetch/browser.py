# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Browser-tier client: proxies fetches to the persistent browser server.

The browser tier runs as a separate, long-lived peer service
(``vasco browser-server`` → ``vasco/fetch/browser_server.py``) that owns the
Camoufox process. This module is a thin client: ``BrowserPool`` connects to the
server's UNIX socket (``$XDG_RUNTIME_DIR/vasco/browser.sock``) and proxies fetch
requests to it.

There is **no in-process browser fallback** — camoufox is a dependency of the
server only, not of this module — so when the server isn't running the browser
tier raises ``BrowserServerUnavailable``, which the fetch chain turns into a
``BROWSER_UNAVAILABLE`` failure (and escalates to the next tier, wayback). All
browser configuration (locale, persistent profile, tracker blocking) lives with
the server; the client passes only the URL and a deadline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import time
from typing import Any

from ..errors import BrowserServerUnavailable

log = logging.getLogger(__name__)

_HEADER = struct.Struct("!I")


def _socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return os.path.join(runtime, "vasco", "browser.sock")


async def _send_request(sock_path: str, request: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(sock_path)
    try:
        payload = json.dumps(request, ensure_ascii=False).encode()
        writer.write(_HEADER.pack(len(payload)) + payload)
        await writer.drain()

        header = await reader.readexactly(_HEADER.size)
        (length,) = _HEADER.unpack(header)
        data = await reader.readexactly(length)
        return json.loads(data)
    finally:
        writer.close()
        await writer.wait_closed()


class BrowserPool:
    """Client handle to the shared browser server.

    Holds no browser of its own — ``fetch`` proxies to the server over the UNIX
    socket. Kept as a class (rather than free functions) for API stability:
    callers do ``get_browser(cfg).fetch(...)`` / ``.close()`` exactly as before.
    ``cfg`` is accepted at the factory for call-site compatibility but unused on
    the client; the server owns all browser configuration.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._remote: bool = False

    async def _ensure_started(self) -> None:
        """Establish (and cache) the connection to the browser server.

        Raises ``BrowserServerUnavailable`` if the server socket is missing or
        the handshake fails — there is no local fallback.
        """
        if self._remote:
            return
        async with self._lock:
            if self._remote:
                return
            sock = _socket_path()
            if os.path.exists(sock):
                try:
                    resp = await _send_request(
                        sock, {"url": "about:blank", "timeout": 5.0}
                    )
                    if "error" not in resp:
                        self._remote = True
                        log.info("connected to browser server at %s", sock)
                        return
                except Exception:
                    pass
            raise BrowserServerUnavailable(
                f"browser server not reachable at {sock}; "
                "start it with `vasco browser-server`"
            )

    async def fetch(
        self, url: str, *, deadline_monotonic: float, mobile: bool = False
    ) -> tuple[str, int, dict[str, str]]:
        """Fetch a URL via the browser server.

        Returns (html, status, headers). Raises ``asyncio.TimeoutError`` if the
        deadline has already passed, or ``BrowserServerUnavailable`` if the
        server isn't running.
        """
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("deadline elapsed before browser fetch could start")

        await self._ensure_started()
        resp = await _send_request(
            _socket_path(),
            {"url": url, "mobile": mobile, "timeout": remaining},
        )
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp.get("html", ""), resp.get("status", 0), resp.get("headers", {})

    async def close(self) -> None:
        self._remote = False


_pool: BrowserPool | None = None


def get_browser(cfg: Any | None = None) -> BrowserPool:
    """Return the process-wide ``BrowserPool`` singleton (a client to the browser
    server).

    ``cfg`` is accepted for call-site compatibility but unused — the server owns
    browser configuration. The browser is *not* started here; the connection is
    established lazily on the first ``.fetch()`` / ``._ensure_started()``.
    """
    global _pool
    if _pool is None:
        _pool = BrowserPool()
    return _pool


def _reset_for_tests() -> None:
    """Test-only hook: drop the singleton so a fresh one is created."""
    global _pool
    _pool = None
