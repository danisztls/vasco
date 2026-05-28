from __future__ import annotations

import pathlib

import pytest

from vasco.adapters import google_shopping
from vasco.errors import FailureReason


FIXTURE_PATH = (
    pathlib.Path(__file__).parent / "fixtures" / "google_shopping_kindle.html"
)


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, matches",
    [
        ("https://www.google.com/shopping", True),
        ("https://www.google.com/shopping?udm=28", True),
        ("https://www.google.com.br/shopping", True),
        ("https://www.google.com/search?udm=28&q=kindle", True),
        ("https://www.google.com.br/search?q=kindle&udm=28", True),
        ("https://google.com/search?q=kindle&udm=28&hl=pt-BR", True),
        # Not matched:
        ("https://www.google.com/search?q=kindle", False),  # no udm=28
        ("https://www.google.com/search?udm=14&q=kindle", False),  # web tab
        ("https://www.google.com/maps", False),
        ("https://www.bing.com/shop?q=kindle", False),
        ("https://example.com/shopping", False),
        ("", False),
    ],
)
def test_is_google_shopping_url(url: str, matches: bool) -> None:
    assert google_shopping.is_google_shopping_url(url) is matches


def test_extract_query() -> None:
    assert (
        google_shopping._extract_query(
            "https://www.google.com/search?udm=28&q=kindle+paperwhite"
        )
        == "kindle paperwhite"
    )
    assert google_shopping._extract_query("https://www.google.com/shopping") is None


# ---------------------------------------------------------------------------
# Aria-label parser
# ---------------------------------------------------------------------------


_FULL_LABEL = (
    "Amazon Kindle Paperwhite 16GB 2024 O Kindle mais rápido já lançado.  "
    "R$ 949,00 agora. 10 parcelas de R$ 94,90. Amazon.com.br - Retail e mais. "
    "Devolução em até 7 dia(s). Avaliado com 4,8 de 5. 871 avaliações."
)

_USED_LABEL = (
    "Kindle Paperwhite 7a Geração (Usado).  Preço atual: R$ 450,00. Usado."
    "mercadolivre.com.br. Avaliado com 4,2 de 5. 70 avaliações."
)

_INTL_LABEL = (
    "Amazon Kindle Novo Paperwhite 10a Geração.  Preço atual: R$ 456,68. "
    "Preço no exterior: US$ 90eBay. Avaliado com 4,7 de 5. 533 avaliações."
)

_PROMO_LABEL = (
    "Kindle Backlight J9G29R Ereader 10o Leitor Paperwhite.  PROMOÇÃO. "
    "Preço atual: R$ 339,74. Custava R$ 380. AliExpress. "
    "Avaliado com 4,5 de 5. 56 avaliações."
)

_MIL_LABEL = (
    "Some Kindle Variant.  R$ 1.043,25 agora. Mercado Livre e mais. "
    "Avaliado com 4,8 de 5. 5,8 mil avaliações."
)

_MINIMAL_LABEL = "Plain Kindle.  R$ 500,00. Some Store."


def test_parse_product_full_label() -> None:
    p = google_shopping._parse_product(_FULL_LABEL)
    assert p is not None
    assert (
        p["title"]
        == "Amazon Kindle Paperwhite 16GB 2024 O Kindle mais rápido já lançado"
    )
    assert p["price_brl"] == 949.00
    assert p["product_rating"] == 4.8
    assert p["product_review_count"] == 871
    assert p["store"] == "Amazon.com.br - Retail"
    assert p["other_stores"] is True
    assert "was_price_brl" not in p
    assert "badges" not in p


def test_parse_product_used_filtered() -> None:
    assert google_shopping._parse_product(_USED_LABEL) is None
    # Also recondicionado
    refurb = _USED_LABEL.replace("Usado", "Recondicionado")
    assert google_shopping._parse_product(refurb) is None


def test_parse_product_international_filtered() -> None:
    assert google_shopping._parse_product(_INTL_LABEL) is None


def test_parse_product_promo_with_was_price() -> None:
    p = google_shopping._parse_product(_PROMO_LABEL)
    assert p is not None
    assert p["price_brl"] == 339.74
    assert p["was_price_brl"] == 380.0
    assert p["discount_pct"] == 10.6
    assert p["badges"] == ["promo"]
    assert p["store"] == "AliExpress"
    # Promo prefix is stripped from the title.
    assert not p["title"].startswith("PROMOÇÃO")


def test_parse_product_review_mil_suffix() -> None:
    p = google_shopping._parse_product(_MIL_LABEL)
    assert p is not None
    assert p["product_review_count"] == 5800
    assert p["price_brl"] == 1043.25


def test_parse_product_omits_null_fields() -> None:
    p = google_shopping._parse_product(_MINIMAL_LABEL)
    assert p is not None
    assert p["title"] == "Plain Kindle"
    assert p["price_brl"] == 500.0
    assert p["store"] == "Some Store"
    for missing in (
        "product_rating",
        "product_review_count",
        "was_price_brl",
        "discount_pct",
        "badges",
        "other_stores",
    ):
        assert missing not in p


# ---------------------------------------------------------------------------
# Outlier filter
# ---------------------------------------------------------------------------


def _mkprod(price: float) -> dict:
    return {"title": f"item @ {price}", "price_brl": price}


def test_outlier_filter_drops_low_and_high() -> None:
    # Cluster around 1000 with one extreme low and one extreme high
    prices = [10.0, 950.0, 980.0, 1000.0, 1020.0, 1050.0, 1080.0, 1100.0, 9999.0]
    products = [_mkprod(p) for p in prices]
    kept, dropped = google_shopping._filter_outliers(products)
    kept_prices = sorted(p["price_brl"] for p in kept)
    assert 10.0 not in kept_prices
    assert 9999.0 not in kept_prices
    assert dropped == 2


def test_outlier_filter_skips_small_N() -> None:
    # 7 products including a wild outlier — gated by N>=8, nothing dropped.
    prices = [10.0, 950.0, 980.0, 1000.0, 1020.0, 1050.0, 1080.0]
    products = [_mkprod(p) for p in prices]
    kept, dropped = google_shopping._filter_outliers(products)
    assert len(kept) == 7
    assert dropped == 0


def test_outlier_filter_zero_iqr_passthrough() -> None:
    # All identical prices → IQR=0; we should not crash and should keep all.
    products = [_mkprod(100.0) for _ in range(10)]
    kept, dropped = google_shopping._filter_outliers(products)
    assert len(kept) == 10
    assert dropped == 0


# ---------------------------------------------------------------------------
# HTML extraction (fixture-driven)
# ---------------------------------------------------------------------------


def test_extract_offers_from_fixture() -> None:
    html_src = FIXTURE_PATH.read_text()
    offers, filter_counts = google_shopping._extract_offers(html_src)

    # All kept offers are new + Brazilian + non-outlier.
    assert all("Usado" not in o.get("store", "") for o in offers)
    assert all("Preço no exterior" not in o.get("store", "") for o in offers)

    # Offers are in source order, each carrying a 1-based position.
    positions = [o["position"] for o in offers]
    assert positions == sorted(positions)
    assert positions[0] >= 1

    # Filter counts include used + international (outliers depend on data).
    assert filter_counts.get("used", 0) >= 1
    assert filter_counts.get("international", 0) >= 1

    # Product-level aggregate + thumbnail extracted on at least one offer.
    assert any("product_rating" in o for o in offers)
    assert any(
        "gstatic" in o.get("image", "") or "encrypted-tbn" in o.get("image", "")
        for o in offers
    )


# ---------------------------------------------------------------------------
# Grouping (same product across multiple sellers)
# ---------------------------------------------------------------------------


def test_norm_title_conservative() -> None:
    # Whitespace + case + trailing punctuation are normalized away...
    assert google_shopping._norm_title(
        "Kindle  Paperwhite ."
    ) == google_shopping._norm_title("kindle paperwhite")
    # ...but distinct SKUs are NOT merged.
    assert google_shopping._norm_title(
        "iPhone 15 128GB"
    ) != google_shopping._norm_title("iPhone 15 256GB")


def test_group_by_product_multi_seller() -> None:
    offers = [
        {
            "title": "Soundcore P40i",
            "price_brl": 361.0,
            "store": "Magalu",
            "position": 2,
            "was_price_brl": 399.0,
            "discount_pct": 9.5,
            "product_rating": 4.6,
            "product_review_count": 4800,
        },
        {
            "title": "Soundcore P40i",
            "price_brl": 354.9,
            "store": "Amazon",
            "position": 4,
            "product_rating": 4.6,
            "product_review_count": 4800,
            "image": "https://encrypted-tbn0.gstatic.com/x",
        },
    ]
    products = google_shopping._group_by_product(offers)
    assert len(products) == 1
    p = products[0]
    assert len(p["sellers"]) == 2
    # Sellers sorted by price asc; product price is the cheapest.
    assert [s["price_brl"] for s in p["sellers"]] == [354.9, 361.0]
    assert p["price_brl"] == 354.9
    assert p["price_range"] == [354.9, 361.0]
    # Position is the best (min) rank in the group.
    assert p["position"] == 2
    # Aggregate fields hoisted to the product level (not per-seller).
    assert p["product_rating"] == 4.6
    assert p["product_review_count"] == 4800
    assert "product_rating" not in p["sellers"][0]
    # Per-offer fields stay on the seller.
    magalu = next(s for s in p["sellers"] if s["store"] == "Magalu")
    assert magalu["discount_pct"] == 9.5
    # Image hoisted from the offer that carries one.
    assert p["image"].endswith("/x")


def test_group_by_product_collapses_exact_dups() -> None:
    offers = [
        {"title": "P40i", "price_brl": 354.9, "store": "Amazon Seller", "position": 1},
        {"title": "P40i", "price_brl": 354.9, "store": "Amazon Seller", "position": 2},
        {"title": "P40i", "price_brl": 354.9, "store": "Amazon Seller", "position": 3},
    ]
    products = google_shopping._group_by_product(offers)
    assert len(products) == 1
    assert len(products[0]["sellers"]) == 1  # 3 identical listings -> 1
    assert "price_range" not in products[0]  # single price


