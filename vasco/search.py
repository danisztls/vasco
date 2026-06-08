from __future__ import annotations

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


def get_searcher(backend: str = "ddg", *, cfg: Any | None = None) -> Searcher:
    if backend in ("ddg", "ddgs", "duckduckgo"):
        from vasco.adapters.ddgs import DdgsBackend

        return DdgsBackend()
    raise ValueError(f"unknown search backend: {backend!r}")
