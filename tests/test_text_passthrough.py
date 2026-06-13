"""Unit tests for the plain-text passthrough seam.

`_is_plaintext_response` (which bodies skip trafilatura) and
`convert.text_to_markdown` (the verbatim conversion). End-to-end coverage of the
fetch-chain wiring lives in `tests/test_fetch_integration.py`.
"""

from __future__ import annotations

import pytest

from vasco.converters import convert
from vasco.fetch.urlutils import _is_plaintext_response


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
