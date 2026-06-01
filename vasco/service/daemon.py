"""vascod — the resident vasco daemon.

Owns the full fetch pipeline (one ``Config`` + one ``Cache`` for the process
lifetime) and serves every local consumer over a UNIX socket. Each request is a
single op routed to the *existing* in-process entry points (``fetch_one``,
``fetch_many``, ``extract``, ``answer``, ``map_site``, ``search``) — vascod is
"the library, made resident, with a socket".

Two-daemon layering: when the pipeline needs the browser tier it routes to the
browser server exactly as a library ``fetch_one`` does today. vascod is just
another of the browser server's clients, never its owner (crash isolation).

Security: UNIX socket only — never an ``AF_INET`` listener. The socket lives in
``$XDG_RUNTIME_DIR`` (0700, user-only) and is chmod'd 0600; inbound frames are
size-capped in ``protocol.read_msg``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from vasco import extract as _extract
from vasco import map as _map
from vasco import summarize as _summarize
from vasco.cache import Cache
from vasco.config import Config, load_config
from vasco.search import get_searcher

from . import protocol
from .coordinator import Coordinator

log = logging.getLogger(__name__)


# Per-op kwarg whitelists: an untrusted request can only set known parameters,
# never inject arbitrary kwargs into the pipeline functions.
_FETCH_KEYS = ("mode", "deadline", "use_cache", "refresh", "raw")
_EXTRACT_KEYS = (
    "query",
    "top",
    "context_chars",
    "mode",
    "rank",
    "deadline",
    "use_cache",
    "refresh",
)
_ANSWER_KEYS = ("question", "mode", "deadline", "use_cache", "refresh")
_MAP_KEYS = ("source", "limit", "exclude")
_SEARCH_KEYS = ("max_results", "region", "time", "site")


def _pick(params: dict[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
    return {k: params[k] for k in allowed if k in params}


def _strip_markdown(env: dict[str, Any]) -> dict[str, Any]:
    """Drop the large ``markdown`` field for triage-only callers (fetch_many)."""
    return {k: v for k, v in env.items() if k != "markdown"}


class Dispatcher:
    """Routes one request op to the in-process pipeline.

    Holds the single shared ``Config`` + ``Cache`` for the daemon's lifetime.
    Kept as a stable seam the connection handler calls and tests exercise; the
    coordinator (single-flight + rate-limit) wraps the fetch-family ops here.
    """

    def __init__(self, cfg: Config, cache: Cache) -> None:
        self.cfg = cfg
        self.cache = cache
        self.coordinator = Coordinator(cfg, cache)

    async def handle(self, op: str, params: dict[str, Any]) -> Any:
        if op == protocol.OP_FETCH:
            return await self.coordinator.fetch(
                params["url"], **_pick(params, _FETCH_KEYS)
            )
        if op == protocol.OP_FETCH_MANY:
            # Run as a coordinated gather over the single-URL fetch, so every URL
            # gets the same single-flight + per-domain rate-limit as a plain
            # `fetch` (and dedupes against concurrent `fetch` calls). The browser
            # tier is warm in the separate browser server, so we don't lose
            # fetch_many's in-process browser-reuse optimization.
            urls = list(params.get("urls") or [])
            workers = max(1, int(params.get("workers", 4)))
            metadata_only = bool(params.get("metadata_only", False))
            kw = _pick(params, _FETCH_KEYS)
            sem = asyncio.Semaphore(workers)

            async def _one(u: str) -> dict[str, Any]:
                async with sem:
                    return await self.coordinator.fetch(u, **kw)

            envs = await asyncio.gather(*[_one(u) for u in urls])
            return [_strip_markdown(e) if metadata_only else e for e in envs]
        if op == protocol.OP_EXTRACT:
            return await _extract.extract(
                params["url"],
                cache=self.cache,
                cfg=self.cfg,
                **_pick(params, _EXTRACT_KEYS),
            )
        if op == protocol.OP_ANSWER:
            return await _summarize.answer(
                params["url"],
                cache=self.cache,
                cfg=self.cfg,
                **_pick(params, _ANSWER_KEYS),
            )
        if op == protocol.OP_MAP:
            # trafilatura does synchronous HTTP — offload so a slow sitemap fetch
            # doesn't block other in-flight clients.
            url = params["url"]
            kw = _pick(params, _MAP_KEYS)
            return await asyncio.to_thread(lambda: list(_map.map_site(url, **kw)))
        if op == protocol.OP_SEARCH:
            backend = params.get("backend") or self.cfg.search.default_backend
            searcher = get_searcher(backend, cfg=self.cfg)
            query = params["query"]
            kw = _pick(params, _SEARCH_KEYS)
            return await asyncio.to_thread(
                lambda: [
                    {"title": r.title, "url": r.url, "snippet": r.snippet}
                    for r in searcher.search(query, **kw)
                ]
            )
        raise ValueError(f"unknown op: {op!r}")


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    dispatcher: Dispatcher,
) -> None:
    try:
        while True:
            try:
                req = await protocol.read_msg(reader)
            except (asyncio.IncompleteReadError, ConnectionResetError):
                break
            if req is None:  # oversized frame — stream is mis-framed, bail
                break
            op = req.get("op", "")
            params = req.get("params") or {}
            try:
                result = await dispatcher.handle(op, params)
                resp: dict[str, Any] = {
                    "protocol_version": protocol.PROTOCOL_VERSION,
                    "ok": True,
                    "result": result,
                }
            except Exception as exc:  # never let one bad request kill the daemon
                log.warning("op %r failed: %s", op, exc)
                resp = {
                    "protocol_version": protocol.PROTOCOL_VERSION,
                    "ok": False,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            await protocol.write_msg(writer, resp)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def run_daemon(
    cfg: Config | None = None, *, sock: str | Path | None = None
) -> None:
    """Serve the full vasco API over a UNIX socket until cancelled.

    ``sock`` overrides the socket path (tests pass a tmp path); production uses
    ``protocol.socket_path()``.
    """
    cfg = cfg or load_config()
    cache = Cache(cfg.cache.path or None)
    dispatcher = Dispatcher(cfg, cache)

    sock_path = Path(sock) if sock is not None else protocol.socket_path()
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()

    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(r, w, dispatcher),
        path=str(sock_path),
    )
    os.chmod(str(sock_path), 0o600)
    log.info("vascod listening on %s", sock_path)
    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        try:
            cache.close()
        except Exception:
            pass
        if sock_path.exists():
            sock_path.unlink()
        log.info("vascod stopped")
