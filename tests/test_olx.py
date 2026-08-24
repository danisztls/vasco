# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import re
from pathlib import Path

import pytest

from vasco.adapters import olx as O

FX = Path(__file__).parent / "fixtures" / "olx"


def _fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


# --- routing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.olx.com.br/imoveis/aluguel/estado-sp", True),
        ("https://sp.olx.com.br/sao-paulo-e-regiao/imoveis/apto-1507054758", True),
        (
            "https://www.olx.com.br/autos-e-pecas/carros-vans-e-utilitarios/estado-sp",
            True,
        ),
        ("https://sp.olx.com.br/x/autos-e-pecas/carros/fiat-1483248894", True),
        # category-landing hubs are still OLX URLs (matched); fetch_olx returns a
        # CATEGORY_LANDING failure for them (see test below), not PARSE_FAILED.
        ("https://www.olx.com.br/imoveis/estado-sp", True),
        ("https://www.olx.com.br/imoveis", True),
        # other OLX categories fall through to the normal fetch path
        ("https://www.olx.com.br/eletronicos-e-celulares/estado-sp", False),
        ("https://www.olx.com.br/", False),
        ("https://www.zapimoveis.com.br/aluguel/", False),
        ("https://example.com/", False),
        ("", False),
    ],
)
def test_is_olx_url(url: str, expected: bool) -> None:
    assert O.is_olx_url(url) is expected


@pytest.mark.parametrize(
    "url,expected",
    [
        # hubs: bare vertical, or vertical + single state-location segment
        ("https://www.olx.com.br/imoveis", True),
        ("https://www.olx.com.br/imoveis/estado-sp", True),
        ("https://www.olx.com.br/autos-e-pecas/estado-rj", True),
        ("https://olx.com.br/imoveis/estado-sp", True),  # bare host
        # not hubs: transaction type, deeper drill-down, or subcategory
        ("https://www.olx.com.br/imoveis/venda", False),
        ("https://www.olx.com.br/imoveis/aluguel/estado-sp", False),
        ("https://www.olx.com.br/imoveis/estado-sp/sao-paulo-e-regiao", False),
        (
            "https://www.olx.com.br/autos-e-pecas/carros-vans-e-utilitarios/estado-sp",
            False,
        ),
        # regional subdomains (detail + region lists) are never treated as hubs
        ("https://sp.olx.com.br/imoveis/estado-sp", False),
        # non-OLX / non-vertical
        ("https://example.com/imoveis/estado-sp", False),
        ("https://www.olx.com.br/eletronicos-e-celulares/estado-sp", False),
    ],
)
def test_is_category_hub(url: str, expected: bool) -> None:
    assert O._is_category_hub(url) is expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.olx.com.br/imoveis/aluguel/estado-sp", "list"),
        ("https://www.olx.com.br/imoveis/aluguel/estado-sp?o=2", "list"),
        ("https://sp.olx.com.br/x/imoveis/apartamento-vista-1507054758", "detail"),
        ("https://sp.olx.com.br/x/imoveis/apartamento-vista-1507054758/", "detail"),
        (
            "https://sp.olx.com.br/x/autos-e-pecas/carros/fiat-cronos-2025-1483248894",
            "detail",
        ),
    ],
)
def test_page_type(url: str, expected: str) -> None:
    assert O._page_type(url) == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.olx.com.br/imoveis/aluguel/estado-sp", "realestate"),
        (
            "https://www.olx.com.br/autos-e-pecas/carros-vans-e-utilitarios/estado-sp",
            "vehicles",
        ),
        ("https://www.olx.com.br/eletronicos-e-celulares/estado-sp", None),
        ("https://example.com/imoveis/", None),
    ],
)
def test_vertical(url: str, expected: str | None) -> None:
    assert O._vertical(url) == expected


# --- numeric / text helpers ------------------------------------------------


def test_brl_int() -> None:
    assert O._brl_int("R$ 2.200") == 2200
    assert O._brl_int("R$ 99.890") == 99890
    assert O._brl_int("R$ 1.278,00") == 1278
    assert O._brl_int(2200) == 2200
    assert O._brl_int("Sob consulta") is None
    assert O._brl_int(None) is None


def test_as_int() -> None:
    assert O._as_int("32m²") == 32
    assert O._as_int("22948") == 22948
    assert O._as_int("4 portas") == 4
    assert O._as_int("0") == 0  # garage_spaces="0" is a real value, not missing
    assert O._as_int("sem garagem") is None


def test_strip_html() -> None:
    assert O._strip_html("a<br><br>b") == "a\n\nb"
    assert O._strip_html("") is None
    assert O._strip_html(None) is None


# --- real-estate list (__NEXT_DATA__) --------------------------------------


def test_re_list_parses_typed_attributes() -> None:
    ads = O._parse_next_data(_fx("re_list.html"))
    assert len(ads) == 3
    ln = O._normalize_ad(ads[0], vertical="realestate", page_type="list", base_url="x")
    assert ln["title"] == "Apartamento Vista Portal do Morumbi"
    assert ln["price"] == 2200
    assert ln["vertical"] == "realestate"
    assert (ln["neighborhood"], ln["municipality"], ln["uf"]) == (
        "Vila Suzana",
        "São Paulo",
        "SP",
    )
    assert ln["url"].endswith("-1507054758")
    assert ln["image"] and len(ln["images"]) == 1  # list = single thumbnail
    assert ln["description"] is None  # description is detail-only
    a = ln["attributes"]
    assert (a["area"], a["bedrooms"], a["bathrooms"], a["parking"]) == (32, 2, 1, 0)
    assert a["condo_fee"] == 322 and a["iptu"] == 10
    assert a["type"] == "Aluguel - apartamento padrão"
    assert "Academia" in a["amenities"]


# --- vehicle list (__NEXT_DATA__) ------------------------------------------


def test_car_list_parses_typed_attributes() -> None:
    ads = O._parse_next_data(_fx("car_list.html"))
    ln = O._normalize_ad(ads[0], vertical="vehicles", page_type="list", base_url="x")
    assert ln["title"].startswith("Fiat Cronos")
    assert ln["price"] == 99890
    a = ln["attributes"]
    assert a["brand"] == "Fiat"
    assert a["year"] == 2025
    assert a["mileage"] == 22948
    assert a["fuel"] == "Flex"
    assert a["gearbox"] == "Automático"
    assert a["doors"] == 4
    assert a["motorpower"] == "1.3"  # decimal liters kept as string, not 13
    assert "Ar condicionado" in a["features"]


# --- detail pages (initial-data) -------------------------------------------


def test_re_detail_parses_body_and_gallery() -> None:
    ad = O._extract_detail_ad(_fx("re_detail.html"))
    assert ad is not None
    ln = O._normalize_ad(
        ad, vertical="realestate", page_type="detail", base_url="https://x-1507054758"
    )
    assert ln["price"] == 2200
    assert ln["description"] and "dormitórios" in ln["description"]
    assert ln["images"]  # full gallery (capped)
    assert ln["attributes"]["bedrooms"] == 2


def test_car_detail_parses_body_and_attributes() -> None:
    ad = O._extract_detail_ad(_fx("car_detail.html"))
    ln = O._normalize_ad(ad, vertical="vehicles", page_type="detail", base_url="x")
    assert ln["description"] and "FIREFLY" in ln["description"]
    assert ln["attributes"]["year"] == 2025
    assert ln["attributes"]["brand"] == "Fiat"


def test_detail_falls_back_to_jsonld_without_initial_data() -> None:
    """When the initial-data blob is absent, the schema.org JSON-LD carries
    enough (title, price, images) for a thin listing."""
    html = re.sub(
        r'<script id="initial-data".*?</script>',
        "",
        _fx("car_detail.html"),
        flags=re.DOTALL,
    )
    assert O._extract_detail_ad(html) is None
    ln = O._jsonld_detail(html, "https://sp.olx.com.br/x-1483248894", "vehicles")
    assert ln is not None
    assert ln["title"].startswith("Fiat Cronos")
    assert ln["price"] == 99890
    assert ln["images"]


# --- markdown rendering ----------------------------------------------------


def test_render_markdown_realestate() -> None:
    ads = O._parse_next_data(_fx("re_list.html"))
    lst = [
        O._normalize_ad(a, vertical="realestate", page_type="list", base_url="x")
        for a in ads
    ]
    md = O._render_markdown(lst, "realestate")
    assert "Apartamento Vista Portal do Morumbi" in md
    assert "R$ 2.200" in md
    assert "32m²" in md
    assert "Vila Suzana, São Paulo" in md


def test_render_markdown_vehicles() -> None:
    ads = O._parse_next_data(_fx("car_list.html"))
    lst = [
        O._normalize_ad(a, vertical="vehicles", page_type="list", base_url="x")
        for a in ads
    ]
    md = O._render_markdown(lst, "vehicles")
    assert "Fiat Cronos" in md
    assert "22.948 km" in md
    assert "Flex" in md


# --- fetch via injected escalating fetcher ---------------------------------

RE_LIST_URL = "https://www.olx.com.br/imoveis/aluguel/estado-sp"


async def test_fetch_uses_injected_fetcher_and_preserves_envelope() -> None:
    """An injected fetcher (the shared escalation chain) supplies the HTML;
    OLX resolves at the cheap http tier — no browser."""
    html = _fx("re_list.html")
    calls: list[str] = []

    async def fake_fetch_html(url: str):
        calls.append(url)
        return html, 200, {}, O.FailureReason.OK, "http"

    env = await O.fetch_olx(RE_LIST_URL, fetch_html=fake_fetch_html)

    assert calls == [RE_LIST_URL]
    assert env["mode_used"] == "olx"
    assert env["content_type"] == "application/x-olx"
    assert env["http_status"] == 200
    assert "failure" not in env
    assert env["quality"]["provider"] == "olx"
    assert env["quality"]["vertical"] == "realestate"
    assert env["quality"]["page_type"] == "list"
    assert env["quality"]["result_count"] == 3


async def test_fetch_detail_reports_vertical_and_one_listing() -> None:
    html = _fx("car_detail.html")
    url = "https://sp.olx.com.br/x/autos-e-pecas/carros/fiat-cronos-2025-1483248894"

    async def fake_fetch_html(_url: str):
        return html, 200, {}, O.FailureReason.OK, "http"

    env = await O.fetch_olx(url, fetch_html=fake_fetch_html)

    assert env["quality"]["vertical"] == "vehicles"
    assert env["quality"]["page_type"] == "detail"
    assert env["quality"]["result_count"] == 1
    assert env["quality"]["listings"][0]["attributes"]["brand"] == "Fiat"


async def test_fetch_passes_through_escalation_failure() -> None:
    """When every tier fails, the adapter surfaces that reason/tier verbatim."""

    async def failing_fetch_html(_url: str):
        return "", 0, {}, O.FailureReason.TIMEOUT, "wayback"

    env = await O.fetch_olx(RE_LIST_URL, fetch_html=failing_fetch_html)

    assert env["failure"]["reason"] == O.FailureReason.TIMEOUT.value
    assert "wayback" in env["failure"]["message"]


async def test_fetch_list_rot_returns_parse_failed() -> None:
    """200 OK but the __NEXT_DATA__ anchor is gone → PARSE_FAILED (scraper-rot),
    not a silent empty success."""

    async def fake_fetch_html(_url: str):
        return (
            "<html><body>no next data</body></html>",
            200,
            {},
            O.FailureReason.OK,
            "http",
        )

    env = await O.fetch_olx(RE_LIST_URL, fetch_html=fake_fetch_html)

    assert "failure" in env
    assert env["failure"]["reason"] == O.FailureReason.PARSE_FAILED.value
    assert "__NEXT_DATA__" in env["failure"]["message"]


async def test_fetch_list_genuine_empty_warns_no_results() -> None:
    """__NEXT_DATA__ present but zero ads → a real empty result: success with a
    `no_results` warning, distinct from the rot case above."""
    html = (
        '<html><body><script id="__NEXT_DATA__">'
        '{"props":{"pageProps":{"ads":[]}}}</script></body></html>'
    )

    async def fake_fetch_html(_url: str):
        return html, 200, {}, O.FailureReason.OK, "http"

    env = await O.fetch_olx(RE_LIST_URL, fetch_html=fake_fetch_html)

    assert "failure" not in env
    assert env["quality"]["result_count"] == 0
    assert "no_results" in env["warnings"]


async def test_fetch_category_hub_short_circuits_without_fetch() -> None:
    """A bare category-landing hub (/imoveis/estado-sp) is App-Router with no
    listings: fetch_olx returns CATEGORY_LANDING *without* fetching, and never
    masquerades as PARSE_FAILED scraper-rot."""
    fetched = False

    async def fake_fetch_html(_url: str):  # pragma: no cover - must not run
        nonlocal fetched
        fetched = True
        return "", 200, {}, O.FailureReason.OK, "browser"

    env = await O.fetch_olx(
        "https://www.olx.com.br/imoveis/estado-sp", fetch_html=fake_fetch_html
    )

    assert fetched is False  # short-circuited before any (browser) fetch
    assert env["mode_used"] == "olx"
    assert env["failure"]["reason"] == O.FailureReason.CATEGORY_LANDING.value
    assert env["failure"]["reason"] != O.FailureReason.PARSE_FAILED.value
    assert "narrow" in env["failure"]["message"].lower()
