"""Shopee marketplace adapter (Brazil).

Shopee is one of Brazil's largest marketplaces, and like the other
structured-data adapters its useful payload — price, rating, seller, condition,
brand — lives in JS-heavy pages the default trafilatura pipeline flattens to
lossy prose. Shopee, however, embeds a clean **schema.org JSON-LD** ``Product``
block server-side for SEO, and that is the robust spine this adapter parses (it
survives Shopee's obfuscated/rotating CSS classes):

- **Product/detail pages** (``shopee.com.br/<slug>-i.<shopId>.<itemId>``) embed a
  ``<script type="application/ld+json">`` ``Product`` (name, image, brand,
  productID, description, ``offers`` with price/priceCurrency/itemCondition/
  availability and a nested ``seller`` Organization carrying the shop's
  aggregateRating, plus a product-level aggregateRating) **and** a
  ``BreadcrumbList`` from which the category path is recovered. The canonical
  ``shopId``/``itemId`` come straight from the ``-i.<shopId>.<itemId>`` URL tail.

Scope is **product pages only**. Shopee's search/category pages are pure
client-side SPAs whose results load via an anti-bot-signed internal API
(``/api/v4/search/search_items`` rejects an unsigned request with ``error
90309999`` even from a logged-in browser), so there is no embeddable structured
data to parse — those URLs are deliberately **not** matched by ``is_shopee_url``
and fall through to the normal fetch path.

Public surface mirrors the other adapters:
- ``is_shopee_url(url)`` — match a Shopee BR *product* URL.
- ``fetch_shopee(url, *, deadline, cfg=None, fetch_html=None)`` — return a v0.1
  envelope (``mode_used="shopee"``, ``content_type="application/x-shopee"``);
  never raises — returns a failure envelope on any fetch/parse failure.

Shopee serves a bot-challenge shell on the plain http tier, so
``vasco/strategy.py`` seeds ``shopee.com.br`` to the **browser** tier (like the
other marketplaces). HTML is still obtained through the shared escalation chain
via the injected ``fetch_html`` — the seed only picks the *starting* tier;
learning can still flip it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .. import envelope
from ..errors import AdapterParseError, FailureReason
from ..fetch import browser

if TYPE_CHECKING:  # bs4 imported lazily in _soup() to keep module import cheap
    from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


def _soup(html: str) -> BeautifulSoup:
    """Parse HTML; bs4 is imported lazily so importing this adapter (and the
    whole fetch stack) doesn't pull bs4 until a page is actually parsed."""
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser")


# Canonical Shopee product URL tail: ``...-i.<shopId>.<itemId>`` (both are long
# digit runs). The slug before it is free-form; the query (``?extraParams=...``)
# is ignored.
_ITEM_RE = re.compile(r"-i\.(\d+)\.(\d+)")


# ---------------------------------------------------------------------------
# URL detection / routing
# ---------------------------------------------------------------------------


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _is_br_host(url: str) -> bool:
    host = _host(url)
    return host == "shopee.com.br" or host.endswith(".shopee.com.br")


def _product_ids(url: str) -> tuple[str, str] | None:
    """``(shop_id, item_id)`` from a ``-i.<shopId>.<itemId>`` product URL, else None."""
    m = _ITEM_RE.search(urlsplit(url).path or "")
    return (m.group(1), m.group(2)) if m else None


def is_shopee_url(url: str) -> bool:
    """Match a Shopee BR *product* URL only.

    Search/category/home URLs carry no embeddable structured data, so leaving
    them unmatched lets them fall through to the normal fetch path instead of
    becoming an adapter failure.
    """
    return bool(url) and _is_br_host(url) and _product_ids(url) is not None


# ---------------------------------------------------------------------------
# Small parsing helpers (numbers, money, dicts)
# ---------------------------------------------------------------------------


