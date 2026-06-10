from __future__ import annotations

import asyncio

import pytest

from vasco.adapters import itad as I
from vasco.config import AdaptersCfg, Config, SteamCfg

# Schema-shaped ITAD payloads (from the official OpenAPI spec v2).
_LOW_PAYLOAD = [
    {
        "id": "018d937f-0000-0000-0000-000000000000",
        "lows": [
            {
                "shop": {"id": 61, "name": "Steam"},
                "price": {"amount": 19.74, "amountInt": 1974, "currency": "BRL"},
                "regular": {"amount": 73.99, "amountInt": 7399, "currency": "BRL"},
                "cut": 73,
                "timestamp": "2023-11-21T00:00:00Z",
            }
        ],
    }
]

_HISTORY_PAYLOAD = [
    {
        "timestamp": "2024-06-01T00:00:00Z",
        "shop": {"id": 61, "name": "Steam"},
        "deal": {
            "price": {"amount": 36.99, "amountInt": 3699, "currency": "BRL"},
            "regular": {"amount": 73.99, "amountInt": 7399, "currency": "BRL"},
            "cut": 50,
        },
    },
    {
        "timestamp": "2024-11-27T00:00:00Z",  # newer
        "shop": {"id": 61, "name": "Steam"},
        "deal": {
            "price": {"amount": 22.19, "amountInt": 2219, "currency": "BRL"},
            "regular": {"amount": 73.99, "amountInt": 7399, "currency": "BRL"},
            "cut": 70,
        },
    },
    {  # price reset to regular — no deal, must be dropped
        "timestamp": "2024-12-05T00:00:00Z",
        "shop": {"id": 61, "name": "Steam"},
        "deal": None,
    },
]


# --- config resolution ------------------------------------------------------


def test_resolve_api_key_env_then_cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VASCO_ITAD_API_KEY", raising=False)
    monkeypatch.delenv("ITAD_API_KEY", raising=False)
    cfg = Config(adapters=AdaptersCfg(steam=SteamCfg(itad_api_key="from-cfg")))
    assert I.resolve_api_key(cfg) == "from-cfg"
    monkeypatch.setenv("VASCO_ITAD_API_KEY", "from-env")
    assert I.resolve_api_key(cfg) == "from-env"
    monkeypatch.delenv("VASCO_ITAD_API_KEY")
    assert I.resolve_api_key(None) == ""


def test_country_follows_steam(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VASCO_ITAD_API_KEY", raising=False)
    # currency region always follows steam.country (no separate itad knob)
    assert (
        I._country(Config(adapters=AdaptersCfg(steam=SteamCfg(country="de")))) == "DE"
    )
    assert I._country(Config()) == "BR"  # default
    assert I._country(None) == "BR"


# --- pure parsers -----------------------------------------------------------


def test_low_from_payload() -> None:
    low = I._low_from_payload(_LOW_PAYLOAD)
    assert low == {
        "price": 19.74,
        "currency": "BRL",
        "cut": 73,
        "regular_price": 73.99,
        "date": "2023-11-21",
    }


def test_low_from_payload_empty_and_malformed() -> None:
    assert I._low_from_payload([]) is None
    assert I._low_from_payload([{"id": "g", "lows": []}]) is None
    assert I._low_from_payload([{"id": "g"}]) is None
    assert I._low_from_payload("nope") is None


def test_history_from_payload_recent_first_and_drops_null_deals() -> None:
    hist = I._history_from_payload(_HISTORY_PAYLOAD, limit=10)
    assert len(hist) == 2  # the deal:null entry dropped
    # sorted most-recent first
    assert hist[0]["date"] == "2024-11-27" and hist[0]["price"] == 22.19
    assert hist[1]["date"] == "2024-06-01" and hist[1]["price"] == 36.99


def test_history_from_payload_respects_limit_and_zero() -> None:
    assert len(I._history_from_payload(_HISTORY_PAYLOAD, limit=1)) == 1
    assert I._history_from_payload(_HISTORY_PAYLOAD, limit=0) == []
    assert I._history_from_payload("bad", limit=5) == []


def test_normalize_deal_guards() -> None:
    assert I._normalize_deal({"price": {"amount": "x"}}) is None
    assert I._normalize_deal({}) is None
    assert I._normalize_deal({"price": {"amount": 10.0, "currency": "BRL"}}) == {
        "price": 10.0,
        "currency": "BRL",
    }


# --- orchestrator -----------------------------------------------------------


def test_steam_price_history_no_key_is_zero_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VASCO_ITAD_API_KEY", raising=False)

    # If it tried any network, httpx import + call would be reached; stub _lookup
    # to explode so we prove it's never called when there's no key.
    async def _boom(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("network attempted without a key")

    monkeypatch.setattr(I, "_lookup", _boom)
    assert asyncio.run(I.steam_price_history("1145360", cfg=None)) is None


def test_steam_price_history_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _lookup(client, appid, key):
        assert appid == "1145360" and key == "k"
        return "GID-123", "hades"

    async def _store_low(client, gid, key, country):
        assert gid == "GID-123" and country == "BR"
        return I._low_from_payload(_LOW_PAYLOAD)

    async def _history(client, gid, key, country, limit):
        return I._history_from_payload(_HISTORY_PAYLOAD, limit)

    monkeypatch.setattr(I, "_lookup", _lookup)
    monkeypatch.setattr(I, "_store_low", _store_low)
    monkeypatch.setattr(I, "_history", _history)

    out = asyncio.run(
        I.steam_price_history(
            "1145360",
            cfg=Config(adapters=AdaptersCfg(steam=SteamCfg(itad_api_key="k"))),
        )
    )
    assert out is not None
    assert out["itad_id"] == "GID-123"
    assert out["itad_url"] == "https://isthereanydeal.com/game/hades/info/"
    assert out["historical_low"]["price"] == 19.74
    assert out["currency"] == "BRL"
    assert [h["date"] for h in out["price_history"]] == ["2024-11-27", "2024-06-01"]


def test_steam_price_history_game_not_on_itad(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _lookup(client, appid, key):
        return None  # not found

    monkeypatch.setattr(I, "_lookup", _lookup)
    out = asyncio.run(
        I.steam_price_history(
            "999", cfg=Config(adapters=AdaptersCfg(steam=SteamCfg(itad_api_key="k")))
        )
    )
    assert out is None


def test_steam_price_history_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _lookup(client, appid, key):
        raise RuntimeError("transport boom")

    monkeypatch.setattr(I, "_lookup", _lookup)
    out = asyncio.run(
        I.steam_price_history(
            "1145360",
            cfg=Config(adapters=AdaptersCfg(steam=SteamCfg(itad_api_key="k"))),
        )
    )
    assert out is None
