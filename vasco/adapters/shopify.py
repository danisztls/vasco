# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Generic Shopify storefront adapter.

Every Shopify store — regardless of theme — exposes the same **platform-level
JSON endpoints** that return clean structured product data over the plain http
tier (no JS render, no theme scraping, no bot challenge on the data path):

- **Product** (``/products/<handle>``) → ``/products/<handle>.js`` — the full
  product object: variants (price/compare_at in **cents**, availability, sku),
  options, images, body description.
- **Collection** (``/collections/<handle>``) → ``/collections/<handle>/products.json``
  — an array of storefront product objects (variants with **decimal-string**
  prices, images, vendor, tags).
- **Search** (``/search?q=<q>``) → ``/search/suggest.json`` — Shopify's
  predictive-search API (decimal-string prices, capped at 10 results by the
  platform; the full ``/search`` HTML page is theme-dependent and not generically
  parseable, so this is the structured-search trade-off).

Because the endpoints are identical on every Shopify store, this adapter is
*generic*: the only per-store knowledge is **which domains are Shopify**. Two
match tiers feed the dispatcher:

- ``is_shopify_url(url, cfg)`` — *certain*: a ``*.myshopify.com`` host, or a
  registered domain in the known set (built-in seeds ∪ ``cfg.adapters.shopify.domains`` ∪
  domains confirmed by a prior probe), on a claimable path.
- ``is_shopify_candidate(url, cfg)`` — *probe-worthy*: an unknown domain on a
  product/collection page shape. The dispatcher runs ``fetch_shopify(probe=True)``;
  a miss raises :class:`NotShopify` (→ dispatcher falls through to a normal fetch,
  **not** a failure) and the domain is negative-memoized; a hit positive-memoizes
  the domain for the rest of the process lifetime.