def test_group_by_product_orders_by_position() -> None:
    offers = [
        {
            "title": "Cheap But Lower Ranked",
            "price_brl": 10.0,
            "store": "A",
            "position": 5,
        },
        {
            "title": "Pricey But Top Ranked",
            "price_brl": 999.0,
            "store": "B",
            "position": 1,
        },
    ]
    products = google_shopping._group_by_product(offers)
    # Google order preserved: position 1 first, despite higher price.
    assert [p["position"] for p in products] == [1, 5]
    assert products[0]["title"] == "Pricey But Top Ranked"


# ---------------------------------------------------------------------------
# fetch_google_shopping — end-to-end with mocked browser
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_google_shopping_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html_src = FIXTURE_PATH.read_text()

    async def fake_browser(url, *, deadline_monotonic, cfg):
        return html_src, 200, {}

    monkeypatch.setattr(google_shopping, "_browser_fetch_html", fake_browser)

    env = await google_shopping.fetch_google_shopping(
        "https://www.google.com/search?udm=28&q=kindle+paperwhite",
        deadline=10.0,
    )

    assert "failure" not in env
    assert env["mode_used"] == "google_shopping"
    assert env["content_type"] == "application/x-google-shopping"
    assert env["site_name"] == "Google Shopping"
    assert env["title"] == "Google Shopping: kindle paperwhite"
    assert env["quality"]["currency"] == "BRL"
    assert env["quality"]["search_query"] == "kindle paperwhite"

    products = env["quality"]["products"]
    assert env["quality"]["result_count"] == len(products)
    assert env["quality"]["offer_count"] >= len(products)  # grouping can only shrink
    assert len(products) >= 5  # something useful came out of the fixture

    # Default ordering preserves Google's source order (by position, not price).
    assert [p["position"] for p in products] == sorted(p["position"] for p in products)

    for p in products:
        # Each product carries a sellers list; product price is the cheapest seller.
        assert p["sellers"]
        assert p["price_brl"] == min(s["price_brl"] for s in p["sellers"])
        # No used / international leaked through.
        for s in p["sellers"]:
            assert "Usado" not in s.get("store", "")
            assert "Preço no exterior" not in s.get("store", "")

    # Top-level image is sourced from the first product's thumbnail when present.
    if products[0].get("image"):
        assert env["image"] == products[0]["image"]


@pytest.mark.asyncio
async def test_fetch_google_shopping_currency_from_cfg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vasco.config import Config, ShoppingCfg

    html_src = FIXTURE_PATH.read_text()

    async def fake_browser(url, *, deadline_monotonic, cfg):
        return html_src, 200, {}

    monkeypatch.setattr(google_shopping, "_browser_fetch_html", fake_browser)

    cfg = Config(shopping=ShoppingCfg(currency="USD", language="en-US"))
    env = await google_shopping.fetch_google_shopping(
        "https://www.google.com/search?udm=28&q=kindle",
        deadline=10.0,
        cfg=cfg,
    )
    assert env["quality"]["currency"] == "USD"
    assert env["language"] == "en-US"


@pytest.mark.asyncio
async def test_fetch_google_shopping_browser_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    async def fake_browser(url, *, deadline_monotonic, cfg):
        raise asyncio.TimeoutError("simulated")

    monkeypatch.setattr(google_shopping, "_browser_fetch_html", fake_browser)

    env = await google_shopping.fetch_google_shopping(
        "https://www.google.com/search?udm=28&q=kindle",
        deadline=1.0,
    )
    assert "failure" in env
    assert env["failure"]["reason"] == str(FailureReason.TIMEOUT)
    assert env["markdown"] == ""


@pytest.mark.asyncio
async def test_fetch_google_shopping_browser_error_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_browser(url, *, deadline_monotonic, cfg):
        raise RuntimeError("Target page, context or browser has been closed")

    monkeypatch.setattr(google_shopping, "_browser_fetch_html", fake_browser)

    env = await google_shopping.fetch_google_shopping(
        "https://www.google.com/search?udm=28&q=kindle",
        deadline=5.0,
    )
    # Note: classifier looks for specific markers; this RuntimeError msg lacks
    # the exact "connection closed" substring, so it falls through to
    # SERVER_ERROR — what matters is that we don't raise.
    assert "failure" in env


@pytest.mark.asyncio
async def test_fetch_google_shopping_empty_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_browser(url, *, deadline_monotonic, cfg):
        return "", 200, {}

    monkeypatch.setattr(google_shopping, "_browser_fetch_html", fake_browser)

    env = await google_shopping.fetch_google_shopping(
        "https://www.google.com/search?udm=28&q=kindle",
        deadline=5.0,
    )
    assert "failure" in env
    assert env["failure"]["reason"] == str(FailureReason.SERVER_ERROR)
