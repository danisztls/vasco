from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol


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


def get_searcher(backend: str = "ddg") -> Searcher:
    if backend in ("ddg", "ddgs", "duckduckgo"):
        return DdgsBackend()
    raise ValueError(f"unknown search backend: {backend!r}")
