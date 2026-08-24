# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from vasco.adapters import amazon as A
from vasco.errors import AdapterParseError

FX = Path(__file__).parent / "fixtures" / "amazon"


def _fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


SEARCH_URL = "https://www.amazon.com.br/s?k=kindle+paperwhite"
PRODUCT_URL = "https://www.amazon.com.br/kindle-paperwhite/dp/B0CFPL6CFY"


# --- routing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (SEARCH_URL, True),
        (PRODUCT_URL, True),
        # product forms: bare /dp/, tracking ref slug, /gp/product/, /gp/aw/d/
        ("https://www.amazon.com.br/dp/B0CFPL6CFY", True),
        ("https://www.amazon.com.br/dp/B0CFPL6CFY/ref=sr_1_1?keywords=x", True),
        ("https://www.amazon.com.br/gp/product/B08N3TCP2F", True),
        ("https://www.amazon.com.br/gp/aw/d/B08N3TCP2F", True),
        # non-listing Amazon URLs fall through (not claimed)
        ("https://www.amazon.com.br/", False),
        ("https://www.amazon.com.br/gp/cart/view.html", False),
        ("https://www.amazon.com.br/b?node=16243890011", False),
        ("https://www.amazon.com.br/s?rh=node%3A123", False),  # browse, no k=
        # other-country Amazon is out of scope
        ("https://www.amazon.com/dp/B0CFPL6CFY", False),
        ("https://www.amazon.co.uk/dp/B0CFPL6CFY", False),
        ("https://example.com/dp/B0CFPL6CFY", False),
        ("", False),
    ],
)
def test_is_amazon_url(url: str, expected: bool) -> None:
    assert A.is_amazon_url(url) is expected


def test_asin_and_page_type() -> None:
    assert A._asin(PRODUCT_URL) == "B0CFPL6CFY"
    assert A._asin("https://www.amazon.com.br/gp/product/B08N3TCP2F") == "B08N3TCP2F"
    assert A._asin(SEARCH_URL) is None
    assert A._page_type(PRODUCT_URL) == "product"
    assert A._page_type(SEARCH_URL) == "search"
    assert A._page_type("https://www.amazon.com.br/") is None


def test_canonical_url_drops_tracking() -> None:
    # The clean /dp/<ASIN> form, host-preserving, ref/query stripped.
    assert (
        A._canonical_url(
            "B0CFPL6CFY", "https://www.amazon.com.br/x/dp/B0CFPL6CFY/ref=y"
        )
        == "https://www.amazon.com.br/dp/B0CFPL6CFY"
    )


# --- numeric / text helpers ------------------------------------------------


def test_rating_num_pt_br_comma() -> None:
    # PT-BR rating is comma-decimal; isolate the leading 0-5 number.
    assert A._rating_num("4,8 de 5 estrelas") == 4.8
    assert A._rating_num("4,8 de 5") == 4.8
    assert A._rating_num("5,0 de 5 estrelas") == 5.0
    assert A._rating_num("4.8") == 4.8
    assert A._rating_num("") is None
    assert A._rating_num(None) is None
    assert A._rating_num("sem avaliações") is None


def test_price_from_el_offscreen_and_visible() -> None:
    # Search cards populate .a-offscreen ("R$ 879,00").
    off = A._soup(
        '<span class="a-price"><span class="a-offscreen">R$ 879,00</span></span>'
    )
    assert A._price_from_el(off.select_one(".a-price")) == 879
    # Product pages often leave .a-offscreen empty; reassemble visible spans.
    vis = A._soup(
        '<span class="a-price"><span class="a-offscreen"></span>'
        '<span class="a-price-whole">1.199<span class="a-price-decimal">,</span></span>'
        '<span class="a-price-fraction">00</span></span>'
    )
    assert A._price_from_el(vis.select_one(".a-price")) == 1199
    assert A._price_from_el(None) is None


def test_list_price_guard() -> None:
    # A real "De:" list price is strictly greater than the current price; a
    # per-unit price masquerading as .a-text-price is dropped.
    assert A._list_price(935.11, 879) == 935.11
    assert A._list_price(64.9, 649) is None  # per-unit price below current
    assert A._list_price(879, 879) is None
    assert A._list_price(None, 879) is None
    assert A._list_price(900, None) is None


