"""MercadoLivre marketplace adapter (Brazil).

MercadoLivre is Brazil's dominant marketplace. Like the other structured-data
adapters, the useful payload — prices, ratings, seller, condition, attributes —
is rendered into JS-heavy pages that the default trafilatura pipeline flattens to
lossy prose. MercadoLivre exposes it cleanly via **schema.org JSON-LD**, which is
the robust spine here (it survives the constant CSS class rotation that makes
scraping ``poly-card`` markup brittle):

- **Search/listing pages** (``lista.mercadolivre.com.br/<q>``, ``/ofertas``,
  category pages) embed a ``<script type="application/ld+json">`` with an
  ``@graph`` of ``Product`` objects (name, image, brand, aggregateRating,
  offers.price/priceCurrency/url).
- **Product/detail pages** (``.../p/MLB<id>``, ``produto.mercadolivre.com.br/
  MLB-<id>-...``) embed a single rich ``Product`` (offers with shippingDetails,
  itemCondition, aggregateRating, brand, sku, color, description). A few
  display-only extras JSON-LD omits — seller, sold-count, installments,
  struck-through original price, the spec table — are lifted best-effort from the
  rendered ``ui-pdp-*`` / ``andes-*`` HTML (never fatal if the markup moves).

Public surface:
- ``is_mercadolivre_url(url)`` — match a mercadolivre.com.br URL.
- ``fetch_mercadolivre(url, *, deadline, cfg=None, fetch_html=None)`` — return a
  v0.1 envelope (``mode_used="mercadolivre"``,
  ``content_type="application/x-mercadolivre"``); never raises — returns a
  failure envelope on any fetch/parse failure.

MercadoLivre serves a bot-challenge shell on the plain http tier across every
surface, so ``vasco/strategy.py`` seeds ``mercadolivre.com.br`` to the **browser**
tier (like Google Shopping / OLX). HTML is still obtained through the shared
escalation chain via the injected ``fetch_html`` — the seed only picks the
*starting* tier; learning can still flip it.

Scope is Brazil only (``mercadolivre.com.br``); Spanish-country MercadoLibre
domains fall through to the normal fetch path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .. import envelope
from ..errors import AdapterParseError, FailureReason
from ..fetch import browser

log = logging.getLogger(__name__)

_GALLERY_CAP: int = 6
_MLB_RE = re.compile(r"MLB-?(\d+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# URL detection / routing
# ---------------------------------------------------------------------------


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_mercadolivre_url(url: str) -> bool:
    host = _host(url)
    return bool(url) and (
        host == "mercadolivre.com.br" or host.endswith(".mercadolivre.com.br")
    )


def _page_type(url: str) -> str:
    """Classify a URL as a 'search' (listing) or 'product' (detail) page.

    Product surfaces: the ``produto.`` host, the catalog ``/p/MLB<id>`` form, and
    bare item URLs ending in an ``MLB-<id>`` slug. Everything else (``lista.``,
    ``/ofertas``, category browse, homepage) is a search/listing page.
    """
    host = _host(url)
    path = (urlsplit(url).path or "/").lower()
    if host.startswith("produto."):
        return "product"
    if "/p/mlb" in path:
        return "product"
    if re.search(r"/mlb-?\d+", path):
        return "product"
    return "search"


# ---------------------------------------------------------------------------
# Normalized product + small parsing helpers
# ---------------------------------------------------------------------------


def _num(value: Any) -> int | float | None:
    """Normalize a numeric price. JSON-LD gives a number (int or float); strings
    are parsed as Brazilian-format money. Whole floats collapse to int."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return int(f) if f.is_integer() else f
    if isinstance(value, str):
        return _brl_to_num(value)
    return None


def _brl_to_num(text: Any) -> int | float | None:
    """Parse a Brazilian money string ("R$ 3.899" → 3899, "357,90" → 357.9,
    "3.185,31" → 3185.31) to a number. Returns None for non-prices."""
    s = re.sub(r"[^\d.,]", "", str(text))
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def _product_id(*candidates: Any) -> str | None:
    """First ``MLB<id>`` found across the candidate strings (url, sku, …)."""
    for c in candidates:
        if not isinstance(c, str):
            continue
        m = _MLB_RE.search(c)
        if m:
            return f"MLB{m.group(1)}"
    return None


def _brand_name(value: Any) -> str | None:
    """Brand is a plain string on PDP JSON-LD but a ``{"name": …}`` object on
    search JSON-LD."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    return None


def _condition(value: Any) -> str | None:
    """Map a schema.org ``itemCondition`` URL to new/used/refurbished."""
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


def _rating(item: dict[str, Any]) -> tuple[float | None, int | None]:
    """(ratingValue, ratingCount) from an aggregateRating block."""
    agg = item.get("aggregateRating")
    if not isinstance(agg, dict):
        return None, None
    try:
        value = (
            float(agg["ratingValue"]) if agg.get("ratingValue") is not None else None
        )
    except (TypeError, ValueError):
        value = None
    count = agg.get("ratingCount")
    count = int(count) if isinstance(count, (int, float)) else None
    return value, count


def _dedup(urls: Any, limit: int = _GALLERY_CAP) -> list[str]:
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


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drop null / empty values so each product carries only what's known."""
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _jsonld_products(html: str) -> list[dict[str, Any]]:
    """All schema.org ``Product`` objects in the page, flattening ``@graph``.

    Search pages wrap many Products in one ``@graph``; product pages have a
    single top-level (or listed) Product. Order is preserved.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except json.JSONDecodeError:
            continue
        candidates: list[Any] = []
        for obj in data if isinstance(data, list) else [data]:
            if not isinstance(obj, dict):
                continue
            if isinstance(obj.get("@graph"), list):
                candidates.extend(obj["@graph"])
            else:
                candidates.append(obj)
        out.extend(
            c for c in candidates if isinstance(c, dict) and c.get("@type") == "Product"
        )
    return out


# ---------------------------------------------------------------------------
# Search (JSON-LD @graph)
# ---------------------------------------------------------------------------


def _search_product(item: dict[str, Any], position: int) -> dict[str, Any] | None:
    offers = item.get("offers") if isinstance(item.get("offers"), dict) else {}
    name = item.get("name")
    url = offers.get("url")
    if not (name and url):
        return None
    rating, review_count = _rating(item)
    availability = offers.get("availability")
    return _compact(
        {
            "position": position,
            "title": name.strip() if isinstance(name, str) else None,
            "url": url,
            "product_id": _product_id(url, item.get("sku"), item.get("productID")),
            "price": _num(offers.get("price")),
            "currency": offers.get("priceCurrency") or None,
            "brand": _brand_name(item.get("brand")),
            "rating": rating,
            "review_count": review_count,
            "image": item.get("image") if isinstance(item.get("image"), str) else None,
            "in_stock": ("instock" in availability.lower())
            if isinstance(availability, str)
            else None,
        }
    )


def _parse_search(html: str) -> list[dict[str, Any]]:
    items = _jsonld_products(html)
    if not items:
        raise AdapterParseError(
            "search page: no schema.org Product JSON-LD found — site structure "
            "may have changed"
        )
    out: list[dict[str, Any]] = []
    position = 0
    for item in items:
        position += 1
        parsed = _search_product(item, position)
        if parsed is not None:
            out.append(parsed)
    return out


# ---------------------------------------------------------------------------
# Product detail (JSON-LD spine + best-effort PDP HTML extras)
# ---------------------------------------------------------------------------


def _text(soup: BeautifulSoup, selector: str) -> str | None:
    el = soup.select_one(selector)
    if not el:
        return None
    txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
    return txt or None


def _sold_quantity(subtitle: str | None) -> int | None:
    """Parse ML's "+5 mil vendidos" / "+100 vendidos" → 5000 / 100."""
    if not subtitle:
        return None
    m = re.search(r"([\d.,]+)\s*(mil|mi)?\s*vendido", subtitle, re.IGNORECASE)
    if not m:
        return None
    base = _brl_to_num(m.group(1))
    if base is None:
        return None
    factor = {"mil": 1000, "mi": 1_000_000}.get((m.group(2) or "").lower(), 1)
    return int(base * factor)


def _spec_attributes(soup: BeautifulSoup) -> dict[str, str]:
    """Lift the PDP technical-specs table (``andes-table`` rows) into a dict."""
    out: dict[str, str] = {}
    for row in soup.select(".andes-table__row"):
        header = row.select_one(".andes-table__header__container") or row.find("th")
        value = row.select_one(".andes-table__column") or row.find("td")
        if not (header and value):
            continue
        key = header.get_text(" ", strip=True)
        val = value.get_text(" ", strip=True)
        if key and val and key not in out:
            out[key] = val
    return out


def _pdp_extras(html: str) -> dict[str, Any]:
    """Best-effort display fields the PDP JSON-LD omits. Resilient by design:
    any missing/moved selector simply yields nothing rather than failing."""
    soup = BeautifulSoup(html, "html.parser")
    extras: dict[str, Any] = {}

    subtitle = _text(soup, ".ui-pdp-subtitle")
    sold = _sold_quantity(subtitle)
    if sold is not None:
        extras["sold_quantity"] = sold

    installments = _text(soup, ".ui-pdp-price__subtitles")
    if installments:
        extras["installments"] = installments

    seller = _text(soup, "[class*=seller__header__title]")
    if seller:
        extras["seller"] = seller

    # Struck-through previous price inside the main price block only (avoids
    # picking up prices from related-product carousels elsewhere on the page).
    prev = soup.select_one(".ui-pdp-price s .andes-money-amount__fraction")
    original = _brl_to_num(prev.get_text(strip=True)) if prev else None
    if original is not None:
        extras["original_price"] = original

    attributes = _spec_attributes(soup)
    if attributes:
        extras["attributes"] = attributes

    return extras


def _free_shipping(offers: dict[str, Any]) -> bool | None:
    shipping = offers.get("shippingDetails")
    if isinstance(shipping, list):
        shipping = shipping[0] if shipping else None
    if not isinstance(shipping, dict):
        return None
    rate = shipping.get("shippingRate")
    if not isinstance(rate, dict) or rate.get("value") is None:
        return None
    try:
        return float(rate["value"]) == 0.0
    except (TypeError, ValueError):
        return None


def _parse_product(html: str, url: str) -> list[dict[str, Any]]:
    products = _jsonld_products(html)
    if not products:
        raise AdapterParseError(
            "product page: no schema.org Product JSON-LD found — site structure "
            "may have changed"
        )
    item = products[0]
    offers = item.get("offers") if isinstance(item.get("offers"), dict) else {}
    rating, review_count = _rating(item)
    agg = item.get("aggregateRating") or {}
    review_count = (
        agg.get("reviewCount")
        if isinstance(agg, dict) and isinstance(agg.get("reviewCount"), (int, float))
        else review_count
    )
    availability = offers.get("availability")
    final_url = offers.get("url") or url

    product = {
        "title": (item.get("name") or "").strip() or None,
        "url": final_url,
        "product_id": _product_id(item.get("sku"), item.get("productID"), final_url),
        "price": _num(offers.get("price")),
        "currency": offers.get("priceCurrency") or None,
        "condition": _condition(item.get("itemCondition")),
        "brand": _brand_name(item.get("brand")),
        "color": item.get("color") if isinstance(item.get("color"), str) else None,
        "rating": rating,
        "review_count": int(review_count)
        if isinstance(review_count, (int, float))
        else None,
        "free_shipping": _free_shipping(offers),
        "in_stock": ("instock" in availability.lower())
        if isinstance(availability, str)
        else None,
        "description": (item.get("description") or "").strip() or None,
        "images": _dedup(item.get("image")),
    }

    # Best-effort HTML extras (seller, sold count, installments, original price,
    # spec table). Never fatal — a markup change just drops the extra fields.
    try:
        extras = _pdp_extras(html)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("mercadolivre PDP extras failed: %s", exc)
        extras = {}
    # Prefer JSON-LD condition; fall back to nothing (subtitle label is noisy).
    product.update({k: v for k, v in extras.items() if v not in (None, "", [], {})})

    compact = _compact(product)
    if compact.get("images") and not compact.get("image"):
        compact["image"] = compact["images"][0]
    return [compact]


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _fmt_price(price: Any, currency: str) -> str:
    if price is None:
        return "Sob consulta"
    if isinstance(price, float) and not price.is_integer():
        body = f"{price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    else:
        body = f"{int(price):,}".replace(",", ".")
    symbol = "R$" if currency in (None, "", "BRL") else currency
    return f"{symbol} {body}"


def _render_markdown(
    products: list[dict[str, Any]], *, page_type: str, currency: str
) -> str:
    if not products:
        return "# MercadoLivre\n\nNenhum produto encontrado."
    parts: list[str] = []
    if page_type == "search":
        parts.append(f"{len(products)} produtos")
        parts.append("")
    for i, p in enumerate(products, 1):
        head = f"{i}. **{p.get('title', '?')}** — {_fmt_price(p.get('price'), p.get('currency') or currency)}"
        extras: list[str] = []
        if p.get("original_price"):
            extras.append(
                f"de {_fmt_price(p['original_price'], p.get('currency') or currency)}"
            )
        if p.get("rating") is not None:
            rb = f"{p['rating']}/5"
            if p.get("review_count"):
                rb += f" ({p['review_count']})"
            extras.append(rb)
        if p.get("brand"):
            extras.append(p["brand"])
        if p.get("seller"):
            extras.append(p["seller"])
        if p.get("sold_quantity"):
            extras.append(f"{p['sold_quantity']} vendidos")
        if p.get("free_shipping"):
            extras.append("frete grátis")
        if extras:
            head += " — " + " · ".join(extras)
        parts.append(head)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Fetch + envelope
# ---------------------------------------------------------------------------


def _base_envelope(url: str, *, http_status: int = 0) -> dict[str, Any]:
    return envelope.base_envelope(
        url_requested=url,
        url_normalized=url,
        url_final=url,
        http_status=http_status,
        mode_used="mercadolivre",
        content_type="application/x-mercadolivre",
    )


def _failure_envelope(
    url: str, reason: FailureReason, message: str, *, http_status: int = 0
) -> dict[str, Any]:
    return envelope.failure_envelope(
        base=_base_envelope(url, http_status=http_status),
        reason=reason,
        message=message,
    )


def _classify_browser_error(exc: BaseException) -> FailureReason:
    msg = str(exc).lower()
    if "timeout" in type(exc).__name__.lower() or "timeout" in msg:
        return FailureReason.TIMEOUT
    if any(
        m in msg for m in ("connection closed", "target closed", "net::err_aborted")
    ):
        return FailureReason.BLOCKED_BOT
    return FailureReason.SERVER_ERROR


async def _browser_fetch_html(
    url: str, *, deadline_monotonic: float, cfg: Any | None
) -> tuple[str, int, dict[str, str]]:
    """Browser fetch helper, isolated so tests can monkeypatch it."""
    pool = browser.get_browser(cfg)
    return await pool.fetch(url, deadline_monotonic=deadline_monotonic)


# An injected HTML fetcher: returns (html, status, headers, reason, mode_used).
# The main flow passes one backed by the shared escalation chain
# (http → browser → mobile → wayback); see fetch._make_adapter_fetcher.
HtmlFetcher = Callable[
    [str], Awaitable[tuple[str, int, dict[str, str], FailureReason, str]]
]


async def _browser_only_fetch(
    url: str, *, deadline: float, cfg: Any | None
) -> tuple[str, int, dict[str, str], FailureReason, str]:
    """Standalone fallback when no escalating fetcher is injected (direct use)."""
    deadline_monotonic = time.monotonic() + max(0.0, float(deadline))
    html, status, headers = await _browser_fetch_html(
        url, deadline_monotonic=deadline_monotonic, cfg=cfg
    )
    return html, status, headers, FailureReason.OK, "browser"


async def fetch_mercadolivre(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
    fetch_html: HtmlFetcher | None = None,
) -> dict[str, Any]:
    """Fetch a MercadoLivre search/product page and return a structured envelope.

    HTML is obtained via ``fetch_html`` — the main flow injects the shared
    ``http → browser → mobile → wayback`` escalation chain (MercadoLivre is seeded
    to the browser tier in ``vasco/strategy.py``). Without an injected fetcher it
    falls back to a browser-only fetch.
    """
    page_type = _page_type(url)

    async def _fetch(target: str):
        if fetch_html is not None:
            return await fetch_html(target)
        return await _browser_only_fetch(target, deadline=deadline, cfg=cfg)

    try:
        html_src, status, _headers, reason, mode_used = await _fetch(url)
    except asyncio.TimeoutError:
        return _failure_envelope(url, FailureReason.TIMEOUT, "fetch deadline elapsed")
    except Exception as exc:
        return _failure_envelope(
            url,
            _classify_browser_error(exc),
            f"fetch failed: {type(exc).__name__}: {exc}",
        )

    if reason != FailureReason.OK:
        return _failure_envelope(
            url, reason, f"fetch failed via {mode_used} tier", http_status=status
        )
    if not html_src:
        return _failure_envelope(
            url,
            FailureReason.SERVER_ERROR,
            f"empty body from {mode_used} tier",
            http_status=status,
        )

    try:
        if page_type == "product":
            products = _parse_product(html_src, url)
        else:
            products = _parse_search(html_src)
    except AdapterParseError as exc:
        log.warning("mercadolivre parse anchor missing (%s): %s", page_type, exc)
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, f"mercadolivre {exc}", http_status=status
        )
    except Exception as exc:
        log.warning("mercadolivre parse failed (%s): %s", page_type, exc)
        return _failure_envelope(
            url,
            FailureReason.PARSE_FAILED,
            f"mercadolivre parse failed: {type(exc).__name__}: {exc}",
            http_status=status,
        )

    from .. import io as io_mod

    # Anchor present but zero parsed products on a search page: a genuinely
    # empty result set (the rot case raised above). Flag it for agents.
    warnings = ["no_results"] if page_type == "search" and not products else []

    currency = next(
        (p["currency"] for p in products if p.get("currency")),
        getattr(getattr(cfg, "shopping", None), "currency", None) or "BRL",
    )
    language = getattr(getattr(cfg, "shopping", None), "language", None) or "pt-BR"
    markdown = _render_markdown(products, page_type=page_type, currency=currency)
    title = (
        products[0].get("title")
        if page_type == "product" and products
        else f"MercadoLivre: {len(products)} produtos"
    )
    return envelope.success_envelope(
        base=_base_envelope(url, http_status=status or 200),
        markdown=markdown,
        metadata={
            "title": title,
            "byline": None,
            "published": None,
            "modified": None,
            "language": language,
            "site_name": "MercadoLivre",
            "image": products[0].get("image") if products else None,
            "word_count": len(markdown.split()),
            "quality": {
                "provider": "mercadolivre",
                "page_type": page_type,
                "currency": currency,
                "result_count": len(products),
                "products": products,
            },
            "warnings": warnings,
        },
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )
