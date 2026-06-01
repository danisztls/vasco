"""vascod daemon tests: start the daemon on a tmp socket and exercise the wire
protocol. The network seam (`_http_fetch`) is stubbed exactly as in
test_fetch_integration; failure/search/fetch_many paths monkeypatch the daemon's
entry points directly so the tests are hermetic (no real network)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from vasco import fetch as fetch_mod
from vasco.config import CacheCfg, Config, ServiceCfg
from vasco.fetch import browser as browser_mod
from vasco.service import client as client_mod
from vasco.service import coordinator as coordinator_mod
from vasco.service import daemon as daemon_mod
from vasco.service import protocol

FIXTURES = Path(__file__).parent / "fixtures"


def _disable_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NopPool:
        async def fetch(self, *a: Any, **kw: Any) -> tuple[str, int, dict[str, str]]:
            return "", 0, {}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(browser_mod, "_pool", None, raising=False)
    monkeypatch.setattr(browser_mod, "get_browser", lambda cfg=None: _NopPool())


def _stub_http(html: str, status: int = 200, headers: dict | None = None):
    async def _fake(
        url: str, *, deadline_monotonic: float, cfg: Any | None = None
    ) -> tuple[str, int, dict[str, str]]:
        hdrs = dict(headers or {})
        hdrs.setdefault("_url_final", url)
        return html, status, hdrs

    return _fake


def _cfg(tmp_path: Path) -> Config:
    return Config(cache=CacheCfg(path=str(tmp_path / "cache.db")))


@asynccontextmanager
async def _running(cfg: Config, sock: Path):
    task = asyncio.create_task(daemon_mod.run_daemon(cfg, sock=sock))
    for _ in range(200):  # up to ~2s for the socket to appear
        if sock.exists():
            break
        await asyncio.sleep(0.01)
    else:
        task.cancel()
        raise RuntimeError("daemon did not start")
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _request(sock: Path, op: str, **params: Any) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(sock))
    try:
        await protocol.write_msg(writer, {"op": op, "params": params})
        resp = await protocol.read_msg(reader)
        assert resp is not None
        return resp
    finally:
        writer.close()
        await writer.wait_closed()


async def test_fetch_success_roundtrips_full_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = (FIXTURES / "article_clean.html").read_text(encoding="utf-8")
    monkeypatch.setattr(fetch_mod, "_http_fetch", _stub_http(html, 200))
    _disable_browser(monkeypatch)

    sock = tmp_path / "vascod.sock"
    async with _running(_cfg(tmp_path), sock):
        resp = await _request(
            sock, protocol.OP_FETCH, url="https://example.com/article"
        )

    assert resp["protocol_version"] == protocol.PROTOCOL_VERSION
    assert resp["ok"] is True
    env = resp["result"]
    assert env["http_status"] == 200
    assert env["markdown"], "expected extracted markdown in the envelope"
    assert "failure" not in env


async def test_fetch_failure_is_envelope_not_transport_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fetch failure must cross the wire as ok=true with a failure envelope —
    the fetch_one never-raises contract, preserved over the socket."""
    failure_env = {
        "url_requested": "https://x.test",
        "failure": {"reason": "not_found"},
    }

    async def _fake_fetch_one(url: str, **kw: Any) -> dict:
        return failure_env

    monkeypatch.setattr(coordinator_mod, "_fetch_one", _fake_fetch_one)

    sock = tmp_path / "vascod.sock"
    async with _running(_cfg(tmp_path), sock):
        resp = await _request(sock, protocol.OP_FETCH, url="https://x.test")

    assert resp["ok"] is True
    assert resp["result"]["failure"]["reason"] == "not_found"


async def test_unknown_op_returns_transport_error(tmp_path: Path) -> None:
    sock = tmp_path / "vascod.sock"
    async with _running(_cfg(tmp_path), sock):
        resp = await _request(sock, "bogus", url="https://x.test")

    assert resp["ok"] is False
    assert resp["error"]["type"] == "ValueError"
    assert "bogus" in resp["error"]["message"]


