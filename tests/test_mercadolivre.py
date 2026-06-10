from __future__ import annotations

from pathlib import Path

import pytest

from vasco.adapters import mercadolivre as M
from vasco.config import AdaptersCfg, Config, MercadolivreCfg
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


async def test_fetch_surfaces_login_wall_cleanly() -> None:
    """When the browser server's cookie-clear recovery can't clear ML's account
    wall, the chain returns LOGIN_REQUIRED — the adapter must surface that reason
    honestly (not the misleading PARSE_FAILED its JSON-LD parser would emit)."""

    async def walled_fetch_html(_url: str):
        return "", 403, {}, M.FailureReason.LOGIN_REQUIRED, "browser"

    env = await M.fetch_mercadolivre(SEARCH_URL, fetch_html=walled_fetch_html)

    assert env["failure"]["reason"] == M.FailureReason.LOGIN_REQUIRED.value


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


# --- relevance: query recovery --------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://lista.mercadolivre.com.br/notebook", "notebook"),
        ("https://lista.mercadolivre.com.br/notebook-gamer", "notebook gamer"),
        # ML appends filters after underscores — strip them to the bare keyword.
        ("https://lista.mercadolivre.com.br/colchao-queen_Desde_49", "colchao queen"),
        ("https://lista.mercadolivre.com.br/cafe%20expresso", "cafe expresso"),
        # category/deal browse (www host) has no user query → not filtered
        ("https://www.mercadolivre.com.br/c/celulares-e-telefones", None),
        ("https://www.mercadolivre.com.br/ofertas", None),
        # product page → no query
        ("https://www.mercadolivre.com.br/notebook-asus/p/MLB43417665", None),
        # ?q= fallback works on any host
        ("https://www.mercadolivre.com.br/search?q=fone+bluetooth", "fone bluetooth"),
        ("https://lista.mercadolivre.com.br/", None),
    ],
)
def test_search_query(url: str, expected: str | None) -> None:
    assert M._search_query(url) == expected


# --- relevance: fold tokenizer --------------------------------------------


def test_fold_accent_and_alnum() -> None:
    # accent-folded, lowercased, alphanumeric tokens (units stay whole)
    assert M._fold("Colchão Queen 16GB!") == ["colchao", "queen", "16gb"]
    assert M._fold("") == []


# --- relevance: filter/sort -----------------------------------------------


def _p(title: str, brand: str | None = None) -> dict:
    d: dict = {"title": title}
    if brand is not None:
        d["brand"] = brand
    return d


def test_apply_relevance_demotes_off_query_by_default() -> None:
    prods = [
        _p("Caderno de Lantejoulas A5"),  # off-query (paper notebook)
        _p("Notebook Lenovo Ideapad"),  # coverage 1
        _p("Notebook Dell Inspiron"),  # coverage 1 (native order tiebreak)
    ]
    ordered, off = M._apply_relevance(
        prods, "notebook dell", drop=False, min_coverage=1
    )
    # nothing dropped; off-query sinks; "dell" lifts the Dell above the Lenovo
    assert [p["title"] for p in ordered] == [
        "Notebook Dell Inspiron",
        "Notebook Lenovo Ideapad",
        "Caderno de Lantejoulas A5",
    ]
    assert off == 1
    assert [p["position"] for p in ordered] == [1, 2, 3]


def test_apply_relevance_drops_when_requested() -> None:
    prods = [_p("Caderno de Lantejoulas A5"), _p("Notebook Lenovo")]
    ordered, off = M._apply_relevance(prods, "notebook", drop=True, min_coverage=1)
    assert [p["title"] for p in ordered] == ["Notebook Lenovo"]
    assert off == 1


def test_apply_relevance_accent_fold_matches() -> None:
    # ASCII slug query must match the accented title
    prods = [_p("Mesa de Jantar"), _p("Colchão Queen Size")]
    ordered, off = M._apply_relevance(prods, "colchao", drop=True, min_coverage=1)
    assert [p["title"] for p in ordered] == ["Colchão Queen Size"]
    assert off == 1


def test_apply_relevance_brand_match_survives() -> None:
    # query hits the brand field even when the title omits it
    prods = [_p("iPhone 15", brand="Apple"), _p("Galaxy S24", brand="Samsung")]
    ordered, off = M._apply_relevance(prods, "samsung", drop=True, min_coverage=1)
    assert [p["title"] for p in ordered] == ["Galaxy S24"]
    assert off == 1


def test_apply_relevance_blank_query_is_noop() -> None:
    prods = [_p("B"), _p("A")]
    ordered, off = M._apply_relevance(prods, "   ", drop=True, min_coverage=1)
    assert ordered == prods  # untouched
    assert off == 0


# --- relevance: end-to-end through fetch -----------------------------------


def _search_html(*names_prices: tuple[str, str]) -> str:
    graph = [
        {
            "@type": "Product",
            "name": name,
            "offers": {
                "@type": "Offer",
                "url": f"https://www.mercadolivre.com.br/MLB-{i}",
                "price": price,
                "priceCurrency": "BRL",
            },
        }
        for i, (name, price) in enumerate(names_prices, 1)
    ]
    import json as _json

    return (
        '<html><body><script type="application/ld+json">'
        + _json.dumps({"@graph": graph})
        + "</script></body></html>"
    )


async def test_fetch_search_demotes_off_query_by_default() -> None:
    # ad placement (caderno) listed first in ML's native order; notebook second
    html = _search_html(("Caderno A5", "10"), ("Notebook Dell Inspiron", "3000"))

    async def fake_fetch_html(_url: str):
        return html, 200, {}, M.FailureReason.OK, "browser"

    env = await M.fetch_mercadolivre(SEARCH_URL, fetch_html=fake_fetch_html)

    assert env["quality"]["result_count"] == 2  # nothing dropped
    assert env["quality"]["demoted"] == 1
    assert "filtered" not in env["quality"]
    assert env["quality"]["products"][0]["title"] == "Notebook Dell Inspiron"


async def test_fetch_search_drop_off_query_via_cfg() -> None:
    html = _search_html(("Caderno A5", "10"), ("Notebook Dell Inspiron", "3000"))
    cfg = Config(
        adapters=AdaptersCfg(mercadolivre=MercadolivreCfg(drop_off_query=True))
    )

    async def fake_fetch_html(_url: str):
        return html, 200, {}, M.FailureReason.OK, "browser"

    env = await M.fetch_mercadolivre(SEARCH_URL, fetch_html=fake_fetch_html, cfg=cfg)

    assert env["quality"]["result_count"] == 1
    assert env["quality"]["filtered"] == {"off_query": 1}
    assert "demoted" not in env["quality"]
    assert env["quality"]["products"][0]["title"] == "Notebook Dell Inspiron"


async def test_fetch_search_relevance_filter_disabled() -> None:
    html = _search_html(("Caderno A5", "10"), ("Notebook Dell", "3000"))
    cfg = Config(
        adapters=AdaptersCfg(mercadolivre=MercadolivreCfg(relevance_filter=False))
    )

    async def fake_fetch_html(_url: str):
        return html, 200, {}, M.FailureReason.OK, "browser"

    env = await M.fetch_mercadolivre(SEARCH_URL, fetch_html=fake_fetch_html, cfg=cfg)

    # untouched: native order preserved, no demote/filter reporting
    assert env["quality"]["result_count"] == 2
    assert env["quality"]["products"][0]["title"] == "Caderno A5"
    assert "demoted" not in env["quality"]
    assert "filtered" not in env["quality"]
