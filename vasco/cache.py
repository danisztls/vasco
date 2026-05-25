from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

_TRACKING_PREFIXES = ("utm_",)
_TRACKING_EXACT = {"fbclid", "gclid", "mc_eid"}

# AMP query params stripped to fold AMP variants into the canonical row.
# `?amp=1` is the vivareal / generic-CMS form; `?output=amp` is Twitter/X
# and several news sites. We only drop `output` when its value is the
# AMP sentinel — `output=json` etc. is meaningful elsewhere.
_AMP_AMP_VALUES = frozenset({"", "1", "true", "amp"})
_AMP_OUTPUT_VALUES = frozenset({"amp"})

_KNOWN_SECOND_LEVELS = {"co", "ac", "gov", "or", "ne"}

# Matches Wikimedia project article URLs across all languages and mobile
# subdomains.  Captures language, project domain, and title.
# Covers: en.wikipedia.org, en.m.wiktionary.org, fr.wikisource.org, etc.
_WIKIMEDIA_RE = re.compile(
    r"^https?://(?P<lang>[a-z]{2,3})(?:\.m)?\."
    r"(?P<project>wikipedia|wiktionary|wikibooks|wikiquote|wikisource|wikivoyage|wikiversity|wikinews)"
    r"\.org/wiki/(?P<title>.+)",
    re.IGNORECASE,
)

# Matches any YouTube URL that points to a specific video, capturing the
# video ID. One of three named groups will be set per match:
#   - id_short: youtu.be/<id>
#   - id_query: <yt-host>/watch?[...&]v=<id>
#   - id_path:  <yt-host>/(embed|shorts|v|live)/<id>
# Used both for cache normalization (collapse every variant to /watch?v=<id>)
# and for extract_video_id in vasco.youtube.
YT_VIDEO_ID_RE = re.compile(
    r"^https?://"
    r"(?:"
    r"(?:www\.)?youtu\.be/(?P<id_short>[A-Za-z0-9_-]+)"
    r"|"
    r"(?:[a-z0-9-]+\.)*(?:youtube\.com|youtube-nocookie\.com)(?:\.[a-z]{2,})?"
    r"/(?:"
    r"watch\?(?:[^#]*?&)?v=(?P<id_query>[A-Za-z0-9_-]+)"
    r"|"
    r"(?:embed|shorts|v|live)/(?P<id_path>[A-Za-z0-9_-]+)"
    r")"
    r")",
    re.IGNORECASE,
)

# Matches any YouTube host that isn't a video URL (channel pages, playlists,
# homepage). Used as a fallback after YT_VIDEO_ID_RE so non-video YouTube URLs
# still get host canonicalization.
_YT_HOST_RE = re.compile(
    r"^(?:[a-z0-9-]+\.)*(?:youtube\.com|youtube-nocookie\.com)(?:\.[a-z]{2,})?$",
    re.IGNORECASE,
)


def _is_tracking_param(key: str) -> bool:
    if key in _TRACKING_EXACT:
        return True
    return any(key.startswith(p) for p in _TRACKING_PREFIXES)


def _is_amp_param(key: str, value: str) -> bool:
    """Is this query param an AMP-mode marker we should drop?"""
    if key == "amp":
        return value.lower() in _AMP_AMP_VALUES
    if key == "output":
        return value.lower() in _AMP_OUTPUT_VALUES
    return False


def _strip_amp_path(path: str) -> str:
    """Collapse common AMP path patterns (`/amp` suffix, `/amp/` segment).

    Examples:
      /article/amp        → /article
      /imovel/amp/foo     → /imovel/foo
      /amp/foo            → /foo
      /amphibian/x        → /amphibian/x   (full segment match only)
    """
    if "/amp" not in path:
        return path
    segments = path.split("/")
    cleaned = [s for s in segments if s != "amp"]
    if cleaned == segments:
        return path
    new_path = "/".join(cleaned)
    if path.startswith("/") and not new_path.startswith("/"):
        new_path = "/" + new_path
    return new_path or "/"


def _canonicalize_youtube_host(raw: str) -> str:
    """Collapse every YouTube URL form for one video to a single canonical
    ``https://youtube.com/watch?v=<id>`` so they share a cache row.

    Covers: youtu.be short links, www./m./music. subdomains, country-local
    TLDs (youtube.com.br), the privacy embed domain youtube-nocookie.com, and
    the alternate video paths (/embed/, /shorts/, /v/, /live/). Non-video
    YouTube URLs (playlists, channels, homepage) get only host canonicalization.
    """
    m = YT_VIDEO_ID_RE.match(raw)
    if m:
        video_id = m.group("id_short") or m.group("id_query") or m.group("id_path")
        parts = urlsplit(raw)
        other = "&".join(
            p for p in parts.query.split("&") if p and not p.startswith("v=")
        )
        new_query = f"v={video_id}" + (f"&{other}" if other else "")
        return urlunsplit(("https", "youtube.com", "/watch", new_query, ""))

    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if host and _YT_HOST_RE.match(host):
        return urlunsplit(("https", "youtube.com", parts.path, parts.query, ""))

    return raw


