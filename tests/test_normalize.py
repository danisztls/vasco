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
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_normalize_repeated_keys_preserve_within_key_order() -> None:
    """Repeated keys should keep their relative order."""
    assert normalize_url("https://example.com/x?a=1&a=2&a=3") == "https://example.com/x?a=1&a=2&a=3"
    assert (
        normalize_url("https://example.com/x?b=9&a=1&a=2") == "https://example.com/x?a=1&a=2&b=9"
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
