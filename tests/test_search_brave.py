# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Any

import httpx
import pytest

from vasco import search
from vasco.adapters.brave import BraveBackend, BraveSearchError
from vasco.config import Config, SearchCfg

_HIT = {"title": "t", "url": "https://example.com/", "description": "d"}


def _backend(
    answer: httpx.Response | Exception, requests: list[httpx.Request] | None = None
) -> BraveBackend:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return BraveBackend("secret-key", transport=httpx.MockTransport(handler))


def _sent_params(**search_kwargs: Any) -> dict[str, str]:
    requests: list[httpx.Request] = []
    backend = _backend(httpx.Response(200, json={}), requests)
    list(backend.search("q", **search_kwargs))
    return dict(requests[0].url.params)


def test_maps_results_and_authenticates() -> None:
    requests: list[httpx.Request] = []
    body = {"web": {"results": [_HIT]}}
    results = list(_backend(httpx.Response(200, json=body), requests).search("q"))
    assert results == [search.SearchResult("t", "https://example.com/", "d")]
    assert requests[0].headers["X-Subscription-Token"] == "secret-key"
    assert "secret-key" not in str(requests[0].url)


def test_html_entities_are_decoded() -> None:
    hit = {"title": "Lula&#x27;s", "url": "https://e.com/", "description": "a &amp; b"}
    body = {"web": {"results": [hit]}}
    [result] = _backend(httpx.Response(200, json=body)).search("q")
    assert (result.title, result.snippet) == ("Lula's", "a & b")


def test_no_web_block_is_empty_not_an_error() -> None:
    assert list(_backend(httpx.Response(200, json={"query": {}})).search("q")) == []


def test_rows_without_a_url_are_dropped() -> None:
    body = {"web": {"results": [{"title": "no url"}, _HIT]}}
    results = list(_backend(httpx.Response(200, json=body)).search("q"))
    assert [r.url for r in results] == ["https://example.com/"]


def test_site_becomes_a_query_operator() -> None:
    assert _sent_params(site="example.com")["q"] == "site:example.com q"


def test_count_is_capped_at_the_api_limit() -> None:
    assert _sent_params(max_results=50)["count"] == "20"
    assert _sent_params(max_results=5)["count"] == "5"


@pytest.mark.parametrize(
    ("time", "freshness"),
    [
        ("d", "pd"),
        ("w", "pw"),
        ("m", "pm"),
        ("y", "py"),
        ("2026-01-01to2026-02-01",) * 2,
    ],
)
def test_time_maps_to_freshness(time: str, freshness: str) -> None:
    assert _sent_params(time=time)["freshness"] == freshness


def test_no_time_sends_no_freshness() -> None:
    assert "freshness" not in _sent_params()


@pytest.mark.parametrize(
    ("region", "country", "search_lang"),
    [
        ("us-en", "US", "en"),
        ("br-pt", "BR", "pt-br"),
        ("pt-br", "BR", "pt-br"),
        ("pt-pt", "PT", "pt-pt"),
        ("uk-en", "GB", "en-gb"),
        ("tw-tzh", "TW", "zh-hant"),
        ("jp-jp", "JP", "ja"),
        # Neither part is in the API's enums, which would answer 422.
        ("wt-wt", "ALL", None),
        ("xa-xx", "ALL", None),
    ],
)
def test_region_maps_to_country_and_language(
    region: str, country: str, search_lang: str | None
) -> None:
    params = _sent_params(region=region)
    assert params["country"] == country
    assert params.get("search_lang") == search_lang


def test_api_error_carries_the_status_and_detail() -> None:
    body = {"error": {"detail": "Rate limit exceeded", "status": 429}}
    with pytest.raises(BraveSearchError, match="429: Rate limit exceeded"):
        list(_backend(httpx.Response(429, json=body)).search("q"))


def test_api_error_without_json_detail() -> None:
    with pytest.raises(BraveSearchError, match="502"):
        list(_backend(httpx.Response(502, text="<html>")).search("q"))


def test_transport_error_is_wrapped_without_the_key() -> None:
    with pytest.raises(BraveSearchError) as excinfo:
        list(_backend(httpx.ConnectTimeout("timed out")).search("q"))
    assert "secret-key" not in str(excinfo.value)


def test_malformed_json_is_an_error() -> None:
    with pytest.raises(BraveSearchError, match="malformed"):
        list(_backend(httpx.Response(200, text="not json")).search("q"))


def test_get_searcher_returns_guarded_brave_backend() -> None:
    cfg = Config(search=SearchCfg(brave_api_key="k"))
    searcher = search.get_searcher("brave", cfg=cfg)
    assert isinstance(searcher, search._GuardedSearcher)
    assert isinstance(searcher._inner, BraveBackend)


@pytest.mark.parametrize("cfg", [None, Config()])
def test_get_searcher_without_a_key_says_so(cfg: Config | None) -> None:
    with pytest.raises(ValueError, match=r"search\.brave_api_key"):
        search.get_searcher("brave", cfg=cfg)
