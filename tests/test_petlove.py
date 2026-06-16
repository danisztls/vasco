from __future__ import annotations

from pathlib import Path

import pytest

from vasco.adapters import petlove as P
from vasco.errors import AdapterParseError

FX = Path(__file__).parent / "fixtures" / "petlove"


def _fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


PRODUCT_URL = (
    "https://www.petlove.com.br/"
    "racao-premier-golden-formula-caes-adultos-frango-e-arroz-mini-bits/p"
)
SEARCH_URL = "https://www.petlove.com.br/busca?q=racao%20golden"


# --- routing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (PRODUCT_URL, True),
        (SEARCH_URL, True),
        # product URL with a ?sku= query / trailing slash still matches
        (PRODUCT_URL + "?sku=31021721-3", True),
        (PRODUCT_URL + "/", True),
        ("https://petlove.com.br/busca?q=gato", True),  # bare host
        # category / brand / content / home: not a /p or /busca page → unmatched
        ("https://www.petlove.com.br/cachorro/racoes/racao-seca", False),
        ("https://www.petlove.com.br/marcas/golden", False),
        ("https://www.petlove.com.br/", False),
        ("https://www.petlove.com.br/p", False),  # bare /p is not a product
        # other hosts out of scope
        ("https://example.com/foo/p", False),
        ("https://petlove.com/foo/p", False),
        ("", False),
    ],
)
def test_is_petlove_url(url: str, expected: bool) -> None:
    assert P.is_petlove_url(url) is expected


def test_page_type() -> None:
    assert P._page_type(SEARCH_URL) == "search"
    assert P._page_type(PRODUCT_URL) == "product"
    assert P._page_type("https://www.petlove.com.br/cachorro") is None


# --- numeric / text helpers ------------------------------------------------


def test_price_parses_dot_decimal() -> None:
    # Petlove JSON-LD prices are en-format (dot decimal), NOT Brazilian comma.
    assert P._price("22.50") == 22.5
    assert P._price("1234.00") == 1234  # whole float collapses to int
    assert P._price(174.9) == 174.9
    assert P._price(100) == 100
    assert P._price(None) is None
    assert P._price(True) is None


def test_in_stock() -> None:
    assert P._in_stock("https://schema.org/InStock") is True
    assert P._in_stock("https://schema.org/OutOfStock") is False
    assert P._in_stock(None) is None


def test_agg_rating_reads_review_count() -> None:
    # Petlove uses reviewCount, not the ratingCount _common.rating reads.
    agg = {"aggregateRating": {"ratingValue": 4.79, "reviewCount": 877}}
    assert P._agg_rating(agg) == (4.79, 877)
    # ratingCount accepted as a fallback
    assert P._agg_rating(
        {"aggregateRating": {"ratingValue": "4", "ratingCount": "3"}}
    ) == (
        4.0,
        3,
    )
    assert P._agg_rating({}) == (None, None)


def test_total_count() -> None:
    assert P._total_count({"description": "Catálogo completo com 69 produtos."}) == 69
    assert P._total_count({"numberOfItems": 42}) == 42
    assert P._total_count({"description": "sem total"}) is None
    assert P._total_count(None) is None


def test_clean_strips_html() -> None:
    assert (
        P._clean("A <strong>ração</strong> <a href='/x'>Golden</a>") == "A ração Golden"
    )
    assert P._clean("") is None
    assert P._clean(None) is None


def test_category_path_drops_home() -> None:
    bc = {
        "itemListElement": [
            {"name": "Home", "item": "https://www.petlove.com.br/"},
            {"name": "Cachorro"},
            {"name": "Rações"},
        ]
    }
    assert P._category_path(bc) == "Cachorro > Rações"
    assert P._category_path(None) is None


# --- search parse (ItemList JSON-LD) ---------------------------------------


def test_parse_search_spine() -> None:
    products, total = P._parse_search(_fx("search.html"))
    assert total == 69  # from the ItemList description
    assert len(products) == 3
    p = products[0]
    assert p["position"] == 1
    assert p["title"].startswith("Ração Seca PremieR Pet Golden Formula")
    assert p["url"].endswith("/p")
    assert p["sku"] == "31021721-3"
    assert p["price"] == 174.9
    assert p["currency"] == "BRL"
    assert p["brand"] == "GoldeN"
    assert p["in_stock"] is True
    assert p["image"].startswith("https://www.petlove.com.br/images/products/")


def test_parse_search_raises_without_jsonld() -> None:
    with pytest.raises(AdapterParseError):
        P._parse_search("<html><body>no jsonld</body></html>")


