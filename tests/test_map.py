# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for `vasco.map.map_site` — the dedupe + limit + exclude logic.

The trafilatura helpers (`sitemap_search`, `find_feed_urls`, `focused_crawler`)
are stubbed out; we exercise the merge/filter machinery in isolation.
"""

from __future__ import annotations

import threading
import time
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

    def _iter_sitemap(url: str, *, max_sitemaps: int) -> Any:
        for u in sitemap or []:
            yield {"url": u, "source": "sitemap", "lastmod": None}

    def _iter_feeds(url: str) -> Any:
        for u in feeds or []:
            yield {"url": u, "source": "feed", "lastmod": None}

    def _iter_spider(url: str, *, limit: int, pages: int, deadline: float) -> Any:
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
    assert [r["source"] for r in records] == ["sitemap"] * 5 + ["notice"]
    assert "limit of 5" in records[-1]["note"]


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


def test_exactly_limit_urls_has_no_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_streams(monkeypatch, sitemap=[f"https://x.test/{i}" for i in range(5)])
    records = list(_map.map_site("https://x.test", limit=5))
    assert len(records) == 5
    assert all(r["source"] == "sitemap" for r in records)


def test_budget_keeps_results_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def slow_sitemap(url: str, *, max_sitemaps: int) -> Any:
        yield {"url": "https://x.test/1", "source": "sitemap", "lastmod": None}
        release.wait(5)

    _patch_streams(monkeypatch)
    monkeypatch.setattr(_map, "_iter_sitemap", slow_sitemap)
    started = time.monotonic()
    records = list(_map.map_site("https://x.test", source="sitemap", budget=0.05))
    release.set()
    assert time.monotonic() - started < 2
    assert [r["source"] for r in records] == ["sitemap", "notice"]
    assert "budget" in records[-1]["note"]


def test_quick_skips_feeds_and_spider(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_streams(
        monkeypatch,
        sitemap=["https://x.test/s"],
        feeds=["https://x.test/f"],
        spider=["https://x.test/p"],
    )
    records = list(_map.map_site("https://x.test", size="quick"))
    assert [r["url"] for r in records] == ["https://x.test/s"]


def test_explicit_source_overrides_size(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_streams(monkeypatch, spider=["https://x.test/p"])
    records = list(_map.map_site("https://x.test", source="spider", size="quick"))
    assert [r["url"] for r in records] == ["https://x.test/p"]


def test_size_sets_default_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_streams(monkeypatch, sitemap=[f"https://x.test/{i}" for i in range(300)])
    urls = lambda size: [  # noqa: E731
        r for r in _map.map_site("https://x.test", size=size) if r["source"] != "notice"
    ]
    assert len(urls("quick")) == 50
    assert len(urls("standard")) == 200
    assert len(urls("full")) == 300


def test_unknown_size_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_streams(monkeypatch)
    with pytest.raises(ValueError, match="unknown map size"):
        list(_map.map_site("https://x.test", size="huge"))


def test_llmstxt_body_capped_by_size(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_streams(monkeypatch, llmstxt="x" * 5000)
    quick = next(_map.map_site("https://x.test", size="quick"))
    assert len(quick["content"]) == 4000
    assert quick["content_truncated"] is True
    standard = next(_map.map_site("https://x.test"))
    assert len(standard["content"]) == 5000
    assert "content_truncated" not in standard


def test_html_llmstxt_is_discarded(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    class _Resp:
        status_code = 200
        content = b"<!DOCTYPE html><html></html>"
        text = "<!DOCTYPE html><html></html>"

    monkeypatch.setattr(_map.httpx, "get", lambda *a, **kw: _Resp())
    assert _map._fetch_llmstxt("https://x.test") == (None, None)


def test_spider_runs_in_steps_up_to_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    from trafilatura import spider

    calls: list[int] = []

    def fake_crawler(url: str, *, max_seen_urls: int, **kw: Any) -> Any:
        calls.append(max_seen_urls)
        known = [f"{url}/{i}" for i in range(len(calls))]
        return ["todo"], known

    monkeypatch.setattr(spider, "focused_crawler", fake_crawler)
    records = list(
        _map._iter_spider(
            "https://x.test", limit=100, pages=12, deadline=time.monotonic() + 5
        )
    )
    assert calls == [5, 5, 2]
    assert [r["url"] for r in records] == [f"https://x.test/{i}" for i in range(3)]


def test_spider_stops_at_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    from trafilatura import spider

    monkeypatch.setattr(
        spider, "focused_crawler", lambda *a, **kw: pytest.fail("must not crawl")
    )
    assert not list(
        _map._iter_spider(
            "https://x.test", limit=100, pages=12, deadline=time.monotonic() - 1
        )
    )


def _patch_sitemaps(
    monkeypatch: pytest.MonkeyPatch,
    found: dict[str, list[str]],
    robots: dict[str, list[str]] | None = None,
) -> list[str]:
    """Stub the walk and robots.txt; returns the URLs the walk was given."""
    searched: list[str] = []

    def walk(url: str, *, max_sitemaps: int) -> tuple[list[str], int]:
        searched.append(url)
        return found.get(url, []), 0

    monkeypatch.setattr(_map, "_walk_sitemaps", walk)
    monkeypatch.setattr(
        "trafilatura.sitemaps.find_robots_sitemaps",
        lambda base: (robots or {}).get(base, []),
    )
    return searched


def _sitemap_urls(url: str) -> list[str]:
    return [r["url"] for r in _map._iter_sitemap(url, max_sitemaps=3)]


def test_sitemap_falls_back_to_the_urls_own_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = "https://x.test/v1/sitemap_index.xml"
    _patch_sitemaps(monkeypatch, {index: ["https://x.test/v1/a"]})
    assert _sitemap_urls("https://x.test/v1/") == ["https://x.test/v1/a"]


def test_sitemap_fallback_prefers_the_directorys_robots_txt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed = "https://x.test/v1/maps/all.xml"
    searched = _patch_sitemaps(
        monkeypatch,
        {listed: ["https://x.test/v1/a"]},
        robots={"https://x.test/v1": [listed]},
    )
    assert _sitemap_urls("https://x.test/v1") == ["https://x.test/v1/a"]
    assert searched == ["https://x.test/v1", listed]


def test_sitemap_found_at_the_root_skips_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searched = _patch_sitemaps(
        monkeypatch, {"https://x.test/v1/": ["https://x.test/v1/a"]}
    )
    assert _sitemap_urls("https://x.test/v1/") == ["https://x.test/v1/a"]
    assert searched == ["https://x.test/v1/"]


@pytest.mark.parametrize(
    "url", ["https://x.test/", "https://x.test", "https://x.test/maps/all.xml"]
)
def test_sitemap_fallback_needs_a_directory(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    searched = _patch_sitemaps(monkeypatch, {})
    assert _sitemap_urls(url) == []
    assert searched == [url]


def _sitemap_site(
    monkeypatch: pytest.MonkeyPatch,
    pages: dict[str, str],
    robots: list[str] | None = None,
) -> None:
    """Run the real walk over `pages`, a stand-in for the site's sitemap files."""
    monkeypatch.setattr(_map, "_SITEMAP_DELAY_SECONDS", 0)
    monkeypatch.setattr("trafilatura.sitemaps.is_live_page", lambda *a, **k: True)
    monkeypatch.setattr(
        "trafilatura.sitemaps.find_robots_sitemaps", lambda *a, **k: robots or []
    )
    monkeypatch.setattr(
        "trafilatura.sitemaps.fetch_url", lambda url, **k: pages.get(url, "")
    )


