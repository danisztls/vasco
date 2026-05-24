"""Top-level fetch orchestration: auto-mode escalation, envelope assembly,
deadline handling, single + batch entry points.

Public surface:
- `fetch_one(url, ...)`: returns a single envelope dict.
- `fetch_many(urls, ...)`: yields envelopes as they complete (NOT in order).

Failures are first-class output: `fetch_one` does not raise; it returns a
failure envelope. The two helpers `_http_fetch` and `_browser_fetch` are
module-level so tests can monkeypatch them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

try:  # pragma: no cover - httpx is an optional dep at import time.
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from . import bot_detect, browser, convert, io as io_mod, pdf, wayback, youtube
from .errors import FailureReason


# Minimum remaining deadline (seconds) before we'll bother escalating from
# http tier to browser tier. Below this floor we return DEADLINE_EXCEEDED
# rather than spawn Firefox for nothing.
BROWSER_MIN_BUDGET: float = 3.0

# Same idea for the post-browser recovery tiers in the auto chain. Mobile
# re-uses the running Camoufox instance, so the floor matches browser.
# Wayback adds an Availability API round-trip on top of the snapshot fetch,
# so it needs slightly more headroom.
MOBILE_MIN_BUDGET: float = 3.0
WAYBACK_MIN_BUDGET: float = 4.0

# Per-tier wall-clock caps. These are the *primary* budget contract — each
# tier runs for up to its cap, and the chain naturally takes up to the sum
# (≈24s for http→browser→mobile→wayback). The caller-supplied `deadline`
# is a kill-switch hard upper bound, defaulted generously so the per-tier
# caps are what users feel in practice. Each tier's effective deadline is
# `min(global_kill_switch, now + tier_cap)`.
HTTP_MAX_BUDGET: float = 5.0
BROWSER_MAX_BUDGET: float = 8.0
MOBILE_MAX_BUDGET: float = 5.0
WAYBACK_MAX_BUDGET: float = 6.0


def _tier_deadline(global_deadline: float, tier_max: float) -> float:
    """Clamp a per-tier deadline so a hung tier can't starve the next one."""
    return min(global_deadline, time.monotonic() + tier_max)


# Failure reasons that justify spending budget on mobile/wayback recovery.
# Other failures (NOT_FOUND, DNS_FAIL, etc.) won't change with a new tier.
_RECOVERABLE_REASONS: frozenset[FailureReason] = frozenset(
    {
        FailureReason.BLOCKED_BOT,
        FailureReason.BLOCKED_CAPTCHA,
        FailureReason.BLOCKED_CLOUDFLARE,
    }
)

# Default request timeout floor (seconds) for httpx within an outer deadline.
_HTTP_TIMEOUT_FLOOR = 1.0


@dataclass
class _Phases:
    """Accumulator threaded through a single fetch to break duration into parts.

    Fields are stamped onto the success/failure envelope at the boundary of
    `_fetch_one_inner` so callers (telemetry, tests) can distinguish a slow
    network from a slow parse from a 2-attempt escalation.
    """

    network_ms: int = 0
    parse_ms: int = 0
    cache_write_ms: int = 0
    attempts: int = 0
    escalated_from: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def _ms_since(monotonic_started: float) -> int:
    return int((time.monotonic() - monotonic_started) * 1000)


def _stamp_phases(
    envelope: dict[str, Any],
    *,
    started_monotonic: float,
    phases: _Phases | None,
) -> dict[str, Any]:
    """Write duration_ms + phase fields onto the envelope in place.

    When `phases` is not None, all timing/attempt fields are stamped — even
    when zero — so a value of 0 unambiguously means "this phase ran fast"
    rather than "this phase was skipped." When `phases` is None (cache hit,
    invalid URL, YouTube), only `duration_ms` is stamped.
    """
    envelope["duration_ms"] = _ms_since(started_monotonic)
    if phases is None:
        return envelope
    envelope["network_ms"] = phases.network_ms
    envelope["parse_ms"] = phases.parse_ms
    envelope["cache_write_ms"] = phases.cache_write_ms
    envelope["attempts"] = phases.attempts
    if phases.escalated_from is not None:
        envelope["escalated_from"] = phases.escalated_from
    return envelope


# ---------------------------------------------------------------------------
# Helpers (module-level so tests can monkeypatch)
# ---------------------------------------------------------------------------


async def _http_fetch(
    url: str,
    *,
    deadline_monotonic: float,
    cfg: Any | None = None,
) -> tuple[str, int, dict[str, str]]:
    """HTTP-tier fetch via httpx. Returns (html, status, headers).

    Connection/DNS/timeout failures are folded into the (html, status, headers)
    tuple using sentinel `status=0` and `_failure_hint` header so that
    `bot_detect.classify` can map them to FailureReason without exceptions.
    """
    if httpx is None:
        return "", 0, {"_failure_hint": "dns_fail"}

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return "", 0, {"_failure_hint": "timeout"}

    timeout = max(_HTTP_TIMEOUT_FLOOR, remaining)
    user_agent = "Mozilla/5.0 (compatible; Vasco/0.1)"
    if cfg is not None:
        try:
            user_agent = cfg.fetch.user_agent or user_agent
        except Exception:
            pass

    headers_out = {"User-Agent": user_agent, "Accept": "*/*"}
    try:
        async with httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            timeout=timeout,
            headers=headers_out,
        ) as client:
            resp = await client.get(url)
            text = resp.text
            hdrs = {str(k): str(v) for k, v in resp.headers.items()}
            hdrs.setdefault("_url_final", str(resp.url))
            return text, int(resp.status_code), hdrs
    except asyncio.TimeoutError:
        return "", 0, {"_failure_hint": "timeout"}
    except Exception as exc:
        name = type(exc).__name__.lower()
        if "timeout" in name:
            return "", 0, {"_failure_hint": "timeout"}
        return "", 0, {"_failure_hint": "dns_fail"}


# Markers are disconnect-specific. "page.goto" / "page.content" were dropped:
# they also appear in plain Playwright TimeoutError messages and were causing
# slow loads to be mislabeled as BLOCKED_BOT.
_BROWSER_DISCONNECT_MARKERS: tuple[str, ...] = (
    "connection closed",
    "target closed",
    "target page, context or browser has been closed",
    "browser has been closed",
    "net::err_aborted",
    "net::err_http2_protocol_error",
    "econnreset",
)


def _looks_like_timeout(exc: BaseException) -> bool:
    # Playwright's TimeoutError is a separate class from asyncio.TimeoutError.
    return type(exc).__name__ == "TimeoutError" or "timeout" in str(exc).lower()


def _looks_like_bot_block(exc: BaseException) -> bool:
    """Heuristic: did Camoufox lose the page mid-load?

    Playwright surfaces anti-bot tear-downs as connection/target-closed errors
    rather than HTTP statuses, so a transport exception during browser fetch is
    far more likely bot detection than a true network failure.
    """
    msg = str(exc).lower()
    return any(m in msg for m in _BROWSER_DISCONNECT_MARKERS)


async def _browser_fetch(
    url: str,
    *,
    deadline_monotonic: float,
    cfg: Any | None = None,
    mobile: bool = False,
) -> tuple[str, int, dict[str, str]]:
    """Browser-tier fetch via the Camoufox singleton.

    When `mobile=True`, the page runs in a fresh mobile context (iOS Safari
    UA + iPhone viewport + touch). Used as a recovery tier when the regular
    browser fetch is blocked.
    """
    pool = browser.get_browser(cfg)
    try:
        return await pool.fetch(
            url, deadline_monotonic=deadline_monotonic, mobile=mobile
        )
    except asyncio.TimeoutError:
        return "", 0, {"_failure_hint": "timeout"}
    except Exception as exc:
        if _looks_like_timeout(exc):
            return "", 0, {"_failure_hint": "timeout"}
        if _looks_like_bot_block(exc):
            return "", 0, {"_failure_hint": "bot_blocked"}
        raise


async def _wayback_fetch(
    url: str,
    *,
    deadline_monotonic: float,
    cfg: Any | None = None,
) -> tuple[str, int, dict[str, str], str | None]:
    """Resolve a Wayback snapshot for `url`, then http-fetch it.

    Returns (html, status, headers, snapshot_url). When no snapshot exists
    or the API fails, returns ("", 0, {"_failure_hint": "wayback_miss"}, None)
    so the caller can fall back to the prior failure envelope.
    """
    snapshot_url = await wayback.find_snapshot(
        url, deadline_monotonic=deadline_monotonic, cfg=cfg
    )
    if snapshot_url is None:
        return "", 0, {"_failure_hint": "wayback_miss"}, None
    html, status, headers = await _http_fetch(
        snapshot_url, deadline_monotonic=deadline_monotonic, cfg=cfg
    )
    headers.setdefault("_url_final", snapshot_url)
    return html, status, headers, snapshot_url


def _parse_retry_after(headers: dict[str, str] | None) -> int | None:
    if not headers:
        return None
    for k, v in headers.items():
        if str(k).lower() == "retry-after":
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
    return None


def _now_epoch() -> int:
    return int(time.time())


def _is_pdf(url: str, headers: dict[str, str] | None) -> bool:
    path = urlsplit(url).path.lower()
    if path.endswith(".pdf"):
        return True
    if not headers:
        return False
    for k, v in headers.items():
        if str(k).lower() == "content-type":
            return "application/pdf" in str(v).lower()
    return False


def _content_type(headers: dict[str, str] | None, default: str) -> str:
    if not headers:
        return default
    for k, v in headers.items():
        if str(k).lower() == "content-type":
            return str(v).split(";", 1)[0].strip() or default
    return default


def _normalize_url(url: str, cache: Any | None) -> str | None:
    if cache is not None and hasattr(cache, "normalize_url"):
        try:
            return cache.normalize_url(url)
        except Exception:
            return None
    try:
        from . import cache as cache_mod

        return cache_mod.normalize_url(url)
    except Exception:
        return url if isinstance(url, str) and "://" in url else None


def _registered_domain(url: str) -> str:
    try:
        from . import cache as cache_mod

        return cache_mod.registered_domain(url)
    except Exception:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host


# ---------------------------------------------------------------------------
# Envelope construction
# ---------------------------------------------------------------------------


def _base_envelope(
    *,
    url_requested: str,
    url_normalized: str | None,
    url_final: str | None,
    http_status: int,
    mode_used: str,
    content_type: str,
) -> dict[str, Any]:
    return {
        "url_requested": url_requested,
        "url_final": url_final or url_requested,
        "url_canonical": url_normalized or url_requested,
        "http_status": http_status,
        "mode_used": mode_used,
        "fetched_at": _now_epoch(),
        "from_cache": False,
        "cache_age_seconds": 0,
        "content_type": content_type,
    }


def _success_envelope(
    *,
    base: dict[str, Any],
    markdown: str,
    metadata: dict[str, Any],
    token_count_estimate: int,
) -> dict[str, Any]:
    env = dict(base)
    env.update(
        {
            "title": metadata.get("title"),
            "byline": metadata.get("byline"),
            "published": metadata.get("published"),
            "modified": metadata.get("modified"),
            "language": metadata.get("language"),
            "site_name": metadata.get("site_name"),
            "word_count": metadata.get("word_count", 0),
            "token_count_estimate": token_count_estimate,
            "quality": metadata.get("quality", {}),
            "links": metadata.get("links", []),
            "markdown": markdown,
            "warnings": list(metadata.get("warnings", [])),
        }
    )
    return env


