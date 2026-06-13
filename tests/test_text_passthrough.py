"""Unit tests for the http-200 content-type routing seam.

`_is_plaintext_response` (verbatim text passthrough), `convert.text_to_markdown`
(the conversion), and `_is_binary_unsupported` (fast-fail for binary blobs).
End-to-end coverage of the fetch-chain wiring lives in
`tests/test_fetch_integration.py`.
"""

from __future__ import annotations

import pytest

from vasco.converters import convert
from vasco.fetch.urlutils import (
    _binary_type_skips_body,
    _is_binary_unsupported,
    _is_plaintext_response,
    _looks_binary,
)


@pytest.mark.parametrize(
    "content_type, expected",
    [
        ("text/plain", True),
        ("text/plain; charset=utf-8", True),
        ("text/markdown", True),
        ("text/x-markdown", True),
        ("text/x-rst", True),
        ("TEXT/PLAIN", True),  # case-insensitive
        ("text/html", False),
        ("application/json", False),  # structured, not prose
        ("application/pdf", False),
        ("", False),
        (None, False),
    ],
)
def test_is_plaintext_response_by_type(content_type, expected) -> None:
    assert _is_plaintext_response(content_type, "# A real markdown body\n") is expected


def test_is_plaintext_response_html_sniff_overrides_type() -> None:
    """A body that sniffs as HTML is NOT passed through even if mislabeled
    text/plain — it still wants trafilatura."""
    assert _is_plaintext_response("text/plain", "<!DOCTYPE html><html>...") is False
    assert _is_plaintext_response("text/plain", "  \n<HTML><body>x</body>") is False
    # Leading text that merely mentions html is fine.
    assert _is_plaintext_response("text/plain", "Use <html> tags in HTML.") is True


def test_text_to_markdown_is_verbatim() -> None:
    body = "# Title\n\nSome prose with a [link](https://x.test).\n"
    md, meta = convert.text_to_markdown(body, content_type="text/markdown")
    assert md == body  # unchanged
    assert meta["word_count"] == len(body.split())
    assert "plaintext_passthrough" in meta["warnings"]
    assert meta["quality"]["boilerplate_ratio"] == 0.0
    # Full html_to_markdown metadata shape (so success_envelope finds every key).
    for key in (
        "title",
        "byline",
        "published",
        "modified",
        "language",
        "site_name",
        "image",
        "links",
    ):
        assert key in meta


def test_text_to_markdown_short_content_warning() -> None:
    md, meta = convert.text_to_markdown("tiny", content_type="text/plain")
    assert md == "tiny"
    assert "short_content" in meta["warnings"]


def test_text_to_markdown_empty_body() -> None:
    md, meta = convert.text_to_markdown("", content_type="text/plain")
    assert md == ""
    assert meta["word_count"] == 0


# --- binary blob fast-fail ---------------------------------------------------


@pytest.mark.parametrize(
    "content_type, expected",
    [
        ("image/png", True),
        ("image/jpeg", True),
        ("image/gif; charset=binary", True),
        ("image/svg+xml", True),  # XML in theory, a graphic blob in practice
        ("audio/mpeg", True),
        ("video/mp4", True),
        ("font/woff2", True),
        ("application/zip", True),
        ("application/gzip", True),
        ("application/x-tar", True),
        ("application/wasm", True),
        ("application/vnd.android.package-archive", True),
        ("text/html", False),
        ("text/plain", False),
        ("application/pdf", False),  # routed to the pdf converter upstream
        ("application/json", False),  # structured text, not a binary blob
        ("application/xml", False),
        ("", False),
        (None, False),
    ],
)
def test_is_binary_unsupported_by_type(content_type, expected) -> None:
    # A non-octet-stream verdict is body-independent.
    assert _is_binary_unsupported(content_type, "anything") is expected


def test_octet_stream_needs_binary_body() -> None:
    """application/octet-stream is the generic 'unknown bytes' type and is
    occasionally a mislabeled text file — only binary-looking bodies fail."""
    binary_body = "PK\x03\x04" + "\x00" * 50  # zip-ish header + NULs
    assert _is_binary_unsupported("application/octet-stream", binary_body) is True
    text_body = "Just a plain README mislabeled as octet-stream.\n" * 3
    assert _is_binary_unsupported("application/octet-stream", text_body) is False


def test_looks_binary() -> None:
    assert _looks_binary("hello world\nthis is text\n") is False
    assert _looks_binary("\x00\x01\x02\x03binary\x00") is True
    # High density of decode-replacement chars (U+FFFD) reads as binary.
    assert _looks_binary("�" * 100 + "abc") is True
    assert _looks_binary("") is False


@pytest.mark.parametrize(
    "content_type, expected",
    [
        # Definite-binary from the header → body download can be skipped.
        ("image/png", True),
        ("image/svg+xml", True),
        ("video/mp4", True),
        ("application/zip", True),
        ("application/wasm", True),
        # Ambiguous: needs a body sniff, so the body is NOT skipped.
        ("application/octet-stream", False),
        # Text-ish / unknown: full body.
        ("text/html", False),
        ("text/plain", False),
        ("application/json", False),
        ("", False),
        (None, False),
    ],
)
def test_binary_type_skips_body(content_type, expected) -> None:
    assert _binary_type_skips_body(content_type) is expected
