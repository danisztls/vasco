"""End-to-end fetch test using a real Cache (sqlite in tmp_path) but a stubbed
HTTP tier. Covers the seam between fetch_one, Cache, and url_requested
preservation — the exact gap that let the dead-CLI-cache bug slip through
unit testing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from vasco import fetch as fetch_mod
from vasco.adapters import youtube as youtube_mod
from vasco.cache import Cache
from vasco.fetch import browser as browser_mod
from vasco.fetch import core as core_mod

# A year-2100 epoch second — used as fetched_at in faked envelopes so the
# cache's TTL (current_time + 86400) is always far in the future, making
# tests independent of the wall clock at run time.
_FAR_FUTURE_FETCHED_AT = 4_102_444_800

FIXTURES = Path(__file__).parent / "fixtures"


def _disable_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NopPool:
        async def fetch(self, *a: Any, **kw: Any) -> tuple[str, int, dict[str, str]]:
            return "", 0, {}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(browser_mod, "_pool", None, raising=False)
    monkeypatch.setattr(browser_mod, "get_browser", lambda cfg=None: _NopPool())


def _stub_http(html: str, status: int = 200, headers: dict | None = None):
    async def _fake(
        url: str,
        *,
        deadline_monotonic: float,
        cfg: Any | None = None,
        profile: str = "browser",
    ) -> tuple[str, int, dict[str, str]]:
        hdrs = dict(headers or {})
        hdrs.setdefault("_url_final", url)
        return html, status, hdrs

    return _fake


def test_fetch_one_round_trips_through_real_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two fetches of the same URL: first miss, second hit, with markdown preserved."""
    html = (FIXTURES / "article_clean.html").read_text(encoding="utf-8")
    monkeypatch.setattr(core_mod, "_http_fetch", _stub_http(html, 200))
    _disable_browser(monkeypatch)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        first = asyncio.run(
            fetch_mod.fetch_one(
                "https://Example.COM/article",
                cache=cache,
                deadline=10.0,
            )
        )
        assert first["from_cache"] is False
        assert first["http_status"] == 200
        assert first["markdown"], "expected extracted markdown"
        assert "failure" not in first

        second = asyncio.run(
            fetch_mod.fetch_one(
                "https://Example.COM/article",
                cache=cache,
                deadline=10.0,
            )
        )
        assert second["from_cache"] is True
        assert second["markdown"] == first["markdown"]
        assert second["title"] == first["title"]
    finally:
        cache.close()


def test_adapter_rot_returns_parse_failed_with_short_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An adapter URL that 200s but has lost its parse anchor surfaces
    PARSE_FAILED end-to-end (through the real cache + escalation chain), and is
    negative-cached on the short transient TTL — not pinned ~24h like the
    UNSUPPORTED_CONTENT_TYPE the adapters used to return."""
    # vivareal *list* pages aren't browser-seeded (only /imovel/ detail is), so
    # they resolve at the http tier and stubbing _http_fetch drives the whole path.
    monkeypatch.setattr(
        core_mod, "_http_fetch", _stub_http("<html><body>no cards</body></html>", 200)
    )
    _disable_browser(monkeypatch)

    url = "https://www.vivareal.com.br/aluguel/sp/sao-carlos/"
    cache = Cache(str(tmp_path / "cache.db"))
    try:
        env = asyncio.run(fetch_mod.fetch_one(url, cache=cache, deadline=10.0))
        assert env["mode_used"] == "realestate"
        assert env["failure"]["reason"] == "parse_failed"
        # Short, self-healing negative TTL (0.33× base), not 96× (~24h).
        assert fetch_mod._ttl_for(env, None) == int(900 * 0.33)

        # The failure is negative-cached and re-served.
        again = asyncio.run(fetch_mod.fetch_one(url, cache=cache, deadline=10.0))
        assert again["failure"]["reason"] == "parse_failed"
    finally:
        cache.close()


def test_cache_hit_preserves_caller_url_casing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cached envelope's url_requested should reflect what the caller passed,
    not the normalized cache key.
    """
    html = (FIXTURES / "article_clean.html").read_text(encoding="utf-8")
    monkeypatch.setattr(core_mod, "_http_fetch", _stub_http(html, 200))
    _disable_browser(monkeypatch)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        # Seed: normalized form gets written.
        asyncio.run(
            fetch_mod.fetch_one(
                "https://Example.COM/foo?b=2&a=1",
                cache=cache,
                deadline=10.0,
            )
        )
        # Hit: a differently-cased equivalent URL should still get a hit,
        # but the envelope must echo the caller's exact input.
        hit = asyncio.run(
            fetch_mod.fetch_one(
                "HTTPS://example.com/foo?a=1&b=2",
                cache=cache,
                deadline=10.0,
            )
        )
        assert hit["from_cache"] is True
        assert hit["url_requested"] == "HTTPS://example.com/foo?a=1&b=2"
    finally:
        cache.close()


def test_no_cache_flag_bypasses_reads_and_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = (FIXTURES / "article_clean.html").read_text(encoding="utf-8")
    monkeypatch.setattr(core_mod, "_http_fetch", _stub_http(html, 200))
    _disable_browser(monkeypatch)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        asyncio.run(
            fetch_mod.fetch_one(
                "https://example.com/x",
                cache=cache,
                use_cache=False,
                deadline=10.0,
            )
        )
        assert cache.stats()["entries"] == 0
    finally:
        cache.close()


def test_youtube_url_routes_to_youtube_fetcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A youtube.com URL bypasses HTTP/browser tier and yields a transcript envelope.
    Both youtu.be and youtube.com forms should hit the same cache row.
    """
    calls: list[str] = []

    async def fake_fetch_youtube(url: str, *, deadline: float, cfg: Any = None) -> dict:
        calls.append(url)
        return {
            "url_requested": url,
            "url_final": url,
            "url_canonical": "https://youtube.com/watch?v=abc123",
            "http_status": 200,
            "mode_used": "youtube",
            "fetched_at": _FAR_FUTURE_FETCHED_AT,
            "from_cache": False,
            "cache_age_seconds": 0,
            "content_type": "text/youtube",
            "title": "T",
            "byline": "B",
            "published": None,
            "modified": None,
            "language": "en",
            "site_name": "YouTube",
            "word_count": 2,
            "token_count_estimate": 2,
            "quality": {},
            "markdown": "hello world",
            "warnings": [],
        }

    # HTTP tier must NOT be hit for YouTube URLs.
    def explode(*a: Any, **kw: Any) -> Any:
        raise AssertionError("HTTP tier should not run for YouTube URLs")

    monkeypatch.setattr(youtube_mod, "fetch_youtube", fake_fetch_youtube)
    monkeypatch.setattr(core_mod, "_http_fetch", explode)
    _disable_browser(monkeypatch)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        first = asyncio.run(
            fetch_mod.fetch_one("https://youtu.be/abc123", cache=cache, deadline=10.0)
        )
        assert first["mode_used"] == "youtube"
        assert first["markdown"] == "hello world"
        assert first["from_cache"] is False
        assert len(calls) == 1

        # Canonical youtube.com form should hit the same cache row (youtu.be
        # was upgraded to this exact shape by normalize_url).
        second = asyncio.run(
            fetch_mod.fetch_one(
                "https://youtube.com/watch?v=abc123",
                cache=cache,
                deadline=10.0,
            )
        )
        assert second["from_cache"] is True
        assert second["mode_used"] == "youtube"
        # youtube.fetch_youtube should NOT have been called again.
        assert len(calls) == 1
    finally:
        cache.close()


def test_youtube_raw_flag_adds_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--raw is meaningless for YouTube (no HTML); we surface a warning so
    callers can detect the mismatch instead of silently dropping the flag.
    """

    async def fake_fetch_youtube(url: str, *, deadline: float, cfg: Any = None) -> dict:
        return {
            "url_requested": url,
            "url_final": url,
            "url_canonical": url,
            "http_status": 200,
            "mode_used": "youtube",
            "fetched_at": _FAR_FUTURE_FETCHED_AT,
            "from_cache": False,
            "cache_age_seconds": 0,
            "content_type": "text/youtube",
            "title": "T",
            "byline": None,
            "published": None,
            "modified": None,
            "language": "en",
            "site_name": "YouTube",
            "word_count": 1,
            "token_count_estimate": 1,
            "quality": {},
            "markdown": "hi",
            "warnings": [],
        }

    monkeypatch.setattr(youtube_mod, "fetch_youtube", fake_fetch_youtube)
    _disable_browser(monkeypatch)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        env = asyncio.run(
            fetch_mod.fetch_one(
                "https://youtu.be/abc123", cache=cache, deadline=10.0, raw=True
            )
        )
        assert "raw_unsupported_for_youtube" in env["warnings"]
    finally:
        cache.close()


def test_refresh_flag_writes_but_ignores_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html_first = "<html><body>" + "first " * 200 + "</body></html>"
    html_second = "<html><body>" + "second " * 200 + "</body></html>"

    monkeypatch.setattr(core_mod, "_http_fetch", _stub_http(html_first, 200))
    _disable_browser(monkeypatch)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        first = asyncio.run(
            fetch_mod.fetch_one("https://example.com/y", cache=cache, deadline=10.0)
        )
        assert first["from_cache"] is False

        # Swap the upstream response and refetch with --refresh.
        monkeypatch.setattr(core_mod, "_http_fetch", _stub_http(html_second, 200))
        second = asyncio.run(
            fetch_mod.fetch_one(
                "https://example.com/y",
                cache=cache,
                refresh=True,
                deadline=10.0,
            )
        )
        # refresh ignores cached read → we should see the new content.
        assert second["from_cache"] is False
        assert "second" in (second.get("markdown") or "").lower()

        # And the cache should now hold the refreshed body.
        third = asyncio.run(
            fetch_mod.fetch_one("https://example.com/y", cache=cache, deadline=10.0)
        )
        assert third["from_cache"] is True
        assert "second" in (third.get("markdown") or "").lower()
    finally:
        cache.close()


def test_browser_tier_unavailable_degrades_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no browser server running, a browser-tier fetch fails as
    BROWSER_UNAVAILABLE rather than raising — the real BrowserPool is used
    (not the NopPool stub), pointed at a socket that doesn't exist."""
    monkeypatch.setattr(
        browser_mod, "_socket_path", lambda: str(tmp_path / "no-server.sock")
    )
    monkeypatch.setattr(browser_mod, "_pool", None, raising=False)

    env = asyncio.run(
        fetch_mod.fetch_one(
            "https://example.com", mode="browser", use_cache=False, deadline=10.0
        )
    )
    assert "failure" in env
    assert env["failure"]["reason"] == "browser_unavailable"


# --- word_count escalation + EMPTY_BODY ---------------------------------------

# A marker-less unrendered shell: classify() returns OK (no "requires JavaScript"
# notice), but trafilatura extracts zero words. This is the Facebook/empty-SPA
# shape that bot_detect cannot see and the post-conversion word_count check must.
_EMPTY_SHELL = (
    "<html><head><title>App</title></head><body>"
    '<div id="root"></div>'
    "<script>" + ("x=1;" * 500) + "</script>"
    "</body></html>"
)


def _stub_browser(
    monkeypatch: pytest.MonkeyPatch, html: str, status: int = 200
) -> None:
    class _Pool:
        async def fetch(
            self, url: str, *, deadline_monotonic: float, mobile: bool = False
        ) -> tuple[str, int, dict[str, str]]:
            return html, status, {"_url_final": url}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(browser_mod, "_pool", None, raising=False)
    monkeypatch.setattr(browser_mod, "get_browser", lambda cfg=None: _Pool())


def _stub_browser_must_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Pool:
        async def fetch(self, *a: Any, **kw: Any) -> tuple[str, int, dict[str, str]]:
            raise AssertionError(
                "browser tier must not run for a content-ful http page"
            )

        async def close(self) -> None:
            return None

    monkeypatch.setattr(browser_mod, "_pool", None, raising=False)
    monkeypatch.setattr(browser_mod, "get_browser", lambda cfg=None: _Pool())


