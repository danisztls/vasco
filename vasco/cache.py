from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

from .urls import normalize_url, registered_domain

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_cache (
  url            TEXT PRIMARY KEY,
  final_url      TEXT,
  canonical_url  TEXT,
  title          TEXT,
  byline         TEXT,
  published      TEXT,
  modified       TEXT,
  language       TEXT,
  site_name      TEXT,
  image          TEXT,
  word_count     INTEGER,
  token_count    INTEGER,
  quality_json   TEXT,
  links_json     TEXT,
  markdown       TEXT,
  warnings_json  TEXT,
  status         INTEGER,
  failure_reason TEXT,
  failure_json   TEXT,
  mode_used      TEXT,
  fetched_at     INTEGER,
  ttl_expires    INTEGER,
  content_type   TEXT,
  html_gz        BLOB
);

DROP TABLE IF EXISTS domain_strategy;
CREATE TABLE IF NOT EXISTS fetch_strategy (
  route_key       TEXT PRIMARY KEY,
  preferred_mode  TEXT,
  success_count   INTEGER DEFAULT 0,
  failure_count   INTEGER DEFAULT 0,
  last_updated    INTEGER
);

-- Per-domain adapter-applicability memo (e.g. "is this domain a Shopify store").
-- Keyed by (provider, registered_domain); `is_match` 1/0. Lets the auto-probe
-- adapters skip re-probing a domain across processes (CLI and vascod share this
-- file). `updated_at` drives a staleness TTL so a re-platformed site self-heals.
CREATE TABLE IF NOT EXISTS adapter_probe (
  provider    TEXT,
  domain      TEXT,
  is_match    INTEGER,
  updated_at  INTEGER,
  PRIMARY KEY (provider, domain)
);