def test_parse_search_empty_itemlist_is_no_results() -> None:
    # ItemList anchor present but holding zero products = genuinely empty search,
    # not rot — returns an empty list (no raise).
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"ItemList","itemListElement":[]}'
        "</script></head></html>"
    )
    products, total = P._parse_search(html)
    assert products == []
    assert total is None


def test_parse_search_falls_back_to_standalone_products() -> None:
    # No ItemList wrapper, but standalone Product blocks present → parsed.
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"Product","name":"Coleira","url":"https://www.petlove.com.br/coleira/p",'
        '"offers":{"price":29.9,"priceCurrency":"BRL","availability":"https://schema.org/InStock"}}'
        "</script></head></html>"
    )
    products, total = P._parse_search(html)
    assert len(products) == 1
    assert products[0]["price"] == 29.9
    assert total is None


# --- product parse (ProductGroup spine: variants + reviews) ----------------


def test_parse_product_spine() -> None:
    [p] = P._parse_product(_fx("product.html"), PRODUCT_URL, max_reviews=3)
    assert p["title"].startswith("Areia Sanitária Meau")
    assert p["url"].endswith("/p")
    assert p["product_id"] == "2492334"  # ProductGroup productGroupID
    assert p["brand"] == "Meau"
    # price spans the variant range (lowest → highest)
    assert p["price"] == 16.11
    assert p["price_max"] == 132.21
    assert p["currency"] == "BRL"
    assert p["in_stock"] is True
    # aggregateRating via reviewCount; full-precision mean is rounded to 2 dp
    assert p["rating"] == 4.72
    assert p["review_count"] == 274
    assert p["category"].startswith("Gatos >")
    assert p["description"].startswith("A Areia Sanitária Meau")
    # every size variant carries its own price (the multiple size/price pairs)
    assert len(p["variants"]) == 4
    pairs = {(v["size"], v["price"]) for v in p["variants"]}
    assert ("4 Kg", 16.11) in pairs
    assert ("12 Kg", 41.31) in pairs
    assert all("price" in v and "size" in v for v in p["variants"])
    # reviews capped + shaped
    assert len(p["reviews"]) == 3
    r0 = p["reviews"][0]
    assert r0["author"] and r0["rating"] and r0["title"] and r0["text"] and r0["date"]


def test_parse_product_dom_extras() -> None:
    # specs table + struck list price come from the rendered DOM (not JSON-LD).
    [p] = P._parse_product(_fx("product.html"), PRODUCT_URL, max_reviews=3)
    assert p["specs"]["TIPO DE AREIA"] == "Bentonita"
    assert p["specs"]["INDICAÇÃO"] == "Gatos"
    assert len(p["specs"]) >= 8
    # struck "preço cheio" for the selected variant (regular price is 16.11)
    assert p["list_price"] == 17.9


def test_parse_product_review_cap() -> None:
    [p] = P._parse_product(_fx("product.html"), PRODUCT_URL, max_reviews=1)
    assert len(p["reviews"]) == 1


def test_parse_product_no_dom_extras_is_clean() -> None:
    # JSON-LD only (no specs/price DOM) → those keys are simply absent, no raise.
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"ProductGroup","name":"X","productGroupID":"1",'
        '"hasVariant":[{"@type":"Product","sku":"1-1","size":"1 Kg",'
        '"offers":{"price":"10.00","priceCurrency":"BRL"}}]}'
        "</script></head></html>"
    )
    [p] = P._parse_product(html, PRODUCT_URL)
    assert "specs" not in p
    assert "list_price" not in p
    assert p["variants"][0]["size"] == "1 Kg"


def test_parse_product_plain_product_fallback() -> None:
    # A single-size product with no ProductGroup wrapper → plain Product path.
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"Product","name":"Brinquedo","sku":"99-1",'
        '"brand":{"@type":"Brand","name":"Petlove"},'
        '"aggregateRating":{"ratingValue":4.5,"reviewCount":12},'
        '"offers":{"price":"19.90","priceCurrency":"BRL","availability":"https://schema.org/InStock",'
        '"url":"https://www.petlove.com.br/brinquedo/p"}}'
        "</script></head></html>"
    )
    [p] = P._parse_product(html, "https://www.petlove.com.br/brinquedo/p")
    assert p["title"] == "Brinquedo"
    assert p["sku"] == "99-1"
    assert p["price"] == 19.9
    assert p["rating"] == 4.5
    assert p["review_count"] == 12
    # no variants on a plain Product → _compact drops the empty list
    assert p.get("variants", []) == []


