# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared plumbing for the content adapters.

Everything here used to be copy-pasted across the marketplace/real-estate
adapter modules (olx, mercadolivre, realestate, google_shopping, aliexpress,
shopify, shopee, steam — plus youtube/wikimedia for the envelope builders):
envelope delegators, the browser-fetch seam, the guarded HTML-acquisition
block, JSON-LD extraction, Brazilian money parsing, and small shaping helpers.

Two import styles, on purpose:

- Pure helpers are imported by name (``from ._common import soup as _soup``)
  so adapter call sites keep their established private-name style.
- The browser seam must be called module-qualified
  (``_common.browser_fetch_html`` / ``_common.browser_only_fetch``) so a
  monkeypatched binding on this module resolves for every adapter — the same
  single-seam convention as ``vasco.fetch.core``.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .. import envelope
from ..errors import FailureReason

if TYPE_CHECKING:  # bs4 imported lazily in soup() to keep module import cheap
    from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# HTML / URL helpers
# ---------------------------------------------------------------------------


def soup(html: str) -> BeautifulSoup:
    """Parse HTML; bs4 is imported lazily so importing an adapter (and the
    whole fetch stack) doesn't pull bs4 until a page is actually parsed."""
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser")


def host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def text(parsed: BeautifulSoup, selector: str) -> str | None:
    """Whitespace-normalized text of the first element matching `selector`."""
    el = parsed.select_one(selector)
    if not el:
        return None
    txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
    return txt or None


def jsonld_objects(html: str) -> list[dict[str, Any]]:
    """All schema.org objects in the page, flattening top-level lists and
    ``@graph`` wrappers. Order is preserved."""
    parsed = soup(html)
    out: list[dict[str, Any]] = []
    for tag in parsed.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except json.JSONDecodeError:
            continue
        for obj in data if isinstance(data, list) else [data]:
            if not isinstance(obj, dict):
                continue
            if isinstance(obj.get("@graph"), list):
                out.extend(o for o in obj["@graph"] if isinstance(o, dict))
            else:
                out.append(obj)
    return out


# ---------------------------------------------------------------------------
# Shaping helpers
# ---------------------------------------------------------------------------


def dedup(urls: Any, limit: int) -> list[str]:
    """First `limit` distinct non-empty strings, accepting a bare string too."""
    if isinstance(urls, str):
        urls = [urls]
    seen: set[str] = set()
    out: list[str] = []
    for u in urls or []:
        if not isinstance(u, str):
            continue
        s = u.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drop null / empty values so each record carries only what's known."""
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


# ---------------------------------------------------------------------------
# Number / money parsing (Brazilian formats)
# ---------------------------------------------------------------------------


def brl_to_num(text: Any) -> int | float | None:
    """Parse a Brazilian money string ("R$ 3.899" → 3899, "357,90" → 357.9,
    "3.185,31" → 3185.31) to a number, tolerating space-split separators
    ("R$ 1 . 544 , 99" → 1544.99). Returns None for non-prices."""
    s = re.sub(r"[^\d.,]", "", str(text))
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def num(value: Any) -> int | float | None:
    """Normalize a numeric price. JSON-LD gives a number (int or float); strings
    are parsed as Brazilian-format money. Whole floats collapse to int."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return int(f) if f.is_integer() else f
    if isinstance(value, str):
        return brl_to_num(value)
    return None


def as_int(value: Any) -> int | None:
    """First integer found in `value` ("32m²" → 32, "2 quartos" → 2, 90.0 → 90).

    Note: strips a thousands ``.`` so "1.200" → 1200; do NOT use for decimal
    fields like motorpower ("1.3") — keep those as strings.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.search(r"\d[\d.]*", str(value).replace(".", ""))
    return int(m.group()) if m else None


def as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def brl_int(value: Any) -> int | None:
    """Parse a BRL price ("R$ 1.278,00" / 1278 / "1.278") to int reais.

    Returns None for non-prices like "Sob consulta"."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value)
    s = re.sub(r"[^\d,]", "", s).split(",")[0]  # drop currency, cents
    return int(s) if s.isdigit() else None


def fmt_price_brl(price: Any, currency: str) -> str:
    """ "R$ 3.185,31"-style price label; "Sob consulta" when unknown."""
    if price is None:
        return "Sob consulta"
    if isinstance(price, float) and not price.is_integer():
        body = f"{price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    else:
        body = f"{int(price):,}".replace(",", ".")
    symbol = "R$" if currency in (None, "", "BRL") else currency
    return f"{symbol} {body}"


# ---------------------------------------------------------------------------
# schema.org field helpers
# ---------------------------------------------------------------------------


def brand_name(value: Any) -> str | None:
    """Brand is a plain string on some JSON-LD surfaces and a ``{"name": …}``
    object on others; accept both."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    return None


def condition(value: Any) -> str | None:
    """Map a schema.org ``itemCondition`` ("NewCondition" or a full URL) to
    new/used/refurbished."""
    if not isinstance(value, str):
        return None
    low = value.lower()
    if "new" in low:
        return "new"
    if "refurb" in low:
        return "refurbished"
    if "used" in low or "damaged" in low:
        return "used"
    return None


