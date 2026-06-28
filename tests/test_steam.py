from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from vasco.adapters import steam as S
from vasco.config import AdaptersCfg, Config, SteamCfg
from vasco.errors import AdapterParseError, FailureReason

FX = Path(__file__).parent / "fixtures" / "steam"
APP_ID = "1145360"


def _fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_itad(monkeypatch: pytest.MonkeyPatch):
    """Keep Steam tests hermetic: stub the ITAD enrichment off by default so an
    ambient VASCO_ITAD_API_KEY never makes app fetches hit the network."""

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(S.itad, "steam_price_history", _none)
    yield


# --- routing ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://store.steampowered.com/app/1145360/Hades/", True),
        ("https://store.steampowered.com/app/570", True),
        ("https://store.steampowered.com/search/?term=hades", True),
        ("https://store.steampowered.com/search?term=hades&cc=br", True),
        # claimable nothing: no term, wrong path, non-numeric app id
        ("https://store.steampowered.com/search/", False),
        ("https://store.steampowered.com/app/notanid/", False),
        ("https://store.steampowered.com/bundle/232/Tomb_Raider/", False),
        ("https://store.steampowered.com/sub/12345/", False),
        ("https://store.steampowered.com/", False),
        # other Steam hosts are out of scope
        ("https://steamcommunity.com/app/1145360", False),
        ("https://steamdb.info/app/1145360/", False),
        ("", False),
    ],
)
def test_is_steam_url(url: str, expected: bool) -> None:
    assert S.is_steam_url(url) is expected


def test_claim_app_and_search() -> None:
    assert S._claim("https://store.steampowered.com/app/1145360/Hades/") == (
        "app",
        "1145360",
    )
    assert S._claim("https://store.steampowered.com/search/?term=blue+jeans") == (
        "search",
        "blue jeans",
    )
    assert S._claim("https://store.steampowered.com/wishlist/") is None


# --- value helpers ----------------------------------------------------------


def test_money_cents() -> None:
    assert S._money_cents(7399) == 73.99
    assert S._money_cents(8899) == 88.99
    assert S._money_cents(0) == 0.0
    assert S._money_cents(None) is None
    assert S._money_cents(True) is None
    assert S._money_cents("1999") == 19.99
    assert S._money_cents("nope") is None


def test_platforms_and_int() -> None:
    assert S._platforms({"windows": True, "mac": True, "linux": False}) == [
        "windows",
        "mac",
    ]
    assert S._platforms(None) == []
    assert S._int("93") == 93
    assert S._int(93) == 93
    assert S._int(True) is None
    assert S._int(None) is None
    assert S._int("x") is None


# --- parsing (real fixtures) ------------------------------------------------


def test_parse_app_fixture() -> None:
    p = S._parse_app(_fx("appdetails_1145360.json"), APP_ID, "x")
    assert p is not None
    assert p["app_id"] == APP_ID
    assert p["title"] == "Hades"
    assert p["type"] == "game"
    assert p["is_free"] is False
    assert p["price"] == 73.99
    assert p["currency"] == "BRL"
    assert "original_price" not in p  # no discount
    assert p["metacritic"] == 93
    assert p["genres"] == ["Ação", "Indie", "RPG"]
    assert p["platforms"] == ["windows", "mac"]
    assert p["developers"] == ["Supergiant Games"]
    assert p["publishers"] == ["Supergiant Games"]
    assert p["release_date"] == "17 set. 2020"
    assert p["coming_soon"] is False
    assert p["early_access"] is False
    assert p["required_age"] == 10
    assert p["dlc_count"] == 1
    assert p["url"] == "https://store.steampowered.com/app/1145360/"
    assert p["image"].startswith("https://")


