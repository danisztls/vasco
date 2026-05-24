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
    ],
)
def test_registered_domain(url: str, expected: str) -> None:
    assert registered_domain(url) == expected