def rating(item: dict[str, Any]) -> tuple[float | None, int | None]:
    """``(ratingValue, ratingCount)`` from an aggregateRating block.

    Some sites render both as strings ("4.99" / "103"), so parse defensively
    rather than guarding on numeric JSON types.
    """
    agg = item.get("aggregateRating")
    if not isinstance(agg, dict):
        return None, None
    return as_float(agg.get("ratingValue")), as_int(agg.get("ratingCount"))


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------


def envelope_builders(
    mode_used: str, content_type: str
) -> tuple[
    Callable[..., dict[str, Any]],
    Callable[..., dict[str, Any]],
]:
    """The per-adapter ``(_base_envelope, _failure_envelope)`` pair.

    Every adapter envelope differs only in its ``mode_used``/``content_type``
    constants; the shape itself still lives in ``vasco/envelope.py``.
    """

    def base(url: str, *, http_status: int = 0) -> dict[str, Any]:
        return envelope.base_envelope(
            url_requested=url,
            url_normalized=url,
            url_final=url,
            http_status=http_status,
            mode_used=mode_used,
            content_type=content_type,
        )

    def failure(
        url: str, reason: FailureReason, message: str, *, http_status: int = 0
    ) -> dict[str, Any]:
        return envelope.failure_envelope(
            base=base(url, http_status=http_status),
            reason=reason,
            message=message,
        )

    return base, failure


# ---------------------------------------------------------------------------
# HTML acquisition (the browser seam + the guarded fetch every adapter shares)
# ---------------------------------------------------------------------------

# An injected HTML fetcher: returns (html, status, headers, reason, mode_used).
# The main flow passes one backed by the shared escalation chain
# (http → browser → mobile; adapters skip the wayback tail); see
# fetch._make_adapter_fetcher.
HtmlFetcher = Callable[
    [str], Awaitable[tuple[str, int, dict[str, str], FailureReason, str]]
]


def classify_browser_error(exc: BaseException) -> FailureReason:
    msg = str(exc).lower()
    if "timeout" in type(exc).__name__.lower() or "timeout" in msg:
        return FailureReason.TIMEOUT
    if any(
        m in msg for m in ("connection closed", "target closed", "net::err_aborted")
    ):
        return FailureReason.BLOCKED_BOT
    return FailureReason.SERVER_ERROR


async def browser_fetch_html(
    url: str, *, deadline_monotonic: float, cfg: Any | None
) -> tuple[str, int, dict[str, str]]:
    """Browser fetch seam, isolated so tests can monkeypatch it (call it
    module-qualified: ``_common.browser_fetch_html``)."""
    from ..fetch import browser

    pool = browser.get_browser(cfg)
    return await pool.fetch(url, deadline_monotonic=deadline_monotonic)


async def browser_only_fetch(
    url: str, *, deadline: float, cfg: Any | None
) -> tuple[str, int, dict[str, str], FailureReason, str]:
    """Standalone fallback when no escalating fetcher is injected.

    Used when an adapter is called directly (e.g. in tests); the production
    path injects the shared escalation chain instead.
    """
    deadline_monotonic = time.monotonic() + max(0.0, float(deadline))
    html, status, headers = await browser_fetch_html(
        url, deadline_monotonic=deadline_monotonic, cfg=cfg
    )
    return html, status, headers, FailureReason.OK, "browser"


async def fetch_with_fallback(
    url: str,
    *,
    fetch_html: HtmlFetcher | None,
    deadline: float,
    cfg: Any | None,
) -> tuple[str, int, dict[str, str], FailureReason, str]:
    """Fetch via the injected escalation chain, or browser-only without one."""
    if fetch_html is not None:
        return await fetch_html(url)
    return await browser_only_fetch(url, deadline=deadline, cfg=cfg)


async def acquire_html(
    url: str,
    *,
    fetch_html: HtmlFetcher | None,
    deadline: float,
    cfg: Any | None,
    fail: Callable[..., dict[str, Any]],
) -> tuple[str, int, str] | dict[str, Any]:
    """The guarded HTML acquisition every adapter shares.

    Returns ``(html, http_status, mode_used)`` on success, or a ready failure
    envelope built via `fail` (signature ``fail(reason, message, *,
    http_status=0)`` — pass ``partial(_failure_envelope, url)`` or a custom
    wrapper) on timeout, fetch error, non-OK tier reason, or empty body.
    """
    try:
        html_src, status, _headers, reason, mode_used = await fetch_with_fallback(
            url, fetch_html=fetch_html, deadline=deadline, cfg=cfg
        )
    except TimeoutError:
        return fail(FailureReason.TIMEOUT, "fetch deadline elapsed")
    except Exception as exc:
        return fail(
            classify_browser_error(exc),
            f"fetch failed: {type(exc).__name__}: {exc}",
        )

    if reason != FailureReason.OK:
        return fail(reason, f"fetch failed via {mode_used} tier", http_status=status)
    if not html_src:
        return fail(
            FailureReason.SERVER_ERROR,
            f"empty body from {mode_used} tier",
            http_status=status,
        )
    return html_src, status, mode_used