def test_clean_img_strips_cdn_suffix() -> None:
    assert (
        A._clean_img("https://m.media-amazon.com/images/I/81-vCHKJb1L._AC_SL1500_.jpg")
        == "https://m.media-amazon.com/images/I/81-vCHKJb1L.jpg"
    )
    assert (
        A._clean_img("https://m.media-amazon.com/images/I/712JlBgtkJL._AC_UL320_.jpg")
        == "https://m.media-amazon.com/images/I/712JlBgtkJL.jpg"
    )
    assert A._clean_img(None) is None


def test_brand_strips_prefixes() -> None:
    assert A._brand(A._soup('<span id="bylineInfo">Marca: Amazon</span>')) == "Amazon"
    assert A._brand(A._soup('<a id="bylineInfo">Visite a loja Amazon</a>')) == "Amazon"
    assert A._brand(A._soup("<div>no byline</div>")) is None


def test_specs_parses_tables_and_bullets() -> None:
    snippet = """
    <table id="productDetails_techSpec_section_1">
      <tr><th>Marca</th><td>Amazon</td></tr>
      <tr><th>Capacidade de armazenamento</th><td>16 GB</td></tr>
    </table>
    <div id="detailBullets_feature_div"><ul>
      <li><span class="a-list-item"><span>Peso do produto</span><span>211 g</span></span></li>
      <li><span class="a-list-item"><span>ASIN</span><span>B0CFPL6CFY</span></span></li>
      <li><span class="a-list-item"><span>Avaliações de clientes</span>
        <span>4,8 4,8 de 5 estrelas (8.400)</span></span></li>
    </ul></div>
    """
    specs = A._specs(A._soup(snippet))
    # ASIN + customer-reviews rows are dropped (they restate normalized fields).
    assert specs == {
        "Marca": "Amazon",
        "Capacidade de armazenamento": "16 GB",
        "Peso do produto": "211 g",
    }


# --- search parse ----------------------------------------------------------


def test_parse_search_cards() -> None:
    prods = A._parse_search(_fx("search.html"), SEARCH_URL)
    assert len(prods) == 4
    p = prods[0]
    assert p["position"] == 1
    assert p["asin"] == "B0CFPL6CFY"
    assert p["title"].startswith("Kindle Paperwhite")
    assert p["url"] == "https://www.amazon.com.br/dp/B0CFPL6CFY"  # clean /dp/ form
    assert p["price"] == 879
    assert p["original_price"] == 935.11  # struck list price (above current)
    assert p["rating"] == 4.8
    assert p["review_count"] == 8399
    assert p["image"].endswith(".jpg") and "_AC_" not in p["image"]
    # a card whose .a-text-price is a per-unit price keeps no fake discount
    # (compact drops the null field entirely)
    assert prods[2].get("original_price") is None


def test_parse_search_rot_raises() -> None:
    # No result cards AND no results container = the anchor is gone = rot/wall.
    with pytest.raises(AdapterParseError):
        A._parse_search("<html><body>no results here</body></html>", SEARCH_URL)


def test_parse_search_empty_container_is_no_results() -> None:
    # Container present but holding zero cards = a genuinely empty search.
    html = '<div class="s-main-slot s-result-list"></div>'
    assert A._parse_search(html, SEARCH_URL) == []


# --- product parse ---------------------------------------------------------


def test_parse_product_spine() -> None:
    [p] = A._parse_product(_fx("product.html"), PRODUCT_URL)
    assert p["title"].startswith("Kindle Paperwhite 16 GB")
    assert p["asin"] == "B0CFPL6CFY"
    assert p["url"] == "https://www.amazon.com.br/dp/B0CFPL6CFY"
    assert (
        p["price"] == 879
    )  # reassembled from visible .a-price-whole/.a-price-fraction
    assert p["rating"] == 4.8
    assert p["review_count"] == 8400
    assert p["brand"] == "Amazon"
    assert p["availability"] == "Em estoque"
    assert p["in_stock"] is True
    assert len(p["features"]) == 7
    assert p["image"].endswith(".jpg") and "_AC_" not in p["image"]


def test_parse_product_raises_without_title() -> None:
    # #productTitle is the product page's anchor; its absence is rot/wall.
    with pytest.raises(AdapterParseError):
        A._parse_product("<html><body>no title</body></html>", PRODUCT_URL)


# --- markdown rendering ----------------------------------------------------


def test_render_markdown_product() -> None:
    products = A._parse_product(_fx("product.html"), PRODUCT_URL)
    for p in products:
        p.setdefault("currency", "BRL")
    md = A._render_markdown(products, page_type="product", currency="BRL")
    assert "Kindle Paperwhite 16 GB" in md
    assert "R$ 879" in md
    assert "Marca: Amazon" in md


def test_render_markdown_search() -> None:
    products = A._parse_search(_fx("search.html"), SEARCH_URL)
    for p in products:
        p.setdefault("currency", "BRL")
    md = A._render_markdown(products, page_type="search", currency="BRL")
    assert "4 produtos" in md
    assert "R$ 879" in md


# --- fetch via injected escalating fetcher ---------------------------------


async def test_fetch_search_envelope() -> None:
    html = _fx("search.html")
    calls: list[str] = []

    async def fake_fetch_html(url: str):
        calls.append(url)
        return html, 200, {}, A.FailureReason.OK, "http"

    env = await A.fetch_amazon(SEARCH_URL, fetch_html=fake_fetch_html)

    assert calls == [SEARCH_URL]
    assert env["mode_used"] == "amazon"
    assert env["content_type"] == "application/x-amazon"
    assert env["http_status"] == 200
    assert "failure" not in env
    assert env["quality"]["provider"] == "amazon"
    assert env["quality"]["page_type"] == "search"
    assert env["quality"]["currency"] == "BRL"
    assert env["quality"]["result_count"] == 4
    assert env["quality"]["products"][0]["asin"] == "B0CFPL6CFY"


async def test_fetch_product_envelope() -> None:
    html = _fx("product.html")

    async def fake_fetch_html(_url: str):
        return html, 200, {}, A.FailureReason.OK, "http"

    env = await A.fetch_amazon(PRODUCT_URL, fetch_html=fake_fetch_html)

    assert env["mode_used"] == "amazon"
    assert "failure" not in env
    assert env["quality"]["page_type"] == "product"
    assert env["quality"]["result_count"] == 1
    prod = env["quality"]["products"][0]
    assert prod["asin"] == "B0CFPL6CFY"
    assert prod["price"] == 879
    assert prod["currency"] == "BRL"
    assert env["title"].startswith("Kindle Paperwhite")


async def test_fetch_passes_through_escalation_failure() -> None:
    async def failing_fetch_html(_url: str):
        return "", 0, {}, A.FailureReason.BLOCKED_CAPTCHA, "browser"

    env = await A.fetch_amazon(PRODUCT_URL, fetch_html=failing_fetch_html)

    assert env["failure"]["reason"] == A.FailureReason.BLOCKED_CAPTCHA.value
    assert "browser" in env["failure"]["message"]


async def test_fetch_rot_returns_parse_failed() -> None:
    """200 OK but no #productTitle (Amazon's spine) → PARSE_FAILED, not empty."""

    async def fake_fetch_html(_url: str):
        return (
            "<html><body>no anchor</body></html>",
            200,
            {},
            A.FailureReason.OK,
            "http",
        )

    env = await A.fetch_amazon(PRODUCT_URL, fetch_html=fake_fetch_html)

    assert "failure" in env
    assert env["failure"]["reason"] == A.FailureReason.PARSE_FAILED.value
    assert "#productTitle" in env["failure"]["message"]


async def test_fetch_search_empty_is_no_results() -> None:
    async def fake_fetch_html(_url: str):
        return (
            '<div class="s-main-slot s-result-list"></div>',
            200,
            {},
            A.FailureReason.OK,
            "http",
        )

    env = await A.fetch_amazon(SEARCH_URL, fetch_html=fake_fetch_html)

    assert "failure" not in env
    assert env["quality"]["result_count"] == 0
    assert env["warnings"] == ["no_results"]
