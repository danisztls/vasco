from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class Searcher(Protocol):
    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        region: str = "us-en",
        time: str | None = None,
        site: str | None = None,
    ) -> Iterator[SearchResult]: ...


class DdgsBackend:
    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        region: str = "us-en",
        time: str | None = None,
        site: str | None = None,
    ) -> Iterator[SearchResult]:
        from ddgs import DDGS

        full_query = f"site:{site} {query}" if site else query
        with DDGS() as client:
            items = list(
                client.text(
                    full_query,
                    region=region,
                    timelimit=time,
                    max_results=max_results,
                )
            )
        for item in items:
            title = item.get("title") or ""
            url = item.get("href") or item.get("url") or ""
            snippet = item.get("body") or item.get("snippet") or ""
            yield SearchResult(title=title, url=url, snippet=snippet)


_TAVILY_API_URL = "https://api.tavily.com/search"
_TAVILY_TIME_MAP = {"d": "day", "w": "week", "m": "month", "y": "year"}


class TavilyBackend:
    """Tavily search API backend. Implements the ``Searcher`` Protocol."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError(
                "Tavily API key not configured. "
                "Set TAVILY_API_KEY or [tavily] api_key in config.toml."
            )
        self._api_key = api_key

    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        region: str = "us-en",  # noqa: ARG002 — Tavily has no region selector
        time: str | None = None,
        site: str | None = None,
    ) -> Iterator[SearchResult]:
        import httpx

        payload: dict[str, Any] = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }
        if time and time in _TAVILY_TIME_MAP:
            payload["time_range"] = _TAVILY_TIME_MAP[time]
        if site:
            payload["include_domains"] = [site]

        with httpx.Client(timeout=15.0) as client:
            resp = client.post(_TAVILY_API_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()

        for item in data.get("results", []) or []:
            yield SearchResult(
                title=item.get("title") or "",
                url=item.get("url") or "",
                snippet=item.get("content") or item.get("snippet") or "",
            )


def _resolve_tavily_key(cfg: Any | None) -> str:
    """Resolve the Tavily API key from env → config, in that order."""
    env_key = os.environ.get("TAVILY_API_KEY") or os.environ.get("VASCO_TAVILY_API_KEY")
    if env_key:
        return env_key
    if cfg is not None:
        try:
            return cfg.tavily.api_key or ""
        except AttributeError:
            return ""
    return ""


def get_searcher(backend: str = "ddg", *, cfg: Any | None = None) -> Searcher:
    if backend in ("ddg", "ddgs", "duckduckgo"):
        return DdgsBackend()
    if backend == "tavily":
        return TavilyBackend(_resolve_tavily_key(cfg))
    raise ValueError(f"unknown search backend: {backend!r}")
