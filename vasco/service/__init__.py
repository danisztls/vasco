"""vascod — the resident vasco daemon and its wire protocol.

`vasco serve` (``daemon.run_daemon``) owns the full fetch pipeline (one Config +
one Cache) and serves every local consumer — CLI, MCP, claudinho — over a UNIX
socket, adding cross-consumer single-flight + per-domain rate-limiting. It sits
in front of the browser server, which it uses as a client (never owns).

`protocol` is the single home for the wire contract; `client.DaemonClient` is the
thin async client the CLI and MCP use (with in-process fallback).
"""

from __future__ import annotations
