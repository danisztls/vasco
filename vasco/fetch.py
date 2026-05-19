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
from typing import Any
from urllib.parse import urlsplit

try:  # pragma: no cover - httpx is an optional dep at import time.
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from . import bot_detect, browser, convert, io as io_mod, pdf, youtube
from .errors import FailureReason


# Minimum remaining deadline (seconds) before we'll bother escalating from
# http tier to browser tier. Below this floor we return DEADLINE_EXCEEDED
# rather than spawn Firefox for nothing.
BROWSER_MIN_BUDGET: float = 3.0

# Default request timeout floor (seconds) for httpx within an outer deadline.
_HTTP_TIMEOUT_FLOOR = 1.0


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


_BROWSER_DISCONNECT_MARKERS: tuple[str, ...] = (
    "connection closed",
    "target closed",
    "target page, context or browser has been closed",
    "browser has been closed",
    "page.content",
    "page.goto",
    "net::err_aborted",
    "net::err_http2_protocol_error",
    "econnreset",
)


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
) -> tuple[str, int, dict[str, str]]:
    """Browser-tier fetch via the Camoufox singleton."""
    pool = browser.get_browser(cfg)
    try:
        return await pool.fetch(url, deadline_monotonic=deadline_monotonic)
    except asyncio.TimeoutError:
        return "", 0, {"_failure_hint": "timeout"}
    except Exception as exc:
        if _looks_like_bot_block(exc):
            return "", 0, {"_failure_hint": "bot_blocked"}
        raise


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


def _ttl_for(envelope: dict[str, Any], cfg: Any | None) -> int:
    success = "failure" not in envelope
    default = 86400 if success else 900
    if cfg is None:
        return default
    try:
        if success:
            return int(cfg.fetch.ttl_seconds)
        return int(cfg.fetch.failure_ttl_seconds)
    except Exception:
        return default


def _hydrate_cache_hit(
    envelope: dict[str, Any], *, url_requested: str
) -> dict[str, Any]:
    """Mark a cached envelope as such, refresh cache_age, and restore the
    caller's original url_requested.
    """
    env = dict(envelope)
    fetched_at = int(env.get("fetched_at") or _now_epoch())
    env["from_cache"] = True
    env["cache_age_seconds"] = max(0, _now_epoch() - fetched_at)
    env["url_requested"] = url_requested
    return env


# ---------------------------------------------------------------------------
# Auto-mode escalation
# ---------------------------------------------------------------------------


async def _do_fetch_html(
    url: str,
    *,
    base: dict[str, Any],
    mode: str,
    deadline_monotonic: float,
    cache: Any | None,
    cfg: Any | None,
) -> tuple[
    str,  # html
    int,  # status
    dict[str, str],  # headers
    FailureReason,  # final reason
    str,  # final mode_used
    bool,  # browser_started (so caller can close)
]:
    """Execute the auto-mode escalation; returns the terminal result."""
    domain = _registered_domain(url)
    strategy: str | None = None
    if cache is not None and hasattr(cache, "get_domain_strategy"):
        try:
            strategy = cache.get_domain_strategy(domain)
        except Exception:
            strategy = None

    effective_mode = mode
    if effective_mode == "auto":
        effective_mode = strategy if strategy in ("http", "browser") else "http"

    browser_started = False

    if effective_mode == "browser":
        html, status, headers = await _browser_fetch(
            url, deadline_monotonic=deadline_monotonic, cfg=cfg
        )
        browser_started = True
        reason = bot_detect.classify(status, html, headers)
        if cache is not None and hasattr(cache, "bump"):
            try:
                cache.bump(domain, mode="browser", success=(reason == FailureReason.OK))
            except Exception:
                pass
        return html, status, headers, reason, "browser", browser_started

    # http tier (with optional escalation)
    html, status, headers = await _http_fetch(
        url, deadline_monotonic=deadline_monotonic, cfg=cfg
    )
    reason = bot_detect.classify(status, html, headers)

    if reason == FailureReason.OK:
        if cache is not None and hasattr(cache, "bump"):
            try:
                cache.bump(domain, mode="http", success=True)
            except Exception:
                pass
        return html, status, headers, reason, "http", browser_started

    # http failed → consider escalation.
    if mode == "http":
        if cache is not None and hasattr(cache, "bump"):
            try:
                cache.bump(domain, mode="http", success=False)
            except Exception:
                pass
        return html, status, headers, reason, "http", browser_started

    time_remaining = deadline_monotonic - time.monotonic()
    if time_remaining < BROWSER_MIN_BUDGET:
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

    # Escalate to browser tier.
    b_html, b_status, b_headers = await _browser_fetch(
        url, deadline_monotonic=deadline_monotonic, cfg=cfg
    )
    browser_started = True
    b_reason = bot_detect.classify(b_status, b_html, b_headers)
    if cache is not None and hasattr(cache, "bump"):
        # Attribute the bump to the tier that actually produced this outcome.
        try:
            cache.bump(domain, mode="browser", success=(b_reason == FailureReason.OK))
        except Exception:
            pass
    return b_html, b_status, b_headers, b_reason, "browser", browser_started


# ---------------------------------------------------------------------------
# Core fetch logic (single body, both entry points use it)
# ---------------------------------------------------------------------------


async def _fetch_pdf(
    url: str,
    *,
    base: dict[str, Any],
    deadline_monotonic: float,
    cfg: Any | None,
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
        return _failure_envelope(
            base=base,
            reason=FailureReason.DNS_FAIL,
            message=f"pdf fetch error: {type(exc).__name__}",
        )

    try:
        text, meta = pdf.pdf_to_text(body)
    except FileNotFoundError as exc:
        return _failure_envelope(
            base=base,
            reason=FailureReason.UNSUPPORTED_CONTENT_TYPE,
            message=str(exc),
        )
    except Exception as exc:
        return _failure_envelope(
            base=base,
            reason=FailureReason.UNSUPPORTED_CONTENT_TYPE,
            message=f"pdf parse error: {type(exc).__name__}",
        )

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
        return _failure_envelope(
            base=base,
            reason=FailureReason.INVALID_URL,
            message="URL could not be normalized",
        ), False

    # --- Cache hit -----------------------------------------------------------
    if use_cache and not refresh and cache is not None:
        try:
            hit = cache.get(normalized)
        except Exception:
            hit = None
        if hit is not None:
            return _hydrate_cache_hit(hit, url_requested=url), False

    deadline_monotonic = time.monotonic() + max(0.0, float(deadline))

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
            try:
                cache.put(envelope, ttl_seconds=_ttl_for(envelope, cfg))
            except Exception:
                pass
        return envelope, False

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
            url, base=base, deadline_monotonic=deadline_monotonic, cfg=cfg
        )
        if use_cache and cache is not None:
            try:
                cache.put(envelope, ttl_seconds=_ttl_for(envelope, cfg))
            except Exception:
                pass
        return envelope, False

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
            )
            if use_cache and cache is not None:
                try:
                    cache.put(envelope, ttl_seconds=_ttl_for(envelope, cfg))
                except Exception:
                    pass
            return envelope, browser_started

        if reason != FailureReason.OK:
            envelope = _failure_envelope(
                base=base,
                reason=reason,
                message=f"{reason} after {mode_used} tier",
                retry_after=_parse_retry_after(headers),
                partial_html=html if raw else None,
            )
            if not raw and html:
                try:
                    markdown, _meta = convert.html_to_markdown(
                        html, url=base["url_final"]
                    )
                    if markdown:
                        envelope["markdown"] = markdown
                except Exception:
                    pass
            if use_cache and cache is not None:
                try:
                    cache.put(envelope, ttl_seconds=_ttl_for(envelope, cfg))
                except Exception:
                    pass
            return envelope, browser_started

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
            markdown, meta = convert.html_to_markdown(html, url=base["url_final"])
            envelope = _success_envelope(
                base=base,
                markdown=markdown,
                metadata=meta,
                token_count_estimate=io_mod.estimate_tokens(markdown),
            )

        if use_cache and cache is not None:
            try:
                cache.put(envelope, ttl_seconds=_ttl_for(envelope, cfg))
            except Exception:
                pass
        return envelope, browser_started
    except Exception as exc:
        # Last-resort safety net: never raise out of fetch.
        envelope = _failure_envelope(
            base=base,
            reason=FailureReason.SERVER_ERROR,
            message=f"unhandled fetch error: {type(exc).__name__}: {exc}",
        )
        if use_cache and cache is not None:
            try:
                cache.put(envelope, ttl_seconds=_ttl_for(envelope, cfg))
            except Exception:
                pass
        return envelope, browser_started


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_one(
    url: str,
    *,
    mode: str = "auto",
    deadline: float = 15.0,
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
    deadline: float = 15.0,
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
