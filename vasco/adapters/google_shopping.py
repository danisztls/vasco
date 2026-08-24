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

import contextlib
import logging
import re
import statistics
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlencode, urlsplit, urlunsplit

from .. import envelope
from ..errors import AdapterParseError, FailureReason
from . import _common
from ._common import HtmlFetcher

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


def _canonicalize_shopping_url(url: str) -> tuple[str, bool]:
    """Rewrite the deprecated ``/shopping?q=`` search form to ``/search?...&udm=28``.

    Google's ``/shopping?q=`` endpoint now serves an empty "Nothing to see here"
    page; the working Shopping-tab form is ``/search?q=...&udm=28``. Returns
    ``(new_url, True)`` when a rewrite happened, else ``(url, False)``. The
    ``/shopping`` homepage / category-browse form (no ``q``) is left untouched.
    """
    parts = urlsplit(url)
    if not (parts.path or "/").startswith("/shopping"):
        return url, False
    qs = parse_qs(parts.query)
    q = qs.get("q", [""])[0]
    if not q:
        return url, False
    new_query: list[tuple[str, str]] = [("q", q), ("udm", "28")]
    for key in ("gl", "hl"):
        val = qs.get(key, [""])[0]
        if val:
            new_query.append((key, val))
    new_url = urlunsplit(
        (parts.scheme, parts.netloc, "/search", urlencode(new_query), "")
    )
    return new_url, True


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

    # Rating/reviews are a Google product-cluster aggregate (identical across
    # every seller of the same product), NOT per-seller. Named with a
    # ``product_`` prefix so consumers don't mistake them for per-offer trust
    # signals; hoisted to the product group during _group_by_product.
    rating_match = re.search(r"Avaliado com (\d[,.]\d)\s+de\s+5", label)
    if rating_match:
        with contextlib.suppress(ValueError):
            product["product_rating"] = float(rating_match.group(1).replace(",", "."))

    review_match = re.search(r"([\d.,]+)\s*(mil)?\s*avaliações", label)
    if review_match:
        rc = _parse_review_count(review_match.group(1), bool(review_match.group(2)))
        if rc is not None:
            product["product_review_count"] = rc

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


def _card_image(card: Any) -> str | None:
    """First Google thumbnail (encrypted-tbn.gstatic.com) inside a card."""
    for src in card.xpath(".//img/@src"):
        if "gstatic" in src or "encrypted-tbn" in src:
            return str(src)
    return None


