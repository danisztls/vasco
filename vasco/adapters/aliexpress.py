"""AliExpress marketplace adapter (Brazil / pt-BR surface).

AliExpress runs Alibaba's ``baxia``/``x5sec`` anti-bot stack: the plain HTTP tier
only ever gets the ``_____tmd_____/punish`` redirect stub, and a *cold* browser
context gets the ``nc`` slider captcha. The **warm persistent browser profile**
(``vasco browser-server``'s ``user_data_dir``) carries an earned ``x5secdata``
clearance plus a stable Camoufox fingerprint and is served real pages — so
``vasco/strategy.py`` seeds ``aliexpress.com`` to the **browser** tier. When the
clearance sours, ``bot_detect`` now recognises the punish page (→
``BLOCKED_CAPTCHA``) and the browser server's manual-VNC solve flow can fire.

Unlike the other marketplaces, AliExpress does **not** embed clean structured
JSON in its pages (the detail page is a CSR ``newDetail`` app whose data loads via
a *signed* mtop XHR). So this adapter parses two robust surfaces:

- **Search/listing pages** (``/w/wholesale-<q>.html``, ``/wholesale``) render
  real product cards into the DOM (``card-out-wrapper`` containers). Each card is
  parsed for title, price, old price, discount, rating, sold count, installments
  and image. Prices come from the structural ``decimal_point`` spans AliExpress
  wraps each fragment in (``R$``/``918``/``,``/``44``), reassembled by
  ``_price_from_spans`` — robust to the per-fragment node splitting that defeats a
  flat-text regex.
- **Product/detail pages** (``/item/<id>.html``) are spine'd on the ``product_id``
  from the URL, enriched with the **open reviews endpoint**
  (``feedback.aliexpress.com/pc/searchEvaluation.do``: rating, count, star
  histogram, top reviews) plus best-effort DOM extras (title, price, gallery) that
  never fail the parse.

Public surface mirrors the other adapters:
- ``is_aliexpress_url(url)`` — match an aliexpress.com URL.
- ``fetch_aliexpress(url, *, deadline, cfg=None, fetch_html=None)`` — return a
  v0.1 envelope (``mode_used="aliexpress"``,
  ``content_type="application/x-aliexpress"``); never raises.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

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


_GALLERY_CAP: int = 8
_REVIEWS_CAP: int = 6
_ITEM_ID_RE = re.compile(r"/item/(\d+)\.html", re.IGNORECASE)
# A run of digits/dots/commas/spaces after "R$", ending in a digit. Used only as
# a best-effort fallback on PDP pages: AliExpress splits each price separator into
# its own text node, so rendered text reads "R$ 1 . 544 , 99". On search cards we
# prefer the structural `decimal_point` spans (see _price_from_spans), which don't
# bleed into the rating/sold digits the way a flat-text regex does.
_PRICE_RE = re.compile(r"R\$[\s\d.,]*\d")
_DISCOUNT_RE = re.compile(r"-\s*(\d+)\s*%")
# A standalone rating number ("4.9"/"5,0"): an entire element's text, so it can't
# collide with split price digits or a bare sold count.
_RATING_RE = re.compile(r"^([0-5][.,]\d)$")
_SOLD_RE = re.compile(r"([\d.,]+)\s*(mil|mi)?\+?\s*vendido", re.IGNORECASE)
# Strip AliExpress CDN derived-size suffixes ("Sxxx.jpg_480x480q75.jpg_.avif",
# "Sxxx.png_480x480") back to the original asset ("Sxxx.jpg"). Anchored to the
# last path segment so it never eats a real path.
_IMG_SIZE_SUFFIX_RE = re.compile(
    r"(\.(?:jpe?g|png|webp|gif|avif))_[^/]*$", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# URL detection / routing
# ---------------------------------------------------------------------------


_AE_DOMAINS: tuple[str, ...] = ("aliexpress.com", "aliexpress.com.br")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_aliexpress_url(url: str) -> bool:
    host = _host(url)
    return bool(url) and any(host == d or host.endswith("." + d) for d in _AE_DOMAINS)


def _page_type(url: str) -> str:
    """Classify a URL as a 'product' (``/item/<id>.html``) or 'search' page.

    Everything that isn't an item page — ``/w/wholesale-*.html``, ``/wholesale``,
    category/store/home — is treated as a search/listing surface.
    """
    return "product" if _ITEM_ID_RE.search(urlsplit(url).path or "") else "search"


def _product_id_from_url(url: str) -> str | None:
    m = _ITEM_ID_RE.search(urlsplit(url).path or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Small parsing helpers (numbers, money, images, dicts)
# ---------------------------------------------------------------------------


def _brl_to_num(text: Any) -> int | float | None:
    """Parse a Brazilian money string to a number, tolerating the space-split
    separators AliExpress renders ("R$ 1 . 544 , 99" → 1544.99, "918" → 918)."""
    s = re.sub(r"[^\d.,]", "", str(text))
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def _rating_num(text: Any) -> float | None:
    """Parse a 0-5 rating ("4.9"/"4,9"); the decimal is a real decimal point,
    unlike money, so don't route it through _brl_to_num (which strips dots)."""
    s = str(text).strip().replace(",", ".")
    try:
        f = float(s)
    except ValueError:
        return None
    return f if 0.0 <= f <= 5.0 else None


def _clean_image(src: str | None, base: str) -> str | None:
    """Absolutize a protocol-relative CDN URL and strip the size/format suffix
    AliExpress appends ("...Sxxx.jpg_480x480q75.jpg_.avif" → "...Sxxx.jpg")."""
    if not src or not src.strip():
        return None
    url = urljoin(base, src.strip())
    # AliExpress media: keep the original asset, drop derived-size suffixes.
    return _IMG_SIZE_SUFFIX_RE.sub(r"\1", url)


def _dedup(urls: Any, limit: int = _GALLERY_CAP, *, base: str = "") -> list[str]:
    if isinstance(urls, str):
        urls = [urls]
    seen: set[str] = set()
    out: list[str] = []
    for u in urls or []:
        cleaned = (
            _clean_image(u, base)
            if base
            else (u.strip() if isinstance(u, str) else None)
        )
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drop null / empty values so each record carries only what's known."""
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


# ---------------------------------------------------------------------------
# Search (rendered product cards)
# ---------------------------------------------------------------------------


def _card_container(anchor: Any) -> Any:
    """Climb from an ``/item/`` anchor to its ``card-out-wrapper`` ancestor (the
    card that bounds one product's price/rating/image), falling back to a few
    parents up when the wrapper class isn't found."""
    node = anchor
    for _ in range(6):
        parent = node.parent
        if parent is None:
            break
        classes = parent.get("class") or []
        if any("card-out-wrapper" in c for c in classes):
            return parent
        node = parent
    return node


def _price_from_spans(el: Any) -> int | float | None:
    """Current price from the structural ``decimal_point`` spans AliExpress wraps
    each price fragment in. Excludes the struck-through (``line-through``) old
    price. Joining the fragment texts reassembles "R$" + "396" + "," + "44"."""
    spans = [
        s
        for s in el.select('[style*="decimal_point"]')
        if "line-through" not in (s.get("style") or "")
    ]
    if not spans:
        return None
    return _brl_to_num("".join(s.get_text(strip=True) for s in spans))


def _old_price(el: Any) -> int | float | None:
    struck = el.select_one('[style*="line-through"]')
    return _brl_to_num(struck.get_text(strip=True)) if struck else None


def _card_rating(card: Any) -> float | None:
    for span in card.find_all("span"):
        m = _RATING_RE.match(span.get_text(strip=True))
        if m:
            return _rating_num(m.group(1))
    return None


def _card_sold(card: Any) -> int | None:
    for el in card.find_all(string=_SOLD_RE):
        m = _SOLD_RE.search(el)
        if not m:
            continue
        base_n = _brl_to_num(m.group(1))
        if base_n is None:
            continue
        factor = {"mil": 1000, "mi": 1_000_000}.get((m.group(2) or "").lower(), 1)
        return int(base_n * factor)
    return None


def _card_image(card: Any, base: str) -> str | None:
    for img in card.select("img"):
        src = img.get("src") or ""
        if "aliexpress-media" in src:
            return _clean_image(src, base)
    first = card.select_one("img")
    return _clean_image(first.get("src") if first else None, base)


def _parse_card(anchor: Any, base: str, position: int) -> dict[str, Any] | None:
    href = anchor.get("href") or ""
    pid_match = _ITEM_ID_RE.search(href)
    if not pid_match:
        return None
    pid = pid_match.group(1)
    card = _card_container(anchor)

    h3 = card.find("h3")
    title_wrap = card.select_one("[title]")
    title = (
        (h3.get_text(" ", strip=True) if h3 else None)
        or (title_wrap.get("title") if title_wrap else None)
        or anchor.get("title")
    )

    disc = _DISCOUNT_RE.search(card.get_text(" ", strip=True))
    inst = card.select_one('[title*="juros"], [title*="parcela"]')

    return _compact(
        {
            "position": position,
            "title": (title or "").strip() or None,
            "url": urljoin(base, href),
            "product_id": pid,
            "price": _price_from_spans(card),
            "old_price": _old_price(card),
            "discount_pct": int(disc.group(1)) if disc else None,
            "rating": _card_rating(card),
            "sold_count": _card_sold(card),
            "installments": (inst.get("title").strip() if inst else None),
            "image": _card_image(card, base),
        }
    )


def _parse_search(html: str, base: str) -> list[dict[str, Any]]:
    soup = _soup(html)
    anchors = soup.select('a[href*="/item/"]')
    if not anchors:
        # No product links and no recognisable results grid: the page didn't
        # render real content (scraper rot or an unexpected layout).
        if not soup.select('[class*="card-out-wrapper"], [class*="search-item"]'):
            raise AdapterParseError(
                "search page: no /item/ product links found — site structure may "
                "have changed or the page did not render"
            )
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    position = 0
    for anchor in anchors:
        pid_match = _ITEM_ID_RE.search(anchor.get("href") or "")
        if not pid_match or pid_match.group(1) in seen:
            continue
        seen.add(pid_match.group(1))
        position += 1
        parsed = _parse_card(anchor, base, position)
        if parsed is not None:
            out.append(parsed)
    return out


# ---------------------------------------------------------------------------
# Product detail (URL spine + reviews endpoint + best-effort DOM)
# ---------------------------------------------------------------------------


def _text(soup: BeautifulSoup, selector: str) -> str | None:
    el = soup.select_one(selector)
    if not el:
        return None
    txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
    return txt or None


def _pdp_title(soup: BeautifulSoup) -> str | None:
    """Best-effort product title. Prefer og:title; fall back to a product-title
    element or <h1>, ignoring the generic "Aliexpress" the CSR shell leaves."""
    og = soup.select_one('meta[property="og:title"]')
    if og and (og.get("content") or "").strip():
        return og["content"].strip()
    for sel in ('h1[class*="title"]', '[class*="product-title"]', "h1"):
        txt = _text(soup, sel)
        if txt and txt.lower() not in ("aliexpress", "você também vai gostar"):
            return txt
    return None


def _pdp_extras(html: str, base: str) -> dict[str, Any]:
    """Best-effort display fields a rendered PDP exposes. Resilient by design: any
    missing/moved selector simply yields nothing rather than failing the parse."""
    soup = _soup(html)
    extras: dict[str, Any] = {}

    title = _pdp_title(soup)
    if title:
        extras["title"] = title

    og_img = soup.select_one('meta[property="og:image"]')
    desc = soup.select_one('meta[name="description"]')
    if desc and (desc.get("content") or "").strip():
        extras["description"] = desc["content"].strip()

    # Prices render once the price module hydrates (absent on a shell). Prefer the
    # structural decimal_point spans; fall back to a text regex.
    price = _price_from_spans(soup)
    if price is None:
        amounts = _PRICE_RE.findall(re.sub(r"\s+", " ", soup.get_text(" ", strip=True)))
        if amounts:
            price = _brl_to_num(amounts[0])
    if price is not None:
        extras["price"] = price

    gallery = [
        img.get("src")
        for img in soup.select("img")
        if img.get("src") and "aliexpress-media.com" in (img.get("src") or "")
    ]
    if og_img and (og_img.get("content") or "").strip():
        gallery.insert(0, og_img["content"])
    images = _dedup(gallery, base=base or "https://www.aliexpress.com/")
    if images:
        extras["images"] = images
        extras["image"] = images[0]

    return extras


def _parse_reviews(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the feedback endpoint JSON into rating + histogram + top reviews.
    Defensive: a missing block just yields fewer fields."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}

    stat = data.get("productEvaluationStatistic")
    if isinstance(stat, dict):
        rating = stat.get("evarageStar")
        out["rating"] = float(rating) if isinstance(rating, (int, float)) else None
        total = stat.get("totalNum")
        out["review_count"] = int(total) if isinstance(total, (int, float)) else None
        histogram = {
            star: int(stat[key])
            for star, key in (
                (5, "fiveStarNum"),
                (4, "fourStarNum"),
                (3, "threeStarNum"),
                (2, "twoStarNum"),
                (1, "oneStarNum"),
            )
            if isinstance(stat.get(key), (int, float))
        }
        if histogram:
            out["rating_histogram"] = histogram

    reviews: list[dict[str, Any]] = []
    for ev in data.get("evaViewList") or []:
        if not isinstance(ev, dict):
            continue
        feedback = ev.get("buyerTranslationFeedback") or ev.get("buyerFeedback")
        eval_score = ev.get("buyerEval")
        stars = (
            round(eval_score / 20, 1) if isinstance(eval_score, (int, float)) else None
        )
        review = _compact(
            {
                "text": feedback.strip() if isinstance(feedback, str) else None,
                "stars": stars,
                "country": ev.get("buyerCountry"),
                "date": ev.get("evalDate"),
                "sku": ev.get("skuInfo"),
                "images": _dedup(ev.get("images"), limit=4),
            }
        )
        if review.get("text") or review.get("stars") is not None:
            reviews.append(review)
        if len(reviews) >= _REVIEWS_CAP:
            break
    if reviews:
        out["reviews"] = reviews
    return _compact(out)


async def _fetch_reviews_json(
    product_id: str, *, lang: str, country: str, page_size: int, timeout: float
) -> dict[str, Any] | None:
    """GET the open AliExpress reviews endpoint. Plain HTTP (no browser), never
    raises — returns None on any failure so reviews are simply omitted."""
    import httpx

    url = (
        "https://feedback.aliexpress.com/pc/searchEvaluation.do"
        f"?productId={product_id}&lang={lang}&country={country}"
        f"&page=1&pageSize={page_size}&filter=all&sort=complex_default"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.aliexpress.com/",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        async with httpx.AsyncClient(
            http2=True, follow_redirects=True, timeout=max(2.0, timeout)
        ) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception as exc:  # network/JSON/anything: reviews are optional
        log.debug("aliexpress reviews fetch failed for %s: %s", product_id, exc)
        return None


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
        return "# AliExpress\n\nNenhum produto encontrado."
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
        if p.get("old_price"):
            extras.append(
                f"de {_fmt_price(p['old_price'], p.get('currency') or currency)}"
            )
        if p.get("discount_pct"):
            extras.append(f"-{p['discount_pct']}%")
        if p.get("rating") is not None:
            rb = f"{p['rating']}/5"
            if p.get("review_count"):
                rb += f" ({p['review_count']})"
            extras.append(rb)
        if p.get("sold_count"):
            extras.append(f"{p['sold_count']} vendidos")
        if extras:
            head += " — " + " · ".join(extras)
        parts.append(head)
        if page_type == "product":
            for r in p.get("reviews", [])[:3]:
                stars = f"{r.get('stars')}★ " if r.get("stars") is not None else ""
                country = f"[{r['country']}] " if r.get("country") else ""
                parts.append(f"   - {stars}{country}{r.get('text', '')}")
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
        mode_used="aliexpress",
        content_type="application/x-aliexpress",
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


def _labels(cfg: Any | None) -> tuple[str, str, str, int]:
    """(currency, language, country, reviews_page_size) from cfg, with defaults."""
    ae = getattr(getattr(cfg, "adapters", None), "aliexpress", None)
    currency = getattr(ae, "currency", None) or "BRL"
    language = getattr(ae, "language", None) or "pt_BR"
    country = getattr(ae, "country", None) or "BR"
    page_size = getattr(ae, "reviews_page_size", None) or _REVIEWS_CAP
    return currency, language, country, int(page_size)


async def fetch_aliexpress(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
    fetch_html: HtmlFetcher | None = None,
) -> dict[str, Any]:
    """Fetch an AliExpress search/product page and return a structured envelope.

    HTML is obtained via ``fetch_html`` — the main flow injects the shared
    ``http → browser → mobile`` escalation chain — no wayback tail, since an
    archived product page would be stale (AliExpress is seeded to the browser
    tier in ``vasco/strategy.py``). Without an injected fetcher it falls back to
    a browser-only fetch. Detail pages additionally fetch the open reviews
    endpoint over plain HTTP.
    """
    page_type = _page_type(url)
    currency, language, country, page_size = _labels(cfg)

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

    page_blocked = reason != FailureReason.OK or not html_src
    block_reason = reason if reason != FailureReason.OK else FailureReason.SERVER_ERROR

    # Search needs the rendered page; a block/empty body is fatal. Product pages
    # are resilient: the reviews endpoint (rating, count, top reviews) is an open
    # plain-HTTP API needing no browser clearance, so we still return product_id +
    # reviews even when the walled PDP HTML never rendered.
    if page_type == "search" and page_blocked:
        return _failure_envelope(
            url, block_reason, f"fetch failed via {mode_used} tier", http_status=status
        )

    warnings: list[str] = []
    try:
        if page_type == "product":
            dom_html = "" if page_blocked else html_src
            products = await _build_product(
                dom_html,
                url,
                language=language,
                country=country,
                page_size=page_size,
                deadline=deadline,
            )
            # Blocked PDP that yielded nothing beyond the bare id+url (no reviews,
            # no DOM): surface the block instead of a hollow success.
            recovered = any(
                set(p) - {"url", "product_id", "currency"} for p in products
            )
            if page_blocked and not recovered:
                return _failure_envelope(
                    url,
                    block_reason,
                    f"product page blocked via {mode_used} tier and no reviews "
                    "available",
                    http_status=status,
                )
            if page_blocked:
                warnings.append("page_blocked")  # reviews only; PDP DOM unavailable
        else:
            products = _parse_search(html_src, url)
    except AdapterParseError as exc:
        log.warning("aliexpress parse anchor missing (%s): %s", page_type, exc)
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, f"aliexpress {exc}", http_status=status
        )
    except Exception as exc:
        log.warning("aliexpress parse failed (%s): %s", page_type, exc)
        return _failure_envelope(
            url,
            FailureReason.PARSE_FAILED,
            f"aliexpress parse failed: {type(exc).__name__}: {exc}",
            http_status=status,
        )

    from .. import io as io_mod

    # Anchor present but zero parsed products on a search page: a genuinely empty
    # result set (the rot case raises above). Detail pages always yield one.
    if page_type == "search" and not products:
        warnings.append("no_results")

    for p in products:
        p.setdefault("currency", currency)
    markdown = _render_markdown(products, page_type=page_type, currency=currency)
    title = (
        products[0].get("title")
        if page_type == "product" and products
        else f"AliExpress: {len(products)} produtos"
    )
    return envelope.success_envelope(
        base=_base_envelope(url, http_status=status or 200),
        markdown=markdown,
        metadata={
            "title": title,
            "byline": None,
            "published": None,
            "modified": None,
            "language": language.replace("_", "-"),
            "site_name": "AliExpress",
            "image": products[0].get("image") if products else None,
            "word_count": len(markdown.split()),
            "quality": {
                "provider": "aliexpress",
                "page_type": page_type,
                "currency": currency,
                "result_count": len(products),
                "products": products,
            },
            "warnings": warnings,
        },
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )


async def _build_product(
    html: str,
    url: str,
    *,
    language: str,
    country: str,
    page_size: int,
    deadline: float,
) -> list[dict[str, Any]]:
    """Build the single detail product: ``product_id`` (URL) spine + reviews
    endpoint + best-effort DOM extras. Detail always yields exactly one product;
    the only failure modes (punish / empty body) are caught upstream."""
    pid = _product_id_from_url(url)
    if not pid:
        raise AdapterParseError("product page: no /item/<id>.html product id in URL")

    product: dict[str, Any] = {"url": url, "product_id": pid}

    # Best-effort DOM extras (title, price, gallery). Never fatal.
    try:
        product.update(_pdp_extras(html, url))
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("aliexpress PDP extras failed: %s", exc)

    # Reviews (rating, histogram, top reviews) from the open endpoint.
    payload = await _fetch_reviews_json(
        pid,
        lang=language,
        country=country,
        page_size=page_size,
        timeout=min(8.0, deadline),
    )
    if payload:
        product.update(_parse_reviews(payload))

    return [_compact(product)]
