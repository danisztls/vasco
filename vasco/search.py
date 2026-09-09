# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from vasco import telemetry
from vasco.quality import smuggling


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


def _guard_result(result: SearchResult, cfg: Any | None) -> SearchResult:
    """Strip an encoded prompt payload out of one search result.

    A result has no failure channel, and dropping it outright would throw away
    the URL — which is the useful part, and which the agent can still fetch (that
    fetch then fails honestly with ``PROMPT_SMUGGLING``). So the payload is
    removed from the title and the snippet is replaced by a marker, rather than
    the whole row disappearing. As everywhere else, the decoded payload goes only
    to the log.
    """
    hits = smuggling.scan_fields({"title": result.title, "snippet": result.snippet})
    if not hits:
        return result
    telemetry.record_smuggling(
        cfg,
        "search",
        url=result.url,
        payloads=smuggling.log_records(hits),
        action="redacted",
    )
    kinds = ", ".join(sorted({hit.kind for hit in hits}))
    return SearchResult(
        title=smuggling.redact(result.title),
        url=result.url,
        snippet=f"[snippet withheld: hidden-text payload detected ({kinds})]",
    )


class _GuardedSearcher:
    """Wraps any backend so smuggled payloads never reach the agent.

    Applied by `get_searcher`, so a future backend inherits the guard instead of
    having to remember it.
    """

    def __init__(self, inner: Searcher, cfg: Any | None) -> None:
        self._inner = inner
        self._cfg = cfg

    def search(self, query: str, **kwargs: Any) -> Iterator[SearchResult]:
        enabled = smuggling.enabled(self._cfg)
        for result in self._inner.search(query, **kwargs):
            yield _guard_result(result, self._cfg) if enabled else result


def get_searcher(backend: str = "ddg", *, cfg: Any | None = None) -> Searcher:
    if backend in ("ddg", "ddgs", "duckduckgo"):
        from vasco.adapters.ddgs import DdgsBackend

        return _GuardedSearcher(DdgsBackend(), cfg)
    raise ValueError(f"unknown search backend: {backend!r}")
