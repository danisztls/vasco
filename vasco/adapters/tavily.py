from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from vasco.search import SearchResult

_TAVILY_API_URL = "https://api.tavily.com/search"
_TAVILY_TIME_MAP = {"d": "day", "w": "week", "m": "month", "y": "year"}


class TavilyBackend:
    """Tavily search API backend. Implements the ``Searcher`` Protocol."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError(
                "Tavily API key not configured. "
                "Set TAVILY_API_KEY or tavily.api_key in config.yaml."
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
