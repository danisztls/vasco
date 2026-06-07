from __future__ import annotations

import json
from pathlib import Path

import pytest

from vasco.adapters import aliexpress as A
from vasco.errors import AdapterParseError, FailureReason

FX = Path(__file__).parent / "fixtures" / "aliexpress"


def _fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


# --- routing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://pt.aliexpress.com/w/wholesale-kindle.html", True),
        ("https://www.aliexpress.com/item/1005008760568743.html", True),
        ("https://m.aliexpress.com/wholesale?SearchText=x", True),
        ("https://aliexpress.com/item/1.html", True),
        ("https://www.aliexpress.com.br/item/1.html", True),
        ("https://example.com/item/123.html", False),
        ("https://notaliexpress.com/x", False),
        ("", False),
    ],
)
def test_is_aliexpress_url(url: str, expected: bool) -> None:
    assert A.is_aliexpress_url(url) is expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.aliexpress.com/item/1005008760568743.html", "product"),
        ("https://pt.aliexpress.com/item/123.html?spm=a2g0o", "product"),
        ("https://pt.aliexpress.com/w/wholesale-kindle.html", "search"),
        ("https://www.aliexpress.com/wholesale?SearchText=x", "search"),
        ("https://www.aliexpress.com/", "search"),
    ],
)
def test_page_type(url: str, expected: str) -> None:
    assert A._page_type(url) == expected


def test_product_id_from_url() -> None:
    assert (
        A._product_id_from_url("https://x.aliexpress.com/item/1005008760568743.html")
        == "1005008760568743"
    )
    assert A._product_id_from_url("https://x.aliexpress.com/w/wholesale-x.html") is None


# --- money / image / rating helpers ----------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("R$918", 918),
        ("R$396,44", 396.44),
        ("R$1.544,99", 1544.99),
        ("R$9.088,18", 9088.18),
        ("", None),
        ("Sob consulta", None),
    ],
)
def test_brl_to_num(text: str, expected) -> None:
    assert A._brl_to_num(text) == expected


def test_rating_num_keeps_decimal_point() -> None:
    # Regression: routing a rating through money-parsing would strip "." → 49.
    assert A._rating_num("4.9") == 4.9
    assert A._rating_num("5,0") == 5.0
    assert A._rating_num("9.9") is None  # out of 0-5 range
    assert A._rating_num("x") is None


@pytest.mark.parametrize(
    "src,expected",
    [
        (
            "//ae-pic-a1.aliexpress-media.com/kf/Sxxx.jpg_480x480q75.jpg_.avif",
            "https://ae-pic-a1.aliexpress-media.com/kf/Sxxx.jpg",
        ),
        (
            "//ae-pic-a1.aliexpress-media.com/kf/Syyy.png_220x220.png",
            "https://ae-pic-a1.aliexpress-media.com/kf/Syyy.png",
        ),
        (
            "https://ae-pic-a1.aliexpress-media.com/kf/Sclean.jpg",
            "https://ae-pic-a1.aliexpress-media.com/kf/Sclean.jpg",
        ),
        (None, None),
    ],
)
def test_clean_image(src, expected) -> None:
    assert A._clean_image(src, "https://pt.aliexpress.com/") == expected


# --- search parsing --------------------------------------------------------


def test_parse_search_extracts_products() -> None:
    products = A._parse_search(
        _fx("search.html"), "https://pt.aliexpress.com/w/wholesale-kindle.html"
    )
    assert len(products) == 4
    first = products[0]
    assert first["product_id"] == "1005008760568743"
    assert first["title"].startswith("Kindle Paperwhite 12ª Geração")
    assert first["price"] == 918
    assert first["old_price"] == 1199
    assert first["discount_pct"] == 23
    assert first["rating"] == 4.9
    assert first["sold_count"] == 168
    assert first["image"].startswith("https://ae-pic-a1.aliexpress-media.com/kf/")
    assert first["image"].endswith(".jpg")  # size suffix stripped
    assert first["url"].startswith("https://")