def test_parse_app_early_access() -> None:
    # Steam tags Early Access as genre id 70 (localized description varies).
    body = (
        '{"%s": {"success": true, "data": {"name": "Manor Lords", '
        '"genres": [{"id": "28", "description": "Simulation"}, '
        '{"id": "70", "description": "Early Access"}]}}}' % APP_ID
    )
    p = S._parse_app(body, APP_ID, "x")
    assert p is not None
    assert p["early_access"] is True
    assert "Early Access" in p["genres"]


def test_epoch_to_date() -> None:
    assert S._epoch_to_date(1599999600) == "2020-09-13"
    assert S._epoch_to_date(0) is None
    assert S._epoch_to_date(None) is None
    assert S._epoch_to_date("nope") is None


def test_parse_review_list() -> None:
    reviews = S._parse_review_list(
        json.loads(_fx("appreviews_1145360.json"))["reviews"], 10
    )
    assert len(reviews) == 3
    first = reviews[0]
    assert first["author"] == "Zagreus"
    assert first["recommended"] is True
    assert first["text"].startswith("One more run")
    assert first["language"] == "english"
    assert first["playtime_hours"] == 90.0  # 5400 min at review
    assert first["votes_up"] == 1284
    assert first["early_access"] is True
    assert first["date"] == "2020-09-13"
    # negative review keeps recommended=False (not dropped by compact)
    assert reviews[2]["recommended"] is False
    assert "early_access" not in reviews[2]


def test_parse_review_list_cap_and_skips_empty() -> None:
    raw = [
        {"review": "  ", "voted_up": True},  # blank → skipped
        {"review": "good", "voted_up": True, "author": {"personaname": "A"}},
        {"review": "also good", "voted_up": True, "author": {"personaname": "B"}},
    ]
    out = S._parse_review_list(raw, 1)
    assert len(out) == 1
    assert out[0]["author"] == "A"
    assert S._parse_review_list("not a list", 10) == []


def test_parse_search_fixture() -> None:
    prods = S._parse_search(_fx("storesearch_hades.json"))
    assert len(prods) >= 2
    first = prods[0]
    assert first["position"] == 1
    assert first["app_id"] == "1145350"
    assert first["title"] == "Hades II"
    assert first["price"] == 88.99
    assert first["currency"] == "BRL"
    assert first["metacritic"] == 94
    assert first["url"] == "https://store.steampowered.com/app/1145350/"


# --- anchor / rot / NOT_FOUND -----------------------------------------------


def test_parse_app_success_false_returns_none() -> None:
    # valid shape, no store page → caller emits NOT_FOUND, not PARSE_FAILED
    assert S._parse_app('{"999": {"success": false}}', "999", "x") is None


def test_parse_app_non_json_raises() -> None:
    with pytest.raises(AdapterParseError):
        S._parse_app("<html>nope</html>", APP_ID, "x")


def test_parse_app_missing_node_raises() -> None:
    with pytest.raises(AdapterParseError):
        S._parse_app('{"other": {"success": true}}', APP_ID, "x")


def test_parse_app_success_but_no_data_raises() -> None:
    with pytest.raises(AdapterParseError):
        S._parse_app(f'{{"{APP_ID}": {{"success": true, "data": {{}}}}}}', APP_ID, "x")


def test_parse_search_no_items_raises() -> None:
    with pytest.raises(AdapterParseError):
        S._parse_search('{"total": 0}')


def test_parse_search_empty_items_is_ok() -> None:
    assert S._parse_search('{"total": 0, "items": []}') == []


# --- fetch_steam (injected fetcher) -----------------------------------------


def _app_fetcher(
    *,
    details: str | None = None,
    reviews: str | None = None,
    players: str | None = None,
    details_reason: FailureReason = FailureReason.OK,
    reviews_reason: FailureReason = FailureReason.OK,
    players_reason: FailureReason = FailureReason.OK,
):
    """Injected fetch_html dispatching fixture bodies by endpoint."""
    details = details if details is not None else _fx("appdetails_1145360.json")
    reviews = reviews if reviews is not None else _fx("appreviews_1145360.json")
    players = players if players is not None else _fx("players_1145360.json")

    async def _fetch(target: str):
        if "/api/appdetails" in target:
            return (details, 200, {}, details_reason, "http")
        if "/appreviews/" in target:
            return (reviews, 200, {}, reviews_reason, "http")
        if "GetNumberOfCurrentPlayers" in target:
            return (players, 200, {}, players_reason, "http")
        raise AssertionError(f"unexpected target {target}")

    return _fetch


