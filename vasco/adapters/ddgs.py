# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from collections.abc import Iterator
from functools import cache
from time import sleep
from typing import Any

from vasco.search import SearchResult, canonical_region

# Pinned rather than `backend="auto"`, which prepends grokipedia (xAI's Wikipedia
# clone) whose flaky typeahead API can raise DDGSException for the whole call.
# Left out on purpose: bing/google/yandex (disabled upstream; naming them only
# logs a warning), brave (scraping it gets rate limited; adapters/brave.py uses
# its API instead) and startpage (returned nothing when tested).
_DDGS_TEXT_BACKENDS = "duckduckgo,mojeek,yahoo,wikipedia"

# ddgs reports an engine's 403/429 as "No results found", so an empty answer is
# often transient: in a replay of 40 empty queries, 26 returned results on retry.
_EMPTY_RETRY_DELAY_SECONDS = 4.0

_BING_AD_PREFIX = "https://www.bing.com/aclick?"
# Extra rows to ask for, so dropping ads still leaves `max_results`. Up to 3 of the
# top 10 were ads when tested.
_AD_ROWS_ALLOWANCE = 5

# Only hosts tested to change the results. Other regions keep Yahoo's US pages.
_YAHOO_REGION_HOSTS = {"br-pt": "br.search.yahoo.com"}


@cache
def _honor_region() -> None:
    """Make the two engines that answer most searches respect `region`, which ddgs
    doesn't: for br-pt both returned the same US pages as us-en. ddgs reads engine
    classes from this registry, so swapping the entries is the only way in."""
    from ddgs.engines import ENGINES
    from ddgs.engines.duckduckgo import Duckduckgo
    from ddgs.engines.yahoo import Yahoo

    class _RegionalDuckduckgo(Duckduckgo):
        # ddgs sends the region as `l`, which DuckDuckGo ignores.
        def build_payload(
            self, query: str, region: str, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            payload = super().build_payload(query, region, *args, **kwargs)
            return {**payload, "kl": region}

    class _RegionalYahoo(Yahoo):
        # ddgs ignores the region here; Yahoo goes by the host instead.
        def build_payload(
            self, query: str, region: str, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            payload = super().build_payload(query, region, *args, **kwargs)
            if host := _YAHOO_REGION_HOSTS.get(region):
                self.search_url = self.search_url.replace("search.yahoo.com", host)
            return payload

    ENGINES["text"]["duckduckgo"] = _RegionalDuckduckgo
    ENGINES["text"]["yahoo"] = _RegionalYahoo


def _text_search(
    query: str, *, max_results: int, region: str, time: str | None
) -> list[SearchResult]:
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException

    _honor_region()
    try:
        with DDGS() as client:
            items = list(
                client.text(
                    query,
                    region=region,
                    timelimit=time,
                    max_results=max_results + _AD_ROWS_ALLOWANCE,
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
    # A row without a URL gives the agent nothing to fetch, and Yahoo's Bing ad
    # links get past ddgs's own filter.
    return [
        result
        for result in results
        if result.url and not result.url.startswith(_BING_AD_PREFIX)
    ][:max_results]


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
        kwargs = {
            "max_results": max_results,
            "region": canonical_region(region),
            "time": time,
        }
        first_error: DDGSException | None = None
        try:
            results = _text_search(full_query, **kwargs)
        except DDGSException as exc:
            # Timeouts are as transient as empty answers, so they get the retry too.
            results, first_error = [], exc
        if not results:
            sleep(_EMPTY_RETRY_DELAY_SECONDS)
            try:
                results = _text_search(full_query, **kwargs)
            except DDGSException:
                # The retry is a bonus: a failed one must not turn an empty
                # answer into an error.
                if first_error is not None:
                    raise first_error from None
                return
        yield from results
