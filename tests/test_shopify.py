from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from vasco.adapters import shopify as S
from vasco.config import AdaptersCfg, Config, ShopifyCfg
from vasco.errors import AdapterParseError, FailureReason

FX = Path(__file__).parent / "fixtures" / "shopify"


def _fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_memos() -> None:
    """Probe / currency memos are process-lifetime — clear between tests."""
    S._reset_for_tests()
    yield
    S._reset_for_tests()


# --- routing ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://simwooddenim.com/products/foo", True),
        ("https://simwooddenim.com/collections/jeans", True),
        ("https://simwooddenim.com/collections/jeans?page=2", True),
        ("https://simwooddenim.com/search?q=jeans", True),
        ("https://simwooddenim.com/products/foo.js", True),
        ("https://simwooddenim.com/products/foo.json", True),
        # *.myshopify.com is always Shopify on a claimable path
        ("https://acme.myshopify.com/products/foo", True),
        # non-claimable paths on a known domain fall through
        ("https://simwooddenim.com/", False),
        ("https://simwooddenim.com/pages/about", False),
        ("https://simwooddenim.com/blogs/news/post", False),
        ("https://simwooddenim.com/cart", False),
        # tag-filtered collection has no platform JSON → not claimed
        ("https://simwooddenim.com/collections/jeans/mens", False),
        # unknown domain → not certain (it's a *candidate*, tested separately)
        ("https://example.com/products/foo", False),
        ("", False),
    ],
)
def test_is_shopify_url(url: str, expected: bool) -> None:
    assert S.is_shopify_url(url) is expected


def test_is_shopify_url_honors_cfg_domains() -> None:
    cfg = Config(adapters=AdaptersCfg(shopify=ShopifyCfg(domains=("mystore.com",))))
    assert S.is_shopify_url("https://mystore.com/products/x", cfg) is True
    assert S.is_shopify_url("https://www.mystore.com/collections/all", cfg) is True
    assert S.is_shopify_url("https://other.com/products/x", cfg) is False


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.com/products/foo", True),
        ("https://example.com/collections/all", True),
        # direct endpoints / search are not candidate *page* shapes
        ("https://example.com/products/foo.js", False),
        ("https://example.com/collections/all.json", False),
        ("https://example.com/search?q=x", False),
        ("https://example.com/", False),
        ("https://example.com/pages/foo", False),
        # known domains are certain, not candidates
        ("https://simwooddenim.com/products/foo", False),
    ],
)
def test_is_shopify_candidate(url: str, expected: bool) -> None:
    assert S.is_shopify_candidate(url) is expected


def test_candidate_disabled_when_autodetect_off() -> None:
    cfg = Config(adapters=AdaptersCfg(shopify=ShopifyCfg(autodetect=False)))
    assert S.is_shopify_candidate("https://example.com/products/foo", cfg) is False


def test_candidate_skips_negative_memoized_domain() -> None:
    assert S.is_shopify_candidate("https://example.com/products/foo") is True
    S._probe_memo["example.com"] = False  # a prior probe proved it's not Shopify
    assert S.is_shopify_candidate("https://example.com/products/foo") is False


def test_positive_memo_promotes_to_certain() -> None:
    assert S.is_shopify_url("https://example.com/products/foo") is False
    S._probe_memo["example.com"] = True  # confirmed by a prior probe
    assert S.is_shopify_url("https://example.com/products/foo") is True
    assert S.is_shopify_candidate("https://example.com/products/foo") is False


# --- endpoint mapping -------------------------------------------------------


def test_claim_product() -> None:
    pt, ep, kind = S._claim("https://shop.com/products/widget", None)
    assert pt == "product" and kind == S._PRODUCT_JS
    assert ep == "https://shop.com/products/widget.js"


def test_claim_product_passthrough() -> None:
    assert S._claim("https://shop.com/products/widget.js", None)[2] == S._PRODUCT_JS
    assert S._claim("https://shop.com/products/widget.json", None)[2] == S._PRODUCT_JSON