def test_parse_search_handles_space_split_cents() -> None:
    """AliExpress renders "R$ 396 , 44" across separate nodes; the structural
    decimal_point spans reassemble it without bleeding into rating/sold digits."""
    products = A._parse_search(_fx("search.html"), "https://x/w/x.html")
    prices = {p["product_id"]: p.get("price") for p in products}
    assert prices["1005008569852414"] == 396.44


def test_parse_search_old_price_is_discount_gated() -> None:
    """The 2nd R$ on a card can be an installment ("6 × R$153"); old_price is only
    set when a -NN% discount badge is present."""
    products = A._parse_search(_fx("search.html"), "https://x/w/x.html")
    for p in products:
        if p.get("old_price") is not None:
            assert p.get("discount_pct") is not None


def test_parse_search_empty_grid_yields_no_products() -> None:
    # Anchor (grid) present but zero item links → genuinely empty result set.
    assert A._parse_search(_fx("search_empty.html"), "https://x/w/x.html") == []


def test_parse_search_no_anchor_raises_parse_failed() -> None:
    with pytest.raises(AdapterParseError):
        A._parse_search(_fx("search_rot.html"), "https://x/w/x.html")


# --- reviews parsing -------------------------------------------------------


def test_parse_reviews_statistic_and_list() -> None:
    rev = A._parse_reviews(json.loads(_fx("reviews.json")))
    assert rev["rating"] == 4.9
    assert rev["review_count"] == 30
    assert rev["rating_histogram"][5] == 29
    assert len(rev["reviews"]) == 3
    r0 = rev["reviews"][0]
    assert r0["stars"] == 5.0  # buyerEval 100 / 20
    assert r0["country"] == "BR"
    assert r0["text"]


def test_parse_reviews_empty_payload() -> None:
    assert A._parse_reviews({}) == {}
    assert A._parse_reviews({"data": None}) == {}


# --- product detail (best-effort DOM + reviews) ----------------------------


def test_pdp_extras_from_rendered_page() -> None:
    extras = A._pdp_extras(
        _fx("item.html"), "https://pt.aliexpress.com/item/1005008760568743.html"
    )
    assert extras["title"].startswith("Kindle Paperwhite 12ª Geração")
    assert extras["price"] == 918
    assert extras["images"]
    assert extras["image"].endswith(".jpg")


def test_pdp_extras_never_raises_on_shell() -> None:
    # A bare shell yields nothing (no title/price/images) but must not raise.
    assert (
        A._pdp_extras("<html><body><div id=root></div></body></html>", "https://x")
        == {}
    )


async def test_build_product_combines_id_dom_and_reviews(monkeypatch) -> None:
    async def fake_reviews(pid, **kw):
        assert pid == "1005008760568743"
        return json.loads(_fx("reviews.json"))

    monkeypatch.setattr(A, "_fetch_reviews_json", fake_reviews)
    products = await A._build_product(
        _fx("item.html"),
        "https://pt.aliexpress.com/item/1005008760568743.html",
        language="pt_BR",
        country="BR",
        page_size=6,
        deadline=10.0,
    )
    assert len(products) == 1
    p = products[0]
    assert p["product_id"] == "1005008760568743"
    assert p["price"] == 918
    assert p["rating"] == 4.9
    assert p["review_count"] == 30
    assert len(p["reviews"]) == 3


async def test_build_product_survives_reviews_failure(monkeypatch) -> None:
    async def no_reviews(pid, **kw):
        return None

    monkeypatch.setattr(A, "_fetch_reviews_json", no_reviews)
    products = await A._build_product(
        _fx("item.html"),
        "https://pt.aliexpress.com/item/1005008760568743.html",
        language="pt_BR",
        country="BR",
        page_size=6,
        deadline=10.0,
    )
    # Still yields exactly one product from URL id + DOM extras.
    assert len(products) == 1
    assert products[0]["product_id"] == "1005008760568743"
    assert "rating" not in products[0]


# --- end-to-end fetch_aliexpress (injected fetch_html) ---------------------


def _stub_fetcher(html: str, *, reason=FailureReason.OK, status=200, mode="browser"):
    async def fetch_html(target: str):
        return html, status, {}, reason, mode

    return fetch_html


async def test_fetch_search_envelope() -> None:
    env = await A.fetch_aliexpress(
        "https://pt.aliexpress.com/w/wholesale-kindle.html",
        fetch_html=_stub_fetcher(_fx("search.html")),
    )
    assert "failure" not in env
    assert env["mode_used"] == "aliexpress"
    assert env["content_type"] == "application/x-aliexpress"
    q = env["quality"]
    assert q["provider"] == "aliexpress"
    assert q["page_type"] == "search"
    assert q["result_count"] == 4
    assert len(q["products"]) == 4
    assert env["site_name"] == "AliExpress"


async def test_fetch_product_envelope(monkeypatch) -> None:
    async def fake_reviews(pid, **kw):
        return json.loads(_fx("reviews.json"))

    monkeypatch.setattr(A, "_fetch_reviews_json", fake_reviews)
    env = await A.fetch_aliexpress(
        "https://pt.aliexpress.com/item/1005008760568743.html",
        fetch_html=_stub_fetcher(_fx("item.html")),
    )
    assert "failure" not in env
    q = env["quality"]
    assert q["page_type"] == "product"
    assert q["result_count"] == 1
    p = q["products"][0]
    assert p["rating"] == 4.9
    assert p["review_count"] == 30


async def test_fetch_empty_search_flags_no_results() -> None:
    env = await A.fetch_aliexpress(
        "https://pt.aliexpress.com/w/wholesale-zzz.html",
        fetch_html=_stub_fetcher(_fx("search_empty.html")),
    )
    assert "failure" not in env
    assert env["quality"]["result_count"] == 0
    assert "no_results" in env["warnings"]


async def test_fetch_rot_search_is_parse_failed() -> None:
    env = await A.fetch_aliexpress(
        "https://pt.aliexpress.com/w/wholesale-x.html",
        fetch_html=_stub_fetcher(_fx("search_rot.html")),
    )
    assert env["failure"]["reason"] == FailureReason.PARSE_FAILED.value


async def test_fetch_search_passes_through_upstream_failure() -> None:
    # bot_detect flags the punish page upstream → the chain hands the adapter a
    # non-OK reason, which a search surfaces verbatim (no parse attempt).
    env = await A.fetch_aliexpress(
        "https://pt.aliexpress.com/w/wholesale-x.html",
        fetch_html=_stub_fetcher("", reason=FailureReason.BLOCKED_CAPTCHA, status=200),
    )
    assert env["failure"]["reason"] == FailureReason.BLOCKED_CAPTCHA.value


async def test_fetch_empty_search_body_is_failure() -> None:
    env = await A.fetch_aliexpress(
        "https://pt.aliexpress.com/w/wholesale-x.html",
        fetch_html=_stub_fetcher("", reason=FailureReason.OK, status=200),
    )
    assert "failure" in env


async def test_fetch_blocked_product_recovers_via_reviews(monkeypatch) -> None:
    """A walled PDP (blocked_captcha, empty HTML) still yields product_id + the
    open reviews endpoint, flagged with a page_blocked warning."""

    async def fake_reviews(pid, **kw):
        return json.loads(_fx("reviews.json"))

    monkeypatch.setattr(A, "_fetch_reviews_json", fake_reviews)
    env = await A.fetch_aliexpress(
        "https://pt.aliexpress.com/item/1005008760568743.html",
        fetch_html=_stub_fetcher("", reason=FailureReason.BLOCKED_CAPTCHA, status=200),
    )
    assert "failure" not in env
    assert "page_blocked" in env["warnings"]
    p = env["quality"]["products"][0]
    assert p["product_id"] == "1005008760568743"
    assert p["rating"] == 4.9
    assert p["review_count"] == 30
    assert "price" not in p  # PDP DOM was unavailable


async def test_fetch_blocked_product_without_reviews_is_failure(monkeypatch) -> None:
    async def no_reviews(pid, **kw):
        return None

    monkeypatch.setattr(A, "_fetch_reviews_json", no_reviews)
    env = await A.fetch_aliexpress(
        "https://pt.aliexpress.com/item/1005008760568743.html",
        fetch_html=_stub_fetcher("", reason=FailureReason.BLOCKED_CAPTCHA, status=200),
    )
    assert env["failure"]["reason"] == FailureReason.BLOCKED_CAPTCHA.value
