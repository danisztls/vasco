"""MCP server tests: verify tool registration and that each adapter routes
through to the underlying v0.1 core module with the right arguments.

These tests bypass the stdio transport — they call the MCPServer's
in-process `call_tool` / `list_tools` API directly. The lifespan (which opens
the cache) is not invoked, so tools that need cache/cfg are exercised with
module globals patched directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from vasco import config as _config
from vasco.interface import mcp as mcp_mod

EXPECTED_TOOL_NAMES = {"search", "fetch", "fetch_many", "extract", "answer", "map"}


@pytest.mark.asyncio
async def test_tools_registered() -> None:
    tools = await mcp_mod.server.list_tools()
    names = {t.name for t in tools}
    assert EXPECTED_TOOL_NAMES.issubset(names), names


@pytest.mark.asyncio
async def test_tool_input_schemas_are_well_formed() -> None:
    tools = await mcp_mod.server.list_tools()
    by_name = {t.name: t for t in tools}
    # Each input schema must be a JSON-Schema object with properties.
    for name in EXPECTED_TOOL_NAMES:
        schema = by_name[name].input_schema
        assert schema["type"] == "object", name
        assert "properties" in schema, name


@pytest.fixture
def patched_cfg(monkeypatch: pytest.MonkeyPatch) -> _config.Config:
    cfg = _config.Config()
    monkeypatch.setattr(mcp_mod, "_cfg", cfg, raising=False)
    monkeypatch.setattr(mcp_mod, "_cache", None, raising=False)
    return cfg


@pytest.mark.asyncio
async def test_search_tool_routes_to_searcher(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco import search as _search

    captured: dict[str, Any] = {}

    class _StubSearcher:
        def search(self, query: str, **kwargs: Any) -> list[_search.SearchResult]:
            captured["query"] = query
            captured["kwargs"] = kwargs
            return [
                _search.SearchResult(title="T1", url="https://x", snippet="s1"),
                _search.SearchResult(title="T2", url="https://y", snippet="s2"),
            ]

    monkeypatch.setattr(_search, "get_searcher", lambda *a, **kw: _StubSearcher())

    result = await mcp_mod.server.call_tool(
        "search",
        {"query": "rust", "max_results": 2, "site": "doc.rust-lang.org"},
    )
    text = _text(result)
    assert captured["query"] == "rust"
    assert captured["kwargs"]["max_results"] == 2
    assert captured["kwargs"]["site"] == "doc.rust-lang.org"
    assert "T1" in text and "T2" in text


@pytest.mark.asyncio
async def test_fetch_tool_routes_to_fetch_one(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco import fetch as _fetch

    captured: dict[str, Any] = {}

    async def fake_fetch_one(url: str, **kwargs: Any) -> dict[str, Any]:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return {"url_requested": url, "markdown": "hello", "mode_used": "http"}

    monkeypatch.setattr(_fetch, "fetch_one", fake_fetch_one)

    result = await mcp_mod.server.call_tool(
        "fetch", {"url": "https://example.com", "mode": "http", "deadline": 5.0}
    )
    assert captured["url"] == "https://example.com"
    assert captured["kwargs"]["mode"] == "http"
    assert captured["kwargs"]["deadline"] == 5.0
    text = _text(result)
    assert "hello" in text


@pytest.mark.asyncio
async def test_fetch_many_tool_drains_iterator(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco import fetch as _fetch

    async def fake_fetch_many(urls, **kwargs):  # type: ignore[no-untyped-def]
        for u in urls:
            yield {"url_requested": u, "markdown": u.upper()}

    monkeypatch.setattr(_fetch, "fetch_many", fake_fetch_many)

    # fetch_many's default is metadata_only=true (triage mode). This test asserts
    # the full pipeline streams content end-to-end, so it opts back into full
    # Markdown explicitly.
    result = await mcp_mod.server.call_tool(
        "fetch_many",
        {"urls": ["https://a", "https://b"], "metadata_only": False},
    )
    text = _text(result)
    assert "HTTPS://A" in text
    assert "HTTPS://B" in text


@pytest.mark.asyncio
async def test_extract_tool_passes_rank(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco import extract as _extract

    captured: dict[str, Any] = {}

    async def fake_extract(url: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        captured["url"] = url
        return {"url": url, "ranker": kwargs.get("rank"), "passages": []}

    monkeypatch.setattr(_extract, "extract", fake_extract)

    await mcp_mod.server.call_tool(
        "extract",
        {"url": "https://x", "query": "q", "rank": "semantic", "top": 3},
    )
    assert captured["rank"] == "semantic"
    assert captured["top"] == 3
    assert captured["query"] == "q"


@pytest.mark.asyncio
async def test_map_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    from vasco import map as _map

    def fake_map_site(url: str, **kwargs: Any):  # type: ignore[no-untyped-def]
        yield {"url": f"{url}/a", "source": "sitemap", "lastmod": None}
        yield {"url": f"{url}/b", "source": "feeds", "lastmod": None}

    monkeypatch.setattr(_map, "map_site", fake_map_site)
    result = await mcp_mod.server.call_tool(
        "map", {"url": "https://example.com", "limit": 10}
    )
    text = _text(result)
    assert "/a" in text and "/b" in text


@pytest.mark.asyncio
async def test_map_tool_forwards_exclude(monkeypatch: pytest.MonkeyPatch) -> None:
    from vasco import map as _map

    captured: dict[str, Any] = {}

    def fake_map_site(url: str, **kwargs: Any):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return iter(())

    monkeypatch.setattr(_map, "map_site", fake_map_site)
    await mcp_mod.server.call_tool(
        "map",
        {"url": "https://example.com", "exclude": ["/team/", "/tag/"]},
    )
    assert captured["exclude"] == ["/team/", "/tag/"]


@pytest.mark.asyncio
async def test_fetch_metadata_only_strips_markdown(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco import fetch as _fetch

    async def fake_fetch_one(url: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "url_requested": url,
            "title": "T",
            "word_count": 42,
            "markdown": "BIG_BODY_CONTENT",
        }

    monkeypatch.setattr(_fetch, "fetch_one", fake_fetch_one)

    result = await mcp_mod.server.call_tool(
        "fetch", {"url": "https://example.com", "metadata_only": True}
    )
    text = _text(result)
    assert "BIG_BODY_CONTENT" not in text
    assert '"title"' in text or "'title'" in text
    assert "42" in text


@pytest.mark.asyncio
async def test_fetch_many_metadata_only_strips_markdown(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco import fetch as _fetch

    async def fake_fetch_many(urls, **kwargs):  # type: ignore[no-untyped-def]
        for u in urls:
            yield {"url_requested": u, "title": "X", "markdown": f"BODY_{u}"}

    monkeypatch.setattr(_fetch, "fetch_many", fake_fetch_many)

    result = await mcp_mod.server.call_tool(
        "fetch_many",
        {"urls": ["https://a", "https://b"], "metadata_only": True},
    )
    text = _text(result)
    assert "BODY_https://a" not in text
    assert "BODY_https://b" not in text


@pytest.mark.asyncio
async def test_fetch_failure_logs_telemetry(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco import fetch as _fetch
    from vasco import telemetry as _telemetry

    async def fake_fetch_one(url: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "url_requested": url,
            "mode_used": "browser",
            "http_status": 0,
            "failure": {
                "reason": "blocked_bot",
                "message": "blocked_bot after browser tier",
            },
        }

    monkeypatch.setattr(_fetch, "fetch_one", fake_fetch_one)

    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        _telemetry, "log_event", lambda cfg, event: captured.append(event)
    )

    await mcp_mod.server.call_tool("fetch", {"url": "https://blocked.test"})

    assert len(captured) == 1
    event = captured[0]
    assert event["tool"] == "fetch"
    assert event["url"] == "https://blocked.test"
    assert event["failure_reason"] == "blocked_bot"
    assert event["mode_used"] == "browser"


@pytest.mark.asyncio
async def test_fetch_success_logs_telemetry(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco import fetch as _fetch
    from vasco import telemetry as _telemetry

    async def fake_fetch_one(url: str, **kwargs: Any) -> dict[str, Any]:
        # Mimic the shape stamped by _fetch._stamp_phases.
        return {
            "url_requested": url,
            "mode_used": "http",
            "http_status": 200,
            "word_count": 42,
            "from_cache": False,
            "markdown": "ok",
            "duration_ms": 123,
            "network_ms": 90,
            "parse_ms": 20,
            "attempts": 1,
        }

    monkeypatch.setattr(_fetch, "fetch_one", fake_fetch_one)

    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        _telemetry, "log_event", lambda cfg, event: captured.append(event)
    )

    await mcp_mod.server.call_tool("fetch", {"url": "https://ok.test"})
    assert len(captured) == 1
    event = captured[0]
    assert event["tool"] == "fetch"
    assert event["outcome"] == "ok"
    assert event["url"] == "https://ok.test"
    assert event["mode_used"] == "http"
    assert event["http_status"] == 200
    assert event["word_count"] == 42
    assert event["from_cache"] is False
    assert event["duration_ms"] == 123
    assert event["network_ms"] == 90
    assert event["parse_ms"] == 20
    assert event["attempts"] == 1
    assert "escalated_from" not in event  # only set on real escalation


@pytest.mark.asyncio
async def test_search_success_logs_telemetry(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco import search as _search
    from vasco import telemetry as _telemetry

    class _StubSearcher:
        def search(self, query: str, **kwargs: Any) -> list[_search.SearchResult]:
            return [
                _search.SearchResult(title="T", url="https://x", snippet="s"),
                _search.SearchResult(title="U", url="https://y", snippet="t"),
            ]

    monkeypatch.setattr(_search, "get_searcher", lambda *a, **kw: _StubSearcher())

    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        _telemetry, "log_event", lambda cfg, event: captured.append(event)
    )

    await mcp_mod.server.call_tool("search", {"query": "foo", "max_results": 2})
    assert len(captured) == 1
    event = captured[0]
    assert event["tool"] == "search"
    assert event["outcome"] == "ok"
    assert event["query"] == "foo"
    assert event["result_count"] == 2
    assert "duration_ms" in event


@pytest.mark.asyncio
async def test_map_success_logs_telemetry(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco import map as _map_mod
    from vasco import telemetry as _telemetry

    monkeypatch.setattr(
        _map_mod,
        "map_site",
        lambda *a, **kw: iter(
            [{"url": "https://x/a", "source": "sitemap", "lastmod": None}]
        ),
    )

    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        _telemetry, "log_event", lambda cfg, event: captured.append(event)
    )

    await mcp_mod.server.call_tool("map", {"url": "https://x"})
    assert len(captured) == 1
    event = captured[0]
    assert event["tool"] == "map"
    assert event["outcome"] == "ok"
    assert event["url"] == "https://x"
    assert event["result_count"] == 1


@pytest.mark.asyncio
async def test_extract_success_logs_telemetry(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco import extract as _extract
    from vasco import telemetry as _telemetry

    async def fake_extract(url: str, **kwargs: Any) -> dict[str, Any]:
        return {"url": url, "passages": [{"text": "a"}, {"text": "b"}]}

    monkeypatch.setattr(_extract, "extract", fake_extract)

    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        _telemetry, "log_event", lambda cfg, event: captured.append(event)
    )

    await mcp_mod.server.call_tool("extract", {"url": "https://x", "query": "q"})
    assert len(captured) == 1
    event = captured[0]
    assert event["tool"] == "extract"
    assert event["outcome"] == "ok"
    assert event["passage_count"] == 2
    assert event["rank"] == "bm25"


@pytest.mark.asyncio
async def test_extract_empty_passages_logs_telemetry(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco import extract as _extract
    from vasco import telemetry as _telemetry

    async def fake_extract(url: str, **kwargs: Any) -> dict[str, Any]:
        return {"url": url, "passages": []}

    monkeypatch.setattr(_extract, "extract", fake_extract)

    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        _telemetry, "log_event", lambda cfg, event: captured.append(event)
    )

    await mcp_mod.server.call_tool("extract", {"url": "https://x", "query": "nope"})

    assert len(captured) == 1
    assert captured[0]["tool"] == "extract"
    assert captured[0]["empty_passages"] is True


@pytest.mark.asyncio
async def test_fetch_many_default_strips_markdown(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locks in the new default: metadata_only=true unless explicitly disabled."""
    from vasco import fetch as _fetch

    async def fake_fetch_many(urls, **kwargs):  # type: ignore[no-untyped-def]
        for u in urls:
            yield {"url_requested": u, "title": "X", "markdown": f"BODY_{u}"}

    monkeypatch.setattr(_fetch, "fetch_many", fake_fetch_many)

    result = await mcp_mod.server.call_tool(
        "fetch_many", {"urls": ["https://a", "https://b"]}
    )
    text = _text(result)
    assert "BODY_https://a" not in text
    assert "BODY_https://b" not in text