def test_claim_collection_with_page_and_limit() -> None:
    cfg = Config(adapters=AdaptersCfg(shopify=ShopifyCfg(collection_limit=100)))
    pt, ep, kind = S._claim("https://shop.com/collections/jeans?page=3", cfg)
    assert pt == "collection" and kind == S._COLLECTION
    assert "/collections/jeans/products.json" in ep
    assert "page=3" in ep and "limit=100" in ep


def test_claim_collection_passthrough() -> None:
    pt, ep, kind = S._claim("https://shop.com/collections/jeans/products.json", None)
    assert pt == "collection" and kind == S._COLLECTION and "limit=250" in ep


def test_claim_search() -> None:
    pt, ep, kind = S._claim("https://shop.com/search?q=blue+jeans", None)
    assert pt == "search" and kind == S._SUGGEST
    assert "/search/suggest.json" in ep and "q=blue+jeans" in ep
    assert "resources%5Btype%5D=product" in ep and "resources%5Blimit%5D=10" in ep


@pytest.mark.parametrize(
    "url",
    [
        "https://shop.com/",
        "https://shop.com/collections/jeans/mens",  # tag filter
        "https://shop.com/search",  # no q
        "https://shop.com/pages/about",
    ],
)
def test_claim_unclaimable(url: str) -> None:
    assert S._claim(url, None) is None


# --- value helpers ----------------------------------------------------------


def test_money_cents_and_decimal() -> None:
    assert S._money(4790, cents=True) == 47.90
    assert S._money(9290, cents=True) == 92.90
    assert S._money("68.90", cents=False) == 68.90
    assert S._money("133.83", cents=False) == 133.83
    assert S._money(None, cents=True) is None
    assert S._money(True, cents=True) is None
    assert S._money("", cents=False) is None
    assert S._money("not-a-price", cents=False) is None


def test_abs_img() -> None:
    assert S._abs_img("//cdn.shopify.com/x.jpg") == "https://cdn.shopify.com/x.jpg"
    assert S._abs_img("https://cdn/x.jpg") == "https://cdn/x.jpg"
    assert S._abs_img({"src": "//c/y.jpg"}) == "https://c/y.jpg"
    assert S._abs_img({"url": "//c/z.jpg"}) == "https://c/z.jpg"
    assert S._abs_img(None) is None


def test_clean_url_strips_attribution() -> None:
    out = S._clean_url(
        "/products/x?_pos=1&_psq=jeans&_ss=e&_v=1.0&variant=42", "https://shop.com"
    )
    assert out == "https://shop.com/products/x?variant=42"


def test_strip_html() -> None:
    assert S._strip_html("<h4>Hi</h4>\n<p>there &amp; more</p>") == "Hi there & more"
    assert S._strip_html("") is None
    assert S._strip_html(None) is None


# --- parsing (real fixtures) ------------------------------------------------


def test_parse_product_js_cents() -> None:
    pt, prods = S._parse(
        _fx("product.js.json"), S._PRODUCT_JS, "https://simwooddenim.com/products/ls07"
    )
    assert pt == "product" and len(prods) == 1
    p = prods[0]
    assert p["price"] == 47.90  # 4790 cents
    assert p["original_price"] == 92.90  # 9290 cents > price
    assert p["brand"] == "SIMWOOD"
    assert p["product_type"] == "Jeans"
    assert p["available"] is True
    assert [o["name"] for o in p["options"]] == ["Color", "Size"]
    assert p["variants"] and p["variants"][0]["price"] == 47.90
    assert p["variants"][0]["sku"]
    assert len(p["images"]) >= 1 and all(i.startswith("https://") for i in p["images"])
    assert p["description"].startswith("Product information")
    assert (
        p["url"]
        == "https://simwooddenim.com/products/ls07-14-2oz-elastic-washed-vintage-jeans"
    )


