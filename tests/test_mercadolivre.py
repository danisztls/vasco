from __future__ import annotations

from pathlib import Path

import pytest

from vasco.adapters import mercadolivre as M
from vasco.errors import AdapterParseError

FX = Path(__file__).parent / "fixtures" / "mercadolivre"


def _fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


# --- routing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.mercadolivre.com.br/x/p/MLB43417665", True),
        ("https://lista.mercadolivre.com.br/notebook", True),
        ("https://produto.mercadolivre.com.br/MLB-123-foo", True),
        ("https://www.mercadolivre.com.br/ofertas", True),
        # Spanish-country MercadoLibre is out of scope → not matched
        ("https://www.mercadolibre.com.ar/x", False),
        ("https://www.mercadolibre.com.mx/x", False),
        ("https://example.com/", False),
        ("", False),
    ],
)
def test_is_mercadolivre_url(url: str, expected: bool) -> None:
    assert M.is_mercadolivre_url(url) is expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.mercadolivre.com.br/notebook-asus/p/MLB43417665", "product"),
        ("https://produto.mercadolivre.com.br/MLB-123-foo", "product"),
        ("https://articulo.mercadolivre.com.br/MLB-456", "product"),
        ("https://lista.mercadolivre.com.br/notebook", "search"),
        ("https://www.mercadolivre.com.br/ofertas", "search"),
        ("https://www.mercadolivre.com.br/c/informatica", "search"),
    ],
)
def test_page_type(url: str, expected: str) -> None:
    assert M._page_type(url) == expected


# --- numeric / text helpers ------------------------------------------------


def test_brl_to_num() -> None:
    assert M._brl_to_num("R$ 3.899") == 3899
    assert M._brl_to_num("3.185,31") == 3185.31
    assert M._brl_to_num("357,90") == 357.9
    assert M._brl_to_num("Sob consulta") is None


def test_num_passthrough() -> None:
    assert M._num(3579) == 3579  # int stays int
    assert M._num(3185.31) == 3185.31  # fractional float stays float
    assert M._num(3185.0) == 3185  # whole float collapses to int
    assert M._num(None) is None
    assert M._num(True) is None  # bool is not a price


def test_product_id() -> None:
    assert M._product_id("https://x/p/MLB43417665") == "MLB43417665"
    assert M._product_id("MLB-123-foo") == "MLB123"
    assert M._product_id(None, "MLB99") == "MLB99"
    assert M._product_id("no-id-here") is None


def test_brand_name_handles_str_and_object() -> None:
    assert M._brand_name("Asus") == "Asus"
    assert M._brand_name({"@type": "Brand", "name": "Asus"}) == "Asus"
    assert M._brand_name({}) is None


def test_condition() -> None:
    assert M._condition("https://schema.org/NewCondition") == "new"
    assert M._condition("https://schema.org/UsedCondition") == "used"
    assert M._condition("https://schema.org/RefurbishedCondition") == "refurbished"
    assert M._condition(None) is None


def test_sold_quantity() -> None:
    assert M._sold_quantity("Novo | +5 mil vendidos") == 5000
    assert M._sold_quantity("+100 vendidos") == 100
    assert M._sold_quantity("Novo") is None


# --- search parse (JSON-LD @graph) -----------------------------------------


def test_parse_search_from_jsonld_graph() -> None:
    products = M._parse_search(_fx("search.html"))
    assert len(products) == 48  # JSON-LD Product count in the @graph
    first = products[0]
    assert first["position"] == 1
    assert first["title"].startswith("Notebook Asus Vivobook Go 15")
    assert first["price"] == 3579
    assert first["currency"] == "BRL"
    assert first["rating"] == 4.9
    assert first["review_count"] == 2152
    assert first["product_id"] == "MLB43417665"
    assert first["brand"] == "Asus"
    assert first["url"].endswith("/p/MLB43417665")


# --- product parse (JSON-LD spine + best-effort PDP HTML extras) -----------


def test_parse_product_spine_and_extras() -> None:
    [p] = M._parse_product(
        _fx("product.html"),
        "https://www.mercadolivre.com.br/x/p/MLB43417665",
    )
    # JSON-LD spine
    assert p["title"].startswith("Notebook Asus Vivobook Go 15")
    assert p["product_id"] == "MLB43417665"
    assert p["price"] == 3185.31
    assert p["currency"] == "BRL"
    assert p["condition"] == "new"
    assert p["brand"] == "Asus"
    assert p["color"] == "Preto"
    assert p["rating"] == 4.9
    assert p["review_count"] == 997  # prefers reviewCount over ratingCount
    assert p["free_shipping"] is True
    assert p["in_stock"] is True
    assert p["images"] and p["image"] == p["images"][0]
    # best-effort PDP HTML extras
    assert p["sold_quantity"] == 5000
    assert p["seller"] == "Loja oficial Asus"
    assert p["original_price"] == 3899
    assert "10x" in p["installments"]
    assert p["attributes"]  # spec table lifted to a dict


def test_parse_product_raises_without_jsonld() -> None:
    # No Product JSON-LD = MercadoLivre's spine is gone = scraper-rot, not empty.
    with pytest.raises(AdapterParseError):
        M._parse_product("<html><body>no jsonld</body></html>", "https://x")


# --- markdown rendering ----------------------------------------------------


def test_render_markdown_product() -> None:
    products = M._parse_product(
        _fx("product.html"), "https://www.mercadolivre.com.br/x/p/MLB43417665"
    )
    md = M._render_markdown(products, page_type="product", currency="BRL")
    assert "Notebook Asus Vivobook Go 15" in md
    assert "R$ 3.185,31" in md
    assert "frete grátis" in md


def test_render_markdown_search_lists_count() -> None:
    products = M._parse_search(_fx("search.html"))
    md = M._render_markdown(products, page_type="search", currency="BRL")
    assert "48 produtos" in md
    assert "Notebook Asus Vivobook Go 15" in md


# --- fetch via injected escalating fetcher ---------------------------------

SEARCH_URL = "https://lista.mercadolivre.com.br/notebook"
PRODUCT_URL = "https://www.mercadolivre.com.br/notebook-asus/p/MLB43417665"


async def test_fetch_search_uses_injected_fetcher_and_envelope() -> None:
    html = _fx("search.html")
    calls: list[str] = []

    async def fake_fetch_html(url: str):
        calls.append(url)
        return html, 200, {}, M.FailureReason.OK, "browser"

    env = await M.fetch_mercadolivre(SEARCH_URL, fetch_html=fake_fetch_html)

    assert calls == [SEARCH_URL]
    assert env["mode_used"] == "mercadolivre"
    assert env["content_type"] == "application/x-mercadolivre"
    assert env["http_status"] == 200
    assert "failure" not in env
    assert env["quality"]["provider"] == "mercadolivre"
    assert env["quality"]["page_type"] == "search"
    assert env["quality"]["currency"] == "BRL"
    assert env["quality"]["result_count"] == 48


async def test_fetch_product_reports_single_product() -> None:
    html = _fx("product.html")

    async def fake_fetch_html(_url: str):
        return html, 200, {}, M.FailureReason.OK, "browser"

    env = await M.fetch_mercadolivre(PRODUCT_URL, fetch_html=fake_fetch_html)

    assert env["quality"]["page_type"] == "product"
    assert env["quality"]["result_count"] == 1
    assert env["quality"]["products"][0]["product_id"] == "MLB43417665"
    assert env["quality"]["products"][0]["seller"] == "Loja oficial Asus"


async def test_fetch_passes_through_escalation_failure() -> None:
    async def failing_fetch_html(_url: str):
        return "", 0, {}, M.FailureReason.BLOCKED_BOT, "browser"

    env = await M.fetch_mercadolivre(SEARCH_URL, fetch_html=failing_fetch_html)

    assert env["failure"]["reason"] == M.FailureReason.BLOCKED_BOT.value
    assert "browser" in env["failure"]["message"]


async def test_fetch_search_rot_returns_parse_failed() -> None:
    """200 OK but no Product JSON-LD (ML's spine) → PARSE_FAILED, not empty."""

    async def fake_fetch_html(_url: str):
        return (
            "<html><body>no jsonld here</body></html>",
            200,
            {},
            M.FailureReason.OK,
            "browser",
        )

    env = await M.fetch_mercadolivre(SEARCH_URL, fetch_html=fake_fetch_html)

    assert "failure" in env
    assert env["failure"]["reason"] == M.FailureReason.PARSE_FAILED.value
    assert "JSON-LD" in env["failure"]["message"]


async def test_fetch_search_genuine_empty_warns_no_results() -> None:
    """Product JSON-LD present (anchor intact) but missing name/url so nothing
    parses → success with a `no_results` warning, not a rot failure."""
    html = (
        '<html><body><script type="application/ld+json">'
        '{"@graph":[{"@type":"Product"}]}</script></body></html>'
    )

    async def fake_fetch_html(_url: str):
        return html, 200, {}, M.FailureReason.OK, "browser"

    env = await M.fetch_mercadolivre(SEARCH_URL, fetch_html=fake_fetch_html)

    assert "failure" not in env
    assert env["quality"]["result_count"] == 0
    assert "no_results" in env["warnings"]
