from __future__ import annotations

from pathlib import Path

import pytest

from vasco.adapters import realestate as R
from vasco.errors import AdapterParseError

FX = Path(__file__).parent / "fixtures" / "realestate"


def _fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


# --- routing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.vivareal.com.br/aluguel/sp/sao-carlos/", True),
        ("https://vivareal.com.br/imovel/x-id-1/", True),
        ("https://www.zapimoveis.com.br/aluguel/", False),
        ("https://example.com/", False),
        ("", False),
    ],
)
def test_is_realestate_url(url: str, expected: bool) -> None:
    assert R.is_realestate_url(url) is expected


@pytest.mark.parametrize(
    "provider,url,expected",
    [
        ("vivareal", "https://www.vivareal.com.br/aluguel/sp/x/", "list"),
        ("vivareal", "https://www.vivareal.com.br/imovel/casa-id-1/", "detail"),
    ],
)
def test_page_type(provider: str, url: str, expected: str) -> None:
    assert R._page_type(url, provider) == expected


# --- numeric helpers -------------------------------------------------------


def test_as_int() -> None:
    assert R._as_int("2 quartos") == 2
    assert R._as_int("45M²m2") == 45
    assert R._as_int(90.0) == 90
    assert R._as_int("sem garagem") is None


# --- vivareal (JSON-LD) ----------------------------------------------------


def test_vivareal_detail_parses_product() -> None:
    [ln] = R._vivareal_detail(_fx("vivareal_detail.html"))
    assert ln["price"] == 1278
    assert ln["bedrooms"] == 2
    assert ln["bathrooms"] == 1
    assert ln["parking"] == 1
    assert ln["area"] == 90
    assert ln["neighborhood"] == "Jardim Ipanema"
    assert ln["city"] == "São Carlos"
    assert ln["url"].endswith("id-2889266573/")
    assert ln["image"] and ln["images"]
    assert ln["title"] and "São Carlos" in ln["title"]
    assert ln["description"] and "dormit" in ln["description"]


def _vivareal_itemlist_html(items: list[dict]) -> str:
    """Minimal page with a vivareal-shaped ItemList JSON-LD block."""
    import json

    doc = {
        "@type": "ItemList",
        "itemListElement": [{"item": it} for it in items],
    }
    return f'<html><script type="application/ld+json">{json.dumps(doc)}</script></html>'


def test_vivareal_condo_fee_from_additional_property() -> None:
    # VivaReal moved the condo fee from `offers.propertyValue` (legacy) to a
    # single `offers.additionalProperty`. Both forms, plus a fee-less house.
    html = _vivareal_itemlist_html(
        [
            {
                "@type": "Apartment",
                "url": "https://www.vivareal.com.br/imovel/id-1/",
                "name": "Apartamento em Centro, São Carlos",
                "offers": {
                    "price": 1667,
                    "additionalProperty": {
                        "@type": "PropertyValue",
                        "name": "Condominium Fee",
                        "value": 318,
                    },
                },
            },
            {
                "@type": "Apartment",
                "url": "https://www.vivareal.com.br/imovel/id-2/",
                "name": "Apartamento em Centro, São Carlos",
                "offers": {
                    "price": 1200,
                    "propertyValue": [
                        {"name": "Condominium Fee", "value": 250},
                    ],
                },
            },
            {
                "@type": "House",
                "url": "https://www.vivareal.com.br/imovel/id-3/",
                "name": "Casa em Vila Brasília, São Carlos",
                "offers": {"price": 1500},
            },
        ]
    )
    new, legacy, house = R._vivareal_list(html)
    assert new["price"] == 1667 and new["condo_fee"] == 318
    assert legacy["price"] == 1200 and legacy["condo_fee"] == 250
    assert house["price"] == 1500 and house["condo_fee"] is None


def test_vivareal_list_raises_without_itemlist() -> None:
    # The detail fixture has a Product, not an ItemList — the list parser's
    # anchor is absent, which signals scraper-rot, not an empty result.
    with pytest.raises(AdapterParseError):
        R._vivareal_list(_fx("vivareal_detail.html"))


# --- markdown rendering ----------------------------------------------------


def test_render_markdown() -> None:
    html = _vivareal_itemlist_html(
        [
            {
                "@type": "Apartment",
                "url": "https://www.vivareal.com.br/imovel/id-1/",
                "name": "Apartamento em Centro, São Carlos",
                "offers": {"price": 1667},
                "floorSize": {"value": 45},
            }
        ]
    )
    md = R._render_markdown(R._vivareal_list(html))
    assert "Apartamento em Centro" in md  # leads with title when present
    assert "45m²" in md
    assert "São Carlos" in md


# --- fetch via injected escalating fetcher ---------------------------------

VIVAREAL_LIST_URL = "https://www.vivareal.com.br/aluguel/sp/sao-carlos/"


async def test_fetch_uses_injected_fetcher_and_records_tier() -> None:
    """An injected fetcher (the shared escalation chain) supplies the HTML;
    the adapter parses it into the realestate envelope."""
    html = _vivareal_itemlist_html(
        [
            {
                "@type": "Apartment",
                "url": "https://www.vivareal.com.br/imovel/id-1/",
                "name": "Apartamento em Centro, São Carlos",
                "offers": {"price": 1667},
            }
        ]
    )
    calls: list[str] = []

    async def fake_fetch_html(url: str):
        calls.append(url)
        return html, 200, {}, R.FailureReason.OK, "http"

    env = await R.fetch_realestate(VIVAREAL_LIST_URL, fetch_html=fake_fetch_html)

    assert calls == [VIVAREAL_LIST_URL]
    assert env["mode_used"] == "realestate"  # envelope contract is preserved
    assert env["http_status"] == 200
    assert "failure" not in env
    assert env["quality"]["result_count"] > 0


async def test_fetch_passes_through_escalation_failure() -> None:
    """When every tier fails, the adapter surfaces that reason/tier verbatim."""

    async def failing_fetch_html(url: str):
        return "", 0, {}, R.FailureReason.TIMEOUT, "wayback"

    env = await R.fetch_realestate(VIVAREAL_LIST_URL, fetch_html=failing_fetch_html)

    assert env["failure"]["reason"] == R.FailureReason.TIMEOUT.value
    assert "wayback" in env["failure"]["message"]


async def test_fetch_list_rot_returns_parse_failed() -> None:
    """200 OK but the provider's list anchor is gone → PARSE_FAILED, not a silent
    empty success."""

    async def fake_fetch_html(_url: str):
        return (
            "<html><body>no cards</body></html>",
            200,
            {},
            R.FailureReason.OK,
            "http",
        )

    env = await R.fetch_realestate(VIVAREAL_LIST_URL, fetch_html=fake_fetch_html)

    assert "failure" in env
    assert env["failure"]["reason"] == R.FailureReason.PARSE_FAILED.value
    assert "ItemList" in env["failure"]["message"]


async def test_fetch_list_genuine_empty_warns_no_results() -> None:
    """Anchor present but the ItemList is empty → a real empty result:
    success with a `no_results` warning."""
    html = _vivareal_itemlist_html([])

    async def fake_fetch_html(_url: str):
        return html, 200, {}, R.FailureReason.OK, "http"

    env = await R.fetch_realestate(VIVAREAL_LIST_URL, fetch_html=fake_fetch_html)

    assert "failure" not in env
    assert env["quality"]["result_count"] == 0
    assert "no_results" in env["warnings"]