Unlike the marketplace adapters (which parse JSON embedded in HTML), this one
fetches the JSON endpoints *directly* through the injected ``fetch_html`` (which
accepts any target URL and shares the http→browser escalation chain, minus the
wayback tail since adapters need live data), so a bot-protected Shopify store
still escalates naturally. Never raises — returns a failure envelope (or, in
probe mode, ``NotShopify``).
"""

from __future__ import annotations

import contextlib
import html as html_mod
import json
import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .. import envelope
from .. import urls as urls_mod
from ..errors import AdapterParseError, FailureReason
from . import _common
from ._common import HtmlFetcher
from ._common import compact as _compact
from ._common import host as _host

log = logging.getLogger(__name__)


# Domains known to be Shopify out of the box (no config required). Extended at
# runtime by cfg.adapters.shopify.domains and by positive probe results.
_SEED_DOMAINS: frozenset[str] = frozenset({"simwooddenim.com"})

# Shopify attribution params appended to product URLs by the predictive-search /
# collection endpoints — noise that shouldn't leak into the canonical product url.
_ATTRIBUTION_PARAMS: frozenset[str] = frozenset({"_pos", "_psq", "_ss", "_v"})

_GALLERY_CAP: int = 8
_TAG_RE = re.compile(r"<[^>]+>")

_PROVIDER = "shopify"  # adapter_probe key namespace

# In-process front for the probe verdict; the durable backing is the SQLite
# `adapter_probe` table (see Cache.get_probe/set_probe), shared by the CLI and
# vascod, so a domain is probed at most once ever (not once per process). The
# memo is seeded from the cache on first lookup and avoids a DB hit thereafter.
# `_currency_memo` stays in-process only (currency is cheap to re-fetch).
_probe_memo: dict[str, bool] = {}  # registered_domain -> is_shopify
_currency_memo: dict[str, str | None] = {}  # registered_domain -> currency code


def _reset_for_tests() -> None:
    """Clear the process-lifetime memos (probe + currency). Does not touch the
    persistent ``adapter_probe`` table — pass a fresh ``Cache`` in tests that
    exercise persistence."""
    _probe_memo.clear()
    _currency_memo.clear()


def _static_known(cfg: Any | None) -> frozenset[str]:
    """Domains known to be Shopify *without* a probe: built-in seeds + config."""
    shopify = getattr(getattr(cfg, "adapters", None), "shopify", None)
    extra = getattr(shopify, "domains", ()) or ()
    return _SEED_DOMAINS | {str(d).lower() for d in extra}


def _probe_state(domain: str, cache: Any | None) -> bool | None:
    """Cached probe verdict for ``domain``: ``True`` (Shopify), ``False`` (not),
    or ``None`` (unknown — never probed, or the verdict went stale). Reads the
    in-process memo first, then the persistent ``adapter_probe`` table (seeding
    the memo so later lookups in the same process skip the DB)."""
    if domain in _probe_memo:
        return _probe_memo[domain]
    if cache is not None and hasattr(cache, "get_probe"):
        try:
            val = cache.get_probe(_PROVIDER, domain)
        except Exception:  # a cache hiccup must never break detection
            val = None
        if val is not None:
            _probe_memo[domain] = val
            return val
    return None


def _set_probe(domain: str, value: bool, cache: Any | None) -> None:
    """Record a probe verdict in both the in-process memo and the persistent
    ``adapter_probe`` table (best-effort — a cache write failure is swallowed)."""
    _probe_memo[domain] = value
    if cache is not None and hasattr(cache, "set_probe"):
        with contextlib.suppress(Exception):
            cache.set_probe(_PROVIDER, domain, value)


class NotShopify(Exception):
    """A probe of a candidate URL did not confirm a Shopify store.

    Raised by ``fetch_shopify(probe=True)`` when the platform JSON endpoint is
    absent or not Shopify-shaped. The dispatcher catches it and **falls through
    to a normal fetch** — a probe miss is not a failure.
    """


# ---------------------------------------------------------------------------
# URL detection / endpoint mapping
# ---------------------------------------------------------------------------


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme or 'https'}://{parts.netloc}"


def _segments(url: str) -> list[str]:
    return [s for s in (urlsplit(url).path or "").split("/") if s]


def _is_myshopify(url: str) -> bool:
    host = _host(url)
    return host == "myshopify.com" or host.endswith(".myshopify.com")


# Endpoint "kinds" — each names a payload shape the parser knows how to unwrap.
_PRODUCT_JS = "product_js"  # bare product object, prices in cents
_PRODUCT_JSON = "product_json"  # {"product": {...}}, prices in cents
_COLLECTION = "collection"  # {"products": [...]}, decimal-string prices
_SUGGEST = "suggest"  # {"resources": {"results": {"products": [...]}}}


def _collection_limit(cfg: Any | None) -> int:
    shopify = getattr(getattr(cfg, "adapters", None), "shopify", None)
    n = getattr(shopify, "collection_limit", 250)
    try:
        return max(1, min(250, int(n)))
    except (TypeError, ValueError):
        return 250


def _with_query(base_url: str, params: dict[str, Any]) -> str:
    parts = urlsplit(base_url)
    existing = dict(parse_qsl(parts.query, keep_blank_values=True))
    existing.update({k: str(v) for k, v in params.items() if v is not None})
    return urlunsplit(parts._replace(query=urlencode(existing)))


def _claim(url: str, cfg: Any | None) -> tuple[str, str, str] | None:
    """Map a URL to ``(page_type, endpoint, kind)`` or ``None`` if not claimable.

    ``page_type`` is the envelope label (product/collection/search); ``endpoint``
    is the JSON URL to fetch; ``kind`` selects the payload unwrap shape. Theme
    paths with no platform-JSON equivalent (homepage, /pages, /blogs, tag-filtered
    collections, …) return ``None`` so they fall through to a normal fetch.
    """
    parts = urlsplit(url)
    segs = _segments(url)
    origin = _origin(url)
    limit = _collection_limit(cfg)

    # --- product ------------------------------------------------------------
    if len(segs) == 2 and segs[0] == "products":
        handle = segs[1]
        if handle.endswith(".js"):
            return "product", url, _PRODUCT_JS
        if handle.endswith(".json"):
            return "product", url, _PRODUCT_JSON
        return "product", f"{origin}/products/{handle}.js", _PRODUCT_JS

    # --- collection ---------------------------------------------------------
    if len(segs) == 2 and segs[0] == "collections":
        handle = segs[1]
        if handle.endswith(".json"):  # /collections/<h>.json is metadata, not items
            return None
        page = dict(parse_qsl(parts.query)).get("page")
        ep = _with_query(
            f"{origin}/collections/{handle}/products.json",
            {"limit": limit, "page": page},
        )
        return "collection", ep, _COLLECTION
    if len(segs) == 3 and segs[0] == "collections" and segs[2] == "products.json":
        return "collection", _with_query(url, {"limit": limit}), _COLLECTION
    # /collections/<handle>/<tag> — tag-filtered, no JSON equivalent → not claimed.

    # --- bare all-products listing -----------------------------------------
    if len(segs) == 1 and segs[0] == "products.json":
        return "collection", _with_query(url, {"limit": limit}), _COLLECTION

    # --- search -------------------------------------------------------------
    if segs == ["search", "suggest.json"]:
        return "search", url, _SUGGEST
    if segs == ["search"]:
        q = dict(parse_qsl(parts.query)).get("q")
        if not q:
            return None
        ep = _with_query(
            f"{origin}/search/suggest.json",
            {"q": q, "resources[type]": "product", "resources[limit]": 10},
        )
        return "search", ep, _SUGGEST

    return None


def is_shopify_url(url: str, cfg: Any | None = None, cache: Any | None = None) -> bool:
    """Certain match: a known/myshopify host on a claimable Shopify path. A domain
    a prior probe confirmed (in-process memo or the persistent ``adapter_probe``
    table via ``cache``) counts as known too."""
    if not url:
        return False
    if _claim(url, cfg) is None:
        return False
    if _is_myshopify(url):
        return True
    dom = urls_mod.registered_domain(url)
    if dom in _static_known(cfg):
        return True
    return _probe_state(dom, cache) is True


def _candidate_shape(url: str) -> bool:
    """A human-facing product/collection *page* shape worth probing (not a direct
    .js/.json endpoint, not /search — too generic across platforms)."""
    segs = _segments(url)
    if len(segs) != 2:
        return False
    if segs[0] == "products":
        return not (segs[1].endswith(".js") or segs[1].endswith(".json"))
    if segs[0] == "collections":
        return not segs[1].endswith(".json")
    return False


def is_shopify_candidate(
    url: str, cfg: Any | None = None, cache: Any | None = None
) -> bool:
    """Probe-worthy: autodetect on, product/collection page shape, and the domain
    has *no* cached verdict yet. A confirmed (True) or rejected (False) verdict —
    from the in-process memo or the persistent ``adapter_probe`` table — means we
    don't probe: True is handled as certain by ``is_shopify_url``, False stays a
    plain fetch. So a domain is probed at most once across all processes."""
    if not url or not _candidate_shape(url):
        return False
    if not getattr(
        getattr(getattr(cfg, "adapters", None), "shopify", None), "autodetect", True
    ):
        return False
    dom = urls_mod.registered_domain(url)
    if dom in _static_known(cfg):  # already certain → not a *candidate*
        return False
    return _probe_state(dom, cache) is None


# ---------------------------------------------------------------------------
# Value normalization helpers (pure)
# ---------------------------------------------------------------------------


def _money(value: Any, *, cents: bool) -> float | None:
    """Normalize a Shopify price to a float. ``.js`` gives integer **cents**;
    ``products.json`` / ``suggest`` give a **decimal string**."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value) / 100.0 if cents else float(value)
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            v = float(s)
        except ValueError:
            return None
        if cents:
            v /= 100.0
    else:
        return None
    return round(v, 2)


