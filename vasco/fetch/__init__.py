# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

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

import contextlib

from vasco import io as io_mod
from vasco import quality as quality_mod
from vasco.adapters import (
    aliexpress,
    amazon,
    gitlab,
    google_shopping,
    mercadolivre,
    olx,
    petlove,
    phabricator,
    realestate,
    scholar,
    shopee,
    shopify,
    steam,
    wikimedia,
    youtube,
)
from vasco.config import QualityCfg
from vasco.converters import convert
from vasco.envelope import (
    base_envelope as _base_envelope,
)
from vasco.envelope import (
    failure_envelope as _failure_envelope,
)
from vasco.envelope import (
    success_envelope as _success_envelope,
)
from vasco.errors import FailureReason

from . import browser
from .caching import (
    _cache_put,
    _finalize_adapter_envelope,
    _hydrate_cache_hit,
    _ttl_for,
)
from .core import _do_fetch_html

# Re-exported so tests/adapters resolve them as `vasco.fetch.<name>`: the chain's
# monkeypatch seam lives in `core`, but `_http_fetch` is also called directly in
# unit tests, and the encoding/budget helpers are read straight off the package.
from .core import _http_fetch as _http_fetch
from .documents import _fetch_pandoc_doc, _fetch_pdf
from .phases import _convert_html, _ms_since, _Phases, _stamp_phases
from .urlutils import (
    _ACCEPT_ENCODING as _ACCEPT_ENCODING,
)
from .urlutils import (
    BROWSER_MIN_BUDGET as BROWSER_MIN_BUDGET,
)
from .urlutils import (
    _content_length,
    _content_type,
    _is_pdf,
    _normalize_url,
    _pandoc_format,
    _parse_retry_after,
)
from .urlutils import (
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
            _AdapterRoute("petlove", petlove.is_petlove_url, petlove.fetch_petlove),
            _AdapterRoute("amazon", amazon.is_amazon_url, amazon.fetch_amazon),
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

    Pipeline: invalid URL → cache hit → adapter dispatch (youtube/wikimedia
    shortcuts, the content-adapter registry, the shopify probe) → binary
    document shortcut (pdf/pandoc) → HTML auto-mode escalation.

    `phases` is None on short-circuit paths (invalid URL, cache hit) where the
    phase breakdown doesn't apply — only `duration_ms` is stamped.
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

    if use_cache and not refresh and cache is not None:
        try:
            hit = cache.get(normalized)
        except Exception:
            hit = None
        if hit is not None:
            return _hydrate_cache_hit(hit, url_requested=url), False, None

    deadline_monotonic = time.monotonic() + max(0.0, float(deadline))
    phases = _Phases()

    def store(envelope: dict[str, Any]) -> dict[str, Any]:
        if use_cache and cache is not None:
            _cache_put(cache, envelope, phases, ttl_seconds=_ttl_for(envelope, cfg))
        return envelope

    adapter_envelope, probe_browser = await _dispatch_adapters(
        url,
        normalized,
        mode=mode,
        deadline=deadline,
        deadline_monotonic=deadline_monotonic,
        use_cache=use_cache,
        raw=raw,
        cache=cache,
        cfg=cfg,
        phases=phases,
    )
    if adapter_envelope is not None:
        return adapter_envelope, probe_browser, phases

    base = _base_envelope(
        url_requested=url,
        url_normalized=normalized,
        url_final=None,
        http_status=0,
        mode_used="http",
        content_type="text/html",
    )

    document = await _try_document(
        url,
        None,
        base=base,
        deadline_monotonic=deadline_monotonic,
        cfg=cfg,
        phases=phases,
        store=store,
    )
    if document is not None:
        return document, False, phases

    envelope, browser_started = await _fetch_html_envelope(
        url,
        base=base,
        mode=mode,
        deadline_monotonic=deadline_monotonic,
        raw=raw,
        cache=cache,
        cfg=cfg,
        phases=phases,
        store=store,
        browser_started=probe_browser,
    )
    return envelope, browser_started, phases


async def _dispatch_adapters(
    url: str,
    normalized: str,
    *,
    mode: str,
    deadline: float,
    deadline_monotonic: float,
    use_cache: bool,
    raw: bool,
    cache: Any | None,
    cfg: Any | None,
    phases: _Phases,
) -> tuple[dict[str, Any] | None, bool]:
    """Route the URL to a content adapter, if one claims it.

    Returns ``(envelope, browser_started)``; ``(None, started)`` means no
    adapter claimed the URL and the normal fetch path should run (a Shopify
    probe miss still reports its browser usage so the pool gets closed).
    """

    def finalize(envelope: dict[str, Any], service: str) -> dict[str, Any]:
        return _finalize_adapter_envelope(
            envelope,
            url=url,
            normalized=normalized,
            raw=raw,
            service=service,
            use_cache=use_cache,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )

    def make_fetcher() -> tuple[Any, dict[str, bool]]:
        return _make_adapter_fetcher(
            url,
            normalized,
            mode=mode,
            deadline_monotonic=deadline_monotonic,
            cache=cache,
            cfg=cfg,
            phases=phases,
        )

    # Self-fetching shortcuts: own envelope shape, no HTTP/browser tier at all.
    # (Wikimedia is Enterprise-API only; without creds it's a normal HTTP page.)
    if youtube.is_youtube_url(url):
        envelope = await youtube.fetch_youtube(url, deadline=deadline, cfg=cfg)
        return finalize(envelope, "youtube"), False
    if wikimedia.is_wikimedia_url(url) and wikimedia.has_credentials(cfg):
        envelope = await wikimedia.fetch_wikimedia(url, deadline=deadline, cfg=cfg)
        return finalize(envelope, "wikimedia"), False

    # Scholar (scientific articles via open scholarly APIs): a closed, deterministic
    # host set (doi.org / sciencedirect PII / pubmed / arxiv / europepmc), so no
    # probe. Owns its own minimal-header httpx client (the metadata APIs are plain
    # JSON GETs), so it takes no `fetch_html` and never touches the browser pool.
    if scholar.is_scholar_url(url):
        envelope = await scholar.fetch_scholar(url, deadline=deadline, cfg=cfg)
        return finalize(envelope, "scholar"), False

    # Content adapters: each parses provider HTML/JSON into its own envelope
    # shape but shares the tier chain + per-route strategy via the injected
    # `fetch_html`.
    for route in _adapter_routes():
        if route.matches(url):
            fetch_html, state = make_fetcher()
            envelope = await route.fetch(
                url, deadline=deadline, cfg=cfg, fetch_html=fetch_html
            )
            return finalize(envelope, route.service), state["browser_started"]

    # Phabricator (task pages + task search): handled outside the route table
    # because matching its known-host set (built-in ∪ cfg.adapters.phabricator.domains)
    # needs cfg, which the url-only `_adapter_routes()` predicates don't carry.
    if phabricator.is_phabricator_url(url, cfg):
        fetch_html, state = make_fetcher()
        envelope = await phabricator.fetch_phabricator(
            url, deadline=deadline, cfg=cfg, fetch_html=fetch_html
        )
        return finalize(envelope, "phabricator"), state["browser_started"]

    # Shopify (product/collection/search via platform JSON endpoints): known
    # domains dispatch directly; unknown product/collection URLs are probed and
    # fall through to a normal fetch on a miss (NotShopify), so a non-Shopify
    # lookalike is never turned into a failure.
    is_known_shopify = shopify.is_shopify_url(url, cfg, cache)
    if is_known_shopify or shopify.is_shopify_candidate(url, cfg, cache):
        fetch_html, state = make_fetcher()
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
            return None, state["browser_started"]
        return finalize(envelope, "shopify"), state["browser_started"]

    # GitLab (projects + issues + merge requests via the public /api/v4 JSON):
    # known hosts (gitlab.com ∪ cfg.adapters.gitlab.domains) dispatch directly;
    # an unknown host on a claimable URL is probed and falls through on a miss
    # (NotGitLab). Placed *after* shopify: a bare-project shape (`/a/b`) overlaps
    # shopify's `/products/x` candidate, and a probe miss returns here (exiting
    # dispatch), so shopify must get first refusal on its own candidates. GitLab
    # owns a minimal-header httpx client (the chain's headers 403 on self-hosted
    # WAFs), so it takes no `fetch_html` and never touches the browser pool.
    is_known_gitlab = gitlab.is_gitlab_url(url, cfg, cache)
    if is_known_gitlab or gitlab.is_gitlab_candidate(url, cfg, cache):
        try:
            envelope = await gitlab.fetch_gitlab(
                url, deadline=deadline, cfg=cfg, cache=cache, probe=not is_known_gitlab
            )
        except gitlab.NotGitLab:
            return None, False
        return finalize(envelope, "gitlab"), False

    return None, False


async def _try_document(
    target: str,
    headers: dict[str, str] | None,
    *,
    base: dict[str, Any],
    deadline_monotonic: float,
    cfg: Any | None,
    phases: _Phases,
    store: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any] | None:
    """Fetch `target` as a binary document (PDF / pandoc format) if it is one.

    Used twice: on the raw URL before the HTML chain runs, and on `url_final` +
    response headers afterwards (a server may serve a document behind a
    redirect). Returns the stored envelope, or None when `target` is not a
    recognized document.
    """
    if _is_pdf(target, headers):
        base["mode_used"] = "pdf"
        return store(
            await _fetch_pdf(
                target,
                base=base,
                deadline_monotonic=deadline_monotonic,
                cfg=cfg,
                phases=phases,
            )
        )
    pandoc_fmt = _pandoc_format(target, headers)
    if pandoc_fmt is not None:
        base["mode_used"] = "pandoc"
        return store(
            await _fetch_pandoc_doc(
                target,
                fmt=pandoc_fmt,
                base=base,
                deadline_monotonic=deadline_monotonic,
                cfg=cfg,
                phases=phases,
            )
        )
    return None


async def _fetch_html_envelope(
    url: str,
    *,
    base: dict[str, Any],
    mode: str,
    deadline_monotonic: float,
    raw: bool,
    cache: Any | None,
    cfg: Any | None,
    phases: _Phases,
    store: Callable[[dict[str, Any]], dict[str, Any]],
    browser_started: bool,
) -> tuple[dict[str, Any], bool]:
    """Run the HTML auto-mode escalation chain and assemble the envelope.

    Returns ``(envelope, browser_started)``. Never raises — the safety net
    turns an unhandled error into a SERVER_ERROR failure envelope.
    """
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
        headers = outcome.headers
        mode_used = outcome.mode_used
        browser_started = outcome.browser_started

        base["http_status"] = int(outcome.status or 0)
        base["mode_used"] = mode_used
        base["content_type"] = _content_type(headers, "text/html")
        url_final = headers.get("_url_final") if isinstance(headers, dict) else None
        base["url_final"] = url_final or url

        # The server may have served a binary document behind a redirect.
        document = await _try_document(
            base["url_final"],
            headers,
            base=base,
            deadline_monotonic=deadline_monotonic,
            cfg=cfg,
            phases=phases,
            store=store,
        )
        if document is not None:
            return document, browser_started

        if outcome.reason != FailureReason.OK:
            if outcome.reason == FailureReason.UNSUPPORTED_CONTENT_TYPE:
                # Produced by the http tier for a binary blob (see core). Surface
                # the type + size as metadata and let the client decide what to do.
                # Skip the partial-markdown conversion below — the body is binary,
                # so trafilatura would only mojibake it.
                size = _content_length(headers)
                size_part = f", {size} bytes" if size else ""
                message = (
                    f"binary content ({base['content_type']}{size_part}) is not "
                    "text-extractable — vasco returns text (HTML / PDF / office "
                    "docs / plain text), not images, audio, video, or archives."
                )
            else:
                message = f"{outcome.reason} after {mode_used} tier"
            envelope = _failure_envelope(
                base=base,
                reason=outcome.reason,
                message=message,
                retry_after=_parse_retry_after(headers),
                partial_html=html if raw else None,
            )
            if (
                not raw
                and html
                and outcome.reason != FailureReason.UNSUPPORTED_CONTENT_TYPE
            ):
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
            return store(envelope), browser_started

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
                    "quality": {},
                    "warnings": ["raw"],
                },
                token_count_estimate=io_mod.estimate_tokens(html or ""),
            )
        else:
            envelope = _converted_success(
                outcome, html, base=base, cfg=cfg, phases=phases
            )
        return store(envelope), browser_started
    except Exception as exc:
        # Last-resort safety net: never raise out of fetch.
        envelope = _failure_envelope(
            base=base,
            reason=FailureReason.SERVER_ERROR,
            message=f"unhandled fetch error: {type(exc).__name__}: {exc}",
        )
        return store(envelope), browser_started


