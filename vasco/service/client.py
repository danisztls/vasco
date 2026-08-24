# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Thin async client for vascod, used by the CLI and MCP.

One-shot per call: connect, send one request, read one response, close. Callers
use :func:`request_or` to route an op through the daemon when it's reachable and
fall back to an in-process call when it isn't — so vasco stays fully functional
standalone (the daemon is an optimization/coordination layer, never a hard
dependency for vasco's own surfaces; only claudinho depends on it).

Failure model:
  - daemon not listening (no socket / refused) → :class:`DaemonUnavailable`
    raised immediately, so the caller falls back fast (no retry penalty);
  - connection dropped mid-request (e.g. a redeploy) → one reconnect after a
    short delay, then :class:`DaemonUnavailable` if it still fails;
  - daemon answered ``ok=false`` → :class:`DaemonError` (a real error — never
    retried, never a reason to fall back).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import protocol

_CONNECT_TIMEOUT = 2.0
# Generous backstop: the server-side per-op deadline (default 30s, full
# escalation chain ~24s) is the real bound. This only guards a wedged daemon.
_READ_TIMEOUT = 120.0
_RECONNECT_DELAY = 0.2


class DaemonError(RuntimeError):
    """The daemon answered ``ok=false`` (malformed request / daemon-side error)."""


class DaemonUnavailable(RuntimeError):
    """The daemon could not be reached (not running, or dropped mid-request)."""


class DaemonClient:
    def __init__(self, sock: str | Path | None = None) -> None:
        self._sock = str(sock) if sock is not None else str(protocol.socket_path())

    async def available(self) -> bool:
        """True if the daemon socket accepts a connection right now."""
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self._sock), _CONNECT_TIMEOUT
            )
        except (TimeoutError, OSError):
            return False
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True

    async def request(self, op: str, **params: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in (0, 1):
            try:
                return await self._once(op, params)
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                # Daemon isn't listening — fail fast, no retry, so callers fall
                # back to in-process without a latency penalty.
                raise DaemonUnavailable(str(exc)) from exc
            except (
                TimeoutError,
                asyncio.IncompleteReadError,
                ConnectionResetError,
            ) as exc:
                # Connection dropped mid-request (e.g. a redeploy) — reconnect once.
                last_exc = exc
                if attempt == 0:
                    await asyncio.sleep(_RECONNECT_DELAY)
                    continue
                raise DaemonUnavailable(str(exc)) from exc
        raise DaemonUnavailable(str(last_exc))  # pragma: no cover

    async def _once(self, op: str, params: dict[str, Any]) -> Any:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self._sock), _CONNECT_TIMEOUT
        )
        try:
            await protocol.write_msg(writer, {"op": op, "params": params})
            resp = await asyncio.wait_for(protocol.read_msg(reader), _READ_TIMEOUT)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if resp is None:
            raise DaemonUnavailable("oversized or truncated response frame")
        pv = resp.get("protocol_version")
        if pv != protocol.PROTOCOL_VERSION:
            raise DaemonError(
                f"protocol version mismatch: client={protocol.PROTOCOL_VERSION} "
                f"daemon={pv}"
            )
        if not resp.get("ok"):
            err = resp.get("error") or {}
            raise DaemonError(err.get("message") or "daemon error")
        return resp.get("result")


async def request_or[T](
    op: str, params: dict[str, Any], *, local: Callable[[], Awaitable[T]]
) -> T:
    """Run ``op`` on vascod if reachable; otherwise await ``local()`` in-process.

    Only daemon *unavailability* triggers the fallback — a daemon-side error
    (:class:`DaemonError`) propagates so callers don't silently mask real bugs.
    """
    try:
        return await DaemonClient().request(op, **params)
    except DaemonUnavailable:
        return await local()