def test_fetch_app_success_merges_enrichment() -> None:
    env = asyncio.run(
        S.fetch_steam(
            "https://store.steampowered.com/app/1145360/Hades/",
            fetch_html=_app_fetcher(),
        )
    )
    assert env["mode_used"] == "steam"
    assert "failure" not in env
    q = env["quality"]
    assert q["provider"] == "steam"
    assert q["page_type"] == "app"
    assert q["app_id"] == "1145360"
    assert q["currency"] == "BRL"
    assert q["result_count"] == 1
    p = q["products"][0]
    assert p["price"] == 73.99
    assert p["metacritic"] == 93
    assert p["early_access"] is False
    # reviews enrichment (summary + bodies)
    assert p["review_score_desc"] == "Overwhelmingly Positive"
    assert p["total_reviews"] == 304159
    assert len(p["reviews"]) == 3
    assert p["reviews"][0]["author"] == "Zagreus"
    assert p["reviews"][2]["recommended"] is False
    assert "## Reviews (3)" in env["markdown"]
    assert "👎" in env["markdown"]  # the negative review rendered
    # players enrichment
    assert p["player_count"] == 3632


def test_fetch_app_max_reviews_zero_disables_bodies() -> None:
    cfg = Config(adapters=AdaptersCfg(steam=SteamCfg(max_reviews=0)))
    captured: list[str] = []

    async def _fetch(target: str):
        captured.append(target)
        if "/api/appdetails" in target:
            return (_fx("appdetails_1145360.json"), 200, {}, FailureReason.OK, "http")
        if "/appreviews/" in target:
            return (_fx("appreviews_1145360.json"), 200, {}, FailureReason.OK, "http")
        return (_fx("players_1145360.json"), 200, {}, FailureReason.OK, "http")

    env = asyncio.run(
        S.fetch_steam(
            "https://store.steampowered.com/app/1145360/", cfg=cfg, fetch_html=_fetch
        )
    )
    p = env["quality"]["products"][0]
    # summary still folds; individual bodies suppressed and not requested
    assert p["total_reviews"] == 304159
    assert "reviews" not in p
    reviews_url = next(t for t in captured if "/appreviews/" in t)
    assert "num_per_page=0" in reviews_url


def test_fetch_app_enrichment_failure_is_best_effort() -> None:
    env = asyncio.run(
        S.fetch_steam(
            "https://store.steampowered.com/app/1145360/Hades/",
            fetch_html=_app_fetcher(
                reviews_reason=FailureReason.TIMEOUT,
                players="garbage not json",
            ),
        )
    )
    assert "failure" not in env
    p = env["quality"]["products"][0]
    # spine survived; enrichment fields simply absent
    assert p["price"] == 73.99
    assert "review_score_desc" not in p
    assert "player_count" not in p


def test_fetch_app_folds_itad_enrichment(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _itad(appid: str, *, cfg=None, **k):
        assert appid == "1145360"
        return {
            "itad_id": "GID",
            "itad_url": "https://isthereanydeal.com/game/hades/info/",
            "historical_low": {
                "price": 19.74,
                "currency": "BRL",
                "cut": 73,
                "date": "2023-11-21",
            },
            "price_history": [{"date": "2024-11-27", "price": 22.19, "cut": 70}],
            "currency": "BRL",
        }

    monkeypatch.setattr(S.itad, "steam_price_history", _itad)
    env = asyncio.run(
        S.fetch_steam(
            "https://store.steampowered.com/app/1145360/Hades/",
            fetch_html=_app_fetcher(),
        )
    )
    p = env["quality"]["products"][0]
    assert p["historical_low"]["price"] == 19.74
    assert p["price_history"][0]["date"] == "2024-11-27"
    assert p["itad_url"].endswith("/hades/info/")
    assert "all-time low BRL 19.74 (2023-11-21)" in env["markdown"]


def test_merge_itad_ignores_non_dicts() -> None:
    p: dict = {"title": "x"}
    S._merge_itad(p, None)
    S._merge_itad(p, RuntimeError("boom"))
    assert p == {"title": "x"}


def test_fetch_app_success_false_is_not_found() -> None:
    env = asyncio.run(
        S.fetch_steam(
            "https://store.steampowered.com/app/999999999/",
            fetch_html=_app_fetcher(details='{"999999999": {"success": false}}'),
        )
    )
    assert env["failure"]["reason"] == FailureReason.NOT_FOUND


def test_fetch_app_broken_json_is_parse_failed() -> None:
    env = asyncio.run(
        S.fetch_steam(
            "https://store.steampowered.com/app/1145360/",
            fetch_html=_app_fetcher(details="<html>blocked</html>"),
        )
    )
    assert env["failure"]["reason"] == FailureReason.PARSE_FAILED


def test_fetch_app_propagates_fetch_failure() -> None:
    env = asyncio.run(
        S.fetch_steam(
            "https://store.steampowered.com/app/1145360/",
            fetch_html=_app_fetcher(details="", details_reason=FailureReason.TIMEOUT),
        )
    )
    assert env["failure"]["reason"] == FailureReason.TIMEOUT


def test_fetch_search_success() -> None:
    async def _fetch(target: str):
        assert "/api/storesearch/" in target
        return (_fx("storesearch_hades.json"), 200, {}, FailureReason.OK, "http")

    env = asyncio.run(
        S.fetch_steam(
            "https://store.steampowered.com/search/?term=hades", fetch_html=_fetch
        )
    )
    assert env["mode_used"] == "steam"
    q = env["quality"]
    assert q["page_type"] == "search"
    assert q["query"] == "hades"
    assert q["result_count"] >= 2
    assert env["warnings"] == []


def test_fetch_search_empty_is_no_results() -> None:
    async def _fetch(target: str):
        return ('{"total": 0, "items": []}', 200, {}, FailureReason.OK, "http")

    env = asyncio.run(
        S.fetch_steam(
            "https://store.steampowered.com/search/?term=zzznope", fetch_html=_fetch
        )
    )
    assert "failure" not in env
    assert env["quality"]["result_count"] == 0
    assert env["warnings"] == ["no_results"]


def test_region_honors_cfg() -> None:
    cfg = Config(adapters=AdaptersCfg(steam=SteamCfg(country="US", language="english")))
    captured: list[str] = []

    async def _fetch(target: str):
        captured.append(target)
        if "/api/appdetails" in target:
            return (_fx("appdetails_1145360.json"), 200, {}, FailureReason.OK, "http")
        if "/appreviews/" in target:
            return (_fx("appreviews_1145360.json"), 200, {}, FailureReason.OK, "http")
        return (_fx("players_1145360.json"), 200, {}, FailureReason.OK, "http")

    asyncio.run(
        S.fetch_steam(
            "https://store.steampowered.com/app/1145360/", cfg=cfg, fetch_html=_fetch
        )
    )
    details_url = next(t for t in captured if "/api/appdetails" in t)
    assert "cc=us" in details_url and "l=english" in details_url


def test_fetch_steam_no_fetcher_fails_cleanly() -> None:
    env = asyncio.run(
        S.fetch_steam("https://store.steampowered.com/app/1145360/", fetch_html=None)
    )
    assert env["failure"]["reason"] == FailureReason.SERVER_ERROR
