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


def test_domain_strategy_three_failures_flips_mode(cache: Cache) -> None:
    domain = "example.com"
    assert cache.get_domain_strategy(domain) is None

    cache.bump(domain, mode="http", success=False)
    cache.bump(domain, mode="http", success=False)
    assert cache.get_domain_strategy(domain) == "http"

    cache.bump(domain, mode="http", success=False)
    assert cache.get_domain_strategy(domain) == "browser"


def test_domain_strategy_success_resets_consecutive_failures(cache: Cache) -> None:
    domain = "example.com"
    cache.bump(domain, mode="http", success=False)
    cache.bump(domain, mode="http", success=False)
    cache.bump(domain, mode="http", success=True)
    cache.bump(domain, mode="http", success=False)
    cache.bump(domain, mode="http", success=False)
    assert cache.get_domain_strategy(domain) == "http"


def test_domain_strategy_success_on_browser_keeps_browser(cache: Cache) -> None:
    domain = "example.com"
    cache.bump(domain, mode="http", success=False)
    cache.bump(domain, mode="http", success=False)
    cache.bump(domain, mode="http", success=False)
    assert cache.get_domain_strategy(domain) == "browser"
    cache.bump(domain, mode="browser", success=True)
    assert cache.get_domain_strategy(domain) == "browser"


def test_purge_expired(cache: Cache, fake_time) -> None:
    cache.put(_success_envelope("https://example.com/a"), ttl_seconds=10)
    cache.put(_success_envelope("https://example.com/b"), ttl_seconds=10_000)
    fake_time["now"] += 100
    deleted = cache.purge()
    assert deleted == 1


def test_stats_reports_entries(cache: Cache, fake_time) -> None:
    assert cache.stats()["entries"] == 0
    cache.put(_success_envelope("https://example.com/a"), ttl_seconds=10)
    cache.put(_success_envelope("https://example.com/b"), ttl_seconds=10)
    assert cache.stats()["entries"] == 2
