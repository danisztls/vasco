from __future__ import annotations

import pytest

from vasco.cache import normalize_url, registered_domain


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://Example.COM/foo", "https://example.com/foo"),
        ("https://example.com:443/foo", "https://example.com/foo"),
        ("http://example.com:80/foo", "http://example.com/foo"),
        ("https://example.com/foo/", "https://example.com/foo"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com/foo?b=2&a=1", "https://example.com/foo?a=1&b=2"),
        (
            "https://example.com/foo?utm_source=x&utm_medium=y&a=1",
            "https://example.com/foo?a=1",
        ),
        ("https://example.com/foo?fbclid=abc&gclid=def", "https://example.com/foo"),
        ("https://example.com/foo#section", "https://example.com/foo"),
        (
            "https://example.com/foo?a=1&a=2",
            "https://example.com/foo?a=1&a=2",
        ),
        (
            "https://Example.COM:443/foo/?b=2&utm_source=x&a=1#frag",
            "https://example.com/foo?a=1&b=2",
        ),
        ("https://example.com:8443/foo", "https://example.com:8443/foo"),
        ("https://example.com/foo?key=", "https://example.com/foo?key="),
        ("https://example.com/foo?key", "https://example.com/foo?key"),
        ("https://example.com/foo?mc_eid=abc&a=1", "https://example.com/foo?a=1"),
        # Broadened tracking denylist: ad-click, social-share, and Alibaba IDs
        # are dropped like the original utm_/fbclid set.
        ("https://example.com/x?gclsrc=aw", "https://example.com/x"),
        ("https://example.com/x?igshid=abc", "https://example.com/x"),
        ("https://example.com/p?spm=a2g0o.detail", "https://example.com/p"),
        # ClearURLs-derived globally-safe params: email/CRM tokens, GA linkers,
        # and Google Merchant's listing-click id all strip.
        ("https://example.com/x?srsltid=AbC123", "https://example.com/x"),
        ("https://example.com/x?mkt_tok=eyJ0", "https://example.com/x"),
        ("https://example.com/x?__hstc=1.2.3&__hssc=4", "https://example.com/x"),
        ("https://example.com/x?_ga=GA1.2&_gl=1xyz&a=1", "https://example.com/x?a=1"),
        # mtm_* (Matomo) prefix strips like utm_*.
        (
            "https://example.com/foo?mtm_campaign=x&mtm_source=y&a=1",
            "https://example.com/foo?a=1",
        ),
        # Mixed: tracking junk drops, the real content param survives.
        ("https://example.com/p?spm=abc&id=42", "https://example.com/p?id=42"),
        # Ambiguous params are KEPT — sometimes load-bearing, so over-stripping
        # would silently collapse distinct pages. These rows lock that in.
        (
            "https://example.com/x?ref=homepage&source=nav",
            "https://example.com/x?ref=homepage&source=nav",
        ),
        (
            "https://example.com/x?page=2&sort=price",
            "https://example.com/x?page=2&sort=price",
        ),
        (
            "https://example.com/foo?b=2&a=1&c=3",
            "https://example.com/foo?a=1&b=2&c=3",
        ),
        ("HTTPS://EXAMPLE.COM/FOO", "https://example.com/FOO"),
        # YouTube short links upgrade to the canonical /watch?v=… form so
        # both URL shapes hit the same cache row.
        ("https://youtu.be/dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://www.youtu.be/dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
        (
            "https://youtu.be/dQw4w9WgXcQ?t=42",
            "https://youtube.com/watch?t=42&v=dQw4w9WgXcQ",
        ),
        (
            "https://youtu.be/dQw4w9WgXcQ?utm_source=x&t=42",
            "https://youtube.com/watch?t=42&v=dQw4w9WgXcQ",
        ),
        # All youtube.com variants (subdomain, local TLD) collapse to the
        # canonical host so the same video shares one cache row.
        (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://www.youtube.com.br/watch?v=dQw4w9WgXcQ&t=42",
            "https://youtube.com/watch?t=42&v=dQw4w9WgXcQ",
        ),
        (
            "https://youtube.com.br/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        # Alternate video paths (/embed, /shorts, /v, /live) collapse to /watch.
        (
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://www.youtube.com/shorts/abcXYZ_-12",
            "https://youtube.com/watch?v=abcXYZ_-12",
        ),
        (
            "https://www.youtube.com/v/dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://www.youtube.com/live/dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        # Nocookie privacy-embed domain collapses to youtube.com/watch.
        (
            "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://youtube-nocookie.com/embed/dQw4w9WgXcQ?start=42",
            "https://youtube.com/watch?start=42&v=dQw4w9WgXcQ",
        ),
        # Watch URL with v= after another query param.
        (
            "https://www.youtube.com/watch?list=PL123&v=dQw4w9WgXcQ&t=42",
            "https://youtube.com/watch?list=PL123&t=42&v=dQw4w9WgXcQ",
        ),
        # Non-video YouTube URLs get only host canonicalization.
        (
            "https://www.youtube.com/playlist?list=PLABC",
            "https://youtube.com/playlist?list=PLABC",
        ),
        (
            "https://m.youtube.com/c/channelname",
            "https://youtube.com/c/channelname",
        ),
        # Non-YouTube hosts containing "youtube" in path are untouched.
        ("https://example.com/youtube.com/foo", "https://example.com/youtube.com/foo"),
        # Wikipedia: mobile subdomain collapses, title gets normalized.
        (
            "https://en.m.wikipedia.org/wiki/Python_(programming_language)",
            "https://en.wikipedia.org/wiki/Python_(programming_language)",
        ),
        (
            "https://de.m.wikipedia.org/wiki/Berlin",
            "https://de.wikipedia.org/wiki/Berlin",
        ),
        # Multi-char and hyphenated language editions fold mobile -> desktop too.
        (
            "https://simple.m.wikipedia.org/wiki/Dog",
            "https://simple.wikipedia.org/wiki/Dog",
        ),
        (
            "https://zh-min-nan.m.wikipedia.org/wiki/Tang",
            "https://zh-min-nan.wikipedia.org/wiki/Tang",
        ),
        # Plain index.php?title= folds to the canonical /wiki/ form (same row).
        (
            "https://en.wikipedia.org/w/index.php?title=new%20york%20city",
            "https://en.wikipedia.org/wiki/New_york_city",
        ),
        # Revision/diff/edit variants are NOT folded — they keep distinct rows.
        (
            "https://en.wikipedia.org/w/index.php?oldid=12345&title=Foo",
            "https://en.wikipedia.org/w/index.php?oldid=12345&title=Foo",
        ),
        # Percent-encoded spaces become underscores, first char uppercased.
        (
            "https://en.wikipedia.org/wiki/new%20york%20city",
            "https://en.wikipedia.org/wiki/New_york_city",
        ),
        # Fragment and query stripped from the title portion.
        (
            "https://en.wikipedia.org/wiki/Foo#History?bar=1",
            "https://en.wikipedia.org/wiki/Foo",
        ),
        # Other Wikimedia projects: same normalization rules apply.
        (
            "https://en.m.wiktionary.org/wiki/hello",
            "https://en.wiktionary.org/wiki/Hello",
        ),
        (
            "https://fr.m.wikisource.org/wiki/Les_Mis%C3%A9rables",
            "https://fr.wikisource.org/wiki/Les_Mis%C3%A9rables",
        ),
        (
            "https://de.wikibooks.org/wiki/some%20book",
            "https://de.wikibooks.org/wiki/Some_book",
        ),
        (
            "https://en.m.wikivoyage.org/wiki/Paris#Get_in",
            "https://en.wikivoyage.org/wiki/Paris",
        ),
        (
            "https://it.wikiquote.org/wiki/Dante_Alighieri",
            "https://it.wikiquote.org/wiki/Dante_Alighieri",
        ),
        # AMP query markers collapse to the canonical URL.
        ("https://example.com/article?amp=1", "https://example.com/article"),
        ("https://example.com/article?amp=", "https://example.com/article"),
        ("https://example.com/article?amp=true", "https://example.com/article"),
        ("https://example.com/article?amp", "https://example.com/article"),
        ("https://example.com/x?output=amp&id=42", "https://example.com/x?id=42"),
        # `output=json` is not AMP — keep it.
        ("https://example.com/x?output=json", "https://example.com/x?output=json"),
        # AMP path segments and suffix get folded.
        ("https://example.com/article/amp", "https://example.com/article"),
        ("https://example.com/amp/article", "https://example.com/article"),
        (
            "https://example.com/imovel/amp/foo-123",
            "https://example.com/imovel/foo-123",
        ),
        # Full-segment match only — don't mangle "amphibian".
        ("https://example.com/amphibian/x", "https://example.com/amphibian/x"),
        # Redirect unwrapping: known wrappers yield the real destination, which
        # then flows through the rest of normalization (host, params, tracking).
        (
            "https://l.facebook.com/l.php?u=https%3A%2F%2Frealsite.com%2Fart&h=AT1",
            "https://realsite.com/art",
        ),
        (
            "https://www.google.com/url?q=https%3A%2F%2Fexample.org%2Fpage&sa=D&usg=x",
            "https://example.org/page",
        ),
        (
            "https://www.google.com/url?url=https%3A%2F%2Fexample.org&rct=j",
            "https://example.org",
        ),
        (
            "https://out.reddit.com/t3_abc?url=https%3A%2F%2Fexample.org%2Fx&token=y",
            "https://example.org/x",
        ),
        (
            "https://www.youtube.com/redirect?q=https%3A%2F%2Fexample.org",
            "https://example.org",
        ),
        # Unwrap then strip tracking from the inner URL in one pass.
        (
            "https://l.facebook.com/l.php?u=https%3A%2F%2Fexample.org%2Fp%3Futm_source%3Dfb%26id%3D9",
            "https://example.org/p?id=9",
        ),
        # A wrapped YouTube link unwraps AND canonicalizes to the bare watch URL.
        (
            "https://www.google.com/url?q=https%3A%2F%2Fyoutu.be%2FdQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        # NEGATIVE: a normal page carrying a ?url= param on a non-redirector
        # host is never unwrapped (value kept verbatim, only param-sorted).
        (
            "https://example.com/view?url=https%3A%2F%2Fother.com",
            "https://example.com/view?url=https%3A%2F%2Fother.com",
        ),
        # NEGATIVE: a relative target isn't a real redirect — leave the wrapper.
        (
            "https://www.google.com/url?q=%2Flocal%2Fpath&sa=D",
            "https://www.google.com/url?q=%2Flocal%2Fpath&sa=D",
        ),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_normalize_repeated_keys_preserve_within_key_order() -> None:
    """Repeated keys should keep their relative order."""
    assert (
        normalize_url("https://example.com/x?a=1&a=2&a=3")
        == "https://example.com/x?a=1&a=2&a=3"
    )
    assert (
        normalize_url("https://example.com/x?b=9&a=1&a=2")
        == "https://example.com/x?a=1&a=2&b=9"
    )


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.foo.example.com/x", "example.com"),
        ("https://example.com/x", "example.com"),
        ("https://example.co.uk/x", "example.co.uk"),
        ("https://www.example.co.uk/x", "example.co.uk"),
        ("https://sub.example.co.uk/x", "example.co.uk"),
        ("https://foo.bar.baz.example.com/x", "example.com"),
        # compound ccSLDs the old hand-rolled heuristic handled
        ("https://www.vivareal.com.br/aluguel/", "vivareal.com.br"),
        # ... and ones it did NOT (real PSL via tldextract gets these right)
        ("https://www.asahi.co.jp/news", "asahi.co.jp"),
        ("https://canberra.act.edu.au/x", "canberra.act.edu.au"),
        # no public suffix → fall back to bare host (minus www.)
        ("http://localhost:8000/x", "localhost"),
        ("http://127.0.0.1:5000/x", "127.0.0.1"),
    ],
)
def test_registered_domain(url: str, expected: str) -> None:
    assert registered_domain(url) == expected