-- Learned per-route HTTP header profile ("browser" default / "honest" minimal).
-- A second strategy dimension alongside fetch_strategy's starting tier, kept in
-- its own table so it never tangles with bump()'s preferred_mode logic. Written
-- only when the adaptive honest-header retry clears a WAF block on the http tier.
CREATE TABLE IF NOT EXISTS route_header_profile (
  route_key    TEXT PRIMARY KEY,
  profile      TEXT,
  updated_at   INTEGER
);
"""

# A probe verdict older than this is treated as unknown (re-probed), so a domain
# that migrates onto or off of a platform heals within the window without a
# manual cache purge.
_PROBE_TTL_SECONDS = 30 * 86400

# Columns added to fetch_cache after the initial release. `CREATE TABLE IF NOT
# EXISTS` never alters an existing table, so these are ALTERed onto older
# on-disk DBs at open time. Only ever add new, nullable columns here (with the
# matching column in _SCHEMA above) — the round-trip guard test in
# tests/test_cache_roundtrip.py fails CI if an envelope field has no column.
_FETCH_CACHE_ADDED_COLUMNS: dict[str, str] = {
    "modified": "TEXT",
    "image": "TEXT",
}


def _default_cache_path() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(xdg) / "vasco" / "cache.db"


class Cache:
    def __init__(self, path: str | None = None) -> None:
        if path is None:
            db_path = _default_cache_path()
        else:
            db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._conn = sqlite3.connect(str(db_path), timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self._conn.executescript(_SCHEMA)
        self._ensure_columns()
        self._conn.commit()

    def _ensure_columns(self) -> None:
        """Back-fill columns added after a DB was first created."""
        existing = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(fetch_cache)")
        }
        for name, decl in _FETCH_CACHE_ADDED_COLUMNS.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE fetch_cache ADD COLUMN {name} {decl}")

    def get(self, url: str) -> dict | None:
        normalized = normalize_url(url)
        cur = self._conn.execute(
            "SELECT * FROM fetch_cache WHERE url = ?", (normalized,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        now = int(time.time())
        if row["ttl_expires"] is not None and row["ttl_expires"] < now:
            return None

        envelope: dict = {
            "url_requested": row["url"],
            "url_final": row["final_url"],
            "url_canonical": row["canonical_url"],
            "http_status": row["status"],
            "mode_used": row["mode_used"],
            "fetched_at": row["fetched_at"],
            "from_cache": True,
            "cache_age_seconds": max(0, now - (row["fetched_at"] or now)),
            "content_type": row["content_type"],
            "title": row["title"],
            "byline": row["byline"],
            "published": row["published"],
            "modified": row["modified"],
            "language": row["language"],
            "site_name": row["site_name"],
            "image": row["image"],
            "word_count": row["word_count"],
            "token_count_estimate": row["token_count"],
            "quality": json.loads(row["quality_json"]) if row["quality_json"] else {},
            "links": json.loads(row["links_json"]) if row["links_json"] else [],
            "markdown": row["markdown"] or "",
            "warnings": json.loads(row["warnings_json"])
            if row["warnings_json"]
            else [],
        }
        if row["failure_json"]:
            envelope["failure"] = json.loads(row["failure_json"])
        elif row["failure_reason"] and row["failure_reason"] != "ok":
            envelope["failure"] = {
                "reason": row["failure_reason"],
                "retry_after_seconds": None,
                "message": "",
            }
        return envelope

    def put(self, envelope: dict, *, ttl_seconds: int) -> None:
        url_requested = envelope.get("url_requested") or envelope.get("url_final") or ""
        normalized = normalize_url(url_requested)
        now = int(time.time())
        fetched_at = int(envelope.get("fetched_at") or now)
        ttl_expires = fetched_at + int(ttl_seconds)

        failure = envelope.get("failure")
        failure_reason = None
        failure_json = None
        if failure:
            failure_reason = failure.get("reason")
            failure_json = json.dumps(failure)

        self._conn.execute(
            """
            INSERT OR REPLACE INTO fetch_cache (
                url, final_url, canonical_url, title, byline, published, modified,
                language, site_name, image, word_count, token_count, quality_json,
                links_json, markdown, warnings_json, status, failure_reason,
                failure_json, mode_used, fetched_at, ttl_expires, content_type, html_gz
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized,
                envelope.get("url_final"),
                envelope.get("url_canonical"),
                envelope.get("title"),
                envelope.get("byline"),
                envelope.get("published"),
                envelope.get("modified"),
                envelope.get("language"),
                envelope.get("site_name"),
                envelope.get("image"),
                envelope.get("word_count"),
                envelope.get("token_count_estimate"),
                json.dumps(envelope.get("quality", {}))
                if envelope.get("quality") is not None
                else None,
                json.dumps(envelope.get("links", []))
                if envelope.get("links") is not None
                else None,
                envelope.get("markdown"),
                json.dumps(envelope.get("warnings", []))
                if envelope.get("warnings") is not None
                else None,
                envelope.get("http_status"),
                failure_reason,
                failure_json,
                envelope.get("mode_used"),
                fetched_at,
                ttl_expires,
                envelope.get("content_type"),
                envelope.get("html_gz"),
            ),
        )
        self._conn.commit()

    def get_strategy(self, route_key: str) -> str | None:
        cur = self._conn.execute(
            "SELECT preferred_mode FROM fetch_strategy WHERE route_key = ?",
            (route_key,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return row["preferred_mode"]

    def bump(self, route_key: str, *, mode: str, success: bool) -> None:
        """Update the per-route fetch strategy.

        `failure_count` tracks **consecutive failures of the preferred mode**.
        Any success resets it to 0. A failure on a non-preferred mode is
        recorded only via `success_count`/`last_updated` — it does not break
        a preferred-mode streak. Three consecutive preferred-mode failures
        flip preferred_mode (http<->browser) and reset failure_count.
        """
        now = int(time.time())
        cur = self._conn.execute(
            "SELECT preferred_mode, success_count, failure_count FROM fetch_strategy WHERE route_key = ?",
            (route_key,),
        )
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                """
                INSERT INTO fetch_strategy (route_key, preferred_mode, success_count, failure_count, last_updated)
                VALUES (?, ?, ?, ?, ?)
                """,
                (route_key, mode, 1 if success else 0, 0 if success else 1, now),
            )
            self._conn.commit()
            return

        preferred = row["preferred_mode"]
        success_count = row["success_count"] or 0
        failure_count = row["failure_count"] or 0

        if success:
            success_count += 1
            failure_count = 0
        elif mode == preferred:
            failure_count += 1
            if failure_count >= 3:
                preferred = "browser" if preferred == "http" else "http"
                failure_count = 0

        self._conn.execute(
            """
            UPDATE fetch_strategy
            SET preferred_mode = ?, success_count = ?, failure_count = ?, last_updated = ?
            WHERE route_key = ?
            """,
            (preferred, success_count, failure_count, now, route_key),
        )
        self._conn.commit()

    def get_header_profile(self, route_key: str) -> str | None:
        """Learned HTTP header profile for a route, or ``None`` if unlearned."""
        cur = self._conn.execute(
            "SELECT profile FROM route_header_profile WHERE route_key = ?",
            (route_key,),
        )
        row = cur.fetchone()
        return row["profile"] if row is not None else None

    def set_header_profile(self, route_key: str, profile: str) -> None:
        """Persist the learned header profile for a route (upsert)."""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO route_header_profile (route_key, profile, updated_at)
            VALUES (?, ?, ?)
            """,
            (route_key, profile, int(time.time())),
        )
        self._conn.commit()

    def get_probe(self, provider: str, domain: str) -> bool | None:
        """Persisted adapter-applicability verdict for a domain, or ``None`` if
        unknown or stale (older than ``_PROBE_TTL_SECONDS``)."""
        cur = self._conn.execute(
            "SELECT is_match, updated_at FROM adapter_probe WHERE provider = ? AND domain = ?",
            (provider, domain),
        )
        row = cur.fetchone()
        if row is None:
            return None
        updated = row["updated_at"]
        if updated is not None and updated < int(time.time()) - _PROBE_TTL_SECONDS:
            return None
        return bool(row["is_match"])

    def set_probe(self, provider: str, domain: str, is_match: bool) -> None:
        """Persist (and refresh the timestamp of) an adapter-applicability verdict."""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO adapter_probe (provider, domain, is_match, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (provider, domain, 1 if is_match else 0, int(time.time())),
        )
        self._conn.commit()

    def purge(self, older_than_seconds: int | None = None) -> int:
        if older_than_seconds is None:
            cur = self._conn.execute(
                "DELETE FROM fetch_cache WHERE ttl_expires IS NOT NULL AND ttl_expires < ?",
                (int(time.time()),),
            )
        else:
            cutoff = int(time.time()) - int(older_than_seconds)
            cur = self._conn.execute(
                "DELETE FROM fetch_cache WHERE fetched_at IS NOT NULL AND fetched_at < ?",
                (cutoff,),
            )
        deleted = cur.rowcount or 0
        self._conn.commit()
        return deleted

    def purge_domain(self, domain: str) -> int:
        """Delete every cache entry whose registered domain matches `domain`.

        `domain` is itself reduced via `registered_domain`, so "www.x.com.br",
        "x.com.br", and a full URL on that host all match the same entries
        (including subdomains).
        """
        target = registered_domain(domain)
        if not target:
            return 0
        # registered_domain() as a SQL function lets a single DELETE do the
        # matching (including subdomains) instead of pulling every URL into
        # Python and filtering there.
        self._conn.create_function(
            "_registered_domain", 1, registered_domain, deterministic=True
        )
        cur = self._conn.execute(
            "DELETE FROM fetch_cache WHERE _registered_domain(url) = ?", (target,)
        )
        # Forget any adapter-probe verdict for the domain too, so a re-fetch
        # re-discovers it fresh rather than trusting a stale memo. Match both the
        # registered-domain key (shopify) and any host-scoped key under it
        # (gitlab keys by full host, e.g. gitlab.wikimedia.org), so a purge of the
        # eTLD+1 clears subdomain verdicts as well.
        self._conn.execute(
            "DELETE FROM adapter_probe WHERE domain = ? OR domain LIKE ?",
            (target, f"%.{target}"),
        )
        self._conn.commit()
        return cur.rowcount

    def stats(self) -> dict:
        entries = self._conn.execute(
            "SELECT COUNT(*) AS n FROM fetch_cache"
        ).fetchone()["n"]
        size_bytes = 0
        try:
            size_bytes = self._path.stat().st_size
        except OSError:
            size_bytes = 0
        return {"entries": entries, "size_bytes": size_bytes}

    def list_entries(self) -> Iterator[dict]:
        cur = self._conn.execute(
            "SELECT url, fetched_at, ttl_expires, status FROM fetch_cache ORDER BY fetched_at DESC"
        )
        for row in cur:
            yield {
                "url": row["url"],
                "fetched_at": row["fetched_at"],
                "ttl_expires": row["ttl_expires"],
                "status": row["status"],
            }

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
