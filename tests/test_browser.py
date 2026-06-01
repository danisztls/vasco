"""Tests for the browser-tier client (`BrowserPool` proxying to the server).

The client holds no browser of its own — it connects to the browser server's
UNIX socket and proxies fetch requests. These tests stand up a tiny fake server
on a tmp socket and assert the proxy round-trip, plus the no-server failure mode.
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
from pathlib import Path

import pytest

from vasco.errors import BrowserServerUnavailable
from vasco.fetch import browser as browser_mod
from vasco.fetch.browser import BrowserPool

_HEADER = struct.Struct("!I")


@pytest.fixture(autouse=True)
def _reset() -> None:
    browser_mod._reset_for_tests()
    yield
    browser_mod._reset_for_tests()


async def _start_fake_server(sock_path: Path, handler) -> asyncio.AbstractServer:
    """A length-prefixed-JSON echo server matching the client's `_send_request`.

    `handler(request_dict) -> response_dict`. One request per connection (the
    client opens a fresh connection per `_send_request` call).
    """

    async def _cb(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = await reader.readexactly(_HEADER.size)
            (length,) = _HEADER.unpack(header)
            req = json.loads(await reader.readexactly(length))
            resp = handler(req)
            payload = json.dumps(resp).encode()
            writer.write(_HEADER.pack(len(payload)) + payload)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    return await asyncio.start_unix_server(_cb, path=str(sock_path))


@pytest.mark.asyncio
async def test_ensure_started_raises_when_no_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        browser_mod, "_socket_path", lambda: str(tmp_path / "nonexistent.sock")
    )
    pool = BrowserPool()
    with pytest.raises(BrowserServerUnavailable):
        await pool._ensure_started()


@pytest.mark.asyncio
async def test_fetch_proxies_to_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sock = tmp_path / "browser.sock"
    monkeypatch.setattr(browser_mod, "_socket_path", lambda: str(sock))

    seen: list[dict] = []

    def handler(req: dict) -> dict:
        seen.append(req)
        if req.get("url") == "about:blank":  # the _ensure_started handshake
            return {"html": "", "status": 200, "headers": {}}
        return {"html": "<html>ok</html>", "status": 200, "headers": {"x": "y"}}

    server = await _start_fake_server(sock, handler)
    try:
        pool = BrowserPool()
        html, status, headers = await pool.fetch(
            "https://example.com", deadline_monotonic=time.monotonic() + 5
        )
        assert (html, status, headers) == ("<html>ok</html>", 200, {"x": "y"})
        # The real request carried through the mobile flag and the URL.
        real = [r for r in seen if r.get("url") == "https://example.com"]
        assert real and real[0]["mobile"] is False
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_fetch_passes_mobile_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sock = tmp_path / "browser.sock"
    monkeypatch.setattr(browser_mod, "_socket_path", lambda: str(sock))

    seen: list[dict] = []

    def handler(req: dict) -> dict:
        seen.append(req)
        return {"html": "", "status": 200, "headers": {}}

    server = await _start_fake_server(sock, handler)
    try:
        pool = BrowserPool()
        await pool.fetch(
            "https://example.com", deadline_monotonic=time.monotonic() + 5, mobile=True
        )
        real = [r for r in seen if r.get("url") == "https://example.com"]
        assert real and real[0]["mobile"] is True
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_fetch_raises_runtimeerror_on_server_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sock = tmp_path / "browser.sock"
    monkeypatch.setattr(browser_mod, "_socket_path", lambda: str(sock))

    def handler(req: dict) -> dict:
        if req.get("url") == "about:blank":
            return {"html": "", "status": 200, "headers": {}}
        return {"error": "boom"}

    server = await _start_fake_server(sock, handler)
    try:
        pool = BrowserPool()
        with pytest.raises(RuntimeError, match="boom"):
            await pool.fetch(
                "https://example.com", deadline_monotonic=time.monotonic() + 5
            )
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_fetch_raises_timeout_when_deadline_passed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(browser_mod, "_socket_path", lambda: str(tmp_path / "x.sock"))
    pool = BrowserPool()
    with pytest.raises(asyncio.TimeoutError):
        await pool.fetch("https://example.com", deadline_monotonic=time.monotonic() - 1)