def test_empty_http_shell_escalates_to_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An http 200 that converts to zero words is escalated to the browser tier,
    which renders real content — the Facebook case."""
    content = (FIXTURES / "article_clean.html").read_text(encoding="utf-8")
    monkeypatch.setattr(core_mod, "_http_fetch", _stub_http(_EMPTY_SHELL, 200))
    _stub_browser(monkeypatch, content, 200)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        env = asyncio.run(
            fetch_mod.fetch_one("https://example.com/post", cache=cache, deadline=10.0)
        )
        assert "failure" not in env
        assert env["mode_used"] == "browser"
        assert env["escalated_from"] == "http"
        assert env["word_count"] > 0
        assert env["markdown"]
    finally:
        cache.close()


def test_empty_everywhere_yields_empty_body_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """http shell escalates, but the browser also renders no text → a clean
    fetch-level EMPTY_BODY failure, not a cached 0-word success."""
    monkeypatch.setattr(core_mod, "_http_fetch", _stub_http(_EMPTY_SHELL, 200))
    _stub_browser(monkeypatch, "", 200)  # browser 200 but no readable text

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        env = asyncio.run(
            fetch_mod.fetch_one("https://example.com/post", cache=cache, deadline=10.0)
        )
        assert "failure" in env
        assert env["failure"]["reason"] == "empty_body"
    finally:
        cache.close()


def test_contentful_http_page_is_not_escalated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real server-rendered page (word_count > 0) stays at the http tier and
    never touches the browser — the WMF VitePress regression guard."""
    content = (FIXTURES / "article_clean.html").read_text(encoding="utf-8")
    monkeypatch.setattr(core_mod, "_http_fetch", _stub_http(content, 200))
    _stub_browser_must_not_run(monkeypatch)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        env = asyncio.run(
            fetch_mod.fetch_one(
                "https://example.com/article", cache=cache, deadline=10.0
            )
        )
        assert "failure" not in env
        assert env["mode_used"] == "http"
        assert "escalated_from" not in env
        assert env["attempts"] == 1
        assert env["word_count"] > 0
    finally:
        cache.close()


# ---------------------------------------------------------------------------
# Shopify adapter (platform JSON endpoints fetched via the shared chain)
# ---------------------------------------------------------------------------

_SHOPIFY_FX = FIXTURES / "shopify"


def _stub_http_dispatch(routes: dict[str, tuple[str, int]], default: tuple[str, int]):
    """An _http_fetch stub that serves a body by URL-suffix match.

    Shopify URLs route their *page* URL to a *different* JSON endpoint, so the
    stub keys on what the adapter actually requests (…/products.json, …/.js,
    …/cart.js). Anything unmatched gets `default` — used to model the page URL
    after a probe miss falls through to a normal HTML fetch.
    """

    async def _fake(
        url: str,
        *,
        deadline_monotonic: float,
        cfg: Any | None = None,
        profile: str = "browser",
    ) -> tuple[str, int, dict[str, str]]:
        body, status = default
        for suffix, payload in routes.items():
            if suffix in url:
                body, status = payload
                break
        return body, status, {"_url_final": url}

    return _fake


def test_shopify_collection_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A known Shopify domain's collection URL → structured envelope through the
    real cache, then a clean cache roundtrip."""
    from vasco.adapters import shopify as shopify_mod

    shopify_mod._reset_for_tests()
    body = (_SHOPIFY_FX / "collection_products.json").read_text(encoding="utf-8")
    monkeypatch.setattr(
        core_mod,
        "_http_fetch",
        _stub_http_dispatch(
            {"products.json": (body, 200), "cart.js": ('{"currency":"USD"}', 200)},
            default=("<html></html>", 200),
        ),
    )
    _disable_browser(monkeypatch)

    url = "https://simwooddenim.com/collections/jeans"
    cache = Cache(str(tmp_path / "cache.db"))
    try:
        env = asyncio.run(fetch_mod.fetch_one(url, cache=cache, deadline=10.0))
        assert env["mode_used"] == "shopify"
        assert "failure" not in env
        assert env["quality"]["provider"] == "shopify"
        assert env["quality"]["page_type"] == "collection"
        assert env["quality"]["result_count"] == 3
        assert env["quality"]["currency"] == "USD"

        again = asyncio.run(fetch_mod.fetch_one(url, cache=cache, deadline=10.0))
        assert again["from_cache"] is True
        assert again["quality"]["result_count"] == 3
    finally:
        cache.close()
        shopify_mod._reset_for_tests()


def test_shopify_rot_returns_parse_failed_short_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A known Shopify product endpoint that 200s but isn't JSON → PARSE_FAILED
    on the short self-healing TTL (scraper-rot heals on redeploy)."""
    from vasco.adapters import shopify as shopify_mod

    shopify_mod._reset_for_tests()
    monkeypatch.setattr(
        core_mod,
        "_http_fetch",
        _stub_http_dispatch({}, default=("<html>not shopify</html>", 200)),
    )
    _disable_browser(monkeypatch)

    url = "https://simwooddenim.com/products/widget"
    cache = Cache(str(tmp_path / "cache.db"))
    try:
        env = asyncio.run(fetch_mod.fetch_one(url, cache=cache, deadline=10.0))
        assert env["mode_used"] == "shopify"
        assert env["failure"]["reason"] == "parse_failed"
        assert fetch_mod._ttl_for(env, None) == int(900 * 0.33)
    finally:
        cache.close()
        shopify_mod._reset_for_tests()


