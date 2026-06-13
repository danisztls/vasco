"""HTML → Markdown conversion via trafilatura + lightweight link/metadata extraction.

Best-effort heuristics:
- `trafilatura_confidence` is approximated as `min(1.0, word_count / 800.0)`.
- `boilerplate_ratio` is `1 - extracted_chars / total_visible_html_chars`.
Both are useful relative signals, not absolute measurements.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

# trafilatura is imported lazily: it pulls htmldate → dateparser, whose
# timezone-parser build costs ~430ms at import time. Deferring it to the first
# `html_to_markdown` call keeps importing this module (and the whole fetch
# stack) cheap, so cache-hit / PDF fetches that never convert pay nothing.
_UNSET: Any = object()
trafilatura: Any = _UNSET
extract_metadata: Any = _UNSET


def _ensure_trafilatura() -> None:
    """Resolve the trafilatura globals on first use; set them to None if the
    optional dep is missing (graceful-degradation path below)."""
    global trafilatura, extract_metadata
    if trafilatura is _UNSET:
        try:  # pragma: no cover - trafilatura is an optional dep at import time.
            import trafilatura as _t
            from trafilatura.metadata import extract_metadata as _em

            trafilatura = _t
            extract_metadata = _em
        except Exception:  # pragma: no cover
            trafilatura = None
            extract_metadata = None


_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _visible_html_chars(html: str) -> int:
    stripped = _SCRIPT_STYLE_RE.sub(" ", html)
    stripped = _TAG_RE.sub(" ", stripped)
    return len(_WS_RE.sub(" ", stripped).strip())


class _LinkParser(HTMLParser):
    """Collect <a href> tags with their anchor text and rel attribute."""

    def __init__(self, base_url: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[dict[str, str | None]] = []
        self._current: dict[str, str | None] | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrd = {k.lower(): v for k, v in attrs}
        href = attrd.get("href")
        if not href:
            return
        rel = attrd.get("rel")
        if self.base_url:
            try:
                href = urljoin(self.base_url, href)
            except Exception:
                pass
        self._current = {"url": href, "anchor": "", "rel": rel}
        self._buf = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._current is None:
            return
        anchor = _WS_RE.sub(" ", "".join(self._buf)).strip()
        self._current["anchor"] = anchor
        self.links.append(self._current)
        self._current = None
        self._buf = []


def _extract_links(html: str, base_url: str | None) -> list[dict[str, str | None]]:
    parser = _LinkParser(base_url)
    try:
        parser.feed(html)
    except Exception:
        # HTMLParser is forgiving but a corrupt input shouldn't kill us.
        pass
    # De-dupe while preserving order.
    seen: set[str] = set()
    out: list[dict[str, str | None]] = []
    for link in parser.links:
        key = str(link.get("url"))
        if key in seen:
            continue
        seen.add(key)
        out.append(link)
    return out


def _word_count(text: str) -> int:
    if not text:
        return 0
    return len(text.split())


def text_to_markdown(text: str, *, content_type: str | None = None) -> tuple[str, dict]:
    """Pass a plain-text / Markdown body through verbatim, with the same
    metadata shape `html_to_markdown` returns.

    trafilatura is an HTML *article* extractor: given structureless plain text
    (a raw ``.md`` / ``.txt`` / RFC / ``LICENSE``) it has no DOM to walk and
    discards everything → empty result. Such a body is already human-readable,
    so it becomes the envelope Markdown unchanged — no extraction, nothing
    stripped (`boilerplate_ratio` is 0.0). ``content_type`` is informational
    (recorded in the warning) and not used to mutate the body.
    """
    body = text or ""
    wc = _word_count(body)
    warnings: list[str] = ["plaintext_passthrough"]
    if len(body.strip()) < 200:
        warnings.append("short_content")
    metadata = {
        "title": None,
        "byline": None,
        "published": None,
        "modified": None,
        "language": None,
        "site_name": None,
        "image": None,
        "word_count": wc,
        "links": [],
        "quality": {
            "trafilatura_confidence": round(min(1.0, wc / 800.0), 4),
            "boilerplate_ratio": 0.0,
        },
        "warnings": warnings,
    }
    return body, metadata


def html_to_markdown(html: str, *, url: str | None = None) -> tuple[str, dict]:
    """Convert HTML to Markdown via trafilatura and return (markdown, metadata).

    Metadata keys: title, byline, published, modified, language, site_name,
    word_count, links, quality {trafilatura_confidence, boilerplate_ratio},
    warnings.
    """
    warnings: list[str] = []

    _ensure_trafilatura()
    if trafilatura is None:
        # Dependency missing — surface a graceful empty result.
        warnings.append("trafilatura_unavailable")
        meta = {
            "title": None,
            "byline": None,
            "published": None,
            "modified": None,
            "language": None,
            "site_name": None,
            "image": None,
            "word_count": 0,
            "links": _extract_links(html, url),
            "quality": {"trafilatura_confidence": 0.0, "boilerplate_ratio": 1.0},
            "warnings": warnings,
        }
        return "", meta

    markdown: str = ""
    try:
        extracted = trafilatura.extract(
            html,
            output_format="markdown",
            with_metadata=True,
            include_links=True,
            include_comments=False,
            url=url,
        )
        if extracted:
            markdown = extracted
    except Exception:
        warnings.append("trafilatura_extract_failed")

    title = byline = published = modified = language = site_name = image = None
    try:
        if extract_metadata is not None:
            md = extract_metadata(html)
            if md is not None:
                title = getattr(md, "title", None)
                byline = getattr(md, "author", None)
                published = getattr(md, "date", None)
                # trafilatura doesn't always expose a separate "modified" field;
                # fall back to None.
                modified = getattr(md, "modified", None) or getattr(
                    md, "modifieddate", None
                )
                language = getattr(md, "language", None)
                site_name = getattr(md, "sitename", None) or getattr(
                    md, "site_name", None
                )
                image = getattr(md, "image", None)
    except Exception:
        warnings.append("trafilatura_metadata_failed")

    wc = _word_count(markdown)
    extracted_chars = len(markdown)
    visible_total = max(1, _visible_html_chars(html))
    boilerplate_ratio = max(
        0.0,
        min(1.0, 1.0 - (extracted_chars / visible_total)),
    )
    confidence = min(1.0, wc / 800.0)

    if extracted_chars < 200:
        warnings.append("short_content")

    metadata = {
        "title": title,
        "byline": byline,
        "published": published,
        "modified": modified,
        "language": language,
        "site_name": site_name,
        "image": image,
        "word_count": wc,
        "links": _extract_links(html, url),
        "quality": {
            "trafilatura_confidence": round(confidence, 4),
            "boilerplate_ratio": round(boilerplate_ratio, 4),
        },
        "warnings": warnings,
    }
    return markdown, metadata
