from __future__ import annotations

from pathlib import Path

import pytest

from vasco.adapters import realestate as R

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


def test_brl_int() -> None:
    assert R._brl_int("R$ 1.278,00") == 1278
    assert R._brl_int("R$ 900") == 900
    assert R._brl_int(1278) == 1278
    assert R._brl_int("Sob consulta") is None
    assert R._brl_int(None) is None


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


def test_vivareal_list_returns_empty_without_itemlist() -> None:
    # The detail fixture has a Product, not an ItemList.
    assert R._vivareal_list(_fx("vivareal_detail.html")) == []














# --- markdown rendering ----------------------------------------------------