def _failure_envelope(
    *,
    base: dict[str, Any],
    reason: FailureReason,
    message: str,
    retry_after: int | None = None,
    partial_html: str | None = None,
    partial_markdown: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    env = dict(base)
    env["failure"] = {
        "reason": str(reason),
        "retry_after_seconds": retry_after,
        "message": message,
    }
    env["markdown"] = partial_markdown or partial_html or ""
    env["warnings"] = list(warnings or [])
    return env


# Negative-cache TTL multipliers, keyed by failure reason. Some failures
# (NOT_FOUND, ROBOTS_DISALLOW, INVALID_URL) won't change for a long time and
# deserve the full success TTL; others (TIMEOUT, SERVER_ERROR) are transient
# and should expire quickly so a retry can pick up a recovered upstream.
_FAILURE_TTL_MULTIPLIER: dict[FailureReason, float] = {
    FailureReason.NOT_FOUND: 96.0,  # ~24h at default 900s base
    FailureReason.ROBOTS_DISALLOW: 96.0,
    FailureReason.INVALID_URL: 96.0,
    FailureReason.UNSUPPORTED_CONTENT_TYPE: 96.0,
    FailureReason.PAYWALL_HARD: 24.0,  # ~6h
    FailureReason.LOGIN_REQUIRED: 24.0,
    FailureReason.BLOCKED_BOT: 4.0,  # ~1h
    FailureReason.BLOCKED_CLOUDFLARE: 4.0,
    FailureReason.BLOCKED_CAPTCHA: 4.0,
    FailureReason.TIMEOUT: 0.33,  # ~5min
    FailureReason.DEADLINE_EXCEEDED: 0.33,
    FailureReason.SERVER_ERROR: 0.33,
    FailureReason.DNS_FAIL: 0.33,
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


# ---------------------------------------------------------------------------
# Auto-mode escalation
# ---------------------------------------------------------------------------


async def _try_wayback_recovery(
    url: str,
    *,
    deadline_monotonic: float,
    cfg: Any | None,
    phases: _Phases,
) -> tuple[str, int, dict[str, str], FailureReason] | None:
    """Last-resort recovery via Wayback Machine.

    Returns the fetched envelope tuple on success, or None when no snapshot
    exists / wayback itself failed (caller keeps the prior failure).
    """
    t0 = time.monotonic()
    wb_html, wb_status, wb_headers, snapshot_url = await _wayback_fetch(
        url, deadline_monotonic=deadline_monotonic, cfg=cfg
    )
    phases.network_ms += _ms_since(t0)
    phases.attempts += 1
    if snapshot_url is None:
        return None
    wb_reason = bot_detect.classify(wb_status, wb_html, wb_headers)
    if wb_reason != FailureReason.OK:
        return None
    return wb_html, wb_status, wb_headers, wb_reason


async def _run_browser_tier(
    url: str,
    *,
    mobile: bool,
    deadline_monotonic: float,
    cfg: Any | None,
    phases: _Phases,
    cache: Any | None,
    domain: str,
    bump: bool,
) -> tuple[str, int, dict[str, str], FailureReason]:
    """Single browser fetch (desktop or mobile) with phase accounting.

    When `bump=True` and the call wasn't mobile, records the outcome against
    the domain strategy cache. Mobile is always a recovery tier — it never
    affects domain strategy.
    """
    tier_cap = MOBILE_MAX_BUDGET if mobile else BROWSER_MAX_BUDGET
    t0 = time.monotonic()
    html, status, headers = await _browser_fetch(
        url,
        deadline_monotonic=_tier_deadline(deadline_monotonic, tier_cap),
        cfg=cfg,
        mobile=mobile,
    )
    phases.network_ms += _ms_since(t0)
    phases.attempts += 1
    reason = bot_detect.classify(status, html, headers)
    if bump and not mobile and cache is not None and hasattr(cache, "bump"):
        try:
            cache.bump(domain, mode="browser", success=(reason == FailureReason.OK))
        except Exception:
            pass
    return html, status, headers, reason


async def _do_fetch_html(
    url: str,
    *,
    base: dict[str, Any],
    mode: str,
    deadline_monotonic: float,
    cache: Any | None,
    cfg: Any | None,
    phases: _Phases,
) -> tuple[
    str,  # html
    int,  # status
    dict[str, str],  # headers
    FailureReason,  # final reason
    str,  # final mode_used
    bool,  # browser_started (so caller can close)
]:
    """Execute the fetch state machine; returns the terminal result.

    Caller mode semantics:
    - `http`, `browser`, `mobile`, `wayback`: terminal — only that tier runs.
    - `auto`: chained — http → browser → browser+mobile → wayback, with the
      starting tier chosen by the cached domain strategy. Recovery tiers
      (mobile, wayback) always run after a browser failure with a recoverable
      reason, gated by remaining budget. The domain strategy is an
      optimization on where to start; it does not shorten the recovery tail.

    Updates `phases` in place: bumps `attempts` for each network call,
    accumulates `network_ms`, and records `escalated_from` if the http tier
    was tried first then escalated.
    """
    domain = _registered_domain(url)
    strategy: str | None = None
    if cache is not None and hasattr(cache, "get_domain_strategy"):
        try:
            strategy = cache.get_domain_strategy(domain)
        except Exception:
            strategy = None

    browser_started = False

    # --- Explicit terminal: browser / mobile --------------------------------
    if mode in ("browser", "mobile"):
        is_mobile = mode == "mobile"
        html, status, headers, reason = await _run_browser_tier(
            url,
            mobile=is_mobile,
            deadline_monotonic=deadline_monotonic,
            cfg=cfg,
            phases=phases,
            cache=cache,
            domain=domain,
            bump=True,
        )
        return html, status, headers, reason, mode, True

    # --- Explicit terminal: wayback -----------------------------------------
    if mode == "wayback":
        result = await _try_wayback_recovery(
            url,
            deadline_monotonic=_tier_deadline(deadline_monotonic, WAYBACK_MAX_BUDGET),
            cfg=cfg,
            phases=phases,
        )
        if result is not None:
            html, status, headers, reason = result
            return html, status, headers, reason, "wayback", browser_started
        return (
            "",
            0,
            {"_failure_hint": "wayback_miss"},
            FailureReason.NOT_FOUND,
            "wayback",
            browser_started,
        )

    # --- mode="http" or "auto" ----------------------------------------------
    # The domain strategy chooses the starting tier in auto mode. It does NOT
    # disable the recovery tail.
    skip_http = mode == "auto" and strategy == "browser"

    if not skip_http:
        t0 = time.monotonic()
        html, status, headers = await _http_fetch(
            url,
            deadline_monotonic=_tier_deadline(deadline_monotonic, HTTP_MAX_BUDGET),
            cfg=cfg,
        )
        phases.network_ms += _ms_since(t0)
        phases.attempts += 1
        reason = bot_detect.classify(status, html, headers)

        if reason == FailureReason.OK:
            if cache is not None and hasattr(cache, "bump"):
                try:
                    cache.bump(domain, mode="http", success=True)
                except Exception:
                    pass
            return html, status, headers, reason, "http", browser_started

        if mode == "http":
            # Caller-explicit http: terminal.
            if cache is not None and hasattr(cache, "bump"):
                try:
                    cache.bump(domain, mode="http", success=False)
                except Exception:
                    pass
            return html, status, headers, reason, "http", browser_started

        # The server gave a definitive "this URL doesn't exist" answer; no
        # later tier can conjure the resource back.
        if reason == FailureReason.NOT_FOUND:
            return html, status, headers, reason, "http", browser_started

        if (deadline_monotonic - time.monotonic()) < BROWSER_MIN_BUDGET:
            if cache is not None and hasattr(cache, "bump"):
                try:
                    cache.bump(domain, mode="http", success=False)
                except Exception:
                    pass
            return (
                html,
                status,
                headers,
                FailureReason.DEADLINE_EXCEEDED,
                "http",
                browser_started,
            )

        phases.escalated_from = "http"

    # --- Browser tier --------------------------------------------------------
    b_html, b_status, b_headers, b_reason = await _run_browser_tier(
        url,
        mobile=False,
        deadline_monotonic=deadline_monotonic,
        cfg=cfg,
        phases=phases,
        cache=cache,
        domain=domain,
        bump=True,
    )
    browser_started = True

    if b_reason == FailureReason.OK or b_reason not in _RECOVERABLE_REASONS:
        return b_html, b_status, b_headers, b_reason, "browser", browser_started

    # --- Recovery tier 1: browser + mobile ----------------------------------
    last_html, last_status, last_headers, last_reason = (
        b_html,
        b_status,
        b_headers,
        b_reason,
    )
    last_mode = "browser"

    if (deadline_monotonic - time.monotonic()) >= MOBILE_MIN_BUDGET:
        # Mobile is best-effort: it's a recovery tier, not a primary path.
        # Wrap so an unexpected Playwright/context error (e.g., capability
        # unsupported by the underlying engine) falls through to wayback
        # instead of failing the whole fetch.
        try:
            m_html, m_status, m_headers, m_reason = await _run_browser_tier(
                url,
                mobile=True,
                deadline_monotonic=deadline_monotonic,
                cfg=cfg,
                phases=phases,
                cache=cache,
                domain=domain,
                bump=False,
            )
        except Exception:
            m_reason = FailureReason.SERVER_ERROR  # soft-skip
            m_html = m_status = m_headers = None  # type: ignore[assignment]
        else:
            if m_reason == FailureReason.OK:
                return (
                    m_html,
                    m_status,
                    m_headers,
                    m_reason,
                    "browser+mobile",
                    browser_started,
                )
            if m_reason not in _RECOVERABLE_REASONS:
                # Mobile surfaced a different (non-block) failure — that
                # fresher signal is more useful than the original browser
                # block.
                return (
                    m_html,
                    m_status,
                    m_headers,
                    m_reason,
                    "browser+mobile",
                    browser_started,
                )
        # Still blocked (or mobile errored); keep the original browser failure
        # as "last" so the reported mode_used reflects what the user is
        # actually blocked from.

    # --- Recovery tier 2: Wayback Machine -----------------------------------
    if (deadline_monotonic - time.monotonic()) >= WAYBACK_MIN_BUDGET:
        wb = await _try_wayback_recovery(
            url,
            deadline_monotonic=_tier_deadline(deadline_monotonic, WAYBACK_MAX_BUDGET),
            cfg=cfg,
            phases=phases,
        )
        if wb is not None:
            wb_html, wb_status, wb_headers, wb_reason = wb
            return wb_html, wb_status, wb_headers, wb_reason, "wayback", browser_started

    # All recovery tiers exhausted. Return the prior browser failure.
    return last_html, last_status, last_headers, last_reason, last_mode, browser_started


# ---------------------------------------------------------------------------
# Core fetch logic (single body, both entry points use it)
# ---------------------------------------------------------------------------


async def _fetch_pdf(
    url: str,
    *,
    base: dict[str, Any],
    deadline_monotonic: float,
    cfg: Any | None,
    phases: _Phases,
) -> dict[str, Any]:
    if httpx is None:
        return _failure_envelope(
            base=base,
            reason=FailureReason.UNSUPPORTED_CONTENT_TYPE,
            message="httpx not available for PDF download",
        )

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return _failure_envelope(
            base=base,
            reason=FailureReason.DEADLINE_EXCEEDED,
            message="deadline elapsed before PDF download",
        )

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            timeout=max(_HTTP_TIMEOUT_FLOOR, remaining),
        ) as client:
            resp = await client.get(url)
            body = resp.content
            base["url_final"] = str(resp.url)
            base["http_status"] = int(resp.status_code)
    except Exception as exc:
        phases.network_ms += _ms_since(t0)
        phases.attempts += 1
        return _failure_envelope(
            base=base,
            reason=FailureReason.DNS_FAIL,
            message=f"pdf fetch error: {type(exc).__name__}",
        )
    phases.network_ms += _ms_since(t0)
    phases.attempts += 1

    t_parse = time.monotonic()
    try:
        text, meta = pdf.pdf_to_text(body)
    except FileNotFoundError as exc:
        phases.parse_ms += _ms_since(t_parse)
        return _failure_envelope(
            base=base,
            reason=FailureReason.UNSUPPORTED_CONTENT_TYPE,
            message=str(exc),
        )
    except Exception as exc:
        phases.parse_ms += _ms_since(t_parse)
        return _failure_envelope(
            base=base,
            reason=FailureReason.UNSUPPORTED_CONTENT_TYPE,
            message=f"pdf parse error: {type(exc).__name__}",
        )
    phases.parse_ms += _ms_since(t_parse)

    base["content_type"] = "application/pdf"
    return _success_envelope(
        base=base,
        markdown=text,
        metadata=meta,
        token_count_estimate=io_mod.estimate_tokens(text),
    )


async def _fetch_one_inner(
    url: str,
    *,
    mode: str,
    deadline: float,
    use_cache: bool,
    refresh: bool,
    raw: bool,
    cache: Any | None,
    cfg: Any | None,
) -> tuple[dict[str, Any], bool]:
    """Single-URL fetch returning (envelope, browser_started).

    Callers are responsible for browser-pool shutdown based on the
    browser_started flag (single fetch closes immediately; batch closes once
    after the whole batch).

    Stamps the envelope with `duration_ms` (always) and — for fresh fetches
    — the phase breakdown captured in `_Phases` (network/parse/cache_write
    in ms, plus attempts and escalated_from). See `_stamp_phases`.
    """
    started = time.monotonic()
    envelope, browser_started, phases = await _fetch_one_body(
        url,
        mode=mode,
        deadline=deadline,
        use_cache=use_cache,
        refresh=refresh,
        raw=raw,
        cache=cache,
        cfg=cfg,
    )
    _stamp_phases(envelope, started_monotonic=started, phases=phases)
    return envelope, browser_started


def _cache_put(
    cache: Any, envelope: dict[str, Any], phases: _Phases, *, ttl_seconds: int
) -> None:
    """Time and execute a cache write. Failures are swallowed (best-effort)."""
    t0 = time.monotonic()
    try:
        cache.put(envelope, ttl_seconds=ttl_seconds)
    except Exception:
        pass
    phases.cache_write_ms += _ms_since(t0)


async def _fetch_one_body(
    url: str,
    *,
    mode: str,
    deadline: float,
    use_cache: bool,
    refresh: bool,
    raw: bool,
    cache: Any | None,
    cfg: Any | None,
) -> tuple[dict[str, Any], bool, _Phases | None]:
    """Full single-fetch state machine. Returns (envelope, browser_started, phases).

    `phases` is None on short-circuit paths (invalid URL, cache hit, YouTube)
    where the phase breakdown doesn't apply — only `duration_ms` is stamped.
    """
    normalized = _normalize_url(url, cache)
    if not normalized:
        base = _base_envelope(
            url_requested=url,
            url_normalized=None,
            url_final=None,
            http_status=0,
            mode_used="http",
            content_type="",
        )
        return (
            _failure_envelope(
                base=base,
                reason=FailureReason.INVALID_URL,
                message="URL could not be normalized",
            ),
            False,
            None,
        )

    # --- Cache hit -----------------------------------------------------------
    if use_cache and not refresh and cache is not None:
        try:
            hit = cache.get(normalized)
        except Exception:
            hit = None
        if hit is not None:
            return _hydrate_cache_hit(hit, url_requested=url), False, None

    deadline_monotonic = time.monotonic() + max(0.0, float(deadline))
    phases = _Phases()

    # --- YouTube shortcut ---------------------------------------------------
    # YouTube transcripts have their own envelope shape (mode_used="youtube",
    # content_type="text/youtube"); skip HTTP/browser tier entirely.
    if youtube.is_youtube_url(url):
        envelope = await youtube.fetch_youtube(url, deadline=deadline, cfg=cfg)
        envelope["url_requested"] = url
        envelope["url_canonical"] = normalized
        if raw:
            envelope.setdefault("warnings", []).append("raw_unsupported_for_youtube")
        if use_cache and cache is not None:
            _cache_put(cache, envelope, phases, ttl_seconds=_ttl_for(envelope, cfg))
        return envelope, False, phases

    base = _base_envelope(
        url_requested=url,
        url_normalized=normalized,
        url_final=None,
        http_status=0,
        mode_used="http",
        content_type="text/html",
    )

    # --- PDF shortcut --------------------------------------------------------
    if _is_pdf(url, None):
        base["mode_used"] = "pdf"
        envelope = await _fetch_pdf(
            url,
            base=base,
            deadline_monotonic=deadline_monotonic,
            cfg=cfg,
            phases=phases,
        )
        if use_cache and cache is not None:
            _cache_put(cache, envelope, phases, ttl_seconds=_ttl_for(envelope, cfg))
        return envelope, False, phases

    # --- HTML auto-mode escalation ------------------------------------------
    browser_started = False
    try:
        (
            html,
            status,
            headers,
            reason,
            mode_used,
            browser_started,
        ) = await _do_fetch_html(
            url,
            base=base,
            mode=mode,
            deadline_monotonic=deadline_monotonic,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )

        base["http_status"] = int(status or 0)
        base["mode_used"] = mode_used
        base["content_type"] = _content_type(headers, "text/html")
        url_final = headers.get("_url_final") if isinstance(headers, dict) else None
        base["url_final"] = url_final or url

        # If the server actually served a PDF behind a redirect, switch.
        if _is_pdf(base["url_final"], headers):
            base["mode_used"] = "pdf"
            envelope = await _fetch_pdf(
                base["url_final"],
                base=base,
                deadline_monotonic=deadline_monotonic,
                cfg=cfg,
                phases=phases,
            )
            if use_cache and cache is not None:
                _cache_put(cache, envelope, phases, ttl_seconds=_ttl_for(envelope, cfg))
            return envelope, browser_started, phases

        if reason != FailureReason.OK:
            envelope = _failure_envelope(
                base=base,
                reason=reason,
                message=f"{reason} after {mode_used} tier",
                retry_after=_parse_retry_after(headers),
                partial_html=html if raw else None,
            )
            if not raw and html:
                t_parse = time.monotonic()
                try:
                    markdown, _meta = convert.html_to_markdown(
                        html, url=base["url_final"]
                    )
                    if markdown:
                        envelope["markdown"] = markdown
                except Exception:
                    pass
                phases.parse_ms += _ms_since(t_parse)
            if use_cache and cache is not None:
                _cache_put(cache, envelope, phases, ttl_seconds=_ttl_for(envelope, cfg))
            return envelope, browser_started, phases

        # Success path.
        if raw:
            envelope = _success_envelope(
                base=base,
                markdown=html,
                metadata={
                    "title": None,
                    "byline": None,
                    "published": None,
                    "modified": None,
                    "language": None,
                    "site_name": None,
                    "word_count": len((html or "").split()),
                    "links": [],
                    "quality": {},
                    "warnings": ["raw"],
                },
                token_count_estimate=io_mod.estimate_tokens(html or ""),
            )
        else:
            t_parse = time.monotonic()
            markdown, meta = convert.html_to_markdown(html, url=base["url_final"])
            phases.parse_ms += _ms_since(t_parse)
            envelope = _success_envelope(
                base=base,
                markdown=markdown,
                metadata=meta,
                token_count_estimate=io_mod.estimate_tokens(markdown),
            )

        if use_cache and cache is not None:
            _cache_put(cache, envelope, phases, ttl_seconds=_ttl_for(envelope, cfg))
        return envelope, browser_started, phases
    except Exception as exc:
        # Last-resort safety net: never raise out of fetch.
        envelope = _failure_envelope(
            base=base,
            reason=FailureReason.SERVER_ERROR,
            message=f"unhandled fetch error: {type(exc).__name__}: {exc}",
        )
        if use_cache and cache is not None:
            _cache_put(cache, envelope, phases, ttl_seconds=_ttl_for(envelope, cfg))
        return envelope, browser_started, phases


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_one(
    url: str,
    *,
    mode: str = "auto",
    deadline: float = 30.0,
    use_cache: bool = True,
    refresh: bool = False,
    raw: bool = False,
    cache: Any | None = None,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """Fetch one URL and return an envelope (success or failure)."""
    envelope, browser_started = await _fetch_one_inner(
        url,
        mode=mode,
        deadline=deadline,
        use_cache=use_cache,
        refresh=refresh,
        raw=raw,
        cache=cache,
        cfg=cfg,
    )
    if browser_started:
        try:
            await browser.get_browser(cfg).close()
        except Exception:
            pass
    return envelope


async def fetch_many(
    urls: list[str],
    *,
    workers: int = 4,
    mode: str = "auto",
    deadline: float = 30.0,
    use_cache: bool = True,
    refresh: bool = False,
    raw: bool = False,
    cache: Any | None = None,
    cfg: Any | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield envelopes as they complete (not in input order).

    Reuses one browser instance for the duration of the batch; closes it once
    in a `finally` block.
    """
    if not urls:
        return

    sem = asyncio.Semaphore(max(1, int(workers)))
    any_browser = False

    async def _bounded(url: str) -> dict[str, Any]:
        nonlocal any_browser
        async with sem:
            envelope, started = await _fetch_one_inner(
                url,
                mode=mode,
                deadline=deadline,
                use_cache=use_cache,
                refresh=refresh,
                raw=raw,
                cache=cache,
                cfg=cfg,
            )
            if started:
                any_browser = True
            return envelope

    tasks = [asyncio.create_task(_bounded(u)) for u in urls]
    try:
        for coro in asyncio.as_completed(tasks):
            envelope = await coro
            yield envelope
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        if any_browser:
            try:
                await browser.get_browser(cfg).close()
            except Exception:
                pass
