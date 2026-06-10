"""Top-level fetch orchestration: dispatch, envelope assembly, deadline
handling, single + batch entry points.

Public surface:
- `fetch_one(url, ...)`: returns a single envelope dict.
- `fetch_many(urls, ...)`: yields envelopes as they complete (NOT in order).

Failures are first-class output: `fetch_one` does not raise; it returns a
failure envelope.

The implementation is split across sibling modules and re-exported here so the
test/adapter `vasco.fetch.X` contract keeps working:
- `core.py` — the network seam (`_http_fetch`/`_browser_fetch`/`_wayback_fetch`,
  monkeypatched as `vasco.fetch.core._http_fetch`) and the auto-escalation chain
  (`_do_fetch_html`).
- `phases.py` — `_Phases`/`_HtmlOutcome` + timing helpers.
- `urlutils.py` — URL/format helpers, tier budget constants.
- `documents.py` — PDF/pandoc binary fetchers.
- `caching.py` — negative-cache TTLs + cache-hit hydration + the adapter finalizer.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, NamedTuple

try:  # pragma: no cover - httpx is an optional dep at import time.
    import httpx as httpx  # re-export anchor: tests patch vasco.fetch.httpx.AsyncClient
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from vasco import io as io_mod, quality as quality_mod
from vasco.config import QualityCfg
from vasco.converters import convert
from vasco.envelope import (
    base_envelope as _base_envelope,
    failure_envelope as _failure_envelope,
    success_envelope as _success_envelope,
)
from vasco.errors import FailureReason
from vasco.adapters import (
    aliexpress,
    google_shopping,
    mercadolivre,
    olx,
    realestate,
    shopee,
    shopify,
    steam,
    wikimedia,
    youtube,
)

from . import browser
from .caching import (
    _cache_put,
    _finalize_adapter_envelope,
    _hydrate_cache_hit,
    _ttl_for,
)
from .core import _do_fetch_html
from .documents import _fetch_pandoc_doc, _fetch_pdf
from .phases import _Phases, _convert_html, _ms_since, _stamp_phases
from .urlutils import (
    _content_type,
    _is_pdf,
    _normalize_url,
    _pandoc_format,
    _parse_retry_after,
)

# Re-exported so tests/adapters resolve them as `vasco.fetch.<name>`: the chain's
# monkeypatch seam lives in `core`, but `_http_fetch` is also called directly in
# unit tests, and the encoding/budget helpers are read straight off the package.
from .core import _http_fetch as _http_fetch
from .urlutils import (
    BROWSER_MIN_BUDGET as BROWSER_MIN_BUDGET,
    _ACCEPT_ENCODING as _ACCEPT_ENCODING,
    _supported_accept_encoding as _supported_accept_encoding,
)


# ---------------------------------------------------------------------------
# Adapter fetcher plumbing
# ---------------------------------------------------------------------------


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

    Content adapters (real-estate, Google Shopping, …) parse provider HTML into
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
# Content-adapter registry
# ---------------------------------------------------------------------------


class _AdapterRoute(NamedTuple):
    """One html-chain content adapter: a URL predicate + its fetch entrypoint.

    All html-chain adapters share the signature
    ``fetch(url, *, deadline=, cfg=, fetch_html=) -> dict`` and obtain their
    HTML through `_make_adapter_fetcher`, so a single dispatch loop handles them.
    `service` is the literal label used for the raw-mode warning + telemetry.
    """

    service: str
    matches: Callable[[str], bool]
    fetch: Callable[..., Awaitable[dict[str, Any]]]


# Built once on first use, not at import time: the adapters import
# `vasco.fetch.browser`, so touching their attributes (`is_X_url`/`fetch_X`)
# during module load would race the `adapter → vasco.fetch` circular import when
# an adapter module is imported before this package. Predicates are
# domain-disjoint; order mirrors the historical branch order so first-match-wins
# parity holds if two ever overlap. youtube/wikimedia (self-fetching, own
# envelope) and shopify (probe + NotShopify fall-through) are handled outside
# this table because their call shapes differ.
_ADAPTER_ROUTES: tuple[_AdapterRoute, ...] | None = None


def _adapter_routes() -> tuple[_AdapterRoute, ...]:
    global _ADAPTER_ROUTES
    if _ADAPTER_ROUTES is None:
        _ADAPTER_ROUTES = (
            _AdapterRoute(
                "google_shopping",
                google_shopping.is_google_shopping_url,
                google_shopping.fetch_google_shopping,
            ),
            _AdapterRoute(
                "realestate", realestate.is_realestate_url, realestate.fetch_realestate
            ),
            _AdapterRoute("olx", olx.is_olx_url, olx.fetch_olx),
            _AdapterRoute(
                "mercadolivre",
                mercadolivre.is_mercadolivre_url,
                mercadolivre.fetch_mercadolivre,
            ),
            _AdapterRoute(
                "aliexpress", aliexpress.is_aliexpress_url, aliexpress.fetch_aliexpress
            ),
            _AdapterRoute("shopee", shopee.is_shopee_url, shopee.fetch_shopee),
            _AdapterRoute("steam", steam.is_steam_url, steam.fetch_steam),
        )
    return _ADAPTER_ROUTES


# ---------------------------------------------------------------------------
# Core fetch logic (single body, both entry points use it)
# ---------------------------------------------------------------------------


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

    # --- Content adapters (HTML via the shared escalation chain) ------------
    # Each parses provider HTML/JSON into its own envelope shape but shares the
    # tier chain + per-route strategy via the injected `fetch_html`.
    for route in _adapter_routes():
        if route.matches(url):
            fetch_html, state = _make_adapter_fetcher(
                url,
                normalized,
                mode=mode,
                deadline_monotonic=deadline_monotonic,
                cache=cache,
                cfg=cfg,
                phases=phases,
            )
            envelope = await route.fetch(
                url, deadline=deadline, cfg=cfg, fetch_html=fetch_html
            )
            envelope = _finalize_adapter_envelope(
                envelope,
                url=url,
                normalized=normalized,
                raw=raw,
                service=route.service,
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
