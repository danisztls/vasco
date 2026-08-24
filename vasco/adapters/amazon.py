# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Amazon marketplace adapter (Brazil).

Amazon is one of Brazil's largest marketplaces, and like the other
structured-data adapters its useful payload — price, rating, brand, availability,
specs — lives in JS-heavy pages the default trafilatura pipeline flattens to lossy
prose. Unlike MercadoLivre/Shopee, Amazon embeds **no** schema.org JSON-LD on its
search or product pages, so (like the AliExpress adapter) the robust spine here is
the **server-rendered DOM**, which Amazon ships in full on the http tier and keeps
markup-stable across themes:

- **Search/listing pages** (``/s?k=<query>``) render result cards into
  ``div[data-component-type="s-search-result"]`` containers, each carrying a
  ``data-asin`` and its own title / ``.a-price`` / rating / review-count / image.
  The ASIN is the canonical spine; the per-card URL is rebuilt as the clean
  ``/dp/<ASIN>`` form (Amazon's hrefs are tracking-laden ``ref=`` slugs).
- **Product/detail pages** (``/dp/<ASIN>``, ``/gp/product/<ASIN>``) carry the
  price/title in stable ids (``#productTitle``, ``#corePriceDisplay_desktop_feature_div``,
  ``#acrPopover``, ``#acrCustomerReviewText``, ``#bylineInfo``, ``#availability``,
  ``#landingImage``). Feature bullets and the detail/spec tables are lifted
  best-effort and never fail the parse.

Scope is **search + product pages only** on ``amazon.com.br``; the homepage,
category/browse nodes, cart, and account URLs aren't matched by ``is_amazon_url``
(they carry no extractable listing), so they fall through to the normal fetch
path. Other-country Amazon domains (``amazon.com``, ``amazon.co.uk``, …) are out of
scope — prices/labels here assume the BR pt-BR storefront.

Public surface mirrors the other adapters:
- ``is_amazon_url(url)`` — match an Amazon BR search/product URL.
- ``fetch_amazon(url, *, deadline, cfg=None, fetch_html=None)`` — return a v0.1
  envelope (``mode_used="amazon"``, ``content_type="application/x-amazon"``);
  never raises — returns a failure envelope on any fetch/parse failure.

Amazon serves full structured HTML on the plain http tier, so — unlike the
bot-challenged marketplaces — it is **not** seeded to the browser tier in
``vasco/strategy.py``; the shared escalation chain still escalates http → browser
on its own if Amazon throws its homegrown robot/captcha wall (recognised by
``fetch.bot_detect`` → ``BLOCKED_CAPTCHA``).
"""

from __future__ import annotations

import logging
import re
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from .. import envelope
from ..errors import AdapterParseError, FailureReason
from . import _common
from ._common import (
    HtmlFetcher,
)
from ._common import (
    as_int as _as_int,
)
from ._common import (
    brl_to_num as _brl_to_num,
)
from ._common import (
    compact as _compact,
)
from ._common import (
    fmt_price_brl as _fmt_price,
)
from ._common import (
    host as _host,
)
from ._common import (
    soup as _soup,
)

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_GALLERY_CAP: int = 6
_FEATURES_CAP: int = 12
_SPECS_CAP: int = 30
# Detail-bullet rows that merely restate a normalized top-level field (and whose
# raw value is noisy — the "customer reviews" row is Amazon's duplicated
# star-widget text "4,7 4,7 de 5 estrelas (63) …"). Dropped from `specs` so the
# clean `rating`/`review_count`/`asin` fields are the single source. Compared
# lowercased; PT-BR + EN forms.
_SKIP_SPEC_KEYS: frozenset[str] = frozenset(
    {
        "asin",
        "avaliações de clientes",
        "avaliacoes de clientes",
        "classificação dos clientes",
        "classificacao dos clientes",
        "customer reviews",
        "customer ratings",
    }
)

# An ASIN is a 10-char uppercase alphanumeric id (B0…, or a 10-digit ISBN). It
# appears after /dp/, /gp/product/, /gp/aw/d/, or a bare /product/ path segment;
# the regex is unanchored so the common slug form (/kindle-paperwhite/dp/B0CFPL6CFY)
# matches too.
_ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})(?:[/?]|$)")
# A review-count aria-label / text starts with the number then the BR word
# ("8.399 classificações" / "8.400 avaliações"); the rating aria starts with
# "4,8 de 5 …" so anchoring on the leading number + word keeps them apart.
_REVIEW_COUNT_RE = re.compile(r"^\s*[\d.,]+\s+(?:classifica|avalia)", re.IGNORECASE)
# A rating ("4,8 de 5 estrelas" / "4,8 de 5"): grab the leading 0-5 decimal.
_RATING_RE = re.compile(r"(\d(?:[.,]\d)?)")
# Amazon CDN size/format transform suffix ("81-vCHKJb1L._AC_SL1500_.jpg" →
# "81-vCHKJb1L.jpg"); anchored to the final segment so it never eats a real path.
_IMG_SUFFIX_RE = re.compile(
    r"\._[A-Za-z0-9,_-]+_\.(jpe?g|png|gif|webp)$", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# URL detection / routing
# ---------------------------------------------------------------------------


def _is_br_host(url: str) -> bool:
    host = _host(url)
    return host == "amazon.com.br" or host.endswith(".amazon.com.br")


def _asin(url: str) -> str | None:
    m = _ASIN_RE.search(urlsplit(url).path or "")
    return m.group(1) if m else None


def _is_search(url: str) -> bool:
    """A keyword-search URL: ``/s`` (or ``/s/ref=…``) with a ``k=`` query."""
    parts = urlsplit(url)
    path = (parts.path or "").lower()
    if not (path == "/s" or path.startswith("/s/")):
        return False
    return bool(parse_qs(parts.query).get("k"))


def _page_type(url: str) -> str | None:
    """``"product"`` / ``"search"`` for a claimable URL, else ``None``.

    Only product (``/dp/<ASIN>``) and keyword-search (``/s?k=``) pages carry
    extractable structured data. Everything else (homepage, ``/b?node=`` browse,
    ``/gp/cart``, account) returns None so it falls through to a normal fetch
    instead of becoming an adapter failure (the petlove-style guard)."""
    if _asin(url):
        return "product"
    if _is_search(url):
        return "search"
    return None


def is_amazon_url(url: str) -> bool:
    """Match an Amazon BR *search* or *product* URL only.

    Non-listing Amazon URLs (home, browse nodes, cart, account) carry no
    extractable structured data, so leaving them unmatched lets them fall through
    to the normal fetch path instead of becoming an adapter failure."""
    return bool(url) and _is_br_host(url) and _page_type(url) is not None


def _canonical_url(asin: str, base: str) -> str:
    """The clean ``https://<host>/dp/<ASIN>`` form (drops Amazon's ``ref=``
    tracking slug + query) using the page's host."""
    host = _host(base) or "www.amazon.com.br"
    return f"https://{host}/dp/{asin}"


# ---------------------------------------------------------------------------
# Small parsing helpers (price, rating, image, brand)
# ---------------------------------------------------------------------------


def _rating_num(text: Any) -> float | None:
    """Parse a 0-5 rating from "4,8 de 5 estrelas"/"4,8 de 5"/"4.8" → 4.8.

    The decimal is comma (PT-BR), so route the captured fragment through a
    comma→dot replace rather than _brl_to_num (which strips dots as thousands)."""
    if not text:
        return None
    m = _RATING_RE.search(str(text))
    if not m:
        return None
    try:
        f = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return f if 0.0 <= f <= 5.0 else None


def _price_from_el(el: Any) -> int | float | None:
    """Number from an ``.a-price`` element, robust to Amazon's two layouts.

    Search cards populate the screen-reader ``.a-offscreen`` ("R$ 879,00"), but
    product pages often leave it empty and render the price only in the visible
    ``.a-price-whole`` ("879,") + ``.a-price-fraction`` ("00") spans. Prefer the
    offscreen text when present, else reassemble the visible fragments."""
    if el is None:
        return None
    off = el.select_one(".a-offscreen")
    if off and off.get_text(strip=True):
        return _brl_to_num(off.get_text(strip=True))
    whole = el.select_one(".a-price-whole")
    if whole is not None:
        frac = el.select_one(".a-price-fraction")
        text = whole.get_text(strip=True) + (frac.get_text(strip=True) if frac else "")
        return _brl_to_num(text)
    return None


def _list_price(
    original: int | float | None, price: int | float | None
) -> int | float | None:
    """A struck-through "De:" list price is meaningful only when it is strictly
    greater than the current price. Amazon's ``.a-text-price`` also wraps per-unit
    prices ("R$ 64,90/100g") that would otherwise misparse as a fake discount, so
    drop anything not above ``price``."""
    if isinstance(original, (int, float)) and isinstance(price, (int, float)):
        return original if original > price else None
    return None


def _current_price_el(scope: Any) -> Any:
    """The current (not struck-through) ``.a-price`` within ``scope``.

    ``.a-text-price`` is the strikethrough list price; the first ``.a-price``
    that isn't one is the price the buyer pays."""
    for ap in scope.select(".a-price"):
        classes = ap.get("class") or []
        if "a-text-price" not in classes:
            return ap
    return None


def _clean_img(src: str | None) -> str | None:
    """Strip Amazon's CDN size/format transform suffix back to the full asset."""
    if not src or not src.strip():
        return None
    return _IMG_SUFFIX_RE.sub(r".\1", src.strip())


# ---------------------------------------------------------------------------
# Search (rendered result cards)
# ---------------------------------------------------------------------------


def _card_review_count(card: Any) -> int | None:
    """Review count from the card's "8.399 classificações" aria-label/text."""
    for el in card.select("[aria-label]"):
        if _REVIEW_COUNT_RE.match(el.get("aria-label") or ""):
            return _as_int(el["aria-label"])
    return None


def _card_sponsored(card: Any) -> bool | None:
    """True when the card is a sponsored/ad placement, else None (so ``compact``
    drops the field on organic results rather than stamping ``False`` on every
    one). Amazon marks ads with a "Patrocinado" label element."""
    if card.select_one(
        '[class*="sponsored-label"], [data-component-type="sp-sponsored-result"]'
    ):
        return True
    return None


def _parse_card(card: Any, base: str, position: int) -> dict[str, Any] | None:
    asin = (card.get("data-asin") or "").strip()
    if not asin:
        return None
    title_el = (
        card.select_one("h2 a span")
        or card.select_one("h2 span")
        or card.select_one("h2")
    )
    title = title_el.get_text(" ", strip=True) if title_el else None
    icon = card.select_one(".a-icon-alt")
    img = card.select_one("img.s-image")
    price = _price_from_el(_current_price_el(card))
    return _compact(
        {
            "position": position,
            "title": (title or "").strip() or None,
            "url": _canonical_url(asin, base),
            "asin": asin,
            "price": price,
            "original_price": _list_price(
                _price_from_el(card.select_one(".a-price.a-text-price")), price
            ),
            "rating": _rating_num(icon.get_text(strip=True) if icon else None),
            "review_count": _card_review_count(card),
            "image": _clean_img(img.get("src") if img else None),
            "sponsored": _card_sponsored(card),
        }
    )


def _parse_search(html: str, base: str) -> list[dict[str, Any]]:
    """All result cards on a search page.

    Raises ``AdapterParseError`` when neither result cards nor the results
    container are present — that is the search page's structural anchor, so its
    absence is scraper-rot (or a wall), not an empty result set. A container
    present but holding zero cards is a genuinely empty search (→ no_results)."""
    soup = _soup(html)
    cards = soup.select('div[data-component-type="s-search-result"]')
    if not cards:
        if not soup.select(
            '[data-component-type="s-search-results"], .s-main-slot, .s-result-list'
        ):
            raise AdapterParseError(
                "search page: no s-search-result cards or results container found "
                "— site structure may have changed or the page was walled"
            )
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    position = 0
    for card in cards:
        asin = (card.get("data-asin") or "").strip()
        if not asin or asin in seen:
            continue
        seen.add(asin)
        position += 1
        parsed = _parse_card(card, base, position)
        if parsed is not None:
            out.append(parsed)
    return out


# ---------------------------------------------------------------------------
# Product detail (stable-id DOM spine + best-effort extras)
# ---------------------------------------------------------------------------


def _brand(soup: BeautifulSoup) -> str | None:
    """Brand from ``#bylineInfo`` ("Marca: Amazon" / "Visite a loja Amazon")."""
    el = soup.select_one("#bylineInfo")
    if not el:
        return None
    txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
    txt = re.sub(
        r"^(marca:|brand:|visite a loja\s*|visit the\s*|loja:)\s*",
        "",
        txt,
        flags=re.IGNORECASE,
    ).strip()
    # "Marca X Loja" trailing "Loja"/"Store" suffix that some bylines append.
    txt = re.sub(r"\s+(loja|store)$", "", txt, flags=re.IGNORECASE).strip()
    return txt or None


def _availability(soup: BeautifulSoup) -> tuple[str | None, bool | None]:
    el = soup.select_one("#availability")
    if not el:
        return None, None
    txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() or None
    low = (txt or "").lower()
    in_stock: bool | None = None
    if any(w in low for w in ("em estoque", "disponível", "in stock")):
        in_stock = True
    elif any(
        w in low
        for w in (
            "indisponível",
            "esgotado",
            "fora de estoque",
            "currently unavailable",
            "out of stock",
        )
    ):
        in_stock = False
    return txt, in_stock


def _features(soup: BeautifulSoup) -> list[str]:
    """The "Sobre este item" feature bullets (``#feature-bullets``)."""
    out: list[str] = []
    for li in soup.select("#feature-bullets .a-list-item"):
        txt = re.sub(r"\s+", " ", li.get_text(" ", strip=True)).strip()
        if txt and txt not in out:
            out.append(txt)
        if len(out) >= _FEATURES_CAP:
            break
    return out


def _specs(soup: BeautifulSoup) -> dict[str, str]:
    """Best-effort product detail/spec table → ``{key: value}``.

    Covers the three common Amazon layouts: the tech-spec / detail-bullets
    ``<table>`` (th/td rows) and the ``#detailBullets_feature_div`` "Key: Value"
    ``<li>`` list. Any missing/moved container just yields fewer keys."""
    out: dict[str, str] = {}

    def add(key: str | None, val: str | None) -> None:
        if not key or not val:
            return
        key = re.sub(r"\s+", " ", key).strip().rstrip(":").strip()
        val = re.sub(r"\s+", " ", val).strip()
        if key.lower() in _SKIP_SPEC_KEYS:
            return
        if key and val and key not in out and len(out) < _SPECS_CAP:
            out[key] = val

    for sel in (
        "#productDetails_techSpec_section_1 tr",
        "#productDetails_detailBullets_sections1 tr",
        ".prodDetTable tr",
    ):
        for row in soup.select(sel):
            th = row.find("th")
            td = row.find("td")
            if th and td:
                add(th.get_text(" ", strip=True), td.get_text(" ", strip=True))

    for li in soup.select("#detailBullets_feature_div li"):
        spans = li.select("span.a-list-item span")
        if len(spans) >= 2:
            add(spans[0].get_text(" ", strip=True), spans[1].get_text(" ", strip=True))
    return out


def _gallery(soup: BeautifulSoup, landing: str | None) -> list[str]:
    """Clean image gallery: the hi-res landing image plus alt thumbnails."""
    urls: list[str] = []
    if landing:
        urls.append(landing)
    urls.extend(
        img.get("src") or "" for img in soup.select("#altImages img, #imageBlock img")
    )
    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        cleaned = _clean_img(u)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
        if len(out) >= _GALLERY_CAP:
            break
    return out


def _landing_image(soup: BeautifulSoup) -> str | None:
    el = soup.select_one("#landingImage") or soup.select_one("#imgTagWrapperId img")
    if not el:
        return None
    return _clean_img(el.get("data-old-hires") or el.get("src"))


def _parse_product(html: str, url: str) -> list[dict[str, Any]]:
    """Build the single product from the page's stable-id DOM spine.

    Raises ``AdapterParseError`` when ``#productTitle`` is absent — that is the
    product page's anchor, so its absence is scraper-rot (or a wall), not an
    empty page."""
    soup = _soup(html)
    title_el = soup.select_one("#productTitle")
    if title_el is None:
        raise AdapterParseError(
            "product page: no #productTitle found — site structure may have "
            "changed or the page was walled"
        )
    title = title_el.get_text(" ", strip=True) or None

    asin = _asin(url)
    asin_input = soup.select_one("input#ASIN, input#ASIN_NO_DP")
    if not asin and asin_input is not None:
        asin = (asin_input.get("value") or "").strip() or None

    price_scope = (
        soup.select_one("#corePriceDisplay_desktop_feature_div")
        or soup.select_one("#corePrice_feature_div")
        or soup.select_one("#apex_desktop")
        or soup.select_one("#price_inside_buybox")
        or soup
    )
    price = _price_from_el(_current_price_el(price_scope))
    original = _list_price(
        _price_from_el(price_scope.select_one(".a-price.a-text-price")), price
    )

    rating_el = soup.select_one("#acrPopover") or soup.select_one(
        "span[data-hook=rating-out-of-text]"
    )
    rating = _rating_num(
        (rating_el.get("title") if rating_el else None)
        or (rating_el.get_text(strip=True) if rating_el else None)
    )
    review_el = soup.select_one("#acrCustomerReviewText")
    review_count = _as_int(review_el.get_text(strip=True)) if review_el else None

    availability, in_stock = _availability(soup)
    landing = _landing_image(soup)

    product = _compact(
        {
            "title": title,
            "url": _canonical_url(asin, url) if asin else url,
            "asin": asin,
            "price": price,
            "original_price": original,
            "rating": rating,
            "review_count": review_count,
            "brand": _brand(soup),
            "availability": availability,
            "in_stock": in_stock,
            "features": _features(soup),
            "specs": _specs(soup),
            "image": landing,
            "images": _gallery(soup, landing),
        }
    )
    if product.get("images") and not product.get("image"):
        product["image"] = product["images"][0]
    return [product]


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_markdown(
    products: list[dict[str, Any]], *, page_type: str, currency: str
) -> str:
    if not products:
        return "# Amazon\n\nNenhum produto encontrado."
    parts: list[str] = []
    if page_type == "search":
        parts.append(f"{len(products)} produtos")
        parts.append("")
        for i, p in enumerate(products, 1):
            head = (
                f"{i}. **{p.get('title', '?')}** — "
                f"{_fmt_price(p.get('price'), p.get('currency') or currency)}"
            )
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
            if p.get("sponsored"):
                extras.append("patrocinado")
            if extras:
                head += " — " + " · ".join(extras)
            parts.append(head)
        return "\n".join(parts)

    # product
    p = products[0]
    parts.append(
        f"# {p.get('title', '?')}\n\n"
        f"**{_fmt_price(p.get('price'), p.get('currency') or currency)}**"
    )
    extras = []
    if p.get("original_price"):
        extras.append(
            f"de {_fmt_price(p['original_price'], p.get('currency') or currency)}"
        )
    if p.get("rating") is not None:
        rb = f"{p['rating']}/5"
        if p.get("review_count"):
            rb += f" ({p['review_count']} avaliações)"
        extras.append(rb)
    if p.get("brand"):
        extras.append(f"Marca: {p['brand']}")
    if p.get("availability"):
        extras.append(p["availability"])
    if extras:
        parts.append(" · ".join(extras))
    parts.extend(f"- {feat}" for feat in p.get("features", []))
    specs = p.get("specs") or {}
    if specs:
        parts.append("")
        for k, v in specs.items():
            parts.append(f"- **{k}**: {v}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Fetch + envelope
# ---------------------------------------------------------------------------


_base_envelope, _failure_envelope = _common.envelope_builders(
    "amazon", "application/x-amazon"
)


async def fetch_amazon(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
    fetch_html: HtmlFetcher | None = None,
) -> dict[str, Any]:
    """Fetch an Amazon BR search/product page and return a structured envelope.

    HTML is obtained via ``fetch_html`` — the main flow injects the shared
    ``http → browser → mobile`` escalation chain (no wayback tail). Amazon serves
    full structured HTML on the http tier, so it isn't seeded to the browser tier;
    the chain escalates on its own if Amazon throws its robot/captcha wall. Without
    an injected fetcher it falls back to a browser-only fetch."""
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

    try:
        if page_type == "product":
            products = _parse_product(html_src, url)
        else:
            products = _parse_search(html_src, url)
    except AdapterParseError as exc:
        log.warning("amazon parse anchor missing (%s): %s", page_type, exc)
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, f"amazon {exc}", http_status=status
        )
    except Exception as exc:
        log.warning("amazon parse failed (%s): %s", page_type, exc)
        return _failure_envelope(
            url,
            FailureReason.PARSE_FAILED,
            f"amazon parse failed: {type(exc).__name__}: {exc}",
            http_status=status,
        )

    from .. import io as io_mod

    amazon = getattr(getattr(cfg, "adapters", None), "amazon", None)
    currency = getattr(amazon, "currency", None) or "BRL"
    language = getattr(amazon, "language", None) or "pt-BR"
    for p in products:
        p.setdefault("currency", currency)

    # Anchor present but zero parsed products on a search page: a genuinely empty
    # result set (the rot case raises above). Product pages always yield one.
    warnings = ["no_results"] if page_type == "search" and not products else []

    markdown = _render_markdown(products, page_type=page_type, currency=currency)
    title = (
        products[0].get("title")
        if page_type == "product" and products
        else f"Amazon: {len(products)} produtos"
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
            "site_name": "Amazon",
            "image": products[0].get("image") if products else None,
            "word_count": len(markdown.split()),
            "quality": {
                "provider": "amazon",
                "page_type": page_type,
                "currency": currency,
                "result_count": len(products),
                "products": products,
            },
            "warnings": warnings,
        },
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )
