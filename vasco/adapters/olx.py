# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OLX.com.br classifieds adapter (real-estate + vehicle verticals).

OLX is Brazil's dominant classifieds marketplace. Like the other structured-data
adapters, the useful payload is embedded JSON that the default trafilatura
pipeline flattens to lossy prose. OLX exposes it cleanly:

- **List/search pages** (``www.olx.com.br/<vertical>/...``) embed a Next.js
  ``<script id="__NEXT_DATA__">`` blob with ``props.pageProps.ads[]``.
- **Detail/ad pages** (``<region>.olx.com.br/.../<slug>-<listId>``) use a
  different stack: a ``<script id="initial-data" data-json="...">`` blob (the
  JSON is HTML-attribute-escaped; BeautifulSoup decodes it on read) carrying
  ``.ad``, with a schema.org JSON-LD block as a secondary source.

Every ad carries a category-agnostic ``properties[]`` name/value array. We
normalize the two highest-value verticals — **real estate** (``imóveis``) and
**vehicles** (``autos-e-pecas``) — into typed ``attributes``; other categories
are not matched by ``is_olx_url`` and fall through to the normal fetch path.

Public surface:
- ``is_olx_url(url)`` — match an OLX real-estate or vehicle URL.
- ``fetch_olx(url, *, deadline, cfg=None, fetch_html=None)`` — return a v0.1
  envelope (``mode_used="olx"``, ``content_type="application/x-olx"``); never
  raises — returns a failure envelope on any fetch/parse failure.

OLX sits behind Cloudflare, so the plain http tier is challenged (403);
``vasco/strategy.py`` seeds ``olx.com.br`` to the **browser** tier (like Google
Shopping) so the chain skips the guaranteed-failing http attempt. HTML is still
obtained through the shared escalation chain via the injected ``fetch_html`` —
the seed only picks the *starting* tier; learning can still flip it.

