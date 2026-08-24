"""Tests for the human-readable output path (`vasco.io.resolve_human` + `vasco.render`).

Renderers write to an in-memory ``Console`` (``record=True`` so we can capture
plain text without ANSI) and we assert on substrings. The key contract is that
renderers never raise on failure/empty/partial envelopes, and that machine output
is unchanged when piped.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from vasco import render
from vasco.io import resolve_human


def _console() -> Console:
    # record=True lets us pull plain text via export_text(); width fixed for stability.
    return Console(file=io.StringIO(), record=True, width=100, force_terminal=False)


def _text(con: Console) -> str:
    return con.export_text()


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# resolve_human truth table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "human,machine,tty,expected",
    [
        (False, False, False, False),  # piped, no flags → machine
        (False, False, True, True),  # terminal, no flags → human
        (True, False, False, True),  # --human into a pipe → human
        (True, False, True, True),  # --human on terminal → human
        (False, True, False, False),  # --json piped → machine
        (False, True, True, False),  # --json on terminal → machine
        (True, True, False, True),  # both set: --human wins (guard is in CLI)
        (True, True, True, True),
    ],
)
def test_resolve_human(human: bool, machine: bool, tty: bool, expected: bool) -> None:
    stream = _FakeTTY() if tty else io.StringIO()
    assert resolve_human(human, machine, stream) is expected


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def test_render_fetch_page() -> None:
    con = _console()
    env = {
        "title": "Example Domain",
        "url_final": "https://example.com/",
        "mode_used": "http",
        "word_count": 12,
        "quality": {"slop_score": 0.10},
        "markdown": "# Example Domain\n\nThis domain is for **examples**.\n",
    }
    render.render_fetch(env, con)
    out = _text(con)
    assert "Example Domain" in out
    assert "http" in out
    assert "examples" in out


def test_render_fetch_failure_never_raises() -> None:
    con = _console()
    env = {
        "url_requested": "https://nope.example/x",
        "failure": {"reason": "not_found", "message": "404 from upstream"},
    }
    render.render_fetch(env, con)
    out = _text(con)
    assert "not_found" in out
    assert "404 from upstream" in out


def test_render_fetch_listings_table() -> None:
    con = _console()
    env = {
        "mode_used": "olx",
        "quality": {
            "provider": "olx",
            "vertical": "imoveis",
            "page_type": "list",
            "result_count": 1,
            "listings": [
                {
                    "title": "Apartamento Centro",
                    "url": "https://olx.com.br/x",
                    "price": 320000,
                    "neighborhood": "Centro",
                    "municipality": "São Carlos",
                    "uf": "SP",
                    "attributes": {
                        "area": 80,
                        "bedrooms": 2,
                        "bathrooms": 1,
                        "parking": 1,
                    },
                }
            ],
        },
    }
    render.render_fetch(env, con)
    out = _text(con)
    assert "Apartamento Centro" in out
    assert "80m" in out  # specs
    assert "Centro" in out
    assert "olx" in out


def test_render_fetch_products_table() -> None:
    con = _console()
    env = {
        "mode_used": "mercadolivre",
        "quality": {
            "provider": "mercadolivre",
            "page_type": "search",
            "result_count": 1,
            "products": [
                {
                    "title": "Notebook X",
                    "url": "https://ml.com/p",
                    "price": 2999,
                    "brand": "Acme",
                    "rating": 4.5,
                    "review_count": 120,
                }
            ],
        },
    }
    render.render_fetch(env, con)
    out = _text(con)
    assert "Notebook X" in out
    assert "Acme" in out
    assert "4.5" in out


# ---------------------------------------------------------------------------
# search / answer / extract / json / streaming
# ---------------------------------------------------------------------------


def test_render_search_table() -> None:
    con = _console()
    rows = [
        {"title": "First", "url": "https://a.test/1", "snippet": "alpha snippet"},
        {"title": "Second", "url": "https://b.test/2", "snippet": "beta snippet"},
    ]
    render.render_search(rows, con)
    out = _text(con)
    assert "First" in out
    assert "https://b.test/2" in out
    assert "alpha snippet" in out


def test_render_search_empty() -> None:
    con = _console()
    render.render_search([], con)
    assert "no results" in _text(con)


def test_render_answer() -> None:
    con = _console()
    result = {
        "answer": "## Summary\n\nThis is the answer.",
        "model": "deepseek-chat",
        "from_cache": True,
        "url": "https://example.com/",
        "question": "what is this?",
    }
    render.render_answer(result, con)
    out = _text(con)
    assert "This is the answer." in out
    assert "deepseek-chat" in out
    assert "what is this?" in out


def test_render_answer_error_never_raises() -> None:
    con = _console()
    result = {"error": "no_api_key", "message": "No answer API key configured."}
    render.render_answer(result, con)
    assert "no_api_key" in _text(con)


def test_render_extract_passages() -> None:
    con = _console()
    result = {
        "query": "pricing",
        "ranker": "bm25",
        "url": "https://example.com/",
        "passages": [
            {"text": "Cheap plans here.", "score": 1.23},
            {"text": "More pricing detail.", "score": 0.98},
        ],
    }
    render.render_extract(result, con)
    out = _text(con)
    assert "pricing" in out
    assert "Cheap plans here." in out
    assert "1.23" in out


def test_render_extract_empty_never_raises() -> None:
    con = _console()
    render.render_extract({"query": "x", "passages": []}, con)
    assert "no passages" in _text(con)


def test_render_json() -> None:
    con = _console()
    render.render_json({"hits": 3, "ratio": 0.5}, con)
    out = _text(con)
    assert "hits" in out
    assert "3" in out


def test_render_map_streams_and_counts() -> None:
    con = _console()
    records = [
        {"url": "https://s.test/a", "source": "sitemap", "lastmod": None},
        {"url": "https://s.test/b", "source": "feed", "lastmod": "2026-01-01"},
    ]
    count = render.render_map(iter(records), con)
    out = _text(con)
    assert count == 2
    assert "https://s.test/a" in out
    assert "sitemap" in out
    assert "2026-01-01" in out


def test_render_cache_list_empty() -> None:
    con = _console()
    render.render_cache_list(iter([]), con)
    assert "cache is empty" in _text(con)