def _price(value: Any) -> int | float | None:
    """Parse a schema.org ``offers.price``.

    Shopee's JSON-LD prices are en-format decimal strings ("557.46") or numbers,
    *not* Brazilian comma-decimals — so the separator is a real decimal point.
    Strip any thousands commas/currency, then float; whole floats collapse to int.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    else:
        s = re.sub(r"[^\d.]", "", str(value))
        if not s or s == ".":
            return None
        try:
            f = float(s)
        except ValueError:
            return None
    return int(f) if f.is_integer() else f


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    f = _as_float(value)
    return int(f) if f is not None else None


def _rating(item: dict[str, Any]) -> tuple[float | None, int | None]:
    """``(ratingValue, ratingCount)`` from an aggregateRating block.

    Shopee renders both as strings ("4.99" / "103"), so parse defensively rather
    than the ``isinstance(..., (int, float))`` guard the numeric-JSON adapters use.
    """
    agg = item.get("aggregateRating")
    if not isinstance(agg, dict):
        return None, None
    return _as_float(agg.get("ratingValue")), _as_int(agg.get("ratingCount"))


def _condition(value: Any) -> str | None:
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


def _in_stock(value: Any) -> bool | None:
    return ("instock" in value.lower()) if isinstance(value, str) else None


def _brand_name(value: Any) -> str | None:
    """Brand is a plain string on Shopee's JSON-LD, but tolerate the
    ``{"name": ...}`` object form schema.org also allows."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    return None


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drop null / empty values so each record carries only what's known."""
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


# ---------------------------------------------------------------------------
# JSON-LD extraction (Product spine + BreadcrumbList)
# ---------------------------------------------------------------------------


def _jsonld_objects(html: str) -> list[dict[str, Any]]:
    """All schema.org objects in the page, flattening top-level lists and
    ``@graph`` wrappers. Order is preserved."""
    soup = _soup(html)
    out: list[dict[str, Any]] = []
    for tag in soup.find_all("script", type="application/ld+json"):
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


def _find(objects: list[dict[str, Any]], typ: str) -> dict[str, Any] | None:
    return next((o for o in objects if o.get("@type") == typ), None)


def _category_path(breadcrumb: dict[str, Any] | None) -> str | None:
    """Recover the category path from a ``BreadcrumbList``.

    The crumb runs ``Shopee > Cat > Subcat > <product name>``; drop the leading
    "Shopee" home crumb and the trailing product crumb, leaving the category
    chain joined with " > " (e.g. "Celulares e Dispositivos > Tablets")."""
    if not breadcrumb:
        return None
    names: list[str] = []
    for el in breadcrumb.get("itemListElement") or []:
        if not isinstance(el, dict):
            continue
        item = el.get("item")
        name = item.get("name") if isinstance(item, dict) else el.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    inner = names[1:-1]  # drop home ("Shopee") + the product itself
    return " > ".join(inner) or None


def _seller(offers: dict[str, Any]) -> dict[str, Any] | None:
    """Lift the nested ``offers.seller`` Organization (name/url + the shop's own
    aggregateRating) into a compact dict, or None when absent."""
    org = offers.get("seller")
    if not isinstance(org, dict):
        return None
    rating, rating_count = _rating(org)
    out = _compact(
        {
            "name": (org.get("name") or "").strip() or None,
            "url": org.get("url") or None,
            "rating": rating,
            "rating_count": rating_count,
        }
    )
    return out or None


def _parse_product(html: str, url: str) -> list[dict[str, Any]]:
    """Build the single product from the page's JSON-LD spine.

    Raises ``AdapterParseError`` when no ``Product`` JSON-LD is present — that is
    Shopee's spine, so its absence is scraper-rot (or a wall), not an empty page.
    """
    objects = _jsonld_objects(html)
    item = _find(objects, "Product")
    if item is None:
        raise AdapterParseError(
            "product page: no schema.org Product JSON-LD found — site structure "
            "may have changed or the page was walled"
        )

    offers = item.get("offers") if isinstance(item.get("offers"), dict) else {}
    rating, review_count = _rating(item)
    ids = _product_ids(url)
    shop_id, item_id = ids if ids else (None, None)
    image = item.get("image") if isinstance(item.get("image"), str) else None

    product = _compact(
        {
            "title": (item.get("name") or "").strip() or None,
            "url": item.get("url") or url,
            "shop_id": shop_id,
            "item_id": item_id,
            "product_id": item_id
            or (str(item["productID"]) if item.get("productID") else None),
            "price": _price(offers.get("price")),
            "currency": offers.get("priceCurrency") or None,
            "condition": _condition(offers.get("itemCondition")),
            "in_stock": _in_stock(offers.get("availability")),
            "brand": _brand_name(item.get("brand")),
            "rating": rating,
            "review_count": review_count,
            "seller": _seller(offers),
            "category": _category_path(_find(objects, "BreadcrumbList")),
            "image": image,
            "images": [image] if image else [],
            "description": (item.get("description") or "").strip() or None,
        }
    )
    return [product]


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


def _render_markdown(products: list[dict[str, Any]], *, currency: str) -> str:
    if not products:
        return "# Shopee\n\nNenhum produto encontrado."
    parts: list[str] = []
    for p in products:
        parts.append(
            f"# {p.get('title', '?')}\n\n"
            f"**{_fmt_price(p.get('price'), p.get('currency') or currency)}**"
        )
        extras: list[str] = []
        if p.get("rating") is not None:
            rb = f"{p['rating']}/5"
            if p.get("review_count"):
                rb += f" ({p['review_count']} avaliações)"
            extras.append(rb)
        if p.get("brand"):
            extras.append(f"Marca: {p['brand']}")
        seller = p.get("seller") or {}
        if seller.get("name"):
            extras.append(f"Vendedor: {seller['name']}")
        if p.get("category"):
            extras.append(p["category"])
        if extras:
            parts.append(" · ".join(extras))
        if p.get("description"):
            parts.append(p["description"])
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Fetch + envelope
# ---------------------------------------------------------------------------


def _base_envelope(url: str, *, http_status: int = 0) -> dict[str, Any]:
    return envelope.base_envelope(
        url_requested=url,
        url_normalized=url,
        url_final=url,
        http_status=http_status,
        mode_used="shopee",
        content_type="application/x-shopee",
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
# (http → browser → mobile; adapters skip the wayback tail); see
# fetch._make_adapter_fetcher.
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


async def fetch_shopee(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
    fetch_html: HtmlFetcher | None = None,
) -> dict[str, Any]:
    """Fetch a Shopee product page and return a structured envelope.

    HTML is obtained via ``fetch_html`` — the main flow injects the shared
    ``http → browser → mobile`` escalation chain — no wayback tail, since an
    archived product page would be stale (Shopee is seeded to the browser tier
    in ``vasco/strategy.py``). Without an injected fetcher it falls back to a
    browser-only fetch.
    """

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
        products = _parse_product(html_src, url)
    except AdapterParseError as exc:
        log.warning("shopee parse anchor missing: %s", exc)
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, f"shopee {exc}", http_status=status
        )
    except Exception as exc:
        log.warning("shopee parse failed: %s", exc)
        return _failure_envelope(
            url,
            FailureReason.PARSE_FAILED,
            f"shopee parse failed: {type(exc).__name__}: {exc}",
            http_status=status,
        )

    from .. import io as io_mod

    currency = next(
        (p["currency"] for p in products if p.get("currency")),
        getattr(getattr(cfg, "shopee", None), "currency", None) or "BRL",
    )
    language = getattr(getattr(cfg, "shopee", None), "language", None) or "pt-BR"
    markdown = _render_markdown(products, currency=currency)
    title = products[0].get("title") if products else "Shopee"
    return envelope.success_envelope(
        base=_base_envelope(url, http_status=status or 200),
        markdown=markdown,
        metadata={
            "title": title,
            "byline": None,
            "published": None,
            "modified": None,
            "language": language,
            "site_name": "Shopee",
            "image": products[0].get("image") if products else None,
            "word_count": len(markdown.split()),
            "quality": {
                "provider": "shopee",
                "page_type": "product",
                "currency": currency,
                "result_count": len(products),
                "products": products,
            },
            "warnings": [],
        },
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )
