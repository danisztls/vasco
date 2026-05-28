"""Google Shopping structured product adapter.

Google Shopping aggregates product listings (price, store, ratings, return
policy) from hundreds of merchants — exactly the data an AI shopping research
agent wants. The default trafilatura pipeline collapses these cards to flat
markdown that loses prices and stores, so we parse the rendered HTML directly
and return structured product dicts via ``quality.products``.

Public surface:
- ``is_google_shopping_url(url)`` — match Google Shopping search + homepage.
- ``fetch_google_shopping(url, *, deadline, cfg=None)`` — return a v0.1 envelope.

Envelope uses ``mode_used="google_shopping"`` and
``content_type="application/x-google-shopping"``. On any fetch/parse failure
it returns a failure envelope rather than raising.

Implementation notes:
- HTTP tier returns an empty JS shell; this adapter always uses the browser.
- Product cards live inside ``<product-viewer-entrypoint>`` elements; each
  card carries a comprehensive ``aria-label`` describing the product in
  Portuguese. Parsing the aria-label is more robust than scraping div
  structure because Google rotates obfuscated class names.
- Used/refurbished items, international sellers, and statistical price
  outliers (Tukey's fence, k=1.5, gated to N≥8) are dropped to keep the
  surface useful for typical shopping queries.
"""

from __future__ import annotations

import asyncio
import logging
import re
import statistics
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit, unquote_plus

from ..errors import FailureReason
from ..fetch import browser

log = logging.getLogger(__name__)


# Matches:
#   https://www.google.com/search?...udm=28...   (Shopping tab)
#   https://www.google.com.br/search?...udm=28...
#   https://www.google.com/shopping[/...]        (Shopping homepage + category browse)
_GOOGLE_HOST_RE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)*google(?:\.[a-z]{2,3}){1,2}/",
    re.IGNORECASE,
)

_BADGE_MAP: dict[str, str] = {
    "PROMOÇÃO": "promo",
    "REDUÇÃO NO PREÇO": "price_drop",
    "PREÇO BAIXO": "low_price",
}

# Tier cap mirrors fetch.BROWSER_MAX_BUDGET; Google Shopping is JS-heavy and
# routinely takes 4–7s to render the product grid.
_BROWSER_TIER_CAP: float = 8.0

# Outlier filter only fires with enough samples; below this, the IQR is
# dominated by individual products.
_OUTLIER_MIN_N: int = 8
_OUTLIER_K: float = 1.5

# Treat as truthy if the aria-label exceeds this length and contains "R$" —
# Google Shopping's comprehensive product summary is always well over this.
_FULL_LABEL_MIN_LEN: int = 100


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


def is_google_shopping_url(url: str) -> bool:
    if not url or not _GOOGLE_HOST_RE.match(url):
        return False
    parts = urlsplit(url)
    path = parts.path or "/"
    if path.startswith("/shopping"):
        return True
    if path == "/search":
        qs = parse_qs(parts.query)
        return "28" in qs.get("udm", [])
    return False


def _extract_query(url: str) -> str | None:
    parts = urlsplit(url)
    qs = parse_qs(parts.query)
    q = qs.get("q", [""])[0]
    return unquote_plus(q) if q else None


# ---------------------------------------------------------------------------
# Aria-label parser
# ---------------------------------------------------------------------------


def _parse_price(raw: str) -> float | None:
    """Parse a Brazilian price string like ``1.043,25`` or ``949``."""
    cleaned = raw.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_review_count(num_str: str, has_mil: bool) -> int | None:
    cleaned = num_str.replace(".", "").replace(",", ".")
    try:
        n = float(cleaned)
    except ValueError:
        return None
    return int(n * 1000) if has_mil else int(n)