def _converted_success(
    outcome: Any,
    html: str,
    *,
    base: dict[str, Any],
    cfg: Any | None,
    phases: _Phases,
) -> dict[str, Any]:
    """Markdown-converted success envelope (or the EMPTY_BODY failure).

    Reuses the http-tier conversion when the chain already did it (the
    word_count escalation needs it, so a kept http result rides back on the
    outcome) — otherwise converts the winning tier's html once here.
    """
    if outcome.meta is not None:
        markdown, meta = outcome.markdown or "", outcome.meta
    else:
        markdown, meta = _convert_html(html, base["url_final"], phases)
    # No extractable text after the auto chain exhausted its content tiers: an
    # unrendered shell the browser tier couldn't fill either. Surface a clean
    # fetch-level failure (PARSE_FAILED-style, produced post-conversion)
    # instead of caching an empty "success".
    if meta.get("word_count", 0) == 0:
        return _failure_envelope(
            base=base,
            reason=FailureReason.EMPTY_BODY,
            message=(
                f"200 OK from {base['mode_used']} tier but no readable text was "
                "extracted — likely a JavaScript-only page the browser "
                "tier could not render (or the browser server was down)."
            ),
        )
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
    return _success_envelope(
        base=base,
        markdown=markdown,
        metadata=meta,
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )


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
        with contextlib.suppress(Exception):
            await browser.get_browser(cfg).close()
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
            with contextlib.suppress(Exception):
                await browser.get_browser(cfg).close()
