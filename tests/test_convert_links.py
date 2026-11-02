# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Links in the Markdown must resolve against the page's URL, not its host."""

from __future__ import annotations

import re

import pytest

from vasco.converters import convert

PAGE = "https://docs.example.org/3/library/task.html"
FILLER = (
    "This paragraph is ordinary documentation prose that gives trafilatura enough text to keep. "
    * 4
)


def _page(href: str, head: str = "") -> str:
    return (
        f"<html><head><title>T</title>{head}</head><body><main><article>"
        f"<p>{FILLER}</p><p>{FILLER} See <a href='{href}'>the target</a> here.</p>"
        f"<p>{FILLER}</p></article></main></body></html>"
    )


def _target(markdown: str) -> str:
    match = re.search(r"\[the target\]\(([^)]*)\)", markdown)
    assert match, markdown
    return match.group(1)


@pytest.mark.parametrize(
    "href, expected",
    [
        ("#id14", f"{PAGE}#id14"),
        ("other.html#x", "https://docs.example.org/3/library/other.html#x"),
        ("../glossary.html", "https://docs.example.org/3/glossary.html"),
        ("/root.html", "https://docs.example.org/root.html"),
        ("//cdn.example.net/a", "https://cdn.example.net/a"),
        ("https://elsewhere.example.com/b", "https://elsewhere.example.com/b"),
    ],
)
def test_links_resolve_against_the_page_url(href: str, expected: str) -> None:
    markdown, _ = convert.html_to_markdown(_page(href), url=PAGE)
    assert _target(markdown) == expected


def test_base_tag_overrides_the_page_url() -> None:
    html = _page("x.html", head="<base href='/other/dir/'>")
    markdown, _ = convert.html_to_markdown(html, url=PAGE)
    assert _target(markdown) == "https://docs.example.org/other/dir/x.html"


def test_malformed_href_does_not_lose_the_page() -> None:
    markdown, meta = convert.html_to_markdown(_page("http://[bad/x"), url=PAGE)
    assert "ordinary documentation prose" in markdown
    assert meta["word_count"] > 0


def test_without_a_url_relative_links_stay_relative() -> None:
    markdown, _ = convert.html_to_markdown(_page("other.html"), url="")
    assert _target(markdown) == "other.html"