@pytest.mark.asyncio
async def test_answer_tool_returns_answer(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco import summarize as _sum

    captured: dict[str, Any] = {}

    async def fake_answer(url: str, **kwargs: Any) -> dict[str, Any]:
        captured["url"] = url
        captured["question"] = kwargs.get("question")
        return {"url": url, "title": "T", "answer": "THE ANSWER", "from_cache": False}

    monkeypatch.setattr(_sum, "answer", fake_answer)

    result = await mcp_mod.server.call_tool(
        "answer", {"url": "https://example.com", "question": "what is X?"}
    )
    text = _text(result)
    assert captured["url"] == "https://example.com"
    assert captured["question"] == "what is X?"
    assert "THE ANSWER" in text


@pytest.mark.asyncio
async def test_answer_tool_logs_success(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco import summarize as _sum
    from vasco import telemetry as _telemetry

    async def fake_answer(url: str, **kwargs: Any) -> dict[str, Any]:
        return {"url": url, "answer": "A", "from_cache": True}

    monkeypatch.setattr(_sum, "answer", fake_answer)

    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        _telemetry, "log_event", lambda cfg, event: captured.append(event)
    )

    await mcp_mod.server.call_tool("answer", {"url": "https://ok.test"})
    assert len(captured) == 1
    assert captured[0]["tool"] == "answer"
    assert captured[0]["outcome"] == "ok"
    assert captured[0]["from_cache"] is True


@pytest.mark.asyncio
async def test_answer_tool_logs_fetch_failure(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco import summarize as _sum
    from vasco import telemetry as _telemetry

    async def fake_answer(url: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "url_requested": url,
            "mode_used": "http",
            "http_status": 404,
            "failure": {"reason": "not_found", "message": "404"},
        }

    monkeypatch.setattr(_sum, "answer", fake_answer)

    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        _telemetry, "log_event", lambda cfg, event: captured.append(event)
    )

    await mcp_mod.server.call_tool("answer", {"url": "https://missing.test"})
    assert len(captured) == 1
    assert captured[0]["tool"] == "answer"
    assert captured[0]["failure_reason"] == "not_found"


@pytest.mark.asyncio
async def test_answer_tool_logs_error_outcome(
    patched_cfg: _config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco import summarize as _sum
    from vasco import telemetry as _telemetry

    async def fake_answer(url: str, **kwargs: Any) -> dict[str, Any]:
        return {"url": url, "answer": None, "error": "no_api_key", "message": "..."}

    monkeypatch.setattr(_sum, "answer", fake_answer)

    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        _telemetry, "log_event", lambda cfg, event: captured.append(event)
    )

    result = await mcp_mod.server.call_tool("answer", {"url": "https://x"})
    assert _text(result)  # error result still returned to caller
    assert len(captured) == 1
    assert captured[0]["tool"] == "answer"
    assert captured[0]["outcome"] == "fail"
    assert captured[0]["error"] == "no_api_key"


@pytest.mark.asyncio
async def test_lifespan_prewarms_browser_when_enabled(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco.fetch import browser as _browser

    _browser._reset_for_tests()
    monkeypatch.setenv("VASCO_BROWSER_PREWARM", "true")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    called = {"n": 0}

    async def fake_start(self: Any) -> None:
        called["n"] += 1

    monkeypatch.setattr(_browser.BrowserPool, "_ensure_started", fake_start)

    async with mcp_mod._lifespan(mcp_mod.server):
        pass
    assert called["n"] == 1
    _browser._reset_for_tests()


@pytest.mark.asyncio
async def test_lifespan_prewarm_failure_does_not_kill_server(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vasco.fetch import browser as _browser

    _browser._reset_for_tests()
    monkeypatch.setenv("VASCO_BROWSER_PREWARM", "true")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    async def boom(self: Any) -> None:
        raise RuntimeError("camoufox missing")

    monkeypatch.setattr(_browser.BrowserPool, "_ensure_started", boom)

    # Must not raise: the lifespan should swallow prewarm errors.
    async with mcp_mod._lifespan(mcp_mod.server):
        pass
    _browser._reset_for_tests()


def _text(result: Any) -> str:
    """Extract a flat string from an MCPServer call_tool result for assertions."""
    # MCP v2 always returns a CallToolResult; v1 returned bare content blocks
    # or a (blocks, structured) tuple. Accept all three so the helper stays
    # honest about what it is handed.
    content = getattr(result, "content", None)
    if content is not None:
        return "\n".join(getattr(b, "text", None) or str(b) for b in content)
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, dict):
        import json

        return json.dumps(result)
    pieces: list[str] = []
    for item in result:  # ContentBlock list
        text = getattr(item, "text", None)
        if text is None:
            text = str(item)
        pieces.append(text)
    return "\n".join(pieces)