def test_shopify_probe_miss_falls_through_to_normal_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown domain's /products/<h> URL is probed; the probe sees HTML (not
    Shopify), so the fetch falls through to a normal HTML→markdown envelope —
    never a failure."""
    from vasco.adapters import shopify as shopify_mod

    shopify_mod._reset_for_tests()
    article = (FIXTURES / "article_clean.html").read_text(encoding="utf-8")
    # Every URL (the .js probe and the page itself) returns the same HTML article.
    monkeypatch.setattr(core_mod, "_http_fetch", _stub_http(article, 200))
    _disable_browser(monkeypatch)

    url = "https://not-a-shop.example/products/widget"
    cache = Cache(str(tmp_path / "cache.db"))
    try:
        env = asyncio.run(fetch_mod.fetch_one(url, cache=cache, deadline=10.0))
        assert env["mode_used"] == "http"  # fell through to the normal path
        assert "failure" not in env
        assert env["word_count"] > 0
        assert "quality" in env and env["quality"].get("provider") != "shopify"
        # Probe proved it's not Shopify → negative-memoized.
        assert shopify_mod._probe_memo.get("not-a-shop.example") is False
    finally:
        cache.close()
        shopify_mod._reset_for_tests()


_GITLAB_FX = FIXTURES / "gitlab"


def _stub_gitlab_api(routes: dict[str, tuple[str, int]], default: tuple[str, int]):
    """Stub for `gitlab._api_get` (the adapter's own httpx seam): serves a
    (body, status) by URL-substring, defaulting otherwise. Reason is always OK
    (an HTTP response arrived); the adapter keys on the status + body shape."""
    from vasco.errors import FailureReason

    async def _fake(url: str, *, deadline: float, cfg: Any = None):
        body, status = default
        for needle, payload in routes.items():
            if needle in url:
                body, status = payload
                break
        return body, status, FailureReason.OK

    return _fake


def test_gitlab_project_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A known-host (gitlab.com) project URL → structured envelope with README
    through the real cache, then a clean cache roundtrip."""
    from vasco.adapters import gitlab as gitlab_mod

    gitlab_mod._reset_for_tests()
    project = (_GITLAB_FX / "project.json").read_text(encoding="utf-8")
    monkeypatch.setattr(
        gitlab_mod,
        "_api_get",
        _stub_gitlab_api(
            {
                "/api/v4/projects/": (project, 200),
                "/-/raw/": ("# mcp-phabricator\n\nAn MCP server.", 200),
            },
            default=("not found", 404),
        ),
    )
    _disable_browser(monkeypatch)

    url = "https://gitlab.com/egardner/mcp-phabricator"
    cache = Cache(str(tmp_path / "cache.db"))
    try:
        env = asyncio.run(fetch_mod.fetch_one(url, cache=cache, deadline=10.0))
        assert env["mode_used"] == "gitlab"
        assert "failure" not in env
        assert env["quality"]["provider"] == "gitlab"
        assert env["quality"]["page_type"] == "project"
        assert env["quality"]["project"]["star_count"] == 11
        assert "## README" in env["markdown"]

        again = asyncio.run(fetch_mod.fetch_one(url, cache=cache, deadline=10.0))
        assert again["from_cache"] is True
        assert again["quality"]["project"]["star_count"] == 11
    finally:
        cache.close()
        gitlab_mod._reset_for_tests()


def test_gitlab_probe_confirms_self_hosted_and_memoizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown forge-hinted host on a bare-project URL is probed; valid GitLab
    JSON confirms it → structured envelope + a persisted True verdict (by host)."""
    from vasco.adapters import gitlab as gitlab_mod

    gitlab_mod._reset_for_tests()
    project = (_GITLAB_FX / "project.json").read_text(encoding="utf-8")
    monkeypatch.setattr(
        gitlab_mod,
        "_api_get",
        _stub_gitlab_api(
            {"/api/v4/projects/": (project, 200), "/-/raw/": ("# readme", 200)},
            default=("not found", 404),
        ),
    )
    _disable_browser(monkeypatch)

    url = "https://gitlab.wikimedia.org/egardner/mcp-phabricator"
    cache = Cache(str(tmp_path / "cache.db"))
    try:
        env = asyncio.run(fetch_mod.fetch_one(url, cache=cache, deadline=10.0))
        assert env["mode_used"] == "gitlab"
        assert env["quality"]["provider"] == "gitlab"
        assert cache.get_probe("gitlab", "gitlab.wikimedia.org") is True
    finally:
        cache.close()
        gitlab_mod._reset_for_tests()


def test_gitlab_probe_miss_falls_through_to_normal_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forge-hinted unknown host whose /api/v4 path returns HTML (not GitLab) →
    the bare /a/b URL falls through to a normal HTML→markdown fetch, never a
    failure, and the host is negative-memoized so it's not re-probed."""
    from vasco.adapters import gitlab as gitlab_mod

    gitlab_mod._reset_for_tests()
    # The probe (gitlab._api_get) sees HTML → not GitLab; the fall-through normal
    # fetch goes through the regular http tier (core._http_fetch).
    monkeypatch.setattr(
        gitlab_mod, "_api_get", _stub_gitlab_api({}, default=("<html>nope</html>", 200))
    )
    article = (FIXTURES / "article_clean.html").read_text(encoding="utf-8")
    monkeypatch.setattr(core_mod, "_http_fetch", _stub_http(article, 200))
    _disable_browser(monkeypatch)

    url = "https://git.not-really.example/some/page"
    cache = Cache(str(tmp_path / "cache.db"))
    try:
        env = asyncio.run(fetch_mod.fetch_one(url, cache=cache, deadline=10.0))
        assert env["mode_used"] == "http"  # fell through to the normal path
        assert "failure" not in env
        assert env["quality"].get("provider") != "gitlab"
        assert gitlab_mod._probe_memo.get("git.not-really.example") is False
    finally:
        cache.close()
        gitlab_mod._reset_for_tests()


def test_gitlab_bare_project_on_plain_host_is_not_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-forge host's bare /a/b URL is NOT a GitLab probe candidate — it goes
    straight to a normal fetch with no /api/v4 round-trip (no spraying the web)."""
    from vasco.adapters import gitlab as gitlab_mod

    gitlab_mod._reset_for_tests()
    fetched: list[str] = []

    async def _spy(
        url: str, *, deadline_monotonic: float, cfg=None, profile: str = "browser"
    ):
        fetched.append(url)
        return "<html><body><p>plain page</p></body></html>", 200, {"_url_final": url}

    monkeypatch.setattr(core_mod, "_http_fetch", _spy)
    _disable_browser(monkeypatch)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        env = asyncio.run(
            fetch_mod.fetch_one(
                "https://news.example/world/story", cache=cache, deadline=10.0
            )
        )
        assert env["mode_used"] == "http"
        # No /api/v4 probe was attempted — only the page itself was fetched.
        assert not any("/api/v4/" in u for u in fetched)
        assert gitlab_mod._probe_memo.get("news.example") is None
    finally:
        cache.close()
        gitlab_mod._reset_for_tests()


# A real Markdown document body (headings, list, link) — structureless from
# trafilatura's DOM-walking point of view, so it would convert to zero words.
_RAW_MARKDOWN = (
    "# mcp-phabricator\n\n"
    "A Model Context Protocol server for Phabricator.\n\n"
    "## Install\n\n"
    "- `pip install mcp-phabricator`\n"
    "- Configure your token\n\n"
    "See the [docs](https://example.org/docs) for details.\n"
)


def test_textplain_body_passes_through_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A text/plain (raw Markdown) 200 is returned verbatim on the http tier:
    no trafilatura extraction, no EMPTY_BODY, no pointless browser escalation."""
    monkeypatch.setattr(
        core_mod,
        "_http_fetch",
        _stub_http(_RAW_MARKDOWN, 200, {"Content-Type": "text/plain; charset=utf-8"}),
    )

    browser_calls: list[str] = []

    class _SpyPool:
        async def fetch(self, url: str, *a: Any, **kw: Any):
            browser_calls.append(url)
            return "", 0, {}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(browser_mod, "_pool", None, raising=False)
    monkeypatch.setattr(browser_mod, "get_browser", lambda cfg=None: _SpyPool())

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        env = asyncio.run(
            fetch_mod.fetch_one(
                "https://gitlab.example/egardner/mcp-phabricator/-/raw/main/README.md",
                cache=cache,
                deadline=10.0,
            )
        )
        assert "failure" not in env
        assert env["mode_used"] == "http"
        assert env["markdown"] == _RAW_MARKDOWN
        assert env["word_count"] > 0
        assert "plaintext_passthrough" in env["warnings"]
        # The whole point: the browser tier never ran for a plain-text file.
        assert browser_calls == []
    finally:
        cache.close()


def test_html_mislabeled_as_textplain_still_extracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real HTML document served with a text/plain header still goes through
    trafilatura (the passthrough only fires for bodies that don't sniff as HTML)."""
    html = (FIXTURES / "article_clean.html").read_text(encoding="utf-8")
    monkeypatch.setattr(
        core_mod,
        "_http_fetch",
        _stub_http(html, 200, {"Content-Type": "text/plain"}),
    )
    _disable_browser(monkeypatch)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        env = asyncio.run(
            fetch_mod.fetch_one(
                "https://example.com/weird-server", cache=cache, deadline=10.0
            )
        )
        assert "failure" not in env
        assert env["mode_used"] == "http"
        # Extracted, not dumped raw — so the passthrough warning is absent.
        assert "plaintext_passthrough" not in env["warnings"]
        assert "<html" not in env["markdown"].lower()
    finally:
        cache.close()


def test_binary_blob_fails_fast_without_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binary blob (image/png) fails fast as UNSUPPORTED_CONTENT_TYPE on the
    http tier — no mojibake extraction, no browser escalation (which would try
    to download the blob and time out)."""
    monkeypatch.setattr(
        core_mod,
        "_http_fetch",
        _stub_http(
            "\x89PNG\r\n\x1a\n\x00\x00binary",
            200,
            {"Content-Type": "image/png", "Content-Length": "12345"},
        ),
    )

    browser_calls: list[str] = []

    class _SpyPool:
        async def fetch(self, url: str, *a: Any, **kw: Any):
            browser_calls.append(url)
            return "", 0, {}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(browser_mod, "_pool", None, raising=False)
    monkeypatch.setattr(browser_mod, "get_browser", lambda cfg=None: _SpyPool())

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        env = asyncio.run(
            fetch_mod.fetch_one(
                "https://cdn.example/logo.png", cache=cache, deadline=10.0
            )
        )
        assert env["failure"]["reason"] == "unsupported_content_type"
        assert env["mode_used"] == "http"
        assert env["attempts"] == 1
        assert browser_calls == []
        # Message surfaces the type + size as metadata (no prescribed remedy).
        msg = env["failure"]["message"]
        assert "image/png" in msg
        assert "12345 bytes" in msg
        # The structured content_type field is also set for programmatic use.
        assert env["content_type"] == "image/png"
        # Permanent (the URL's type won't change) → long negative-cache TTL.
        assert fetch_mod._ttl_for(env, None) == int(900 * 96.0)
    finally:
        cache.close()
