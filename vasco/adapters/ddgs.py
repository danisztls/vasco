# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from collections.abc import Iterator
from time import sleep

from vasco.search import SearchResult

# Pinned rather than `backend="auto"`, which prepends grokipedia (xAI's Wikipedia
# clone) whose flaky typeahead API can raise DDGSException for the whole call.
# Left out on purpose: bing/google/yandex (disabled upstream; naming them only
# logs a warning), brave (scraping it gets rate limited; adapters/brave.py uses
# its API instead) and startpage (returned nothing when tested).
_DDGS_TEXT_BACKENDS = "duckduckgo,mojeek,yahoo,wikipedia"

# ddgs reports an engine's 403/429 as "No results found", so an empty answer is
# often transient: in a replay of 40 empty queries, 26 returned results on retry.
_EMPTY_RETRY_DELAY_SECONDS = 4.0


def _text_search(
    query: str, *, max_results: int, region: str, time: str | None
) -> list[SearchResult]:
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException

    try:
        with DDGS() as client:
            items = list(
                client.text(
                    query,
                    region=region,
                    timelimit=time,
                    max_results=max_results,
                    backend=_DDGS_TEXT_BACKENDS,
                )
            )
    except DDGSException as exc:
        if "no results" in str(exc).lower():
            return []
        raise
    results = [
        SearchResult(
            title=item.get("title") or "",
            url=item.get("href") or item.get("url") or "",
            snippet=item.get("body") or item.get("snippet") or "",
        )
        for item in items
    ]
    # A row without a URL gives the agent nothing to fetch.
    return [result for result in results if result.url]


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
        from ddgs.exceptions import DDGSException

        full_query = f"site:{site} {query}" if site else query
        kwargs = {"max_results": max_results, "region": region, "time": time}
        results = _text_search(full_query, **kwargs)
        if not results:
            sleep(_EMPTY_RETRY_DELAY_SECONDS)
            try:
                results = _text_search(full_query, **kwargs)
            except DDGSException:
                # The retry is a bonus: a failed one must not turn an empty
                # answer into an error.
                return
        yield from results
