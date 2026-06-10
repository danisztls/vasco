"""IsThereAnyDeal (ITAD) price-history client.

Enriches Steam app pages with **Steam-only** historical pricing from the
official ITAD API v2 (https://docs.isthereanydeal.com), so the Steam adapter
can report an all-time-low price and a recent price-change log on top of the
current store price. Three calls:

- ``GET /games/lookup/v1?appid=`` — resolve a Steam appid → ITAD game id (uuid).
- ``POST /games/storelow/v2`` (``shops=61``) — the all-time-low Steam price.
- ``GET /games/history/v2`` (``shops=61``) — the recent Steam price-change log
  (ITAD defaults to the last 3 months).

``shops=61`` is ITAD's Steam shop id, intrinsic to "Steam price history" (not
configurable). The ``country`` (ISO-3166-1 alpha-2, e.g. ``BR`` → BRL) follows
``steam.country`` so the currency matches the displayed store price. The **only**
config is the API key (``steam.itad_api_key`` in config, or ``VASCO_ITAD_API_KEY``);
its presence is the enable switch.

Never raises: every failure path — no key, transport error, bad JSON, a game ITAD
doesn't track — returns ``None``/empty so the Steam envelope degrades gracefully
to store-only data (mirrors :mod:`vasco.summarize`).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

_BASE = "https://api.isthereanydeal.com"
# ITAD's shop id for Steam — intrinsic to "Steam price history", not a knob.
_STEAM_SHOP_ID = 61
# Recent price-log entries to keep (most-recent first). Not a config knob — the
# only ITAD setting is the api key.
_HISTORY_LIMIT = 10


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def resolve_api_key(cfg: Any | None) -> str:
    """ITAD key from env (in-process convenience) then config
    (``steam.itad_api_key``). Mirrors :func:`vasco.summarize.resolve_api_key` —
    env only reaches in-process tools (CLI), so vascod must read the key from
    config. Presence of a key is the *only* enable switch."""
    env_key = os.environ.get("VASCO_ITAD_API_KEY") or os.environ.get("ITAD_API_KEY")
    if env_key:
        return env_key
    try:
        return getattr(getattr(cfg, "steam", None), "itad_api_key", "") or ""
    except Exception:
        return ""


def _country(cfg: Any | None) -> str:
    """ITAD ``country`` (uppercased ISO alpha-2) — always the Steam region
    (``steam.country``, default ``BR``), so the historical low's currency matches
    the displayed store price. Not a separate knob: a mismatched currency in one
    envelope has no sane use."""
    c = (getattr(getattr(cfg, "steam", None), "country", "BR") or "BR").strip()
    return (c or "BR").upper()


# ---------------------------------------------------------------------------
# Pure normalization helpers
# ---------------------------------------------------------------------------


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


def _date(ts: Any) -> str | None:
    """ISO-8601 timestamp → ``YYYY-MM-DD`` (ITAD lows/history are date-grained)."""
    if isinstance(ts, str) and ts:
        return ts.split("T", 1)[0]
    return None


def _normalize_deal(entry: dict[str, Any]) -> dict[str, Any] | None:
    """A ``{price, regular, cut, timestamp}`` ITAD deal → a compact dict, or
    ``None`` when it carries no usable price."""
    if not isinstance(entry, dict):
        return None
    price = entry.get("price") if isinstance(entry.get("price"), dict) else {}
    amount = price.get("amount")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return None
    regular = entry.get("regular") if isinstance(entry.get("regular"), dict) else {}
    reg_amount = regular.get("amount")
    out = {
        "price": round(float(amount), 2),
        "currency": price.get("currency")
        if isinstance(price.get("currency"), str)
        else None,
        "cut": _int(entry.get("cut")),
        "regular_price": round(float(reg_amount), 2)
        if isinstance(reg_amount, (int, float)) and not isinstance(reg_amount, bool)
        else None,
        "date": _date(entry.get("timestamp")),
    }
    return {k: v for k, v in out.items() if v is not None}


def _low_from_payload(data: Any) -> dict[str, Any] | None:
    """Parse a ``/games/storelow/v2`` response → the Steam all-time-low deal.
    The response is filtered to ``shops=61``, so the first ``lows`` entry is the
    Steam low."""
    if not isinstance(data, list) or not data:
        return None
    first = data[0] if isinstance(data[0], dict) else {}
    lows = first.get("lows")
    if not isinstance(lows, list) or not lows:
        return None
    return _normalize_deal(lows[0])


def _history_from_payload(data: Any, limit: int) -> list[dict[str, Any]]:
    """Parse a ``/games/history/v2`` response → recent price-cut events
    (most-recent first, capped at ``limit``). Entries whose ``deal`` is ``null``
    (price reset to regular) are dropped — only actual price points are kept."""
    if not isinstance(data, list) or limit <= 0:
        return []
    rows: list[tuple[str, dict[str, Any]]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        deal = item.get("deal")
        if not isinstance(deal, dict):
            continue
        deal_with_ts = {**deal, "timestamp": item.get("timestamp")}
        norm = _normalize_deal(deal_with_ts)
        if norm is None:
            continue
        rows.append((str(item.get("timestamp") or ""), norm))
    # ISO-8601 timestamps sort lexicographically; newest first.
    rows.sort(key=lambda t: t[0], reverse=True)
    return [norm for _ts, norm in rows[:limit]]


# ---------------------------------------------------------------------------
# I/O (thin; each swallows failures into None/empty)
# ---------------------------------------------------------------------------


async def _lookup(client: Any, appid: str, key: str) -> tuple[str, str | None] | None:
    """Steam appid → ``(itad_id, slug)`` or ``None`` if ITAD doesn't track it."""
    resp = await client.get(
        f"{_BASE}/games/lookup/v1", params={"key": key, "appid": appid}
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not data.get("found"):
        return None
    game = data.get("game") if isinstance(data.get("game"), dict) else {}
    gid = game.get("id")
    if not isinstance(gid, str) or not gid:
        return None
    slug = game.get("slug") if isinstance(game.get("slug"), str) else None
    return gid, slug


async def _store_low(
    client: Any, gid: str, key: str, country: str
) -> dict[str, Any] | None:
    resp = await client.post(
        f"{_BASE}/games/storelow/v2",
        params={"key": key, "country": country, "shops": str(_STEAM_SHOP_ID)},
        json=[gid],
    )
    resp.raise_for_status()
    return _low_from_payload(resp.json())


async def _history(
    client: Any, gid: str, key: str, country: str, limit: int
) -> list[dict[str, Any]]:
    resp = await client.get(
        f"{_BASE}/games/history/v2",
        params={
            "key": key,
            "id": gid,
            "country": country,
            "shops": str(_STEAM_SHOP_ID),
        },
    )
    resp.raise_for_status()
    return _history_from_payload(resp.json(), limit)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def steam_price_history(
    appid: str, *, cfg: Any | None = None, timeout: float = 12.0
) -> dict[str, Any] | None:
    """Steam appid → ``{itad_id, itad_url?, historical_low?, price_history?,
    currency?}`` or ``None``.

    Returns ``None`` with **zero network** when no API key is configured, so it's
    free to schedule unconditionally alongside the Steam fetches. Looks the appid
    up once, then fetches the all-time low and the recent history concurrently.
    Never raises.
    """
    key = resolve_api_key(cfg)
    if not key:
        return None
    country = _country(cfg)

    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            found = await _lookup(client, appid, key)
            if found is None:
                return None
            gid, slug = found
            low, history = await asyncio.gather(
                _store_low(client, gid, key, country),
                _history(client, gid, key, country, _HISTORY_LIMIT),
            )
    except Exception as exc:  # transport / JSON / anything — enrichment is optional
        log.debug("itad price history failed for appid %s: %s", appid, exc)
        return None

    if low is None and not history:
        return None
    out: dict[str, Any] = {"itad_id": gid}
    if slug:
        out["itad_url"] = f"https://isthereanydeal.com/game/{slug}/info/"
    if low:
        out["historical_low"] = low
    if history:
        out["price_history"] = history
    currency = (low or {}).get("currency") or (
        history[0].get("currency") if history else None
    )
    if currency:
        out["currency"] = currency
    return out
