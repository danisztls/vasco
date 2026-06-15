"""Guard test: the cache must round-trip every field the canonical envelope
builders produce.

This is the enforcement the CLAUDE.md invariant ("same shape across fetch_one,
extract, cache.get") previously relied on a human to honour. It caught — and
now prevents a regression of — the bug where `image` and `modified` had no
cache column and were silently dropped on every cache hit.

Because the envelopes here are built through `vasco.envelope` (the same source
of truth core + adapters use), adding a field to the builder without a matching
column in `vasco/cache.py` makes this test fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vasco import envelope as env_mod
from vasco.cache import Cache
from vasco.errors import FailureReason

# Provenance the cache re-derives on read rather than storing verbatim.
_CACHE_MANAGED = {"from_cache", "cache_age_seconds"}


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    c = Cache(str(tmp_path / "cache.db"))
    yield c
    c.close()


def _full_success() -> dict:
    base = env_mod.base_envelope(
        url_requested="https://example.com/foo",
        url_normalized="https://example.com/foo",
        url_final="https://example.com/foo",
        http_status=200,
        mode_used="http",
        content_type="text/html",
    )
    return env_mod.success_envelope(
        base=base,
        markdown="# hi\n\nbody",
        metadata={
            "title": "Example",
            "byline": "Jane Doe",
            "published": "2025-11-01",
            "modified": "2025-11-02",
            "language": "en",
            "site_name": "Example",
            "image": "https://example.com/og.png",
            "word_count": 100,
            "quality": {"boilerplate_ratio": 0.1},
            "warnings": ["x"],
        },
        token_count_estimate=130,
    )


def test_success_envelope_keys_all_have_columns(cache: Cache) -> None:
    """Every key the success builder produces survives a cache round-trip."""
    env = _full_success()
    cache.put(env, ttl_seconds=3600)
    got = cache.get(env["url_requested"])
    assert got is not None

    missing = set(env) - set(got)
    assert not missing, f"cache.get dropped envelope keys: {sorted(missing)}"

    for key in set(env) - _CACHE_MANAGED:
        assert got[key] == env[key], f"value drift for {key!r}: {got[key]!r}"

    # The fields that were the original bug, pinned explicitly.
    assert got["image"] == "https://example.com/og.png"
    assert got["modified"] == "2025-11-02"
    assert got["from_cache"] is True


def test_failure_envelope_roundtrip(cache: Cache) -> None:
    base = env_mod.base_envelope(
        url_requested="https://example.com/bad",
        url_normalized="https://example.com/bad",
        url_final="https://example.com/bad",
        http_status=403,
        mode_used="http",
        content_type="text/html",
    )
    env = env_mod.failure_envelope(
        base=base, reason=FailureReason.BLOCKED_CLOUDFLARE, message="blocked"
    )
    cache.put(env, ttl_seconds=3600)
    got = cache.get(env["url_requested"])
    assert got is not None
    assert got["failure"]["reason"] == env["failure"]["reason"]
    assert got["failure"]["message"] == "blocked"


def test_added_columns_backfilled_on_existing_db(tmp_path: Path) -> None:
    """Opening an old DB that predates image/modified ALTERs them in, so the
    round-trip works without a destructive rebuild."""
    import sqlite3

    db = tmp_path / "old.db"
    # Minimal legacy table without image/modified columns.
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE fetch_cache (url TEXT PRIMARY KEY, title TEXT, "
        "fetched_at INTEGER, ttl_expires INTEGER)"
    )
    conn.commit()
    conn.close()

    cache = Cache(str(db))
    try:
        cols = {
            row["name"] for row in cache._conn.execute("PRAGMA table_info(fetch_cache)")
        }
        assert {"image", "modified"} <= cols
    finally:
        cache.close()
