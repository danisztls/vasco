from __future__ import annotations

import json

import httpx
import pytest

from vasco import search


def _mock_transport(
    captured: dict, *, results: list[dict] | None = None, status: int = 200
) -> httpx.MockTransport:
    body = json.dumps({"results": results if results is not None else []}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            status, content=body, headers={"content-type": "application/json"}
        )

    return httpx.MockTransport(handler)


def _patch_httpx_client(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport
) -> None:
    original_client = httpx.Client

    def factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)


def test_tavily_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    transport = _mock_transport(
        captured,
        results=[
            {"title": "T1", "url": "https://example.com/1", "content": "snip 1"},
            {"title": "T2", "url": "https://example.com/2", "content": "snip 2"},
        ],
    )
    _patch_httpx_client(monkeypatch, transport)

    backend = search.TavilyBackend(api_key="tvly-test")
    results = list(backend.search("rust async", max_results=5))

    assert captured["method"] == "POST"
    assert captured["url"].startswith("https://api.tavily.com/search")
    body = captured["json"]
    assert body["api_key"] == "tvly-test"
    assert body["query"] == "rust async"
    assert body["max_results"] == 5
    assert body["search_depth"] == "basic"
    assert "time_range" not in body
    assert "include_domains" not in body

    assert len(results) == 2
    assert results[0].title == "T1"
    assert results[0].url == "https://example.com/1"
    assert results[0].snippet == "snip 1"


def test_tavily_time_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    _patch_httpx_client(monkeypatch, _mock_transport(captured))
    backend = search.TavilyBackend(api_key="tvly-test")
    list(backend.search("q", time="w"))
    assert captured["json"]["time_range"] == "week"


def test_tavily_unknown_time_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    _patch_httpx_client(monkeypatch, _mock_transport(captured))
    backend = search.TavilyBackend(api_key="tvly-test")
    list(backend.search("q", time="garbage"))
    assert "time_range" not in captured["json"]


def test_tavily_site_maps_to_include_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    _patch_httpx_client(monkeypatch, _mock_transport(captured))
    backend = search.TavilyBackend(api_key="tvly-test")
    list(backend.search("q", site="doc.rust-lang.org"))
    assert captured["json"]["include_domains"] == ["doc.rust-lang.org"]


def test_tavily_missing_key_raises() -> None:
    with pytest.raises(ValueError, match="Tavily API key"):
        search.TavilyBackend(api_key="")


def test_get_searcher_tavily_with_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-from-env")
    backend = search.get_searcher("tavily")
    assert isinstance(backend, search.TavilyBackend)


def test_get_searcher_tavily_prefers_canonical_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-canonical")
    monkeypatch.setenv("VASCO_TAVILY_API_KEY", "tvly-prefixed")
    assert search._resolve_tavily_key(cfg=None) == "tvly-canonical"


def test_get_searcher_tavily_falls_back_to_vasco_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("VASCO_TAVILY_API_KEY", "tvly-prefixed")
    assert search._resolve_tavily_key(cfg=None) == "tvly-prefixed"


def test_get_searcher_tavily_reads_cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("VASCO_TAVILY_API_KEY", raising=False)

    class _StubCfg:
        class tavily:  # noqa: N801
            api_key = "tvly-from-cfg"

    assert search._resolve_tavily_key(cfg=_StubCfg()) == "tvly-from-cfg"


def test_get_searcher_tavily_no_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("VASCO_TAVILY_API_KEY", raising=False)
    with pytest.raises(ValueError, match="Tavily API key"):
        search.get_searcher("tavily")


def test_get_searcher_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown search backend"):
        search.get_searcher("kagi")