def test_parse_collection_decimal_strings() -> None:
    pt, prods = S._parse(
        _fx("collection_products.json"),
        S._COLLECTION,
        "https://simwooddenim.com/collections/jeans",
    )
    assert pt == "collection" and len(prods) == 3
    c = prods[0]
    assert c["position"] == 1
    assert c["price"] == 68.90  # min variant price (decimal string)
    assert c["original_price"] == 133.83
    assert c["available"] is True
    assert c["brand"] == "SIMWOOD"
    assert c["url"].startswith("https://simwooddenim.com/products/")
    assert c["image"].startswith("https://")
    # listing cards stay lean — no per-variant gallery/options
    assert "variants" not in c and "images" not in c


def test_parse_suggest_strips_attribution() -> None:
    pt, prods = S._parse(
        _fx("suggest.json"), S._SUGGEST, "https://simwooddenim.com/search?q=jeans"
    )
    assert pt == "search" and len(prods) == 5
    s = prods[0]
    assert s["price"] == 47.90
    assert "_pos" not in s["url"] and "_psq" not in s["url"]
    assert s["url"].startswith("https://simwooddenim.com/products/")


# --- anchor / rot / empty ---------------------------------------------------


def test_parse_non_json_raises() -> None:
    with pytest.raises(AdapterParseError):
        S._parse("<html>not json</html>", S._PRODUCT_JS, "https://shop.com/products/x")


def test_parse_product_without_object_raises() -> None:
    with pytest.raises(AdapterParseError):
        S._parse('{"foo": 1}', S._PRODUCT_JS, "https://shop.com/products/x")


def test_parse_collection_without_products_key_raises() -> None:
    with pytest.raises(AdapterParseError):
        S._parse('{"foo": []}', S._COLLECTION, "https://shop.com/collections/x")


def test_parse_empty_collection_is_ok() -> None:
    pt, prods = S._parse(
        '{"products": []}', S._COLLECTION, "https://shop.com/collections/x"
    )
    assert pt == "collection" and prods == []


# --- fetch_shopify (injected fetcher) ---------------------------------------


def _fetcher(body: str, status: int = 200, reason: FailureReason = FailureReason.OK):
    """Build an injected fetch_html that serves `body`, plus cart.js currency."""

    async def _fetch(target: str):
        if target.endswith("/cart.js"):
            return ('{"currency": "USD"}', 200, {}, FailureReason.OK, "http")
        return (body, status, {}, reason, "http")

    return _fetch


def test_fetch_shopify_collection_success() -> None:
    env = asyncio.run(
        S.fetch_shopify(
            "https://simwooddenim.com/collections/jeans",
            fetch_html=_fetcher(_fx("collection_products.json")),
        )
    )
    assert env["mode_used"] == "shopify"
    assert "failure" not in env
    q = env["quality"]
    assert q["provider"] == "shopify"
    assert q["page_type"] == "collection"
    assert q["shop"] == "simwooddenim.com"
    assert q["collection"] == "jeans"
    assert q["currency"] == "USD"
    assert q["result_count"] == 3
    assert env["warnings"] == []


def test_fetch_shopify_search_query_and_currency_memo() -> None:
    calls = {"cart": 0}
    body = _fx("suggest.json")

    async def _fetch(target: str):
        if target.endswith("/cart.js"):
            calls["cart"] += 1
            return ('{"currency": "USD"}', 200, {}, FailureReason.OK, "http")
        return (body, 200, {}, FailureReason.OK, "http")

    env1 = asyncio.run(
        S.fetch_shopify("https://simwooddenim.com/search?q=jeans", fetch_html=_fetch)
    )
    env2 = asyncio.run(
        S.fetch_shopify("https://simwooddenim.com/search?q=denim", fetch_html=_fetch)
    )
    assert env1["quality"]["query"] == "jeans"
    assert env2["quality"]["query"] == "denim"
    assert env1["quality"]["currency"] == "USD"
    # cart.js fetched once per domain (memoized), not per request.
    assert calls["cart"] == 1


