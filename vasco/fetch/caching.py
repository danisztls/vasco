# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cache-side concerns of the fetch path: per-reason negative-cache TTLs,
cache-hit hydration, the timed cache write, and the adapter-envelope finalizer.

The base/success/failure envelope builders live in `vasco.envelope`; this module
only decides *how long* an envelope lives in cache and stamps the caller's URLs
onto adapter-built envelopes before persisting them.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from vasco.envelope import now_epoch as _now_epoch
from vasco.errors import FailureReason

from .phases import _ms_since, _Phases

# Negative-cache TTL multipliers, keyed by failure reason. Some failures
# (NOT_FOUND, ROBOTS_DISALLOW, INVALID_URL) won't change for a long time and
# deserve the full success TTL; others (TIMEOUT, SERVER_ERROR) are transient
# and should expire quickly so a retry can pick up a recovered upstream.
_FAILURE_TTL_MULTIPLIER: dict[FailureReason, float] = {
    FailureReason.NOT_FOUND: 96.0,  # ~24h at default 900s base
    FailureReason.ROBOTS_DISALLOW: 96.0,
    FailureReason.INVALID_URL: 96.0,
    FailureReason.UNSUPPORTED_CONTENT_TYPE: 96.0,
    # A category-landing hub is a stable property of the URL shape (no listings
    # there, ever), so pin it long like the other structural permanents.
    FailureReason.CATEGORY_LANDING: 96.0,
    FailureReason.PAYWALL_HARD: 24.0,  # ~6h
    FailureReason.LOGIN_REQUIRED: 24.0,
    FailureReason.BLOCKED_BOT: 4.0,  # ~1h
    FailureReason.BLOCKED_CLOUDFLARE: 4.0,
    FailureReason.BLOCKED_CAPTCHA: 4.0,
    FailureReason.TIMEOUT: 0.33,  # ~5min
    FailureReason.DEADLINE_EXCEEDED: 0.33,
    FailureReason.SERVER_ERROR: 0.33,
    FailureReason.DNS_FAIL: 0.33,
    # Scraper-rot: fixed by a code change (or a site reverting), so expire fast
    # — a 24h pin would keep serving the failure long after the adapter is fixed.
    FailureReason.PARSE_FAILED: 0.33,
    # Browser server not running: transient/operational, heals as soon as the
    # peer service is back — retry soon rather than pinning the failure.
    FailureReason.BROWSER_UNAVAILABLE: 0.33,
    # Empty body: a 200 that rendered no text — a JS shell may render later, or
    # the browser tier may simply have been down. Expire fast so a retry heals.
    FailureReason.EMPTY_BODY: 0.33,
}


def _ttl_for(envelope: dict[str, Any], cfg: Any | None) -> int:
    success = "failure" not in envelope
    if success:
        try:
            return int(cfg.fetch.ttl_seconds) if cfg is not None else 86400
        except Exception:
            return 86400
    try:
        base = int(cfg.fetch.failure_ttl_seconds) if cfg is not None else 900
    except Exception:
        base = 900
    reason_str = envelope.get("failure", {}).get("reason")
    try:
        reason = FailureReason(reason_str)
    except (ValueError, TypeError):
        return base
    return max(1, int(base * _FAILURE_TTL_MULTIPLIER.get(reason, 1.0)))


_LIVE_FETCH_PHASE_KEYS = (
    "duration_ms",
    "network_ms",
    "parse_ms",
    "cache_write_ms",
    "attempts",
    "escalated_from",
)


def _hydrate_cache_hit(
    envelope: dict[str, Any], *, url_requested: str
) -> dict[str, Any]:
    """Mark a cached envelope as such, refresh cache_age, and restore the
    caller's original url_requested.

    Live-fetch phase fields are stripped: they describe how the entry was
    originally obtained and are misleading on a cache hit. The caller stamps
    a fresh `duration_ms` for the cache-read path.
    """
    env = {k: v for k, v in envelope.items() if k not in _LIVE_FETCH_PHASE_KEYS}
    fetched_at = int(env.get("fetched_at") or _now_epoch())
    env["from_cache"] = True
    env["cache_age_seconds"] = max(0, _now_epoch() - fetched_at)
    env["url_requested"] = url_requested
    return env


def _cache_put(
    cache: Any, envelope: dict[str, Any], phases: _Phases, *, ttl_seconds: int
) -> None:
    """Time and execute a cache write. Failures are swallowed (best-effort)."""
    t0 = time.monotonic()
    with contextlib.suppress(Exception):
        cache.put(envelope, ttl_seconds=ttl_seconds)
    phases.cache_write_ms += _ms_since(t0)


def _finalize_adapter_envelope(
    envelope: dict[str, Any],
    *,
    url: str,
    normalized: str,
    raw: bool,
    service: str,
    use_cache: bool,
    cache: Any | None,
    cfg: Any | None,
    phases: _Phases | None,
) -> dict[str, Any]:
    """Stamp the caller's URLs onto an adapter-built envelope, add the raw-mode
    warning, and write it to cache. Shared by the youtube/wikimedia branches,
    the adapter registry loop, and shopify in `_fetch_one_body`."""
    envelope["url_requested"] = url
    envelope["url_canonical"] = normalized
    if raw:
        envelope.setdefault("warnings", []).append(f"raw_unsupported_for_{service}")
    if use_cache and cache is not None:
        _cache_put(cache, envelope, phases, ttl_seconds=_ttl_for(envelope, cfg))
    return envelope
