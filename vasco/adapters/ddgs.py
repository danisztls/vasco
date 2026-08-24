# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from collections.abc import Iterator

from vasco.search import SearchResult

# All current ddgs text backends EXCEPT "grokipedia" (xAI's Wikipedia clone).
# Default `backend="auto"` prepends grokipedia + wikipedia ahead of the real
# search engines, and grokipedia's typeahead API is flaky enough to raise
# DDGSException for the whole call. Pinning the list keeps coverage parity
# with auto while dropping the unstable provider.
_DDGS_TEXT_BACKENDS = "google,bing,brave,duckduckgo,mojeek,yahoo,yandex,wikipedia"


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
        from ddgs.exceptions import DDGSException

        full_query = f"site:{site} {query}" if site else query
        try:
            with DDGS() as client:
                items = list(
                    client.text(
                        full_query,
                        region=region,
                        timelimit=time,
                        max_results=max_results,
                        backend=_DDGS_TEXT_BACKENDS,
                    )
                )
        except DDGSException as exc:
            if "no results" in str(exc).lower():
                return
            raise
        for item in items:
            title = item.get("title") or ""
            url = item.get("href") or item.get("url") or ""
            snippet = item.get("body") or item.get("snippet") or ""
            yield SearchResult(title=title, url=url, snippet=snippet)
