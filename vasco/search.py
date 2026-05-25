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
        from vasco.adapters.ddgs import DdgsBackend

        return DdgsBackend()
    if backend == "tavily":
        from vasco.adapters.tavily import TavilyBackend

        return TavilyBackend(_resolve_tavily_key(cfg))
    raise ValueError(f"unknown search backend: {backend!r}")
