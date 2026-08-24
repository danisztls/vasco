# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Petlove marketplace adapter (Brazil).

Petlove (``petlove.com.br``) is Brazil's largest pet-supplies marketplace. Like
the other structured-data adapters, its useful payload — prices, ratings,
variants, reviews — is rendered by a JS-heavy Nuxt app that the default
trafilatura pipeline flattens to lossy prose. Petlove, however, embeds clean
**schema.org JSON-LD** server-side for SEO, and that is the robust spine this
adapter parses (it survives Petlove's CSS rotation):

- **Search pages** (``/busca?q=<query>``) embed an ``ItemList`` whose
  ``itemListElement`` is the list of result ``Product`` objects (name, image,
  sku, url, brand, ``offers`` with price/priceCurrency/availability), plus a
  ``description`` carrying the catalogue total ("… com 69 produtos
  disponíveis."). The same Products are also emitted as standalone blocks, used
  as a fallback when the ``ItemList`` wrapper is absent.
- **Product/detail pages** (``/<slug>/p``) embed a ``ProductGroup`` — Petlove
  sells one product in several sizes, so the page carries the group (name,
  description, brand, ``productGroupID``, ``aggregateRating``, ``review``) with a
  ``hasVariant`` list of per-size ``Product`` offers (sku, size, price,
  availability). A single-size product without a group falls back to a plain
  ``Product``.

Public surface mirrors the other adapters:
- ``is_petlove_url(url)`` — match a Petlove BR *search* or *product* URL.
- ``fetch_petlove(url, *, deadline, cfg=None, fetch_html=None)`` — return a v0.1
  envelope (``mode_used="petlove"``, ``content_type="application/x-petlove"``);
  never raises — returns a failure envelope on any fetch/parse failure.

Scope is search + product pages only; category/brand/content URLs are left
unmatched so they fall through to the normal fetch path (the URL alone can't
tell a listing category from an editorial page, and a non-listing page must not
become an adapter failure).

Petlove sits behind Cloudflare's "Just a moment…" interstitial — the plain http
tier gets a 403 challenge — so ``vasco/strategy.py`` seeds ``petlove.com.br`` to
the **browser** tier (like OLX). HTML is still obtained through the shared
escalation chain via the injected ``fetch_html`` — the seed only picks the
*starting* tier; learning can still flip it.
"""

from __future__ import annotations

import logging
import re
from functools import partial
from typing import Any
from urllib.parse import urlsplit

from .. import envelope
from ..errors import AdapterParseError, FailureReason
from . import _common
from ._common import (
    HtmlFetcher,
)
from ._common import (
    as_float as _as_float,
)
from ._common import (
    as_int as _as_int,
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
    dedup as _dedup,
)
from ._common import (
    fmt_price_brl as _fmt_price,
)
from ._common import (
    host as _host,
)
from ._common import (
    jsonld_objects as _jsonld_objects,
)
from ._common import (
    soup as _soup,
)

log = logging.getLogger(__name__)

_GALLERY_CAP: int = 8
_DEFAULT_MAX_REVIEWS: int = 10
_TOTAL_RE = re.compile(r"(\d[\d.]*)\s*produtos", re.IGNORECASE)
# A BRL amount, tolerating Petlove's space-split separators ("R$14 , 32").
_MONEY_RE = re.compile(r"R\$\s*(\d[\d.\s]*,\s*\d{2})")


# ---------------------------------------------------------------------------
# URL detection / routing
# ---------------------------------------------------------------------------


def _is_petlove_host(url: str) -> bool:
    host = _host(url)
    return host == "petlove.com.br" or host.endswith(".petlove.com.br")


def _page_type(url: str) -> str | None:
    """Classify a Petlove URL as ``"search"``, ``"product"``, or ``None``.

    ``None`` (category/brand/content) is left unmatched so it falls through to
    the normal fetch path instead of becoming an adapter failure — the URL alone
    can't distinguish a listing category from an editorial page.
    """
    path = (urlsplit(url).path or "/").rstrip("/").lower()
    if path == "/busca":
        return "search"
    if path != "/p" and path.endswith("/p"):
        return "product"
    return None


def is_petlove_url(url: str) -> bool:
    """Match a Petlove BR *search* (``/busca``) or *product* (``…/p``) URL."""
    return bool(url) and _is_petlove_host(url) and _page_type(url) is not None


# ---------------------------------------------------------------------------
# Small parsing helpers (numbers, money, JSON-LD shaping)
# ---------------------------------------------------------------------------


def _price(value: Any) -> int | float | None:
    """Parse a schema.org ``offers.price``.

    Petlove JSON-LD prices are en-format decimals — a bare number on search
    (``174.9``) or a dot-decimal string on variant offers (``"22.50"``), *not*
    Brazilian comma-decimals — so the separator is a real decimal point. Strip
    any thousands separators/currency, then float; whole floats collapse to int.
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


def _in_stock(value: Any) -> bool | None:
    return ("instock" in value.lower()) if isinstance(value, str) else None


def _find(objects: list[dict[str, Any]], typ: str) -> dict[str, Any] | None:
    return next((o for o in objects if o.get("@type") == typ), None)


def _agg_rating(item: dict[str, Any]) -> tuple[float | None, int | None]:
    """``(ratingValue, reviewCount)`` from an ``aggregateRating`` block.

    Petlove uses ``reviewCount`` (not the ``ratingCount`` that ``_common.rating``
    reads), and emits ``ratingValue`` as a full float — so parse defensively and
    accept either count key.
    """
    agg = item.get("aggregateRating")
    if not isinstance(agg, dict):
        return None, None
    count = agg.get("reviewCount")
    if count is None:
        count = agg.get("ratingCount")
    value = _as_float(agg.get("ratingValue"))
    # Petlove emits a full-precision mean (4.798175598631699); round for a clean
    # envelope/render (other sites already ship a short value like 4.8).
    return (round(value, 2) if value is not None else None), _as_int(count)


def _clean(value: Any) -> str | None:
    """Strip HTML tags from a JSON-LD description and collapse whitespace.

    Petlove descriptions/reviews ship as HTML fragments (``<strong>``/``<a>``/
    ``<p>``/``<br>``); render them to plain text for the envelope.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    txt = _soup(value).get_text(" ", strip=True)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt or None


# ---------------------------------------------------------------------------
# Search (ItemList JSON-LD)
# ---------------------------------------------------------------------------


def _total_count(itemlist: dict[str, Any] | None) -> int | None:
    """Catalogue total from the ``ItemList`` (``numberOfItems`` or its
    ``description``: "Catálogo completo com 69 produtos disponíveis.")."""
    if not itemlist:
        return None
    n = _as_int(itemlist.get("numberOfItems"))
    if n is not None:
        return n
    desc = itemlist.get("description")
    if isinstance(desc, str):
        m = _TOTAL_RE.search(desc)
        if m:
            return _as_int(m.group(1))
    return None


def _search_product(item: dict[str, Any], position: int) -> dict[str, Any] | None:
    offers = item.get("offers") if isinstance(item.get("offers"), dict) else {}
    name = item.get("name")
    url = offers.get("url") or item.get("url")
    if not (name and url):
        return None
    image = item.get("image") if isinstance(item.get("image"), str) else None
    return _compact(
        {
            "position": position,
            "title": name.strip() if isinstance(name, str) else None,
            "url": item.get("url") or url,
            "sku": item.get("sku") or None,
            "price": _price(offers.get("price")),
            "currency": offers.get("priceCurrency") or None,
            "brand": _brand_name(item.get("brand")),
            "in_stock": _in_stock(offers.get("availability")),
            "image": image,
        }
    )


def _parse_search(html: str) -> tuple[list[dict[str, Any]], int | None]:
    """Return ``(products, total_count)`` from a search page's JSON-LD.

    Anchors on the ``ItemList`` (the search-result container); falls back to
    standalone ``Product`` blocks when the wrapper is absent. With neither the
    spine is gone — scraper-rot (or a wall), not an empty search — so raise
    ``AdapterParseError``. An ``ItemList`` present but holding zero items is a
    genuinely empty search (``no_results``), not rot.
    """
    objects = _jsonld_objects(html)
    itemlist = _find(objects, "ItemList")
    standalone = [o for o in objects if o.get("@type") == "Product"]
    if itemlist is None and not standalone:
        raise AdapterParseError(
            "search page: no ItemList/Product JSON-LD found — site structure may "
            "have changed or the page was walled"
        )

    raw: list[dict[str, Any]] = []
    if itemlist is not None:
        raw = [
            e
            for e in (itemlist.get("itemListElement") or [])
            if isinstance(e, dict) and e.get("@type") == "Product"
        ]
    if not raw:
        raw = standalone

    out: list[dict[str, Any]] = []
    for _i, item in enumerate(raw, 1):
        parsed = _search_product(item, len(out) + 1)
        if parsed is not None:
            out.append(parsed)
    return out, _total_count(itemlist)


# ---------------------------------------------------------------------------
# Product detail (ProductGroup spine: variants + reviews + aggregateRating)
# ---------------------------------------------------------------------------


def _category_path(breadcrumb: dict[str, Any] | None) -> str | None:
    """Recover the category chain from a ``BreadcrumbList``.

    The crumb runs ``Home > Cachorro > Rações > Ração Seca`` (it ends at the
    category, not the product); drop the leading "Home" crumb and join the rest
    with " > "."""
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
    if names and names[0].lower() == "home":
        names = names[1:]
    return " > ".join(names) or None


def _variant(v: dict[str, Any]) -> dict[str, Any]:
    offers = v.get("offers") if isinstance(v.get("offers"), dict) else {}
    return _compact(
        {
            "sku": v.get("sku") or None,
            "size": (v.get("size") or "").strip() or None
            if isinstance(v.get("size"), str)
            else v.get("size"),
            "price": _price(offers.get("price")),
            "currency": offers.get("priceCurrency") or None,
            "in_stock": _in_stock(offers.get("availability")),
            "url": offers.get("url") or None,
            "image": v.get("image") if isinstance(v.get("image"), str) else None,
        }
    )


def _reviews(value: Any, cap: int) -> list[dict[str, Any]]:
    """Lift embedded ``review`` objects (author/rating/title/text/date), capped."""
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for r in value:
        if not isinstance(r, dict):
            continue
        rr = r.get("reviewRating") if isinstance(r.get("reviewRating"), dict) else {}
        author = r.get("author")
        author_name = (
            author.get("name")
            if isinstance(author, dict)
            else (author if isinstance(author, str) else None)
        )
        review = _compact(
            {
                "author": author_name.strip() if isinstance(author_name, str) else None,
                "rating": _as_float(rr.get("ratingValue")),
                "title": (r.get("name") or "").strip() or None
                if isinstance(r.get("name"), str)
                else None,
                "text": _clean(r.get("description")),
                "date": (r.get("datePublished") or "").strip() or None
                if isinstance(r.get("datePublished"), str)
                else None,
            }
        )
        if review:
            out.append(review)
        if len(out) >= cap:
            break
    return out


def _from_group(
    group: dict[str, Any],
    objects: list[dict[str, Any]],
    url: str,
    *,
    max_reviews: int,
) -> dict[str, Any]:
    variants = [v for v in (_variant(x) for x in group.get("hasVariant") or []) if v]
    prices = [v["price"] for v in variants if v.get("price") is not None]
    images = _dedup(
        [v.get("image") for v in variants] + [group.get("image")], _GALLERY_CAP
    )
    rating, review_count = _agg_rating(group)
    currency = next((v["currency"] for v in variants if v.get("currency")), None)
    lo = min(prices) if prices else None
    hi = max(prices) if prices else None
    product = _compact(
        {
            "title": (group.get("name") or "").strip() or None,
            "url": group.get("url") or url,
            "product_id": str(group["productGroupID"])
            if group.get("productGroupID")
            else None,
            "brand": _brand_name(group.get("brand")),
            "price": lo,
            "price_max": hi if hi is not None and hi != lo else None,
            "currency": currency,
            "in_stock": any(v.get("in_stock") for v in variants) if variants else None,
            "rating": rating,
            "review_count": review_count,
            "category": _category_path(_find(objects, "BreadcrumbList")),
            "description": _clean(group.get("description")),
            "image": images[0] if images else None,
            "images": images,
            "variants": variants,
            "reviews": _reviews(group.get("review"), max_reviews),
        }
    )
    return product


def _from_single(
    item: dict[str, Any],
    objects: list[dict[str, Any]],
    url: str,
    *,
    max_reviews: int,
) -> dict[str, Any]:
    """Build a product from a plain ``Product`` (a single-size item with no
    ``ProductGroup`` wrapper)."""
    offers = item.get("offers") if isinstance(item.get("offers"), dict) else {}
    image = item.get("image") if isinstance(item.get("image"), str) else None
    rating, review_count = _agg_rating(item)
    return _compact(
        {
            "title": (item.get("name") or "").strip() or None,
            "url": item.get("url") or offers.get("url") or url,
            "product_id": str(item.get("productID") or item.get("sku") or "") or None,
            "sku": item.get("sku") or None,
            "brand": _brand_name(item.get("brand")),
            "price": _price(offers.get("price")),
            "currency": offers.get("priceCurrency") or None,
            "in_stock": _in_stock(offers.get("availability")),
            "rating": rating,
            "review_count": review_count,
            "category": _category_path(_find(objects, "BreadcrumbList")),
            "description": _clean(item.get("description")),
            "image": image,
            "images": [image] if image else [],
            "variants": [],
            "reviews": _reviews(item.get("review"), max_reviews),
        }
    )


def _moneys(text: str) -> list[int | float]:
    """All BRL amounts in `text`, in order (handles "R$17,90 R$16 , 11")."""
    out: list[int | float] = []
    for m in _MONEY_RE.finditer(text or ""):
        v = _brl_to_num(m.group(1))
        if v is not None:
            out.append(v)
    return out


def _dom_extras(html: str) -> dict[str, Any]:
    """Best-effort fields the JSON-LD omits, scraped from the rendered DOM.

    JSON-LD carries the variants/prices/rating/reviews spine, but **not** the
    technical-specs table nor the struck "preço cheio" — those live only in the
    rendered HTML. Resilient by design: any missing/moved selector simply yields
    nothing rather than failing the parse.
    """
    soup = _soup(html)
    extras: dict[str, Any] = {}

    # Specifications table: repeated `.properties__list` rows, each a
    # name/value pair (e.g. "TIPO DE AREIA" → "Bentonita").
    specs: dict[str, str] = {}
    for row in soup.select(".properties__list"):
        name = row.select_one(".properties__list__name")
        value = row.select_one(".properties__list__value")
        if not (name and value):
            continue
        key = re.sub(r"\s+", " ", name.get_text(" ", strip=True)).strip()
        val = re.sub(r"\s+", " ", value.get_text(" ", strip=True)).strip()
        if key and val and key not in specs:
            specs[key] = val
    if specs:
        extras["specs"] = specs

    # Struck "preço cheio" for the selected variant — the regular price the
    # bottom CTA bar shows alongside it is already the JSON-LD `price`, so only
    # the higher (list) amount is new.
    single = soup.select_one(".bottom-ctas-bar__single-price")
    if single is not None:
        amounts = _moneys(single.get_text(" ", strip=True))
        if len(amounts) >= 2 and max(amounts) > min(amounts):
            extras["list_price"] = max(amounts)

    # Description fallback: the JSON-LD usually carries it, but a sparse page may
    # only render it in the DOM.
    details = soup.select_one("section.product-details")
    if details is not None:
        txt = _clean(details.get_text(" ", strip=True))
        if txt:
            extras["description_dom"] = txt

    return extras


def _parse_product(
    html: str, url: str, *, max_reviews: int = _DEFAULT_MAX_REVIEWS
) -> list[dict[str, Any]]:
    """Build the single product from the page's JSON-LD spine, enriched with the
    best-effort DOM extras the JSON-LD omits (specs, struck list price).

    Prefers the ``ProductGroup`` (variants), falling back to a plain ``Product``.
    Raises ``AdapterParseError`` when neither is present — that is Petlove's
    spine, so its absence is scraper-rot (or a wall), not an empty page.
    """
    objects = _jsonld_objects(html)
    group = _find(objects, "ProductGroup")
    if group is not None:
        product = _from_group(group, objects, url, max_reviews=max_reviews)
    else:
        single = _find(objects, "Product")
        if single is None:
            raise AdapterParseError(
                "product page: no ProductGroup/Product JSON-LD found — site "
                "structure may have changed or the page was walled"
            )
        product = _from_single(single, objects, url, max_reviews=max_reviews)

    try:
        extras = _dom_extras(html)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("petlove DOM extras failed: %s", exc)
        extras = {}
    desc_dom = extras.pop("description_dom", None)
    product.update({k: v for k, v in extras.items() if v not in (None, "", [], {})})
    if not product.get("description") and desc_dom:
        product["description"] = desc_dom

    return [product]


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _price_label(p: dict[str, Any], currency: str) -> str:
    cur = p.get("currency") or currency
    lo = _fmt_price(p.get("price"), cur)
    if p.get("price_max") is not None:
        return f"{lo} – {_fmt_price(p['price_max'], cur)}"
    return lo


def _render_search(products: list[dict[str, Any]], *, currency: str) -> str:
    if not products:
        return "# Petlove\n\nNenhum produto encontrado."
    parts = [f"{len(products)} produtos", ""]
    for i, p in enumerate(products, 1):
        head = f"{i}. **{p.get('title', '?')}** — {_price_label(p, currency)}"
        if p.get("brand"):
            head += f" — {p['brand']}"
        if p.get("in_stock") is False:
            head += " — esgotado"
        parts.append(head)
    return "\n".join(parts)


def _render_review(r: dict[str, Any]) -> str:
    """One review as a Markdown bullet: ``- ★★★★ Author — "Title": text``."""
    rating = r.get("rating")
    stars = "★" * round(rating) if isinstance(rating, (int, float)) else ""
    head = " ".join(x for x in (stars, r.get("author") or "Anônimo") if x)
    line = f"- **{head}**"
    if r.get("title"):
        line += f" — {r['title']}"
    text = r.get("text") or ""
    if len(text) > 280:
        text = text[:279].rstrip() + "…"
    if text:
        line += f": {text}"
    return line


def _render_product(products: list[dict[str, Any]], *, currency: str) -> str:
    if not products:
        return "# Petlove\n\nNenhum produto encontrado."
    p = products[0]
    cur = p.get("currency") or currency
    price_line = f"**{_price_label(p, currency)}**"
    if p.get("list_price") is not None:
        price_line += f" (de {_fmt_price(p['list_price'], cur)})"
    parts = [f"# {p.get('title', '?')}", price_line]

    meta: list[str] = []
    if p.get("rating") is not None:
        rb = f"{round(p['rating'], 2)}/5"
        if p.get("review_count"):
            rb += f" ({p['review_count']} avaliações)"
        meta.append(rb)
    if p.get("brand"):
        meta.append(f"Marca: {p['brand']}")
    if p.get("category"):
        meta.append(p["category"])
    if meta:
        parts.append(" · ".join(meta))

    # The multiple size/price pairs, one per line so each is legible.
    variants = p.get("variants") or []
    if variants:
        lines = ["## Tamanhos e preços"]
        for v in variants:
            line = f"- {v.get('size', '?')} — {_fmt_price(v.get('price'), v.get('currency') or cur)}"
            if v.get("in_stock") is False:
                line += " (esgotado)"
            lines.append(line)
        parts.append("\n".join(lines))

    if p.get("description"):
        parts.append("## Descrição\n\n" + p["description"])

    specs = p.get("specs") or {}
    if specs:
        parts.append(
            "## Especificações\n\n" + "\n".join(f"- {k}: {v}" for k, v in specs.items())
        )

    reviews = p.get("reviews") or []
    if reviews:
        head = "## Avaliações"
        if p.get("review_count"):
            head += f" ({len(reviews)} de {p['review_count']})"
        parts.append(head + "\n\n" + "\n".join(_render_review(r) for r in reviews))

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Fetch + envelope
# ---------------------------------------------------------------------------


_base_envelope, _failure_envelope = _common.envelope_builders(
    "petlove", "application/x-petlove"
)


async def fetch_petlove(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
    fetch_html: HtmlFetcher | None = None,
) -> dict[str, Any]:
    """Fetch a Petlove search/product page and return a structured envelope.

    HTML is obtained via ``fetch_html`` — the main flow injects the shared
    ``http → browser → mobile`` escalation chain — no wayback tail, since an
    archived listing would be stale (Petlove is seeded to the browser tier in
    ``vasco/strategy.py``). Without an injected fetcher it falls back to a
    browser-only fetch.
    """
    page_type = _page_type(url) or "search"

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

    petlove = getattr(getattr(cfg, "adapters", None), "petlove", None)
    max_reviews = max(
        0, int(getattr(petlove, "max_reviews", _DEFAULT_MAX_REVIEWS) or 0)
    )

    total_count: int | None = None
    try:
        if page_type == "product":
            products = _parse_product(html_src, url, max_reviews=max_reviews)
        else:
            products, total_count = _parse_search(html_src)
    except AdapterParseError as exc:
        log.warning("petlove parse anchor missing (%s): %s", page_type, exc)
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, f"petlove {exc}", http_status=status
        )
    except Exception as exc:
        log.warning("petlove parse failed (%s): %s", page_type, exc)
        return _failure_envelope(
            url,
            FailureReason.PARSE_FAILED,
            f"petlove parse failed: {type(exc).__name__}: {exc}",
            http_status=status,
        )

    from .. import io as io_mod

    currency = next(
        (p["currency"] for p in products if p.get("currency")),
        getattr(petlove, "currency", None) or "BRL",
    )
    language = getattr(petlove, "language", None) or "pt-BR"
    warnings = ["no_results"] if page_type == "search" and not products else []

    if page_type == "product":
        markdown = _render_product(products, currency=currency)
        title = products[0].get("title") if products else "Petlove"
    else:
        markdown = _render_search(products, currency=currency)
        title = f"Petlove: {len(products)} produtos"

    quality: dict[str, Any] = {
        "provider": "petlove",
        "page_type": page_type,
        "currency": currency,
        "result_count": len(products),
        "products": products,
    }
    if total_count is not None:
        quality["total_count"] = total_count

    return envelope.success_envelope(
        base=_base_envelope(url, http_status=status or 200),
        markdown=markdown,
        metadata={
            "title": title,
            "byline": None,
            "published": None,
            "modified": None,
            "language": language,
            "site_name": "Petlove",
            "image": products[0].get("image") if products else None,
            "word_count": len(markdown.split()),
            "quality": quality,
            "warnings": warnings,
        },
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )
