"""The network seam and the auto-mode escalation chain.

`_http_fetch` and `_browser_fetch` are the monkeypatch seam — tests stub them
(as `vasco.fetch.core._http_fetch` / `._browser_fetch`) so no network or browser
is required. Every function that calls them as a bare module global lives here so
the patched binding is the one that resolves. `_do_fetch_html` is the state
machine that drives the `http → browser → browser+mobile → wayback` chain; it
returns a `_HtmlOutcome` and never builds an envelope (the dispatcher does that).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlsplit

try:  # pragma: no cover - httpx is an optional dep at import time.
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from vasco import strategy as seed_strategies
from vasco.adapters import wayback
from vasco.errors import BrowserServerUnavailable, FailureReason
from vasco.urls import registered_domain

from . import bot_detect, browser
from .phases import _HtmlOutcome, _Phases, _convert_html, _convert_text, _ms_since
from .urlutils import (
    _ACCEPT_ENCODING,
    _HTTP_TIMEOUT_FLOOR,
    _RECOVERABLE_REASONS,
    _SNIFF_BYTES,
    _binary_type_skips_body,
    _content_type,
    _is_binary_unsupported,
    _is_pdf,
    _is_plaintext_response,
    _looks_binary,
    _pandoc_format,
    _route_key,
    _tier_deadline,
    BROWSER_MAX_BUDGET,
    BROWSER_MIN_BUDGET,
    HTTP_MAX_BUDGET,
    MOBILE_MAX_BUDGET,
    MOBILE_MIN_BUDGET,
    WAYBACK_MAX_BUDGET,
    WAYBACK_MIN_BUDGET,
)


# ---------------------------------------------------------------------------
# Network seam (module-level so tests can monkeypatch them)
# ---------------------------------------------------------------------------


# A plain, honest client UA for the `honest` header profile. NOT a spoofed
# browser: a "Chrome" UA without the full browser header suite reads as a headless
# bot to stricter WAFs (→ 403), but an honest client is waved through. Verified
# against gitlab.com + gitlab.wikimedia.org. Deliberately not cfg.fetch.user_agent
# (its default IS the Chrome UA this profile exists to avoid).
_HONEST_USER_AGENT = "Mozilla/5.0 (compatible; Vasco/0.1)"


def _build_request_headers(profile: str, cfg: Any | None) -> dict[str, str]:
    """HTTP request headers for `profile`.

    `browser` (default): a full modern-Chrome shape (`Sec-Fetch-*` etc.) so WAFs
    that reject a bare User-Agent don't short-circuit before the browser tier; the
    UA stays configurable via `cfg.fetch.user_agent`. `honest`: a minimal set
    (plain UA + `Accept`, no `Sec-Fetch-*`/`Upgrade-Insecure-Requests`) for WAFs
    that 403 the half-fingerprint.
    """
    if profile == "honest":
        return {"User-Agent": _HONEST_USER_AGENT, "Accept": "*/*"}
    user_agent = _HONEST_USER_AGENT
    if cfg is not None:
        try:
            user_agent = cfg.fetch.user_agent or user_agent
        except Exception:
            pass
    return {
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


async def _http_fetch(
    url: str,
    *,
    deadline_monotonic: float,
    cfg: Any | None = None,
    profile: str = "browser",
) -> tuple[str, int, dict[str, str]]:
    """HTTP-tier fetch via httpx. Returns (html, status, headers).

    `profile` selects the header set (see `_build_request_headers`): `browser`
    (default modern-Chrome shape) or `honest` (minimal client headers).
    Connection/DNS/timeout failures are folded into the (html, status, headers)
    tuple using sentinel `status=0` and `_failure_hint` header so that
    `bot_detect.classify` can map them to FailureReason without exceptions.

    The response is **streamed**, not eagerly read, so the body download can be
    skipped for a binary blob recognized from its `Content-Type` header alone
    (`_binary_type_skips_body`) — a large image/video/archive is rejected without
    pulling the bytes. `application/octet-stream` is ambiguous (sometimes a
    mislabeled text file), so only a `_SNIFF_BYTES` prefix is read to tell binary
    from text; everything else (html/plain/json/xml/unknown) is read in full.
    The verdict itself is re-derived downstream in `_do_fetch_html`
    (`_is_binary_unsupported`); this only governs how much gets downloaded.
    """
    if httpx is None:
        return "", 0, {"_failure_hint": "dns_fail"}

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return "", 0, {"_failure_hint": "timeout"}

    timeout = max(_HTTP_TIMEOUT_FLOOR, remaining)
    headers_out = _build_request_headers(profile, cfg)
    try:
        async with httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            timeout=timeout,
            headers=headers_out,
        ) as client:
            async with client.stream("GET", url) as resp:
                hdrs = {str(k): str(v) for k, v in resp.headers.items()}
                hdrs.setdefault("_url_final", str(resp.url))
                status = int(resp.status_code)
                ct = _content_type(hdrs, "")

                # Definite binary from the header: don't download the body at all.
                if _binary_type_skips_body(ct):
                    return "", status, hdrs

                # Ambiguous octet-stream: sniff a small prefix, then either stop
                # (binary) or read the rest (mislabeled text).
                if ct == "application/octet-stream":
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf += chunk
                        if len(buf) >= _SNIFF_BYTES:
                            break
                    sniff = bytes(buf).decode("utf-8", "replace")
                    if _looks_binary(sniff):
                        return sniff, status, hdrs
                    async for chunk in resp.aiter_bytes():
                        buf += chunk
                    return bytes(buf).decode("utf-8", "replace"), status, hdrs

                # Text-ish: read the full body and decode via httpx's charset.
                await resp.aread()
                return resp.text, status, hdrs
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


# A fingerprint block on the http tier (a WAF rejecting our header shape) may
# clear with the honest profile. A generic 403 classifies as BLOCKED_CLOUDFLARE,
# a mid-load bot tear-down as BLOCKED_BOT. Captcha/login/rate-limit/5xx won't
# clear with different headers, so they're excluded.
_HEADER_RETRY_REASONS: frozenset[FailureReason] = frozenset(
    {FailureReason.BLOCKED_CLOUDFLARE, FailureReason.BLOCKED_BOT}
)


def _resolve_header_profile(
    url: str, route: str, cache: Any | None, cfg: Any | None
) -> str:
    """Resolve the http-tier header profile for `url` (``browser``/``honest``).

    Precedence: a user ``domains:`` rule (exact host beats registered-domain) →
    the learned per-route profile (`cache.get_header_profile`) → the code seed
    (`strategy.seed_header_profile`) → ``browser``.
    """
    host = (urlsplit(url).hostname or "").lower()
    rd = registered_domain(url)
    rules = getattr(cfg, "domains", ()) or ()
    for want in (host, rd):  # exact host wins over a registered-domain rule
        if not want:
            continue
        for rule in rules:
            if rule.host == want:
                return rule.headers
    if cache is not None and hasattr(cache, "get_header_profile"):
        try:
            learned = cache.get_header_profile(route)
        except Exception:
            learned = None
        if learned:
            return learned
    return seed_strategies.seed_header_profile(host) or "browser"


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

    # The http tier's header profile (browser default / honest) — a second
    # learned+seeded strategy dimension, resolved once for this fetch.
    header_profile = _resolve_header_profile(url, route, cache, cfg)

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
    # disable the recovery tail. A `honest` header profile (seed/config/learned)
    # overrides a learned "browser" tier: the profile exists precisely to make the
    # http tier succeed where the browser-spoof headers were blocked, and the route
    # may have learned "browser" *because* of that block — so always give honest
    # http a chance before the browser tier.
    skip_http = mode == "auto" and strategy == "browser" and header_profile != "honest"

    if not skip_http:
        t0 = time.monotonic()
        html, status, headers = await _http_fetch(
            url,
            deadline_monotonic=_tier_deadline(deadline_monotonic, HTTP_MAX_BUDGET),
            cfg=cfg,
            profile=header_profile,
        )
        phases.network_ms += _ms_since(t0)
        phases.attempts += 1
        reason = bot_detect.classify(status, html, headers)

        # Adaptive honest-header retry: a fingerprint block on the browser profile
        # may clear with honest minimal headers. Try the http tier once more before
        # spending a browser launch, and learn the winner so the next fetch starts
        # honest. (No-op when the profile is already honest, or budget is spent.)
        if (
            reason in _HEADER_RETRY_REASONS
            and header_profile == "browser"
            and (deadline_monotonic - time.monotonic()) >= _HTTP_TIMEOUT_FLOOR
        ):
            t0 = time.monotonic()
            h_html, h_status, h_headers = await _http_fetch(
                url,
                deadline_monotonic=_tier_deadline(deadline_monotonic, HTTP_MAX_BUDGET),
                cfg=cfg,
                profile="honest",
            )
            phases.network_ms += _ms_since(t0)
            phases.attempts += 1
            if bot_detect.classify(h_status, h_html, h_headers) == FailureReason.OK:
                html, status, headers, reason = (
                    h_html,
                    h_status,
                    h_headers,
                    FailureReason.OK,
                )
                if cache is not None and hasattr(cache, "set_header_profile"):
                    try:
                        cache.set_header_profile(route, "honest")
                    except Exception:
                        pass

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
                ct = _content_type(headers, "")
                if _is_binary_unsupported(ct, html):
                    # A binary blob (image / audio / video / archive / octet-
                    # stream). Fail fast: mojibake-ing it through trafilatura gives
                    # zero words, which would read as an unrendered shell and
                    # escalate to the browser tier — which then tries to *download*
                    # the blob and times out (a misleading TIMEOUT). UNSUPPORTED_
                    # CONTENT_TYPE is honest and carries a ~24h negative-cache TTL.
                    return _HtmlOutcome(
                        html,
                        status,
                        headers,
                        FailureReason.UNSUPPORTED_CONTENT_TYPE,
                        "http",
                        browser_started,
                    )
                if _is_plaintext_response(ct, html):
                    # A text/plain / Markdown body (raw .md / .txt / RFC / LICENSE)
                    # is already readable text. Pass it through verbatim — running
                    # it through the HTML extractor yields zero words, which would
                    # otherwise look like an unrendered shell and pointlessly
                    # escalate to the browser tier (or fail EMPTY_BODY).
                    markdown, meta = _convert_text(html, ct, phases)
                else:
                    markdown, meta = _convert_html(html, url_final, phases)
                    escalate_empty = (
                        mode == "auto"
                        and meta.get("word_count", 0) == 0
                        and (deadline_monotonic - time.monotonic())
                        >= BROWSER_MIN_BUDGET
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