def test_currency_none_on_failure() -> None:
    async def _fetch(target: str):
        if target.endswith("/cart.js"):
            return ("", 500, {}, FailureReason.SERVER_ERROR, "http")
        return (_fx("collection_products.json"), 200, {}, FailureReason.OK, "http")

    env = asyncio.run(
        S.fetch_shopify("https://simwooddenim.com/collections/jeans", fetch_html=_fetch)
    )
    assert "currency" not in env["quality"]  # compacted away when None


def test_fetch_shopify_empty_collection_warns_no_results() -> None:
    env = asyncio.run(
        S.fetch_shopify(
            "https://simwooddenim.com/collections/empty",
            fetch_html=_fetcher('{"products": []}'),
        )
    )
    assert "failure" not in env
    assert env["quality"]["result_count"] == 0
    assert "no_results" in env["warnings"]


def test_fetch_shopify_rot_returns_parse_failed() -> None:
    env = asyncio.run(
        S.fetch_shopify(
            "https://simwooddenim.com/products/x",
            fetch_html=_fetcher("<html>not shopify</html>"),
        )
    )
    assert env["failure"]["reason"] == str(FailureReason.PARSE_FAILED)


def test_fetch_shopify_propagates_fetch_failure() -> None:
    env = asyncio.run(
        S.fetch_shopify(
            "https://simwooddenim.com/products/x",
            fetch_html=_fetcher("", status=404, reason=FailureReason.NOT_FOUND),
        )
    )
    assert env["failure"]["reason"] == str(FailureReason.NOT_FOUND)


# --- probe semantics --------------------------------------------------------


def test_probe_miss_non_json_raises_not_shopify_and_negative_memos() -> None:
    with pytest.raises(S.NotShopify):
        asyncio.run(
            S.fetch_shopify(
                "https://example.com/products/x",
                fetch_html=_fetcher("<html>wordpress</html>"),
                probe=True,
            )
        )
    assert S._probe_memo.get("example.com") is False


def test_probe_miss_404_does_not_negative_memo() -> None:
    with pytest.raises(S.NotShopify):
        asyncio.run(
            S.fetch_shopify(
                "https://example.com/products/x",
                fetch_html=_fetcher("", status=404, reason=FailureReason.NOT_FOUND),
                probe=True,
            )
        )
    # 404 is ambiguous (bad handle vs. not Shopify) — don't pin the domain.
    assert "example.com" not in S._probe_memo


def test_probe_hit_positive_memos_and_returns_envelope() -> None:
    env = asyncio.run(
        S.fetch_shopify(
            "https://example.com/products/ls07",
            fetch_html=_fetcher(_fx("product.js.json")),
            probe=True,
        )
    )
    assert env["mode_used"] == "shopify"
    assert env["quality"]["page_type"] == "product"
    assert S._probe_memo.get("example.com") is True


# --- persistent probe memo (adapter_probe table) ----------------------------


def test_probe_verdict_persists_across_process(tmp_path) -> None:
    """A confirmed probe is written to the cache; after the in-process memo is
    cleared (a fresh process), the domain is still recognized via the DB —
    without re-probing."""
    from vasco.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    try:
        env = asyncio.run(
            S.fetch_shopify(
                "https://newstore.com/products/ls07",
                fetch_html=_fetcher(_fx("product.js.json")),
                cache=cache,
                probe=True,
            )
        )
        assert env["mode_used"] == "shopify"
        assert cache.get_probe("shopify", "newstore.com") is True

        # Simulate a brand-new process: only the persistent store survives.
        S._reset_for_tests()
        assert S.is_shopify_url("https://newstore.com/products/x", cache=cache) is True
        # Confirmed → no longer a *candidate* (it's certain), so no re-probe.
        assert (
            S.is_shopify_candidate("https://newstore.com/products/y", cache=cache)
            is False
        )
    finally:
        cache.close()


def test_negative_probe_persists_and_skips_reprobe(tmp_path) -> None:
    from vasco.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    try:
        with pytest.raises(S.NotShopify):
            asyncio.run(
                S.fetch_shopify(
                    "https://notshop.com/products/x",
                    fetch_html=_fetcher("<html>not shopify</html>"),
                    cache=cache,
                    probe=True,
                )
            )
        assert cache.get_probe("shopify", "notshop.com") is False

        S._reset_for_tests()  # fresh process
        # Persisted negative → not a candidate (no re-probe) and not certain.
        assert (
            S.is_shopify_candidate("https://notshop.com/products/x", cache=cache)
            is False
        )
        assert S.is_shopify_url("https://notshop.com/products/x", cache=cache) is False
    finally:
        cache.close()


def test_ambiguous_404_probe_not_persisted(tmp_path) -> None:
    """A 404 during a probe is ambiguous (bad handle vs. not Shopify) — it must
    not write a verdict, so the domain stays probeable."""
    from vasco.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    try:
        with pytest.raises(S.NotShopify):
            asyncio.run(
                S.fetch_shopify(
                    "https://maybe.com/products/x",
                    fetch_html=_fetcher("", status=404, reason=FailureReason.NOT_FOUND),
                    cache=cache,
                    probe=True,
                )
            )
        assert cache.get_probe("shopify", "maybe.com") is None
        S._reset_for_tests()
        assert (
            S.is_shopify_candidate("https://maybe.com/products/x", cache=cache) is True
        )
    finally:
        cache.close()


def test_seed_domain_not_written_to_probe_table(tmp_path) -> None:
    """Statically-known (seed/config) domains never consult the probe table, so a
    successful fetch should not write a redundant row."""
    from vasco.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    try:
        env = asyncio.run(
            S.fetch_shopify(
                "https://simwooddenim.com/collections/jeans",
                fetch_html=_fetcher(_fx("collection_products.json")),
                cache=cache,
            )
        )
        assert env["mode_used"] == "shopify"
        assert cache.get_probe("shopify", "simwooddenim.com") is None
    finally:
        cache.close()


def test_stale_probe_verdict_is_reprobed(tmp_path) -> None:
    """A verdict older than the TTL is treated as unknown so a re-platformed site
    self-heals."""
    import time

    from vasco import cache as cache_mod
    from vasco.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    try:
        cache.set_probe("shopify", "old.com", False)
        # Backdate the row beyond the TTL.
        stale = int(time.time()) - cache_mod._PROBE_TTL_SECONDS - 1
        cache._conn.execute(
            "UPDATE adapter_probe SET updated_at = ? WHERE domain = ?",
            (stale, "old.com"),
        )
        cache._conn.commit()
        assert cache.get_probe("shopify", "old.com") is None  # expired → unknown
        assert S.is_shopify_candidate("https://old.com/products/x", cache=cache) is True
    finally:
        cache.close()


def test_purge_domain_forgets_probe(tmp_path) -> None:
    from vasco.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    try:
        cache.set_probe("shopify", "gone.com", True)
        assert cache.get_probe("shopify", "gone.com") is True
        cache.purge_domain("https://www.gone.com/anything")
        assert cache.get_probe("shopify", "gone.com") is None
    finally:
        cache.close()


def test_render_markdown_product_includes_description() -> None:
    _pt, prods = S._parse(
        _fx("product.js.json"), S._PRODUCT_JS, "https://simwooddenim.com/products/ls07"
    )
    md = S._render_markdown(
        prods, page_type="product", currency="USD", shop="simwooddenim.com"
    )
    assert "USD" in md and prods[0]["title"] in md
    assert "Product information" in md  # description rendered on product pages