def _extract_offers(html_src: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Parse rendered Google Shopping HTML into (offers, filter_counts).

    Each offer is a single seller listing carrying ``position`` (1-based rank in
    Google's source order) and, when present, an ``image`` thumbnail. Offers are
    returned in source order — no price sort. ``filter_counts`` reports drops per
    reason: ``used``, ``international``, ``outlier`` (zero counts omitted).
    """
    from lxml import html as lxml_html  # local import: lxml is heavy

    tree = lxml_html.fromstring(html_src)
    cards = tree.xpath(".//product-viewer-entrypoint")
    if not cards:
        raise AdapterParseError(
            "no <product-viewer-entrypoint> cards found — site structure may "
            "have changed"
        )

    offers: list[dict[str, Any]] = []
    used_dropped = 0
    intl_dropped = 0
    position = 0

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
            position += 1
            parsed["position"] = position
            image = _card_image(card)
            if image:
                parsed["image"] = image
            offers.append(parsed)

    offers, outlier_dropped = _filter_outliers(offers)

    counts: dict[str, int] = {}
    if used_dropped:
        counts["used"] = used_dropped
    if intl_dropped:
        counts["international"] = intl_dropped
    if outlier_dropped:
        counts["outlier"] = outlier_dropped

    return offers, counts


# ---------------------------------------------------------------------------
# Grouping (same product across multiple sellers)
# ---------------------------------------------------------------------------


_SELLER_FIELDS = ("was_price_brl", "discount_pct", "badges", "other_stores")
_AGGREGATE_FIELDS = ("product_rating", "product_review_count", "image")


def _norm_title(title: str) -> str:
    """Normalization key for grouping. Conservative on purpose: lowercase +
    whitespace-collapse + trailing-punctuation strip, exact match only. This
    under-merges (distinct SKUs like "128GB"/"256GB" or seller title variants
    stay separate) — preferred over fuzzy matching, which risks merging
    genuinely different products."""
    return re.sub(r"\s+", " ", title.lower()).strip().rstrip(".,;:!?-–— ")


def _group_by_product(offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse offers of the same product into one entry with a ``sellers``
    list, preserving Google's source order via ``position`` (min rank in group).
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for off in offers:
        groups.setdefault(_norm_title(off["title"]), []).append(off)

    products: list[dict[str, Any]] = []
    for group in groups.values():
        by_pos = sorted(group, key=lambda o: o["position"])

        seen: set[tuple[str | None, float]] = set()
        sellers: list[dict[str, Any]] = []
        for o in group:
            store = o.get("store")
            key = (store, o["price_brl"])
            if key in seen:
                continue
            seen.add(key)
            seller: dict[str, Any] = {}
            if store is not None:
                seller["store"] = store
            seller["price_brl"] = o["price_brl"]
            for f in _SELLER_FIELDS:
                if f in o:
                    seller[f] = o[f]
            sellers.append(seller)
        sellers.sort(key=lambda s: s["price_brl"])

        prices = [s["price_brl"] for s in sellers]
        product: dict[str, Any] = {
            "title": by_pos[0]["title"],
            "position": by_pos[0]["position"],
            "price_brl": min(prices),
            "sellers": sellers,
        }
        if min(prices) != max(prices):
            product["price_range"] = [min(prices), max(prices)]
        for f in _AGGREGATE_FIELDS:
            for o in by_pos:
                if f in o:
                    product[f] = o[f]
                    break
        products.append(product)

    products.sort(key=lambda p: p["position"])
    return products


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
    parts.append(f"{len(products)} products (Google order)")
    parts.append("")
    for i, p in enumerate(products, 1):
        title = p["title"]
        price = f"R$ {p['price_brl']:.2f}".replace(".", ",")
        line = f"{i}. **{title}** — from {price}"
        extras: list[str] = []
        if p.get("price_range"):
            hi = f"R$ {p['price_range'][1]:.2f}".replace(".", ",")
            extras.append(f"up to {hi}")
        sellers = p.get("sellers", [])
        if sellers:
            names = ", ".join(s["store"] for s in sellers if s.get("store"))
            if names:
                extras.append(f"{len(sellers)} sellers: {names}")
        if p.get("product_rating") is not None:
            rating_bit = f"{p['product_rating']}/5"
            if p.get("product_review_count"):
                rating_bit += f" ({p['product_review_count']} product reviews)"
            extras.append(rating_bit)
        if extras:
            line += " — " + " — ".join(extras)
        parts.append(line)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_base_envelope, _failure_envelope = _common.envelope_builders(
    "google_shopping", "application/x-google-shopping"
)


async def fetch_google_shopping(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
    fetch_html: HtmlFetcher | None = None,
) -> dict[str, Any]:
    """Fetch a Google Shopping page and return a structured envelope.

    HTML is obtained via ``fetch_html`` — the main flow injects the shared
    escalation chain (seeded to the browser tier for Google routes). Without an
    injected fetcher it falls back to a direct browser fetch.
    """
    fetch_url, rewritten = _canonicalize_shopping_url(url)

    def _fail(
        reason: FailureReason, message: str, *, http_status: int = 0
    ) -> dict[str, Any]:
        # Keep url_requested as the original; reflect the rewrite on failures too
        # so callers see we actually fetched the working /search?udm=28 endpoint.
        env = _failure_envelope(url, reason, message, http_status=http_status)
        if rewritten:
            env["url_final"] = fetch_url
            env["url_canonical"] = fetch_url
            env["warnings"] = ["rewrote_shopping_search_to_udm28"]
        return env

    got = await _common.acquire_html(
        fetch_url, fetch_html=fetch_html, deadline=deadline, cfg=cfg, fail=_fail
    )
    if isinstance(got, dict):
        return got
    html_src, status, _mode_used = got

    try:
        offers, filter_counts = _extract_offers(html_src)
        products = _group_by_product(offers)
    except AdapterParseError as exc:
        log.warning("Google Shopping parse anchor missing: %s", exc)
        return _fail(
            FailureReason.PARSE_FAILED,
            f"google_shopping {exc}",
            http_status=status,
        )
    except Exception as exc:
        log.warning("Google Shopping HTML parse failed: %s", exc)
        return _fail(
            FailureReason.PARSE_FAILED,
            f"google_shopping parse failed: {type(exc).__name__}: {exc}",
            http_status=status,
        )

    query = _extract_query(fetch_url)
    markdown = _render_markdown(products, query=query)

    from .. import io as io_mod

    shopping = getattr(getattr(cfg, "adapters", None), "shopping", None)
    currency = getattr(shopping, "currency", None) or "BRL"
    language = getattr(shopping, "language", None) or "pt-BR"

    quality: dict[str, Any] = {
        "products": products,
        "currency": currency,
        "result_count": len(products),
        "offer_count": len(offers),
    }
    if query:
        quality["search_query"] = query
    if filter_counts:
        quality["filtered"] = filter_counts

    # Cards were present (else _extract_offers raised), so zero products means a
    # genuinely empty / fully-filtered result set — not scraper-rot.
    warnings = ["rewrote_shopping_search_to_udm28"] if rewritten else []
    if not products:
        warnings.append("no_results")

    env = envelope.success_envelope(
        base=_base_envelope(url, http_status=status or 200),
        markdown=markdown,
        metadata={
            "title": f"Google Shopping: {query}" if query else "Google Shopping",
            "byline": None,
            "published": None,
            "modified": None,
            "language": language,
            "site_name": "Google Shopping",
            "image": products[0].get("image") if products else None,
            "word_count": len(markdown.split()),
            "quality": quality,
            "warnings": warnings,
        },
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )
    if rewritten:
        env["url_final"] = fetch_url
        env["url_canonical"] = fetch_url
    return env
