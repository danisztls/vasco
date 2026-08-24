# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Wire protocol for vascod — the single home for the socket contract.

Both ends (the daemon and every thin client) read against this one module so the
shape lives in exactly one place. The *payload* data model is the fetch envelope
itself (``vasco/envelope.py``), which is already JSON-serializable; this layer
only adds framing and a ``protocol_version`` so the transport can version
independently of the envelope.

Framing (identical shape to ``vasco/fetch/browser_server.py``):
  - 4-byte big-endian uint32 length prefix
  - JSON payload

Request:  ``{"op": "fetch", "params": {...}}``
Response: ``{"protocol_version": N, "ok": true,  "result": <payload>}``
          ``{"protocol_version": N, "ok": false, "error": {"type": ..., "message": ...}}``

A *fetch* failure is **not** a transport error: it comes back as ``ok=true`` with
the failure envelope as ``result`` (the ``fetch_one`` never-raises contract
crosses the wire intact). ``ok=false`` is reserved for malformed requests and
unexpected exceptions inside the daemon.
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
from pathlib import Path

# Bump when the wire shape changes incompatibly. Clients echo the value they
# were built against; the daemon stamps its own on every response so a mismatch
# is detectable (see client.DaemonClient).
PROTOCOL_VERSION = 1

# Operation names — mirror the public API surface.
OP_FETCH = "fetch"
OP_FETCH_MANY = "fetch_many"
OP_EXTRACT = "extract"
OP_ANSWER = "answer"
OP_MAP = "map"
OP_SEARCH = "search"
OPS = frozenset({OP_FETCH, OP_FETCH_MANY, OP_EXTRACT, OP_ANSWER, OP_MAP, OP_SEARCH})

_HEADER = struct.Struct("!I")
# Bound inbound memory: reject frames larger than this (mirror browser_server).
MAX_FRAME = 10 * 1024 * 1024  # 10 MiB


def socket_path() -> Path:
    """Resolve the vascod socket path.

    ``$XDG_RUNTIME_DIR/vasco/vascod.sock`` by default; ``VASCO_SERVICE_SOCKET``
    overrides it. Reading the env directly (not vasco config) keeps the vendored
    claudinho client in lockstep with the daemon without importing config.
    """
    override = os.environ.get("VASCO_SERVICE_SOCKET")
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(runtime) / "vasco" / "vascod.sock"


async def read_msg(reader: asyncio.StreamReader) -> dict | None:
    """Read one length-prefixed JSON message.

    Returns ``None`` for an oversized frame (the stream is then mis-framed and the
    caller should stop reading). Raises ``IncompleteReadError`` on clean EOF.
    """
    header = await reader.readexactly(_HEADER.size)
    (length,) = _HEADER.unpack(header)
    if length > MAX_FRAME:
        return None
    data = await reader.readexactly(length)
    return json.loads(data)


async def write_msg(writer: asyncio.StreamWriter, msg: dict) -> None:
    payload = json.dumps(msg, ensure_ascii=False).encode()
    writer.write(_HEADER.pack(len(payload)) + payload)
    await writer.drain()
