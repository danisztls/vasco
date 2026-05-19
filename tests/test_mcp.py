"""MCP server tests: verify tool registration and that each adapter routes
through to the underlying v0.1 core module with the right arguments.

These tests bypass the stdio transport — they call the FastMCP server's
in-process `call_tool` / `list_tools` API directly. The lifespan (which opens
the cache) is not invoked, so tools that need cache/cfg are exercised with
module globals patched directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from vasco import mcp as mcp_mod
from vasco import config as _config


EXPECTED_TOOL_NAMES = {"search", "fetch", "fetch_many", "extract", "map", "normalize"}


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
        schema = by_name[name].inputSchema
        assert schema["type"] == "object", name
        assert "properties" in schema, name


@pytest.fixture
def patched_cfg(monkeypatch: pytest.MonkeyPatch) -> _config.Config:
    cfg = _config.Config()
    monkeypatch.setattr(mcp_mod, "_cfg", cfg, raising=False)
    monkeypatch.setattr(mcp_mod, "_cache", None, raising=False)
    return cfg


@pytest.mark.asyncio
async def test_normalize_tool(patched_cfg: _config.Config) -> None:
    result = await mcp_mod.server.call_tool(
        "normalize", {"url": "https://Example.COM:443/foo?b=2&a=1"}
    )
    # FastMCP serializes scalar returns as TextContent. The structured_content
    # key holds the typed value if available.
    text = _text(result)
    assert "https://example.com/foo?a=1&b=2" in text


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

    result = await mcp_mod.server.call_tool(
        "fetch_many", {"urls": ["https://a", "https://b"]}
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


def _text(result: Any) -> str:
    """Extract a flat string from a FastMCP call_tool result for assertions."""
    # FastMCP returns either Sequence[ContentBlock] or dict[str, Any].
    if isinstance(result, tuple):
        # (content_blocks, structured_dict)
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
