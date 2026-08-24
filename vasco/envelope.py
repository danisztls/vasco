# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The fetch envelope: the single contract every fetch path returns.

`fetch_one`, `extract`, `cache.get`, and every source adapter
(youtube / wikimedia / realestate / google_shopping) build their result through
the helpers here, so the envelope's shape lives in exactly one place. Adding a
field means editing this module only — plus the cache column mapping in
`vasco/cache.py`, which `tests/test_cache_roundtrip.py` enforces stays in sync.

`FetchEnvelope` documents the contract for type checkers and readers; it is
typing-only (no runtime cost). The builders below are the runtime source of
truth.
"""

from __future__ import annotations

import time
from typing import Any, TypedDict

from vasco.errors import FailureReason


def now_epoch() -> int:
    return int(time.time())


class FetchEnvelope(TypedDict, total=False):
    # Identity / provenance — always present (set by ``base_envelope``).
    url_requested: str
    url_final: str
    url_canonical: str
    http_status: int
    mode_used: str
    fetched_at: int
    from_cache: bool
    cache_age_seconds: int
    content_type: str
    # Success content (set by ``success_envelope``).
    title: str | None
    byline: str | None
    published: str | None
    modified: str | None
    language: str | None
    site_name: str | None
    image: str | None
    word_count: int
    token_count_estimate: int
    quality: dict[str, Any]
    markdown: str
    warnings: list[str]
    # Failure (set by ``failure_envelope``, mutually exclusive with success).
    failure: dict[str, Any]


def base_envelope(
    *,
    url_requested: str,
    url_normalized: str | None,
    url_final: str | None,
    http_status: int,
    mode_used: str,
    content_type: str,
    fetched_at: int | None = None,
) -> dict[str, Any]:
    """The provenance fields shared by every envelope, success or failure."""
    return {
        "url_requested": url_requested,
        "url_final": url_final or url_requested,
        "url_canonical": url_normalized or url_requested,
        "http_status": http_status,
        "mode_used": mode_used,
        "fetched_at": fetched_at if fetched_at is not None else now_epoch(),
        "from_cache": False,
        "cache_age_seconds": 0,
        "content_type": content_type,
    }


def success_envelope(
    *,
    base: dict[str, Any],
    markdown: str,
    metadata: dict[str, Any],
    token_count_estimate: int,
) -> dict[str, Any]:
    """A success envelope. Every content key is always present (defaulting to
    ``None``/empty), so callers cannot silently omit one."""
    env = dict(base)
    env.update(
        {
            "title": metadata.get("title"),
            "byline": metadata.get("byline"),
            "published": metadata.get("published"),
            "modified": metadata.get("modified"),
            "language": metadata.get("language"),
            "site_name": metadata.get("site_name"),
            "image": metadata.get("image"),
            "word_count": metadata.get("word_count", 0),
            "token_count_estimate": token_count_estimate,
            "quality": metadata.get("quality", {}),
            "markdown": markdown,
            "warnings": list(metadata.get("warnings", [])),
        }
    )
    return env


def failure_envelope(
    *,
    base: dict[str, Any],
    reason: FailureReason,
    message: str,
    retry_after: int | None = None,
    partial_html: str | None = None,
    partial_markdown: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """A failure envelope. ``reason`` is a `FailureReason`; partial content (if
    any was recovered) is surfaced as ``markdown``."""
    env = dict(base)
    env["failure"] = {
        "reason": str(reason),
        "retry_after_seconds": retry_after,
        "message": message,
    }
    env["markdown"] = partial_markdown or partial_html or ""
    env["warnings"] = list(warnings or [])
    return env