def _canonicalize_wikimedia(raw: str) -> str:
    """Collapse Wikimedia project URL variants to a canonical form.

    Strips mobile subdomains and normalizes the title (spaces → underscores,
    first char uppercase) so different encodings share a cache row.
    Works for all Wikimedia projects: wikipedia, wiktionary, wikibooks, etc.
    """
    m = _WIKIMEDIA_RE.match(raw)
    if not m:
        return raw
    lang = m.group("lang").lower()
    project = m.group("project").lower()
    title_raw = m.group("title").split("#")[0].split("?")[0]
    title = unquote(title_raw).replace(" ", "_")
    if title:
        title = title[0].upper() + title[1:]
    return (
        f"https://{lang}.{project}.org/wiki/{quote(title, safe="/:@!$&'()*+,;=-._~")}"
    )


def normalize_url(url: str) -> str:
    """Normalize a URL.

    Rules:
      - Lowercase scheme and host
      - Drop fragment
      - Drop default ports (80/http, 443/https)
      - Drop trailing slash from non-root paths
      - Sort query params alphabetically (preserving order of repeated keys)
      - Drop tracking params: utm_*, fbclid, gclid, mc_eid
      - Drop AMP markers: ``?amp=`` (any AMP-ish value) and ``?output=amp``;
        strip ``/amp/`` segments and ``/amp`` suffix from the path
      - Leave percent-encoded characters alone
      - Collapse YouTube variants (``youtu.be``, ``m./music./www.youtube.com``,
        local TLDs like ``youtube.com.br``) to bare ``youtube.com``
    """
    if not url:
        return url
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw

    raw = _canonicalize_youtube_host(raw)
    raw = _canonicalize_wikimedia(raw)
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    host = host.lower()

    port = parts.port
    if port is not None:
        if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
            port = None

    userinfo = ""
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo += ":" + parts.password
        userinfo += "@"

    netloc = userinfo + host
    if port is not None:
        netloc += f":{port}"

    path = _strip_amp_path(parts.path)
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"

    query = ""
    if parts.query:
        tokens = [tok for tok in parts.query.split("&") if tok != ""]
        triples: list[tuple[str, str, bool]] = []
        for tok in tokens:
            if "=" in tok:
                k, _, v = tok.partition("=")
                has_eq = True
            else:
                k, v = tok, ""
                has_eq = False
            k_dec = unquote(k)
            if _is_tracking_param(k_dec):
                continue
            if _is_amp_param(k_dec, unquote(v)):
                continue
            triples.append((k_dec, v, has_eq))
        triples.sort(key=lambda t: t[0])
        encoded_parts = []
        for k_dec, v_raw, has_eq in triples:
            k_enc = quote(k_dec, safe="")
            if has_eq:
                encoded_parts.append(f"{k_enc}={v_raw}")
            else:
                encoded_parts.append(k_enc)
        query = "&".join(encoded_parts)

    return urlunsplit((scheme, netloc, path, query, ""))


def registered_domain(url: str) -> str:
    """Best-effort registered domain extraction.

    Strips a leading "www." then returns the last two labels, unless the
    second-to-last label is a known secondary suffix (co, ac, gov, org, net,
    edu, or, ne, com) in which case the last three labels are returned.
    This is a heuristic, not a real PSL lookup.
    """
    if not url:
        return ""
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    host = urlsplit(raw).hostname or ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    if labels[-2] in _KNOWN_SECOND_LEVELS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


_SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_cache (
  url            TEXT PRIMARY KEY,
  final_url      TEXT,
  canonical_url  TEXT,
  title          TEXT,
  byline         TEXT,
  published      TEXT,
  language       TEXT,
  site_name      TEXT,
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

CREATE TABLE IF NOT EXISTS domain_strategy (
  domain          TEXT PRIMARY KEY,
  preferred_mode  TEXT,
  success_count   INTEGER DEFAULT 0,
  failure_count   INTEGER DEFAULT 0,
  last_updated    INTEGER
);
"""


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
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

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
            "language": row["language"],
            "site_name": row["site_name"],
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
                url, final_url, canonical_url, title, byline, published, language,
                site_name, word_count, token_count, quality_json, links_json,
                markdown, warnings_json, status, failure_reason, failure_json,
                mode_used, fetched_at, ttl_expires, content_type, html_gz
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized,
                envelope.get("url_final"),
                envelope.get("url_canonical"),
                envelope.get("title"),
                envelope.get("byline"),
                envelope.get("published"),
                envelope.get("language"),
                envelope.get("site_name"),
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

    def get_domain_strategy(self, domain: str) -> str | None:
        cur = self._conn.execute(
            "SELECT preferred_mode FROM domain_strategy WHERE domain = ?", (domain,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return row["preferred_mode"]

    def bump(self, domain: str, *, mode: str, success: bool) -> None:
        """Update domain strategy.

        `failure_count` tracks **consecutive failures of the preferred mode**.
        Any success resets it to 0. A failure on a non-preferred mode is
        recorded only via `success_count`/`last_updated` — it does not break
        a preferred-mode streak. Three consecutive preferred-mode failures
        flip preferred_mode (http<->browser) and reset failure_count.
        """
        now = int(time.time())
        cur = self._conn.execute(
            "SELECT preferred_mode, success_count, failure_count FROM domain_strategy WHERE domain = ?",
            (domain,),
        )
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                """
                INSERT INTO domain_strategy (domain, preferred_mode, success_count, failure_count, last_updated)
                VALUES (?, ?, ?, ?, ?)
                """,
                (domain, mode, 1 if success else 0, 0 if success else 1, now),
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
            UPDATE domain_strategy
            SET preferred_mode = ?, success_count = ?, failure_count = ?, last_updated = ?
            WHERE domain = ?
            """,
            (preferred, success_count, failure_count, now, domain),
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
