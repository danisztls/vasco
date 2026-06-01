from __future__ import annotations

from pathlib import Path

import pytest

from vasco import cache as cache_module
from vasco.cache import Cache


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    c = Cache(str(tmp_path / "cache.db"))
    yield c
    c.close()


@pytest.fixture
def fake_time(monkeypatch: pytest.MonkeyPatch):
    state = {"now": 1_700_000_000}

    def _now() -> float:
        return state["now"]

    monkeypatch.setattr(cache_module.time, "time", _now)
    return state


def _success_envelope(url: str = "https://example.com/foo") -> dict:
    return {
        "url_requested": url,
        "url_final": url,
        "url_canonical": url,
        "http_status": 200,
        "mode_used": "http",
        "fetched_at": None,
        "content_type": "text/html",
        "title": "Example",
        "byline": "Jane Doe",
        "published": "2025-11-01",
        "language": "en",
        "site_name": "Example",
        "word_count": 100,
        "token_count_estimate": 130,
        "quality": {"trafilatura_confidence": 0.9, "boilerplate_ratio": 0.1},
        "links": [{"url": "https://example.com/a", "anchor": "a", "rel": None}],
        "markdown": "# hi",
        "warnings": [],
    }


def _failure_envelope(url: str = "https://example.com/bad") -> dict:
    env = _success_envelope(url)
    env["http_status"] = 403
    env["markdown"] = ""
    env["failure"] = {
        "reason": "blocked_cloudflare",
        "retry_after_seconds": None,
        "message": "blocked",
    }
    return env


def test_put_then_get_returns_envelope(cache: Cache, fake_time) -> None:
    env = _success_envelope()
    cache.put(env, ttl_seconds=86400)
    got = cache.get("https://example.com/foo")
    assert got is not None
    assert got["from_cache"] is True
    assert got["cache_age_seconds"] == 0
    assert got["title"] == "Example"
    assert got["quality"]["trafilatura_confidence"] == 0.9
    assert got["links"][0]["url"] == "https://example.com/a"
    assert got["markdown"] == "# hi"


def test_get_normalizes_url(cache: Cache, fake_time) -> None:
    env = _success_envelope("https://Example.COM/foo?b=2&a=1")
    cache.put(env, ttl_seconds=86400)
    got = cache.get("https://example.com/foo?a=1&b=2")
    assert got is not None


def test_get_expired_returns_none(cache: Cache, fake_time) -> None:
    cache.put(_success_envelope(), ttl_seconds=1)
    fake_time["now"] += 2
    assert cache.get("https://example.com/foo") is None


def test_failure_envelope_roundtrip(cache: Cache, fake_time) -> None:
    cache.put(_failure_envelope(), ttl_seconds=900)
    got = cache.get("https://example.com/bad")
    assert got is not None
    assert "failure" in got
    assert got["failure"]["reason"] == "blocked_cloudflare"
    assert got["failure"]["message"] == "blocked"


def test_get_missing_returns_none(cache: Cache) -> None:
    assert cache.get("https://nope.example.com/x") is None


def test_strategy_three_failures_flips_mode(cache: Cache) -> None:
    route = "example.com/a"
    assert cache.get_strategy(route) is None

    cache.bump(route, mode="http", success=False)
    cache.bump(route, mode="http", success=False)
    assert cache.get_strategy(route) == "http"

    cache.bump(route, mode="http", success=False)
    assert cache.get_strategy(route) == "browser"


def test_strategy_success_resets_consecutive_failures(cache: Cache) -> None:
    route = "example.com/a"
    cache.bump(route, mode="http", success=False)
    cache.bump(route, mode="http", success=False)
    cache.bump(route, mode="http", success=True)
    cache.bump(route, mode="http", success=False)
    cache.bump(route, mode="http", success=False)
    assert cache.get_strategy(route) == "http"


def test_strategy_success_on_browser_keeps_browser(cache: Cache) -> None:
    route = "example.com/a"
    cache.bump(route, mode="http", success=False)
    cache.bump(route, mode="http", success=False)
    cache.bump(route, mode="http", success=False)
    assert cache.get_strategy(route) == "browser"
    cache.bump(route, mode="browser", success=True)
    assert cache.get_strategy(route) == "browser"


def test_strategy_keys_are_independent(cache: Cache) -> None:
    """Two route keys under the same domain learn separate starting tiers."""
    a, b = "d.com/a", "d.com/b"
    for _ in range(3):
        cache.bump(a, mode="http", success=False)
    cache.bump(b, mode="http", success=True)
    assert cache.get_strategy(a) == "browser"  # flipped after 3 failures
    assert cache.get_strategy(b) == "http"  # untouched by a's failures


def test_purge_expired(cache: Cache, fake_time) -> None:
    cache.put(_success_envelope("https://example.com/a"), ttl_seconds=10)
    cache.put(_success_envelope("https://example.com/b"), ttl_seconds=10_000)
    fake_time["now"] += 100
    deleted = cache.purge()
    assert deleted == 1


def test_purge_domain(cache: Cache, fake_time) -> None:
    cache.put(
        _success_envelope("https://www.vivareal.com.br/aluguel/x"), ttl_seconds=10_000
    )
    cache.put(
        _success_envelope("https://vivareal.com.br/imovel/y-id-1/"), ttl_seconds=10_000
    )
    cache.put(_success_envelope("https://example.com/keep"), ttl_seconds=10_000)

    # Matches the registered domain across www + bare host, leaves others.
    deleted = cache.purge_domain("vivareal.com.br")
    assert deleted == 2
    assert cache.get("https://example.com/keep") is not None
    assert cache.get("https://www.vivareal.com.br/aluguel/x") is None
    # Accepts a full URL or subdomain form too.
    assert cache.purge_domain("https://www.example.com/whatever") == 1
    assert cache.get("https://example.com/keep") is None


def test_purge_domain_no_match_returns_zero(cache: Cache, fake_time) -> None:
    cache.put(_success_envelope("https://example.com/a"), ttl_seconds=10_000)
    assert cache.purge_domain("nope.com") == 0
    assert cache.get("https://example.com/a") is not None


def test_stats_reports_entries(cache: Cache, fake_time) -> None:
    assert cache.stats()["entries"] == 0
    cache.put(_success_envelope("https://example.com/a"), ttl_seconds=10)
    cache.put(_success_envelope("https://example.com/b"), ttl_seconds=10)
    assert cache.stats()["entries"] == 2


def test_failure_ttl_scales_per_reason() -> None:
    """NOT_FOUND should outlive transient failures; TIMEOUT should be short."""
    from vasco.fetch import _ttl_for

    def env(reason: str) -> dict:
        return {"failure": {"reason": reason}}

    base = 900  # default cfg.fetch.failure_ttl_seconds
    assert _ttl_for(env("not_found"), None) == base * 96
    assert _ttl_for(env("blocked_bot"), None) == base * 4
    assert _ttl_for(env("timeout"), None) == int(base * 0.33)
    assert _ttl_for(env("server_error"), None) == int(base * 0.33)
    # Scraper-rot is fixed by a code change — short TTL so a fixed adapter heals
    # fast instead of being pinned to the stale failure for ~24h.
    assert _ttl_for(env("parse_failed"), None) == int(base * 0.33)
    # Unknown reasons fall back to the base TTL.
    assert _ttl_for(env("totally_made_up"), None) == base
