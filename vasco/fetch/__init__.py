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
import importlib.util
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

try:  # pragma: no cover - httpx is an optional dep at import time.
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from . import bot_detect, browser
from vasco import io as io_mod, quality as quality_mod, strategy as seed_strategies
from vasco.config import QualityCfg
from vasco.envelope import (
    base_envelope as _base_envelope,
    failure_envelope as _failure_envelope,
    now_epoch as _now_epoch,
    success_envelope as _success_envelope,
)
from vasco.converters import convert, pandoc, pdf
from vasco.adapters import (
    aliexpress,
    google_shopping,
    mercadolivre,
    olx,
    realestate,
    shopee,
    shopify,
    wayback,
    wikimedia,
    youtube,
)
from vasco.errors import BrowserServerUnavailable, FailureReason


def _supported_accept_encoding() -> str:
    """Build an ``Accept-Encoding`` value from encodings we can actually decode.

    Advertising an encoding httpx can't decode (e.g. ``zstd`` without the
    ``zstandard`` package) makes the server send it and httpx hand back the
    raw compressed bytes — silently corrupting ``.text`` so extraction yields
    nothing. gzip/deflate are always available via stdlib zlib; br and zstd
    depend on optional packages (declared as deps, but probed here so a
    minimal env degrades gracefully instead of corrupting).
    """
    encodings = ["gzip", "deflate"]
    if importlib.util.find_spec("brotli") or importlib.util.find_spec("brotlicffi"):
        encodings.append("br")
    if importlib.util.find_spec("zstandard"):
        encodings.append("zstd")
    return ", ".join(encodings)


_ACCEPT_ENCODING = _supported_accept_encoding()


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
# 12s (not 8) gives heavy-but-loadable pages a fair shot at reaching
# domcontentloaded. The chain still fits the 30s kill-switch: 5+12+5+6 = 28s,
# and the MIN-budget gates below self-truncate mobile/wayback when little time
# remains. Don't raise past 12 without also bumping the default deadline.
BROWSER_MAX_BUDGET: float = 12.0
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


@dataclass
class _HtmlOutcome:
    """Terminal result of the html fetch state machine (`_do_fetch_html`).

    Carries the raw tier result. For a *kept* http-tier success in auto/http mode
    it also carries the trafilatura conversion (`markdown`/`meta`) so the caller
    reuses it instead of converting the same html twice — the common-path
    optimization that keeps word_count escalation "basically free". Every other
    tier leaves these None and is converted once downstream.
    """

    html: str
    status: int
    headers: dict[str, str]
    reason: FailureReason
    mode_used: str
    browser_started: bool
    markdown: str | None = None
    meta: dict[str, Any] | None = None


def _ms_since(monotonic_started: float) -> int:
    return int((time.monotonic() - monotonic_started) * 1000)


def _convert_html(html: str, url: str, phases: _Phases) -> tuple[str, dict[str, Any]]:
    """Convert html→markdown with parse-phase timing; never raises.

    On any conversion error returns ``("", {"word_count": 0})`` so callers treat
    it as empty content (and escalate) rather than crash.
    """
    t0 = time.monotonic()
    try:
        markdown, meta = convert.html_to_markdown(html, url=url)
    except Exception:
        markdown, meta = "", {"word_count": 0}
    phases.parse_ms += _ms_since(t0)
    return markdown, meta


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

    # A bare User-Agent + Accept:*/* is itself a "non-browser" tell on the
    # HTTP tier — any one missing Sec-Fetch-* header lets WAFs short-circuit
    # before we even reach the browser tier. We send a fixed modern-Chrome
    # shape; the UA stays configurable via cfg.fetch.user_agent.
    headers_out = {
        "User-Agent": user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": _ACCEPT_ENCODING,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
    }
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
    except BrowserServerUnavailable:
        # The browser tier is a separate peer service that isn't running. Don't
        # raise — return a sentinel so the auto chain escalates (e.g. to wayback)
        # like any other browser-tier failure; classify() maps it to
        # BROWSER_UNAVAILABLE if the whole chain ultimately fails.
        return "", 0, {"_failure_hint": "browser_unavailable"}
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


def _pandoc_format(url: str, headers: dict[str, str] | None) -> str | None:
    ext = (
        urlsplit(url).path.rsplit(".", 1)[-1].lower()
        if "." in urlsplit(url).path
        else ""
    )
    if ext in pandoc.FORMAT_BY_EXT:
        return pandoc.FORMAT_BY_EXT[ext]
    if not headers:
        return None
    for k, v in headers.items():
        if str(k).lower() == "content-type":
            ct = str(v).split(";", 1)[0].strip().lower()
            if ct in pandoc.FORMAT_BY_MIME:
                return pandoc.FORMAT_BY_MIME[ct]
    return None


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
        from vasco import cache as cache_mod

        return cache_mod.registered_domain(url)
    except Exception:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host


def _route_key(url: str) -> str:
    """Per-route strategy key (registered domain + first path segment).

    Falls back to the bare registered domain if `cache.route_key` is
    unavailable for any reason.
    """
    try:
        from vasco import cache as cache_mod

        return cache_mod.route_key(url)
    except Exception:
        return _registered_domain(url)


# ---------------------------------------------------------------------------
# Envelope TTL + cache-hit hydration
# (the base/success/failure builders live in vasco.envelope)
# ---------------------------------------------------------------------------


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
    route: str,
    bump: bool,
) -> tuple[str, int, dict[str, str], FailureReason]:
    """Single browser fetch (desktop or mobile) with phase accounting.

    When `bump=True` and the call wasn't mobile, records the outcome against
    the per-route strategy cache. Mobile is always a recovery tier — it never
    affects the strategy.
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
            cache.bump(route, mode="browser", success=(reason == FailureReason.OK))
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
    raw: bool = False,
    allow_snapshot: bool = True,
) -> _HtmlOutcome:
    """Execute the fetch state machine; returns the terminal result.

    Caller mode semantics:
    - `http`, `browser`, `mobile`, `wayback`: terminal — only that tier runs.
    - `auto`: chained — http → browser → browser+mobile → wayback, with the
      starting tier chosen by the cached domain strategy. Recovery tiers
      (mobile, wayback) always run after a browser failure with a recoverable
      reason, gated by remaining budget. The domain strategy is an
      optimization on where to start; it does not shorten the recovery tail.

    `allow_snapshot` gates *only* the automatic Wayback recovery tier (the last
    auto-mode tier). Content adapters set it False because they parse live
    structured data (prices, stock, listings) — an archived snapshot is stale and
    its rewritten HTML breaks the adapter's anchor, so an honest `BLOCKED_*`
    failure beats a plausible-but-wrong one. It does NOT affect the explicit
    `mode="wayback"` terminal (deliberate user intent always honored).

    Updates `phases` in place: bumps `attempts` for each network call,
    accumulates `network_ms`, and records `escalated_from` if the http tier
    was tried first then escalated.

    Content escalation: in auto mode (and not `raw`), an http-tier 200 is
    converted here and, if it extracts zero words (an unrendered shell that
    `bot_detect` couldn't flag — it only sees raw HTML), the chain escalates to
    the browser tier instead of accepting an empty success. The conversion is
    returned on the `_HtmlOutcome` so a kept http result is never converted
    twice. `raw` skips this entirely (callers want html verbatim — including the
    content adapters, which parse embedded JSON, not prose).
    """
    route = _route_key(url)
    strategy: str | None = None
    if cache is not None and hasattr(cache, "get_strategy"):
        try:
            strategy = cache.get_strategy(route)
        except Exception:
            strategy = None
    # No learned row yet → fall back to the declarative seed (vasco/strategy.py).
    if strategy is None:
        strategy = seed_strategies.seed_strategy(route)

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
            route=route,
            bump=True,
        )
        return _HtmlOutcome(html, status, headers, reason, mode, True)

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
            return _HtmlOutcome(
                html, status, headers, reason, "wayback", browser_started
            )
        return _HtmlOutcome(
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
            # Content-sufficiency check: a 200 that converts to zero words is an
            # unrendered shell (often marker-less, so bot_detect can't flag it).
            # Convert here so the verdict uses trafilatura's authoritative
            # word_count; the conversion rides back on the outcome so a kept http
            # result isn't converted twice. Skip for raw mode (verbatim html) and
            # non-HTML payloads (pdf/doc redirects) where word_count is meaningless.
            url_final = (
                headers.get("_url_final") if isinstance(headers, dict) else None
            ) or url
            markdown = meta = None
            escalate_empty = False
            if (
                not raw
                and not _is_pdf(url_final, headers)
                and _pandoc_format(url_final, headers) is None
            ):
                markdown, meta = _convert_html(html, url_final, phases)
                escalate_empty = (
                    mode == "auto"
                    and meta.get("word_count", 0) == 0
                    and (deadline_monotonic - time.monotonic()) >= BROWSER_MIN_BUDGET
                )
            if not escalate_empty:
                if cache is not None and hasattr(cache, "bump"):
                    try:
                        cache.bump(route, mode="http", success=True)
                    except Exception:
                        pass
                return _HtmlOutcome(
                    html,
                    status,
                    headers,
                    reason,
                    "http",
                    browser_started,
                    markdown,
                    meta,
                )
            # Empty unrendered shell → escalate to the browser tier. Discard the
            # wasted shell conversion and do not record an http success (so the
            # route doesn't learn to start at http for a page that needs a render).
            phases.escalated_from = "http"

        if mode == "http" and reason != FailureReason.OK:
            # Caller-explicit http: terminal.
            if cache is not None and hasattr(cache, "bump"):
                try:
                    cache.bump(route, mode="http", success=False)
                except Exception:
                    pass
            return _HtmlOutcome(html, status, headers, reason, "http", browser_started)

        # The server gave a definitive "this URL doesn't exist" answer; no
        # later tier can conjure the resource back.
        if reason == FailureReason.NOT_FOUND:
            return _HtmlOutcome(html, status, headers, reason, "http", browser_started)

        if (
            reason != FailureReason.OK
            and (deadline_monotonic - time.monotonic()) < BROWSER_MIN_BUDGET
        ):
            if cache is not None and hasattr(cache, "bump"):
                try:
                    cache.bump(route, mode="http", success=False)
                except Exception:
                    pass
            return _HtmlOutcome(
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
        route=route,
        bump=True,
    )
    browser_started = True

    if b_reason == FailureReason.OK or b_reason not in _RECOVERABLE_REASONS:
        return _HtmlOutcome(
            b_html, b_status, b_headers, b_reason, "browser", browser_started
        )

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
                route=route,
                bump=False,
            )
        except Exception:
            m_reason = FailureReason.SERVER_ERROR  # soft-skip
            m_html = m_status = m_headers = None  # type: ignore[assignment]
        else:
            if m_reason == FailureReason.OK:
                return _HtmlOutcome(
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
                return _HtmlOutcome(
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
    # Skipped when `allow_snapshot` is off (content adapters): an archived
    # snapshot of a live commerce/listing page is stale and breaks their anchor.
    if allow_snapshot and (deadline_monotonic - time.monotonic()) >= WAYBACK_MIN_BUDGET:
        wb = await _try_wayback_recovery(
            url,
            deadline_monotonic=_tier_deadline(deadline_monotonic, WAYBACK_MAX_BUDGET),
            cfg=cfg,
            phases=phases,
        )
        if wb is not None:
            wb_html, wb_status, wb_headers, wb_reason = wb
            return _HtmlOutcome(
                wb_html, wb_status, wb_headers, wb_reason, "wayback", browser_started
            )

    # All recovery tiers exhausted. Return the prior browser failure.
    return _HtmlOutcome(
        last_html, last_status, last_headers, last_reason, last_mode, browser_started
    )


def _make_adapter_fetcher(
    url: str,
    normalized: str,
    *,
    mode: str,
    deadline_monotonic: float,
    cache: Any | None,
    cfg: Any | None,
    phases: _Phases,
) -> tuple[Any, dict[str, bool]]:
    """Build an injectable HTML fetcher backed by the shared escalation chain.

    Content adapters (real-estate, Google Shopping) parse provider HTML into
    their own envelope but obtain that HTML through this fetcher, so they share
    one fetch path and the per-route strategy/seed system instead of hardcoding
    a browser-only fetch. Returns ``(fetch_html, state)``; ``state``'s
    ``browser_started`` reflects whether any browser tier ran (so the caller
    knows whether to close the pool).
    """
    base = _base_envelope(
        url_requested=url,
        url_normalized=normalized,
        url_final=None,
        http_status=0,
        mode_used="http",
        content_type="text/html",
    )
    state = {"browser_started": False}

    async def fetch_html(target: str):
        # raw=True: adapters parse embedded JSON, not prose, so the word_count
        # escalation must not apply (a valid listing page can have little prose
        # but rich JSON). They run their own fetch/parse and share only the tier
        # chain, so they want the html verbatim with no trafilatura conversion.
        # allow_snapshot=False: adapters parse live structured data, so an
        # archived Wayback snapshot is stale and its rewritten HTML breaks the
        # anchor — fail honestly with the block reason instead of recovering.
        outcome = await _do_fetch_html(
            target,
            base=base,
            mode=mode,
            deadline_monotonic=deadline_monotonic,
            cache=cache,
            cfg=cfg,
            phases=phases,
            raw=True,
            allow_snapshot=False,
        )
        state["browser_started"] = state["browser_started"] or outcome.browser_started
        return (
            outcome.html,
            outcome.status,
            outcome.headers,
            outcome.reason,
            outcome.mode_used,
        )

    return fetch_html, state


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


async def _fetch_pandoc_doc(
    url: str,
    *,
    fmt: str,
    base: dict[str, Any],
    deadline_monotonic: float,
    cfg: Any | None,
    phases: _Phases,
) -> dict[str, Any]:
    if httpx is None:
        return _failure_envelope(
            base=base,
            reason=FailureReason.UNSUPPORTED_CONTENT_TYPE,
            message="httpx not available for document download",
        )

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return _failure_envelope(
            base=base,
            reason=FailureReason.DEADLINE_EXCEEDED,
            message="deadline elapsed before document download",
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
            message=f"document fetch error: {type(exc).__name__}",
        )
    phases.network_ms += _ms_since(t0)
    phases.attempts += 1

    t_parse = time.monotonic()
    try:
        text, meta = pandoc.pandoc_to_markdown(body, fmt=fmt)
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
            message=f"pandoc convert error: {type(exc).__name__}",
        )
    phases.parse_ms += _ms_since(t_parse)

    mime = next(
        (m for m, f in pandoc.FORMAT_BY_MIME.items() if f == fmt),
        f"application/{fmt}",
    )
    base["content_type"] = mime
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
    warning, and write it to cache. Shared by the youtube / wikimedia /
    google_shopping / realestate dispatch branches in `_fetch_one_body`."""
    envelope["url_requested"] = url
    envelope["url_canonical"] = normalized
    if raw:
        envelope.setdefault("warnings", []).append(f"raw_unsupported_for_{service}")
    if use_cache and cache is not None:
        _cache_put(cache, envelope, phases, ttl_seconds=_ttl_for(envelope, cfg))
    return envelope


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
        envelope = _finalize_adapter_envelope(
            envelope,
            url=url,
            normalized=normalized,
            raw=raw,
            service="youtube",
            use_cache=use_cache,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )
        return envelope, False, phases

    # --- Wikimedia shortcut (Enterprise only; no creds → normal HTTP) ------
    if wikimedia.is_wikimedia_url(url) and wikimedia.has_credentials(cfg):
        envelope = await wikimedia.fetch_wikimedia(url, deadline=deadline, cfg=cfg)
        envelope = _finalize_adapter_envelope(
            envelope,
            url=url,
            normalized=normalized,
            raw=raw,
            service="wikimedia",
            use_cache=use_cache,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )
        return envelope, False, phases

    # --- Google Shopping route (HTML via the shared escalation chain) -------
    if google_shopping.is_google_shopping_url(url):
        fetch_html, state = _make_adapter_fetcher(
            url,
            normalized,
            mode=mode,
            deadline_monotonic=deadline_monotonic,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )
        envelope = await google_shopping.fetch_google_shopping(
            url, deadline=deadline, cfg=cfg, fetch_html=fetch_html
        )
        envelope = _finalize_adapter_envelope(
            envelope,
            url=url,
            normalized=normalized,
            raw=raw,
            service="google_shopping",
            use_cache=use_cache,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )
        return envelope, state["browser_started"], phases

    # --- Real-estate route (HTML via the shared escalation chain) -----------
    if realestate.is_realestate_url(url):
        fetch_html, state = _make_adapter_fetcher(
            url,
            normalized,
            mode=mode,
            deadline_monotonic=deadline_monotonic,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )
        envelope = await realestate.fetch_realestate(
            url, deadline=deadline, cfg=cfg, fetch_html=fetch_html
        )
        envelope = _finalize_adapter_envelope(
            envelope,
            url=url,
            normalized=normalized,
            raw=raw,
            service="realestate",
            use_cache=use_cache,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )
        return envelope, state["browser_started"], phases

    # --- OLX route (real-estate + vehicle verticals; HTML via the chain) -----
    if olx.is_olx_url(url):
        fetch_html, state = _make_adapter_fetcher(
            url,
            normalized,
            mode=mode,
            deadline_monotonic=deadline_monotonic,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )
        envelope = await olx.fetch_olx(
            url, deadline=deadline, cfg=cfg, fetch_html=fetch_html
        )
        envelope = _finalize_adapter_envelope(
            envelope,
            url=url,
            normalized=normalized,
            raw=raw,
            service="olx",
            use_cache=use_cache,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )
        return envelope, state["browser_started"], phases

    # --- MercadoLivre route (search + product; HTML via the chain) ----------
    if mercadolivre.is_mercadolivre_url(url):
        fetch_html, state = _make_adapter_fetcher(
            url,
            normalized,
            mode=mode,
            deadline_monotonic=deadline_monotonic,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )
        envelope = await mercadolivre.fetch_mercadolivre(
            url, deadline=deadline, cfg=cfg, fetch_html=fetch_html
        )
        envelope = _finalize_adapter_envelope(
            envelope,
            url=url,
            normalized=normalized,
            raw=raw,
            service="mercadolivre",
            use_cache=use_cache,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )
        return envelope, state["browser_started"], phases

    # --- AliExpress route (search cards + detail/reviews; HTML via the chain) -
    if aliexpress.is_aliexpress_url(url):
        fetch_html, state = _make_adapter_fetcher(
            url,
            normalized,
            mode=mode,
            deadline_monotonic=deadline_monotonic,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )
        envelope = await aliexpress.fetch_aliexpress(
            url, deadline=deadline, cfg=cfg, fetch_html=fetch_html
        )
        envelope = _finalize_adapter_envelope(
            envelope,
            url=url,
            normalized=normalized,
            raw=raw,
            service="aliexpress",
            use_cache=use_cache,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )
        return envelope, state["browser_started"], phases

    # --- Shopee route (product pages via the Product JSON-LD spine) ----------
    if shopee.is_shopee_url(url):
        fetch_html, state = _make_adapter_fetcher(
            url,
            normalized,
            mode=mode,
            deadline_monotonic=deadline_monotonic,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )
        envelope = await shopee.fetch_shopee(
            url, deadline=deadline, cfg=cfg, fetch_html=fetch_html
        )
        envelope = _finalize_adapter_envelope(
            envelope,
            url=url,
            normalized=normalized,
            raw=raw,
            service="shopee",
            use_cache=use_cache,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )
        return envelope, state["browser_started"], phases

    # --- Shopify route (product/collection/search via platform JSON endpoints) -
    # Known domains dispatch directly; unknown product/collection URLs are probed
    # and fall through to a normal fetch on a miss (NotShopify), so a non-Shopify
    # lookalike is never turned into a failure.
    shopify_probe_browser = False
    is_known_shopify = shopify.is_shopify_url(url, cfg, cache)
    if is_known_shopify or shopify.is_shopify_candidate(url, cfg, cache):
        fetch_html, state = _make_adapter_fetcher(
            url,
            normalized,
            mode=mode,
            deadline_monotonic=deadline_monotonic,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )
        try:
            envelope = await shopify.fetch_shopify(
                url,
                deadline=deadline,
                cfg=cfg,
                fetch_html=fetch_html,
                cache=cache,
                probe=not is_known_shopify,
            )
        except shopify.NotShopify:
            # Probe miss → fall through to the normal fetch path below; carry the
            # probe's browser usage forward so the pool is still closed.
            shopify_probe_browser = state["browser_started"]
        else:
            envelope = _finalize_adapter_envelope(
                envelope,
                url=url,
                normalized=normalized,
                raw=raw,
                service="shopify",
                use_cache=use_cache,
                cache=cache,
                cfg=cfg,
                phases=phases,
            )
            return envelope, state["browser_started"], phases

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

    # --- Pandoc document shortcut -------------------------------------------
    pandoc_fmt = _pandoc_format(url, None)
    if pandoc_fmt is not None:
        base["mode_used"] = "pandoc"
        envelope = await _fetch_pandoc_doc(
            url,
            fmt=pandoc_fmt,
            base=base,
            deadline_monotonic=deadline_monotonic,
            cfg=cfg,
            phases=phases,
        )
        if use_cache and cache is not None:
            _cache_put(cache, envelope, phases, ttl_seconds=_ttl_for(envelope, cfg))
        return envelope, False, phases

    # --- HTML auto-mode escalation ------------------------------------------
    browser_started = shopify_probe_browser
    try:
        outcome = await _do_fetch_html(
            url,
            base=base,
            mode=mode,
            deadline_monotonic=deadline_monotonic,
            cache=cache,
            cfg=cfg,
            phases=phases,
            raw=raw,
        )
        html = outcome.html
        status = outcome.status
        headers = outcome.headers
        reason = outcome.reason
        mode_used = outcome.mode_used
        browser_started = outcome.browser_started

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

        # Same for pandoc-supported document formats behind a redirect.
        pandoc_fmt = _pandoc_format(base["url_final"], headers)
        if pandoc_fmt is not None:
            base["mode_used"] = "pandoc"
            envelope = await _fetch_pandoc_doc(
                base["url_final"],
                fmt=pandoc_fmt,
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
            # Reuse the http-tier conversion when the chain already did it (the
            # word_count escalation needs it, so a kept http result rides back on
            # the outcome) — otherwise convert the winning tier's html once here.
            if outcome.meta is not None:
                markdown, meta = outcome.markdown or "", outcome.meta
            else:
                markdown, meta = _convert_html(html, base["url_final"], phases)
            # No extractable text after the auto chain exhausted its content
            # tiers: an unrendered shell the browser tier couldn't fill either.
            # Surface a clean fetch-level failure (PARSE_FAILED-style, produced
            # post-conversion) instead of caching an empty "success".
            if meta.get("word_count", 0) == 0:
                envelope = _failure_envelope(
                    base=base,
                    reason=FailureReason.EMPTY_BODY,
                    message=(
                        f"200 OK from {mode_used} tier but no readable text was "
                        "extracted — likely a JavaScript-only page the browser "
                        "tier could not render (or the browser server was down)."
                    ),
                )
                if use_cache and cache is not None:
                    _cache_put(
                        cache, envelope, phases, ttl_seconds=_ttl_for(envelope, cfg)
                    )
                return envelope, browser_started, phases
            quality_cfg = cfg.quality if cfg is not None else QualityCfg()
            if quality_cfg is not None:
                quality_scores = quality_mod.score(
                    markdown,
                    url=base["url_final"],
                    cfg=quality_cfg,
                    existing_quality=meta.get("quality", {}),
                    metadata=meta,
                    raw_html=html,
                )
                meta["quality"].update(quality_scores)
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
