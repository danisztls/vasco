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
  offers.price/priceCurrency/url). On keyword searches (``lista.`` host) results
  are then relevance-sorted against the query recovered from the URL so
  MercadoLivre's premium-ad placement (off-keyword products injected into the
  native order) sinks to the bottom; off-keyword items are demoted by default and
  dropped only when ``mercadolivre.drop_off_query`` is set.
- **Product/detail pages** (``.../p/MLB<id>``, the ``.../up/MLBU<id>`` "unified
  product" form, ``produto.mercadolivre.com.br/MLB-<id>-...``) embed a single
  rich ``Product`` (offers with shippingDetails,
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

import json
import logging
import re
import unicodedata
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlsplit

from .. import envelope
from ..errors import AdapterParseError, FailureReason
from . import _common
from ._common import (
    HtmlFetcher,
)
from ._common import (
    brand_name as _brand_name,
)
from ._common import (
    brl_to_num as _brl_to_num,
)
from ._common import (
    compact as _compact,
)
from ._common import (
    condition as _condition,
)
from ._common import (
    dedup as _dedup,
)
from ._common import (
    fmt_price_brl as _fmt_price,
)
from ._common import (
    host as _host,
)
from ._common import (
    num as _num,
)
from ._common import (
    rating as _rating,
)
from ._common import (
    soup as _soup,
)
from ._common import (
    text as _text,
)

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_GALLERY_CAP: int = 6
# MLB<digits> item ids plus the MLBU<digits> "unified product" family id (the
# /up/ form). The optional letter is captured so MLBU1490047005 stays distinct
# from a hypothetical MLB1490047005 — it is never collapsed away.
_MLB_RE = re.compile(r"MLB-?([A-Z]?\d+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# URL detection / routing
# ---------------------------------------------------------------------------


def is_mercadolivre_url(url: str) -> bool:
    host = _host(url)
    return bool(url) and (
        host == "mercadolivre.com.br" or host.endswith(".mercadolivre.com.br")
    )


def _page_type(url: str) -> str:
    """Classify a URL as a 'search' (listing) or 'product' (detail) page.

    Product surfaces: the ``produto.`` host, the catalog ``/p/MLB<id>`` form, the
    ``/up/MLBU<id>`` "unified product" form, and bare item URLs ending in an
    ``MLB-<id>`` slug. Everything else (``lista.``, ``/ofertas``, category browse,
    homepage) is a search/listing page.
    """
    host = _host(url)
    path = (urlsplit(url).path or "/").lower()
    if host.startswith("produto."):
        return "product"
    if "/p/mlb" in path or "/up/mlb" in path:  # /p/MLB… catalog, /up/MLBU… unified
        return "product"
    if re.search(r"/mlb-?\d+", path):
        return "product"
    return "search"


def _search_query(url: str) -> str | None:
    """Recover the keyword query from a MercadoLivre search URL.

    Keyword searches live on the ``lista.`` subdomain, with the query in the first
    path segment as a hyphen-joined slug followed by ML's filters after
    underscores (``lista.mercadolivre.com.br/notebook-gamer_Desde_49`` → ``notebook
    gamer``). Returns None for category/deal browse (served from ``www.`` with
    category slugs that are *not* a user query) and any URL with no usable
    keyword — those have nothing to match against and must not be filtered. Falls
    back to the ``?q=`` / ``as_word`` query param on any host.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("lista."):
        segs = [s for s in (parts.path or "").split("/") if s]
        if segs:
            slug = segs[0].split("_", 1)[0]  # drop _Desde_/_NoIndex_/_PriceRange_
            query = unquote(slug).replace("-", " ").replace("+", " ").strip()
            if query:
                return query
    qs = parse_qs(parts.query)
    for key in ("q", "as_word"):
        vals = qs.get(key)
        if vals and vals[0].strip():
            return vals[0].strip()
    return None


# ---------------------------------------------------------------------------
# Normalized product + small parsing helpers
# ---------------------------------------------------------------------------


def _product_id(*candidates: Any) -> str | None:
    """First ``MLB<id>`` found across the candidate strings (url, sku, …)."""
    for c in candidates:
        if not isinstance(c, str):
            continue
        m = _MLB_RE.search(c)
        if m:
            return f"MLB{m.group(1)}"
    return None


def _jsonld_products(html: str) -> list[dict[str, Any]]:
    """All schema.org ``Product`` objects in the page, flattening ``@graph``.

    Search pages wrap many Products in one ``@graph``; product pages have a
    single top-level (or listed) Product. Order is preserved.
    """
    soup = _soup(html)
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
    for position, item in enumerate(items, start=1):
        parsed = _search_product(item, position)
        if parsed is not None:
            out.append(parsed)
    return out


# ---------------------------------------------------------------------------
# Relevance filter (fight ML's premium-ad placement on keyword searches)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _fold(text: str) -> list[str]:
    """Accent-fold, lowercase, and split into alphanumeric tokens.

    Accent folding is load-bearing for PT-BR: the ASCII URL slug ``colchao`` must
    match the accented title ``Colchão``.
    """
    if not text:
        return []
    ascii_text = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    )
    return _TOKEN_RE.findall(ascii_text.lower())


def _apply_relevance(
    products: list[dict[str, Any]], query: str, *, drop: bool, min_coverage: int
) -> tuple[list[dict[str, Any]], int]:
    """Relevance-sort search results, sinking off-keyword (ad-placement) items.

    ``coverage`` — the count of distinct query tokens present in a product's
    title+brand — is the primary, robust signal: product titles are short, so raw
    BM25 alone is weak (and can go negative on a tiny corpus). BM25 only refines
    ordering among matches, with MercadoLivre's native order as the stable
    tiebreaker. Off-keyword items (coverage < ``min_coverage``) are sorted to the
    bottom by default and **dropped** only when ``drop`` is set — strict dropping
    also loses legitimate synonym matches (a MacBook/laptop on a "notebook"
    search). Returns ``(ordered, off_query_count)``; a blank/untokenizable query
    is a no-op.
    """
    query_tokens = list(dict.fromkeys(_fold(query)))  # distinct, order-preserving
    if not query_tokens:
        return products, 0

    docs: list[list[str]] = []
    coverages: list[int] = []
    for p in products:
        toks = _fold(p.get("title") or "")
        brand = p.get("brand")
        if isinstance(brand, str):
            toks = toks + _fold(brand)
        docs.append(toks)
        present = set(toks)
        coverages.append(sum(1 for q in query_tokens if q in present))

    from ..extract import bm25_scores

    scores = bm25_scores(docs, query_tokens)

    order = sorted(
        range(len(products)),
        key=lambda i: (coverages[i], scores[i], -i),
        reverse=True,
    )
    off_query = sum(1 for c in coverages if c < min_coverage)
    ordered: list[dict[str, Any]] = []
    for i in order:
        if drop and coverages[i] < min_coverage:
            continue
        p = products[i]
        p["position"] = len(ordered) + 1  # renumber to the new relevance rank
        ordered.append(p)

    return ordered, off_query


# ---------------------------------------------------------------------------
# Product detail (JSON-LD spine + best-effort PDP HTML extras)
# ---------------------------------------------------------------------------


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
    soup = _soup(html)
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
        "images": _dedup(item.get("image"), _GALLERY_CAP),
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

_base_envelope, _failure_envelope = _common.envelope_builders(
    "mercadolivre", "application/x-mercadolivre"
)


async def fetch_mercadolivre(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
    fetch_html: HtmlFetcher | None = None,
) -> dict[str, Any]:
    """Fetch a MercadoLivre search/product page and return a structured envelope.

    HTML is obtained via ``fetch_html`` — the main flow injects the shared
    ``http → browser → mobile`` escalation chain — no wayback tail, since an
    archived product page would be stale (MercadoLivre is seeded to the browser
    tier in ``vasco/strategy.py``). Without an injected fetcher it falls back to
    a browser-only fetch.
    """
    page_type = _page_type(url)

    got = await _common.acquire_html(
        url,
        fetch_html=fetch_html,
        deadline=deadline,
        cfg=cfg,
        fail=partial(_failure_envelope, url),
    )
    if isinstance(got, dict):
        return got
    html_src, status, _mode_used = got

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

    # Relevance pass: MercadoLivre's premium-ad placement injects off-query
    # products into keyword search results, in ML's native order. Relevance-sort
    # so keyword matches rise and off-keyword items sink; drop them only when
    # configured. Product/detail and category-browse pages have no user query and
    # are left untouched.
    filtered: dict[str, int] = {}
    demoted = 0
    if page_type == "search" and products:
        ml_cfg = getattr(getattr(cfg, "adapters", None), "mercadolivre", None)
        if getattr(ml_cfg, "relevance_filter", True):
            query = _search_query(url)
            if query:
                drop = bool(getattr(ml_cfg, "drop_off_query", False))
                min_cov = max(
                    1, int(getattr(ml_cfg, "min_query_token_coverage", 1) or 1)
                )
                products, off_query = _apply_relevance(
                    products, query, drop=drop, min_coverage=min_cov
                )
                if off_query:
                    if drop:
                        filtered = {"off_query": off_query}
                    else:
                        demoted = off_query

    # Distinguish a genuinely empty parse ("no_results", the rot case raised
    # above) from a search where dropping removed every off-query item
    # ("all_off_query"). Demoting never empties the list.
    if page_type == "search" and not products:
        warnings = ["all_off_query"] if filtered else ["no_results"]
    else:
        warnings = []

    shopping = getattr(getattr(cfg, "adapters", None), "shopping", None)
    currency = next(
        (p["currency"] for p in products if p.get("currency")),
        getattr(shopping, "currency", None) or "BRL",
    )
    language = getattr(shopping, "language", None) or "pt-BR"
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
                **({"filtered": filtered} if filtered else {}),
                **({"demoted": demoted} if demoted else {}),
            },
            "warnings": warnings,
        },
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )
