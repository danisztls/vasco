# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from collections.abc import Iterator
from html import unescape
from typing import Any

import httpx

from vasco.search import SearchResult, canonical_region

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_TIMEOUT_SECONDS = 10.0
# The API's per-request cap. Going past it would need one billed request per page.
_MAX_COUNT = 20

_FRESHNESS = {"d": "pd", "w": "pw", "m": "pm", "y": "py"}

# The API answers 422 to a value outside these enums, so a `region` it doesn't
# cover has to be dropped rather than forwarded.
_COUNTRIES = frozenset(
    [
        "AR",
        "AU",
        "AT",
        "BE",
        "BR",
        "CA",
        "CL",
        "DK",
        "FI",
        "FR",
        "DE",
        "GR",
        "HK",
        "IN",
        "ID",
        "IT",
        "JP",
        "KR",
        "MY",
        "MX",
        "NL",
        "NZ",
        "NO",
        "CN",
        "PL",
        "PT",
        "PH",
        "RU",
        "SA",
        "ZA",
        "ES",
        "SE",
        "CH",
        "TW",
        "TR",
        "GB",
        "US",
    ]
)
_LANGUAGES = frozenset(
    [
        "ar",
        "eu",
        "bn",
        "bg",
        "ca",
        "hr",
        "cs",
        "da",
        "nl",
        "en",
        "et",
        "fi",
        "fr",
        "gl",
        "de",
        "el",
        "gu",
        "he",
        "hi",
        "hu",
        "is",
        "it",
        "ja",
        "kn",
        "ko",
        "lv",
        "lt",
        "ms",
        "ml",
        "mr",
        "nb",
        "pl",
        "pa",
        "ro",
        "ru",
        "sr",
        "sk",
        "sl",
        "es",
        "sv",
        "ta",
        "te",
        "th",
        "tr",
        "uk",
        "vi",
    ]
)
# Regions are DuckDuckGo codes (`<country>-<language>`); these differ from Brave's.
_COUNTRY_ALIASES = {"uk": "GB"}
_LANGUAGE_ALIASES = {
    "br-pt": "pt-br",
    "pt-pt": "pt-pt",
    "uk-en": "en-gb",
    "cn-zh": "zh-hans",
    "hk-tzh": "zh-hant",
    "tw-tzh": "zh-hant",
    "jp-jp": "ja",
    "kr-kr": "ko",
    "no-no": "nb",
}


class BraveSearchError(RuntimeError):
    """A Brave API call failed, as opposed to answering with no results."""


def _region_params(region: str) -> dict[str, str]:
    normalized = canonical_region(region)
    code, _, language = normalized.partition("-")
    country = _COUNTRY_ALIASES.get(code, code.upper())
    params = {"country": country if country in _COUNTRIES else "ALL"}
    search_lang = _LANGUAGE_ALIASES.get(normalized, language)
    if search_lang in _LANGUAGES or normalized in _LANGUAGE_ALIASES:
        params["search_lang"] = search_lang
    return params


def _error_detail(response: httpx.Response) -> str:
    try:
        detail = response.json()["error"]["detail"]
    except (ValueError, KeyError, TypeError):
        return response.reason_phrase
    return str(detail)


class BraveBackend:
    def __init__(
        self, api_key: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._api_key = api_key
        self._transport = transport

    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        region: str = "us-en",
        time: str | None = None,
        site: str | None = None,
    ) -> Iterator[SearchResult]:
        params: dict[str, Any] = {
            "q": f"site:{site} {query}" if site else query,
            "count": min(max_results, _MAX_COUNT),
            "result_filter": "web",
            # Otherwise snippets arrive with <strong> highlight markup.
            "text_decorations": "false",
            **_region_params(region),
        }
        if time:
            # Anything else is passed through for Brave's own date-range syntax.
            params["freshness"] = _FRESHNESS.get(time, time)
        headers = {"X-Subscription-Token": self._api_key, "Accept": "application/json"}
        try:
            with httpx.Client(
                timeout=_TIMEOUT_SECONDS, transport=self._transport
            ) as client:
                response = client.get(_ENDPOINT, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise BraveSearchError(f"Brave search request failed: {exc}") from exc
        if response.status_code != 200:
            raise BraveSearchError(
                f"Brave search returned {response.status_code}: "
                f"{_error_detail(response)}"
            )
        try:
            # No `web` block is how the API says a query matched nothing.
            items = response.json().get("web", {}).get("results", [])
        except (ValueError, AttributeError) as exc:
            raise BraveSearchError("Brave search returned malformed JSON") from exc
        for item in items:
            url = item.get("url") or ""
            if url:
                # The API leaves HTML entities (&#x27;) in its text fields.
                yield SearchResult(
                    title=unescape(item.get("title") or ""),
                    url=url,
                    snippet=unescape(item.get("description") or ""),
                )