Bare category-landing **hub** pages (``/imoveis/estado-sp``, ``/autos-e-pecas``)
are App-Router navigation pages with no embedded listing JSON and nothing to
extract. They are matched (``is_olx_url`` stays true) but short-circuit to a
``CATEGORY_LANDING`` failure — a clear, accurate signal ("not a listing page;
narrow the URL") distinct from ``PARSE_FAILED`` (scraper-rot) — *before* any
fetch, since the hub is a stable property of the URL shape (``_is_category_hub``).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
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
    as_int as _as_int,
)
from ._common import (
    brl_int as _brl_int,
)
from ._common import (
    dedup as _dedup,
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

_GALLERY_CAP: int = 6

# path segment → normalized vertical key
_VERTICAL_SEGMENTS: dict[str, str] = {
    "imoveis": "realestate",
    "autos-e-pecas": "vehicles",
}


# ---------------------------------------------------------------------------
# URL detection / routing
# ---------------------------------------------------------------------------


def _vertical(url: str) -> str | None:
    """Return the OLX vertical for a URL, or None if unsupported.

    Both list (``www.olx.com.br/imoveis/...``) and detail
    (``sp.olx.com.br/<region>/autos-e-pecas/...-<id>``) URLs embed the vertical
    as a path segment, so a single segment check covers both page types.
    """
    host = _host(url)
    if not (host == "olx.com.br" or host.endswith(".olx.com.br")):
        return None
    segments = [s for s in (urlsplit(url).path or "").lower().split("/") if s]
    for seg in segments:
        if seg in _VERTICAL_SEGMENTS:
            return _VERTICAL_SEGMENTS[seg]
    return None


def _is_category_hub(url: str) -> bool:
    """Bare OLX category-landing hub pages (App-Router, no listings).

    These render promo carousels + filter navigation, not a search-results ad
    list, and ship no ``__NEXT_DATA__``. Verified shapes (www host only)::

        /imoveis            /autos-e-pecas            -> 0 trailing segments
        /imoveis/estado-sp  /autos-e-pecas/estado-sp  -> single state-location seg

    Any further refinement (transaction type ``/venda``/``/aluguel``, a
    subcategory, or a deeper location drill-down) is a real Pages-Router listing
    and is **not** a hub. Restricted to the www/bare host; regional subdomains
    carry detail + region lists and stay matched.
    """
    if _host(url) not in ("www.olx.com.br", "olx.com.br"):
        return False
    segs = [s for s in (urlsplit(url).path or "").lower().split("/") if s]
    if not segs or segs[0] not in _VERTICAL_SEGMENTS:
        return False
    rest = segs[1:]
    if not rest:
        return True
    return len(rest) == 1 and re.fullmatch(r"estado-[a-z]{2}", rest[0]) is not None


def is_olx_url(url: str) -> bool:
    return bool(url) and _vertical(url) is not None


def _page_type(url: str) -> str:
    """A detail page's path ends with the long numeric ``listId`` (``...-1483248894``)."""
    path = (urlsplit(url).path or "").rstrip("/")
    return "detail" if re.search(r"-\d{6,}$", path) else "list"


# ---------------------------------------------------------------------------
# Normalized listing + small parsing helpers
# ---------------------------------------------------------------------------

_LISTING_FIELDS = (
    "url",
    "title",
    "price",
    "old_price",
    "category",
    "vertical",
    "neighborhood",
    "municipality",
    "uf",
    "image",
    "images",
    "description",
    "date",
    "attributes",
)


def _listing(**kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = dict.fromkeys(_LISTING_FIELDS)
    out["images"] = []
    out["attributes"] = {}
    out.update({k: v for k, v in kw.items() if k in _LISTING_FIELDS})
    if out.get("images") and not out.get("image"):
        out["image"] = out["images"][0]
    return out


def _split_list(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _strip_html(value: Any) -> str | None:
    """Render an ad ``body`` (``<br>``-delimited HTML) to plain text."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = _soup(text).get_text()
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


# ---------------------------------------------------------------------------
# Per-vertical property normalization (olx property name → attribute key + parser)
# ---------------------------------------------------------------------------

_REALESTATE_ATTRS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "real_estate_type": ("type", str),
    "size": ("area", _as_int),
    "rooms": ("bedrooms", _as_int),
    "bathrooms": ("bathrooms", _as_int),
    "garage_spaces": ("parking", _as_int),
    "condominio": ("condo_fee", _brl_int),
    "iptu": ("iptu", _brl_int),
    "re_features": ("amenities", _split_list),
}

_VEHICLE_ATTRS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "vehicle_brand": ("brand", str),
    "vehicle_model": ("model", str),
    "cartype": ("cartype", str),
    "regdate": ("year", _as_int),
    "mileage": ("mileage", _as_int),
    "motorpower": ("motorpower", str),  # decimal liters; keep as string
    "fuel": ("fuel", str),
    "gearbox": ("gearbox", str),
    "carcolor": ("color", str),
    "doors": ("doors", _as_int),
    "car_features": ("features", _split_list),
}

_ATTR_MAPS = {"realestate": _REALESTATE_ATTRS, "vehicles": _VEHICLE_ATTRS}


def _attributes(props: dict[str, Any], vertical: str) -> dict[str, Any]:
    """Lift the vertical's known ``properties`` into typed attributes.

    Only present, non-empty properties land in the output, so consumers see
    "what's known" rather than every key with possible nulls."""
    out: dict[str, Any] = {}
    for olx_name, (key, parser) in _ATTR_MAPS.get(vertical, {}).items():
        if olx_name not in props:
            continue
        parsed = parser(props[olx_name])
        if parsed is None or parsed == [] or parsed == "":
            continue
        out[key] = parsed
    return out


# ---------------------------------------------------------------------------
# Raw-ad extraction (list __NEXT_DATA__ / detail initial-data / JSON-LD)
# ---------------------------------------------------------------------------


def _parse_next_data(html: str) -> list[dict[str, Any]]:
    """List pages: ``props.pageProps.ads[]`` from the Next.js data blob.

    Raises ``AdapterParseError`` when the ``__NEXT_DATA__`` anchor is absent or
    unparseable (scraper-rot); an empty-but-present ``ads`` array returns ``[]``
    (a genuinely empty result page).
    """
    soup = _soup(html)
    tag = soup.find("script", id="__NEXT_DATA__")
    if not (tag and tag.string):
        raise AdapterParseError(
            "list page: <script id='__NEXT_DATA__'> not found — site structure "
            "may have changed"
        )
    try:
        data = json.loads(tag.string)
    except json.JSONDecodeError as exc:
        raise AdapterParseError(
            f"list page: __NEXT_DATA__ JSON malformed ({exc})"
        ) from exc
    ads = (((data.get("props") or {}).get("pageProps") or {}).get("ads")) or []
    return [a for a in ads if isinstance(a, dict)]


def _extract_detail_ad(html: str) -> dict[str, Any] | None:
    """Detail pages: the ``.ad`` object from ``<script id="initial-data">``.

    The JSON lives in the tag's ``data-json`` attribute (HTML-entity-escaped);
    BeautifulSoup returns it already decoded.
    """
    soup = _soup(html)
    tag = soup.find("script", id="initial-data")
    if not tag:
        return None
    blob = tag.get("data-json") or (tag.string or "")
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    ad = data.get("ad") if isinstance(data, dict) else None
    return ad if isinstance(ad, dict) else None


def _jsonld_detail(html: str, url: str, vertical: str) -> dict[str, Any] | None:
    """Fallback for detail pages lacking ``initial-data``: build a thin listing
    from the schema.org ``Offer``/product JSON-LD (title, price, images, body)."""
    for obj in _jsonld_objects(html):
        offer = obj.get("makesOffer") if obj.get("@type") == "Person" else obj
        if not isinstance(offer, dict):
            continue
        item = offer.get("itemOffered") if offer.get("@type") == "Offer" else offer
        if not isinstance(item, dict):
            continue
        spec = (
            (offer.get("priceSpecification") or {}) if isinstance(offer, dict) else {}
        )
        price = _brl_int(spec.get("price"))
        images = _dedup(
            (
                img.get("contentUrl")
                for img in (item.get("image") or [])
                if isinstance(img, dict)
            ),
            _GALLERY_CAP,
        )
        if not (item.get("name") or price or images):
            continue
        return _listing(
            url=url,
            title=item.get("name") or None,
            price=price,
            vertical=vertical,
            description=_strip_html(item.get("description")),
            images=images,
        )
    return None


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _location_obj(ad: dict[str, Any]) -> dict[str, Any]:
    """Structured location: list ads carry ``locationDetails`` (and a string
    ``location``); detail ads carry a ``location`` object. Prefer whichever is
    a dict."""
    for key in ("locationDetails", "location"):
        val = ad.get(key)
        if isinstance(val, dict):
            return val
    return {}


def _images(ad: dict[str, Any], limit: int) -> list[str]:
    return _dedup(
        (
            img.get("original")
            for img in (ad.get("images") or [])
            if isinstance(img, dict)
        ),
        limit,
    )


def _normalize_ad(
    ad: dict[str, Any], *, vertical: str, page_type: str, base_url: str
) -> dict[str, Any]:
    props = {
        p["name"]: p.get("value")
        for p in (ad.get("properties") or [])
        if isinstance(p, dict) and p.get("name")
    }
    loc = _location_obj(ad)
    is_detail = page_type == "detail"
    return _listing(
        url=ad.get("url")
        or ad.get("canonicalUrl")
        or ad.get("friendlyUrl")
        or base_url,
        title=ad.get("subject") or None,
        price=_brl_int(ad.get("priceValue")),
        old_price=_brl_int(ad.get("oldPrice")),
        category=ad.get("categoryName") or props.get("category"),
        vertical=vertical,
        neighborhood=loc.get("neighbourhood") or None,
        municipality=loc.get("municipality") or None,
        uf=loc.get("uf") or None,
        description=_strip_html(ad.get("body")) if is_detail else None,
        date=ad.get("date") or ad.get("listTime"),
        images=_images(ad, _GALLERY_CAP if is_detail else 1),
        attributes=_attributes(props, vertical),
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _fmt_price(price: Any) -> str:
    return f"R$ {int(price):,}".replace(",", ".") if price else "Sob consulta"


def _re_specs(a: dict[str, Any]) -> list[str]:
    return [
        s
        for s in (
            f"{a['area']}m²" if a.get("area") else None,
            f"{a['bedrooms']} quartos" if a.get("bedrooms") else None,
            f"{a['bathrooms']} ban." if a.get("bathrooms") else None,
            f"{a['parking']} vaga" if a.get("parking") else None,
        )
        if s
    ]


def _vehicle_specs(a: dict[str, Any]) -> list[str]:
    return [
        s
        for s in (
            str(a["year"]) if a.get("year") else None,
            f"{int(a['mileage']):,} km".replace(",", ".") if a.get("mileage") else None,
            a.get("fuel"),
            a.get("gearbox"),
        )
        if s
    ]


def _render_markdown(listings: list[dict], vertical: str) -> str:
    specs_fn = _vehicle_specs if vertical == "vehicles" else _re_specs
    parts: list[str] = []
    for i, ln in enumerate(listings, 1):
        specs = specs_fn(ln.get("attributes") or {})
        loc = ", ".join(
            s for s in (ln.get("neighborhood"), ln.get("municipality")) if s
        )
        head = " · ".join(
            x
            for x in (ln.get("title"), _fmt_price(ln.get("price")), " · ".join(specs))
            if x
        )
        line = f"{i}. {head}"
        if loc:
            line += f" — {loc}"
        parts.append(line)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Fetch + envelope
# ---------------------------------------------------------------------------

_base_envelope, _failure_envelope = _common.envelope_builders(
    "olx", "application/x-olx"
)


async def fetch_olx(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
    fetch_html: HtmlFetcher | None = None,
) -> dict[str, Any]:
    """Fetch an OLX list/detail page and return a structured envelope.

    HTML is obtained via ``fetch_html`` — the main flow injects the shared
    ``http → browser → mobile`` escalation chain — no wayback tail, since OLX
    resolves at the cheap http tier and an archived listing would be stale.
    Without an injected fetcher it falls back to a browser-only fetch.
    """
    vertical = _vertical(url)
    if vertical is None:  # pragma: no cover - routing guards this
        return _failure_envelope(
            url, FailureReason.INVALID_URL, "not a supported OLX vertical"
        )
    if _is_category_hub(url):
        # An App-Router navigation hub (no embedded listing JSON, nothing to
        # extract). Stable from the URL alone — fail clearly without spending a
        # browser fetch, and don't let it masquerade as scraper-rot.
        return _failure_envelope(
            url,
            FailureReason.CATEGORY_LANDING,
            "OLX category-landing hub page — no listings to extract. Narrow the "
            "URL with a transaction type (e.g. /imoveis/venda, /imoveis/aluguel), "
            "a subcategory, or a deeper location (e.g. "
            "/imoveis/estado-sp/sao-paulo-e-regiao).",
        )
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
        if page_type == "list":
            listings = [
                _normalize_ad(ad, vertical=vertical, page_type="list", base_url=url)
                for ad in _parse_next_data(html_src)
            ]
        else:
            ad = _extract_detail_ad(html_src)
            if ad is not None:
                listings = [
                    _normalize_ad(
                        ad, vertical=vertical, page_type="detail", base_url=url
                    )
                ]
            else:
                fallback = _jsonld_detail(html_src, url, vertical)
                if fallback is None:
                    raise AdapterParseError(
                        "detail page: no <script id='initial-data'> or schema.org "
                        "Offer found — site structure may have changed"
                    )
                listings = [fallback]
    except AdapterParseError as exc:
        log.warning("olx parse anchor missing (%s/%s): %s", vertical, page_type, exc)
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, f"olx {exc}", http_status=status
        )
    except Exception as exc:
        log.warning("olx parse failed (%s/%s): %s", vertical, page_type, exc)
        return _failure_envelope(
            url,
            FailureReason.PARSE_FAILED,
            f"olx parse failed: {type(exc).__name__}: {exc}",
            http_status=status,
        )

    from .. import io as io_mod

    # Anchor present but zero items: a genuinely empty list page (vs. the rot
    # case above, which raised). Flag it so an agent can tell the two apart.
    warnings = ["no_results"] if page_type == "list" and not listings else []
    markdown = _render_markdown(listings, vertical)
    title = (
        listings[0].get("title")
        if page_type == "detail" and listings
        else f"OLX: {len(listings)} anúncios"
    )
    return envelope.success_envelope(
        base=_base_envelope(url, http_status=status or 200),
        markdown=markdown,
        metadata={
            "title": title,
            "byline": None,
            "published": None,
            "modified": None,
            "language": "pt-BR",
            "site_name": "OLX",
            "image": listings[0].get("image") if listings else None,
            "word_count": len(markdown.split()),
            "quality": {
                "provider": "olx",
                "vertical": vertical,
                "page_type": page_type,
                "result_count": len(listings),
                "listings": listings,
            },
            "warnings": warnings,
        },
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )
