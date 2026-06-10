"""Steam store adapter.

Steam exposes clean **public JSON endpoints** that return structured store data
over the plain http tier — no auth, no JS render, no bot challenge on the data
path. This adapter fetches them *directly* through the injected ``fetch_html``
(like :mod:`vasco.adapters.shopify`, not embedded-in-HTML like the marketplace
adapters), so it shares the ``http → browser`` escalation chain (minus the
wayback tail) and parses a JSON anchor.

Two page types are claimed:

- **App** (``/app/<id>``) → the storefront ``appdetails`` API is the spine
  (price/genres/metacritic/release/platforms), enriched **best-effort** by the
  public ``appreviews`` summary, the live ``GetNumberOfCurrentPlayers`` count,
  and — when an ITAD key is configured — IsThereAnyDeal historical pricing
  (all-time low + recent price log; see :mod:`vasco.adapters.itad`). The calls
  run concurrently; only ``appdetails`` can fail the fetch.
- **Search** (``/search/?term=``) → the ``storesearch`` API returns a clean list
  of apps with price/metascore/platforms.

Scope is intentionally narrow: bundle/sub/dlc/community URLs are *not* claimed
(``_claim`` returns ``None``) and fall through to a normal fetch. The adapter
never raises — it returns a failure envelope.

Rot contract (per the project invariants): a broken/non-JSON ``appdetails`` body
or a search response with no ``items`` array → :class:`AdapterParseError` →
``PARSE_FAILED``; an ``appdetails`` node with ``success: false`` (a delisted or
nonexistent appid — valid shape, no store page) → ``NOT_FOUND``; a search with an
``items`` array that is empty → ``success`` + ``["no_results"]``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlsplit

from .. import envelope
from ..errors import AdapterParseError, FailureReason
from . import itad

log = logging.getLogger(__name__)


_STORE = "https://store.steampowered.com"
_API = "https://api.steampowered.com"
_HOST = "store.steampowered.com"


# An injected HTML fetcher: returns (body, status, headers, reason, mode_used).
# The main flow passes one backed by the shared escalation chain; see
# fetch._make_adapter_fetcher.
HtmlFetcher = Callable[
    [str], Awaitable[tuple[str, int, dict[str, str], FailureReason, str]]
]


# ---------------------------------------------------------------------------
# URL detection / endpoint mapping
# ---------------------------------------------------------------------------


def _segments(url: str) -> list[str]:
    return [s for s in (urlsplit(url).path or "").split("/") if s]


def _claim(url: str) -> tuple[str, str] | None:
    """Map a Steam store URL to ``(page_type, key)`` or ``None`` if unclaimable.

    ``("app", app_id)`` for ``/app/<id>[/...]``; ``("search", term)`` for
    ``/search...?term=<q>``. Everything else (bundle/sub/dlc/community/homepage,
    or a ``/search`` with no term) returns ``None`` → normal fetch.
    """
    if not url:
        return None
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != _HOST:
        return None
    segs = _segments(url)
    if not segs:
        return None
    if segs[0] == "app" and len(segs) >= 2 and segs[1].isdigit():
        return "app", segs[1]
    if segs[0] == "search":
        term = (parse_qs(parts.query).get("term") or [""])[0].strip()
        if term:
            return "search", term
    return None


def is_steam_url(url: str) -> bool:
    """Certain match: a ``store.steampowered.com`` app or search URL we claim."""
    return _claim(url) is not None


def _region(cfg: Any | None) -> tuple[str, str]:
    """``(cc, language)`` from ``cfg.adapters.steam`` — Steam's storefront region knobs."""
    steam = getattr(getattr(cfg, "adapters", None), "steam", None)
    cc = str(getattr(steam, "country", "US") or "US").lower()
    lang = str(getattr(steam, "language", "english") or "english").lower()
    return cc, lang


def _appdetails_url(app_id: str, cc: str, lang: str) -> str:
    return f"{_STORE}/api/appdetails?appids={app_id}&cc={cc}&l={lang}"


def _appreviews_url(app_id: str) -> str:
    return (
        f"{_STORE}/appreviews/{app_id}?json=1&num_per_page=0"
        "&language=all&purchase_type=all&review_type=all"
    )


def _players_url(app_id: str) -> str:
    return f"{_API}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={app_id}"


def _storesearch_url(term: str, cc: str, lang: str) -> str:
    return f"{_STORE}/api/storesearch/?term={quote_plus(term)}&cc={cc}&l={lang}"


def _app_canonical(app_id: str) -> str:
    return f"{_STORE}/app/{app_id}/"


# ---------------------------------------------------------------------------
# Value normalization helpers (pure)
# ---------------------------------------------------------------------------


def _money_cents(value: Any) -> float | None:
    """Steam store prices are integer **cents** (7399 → 73.99)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value) / 100.0, 2)
    if isinstance(value, str) and value.strip():
        try:
            return round(float(value) / 100.0, 2)
        except ValueError:
            return None
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (float, str)):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    return None


def _platforms(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    return [k for k in ("windows", "mac", "linux") if value.get(k)]


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v.strip()]


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


# ---------------------------------------------------------------------------
# Parsers (pure)
# ---------------------------------------------------------------------------


def _parse_app(body: str, app_id: str, url: str) -> dict[str, Any] | None:
    """Parse an ``appdetails`` body → a product dict, or ``None`` for a
    ``success: false`` node (→ the caller emits ``NOT_FOUND``).

    Raises :class:`AdapterParseError` when the body isn't a Steam appdetails
    response (not JSON / no appid node / success-but-no-data) — scraper-rot.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        raise AdapterParseError("appdetails: response was not JSON")
    node = data.get(str(app_id)) if isinstance(data, dict) else None
    if not isinstance(node, dict):
        raise AdapterParseError(
            "appdetails: missing appid node — not a Steam appdetails response"
        )
    if node.get("success") is not True:
        return None  # delisted / nonexistent appid → NOT_FOUND
    d = node.get("data")
    if not isinstance(d, dict) or not isinstance(d.get("name"), str) or not d["name"]:
        raise AdapterParseError(
            "appdetails: success but no usable data — Steam markup changed"
        )

    price = d.get("price_overview") if isinstance(d.get("price_overview"), dict) else {}
    final = _money_cents(price.get("final"))
    initial = _money_cents(price.get("initial"))
    discount = _int(price.get("discount_percent")) or 0
    genres = [
        g["description"]
        for g in (d.get("genres") or [])
        if isinstance(g, dict) and isinstance(g.get("description"), str)
    ]
    categories = [
        c["description"]
        for c in (d.get("categories") or [])
        if isinstance(c, dict) and isinstance(c.get("description"), str)
    ]
    release = d.get("release_date") if isinstance(d.get("release_date"), dict) else {}
    meta = d.get("metacritic") if isinstance(d.get("metacritic"), dict) else {}
    recs = (
        d.get("recommendations") if isinstance(d.get("recommendations"), dict) else {}
    )
    dlc = d.get("dlc") if isinstance(d.get("dlc"), list) else []

    return _compact(
        {
            "app_id": app_id,
            "type": d.get("type") if isinstance(d.get("type"), str) else None,
            "title": d["name"].strip(),
            "url": _app_canonical(app_id),
            "is_free": bool(d.get("is_free")),
            "price": final,
            "original_price": initial
            if (discount and initial and final and initial > final)
            else None,
            "discount_percent": discount or None,
            "currency": price.get("currency")
            if isinstance(price.get("currency"), str)
            else None,
            "short_description": d.get("short_description").strip()
            if isinstance(d.get("short_description"), str)
            and d["short_description"].strip()
            else None,
            "genres": genres,
            "categories": categories,
            "release_date": release.get("date")
            if isinstance(release.get("date"), str) and release["date"]
            else None,
            "coming_soon": bool(release.get("coming_soon")) if release else None,
            "metacritic": _int(meta.get("score")),
            "platforms": _platforms(d.get("platforms")),
            "developers": _str_list(d.get("developers")),
            "publishers": _str_list(d.get("publishers")),
            "controller_support": d.get("controller_support")
            if isinstance(d.get("controller_support"), str)
            else None,
            "required_age": _int(d.get("required_age")) or None,
            "dlc_count": len(dlc) or None,
            "recommendations": _int(recs.get("total")),
            "image": d.get("header_image")
            if isinstance(d.get("header_image"), str)
            else None,
        }
    )


def _merge_reviews(product: dict[str, Any], result: Any) -> None:
    """Fold the ``appreviews`` summary into ``product`` (best-effort, never
    raises — ``result`` is a fetch tuple or an Exception from ``gather``)."""
    summary = _summary_from_fetch(result, key="query_summary")
    if not isinstance(summary, dict):
        return
    desc = summary.get("review_score_desc")
    if isinstance(desc, str) and desc:
        product["review_score_desc"] = desc
    for src, dst in (
        ("review_score", "review_score"),
        ("total_reviews", "total_reviews"),
        ("total_positive", "total_positive"),
        ("total_negative", "total_negative"),
    ):
        val = _int(summary.get(src))
        if val is not None:
            product[dst] = val


def _merge_players(product: dict[str, Any], result: Any) -> None:
    """Fold the live player count into ``product`` (best-effort, never raises)."""
    response = _summary_from_fetch(result, key="response")
    if not isinstance(response, dict):
        return
    if _int(response.get("result")) == 1:
        count = _int(response.get("player_count"))
        if count is not None:
            product["player_count"] = count


def _merge_itad(product: dict[str, Any], result: Any) -> None:
    """Fold ITAD price history into ``product`` (best-effort — ``result`` is the
    dict from :func:`itad.steam_price_history`, ``None``, or a gather exception):
    ``historical_low`` (all-time-low deal), ``price_history`` (recent cuts), and
    the ITAD game URL."""
    if not isinstance(result, dict):
        return
    low = result.get("historical_low")
    if isinstance(low, dict) and low:
        product["historical_low"] = low
    history = result.get("price_history")
    if isinstance(history, list) and history:
        product["price_history"] = history
    if isinstance(result.get("itad_url"), str):
        product["itad_url"] = result["itad_url"]


def _summary_from_fetch(result: Any, *, key: str) -> Any:
    """Extract ``json(body)[key]`` from a ``gather`` result, swallowing every
    failure (exception, non-OK fetch, bad JSON) — enrichment is optional."""
    if isinstance(result, Exception) or not isinstance(result, tuple):
        return None
    try:
        body, _status, _headers, reason, _mode = result
    except (ValueError, TypeError):
        return None
    if reason != FailureReason.OK or not body:
        return None
    try:
        return json.loads(body).get(key)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


def _parse_search(body: str) -> list[dict[str, Any]]:
    """Parse a ``storesearch`` body → normalized app cards. Raises
    :class:`AdapterParseError` when the ``items`` anchor is absent (an empty
    ``items`` list is a legitimate no-results, returns ``[]``)."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        raise AdapterParseError("storesearch: response was not JSON")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise AdapterParseError(
            "storesearch: no `items` array — not a Steam search response"
        )
    out: list[dict[str, Any]] = []
    position = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        app_id = item.get("id")
        name = item.get("name")
        if app_id is None or not isinstance(name, str) or not name.strip():
            continue
        position += 1
        price = item.get("price") if isinstance(item.get("price"), dict) else {}
        out.append(
            _compact(
                {
                    "position": position,
                    "type": item.get("type")
                    if isinstance(item.get("type"), str)
                    else None,
                    "app_id": str(app_id),
                    "title": name.strip(),
                    "url": _app_canonical(str(app_id)),
                    "price": _money_cents(price.get("final")),
                    "currency": price.get("currency")
                    if isinstance(price.get("currency"), str)
                    else None,
                    "metacritic": _int(item.get("metascore")),
                    "platforms": _platforms(item.get("platforms")),
                    "controller_support": item.get("controller_support")
                    if isinstance(item.get("controller_support"), str)
                    else None,
                    "image": item.get("tiny_image")
                    if isinstance(item.get("tiny_image"), str)
                    else None,
                }
            )
        )
    return out


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _fmt_price(product: dict[str, Any]) -> str:
    if product.get("is_free"):
        return "Free"
    price = product.get("price")
    if price is None:
        return "—"
    cur = product.get("currency") or ""
    body = f"{price:,.2f}"
    return f"{cur} {body}".strip()


def _render_app(p: dict[str, Any]) -> str:
    parts = [f"# {p.get('title', '?')} — {_fmt_price(p)}"]
    facts: list[str] = []
    if p.get("original_price") and p.get("discount_percent"):
        facts.append(
            f"-{p['discount_percent']}% (was {p['currency']} {p['original_price']:,.2f})"
        )
    if p.get("metacritic"):
        facts.append(f"Metacritic {p['metacritic']}")
    if p.get("review_score_desc"):
        rv = p["review_score_desc"]
        if p.get("total_reviews"):
            rv += f" ({p['total_reviews']:,} reviews)"
        facts.append(rv)
    if p.get("player_count") is not None:
        facts.append(f"{p['player_count']:,} playing now")
    low = p.get("historical_low")
    if isinstance(low, dict) and low.get("price") is not None:
        cur = low.get("currency") or p.get("currency") or ""
        s = f"all-time low {cur} {low['price']:,.2f}".strip()
        if low.get("date"):
            s += f" ({low['date']})"
        facts.append(s)
    if p.get("genres"):
        facts.append(", ".join(p["genres"]))
    if p.get("release_date"):
        facts.append(f"Released {p['release_date']}")
    if facts:
        parts.append("")
        parts.append(" · ".join(facts))
    if p.get("short_description"):
        parts.append("")
        parts.append(p["short_description"])
    return "\n".join(parts)


def _render_search(products: list[dict[str, Any]], term: str) -> str:
    if not products:
        return f'# Steam: "{term}"\n\nNo results.'
    parts = [f'{len(products)} results for "{term}"', ""]
    for i, p in enumerate(products, 1):
        head = f"{i}. **{p.get('title', '?')}** — {_fmt_price(p)}"
        if p.get("metacritic"):
            head += f" · Metacritic {p['metacritic']}"
        parts.append(head)
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
        mode_used="steam",
        content_type="application/x-steam",
    )


def _failure_envelope(
    url: str, reason: FailureReason, message: str, *, http_status: int = 0
) -> dict[str, Any]:
    return envelope.failure_envelope(
        base=_base_envelope(url, http_status=http_status),
        reason=reason,
        message=message,
    )


def _success_envelope(
    url: str,
    *,
    page_type: str,
    products: list[dict[str, Any]],
    status: int,
    markdown: str,
    quality_extra: dict[str, Any],
) -> dict[str, Any]:
    from .. import io as io_mod

    first = products[0] if products else {}
    quality = _compact(
        {
            "provider": "steam",
            "page_type": page_type,
            "currency": first.get("currency"),
            "result_count": len(products),
            "products": products,
            **quality_extra,
        }
    )
    warnings = ["no_results"] if page_type == "search" and not products else []
    return envelope.success_envelope(
        base=_base_envelope(url, http_status=status or 200),
        markdown=markdown,
        metadata={
            "title": first.get("title")
            if page_type == "app"
            else quality_extra.get("query"),
            "byline": None,
            "published": None,
            "modified": None,
            "language": None,
            "site_name": "Steam",
            "image": first.get("image"),
            "word_count": len(markdown.split()),
            "quality": quality,
            "warnings": warnings,
        },
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )


async def _fetch_app(
    url: str,
    app_id: str,
    cc: str,
    lang: str,
    fetch_html: HtmlFetcher,
    cfg: Any | None = None,
) -> dict[str, Any]:
    # appdetails is the spine; reviews, players, and ITAD price history are all
    # best-effort. Run them concurrently — only the spine can fail the fetch.
    # `steam_price_history` returns None with no network when no ITAD key is set,
    # so it's free to schedule unconditionally.
    details, reviews, players, itad_res = await asyncio.gather(
        fetch_html(_appdetails_url(app_id, cc, lang)),
        fetch_html(_appreviews_url(app_id)),
        fetch_html(_players_url(app_id)),
        itad.steam_price_history(app_id, cfg=cfg),
        return_exceptions=True,
    )

    if isinstance(details, Exception):
        reason = (
            FailureReason.TIMEOUT
            if isinstance(details, asyncio.TimeoutError)
            else FailureReason.SERVER_ERROR
        )
        return _failure_envelope(url, reason, f"appdetails fetch failed: {details}")
    body, status, _headers, reason, mode_used = details
    if reason != FailureReason.OK or not body:
        return _failure_envelope(
            url,
            reason,
            f"appdetails fetch failed via {mode_used} tier",
            http_status=status,
        )

    try:
        product = _parse_app(body, app_id, url)
    except AdapterParseError as exc:
        log.warning("steam appdetails parse anchor missing: %s", exc)
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, f"steam {exc}", http_status=status
        )
    except Exception as exc:  # defensive — never raise out of an adapter
        log.warning("steam appdetails parse failed: %s", exc)
        return _failure_envelope(
            url,
            FailureReason.PARSE_FAILED,
            f"steam appdetails parse failed: {type(exc).__name__}: {exc}",
            http_status=status,
        )

    if product is None:  # success:false → no store page
        return _failure_envelope(
            url,
            FailureReason.NOT_FOUND,
            f"steam: appid {app_id} has no store page (delisted or nonexistent)",
            http_status=status,
        )

    _merge_reviews(product, reviews)
    _merge_players(product, players)
    _merge_itad(product, itad_res)

    return _success_envelope(
        url,
        page_type="app",
        products=[product],
        status=status,
        markdown=_render_app(product),
        quality_extra={"app_id": app_id},
    )


async def _fetch_search(
    url: str, term: str, cc: str, lang: str, fetch_html: HtmlFetcher
) -> dict[str, Any]:
    try:
        body, status, _headers, reason, mode_used = await fetch_html(
            _storesearch_url(term, cc, lang)
        )
    except asyncio.TimeoutError:
        return _failure_envelope(
            url, FailureReason.TIMEOUT, "storesearch deadline elapsed"
        )
    except Exception as exc:
        return _failure_envelope(
            url, FailureReason.SERVER_ERROR, f"storesearch fetch failed: {exc}"
        )
    if reason != FailureReason.OK or not body:
        return _failure_envelope(
            url,
            reason,
            f"storesearch fetch failed via {mode_used} tier",
            http_status=status,
        )
    try:
        products = _parse_search(body)
    except AdapterParseError as exc:
        log.warning("steam storesearch parse anchor missing: %s", exc)
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, f"steam {exc}", http_status=status
        )
    except Exception as exc:  # defensive
        log.warning("steam storesearch parse failed: %s", exc)
        return _failure_envelope(
            url,
            FailureReason.PARSE_FAILED,
            f"steam storesearch parse failed: {type(exc).__name__}: {exc}",
            http_status=status,
        )

    return _success_envelope(
        url,
        page_type="search",
        products=products,
        status=status,
        markdown=_render_search(products, term),
        quality_extra={"query": term},
    )


async def fetch_steam(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
    fetch_html: HtmlFetcher | None = None,
) -> dict[str, Any]:
    """Fetch a Steam app/search URL → a structured envelope. Never raises.

    ``appdetails``/``storesearch``/``appreviews``/players are obtained via
    ``fetch_html`` (the shared escalation chain). ``deadline`` is honored by the
    injected fetcher's own budget. App pages are additionally enriched with ITAD
    historical pricing when an ITAD key is configured (best-effort; see
    :mod:`vasco.adapters.itad`).
    """
    claim = _claim(url)
    if claim is None:  # defensive — dispatch only calls us on a claimable URL
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, "steam: unrecognized URL shape"
        )
    if fetch_html is None:
        return _failure_envelope(
            url, FailureReason.SERVER_ERROR, "steam: no HTML fetcher injected"
        )
    page_type, key = claim
    cc, lang = _region(cfg)
    if page_type == "search":
        return await _fetch_search(url, key, cc, lang, fetch_html)
    return await _fetch_app(url, key, cc, lang, fetch_html, cfg)
