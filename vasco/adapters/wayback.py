# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Wayback Machine snapshot discovery for the fetch recovery chain.

Used by `fetch._do_fetch_html` as a last-resort tier when both the http and
browser tiers (including the mobile variant) hit anti-bot protection. The
Availability API resolves a snapshot timestamp; the returned URL uses the
`if_` modifier so the served HTML contains only the original page, without
the Wayback toolbar wrapper.

Failures are silent: any error path returns None and the caller keeps the
prior failure envelope.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

try:  # pragma: no cover - httpx is an optional dep at import time.
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


_AVAILABILITY_API = "https://archive.org/wayback/available"
_SNAPSHOT_PREFIX = "https://web.archive.org/web/"


async def find_snapshot(
    url: str,
    *,
    deadline_monotonic: float,
    cfg: Any | None = None,
) -> str | None:
    """Resolve the closest available Wayback snapshot for `url`.

    Returns a snapshot URL with the `if_` modifier injected (so the response
    is the unwrapped original HTML), or None if no usable snapshot exists,
    the API fails, or the deadline elapses.

    The Availability API is inconsistent about trailing slashes: querying
    `.../path/` may return empty while `.../path` returns a snapshot (or
    vice versa). We try both variants in sequence to maximize hit rate.
    """
    if httpx is None:
        return None

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return None

    timeout = min(5.0, max(1.0, remaining))
    user_agent = "Mozilla/5.0 (compatible; Vasco/0.1)"
    if cfg is not None:
        with contextlib.suppress(Exception):
            user_agent = cfg.fetch.user_agent or user_agent

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        ) as client:
            for variant in _url_variants(url):
                if deadline_monotonic - time.monotonic() <= 0:
                    return None
                try:
                    resp = await client.get(_AVAILABILITY_API, params={"url": variant})
                except Exception:
                    continue
                if resp.status_code != 200:
                    continue
                try:
                    data = resp.json()
                except Exception:
                    continue
                snapshot = _extract_snapshot(data)
                if snapshot is not None:
                    return snapshot
                # 200 + empty archived_snapshots is an authoritative "no
                # snapshot" — don't waste budget on the trailing-slash retry.
                return None
    except Exception:
        return None
    return None


def _url_variants(url: str) -> list[str]:
    """Return the URL plus its toggled-trailing-slash variant.

    Order matters: the input form is tried first. We only generate the
    second variant for the path component (toggling slash on the bare host
    `https://example.com` vs `https://example.com/` is unlikely to differ
    in Wayback's index and would just double the request count).
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if not parts.path or parts.path == "/":
        return [url]
    if parts.path.endswith("/"):
        alt = parts._replace(path=parts.path.rstrip("/")).geturl()
    else:
        alt = parts._replace(path=parts.path + "/").geturl()
    return [url, alt]


def _extract_snapshot(data: dict | None) -> str | None:
    """Pull a usable snapshot URL out of the Availability API payload."""
    closest = (data or {}).get("archived_snapshots", {}).get("closest") or {}
    if not closest.get("available"):
        return None
    # Snapshot's stored HTTP status: only accept 200s. A captured 4xx/5xx
    # carries no original content worth recovering.
    status = str(closest.get("status") or "")
    if not status.startswith("2"):
        return None
    snapshot_url = closest.get("url")
    if not snapshot_url:
        return None
    return _inject_if_modifier(_normalize_scheme(snapshot_url))


def _normalize_scheme(snapshot_url: str) -> str:
    """The Availability API sometimes returns http://web.archive.org/...; force https."""
    if snapshot_url.startswith("http://web.archive.org/"):
        return "https://" + snapshot_url[len("http://") :]
    return snapshot_url


def _inject_if_modifier(snapshot_url: str) -> str:
    """Insert `if_` after the timestamp segment.

    `https://web.archive.org/web/<ts>/<orig>` becomes
    `https://web.archive.org/web/<ts>if_/<orig>`, which causes Wayback to
    serve the original HTML without prepending its toolbar/frame markup.

    If the timestamp segment already carries a modifier (e.g. `if_`, `id_`,
    `im_`) or the URL doesn't match the expected shape, returns it unchanged.
    """
    if not snapshot_url.startswith(_SNAPSHOT_PREFIX):
        return snapshot_url
    rest = snapshot_url[len(_SNAPSHOT_PREFIX) :]
    slash = rest.find("/")
    if slash <= 0:
        return snapshot_url
    timestamp_part = rest[:slash]
    after = rest[slash:]
    if not timestamp_part.isdigit():
        # Already has a modifier suffix, or is malformed; leave alone.
        return snapshot_url
    return f"{_SNAPSHOT_PREFIX}{timestamp_part}if_{after}"
