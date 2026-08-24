# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for `vasco.map.map_site` — the dedupe + limit + exclude logic.

The trafilatura helpers (`sitemap_search`, `find_feed_urls`, `focused_crawler`)
are stubbed out; we exercise the merge/filter machinery in isolation.
"""

from __future__ import annotations

from typing import Any

import pytest

from vasco import map as _map


def _patch_streams(
    monkeypatch: pytest.MonkeyPatch,
    *,
    llmstxt: str | None = None,
    sitemap: list[str] | None = None,
    feeds: list[str] | None = None,
    spider: list[str] | None = None,
) -> None:
    def _iter_llmstxt(url: str) -> Any:
        if llmstxt is not None:
            yield {
                "url": f"{url}/llms.txt",
                "source": "llmstxt",
                "content": llmstxt,
                "lastmod": None,
            }

    def _iter_sitemap(url: str) -> Any:
        for u in sitemap or []:
            yield {"url": u, "source": "sitemap", "lastmod": None}

    def _iter_feeds(url: str) -> Any:
        for u in feeds or []:
            yield {"url": u, "source": "feed", "lastmod": None}

    def _iter_spider(url: str, *, limit: int) -> Any:
        for u in spider or []:
            yield {"url": u, "source": "spider", "lastmod": None}

    monkeypatch.setattr(_map, "_iter_llmstxt", _iter_llmstxt)
    monkeypatch.setattr(_map, "_iter_sitemap", _iter_sitemap)
    monkeypatch.setattr(_map, "_iter_feeds", _iter_feeds)
    monkeypatch.setattr(_map, "_iter_spider", _iter_spider)


def test_exclude_filters_substring_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_streams(
        monkeypatch,
        sitemap=[
            "https://x.test/posts/a",
            "https://x.test/team/alice",
            "https://x.test/team/bob",
            "https://x.test/posts/b",
            "https://x.test/tag/python",
        ],
    )
    records = list(_map.map_site("https://x.test", exclude=["/team/", "/tag/"]))
    urls = [r["url"] for r in records]
    assert urls == [
        "https://x.test/posts/a",
        "https://x.test/posts/b",
    ]


def test_exclude_none_keeps_all(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_streams(monkeypatch, sitemap=["https://x.test/a", "https://x.test/b"])
    records = list(_map.map_site("https://x.test"))
    assert {r["url"] for r in records} == {"https://x.test/a", "https://x.test/b"}


def test_exclude_empty_list_keeps_all(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_streams(monkeypatch, sitemap=["https://x.test/a"])
    records = list(_map.map_site("https://x.test", exclude=[]))
    assert {r["url"] for r in records} == {"https://x.test/a"}


def test_exclude_empty_pattern_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `""` entry shouldn't match every URL."""
    _patch_streams(monkeypatch, sitemap=["https://x.test/a", "https://x.test/b"])
    records = list(_map.map_site("https://x.test", exclude=["", "/never/"]))
    assert {r["url"] for r in records} == {"https://x.test/a", "https://x.test/b"}


def test_dedupe_across_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_streams(
        monkeypatch,
        sitemap=["https://x.test/a", "https://x.test/b"],
        feeds=["https://x.test/a", "https://x.test/c"],
    )
    urls = [r["url"] for r in _map.map_site("https://x.test", source="all")]
    assert urls == ["https://x.test/a", "https://x.test/b", "https://x.test/c"]


def test_limit_caps_output(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_streams(
        monkeypatch,
        sitemap=[f"https://x.test/{i}" for i in range(20)],
    )
    records = list(_map.map_site("https://x.test", limit=5))
    assert len(records) == 5


def test_llmstxt_appears_first(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_streams(
        monkeypatch,
        llmstxt="# Site\n- [Docs](https://x.test/docs)",
        sitemap=["https://x.test/a", "https://x.test/b"],
    )
    records = list(_map.map_site("https://x.test", source="all"))
    assert records[0]["source"] == "llmstxt"
    assert records[0]["content"] == "# Site\n- [Docs](https://x.test/docs)"


def test_llmstxt_source_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_streams(
        monkeypatch,
        llmstxt="# Docs",
        sitemap=["https://x.test/a"],
    )
    records = list(_map.map_site("https://x.test", source="llmstxt"))
    assert len(records) == 1
    assert records[0]["source"] == "llmstxt"


def test_llmstxt_dedup_with_sitemap(monkeypatch: pytest.MonkeyPatch) -> None:
    """llmstxt URL participates in dedup — sitemap won't re-emit it."""
    _patch_streams(
        monkeypatch,
        llmstxt="# Site",
        sitemap=["https://x.test/llms.txt", "https://x.test/other"],
    )
    records = list(_map.map_site("https://x.test", source="all"))
    urls = [r["url"] for r in records]
    assert urls.count("https://x.test/llms.txt") == 1