def _urlset(*urls: str) -> str:
    locs = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return (
        '<?xml version="1.0"?><urlset '
        f'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'
    )


def _sitemapindex(*urls: str) -> str:
    locs = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls)
    return (
        '<?xml version="1.0"?><sitemapindex '
        f'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</sitemapindex>'
    )


def _big_site() -> dict[str, str]:
    children = [f"https://www.example.com/sitemap-{i}.xml" for i in range(5)]
    pages = {"https://www.example.com/sitemap.xml": _sitemapindex(*children)}
    for i, child in enumerate(children):
        pages[child] = _urlset(f"https://www.example.com/page-{i}")
    return pages


def test_map_notes_sitemaps_left_unread(monkeypatch: pytest.MonkeyPatch) -> None:
    _sitemap_site(monkeypatch, _big_site())
    records = list(
        _map.map_site("https://www.example.com/", source="sitemap", size="quick")
    )
    # The index plus two children fit the quick cap of 3; three children don't.
    assert [r["source"] for r in records] == ["sitemap", "sitemap", "notice"]
    assert "left at least 3 unread" in records[-1]["note"]


def test_map_is_silent_when_every_sitemap_was_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sitemap_site(monkeypatch, _big_site())
    records = list(
        _map.map_site("https://www.example.com/", source="sitemap", size="standard")
    )
    assert len(records) == 5 and all(r["source"] == "sitemap" for r in records)


def test_map_unfound_guesses_are_not_unread_sitemaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap stops the walk among guessed locations here, which says nothing
    about the site having more."""
    _sitemap_site(
        monkeypatch,
        {"https://www.example.com/sitemap.xml": _urlset("https://www.example.com/a")},
    )
    records = list(
        _map.map_site("https://www.example.com/", source="sitemap", size="quick")
    )
    assert [r["url"] for r in records] == ["https://www.example.com/a"]


def test_map_robots_sitemaps_left_unread_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed = [f"https://www.example.com/s{i}.xml" for i in range(4)]
    _sitemap_site(monkeypatch, {u: _urlset(u + "-page") for u in listed}, robots=listed)
    records = list(
        _map.map_site("https://www.example.com/", source="sitemap", size="quick")
    )
    assert "left at least 1 unread" in records[-1]["note"]


def test_map_page_url_still_narrows_walk_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sitemap_site(
        monkeypatch,
        {
            "https://www.example.com/sitemap.xml": _urlset(
                "https://www.example.com/blog/a", "https://www.example.com/shop/b"
            )
        },
    )
    assert _sitemap_urls("https://www.example.com/blog/") == [
        "https://www.example.com/blog/a"
    ]


def test_map_keeps_one_final_notice_when_the_limit_also_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sitemap_site(monkeypatch, _big_site())
    records = list(
        _map.map_site(
            "https://www.example.com/", source="sitemap", size="quick", limit=2
        )
    )
    assert [r["source"] for r in records] == ["sitemap", "sitemap", "notice"]
    assert "unread" in records[-1]["note"]
