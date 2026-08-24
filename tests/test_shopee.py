# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from vasco.adapters import shopee as S
from vasco.errors import AdapterParseError

FX = Path(__file__).parent / "fixtures" / "shopee"


def _fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


PRODUCT_URL = (
    "https://shopee.com.br/E-Reader-Kindle-11ª-Geração-Amazon-com-16GB-"
    "Luz-integrada-e-Wi-Fi-i.1083800536.58257124661"
)


# --- routing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (PRODUCT_URL, True),
        # product tail with a trailing query (Shopee appends extraParams)
        ("https://shopee.com.br/Kindle-i.854082976.18399888291?extraParams=x", True),
        # search / category / list / home: no -i.<shop>.<item> tail → not matched
        ("https://shopee.com.br/search?keyword=kindle", False),
        ("https://shopee.com.br/Tablets-cat.11059988.11060169", False),
        ("https://shopee.com.br/list/Kindle", False),
        ("https://shopee.com.br/", False),
        # other-country Shopee is out of scope
        ("https://shopee.com.my/x-i.1.2", False),
        ("https://shopee.sg/x-i.1.2", False),
        ("https://example.com/x-i.1.2", False),
        ("", False),
    ],
)
def test_is_shopee_url(url: str, expected: bool) -> None:
    assert S.is_shopee_url(url) is expected


def test_product_ids() -> None:
    assert S._product_ids(PRODUCT_URL) == ("1083800536", "58257124661")
    assert S._product_ids("https://shopee.com.br/x-i.1.2?foo=bar") == ("1", "2")
    assert S._product_ids("https://shopee.com.br/search?keyword=k") is None


# --- numeric / text helpers ------------------------------------------------


def test_price_parses_dot_decimal() -> None:
    # Shopee JSON-LD prices are en-format (dot decimal), NOT Brazilian comma.
    assert S._price("557.46") == 557.46
    assert S._price("1234.00") == 1234  # whole float collapses to int
    assert S._price(99.9) == 99.9
    assert S._price(100) == 100
    assert S._price("R$ 1234.00") == 1234  # tolerate stray currency text
    assert S._price("Sob consulta") is None
    assert S._price(None) is None
    assert S._price(True) is None


def test_rating_parses_string_values() -> None:
    agg = {"aggregateRating": {"ratingValue": "4.99", "ratingCount": "103"}}
    assert S._rating(agg) == (4.99, 103)
    assert S._rating({}) == (None, None)


def test_condition() -> None:
    assert S._condition("NewCondition") == "new"
    assert S._condition("http://schema.org/UsedCondition") == "used"
    assert S._condition("RefurbishedCondition") == "refurbished"
    assert S._condition(None) is None


def test_in_stock() -> None:
    assert S._in_stock("http://schema.org/InStock") is True
    assert S._in_stock("http://schema.org/OutOfStock") is False
    assert S._in_stock(None) is None


# --- product parse (JSON-LD spine + breadcrumb) ----------------------------


def test_parse_product_spine() -> None:
    [p] = S._parse_product(_fx("product.html"), PRODUCT_URL)
    assert p["title"].startswith("E-Reader Kindle 11ª Geração")
    assert p["url"].endswith("i.1083800536.58257124661")
    assert p["shop_id"] == "1083800536"
    assert p["item_id"] == "58257124661"
    assert p["product_id"] == "58257124661"
    assert p["price"] == 557.46
    assert p["currency"] == "BRL"
    assert p["condition"] == "new"
    assert p["in_stock"] is True
    assert p["brand"] == "Amazon"
    # product-level aggregateRating (not the shop's)
    assert p["rating"] == 4.99
    assert p["review_count"] == 103
    # nested seller Organization + its own aggregateRating
    assert p["seller"]["name"] == "Casas Bahia Oficial"
    assert p["seller"]["rating"] == 4.91
    assert p["seller"]["rating_count"] == 139123
    # breadcrumb minus home + product = the category chain
    assert p["category"] == "Celulares e Dispositivos > Tablets"
    assert p["image"].startswith("https://down-br.img.susercontent.com/")
    assert p["images"] == [p["image"]]
    assert p["description"]


def test_parse_product_raises_without_jsonld() -> None:
    # No Product JSON-LD = Shopee's spine is gone = scraper-rot / wall, not empty.
    with pytest.raises(AdapterParseError):
        S._parse_product("<html><body>no jsonld</body></html>", PRODUCT_URL)


def test_parse_product_logged_out_shell_message() -> None:
    # Shopee's logged-out shell carries only a site-level `WebSite` JSON-LD (no
    # Product): the message must name the expired session, not "markup changed".
    with pytest.raises(AdapterParseError, match="session likely expired"):
        S._parse_product(_fx("logged_out_shell.html"), PRODUCT_URL)


def test_parse_product_no_website_keeps_generic_message() -> None:
    # A page with neither Product nor WebSite JSON-LD is real scraper-rot / wall
    # — keep the generic message, don't misattribute it to a session expiry.
    with pytest.raises(AdapterParseError, match="site structure"):
        S._parse_product("<html><body>no jsonld</body></html>", PRODUCT_URL)


# --- markdown rendering ----------------------------------------------------


def test_render_markdown_product() -> None:
    products = S._parse_product(_fx("product.html"), PRODUCT_URL)
    md = S._render_markdown(products, currency="BRL")
    assert "E-Reader Kindle 11ª Geração" in md
    assert "R$ 557,46" in md
    assert "Casas Bahia Oficial" in md


# --- fetch via injected escalating fetcher ---------------------------------


async def test_fetch_product_envelope() -> None:
    html = _fx("product.html")
    calls: list[str] = []

    async def fake_fetch_html(url: str):
        calls.append(url)
        return html, 200, {}, S.FailureReason.OK, "browser"

    env = await S.fetch_shopee(PRODUCT_URL, fetch_html=fake_fetch_html)

    assert calls == [PRODUCT_URL]
    assert env["mode_used"] == "shopee"
    assert env["content_type"] == "application/x-shopee"
    assert env["http_status"] == 200
    assert "failure" not in env
    assert env["quality"]["provider"] == "shopee"
    assert env["quality"]["page_type"] == "product"
    assert env["quality"]["currency"] == "BRL"
    assert env["quality"]["result_count"] == 1
    assert env["quality"]["products"][0]["item_id"] == "58257124661"


async def test_fetch_passes_through_escalation_failure() -> None:
    async def failing_fetch_html(_url: str):
        return "", 0, {}, S.FailureReason.BLOCKED_CAPTCHA, "browser"

    env = await S.fetch_shopee(PRODUCT_URL, fetch_html=failing_fetch_html)

    assert env["failure"]["reason"] == S.FailureReason.BLOCKED_CAPTCHA.value
    assert "browser" in env["failure"]["message"]


async def test_fetch_rot_returns_parse_failed() -> None:
    """200 OK but no Product JSON-LD (Shopee's spine) → PARSE_FAILED, not empty."""

    async def fake_fetch_html(_url: str):
        return (
            "<html><body>no jsonld here</body></html>",
            200,
            {},
            S.FailureReason.OK,
            "browser",
        )

    env = await S.fetch_shopee(PRODUCT_URL, fetch_html=fake_fetch_html)

    assert "failure" in env
    assert env["failure"]["reason"] == S.FailureReason.PARSE_FAILED.value
    assert "Product JSON-LD" in env["failure"]["message"]