def _abs_img(value: Any) -> str | None:
    """Absolutize an image reference (string URL, or {src|url} object).
    Protocol-relative ``//cdn…`` becomes ``https://cdn…``."""
    if isinstance(value, dict):
        value = value.get("src") or value.get("url")
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.startswith("//"):
        return "https:" + s
    return s


def _images(value: Any, limit: int = _GALLERY_CAP) -> list[str]:
    if isinstance(value, (str, dict)):
        value = [value]
    seen: set[str] = set()
    out: list[str] = []
    for item in value or []:
        u = _abs_img(item)
        if u and u not in seen:
            seen.add(u)
            out.append(u)
            if len(out) >= limit:
                break
    return out


def _strip_html(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = html_mod.unescape(_TAG_RE.sub(" ", value))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _option_names(options: Any) -> list[str]:
    out: list[str] = []
    for o in options or []:
        if isinstance(o, dict) and isinstance(o.get("name"), str):
            out.append(o["name"])
        elif isinstance(o, str):
            out.append(o)
    return out


def _options_detail(options: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for o in options or []:
        if isinstance(o, dict) and o.get("name"):
            values = [v for v in (o.get("values") or []) if isinstance(v, str)]
            out.append({"name": str(o["name"]), "values": values})
    return out


def _clean_url(raw: Any, origin: str) -> str | None:
    """Absolutize a product url path and drop Shopify attribution params.

    Stripping happens here (adapter-local), never in ``urls.normalize_url`` —
    keeping the cache key untouched avoids invalidating every cached entry.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    absolute = urljoin(origin + "/", raw.strip())
    parts = urlsplit(absolute)
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in _ATTRIBUTION_PARAMS
    ]
    return urlunsplit(parts._replace(query=urlencode(kept), fragment=""))


def _pid(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return str(int(value))
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# ---------------------------------------------------------------------------
# Per-shape product parsers
# ---------------------------------------------------------------------------


def _variant_prices(variants: Any, *, cents: bool) -> list[tuple[float, float | None]]:
    """[(price, compare_at)] for each variant with a usable price."""
    out: list[tuple[float, float | None]] = []
    for v in variants or []:
        if not isinstance(v, dict):
            continue
        price = _money(v.get("price"), cents=cents)
        if price is None:
            continue
        out.append((price, _money(v.get("compare_at_price"), cents=cents)))
    return out


def _listing_product(
    obj: dict[str, Any], origin: str, position: int, *, cents: bool
) -> dict[str, Any] | None:
    """A collection/search card → a normalized product dict. ``cents`` reflects
    the endpoint's price encoding (collection/suggest are decimal strings)."""
    title = obj.get("title")
    if not isinstance(title, str) or not title.strip():
        return None

    # Top-level price (suggest carries it) else derive from variants (collection).
    price = _money(obj.get("price"), cents=cents)
    original = _money(obj.get("compare_at_price"), cents=cents)
    variants = obj.get("variants") if isinstance(obj.get("variants"), list) else []
    if price is None and variants:
        vp = _variant_prices(variants, cents=cents)
        if vp:
            price, original = min(vp, key=lambda t: t[0])

    available = obj.get("available")
    if available is None and variants:
        available = any(
            bool(v.get("available")) for v in variants if isinstance(v, dict)
        )

    image = _abs_img(obj.get("featured_image")) or _abs_img(obj.get("image"))
    if image is None:
        imgs = _images(obj.get("images"), limit=1)
        image = imgs[0] if imgs else None

    handle = obj.get("handle")
    url = _clean_url(obj.get("url"), origin) or (
        f"{origin}/products/{handle}" if isinstance(handle, str) and handle else None
    )

    return _compact(
        {
            "position": position,
            "title": title.strip(),
            "url": url,
            "product_id": _pid(obj.get("id")),
            "handle": handle if isinstance(handle, str) else None,
            "price": price,
            "original_price": original
            if (original and price and original > price)
            else None,
            "brand": obj.get("vendor") if isinstance(obj.get("vendor"), str) else None,
            "product_type": (obj.get("type") or obj.get("product_type")) or None,
            "tags": [t for t in (obj.get("tags") or []) if isinstance(t, str)],
            "available": bool(available) if available is not None else None,
            "image": image,
        }
    )


def _detail_product(obj: dict[str, Any], origin: str, *, cents: bool) -> dict[str, Any]:
    """A full product object (``.js`` / ``.json``) → a rich product dict."""
    variants_raw = obj.get("variants") if isinstance(obj.get("variants"), list) else []
    vp = _variant_prices(variants_raw, cents=cents)
    price = _money(obj.get("price"), cents=cents)
    original = _money(obj.get("compare_at_price"), cents=cents)
    if price is None and vp:
        price, original = min(vp, key=lambda t: t[0])

    variants: list[dict[str, Any]] = []
    for v in variants_raw:
        if not isinstance(v, dict):
            continue
        variants.append(
            _compact(
                {
                    "id": _pid(v.get("id")),
                    "title": v.get("title")
                    if isinstance(v.get("title"), str)
                    else None,
                    "sku": v.get("sku") if isinstance(v.get("sku"), str) else None,
                    "price": _money(v.get("price"), cents=cents),
                    "available": bool(v.get("available"))
                    if v.get("available") is not None
                    else None,
                }
            )
        )

    handle = obj.get("handle")
    url = _clean_url(obj.get("url"), origin) or (
        f"{origin}/products/{handle}" if isinstance(handle, str) and handle else None
    )
    images = _images(obj.get("images"))
    image = _abs_img(obj.get("featured_image")) or (images[0] if images else None)

    available = obj.get("available")
    if available is None and variants_raw:
        available = any(
            bool(v.get("available")) for v in variants_raw if isinstance(v, dict)
        )

    return _compact(
        {
            "position": 1,
            "title": (obj.get("title") or "").strip() or None
            if isinstance(obj.get("title"), str)
            else None,
            "url": url,
            "product_id": _pid(obj.get("id")),
            "handle": handle if isinstance(handle, str) else None,
            "price": price,
            "original_price": original
            if (original and price and original > price)
            else None,
            "brand": obj.get("vendor") if isinstance(obj.get("vendor"), str) else None,
            "product_type": (obj.get("type") or obj.get("product_type")) or None,
            "tags": [t for t in (obj.get("tags") or []) if isinstance(t, str)],
            "available": bool(available) if available is not None else None,
            "image": image,
            "images": images,
            "description": _strip_html(obj.get("description") or obj.get("body_html")),
            "options": _options_detail(obj.get("options")),
            "variants": variants,
            "published_at": obj.get("published_at")
            if isinstance(obj.get("published_at"), str)
            else None,
        }
    )


def _parse(body: str, kind: str, url: str) -> tuple[str, list[dict[str, Any]]]:
    """Unwrap a payload to ``(page_type, products)``.

    Raises :class:`AdapterParseError` when the structural anchor for ``kind`` is
    missing (the body isn't a Shopify endpoint of that shape) — scraper-rot. An
    anchor present but empty is a legitimate empty result (returns ``[]``).
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AdapterParseError(f"{kind}: response was not JSON") from exc
    origin = _origin(url)

    if kind in (_PRODUCT_JS, _PRODUCT_JSON):
        obj = data.get("product") if kind == _PRODUCT_JSON else data
        if not isinstance(obj, dict) or not (obj.get("id") or obj.get("variants")):
            raise AdapterParseError(
                "product endpoint: no Shopify product object — not a Shopify store "
                "or its markup changed"
            )
        return "product", [_detail_product(obj, origin, cents=(kind == _PRODUCT_JS))]

    if kind == _COLLECTION:
        items = data.get("products")
        if not isinstance(items, list):
            raise AdapterParseError(
                "collection endpoint: no `products` array — not a Shopify endpoint"
            )
        out = _parse_listing(items, origin, cents=False)
        return "collection", out

    # _SUGGEST
    try:
        items = data["resources"]["results"]["products"]
    except (KeyError, TypeError):
        items = None
    if not isinstance(items, list):
        raise AdapterParseError(
            "search endpoint: no predictive `products` array — not a Shopify endpoint"
        )
    return "search", _parse_listing(items, origin, cents=False)


def _parse_listing(
    items: list[Any], origin: str, *, cents: bool
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    position = 0
    for obj in items:
        if not isinstance(obj, dict):
            continue
        position += 1
        parsed = _listing_product(obj, origin, position, cents=cents)
        if parsed is not None:
            out.append(parsed)
    return out


# ---------------------------------------------------------------------------
# Currency (best-effort, per-domain memoized via /cart.js)
# ---------------------------------------------------------------------------


async def _currency(url: str, fetch: HtmlFetcher | None) -> str | None:
    """Shopify omits currency from the product endpoints; ``/cart.js`` carries it.
    Best-effort and memoized per registered domain — ``None`` on any failure."""
    dom = urls_mod.registered_domain(url)
    if dom in _currency_memo:
        return _currency_memo[dom]
    result: str | None = None
    if fetch is not None:
        try:
            body, _status, _headers, reason, _mode = await fetch(
                f"{_origin(url)}/cart.js"
            )
            if reason == FailureReason.OK and body:
                code = json.loads(body).get("currency")
                result = code if isinstance(code, str) and code else None
        except Exception as exc:  # network / JSON / anything: currency is optional
            log.debug("shopify cart.js currency lookup failed for %s: %s", dom, exc)
    _currency_memo[dom] = result
    return result


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _fmt_price(price: Any, currency: str | None) -> str:
    if price is None:
        return "—"
    body = (
        f"{price:,.2f}"
        if isinstance(price, float) and not float(price).is_integer()
        else f"{int(price):,}"
    )
    return f"{currency} {body}" if currency else body


def _render_markdown(
    products: list[dict[str, Any]], *, page_type: str, currency: str | None, shop: str
) -> str:
    if not products:
        return f"# {shop}\n\nNo products found."
    parts: list[str] = []
    if page_type != "product":
        parts.append(f"{len(products)} products")
        parts.append("")
    for i, p in enumerate(products, 1):
        head = (
            f"{i}. **{p.get('title', '?')}** — {_fmt_price(p.get('price'), currency)}"
        )
        extras: list[str] = []
        if p.get("original_price"):
            extras.append(f"was {_fmt_price(p['original_price'], currency)}")
        if p.get("brand"):
            extras.append(str(p["brand"]))
        if p.get("available") is False:
            extras.append("sold out")
        if extras:
            head += " — " + " · ".join(extras)
        parts.append(head)
        if page_type == "product" and p.get("description"):
            parts.append("")
            parts.append(str(p["description"]))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Fetch + envelope
# ---------------------------------------------------------------------------


_base_envelope, _failure_envelope = _common.envelope_builders(
    "shopify", "application/x-shopify"
)


async def fetch_shopify(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
    fetch_html: HtmlFetcher | None = None,
    cache: Any | None = None,
    probe: bool = False,
) -> dict[str, Any]:
    """Fetch a Shopify product/collection/search page → a structured envelope.

    The JSON endpoint is obtained via ``fetch_html`` (the shared escalation
    chain). When ``probe=True`` (an unconfirmed candidate domain), any sign the
    URL isn't a Shopify endpoint raises :class:`NotShopify` so the dispatcher can
    fall through to a normal fetch; a hit confirms the domain. The verdict is
    persisted via ``cache`` (the ``adapter_probe`` table) so the domain is never
    re-probed in a later process.
    """
    claim = _claim(url, cfg)
    if claim is None:  # defensive — dispatch only calls us on a claimable URL
        if probe:
            raise NotShopify(f"{url}: not a claimable Shopify path")
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, "shopify: unrecognized URL shape"
        )
    page_type, endpoint, kind = claim
    dom = urls_mod.registered_domain(url)

    if fetch_html is None:
        if probe:
            raise NotShopify("shopify probe requires an injected fetcher")
        return _failure_envelope(
            url, FailureReason.SERVER_ERROR, "shopify: no HTML fetcher injected"
        )

    # --- fetch the endpoint -------------------------------------------------
    try:
        body, status, _headers, reason, mode_used = await fetch_html(endpoint)
    except TimeoutError:
        if probe:
            raise NotShopify("shopify probe timed out") from None
        return _failure_envelope(url, FailureReason.TIMEOUT, "fetch deadline elapsed")
    except Exception as exc:
        if probe:
            raise NotShopify(f"shopify probe fetch failed: {exc}") from exc
        return _failure_envelope(
            url,
            FailureReason.SERVER_ERROR,
            f"fetch failed: {type(exc).__name__}: {exc}",
        )

    if reason != FailureReason.OK or not body:
        if probe:
            # 404/other on a candidate is ambiguous (bad handle vs. not Shopify):
            # don't negative-memo the domain; just fall through for this URL.
            raise NotShopify(f"shopify probe endpoint returned {reason}")
        return _failure_envelope(
            url, reason, f"fetch failed via {mode_used} tier", http_status=status
        )

    # --- parse --------------------------------------------------------------
    try:
        page_type, products = _parse(body, kind, url)
    except AdapterParseError as exc:
        if probe:
            # A 200 that isn't Shopify-shaped is a definitive "not Shopify" →
            # negative-memo (persisted) so we never re-probe this domain.
            _set_probe(dom, False, cache)
            raise NotShopify(str(exc)) from exc
        log.warning("shopify parse anchor missing (%s): %s", kind, exc)
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, f"shopify {exc}", http_status=status
        )
    except Exception as exc:
        if probe:
            raise NotShopify(f"shopify probe parse error: {exc}") from exc
        log.warning("shopify parse failed (%s): %s", kind, exc)
        return _failure_envelope(
            url,
            FailureReason.PARSE_FAILED,
            f"shopify parse failed: {type(exc).__name__}: {exc}",
            http_status=status,
        )

    # A detail page parsing to zero items is always rot (it must yield one).
    if page_type == "product" and not products:
        if probe:
            _set_probe(dom, False, cache)
            raise NotShopify("shopify probe: product endpoint yielded no product")
        return _failure_envelope(
            url,
            FailureReason.PARSE_FAILED,
            "shopify product endpoint yielded no product — markup changed",
            http_status=status,
        )

    # Confirmed Shopify (probe or not). Persist so the domain is known next run —
    # but only for probe-discovered domains; seed/config domains are already
    # statically known and never consult the probe table, so a row would be dead
    # weight. Re-writing a probe-confirmed domain also refreshes its TTL.
    if dom not in _static_known(cfg):
        _set_probe(dom, True, cache)

    from .. import io as io_mod

    currency = await _currency(url, fetch_html)
    warnings = (
        ["no_results"] if page_type in ("collection", "search") and not products else []
    )
    markdown = _render_markdown(
        products, page_type=page_type, currency=currency, shop=dom
    )
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query)).get("q") if page_type == "search" else None
    collection = (
        _segments(url)[1]
        if page_type == "collection" and len(_segments(url)) >= 2
        else None
    )
    title = (
        products[0].get("title")
        if page_type == "product" and products
        else f"{dom}: {len(products)} products"
    )

    quality = _compact(
        {
            "provider": "shopify",
            "shop": dom,
            "page_type": page_type,
            "collection": collection,
            "query": query,
            "currency": currency,
            "result_count": len(products),
            "products": products,
        }
    )
    return envelope.success_envelope(
        base=_base_envelope(url, http_status=status or 200),
        markdown=markdown,
        metadata={
            "title": title,
            "byline": None,
            "published": products[0].get("published_at") if products else None,
            "modified": None,
            "language": None,
            "site_name": dom,
            "image": products[0].get("image") if products else None,
            "word_count": len(markdown.split()),
            "quality": quality,
            "warnings": warnings,
        },
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )
