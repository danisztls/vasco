from __future__ import annotations

from typing import Any

import pytest

from vasco import extract as extract_mod
from vasco.extract import extract

# We patch the *bound* reference inside extract.py via its `_fetch` alias.
# extract.py does ``from vasco import fetch as _fetch`` then calls
# ``_fetch.fetch_one(...)`` — patching ``vasco.fetch.fetch_one`` is sufficient
# because the alias resolves the attribute at call time.


_CAMOUFOX_MARKDOWN = """# Browser stealth notes

Modern websites probe browsers for thousands of subtle signals. Common
fingerprinting vectors include Canvas, WebGL, audio context, and timezone.

Camoufox patches Firefox to hide fingerprinting surfaces and spoof the
results that scrapers usually leak. It is designed for anti-fingerprinting
work on JS-heavy sites and pairs well with Playwright.

Playwright drives the browser via CDP-like protocols and supports both
Chromium and Firefox. It is great for general scraping.

In contrast, plain httpx is fast but cannot evaluate JavaScript at all.
"""


def _success_envelope(
    markdown: str = _CAMOUFOX_MARKDOWN, **overrides: Any
) -> dict[str, Any]:
    env: dict[str, Any] = {
        "url_requested": "https://example.com/article",
        "url_final": "https://example.com/article",
        "url_canonical": "https://example.com/article",
        "http_status": 200,
        "mode_used": "http",
        "title": "Camoufox notes",
        "byline": "Anonymous",
        "published": "2025-11-01",
        "markdown": markdown,
    }
    env.update(overrides)
    return env


def _patch_fetch(
    monkeypatch: pytest.MonkeyPatch, envelope: dict[str, Any]
) -> list[dict[str, Any]]:
    """Patch fetch_one to return ``envelope`` and record the kwargs it saw."""
    calls: list[dict[str, Any]] = []

    async def fake_fetch_one(url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"url": url, **kwargs})
        return envelope

    monkeypatch.setattr("vasco.fetch.fetch_one", fake_fetch_one, raising=False)
    return calls


async def test_top_passage_matches_query(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetch(monkeypatch, _success_envelope())
    result = await extract(
        "https://example.com/article",
        query="camoufox anti-fingerprinting",
    )
    assert result["passages"], "expected at least one passage"
    top = result["passages"][0]
    text_lower = top["text"].lower()
    assert "camoufox" in text_lower
    assert "fingerprinting" in text_lower
    assert top["score"] > 0
    assert isinstance(top["offset"], int)
    assert top["offset"] >= 0
    # Metadata flows through.
    assert result["url"] == "https://example.com/article"
    assert result["title"] == "Camoufox notes"
    assert result["query"] == "camoufox anti-fingerprinting"
    assert result["mode_used"] == "http"


async def test_top_one_returns_single_passage(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetch(monkeypatch, _success_envelope())
    result = await extract(
        "https://example.com/article",
        query="camoufox fingerprinting playwright",
        top=1,
    )
    assert len(result["passages"]) == 1


async def test_context_chars_window_size(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetch(monkeypatch, _success_envelope())
    context_chars = 100
    result = await extract(
        "https://example.com/article",
        query="camoufox fingerprinting",
        top=1,
        context_chars=context_chars,
    )
    assert result["passages"], "expected a passage"
    passage = result["passages"][0]
    text_len = len(passage["text"])
    ctx_len = len(passage["context"])
    # Context is text + up to context_chars on each side, clipped at document
    # boundaries — so it must be at least the text length and never exceed
    # text + 2 * context_chars.
    assert text_len <= ctx_len <= text_len + 2 * context_chars
    # The passage's text should appear inside its surrounding context.
    assert passage["text"] in passage["context"]


async def test_failure_envelope_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    failure_env = {
        "url_requested": "https://example.com/missing",
        "http_status": 404,
        "mode_used": "http",
        "failure": {
            "reason": "not_found",
            "retry_after_seconds": None,
            "message": "HTTP 404",
        },
    }
    _patch_fetch(monkeypatch, failure_env)
    result = await extract(
        "https://example.com/missing",
        query="anything",
    )
    assert result["passages"] == []
    assert result["failure"]["reason"] == "not_found"
    assert result["query"] == "anything"
    assert result["url"] == "https://example.com/missing"


async def test_empty_markdown_returns_no_passages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch(monkeypatch, _success_envelope(markdown=""))
    result = await extract(
        "https://example.com/article",
        query="anything",
    )
    assert result["passages"] == []
    assert "failure" not in result


async def test_no_positive_scores_returns_no_passages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Markdown has content but shares zero query tokens — BM25 yields 0 scores.
    md = (
        "The quick brown fox jumps over the lazy dog near the riverbank.\n\n"
        "Birds were singing in the trees and the morning air was cool.\n\n"
        "Children walked along the path carrying baskets of bread."
    )
    _patch_fetch(monkeypatch, _success_envelope(markdown=md))
    result = await extract(
        "https://example.com/article",
        query="zzzqqq xyzzy qwopzx",
    )
    assert result["passages"] == []


async def test_segmentation_splits_long_paragraph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One giant "paragraph" composed of many sentences; one sentence mentions
    # the rare keyword. Make sure it's surfaced and not buried by neighbors.
    long_para = (
        "Lorem ipsum dolor sit amet consectetur adipiscing elit. "
        "Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. "
        "Ut enim ad minim veniam quis nostrud exercitation ullamco laboris. "
        "Duis aute irure dolor in reprehenderit in voluptate velit esse cillum. "
        "Quokka research focuses on tiny marsupials native to a single island. "
        "Excepteur sint occaecat cupidatat non proident sunt in culpa qui officia. "
        "Mollit anim id est laborum sed ut perspiciatis unde omnis iste natus. "
        "Error sit voluptatem accusantium doloremque laudantium totam rem aperiam."
    )
    _patch_fetch(monkeypatch, _success_envelope(markdown=long_para))
    result = await extract(
        "https://example.com/article",
        query="quokka marsupials",
        top=1,
    )
    assert result["passages"], "expected a hit"
    assert "quokka" in result["passages"][0]["text"].lower()


def test_extract_module_exports_expected_symbol() -> None:
    # Belt-and-suspenders: confirms the public function is importable.
    assert callable(extract_mod.extract)
