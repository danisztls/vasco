"""Real-estate listing adapter (Brazilian portals).

Brazilian real-estate sites render their useful payload as structured listings
(price, area, bedrooms, address) inside JS-heavy pages; the default trafilatura
pipeline flattens these to prose and drops the JSON-LD ``ItemList`` / cards.
This adapter parses the rendered HTML per provider into normalized listing
dicts exposed via ``quality.listings``.

Public surface:
- ``is_realestate_url(url)`` — match a supported provider domain.
- ``fetch_realestate(url, *, deadline, cfg=None)`` — return a v0.1 envelope.

Envelope uses ``mode_used="realestate"`` and
``content_type="application/x-realestate"``. On any fetch/parse failure it
returns a failure envelope rather than raising.

Two page types per provider:
- **list** pages yield many listings, each with a single thumbnail (cheap).
- **detail** pages (individual listing URLs) yield one listing with the full
  photo gallery and full fields.

Supported providers (domain → key):
- ``vivareal.com.br`` (JSON-LD ``ItemList`` / ``Product``)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..errors import FailureReason
from ..fetch import browser

log = logging.getLogger(__name__)

# Browser tier cap; these portals are JS-heavy and take a few seconds to render.
_BROWSER_TIER_CAP: float = 8.0
_GALLERY_CAP: int = 4

# host suffix → (provider key, display name)
_PROVIDERS: dict[str, tuple[str, str]] = {
    "vivareal.com.br": ("vivareal", "VivaReal"),
}

_VIVAREAL_TYPE_MAP = {
    "Apartment": "Apartamento",
    "House": "Casa",
    "SingleFamilyResidence": "Casa",
    "Condominium": "Condomínio",
    "Flat": "Flat",
    "Penthouse": "Cobertura",
    "Place": "Imóvel",
    "Accommodation": "Imóvel",
    "Residence": "Imóvel",
    "LandProperty": "Terreno",
}

_VIVAREAL_AMENITIES = {
    "Pool": "Piscina",
    "Gated Community": "Condomínio Fechado",
    "Elevator": "Elevador",
    "Balcony": "Varanda",
    "Furnished": "Mobiliado",
    "Party Hall": "Salão de Festas",
    "Barbecue Grill": "Churrasqueira",
    "Playground": "Playground",
    "Pets Allowed": "Aceita Pets",
}


# ---------------------------------------------------------------------------
# URL detection / routing
# ---------------------------------------------------------------------------


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _provider_for(url: str) -> tuple[str, str] | None:
    host = _host(url)
    for suffix, info in _PROVIDERS.items():
        if host == suffix or host.endswith("." + suffix):
            return info
    return None


def is_realestate_url(url: str) -> bool:
    return bool(url) and _provider_for(url) is not None


def _page_type(url: str, provider: str) -> str:
    """Classify a URL as a 'list' or 'detail' page for the given provider."""
    parts = urlsplit(url)
    path = (parts.path or "/").lower()
    if provider == "vivareal":
        return "detail" if "/imovel/" in path else "list"
    return "list"


# ---------------------------------------------------------------------------
# Normalized listing + small parsing helpers
# ---------------------------------------------------------------------------

_LISTING_FIELDS = (
    "url",
    "title",
    "type",
    "price",
    "condo_fee",
    "iptu",
    "area",
    "bedrooms",
    "bathrooms",
    "parking",
    "neighborhood",
    "city",
    "street",
    "description",
    "amenities",
    "image",
    "images",
)


def _listing(**kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {k: None for k in _LISTING_FIELDS}
    out["amenities"] = []
    out["images"] = []
    out.update({k: v for k, v in kw.items() if k in _LISTING_FIELDS})
    if out.get("images") and not out.get("image"):
        out["image"] = out["images"][0]
    return out


def _as_int(value: Any) -> int | None:
    """First integer found in `value` (handles "2 quartos", "R$ 1.278", 90.0)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.search(r"\d[\d.]*", str(value).replace(".", ""))
    return int(m.group()) if m else None


def _brl_int(value: Any) -> int | None:
    """Parse a BRL price ("R$ 1.278,00" / 1278 / "1.278") to int reais."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value)
    s = re.sub(r"[^\d,]", "", s).split(",")[0]  # drop currency, cents
    return int(s) if s.isdigit() else None


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


# ---------------------------------------------------------------------------
# VivaReal (JSON-LD)
# ---------------------------------------------------------------------------


def _jsonld_objects(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            d = json.loads(tag.string)
        except json.JSONDecodeError:
            continue
        out.extend(d if isinstance(d, list) else [d])
    return [o for o in out if isinstance(o, dict)]


def _vivareal_condo_fee(pv: Any) -> int | None:
    entries = pv if isinstance(pv, list) else [pv]
    for e in entries:
        if isinstance(e, dict) and e.get("name") == "Condominium Fee":
            return _as_int(e.get("value"))
    return None


def _vivareal_item(item: dict) -> dict | None:
    url = item.get("url") or ""
    if not url:
        return None
    name = item.get("name") or ""
    addr = item.get("address") or {}
    nbh = re.search(r"\bem ([^,]+),", name)
    parking = re.search(r"(\d+)\s*vaga", name, re.IGNORECASE)
    amenities = [
        _VIVAREAL_AMENITIES[k]
        for f in (item.get("amenityFeature") or [])
        if isinstance(f, dict)
        for k in [f.get("value") or f.get("name")]
        if k in _VIVAREAL_AMENITIES
    ]
    offers = item.get("offers") or {}
    return _listing(
        url=url,
        title=name or None,
        type=_VIVAREAL_TYPE_MAP.get(item.get("@type", ""), "Imóvel"),
        price=_as_int(offers.get("price")),
        condo_fee=_vivareal_condo_fee(offers.get("propertyValue")),
        area=_as_int((item.get("floorSize") or {}).get("value")),
        bedrooms=_as_int(item.get("numberOfBedrooms")),
        bathrooms=_as_int(item.get("numberOfBathroomsTotal")),
        parking=int(parking.group(1)) if parking else None,
        neighborhood=nbh.group(1).strip() if nbh else None,
        city=(addr.get("addressLocality") or "").strip() or None,
        street=(addr.get("streetAddress") or "").strip() or None,
        amenities=list(dict.fromkeys(amenities)),
        images=_dedup(item.get("image")),
    )


def _vivareal_list(html: str) -> list[dict]:
    for obj in _jsonld_objects(html):
        if obj.get("@type") != "ItemList":
            continue
        out = []
        for e in obj.get("itemListElement") or []:
            parsed = _vivareal_item(e.get("item", e))
            if parsed:
                out.append(parsed)
        return out
    return []


def _vivareal_detail(html: str) -> list[dict]:
    product = next(
        (o for o in _jsonld_objects(html) if o.get("@type") == "Product"), None
    )
    if not product:
        return []
    name = product.get("name") or ""
    desc = product.get("description") or ""
    offers = product.get("offers") or {}
    nbh = re.search(r"\bem ([^,]+),", name)
    city = re.search(r",\s*([^,\-]+?)\s*-\s*[A-Z]{2}", name)
    beds = re.search(r"(\d+)\s*dormit", desc, re.IGNORECASE)
    baths = re.search(r"(\d+)\s*(?:total de\s*)?banheiro", desc, re.IGNORECASE)
    parking = re.search(r"(\d+)\s*(?:garagem|vaga)", desc, re.IGNORECASE)
    area = re.search(r"constru[ií]da\s*([\d.,]+)", desc, re.IGNORECASE)
    return [
        _listing(
            url=offers.get("url") or product.get("sku") or "",
            title=name or None,
            type="Imóvel",
            price=_as_int(offers.get("price")),
            area=_as_int(area.group(1)) if area else None,
            bedrooms=int(beds.group(1)) if beds else None,
            bathrooms=int(baths.group(1)) if baths else None,
            parking=int(parking.group(1)) if parking else None,
            neighborhood=nbh.group(1).strip() if nbh else None,
            city=city.group(1).strip() if city else None,
            description=desc.strip() or None,
            images=_dedup(product.get("image")),
        )
    ]





















_PARSERS = {
    ("vivareal", "list"): lambda html, base, url: _vivareal_list(html),
    ("vivareal", "detail"): lambda html, base, url: _vivareal_detail(html),
}


# ---------------------------------------------------------------------------
# Markdown rendering + envelope
# ---------------------------------------------------------------------------


def _fmt_price(listing: dict) -> str:
    p = listing.get("price")
    if not p:
        return "Sob consulta"
    base = f"R$ {int(p):,}".replace(",", ".")
    condo = listing.get("condo_fee")
    return f"{base} + R$ {int(condo):,}".replace(",", ".") if condo else base


def _render_markdown(listings: list[dict]) -> str:
    parts: list[str] = []
    for i, ln in enumerate(listings, 1):
        specs = [
            s
            for s in (
                f"{ln['area']}m²" if ln.get("area") else None,
                f"{ln['bedrooms']} quartos" if ln.get("bedrooms") else None,
                f"{ln['bathrooms']} ban." if ln.get("bathrooms") else None,
                f"{ln['parking']} vaga" if ln.get("parking") else None,
            )
            if s
        ]
        loc = ", ".join(s for s in (ln.get("neighborhood"), ln.get("city")) if s)
        lead = ln.get("title") or ln.get("type")
        head = " · ".join(x for x in (lead, _fmt_price(ln), " · ".join(specs)) if x)
        line = f"{i}. {head}"
        if loc:
            line += f" — {loc}"
        parts.append(line)
    return "\n".join(parts)


def _base_envelope(url: str, *, http_status: int = 0) -> dict[str, Any]:
    return {
        "url_requested": url,
        "url_final": url,
        "url_canonical": url,
        "http_status": http_status,
        "mode_used": "realestate",
        "fetched_at": int(time.time()),
        "from_cache": False,
        "cache_age_seconds": 0,
        "content_type": "application/x-realestate",
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


async def fetch_realestate(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """Fetch a real-estate list/detail page and return a structured envelope."""
    info = _provider_for(url)
    if info is None:  # pragma: no cover - routing guards this
        return _failure_envelope(
            url, FailureReason.INVALID_URL, "not a supported real-estate domain"
        )
    provider, display = info
    page_type = _page_type(url, provider)

    deadline_monotonic = time.monotonic() + max(0.0, float(deadline))
    tier_deadline = min(deadline_monotonic, time.monotonic() + _BROWSER_TIER_CAP)

    try:
        html_src, status, _headers = await _browser_fetch_html(
            url, deadline_monotonic=tier_deadline, cfg=cfg
        )
    except asyncio.TimeoutError:
        return _failure_envelope(url, FailureReason.TIMEOUT, "browser deadline elapsed")
    except Exception as exc:
        return _failure_envelope(
            url,
            _classify_browser_error(exc),
            f"browser fetch failed: {type(exc).__name__}: {exc}",
        )

    if not html_src:
        return _failure_envelope(
            url,
            FailureReason.SERVER_ERROR,
            "browser returned empty body",
            http_status=status,
        )

    try:
        listings = _PARSERS[(provider, page_type)](html_src, url, url)
    except Exception as exc:
        log.warning("realestate parse failed (%s/%s): %s", provider, page_type, exc)
        return _failure_envelope(
            url,
            FailureReason.UNSUPPORTED_CONTENT_TYPE,
            f"parse failed: {type(exc).__name__}: {exc}",
            http_status=status,
        )

    from .. import io as io_mod

    markdown = _render_markdown(listings)
    env = _base_envelope(url, http_status=status or 200)
    env.update(
        {
            "title": f"{display}: {len(listings)} imóveis"
            if page_type == "list"
            else display,
            "byline": None,
            "published": None,
            "modified": None,
            "language": "pt-BR",
            "site_name": display,
            "image": listings[0].get("image") if listings else None,
            "word_count": len(markdown.split()),
            "token_count_estimate": io_mod.estimate_tokens(markdown),
            "quality": {
                "provider": provider,
                "page_type": page_type,
                "result_count": len(listings),
                "listings": listings,
            },
            "links": [],
            "markdown": markdown,
            "warnings": [],
        }
    )
    return env