def _parse_product(label: str) -> dict[str, Any] | None:
    """Parse one Google Shopping product aria-label.

    Returns ``None`` for used/refurbished items and for international sellers
    (anything Google prefixes with ``Preço no exterior:``). Returns ``None``
    on unparseable input (no title or no price).

    Null/empty fields are omitted from the returned dict — the schema is
    "what's present" rather than "every key with possible nulls".
    """
    # Filter rule 1: used / refurbished
    if re.search(r"\.\s*(?:Usado|Recondicionado)\.", label):
        return None
    # Filter rule 2: international (foreign sellers / converted FX prices)
    if "Preço no exterior:" in label:
        return None

    product: dict[str, Any] = {}

    badges = [en for pt, en in _BADGE_MAP.items() if pt in label]

    # Title: everything before the first "  " (double-space) that precedes
    # the price block. Google always inserts two spaces between the product
    # name and the structured info that follows.
    title_match = re.match(r"^(.+?)\.\s{2,}", label)
    if not title_match:
        return None
    title = title_match.group(1).strip()
    # Promo badges sometimes prefix the title with no separator ("PROMOÇÃOAr…").
    for pt in _BADGE_MAP:
        if title.startswith(pt):
            title = title[len(pt) :].strip()
    if not title:
        return None
    product["title"] = title

    # Current price (first R$ occurrence — handles both "Preço atual: R$ X"
    # and "R$ X agora" forms).
    price_match = re.search(r"(?:Preço atual: )?R\$\s*([\d.,]+)", label)
    if not price_match:
        return None
    price = _parse_price(price_match.group(1))
    if price is None:
        return None
    product["price_brl"] = price

    # Previous price (only present on items "on sale").
    was_match = re.search(r"Custava R\$\s*([\d.,]+)", label)
    if was_match:
        was = _parse_price(was_match.group(1))
        if was and was > price:
            product["was_price_brl"] = was
            product["discount_pct"] = round((was - price) / was * 100, 1)

    rating_match = re.search(r"Avaliado com (\d[,.]\d)\s+de\s+5", label)
    if rating_match:
        try:
            product["rating"] = float(rating_match.group(1).replace(",", "."))
        except ValueError:
            pass

    review_match = re.search(r"([\d.,]+)\s*(mil)?\s*avaliações", label)
    if review_match:
        rc = _parse_review_count(review_match.group(1), bool(review_match.group(2)))
        if rc is not None:
            product["review_count"] = rc

    # Store: find the segment that isn't a known structured field. The label
    # is a dot-separated sentence; the store is the only free-form text.
    segments = [s.strip().rstrip(".") for s in re.split(r"\.\s+", label) if s.strip()]
    skip_markers = (
        "R$",
        "Preço atual",
        "parcelas",
        "Avaliado",
        "avaliações",
        "Devolução",
        "Custava",
        "PROMOÇÃO",
        "REDUÇÃO NO PREÇO",
        "PREÇO BAIXO",
    )
    for seg in segments:
        if any(m in seg for m in skip_markers):
            continue
        if seg == product["title"]:
            continue
        has_more = seg.endswith("e mais") or " e mais" in seg
        store = re.sub(r"\s+e mais$", "", seg).strip()
        if store and len(store) < 80:
            product["store"] = store
            if has_more:
                product["other_stores"] = True
            break

    if badges:
        product["badges"] = badges

    return product


# ---------------------------------------------------------------------------
# Outlier filter
# ---------------------------------------------------------------------------


def _filter_outliers(
    products: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Drop price outliers via Tukey's fence (IQR k=1.5), gated to N≥8.

    Returns ``(kept, dropped_count)``. Below the N threshold or when the IQR
    is zero (all prices identical), nothing is filtered.
    """
    if len(products) < _OUTLIER_MIN_N:
        return products, 0
    prices = [p["price_brl"] for p in products]
    q1, _q2, q3 = statistics.quantiles(prices, n=4)
    iqr = q3 - q1
    if iqr <= 0:
        return products, 0
    lo = q1 - _OUTLIER_K * iqr
    hi = q3 + _OUTLIER_K * iqr
    kept = [p for p in products if lo <= p["price_brl"] <= hi]
    return kept, len(products) - len(kept)


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------


def _extract_products(html_src: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Parse rendered Google Shopping HTML into (products, filter_counts).

    ``filter_counts`` reports drops per reason: ``used``, ``international``,
    ``outlier``. Keys with zero counts are omitted from the returned dict.
    """
    from lxml import html as lxml_html  # local import: lxml is heavy

    tree = lxml_html.fromstring(html_src)
    cards = tree.xpath(".//product-viewer-entrypoint")

    products: list[dict[str, Any]] = []
    used_dropped = 0
    intl_dropped = 0

    for card in cards:
        full_label: str | None = None
        for el in card.iter():
            aria = el.get("aria-label") or ""
            if len(aria) >= _FULL_LABEL_MIN_LEN and "R$" in aria:
                full_label = aria
                break
        if full_label is None:
            # Card has only partial aria-labels (typically duplicate carousel
            # entries); skip — they're either covered by a sibling card or
            # not informative enough on their own.
            continue
        # Track filter reasons before parse drops them.
        if re.search(r"\.\s*(?:Usado|Recondicionado)\.", full_label):
            used_dropped += 1
            continue
        if "Preço no exterior:" in full_label:
            intl_dropped += 1
            continue
        parsed = _parse_product(full_label)
        if parsed is not None:
            products.append(parsed)

    products, outlier_dropped = _filter_outliers(products)
    products.sort(key=lambda p: p["price_brl"])

    counts: dict[str, int] = {}
    if used_dropped:
        counts["used"] = used_dropped
    if intl_dropped:
        counts["international"] = intl_dropped
    if outlier_dropped:
        counts["outlier"] = outlier_dropped

    return products, counts


# ---------------------------------------------------------------------------
# Markdown summary
# ---------------------------------------------------------------------------


def _render_markdown(products: list[dict[str, Any]], *, query: str | None) -> str:
    if not products:
        header = (
            f"# Google Shopping: {query}\n\nNo products found."
            if query
            else "# Google Shopping\n\nNo products found."
        )
        return header
    parts: list[str] = []
    if query:
        parts.append(f"# Google Shopping: {query}")
    else:
        parts.append("# Google Shopping")
    parts.append("")
    parts.append(f"{len(products)} products (sorted by price)")
    parts.append("")
    for i, p in enumerate(products, 1):
        title = p["title"]
        price = f"R$ {p['price_brl']:.2f}".replace(".", ",")
        line = f"{i}. **{title}** — {price}"
        extras: list[str] = []
        if p.get("was_price_brl"):
            was = f"R$ {p['was_price_brl']:.2f}".replace(".", ",")
            extras.append(f"was {was} (-{p.get('discount_pct', 0)}%)")
        if p.get("store"):
            store_bit = p["store"]
            if p.get("other_stores"):
                store_bit += " (+ more)"
            extras.append(store_bit)
        if p.get("rating") is not None:
            rating_bit = f"{p['rating']}/5"
            if p.get("review_count"):
                rating_bit += f" ({p['review_count']} reviews)"
            extras.append(rating_bit)
        if p.get("badges"):
            extras.append(", ".join(p["badges"]))
        if extras:
            line += " — " + " — ".join(extras)
        parts.append(line)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------


def _base_envelope(url: str, *, http_status: int = 0) -> dict[str, Any]:
    return {
        "url_requested": url,
        "url_final": url,
        "url_canonical": url,
        "http_status": http_status,
        "mode_used": "google_shopping",
        "fetched_at": int(time.time()),
        "from_cache": False,
        "cache_age_seconds": 0,
        "content_type": "application/x-google-shopping",
    }


def _failure_envelope(
    url: str, reason: FailureReason, message: str, *, http_status: int = 0
) -> dict[str, Any]:
    env = _base_envelope(url, http_status=http_status)
    env["failure"] = {
        "reason": str(reason),
        "retry_after_seconds": None,
        "message": message,
    }
    env["markdown"] = ""
    env["warnings"] = []
    return env


def _classify_browser_error(exc: BaseException) -> FailureReason:
    msg = str(exc).lower()
    name = type(exc).__name__.lower()
    if "timeout" in name or "timeout" in msg:
        return FailureReason.TIMEOUT
    if any(
        m in msg
        for m in (
            "connection closed",
            "target closed",
            "net::err_aborted",
        )
    ):
        return FailureReason.BLOCKED_BOT
    return FailureReason.SERVER_ERROR


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def _browser_fetch_html(
    url: str, *, deadline_monotonic: float, cfg: Any | None
) -> tuple[str, int, dict[str, str]]:
    """Browser fetch helper, isolated so tests can monkeypatch it."""
    pool = browser.get_browser(cfg)
    return await pool.fetch(url, deadline_monotonic=deadline_monotonic)


async def fetch_google_shopping(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """Fetch a Google Shopping page and return a structured envelope."""
    deadline_monotonic = time.monotonic() + max(0.0, float(deadline))
    tier_deadline = min(deadline_monotonic, time.monotonic() + _BROWSER_TIER_CAP)

    try:
        html_src, status, _headers = await _browser_fetch_html(
            url, deadline_monotonic=tier_deadline, cfg=cfg
        )
    except asyncio.TimeoutError:
        return _failure_envelope(url, FailureReason.TIMEOUT, "browser deadline elapsed")
    except Exception as exc:
        reason = _classify_browser_error(exc)
        return _failure_envelope(
            url, reason, f"browser fetch failed: {type(exc).__name__}: {exc}"
        )

    if not html_src:
        return _failure_envelope(
            url,
            FailureReason.SERVER_ERROR,
            "browser returned empty body",
            http_status=status,
        )

    try:
        products, filter_counts = _extract_products(html_src)
    except Exception as exc:
        log.warning("Google Shopping HTML parse failed: %s", exc)
        return _failure_envelope(
            url,
            FailureReason.UNSUPPORTED_CONTENT_TYPE,
            f"parse failed: {type(exc).__name__}: {exc}",
            http_status=status,
        )

    query = _extract_query(url)
    markdown = _render_markdown(products, query=query)

    from .. import io as io_mod

    env = _base_envelope(url, http_status=status or 200)
    quality: dict[str, Any] = {
        "products": products,
        "currency": "BRL",
        "result_count": len(products),
    }
    if query:
        quality["search_query"] = query
    if filter_counts:
        quality["filtered"] = filter_counts

    env.update(
        {
            "title": f"Google Shopping: {query}" if query else "Google Shopping",
            "byline": None,
            "published": None,
            "modified": None,
            "language": "pt-BR",
            "site_name": "Google Shopping",
            "image": None,
            "word_count": len(markdown.split()),
            "token_count_estimate": io_mod.estimate_tokens(markdown),
            "quality": quality,
            "links": [],
            "markdown": markdown,
            "warnings": [],
        }
    )
    return env