def test_parse_product_raises_without_jsonld() -> None:
    with pytest.raises(AdapterParseError):
        P._parse_product("<html><body>no jsonld</body></html>", PRODUCT_URL)


# --- markdown rendering ----------------------------------------------------


def test_render_product_markdown() -> None:
    products = P._parse_product(_fx("product.html"), PRODUCT_URL, max_reviews=3)
    md = P._render_product(products, currency="BRL")
    assert "# Areia Sanitária Meau" in md
    assert "R$ 16,11 – R$ 132,21" in md  # the size/price range
    assert "de R$ 17,90" in md  # struck list price
    assert "274 avaliações" in md
    # each size/price pair is its own bullet under a clear heading
    assert "## Tamanhos e preços" in md
    assert "- 4 Kg — R$ 16,11" in md
    assert "- 12 Kg — R$ 41,31" in md
    # specs section
    assert "## Especificações" in md
    assert "- TIPO DE AREIA: Bentonita" in md
    # reviews section (the markdown previously dropped these)
    assert "## Avaliações" in md
    assert "★" in md
    assert "Claudia" in md


def test_render_search_markdown() -> None:
    products, _ = P._parse_search(_fx("search.html"))
    md = P._render_search(products, currency="BRL")
    assert "3 produtos" in md
    assert "R$ 174,90" in md
    assert "GoldeN" in md


# --- fetch via injected escalating fetcher ---------------------------------


async def test_fetch_product_envelope() -> None:
    html = _fx("product.html")
    calls: list[str] = []

    async def fake_fetch_html(url: str):
        calls.append(url)
        return html, 200, {}, P.FailureReason.OK, "browser"

    env = await P.fetch_petlove(PRODUCT_URL, fetch_html=fake_fetch_html)

    assert calls == [PRODUCT_URL]
    assert env["mode_used"] == "petlove"
    assert env["content_type"] == "application/x-petlove"
    assert env["http_status"] == 200
    assert "failure" not in env
    assert env["quality"]["provider"] == "petlove"
    assert env["quality"]["page_type"] == "product"
    assert env["quality"]["currency"] == "BRL"
    assert env["quality"]["result_count"] == 1
    prod = env["quality"]["products"][0]
    assert prod["product_id"] == "2492334"
    assert len(prod["variants"]) == 4  # the multiple size/price pairs
    assert prod["specs"]["TIPO DE AREIA"] == "Bentonita"
    assert "total_count" not in env["quality"]  # product page has no catalogue total


async def test_fetch_search_envelope() -> None:
    html = _fx("search.html")

    async def fake_fetch_html(_url: str):
        return html, 200, {}, P.FailureReason.OK, "browser"

    env = await P.fetch_petlove(SEARCH_URL, fetch_html=fake_fetch_html)

    assert env["mode_used"] == "petlove"
    assert env["quality"]["page_type"] == "search"
    assert env["quality"]["result_count"] == 3
    assert env["quality"]["total_count"] == 69
    assert env["warnings"] == []


async def test_fetch_passes_through_escalation_failure() -> None:
    async def failing_fetch_html(_url: str):
        return "", 0, {}, P.FailureReason.BLOCKED_CLOUDFLARE, "browser"

    env = await P.fetch_petlove(PRODUCT_URL, fetch_html=failing_fetch_html)

    assert env["failure"]["reason"] == P.FailureReason.BLOCKED_CLOUDFLARE.value
    assert "browser" in env["failure"]["message"]


async def test_fetch_rot_returns_parse_failed() -> None:
    """200 OK but no JSON-LD spine → PARSE_FAILED, not an empty success."""

    async def fake_fetch_html(_url: str):
        return (
            "<html><body>no jsonld here</body></html>",
            200,
            {},
            P.FailureReason.OK,
            "browser",
        )

    env = await P.fetch_petlove(PRODUCT_URL, fetch_html=fake_fetch_html)

    assert "failure" in env
    assert env["failure"]["reason"] == P.FailureReason.PARSE_FAILED.value
    assert "ProductGroup/Product JSON-LD" in env["failure"]["message"]


async def test_fetch_search_empty_is_no_results() -> None:
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"ItemList","itemListElement":[]}'
        "</script></head></html>"
    )

    async def fake_fetch_html(_url: str):
        return html, 200, {}, P.FailureReason.OK, "browser"

    env = await P.fetch_petlove(SEARCH_URL, fetch_html=fake_fetch_html)

    assert "failure" not in env
    assert env["quality"]["result_count"] == 0
    assert env["warnings"] == ["no_results"]
