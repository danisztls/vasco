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
        ("https://corretorromildobinda.com.br/pt/pesq_imovel.php?ptip=A", True),
        ("https://www.barretoimobiliaria.com/imoveis/?x=1", True),
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
        ("barreto", "https://www.barretoimobiliaria.com/imoveis/?x=1", "list"),
        ("barreto", "https://www.barretoimobiliaria.com/imovel/kitnet/", "detail"),
        ("binda", "https://corretorromildobinda.com.br/pt/pesq_imovel.php", "list"),
        ("binda", "https://corretorromildobinda.com.br/pt/imovel.php?id=326", "detail"),
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


def test_vivareal_list_returns_empty_without_itemlist() -> None:
    # The detail fixture has a Product, not an ItemList.
    assert R._vivareal_list(_fx("vivareal_detail.html")) == []


# --- binda (CSS cards) -----------------------------------------------------


def test_binda_list_parses_cards() -> None:
    base = "https://corretorromildobinda.com.br/pt/pesq_imovel.php"
    items = R._binda_list(_fx("binda_list.html"), base)
    assert len(items) == 9
    first = items[0]
    assert first["url"].startswith(
        "https://corretorromildobinda.com.br/pt/imovel.php?id="
    )
    assert first["price"] == 900
    assert first["image"]


def test_binda_detail_extracts_gallery() -> None:
    base = "https://corretorromildobinda.com.br/pt/"
    url = "https://corretorromildobinda.com.br/pt/imovel.php?id=326"
    [ln] = R._binda_detail(_fx("binda_detail.html"), base, url)
    assert ln["url"] == url
    assert len(ln["images"]) == 4  # capped gallery
    assert all("_848.jpeg" in u for u in ln["images"])
    # No bogus neighborhood scraped from the footer.
    assert ln["neighborhood"] is None


# --- barreto (Elementor) ---------------------------------------------------


def test_barreto_list_positional_specs() -> None:
    items = R._barreto_list(
        _fx("barreto_list.html"), "https://www.barretoimobiliaria.com/"
    )
    assert len(items) == 1
    ln = items[0]
    assert ln["type"] == "Apartamento"
    assert (ln["bedrooms"], ln["bathrooms"], ln["parking"], ln["area"]) == (1, 1, 1, 45)
    assert ln["neighborhood"] == "SAPUCAIA"
    assert ln["city"] == "Baixo Guandu"
    assert ln["url"].endswith("/imovel/partamentos-para-locacao/")


def test_barreto_detail_labeled_specs_and_gallery() -> None:
    url = "https://www.barretoimobiliaria.com/imovel/kitnet/"
    [ln] = R._barreto_detail(
        _fx("barreto_detail.html"), "https://www.barretoimobiliaria.com/", url
    )
    assert (ln["bedrooms"], ln["bathrooms"], ln["parking"], ln["area"]) == (1, 1, 1, 45)
    assert ln["neighborhood"] == "SAPUCAIA"
    assert ln["images"]  # gallery from /wp-content/uploads/, logo excluded
    assert all("logo" not in u.lower() for u in ln["images"])


# --- markdown rendering ----------------------------------------------------


def test_render_markdown() -> None:
    items = R._barreto_list(
        _fx("barreto_list.html"), "https://www.barretoimobiliaria.com/"
    )
    md = R._render_markdown(items)
    assert "Apartamento" in md
    assert "45m²" in md
    assert "Baixo Guandu" in md