async def test_fetch_many_metadata_only_strips_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_fetch_one(url: str, **kw: Any) -> dict:
        return {"url_requested": url, "markdown": "BIG", "word_count": 1}

    monkeypatch.setattr(coordinator_mod, "_fetch_one", _fake_fetch_one)

    sock = tmp_path / "vascod.sock"
    async with _running(_cfg(tmp_path), sock):
        resp = await _request(
            sock,
            protocol.OP_FETCH_MANY,
            urls=["https://a.test", "https://b.test"],
            metadata_only=True,
        )

    assert resp["ok"] is True
    rows = resp["result"]
    assert len(rows) == 2
    assert all("markdown" not in r for r in rows)
    assert all(r["word_count"] == 1 for r in rows)


async def test_search_roundtrips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FakeResult:
        def __init__(self, title: str, url: str, snippet: str) -> None:
            self.title, self.url, self.snippet = title, url, snippet

    class _FakeSearcher:
        def search(self, query: str, **kw: Any):
            return [_FakeResult("T", "https://r.test", "S")]

    monkeypatch.setattr(
        daemon_mod, "get_searcher", lambda backend, cfg=None: _FakeSearcher()
    )

    sock = tmp_path / "vascod.sock"
    async with _running(_cfg(tmp_path), sock):
        resp = await _request(sock, protocol.OP_SEARCH, query="hello", max_results=1)

    assert resp["ok"] is True
    assert resp["result"] == [{"title": "T", "url": "https://r.test", "snippet": "S"}]


async def test_pipelined_requests_on_one_connection(tmp_path: Path) -> None:
    """The handler loops over a connection, so a client can send N requests on
    one socket (claudinho's gather opens N connections, but pipelining must work)."""
    sock = tmp_path / "vascod.sock"
    async with _running(_cfg(tmp_path), sock):
        reader, writer = await asyncio.open_unix_connection(str(sock))
        try:
            for _ in range(3):
                await protocol.write_msg(writer, {"op": "bogus", "params": {}})
                resp = await protocol.read_msg(reader)
                assert resp is not None and resp["ok"] is False
        finally:
            writer.close()
            await writer.wait_closed()


# --- DaemonClient / request_or -------------------------------------------------


async def test_client_request_routes_to_running_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Searcher:
        def search(self, query: str, **kw: Any):
            class _R:
                title, url, snippet = "T", "https://r.test", "S"

            return [_R()]

    monkeypatch.setattr(
        daemon_mod, "get_searcher", lambda backend, cfg=None: _Searcher()
    )

    sock = tmp_path / "vascod.sock"
    async with _running(_cfg(tmp_path), sock):
        client = client_mod.DaemonClient(sock)
        assert await client.available() is True
        rows = await client.request(protocol.OP_SEARCH, query="x", max_results=1)
        assert rows == [{"title": "T", "url": "https://r.test", "snippet": "S"}]


async def test_request_or_falls_back_when_daemon_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No daemon listening → request_or runs the in-process local path."""
    monkeypatch.setenv("VASCO_SERVICE_SOCKET", str(tmp_path / "nope.sock"))

    async def _local() -> str:
        return "local-ran"

    assert await client_mod.DaemonClient().available() is False
    result = await client_mod.request_or(
        protocol.OP_FETCH, {"url": "https://x.test"}, local=_local
    )
    assert result == "local-ran"


async def test_client_ok_false_raises_daemon_error_not_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon-side error must surface as DaemonError, never trigger fallback."""
    sock = tmp_path / "vascod.sock"
    async with _running(_cfg(tmp_path), sock):
        with pytest.raises(client_mod.DaemonError):
            await client_mod.DaemonClient(sock).request("bogus")

        # request_or must let DaemonError propagate (never run local).
        monkeypatch.setenv("VASCO_SERVICE_SOCKET", str(sock))

        async def _local() -> str:
            raise AssertionError("local must not run on a daemon-side error")

        with pytest.raises(client_mod.DaemonError):
            await client_mod.request_or("bogus", {}, local=_local)


# --- coordinator: single-flight + rate limit ----------------------------------


def _gated_fetch_one(state: dict[str, Any]):
    """A fake fetch_one that counts calls and blocks on `release` so the test can
    hold fetches in-flight and observe single-flight collapsing."""

    async def _fake(url: str, **kw: Any) -> dict:
        state["calls"] += 1
        state["started"].set()
        await state["release"].wait()
        return {"url_requested": url, "markdown": "x"}

    return _fake


async def test_single_flight_collapses_concurrent_identical_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"calls": 0, "started": asyncio.Event(), "release": asyncio.Event()}
    monkeypatch.setattr(coordinator_mod, "_fetch_one", _gated_fetch_one(state))

    sock = tmp_path / "vascod.sock"
    url = "https://dup.test/a"
    async with _running(_cfg(tmp_path), sock):
        t1 = asyncio.create_task(_request(sock, protocol.OP_FETCH, url=url))
        await state["started"].wait()  # first fetch is now in-flight
        t2 = asyncio.create_task(_request(sock, protocol.OP_FETCH, url=url))
        await asyncio.sleep(0.05)  # let t2 reach the coordinator and join
        state["release"].set()
        r1, r2 = await asyncio.gather(t1, t2)

    assert r1["ok"] and r2["ok"]
    assert r1["result"]["url_requested"] == url
    assert state["calls"] == 1  # one network fetch served both callers


async def test_distinct_urls_are_not_deduped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"calls": 0, "started": asyncio.Event(), "release": asyncio.Event()}
    monkeypatch.setattr(coordinator_mod, "_fetch_one", _gated_fetch_one(state))

    sock = tmp_path / "vascod.sock"
    async with _running(_cfg(tmp_path), sock):
        t1 = asyncio.create_task(
            _request(sock, protocol.OP_FETCH, url="https://x.test/1")
        )
        t2 = asyncio.create_task(
            _request(sock, protocol.OP_FETCH, url="https://x.test/2")
        )
        await asyncio.sleep(0.05)
        state["release"].set()
        await asyncio.gather(t1, t2)

    assert state["calls"] == 2


async def test_single_flight_disabled_runs_each_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"calls": 0, "started": asyncio.Event(), "release": asyncio.Event()}
    monkeypatch.setattr(coordinator_mod, "_fetch_one", _gated_fetch_one(state))

    cfg = Config(
        cache=CacheCfg(path=str(tmp_path / "cache.db")),
        service=ServiceCfg(single_flight=False),
    )
    sock = tmp_path / "vascod.sock"
    url = "https://dup.test/a"
    async with _running(cfg, sock):
        t1 = asyncio.create_task(_request(sock, protocol.OP_FETCH, url=url))
        await state["started"].wait()
        t2 = asyncio.create_task(_request(sock, protocol.OP_FETCH, url=url))
        await asyncio.sleep(0.05)
        state["release"].set()
        await asyncio.gather(t1, t2)

    assert state["calls"] == 2  # no single-flight → both fetched


async def test_domain_rate_limiter_spaces_same_domain() -> None:
    lim = coordinator_mod._DomainRateLimiter(rps=20)  # 50ms min interval
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await lim.acquire("d")
    await lim.acquire("d")
    await lim.acquire("d")
    # 1st immediate, 2nd at ~50ms, 3rd at ~100ms.
    assert loop.time() - t0 >= 0.09


async def test_domain_rate_limiter_independent_domains() -> None:
    lim = coordinator_mod._DomainRateLimiter(rps=5)  # 200ms min interval
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await lim.acquire("a")
    await lim.acquire("b")  # different domain — must not wait behind "a"
    assert loop.time() - t0 < 0.1
