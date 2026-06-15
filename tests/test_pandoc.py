"""Tests for the pandoc converter and fetch-pipeline routing."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any
import pytest

from vasco.fetch import browser as browser_mod
from vasco.fetch import core as core_mod
from vasco import fetch as fetch_mod
from vasco.cache import Cache
from vasco.converters import pandoc


# ---------------------------------------------------------------------------
# Unit tests for pandoc.pandoc_to_markdown
# ---------------------------------------------------------------------------


def test_pandoc_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(FileNotFoundError, match="pandoc not found"):
        pandoc.pandoc_to_markdown(b"hello", fmt="rtf")


def test_pandoc_converts_body(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_md = (
        "# Hello World\n\nSome content here that is long enough to pass the short-content check easily with enough words to matter."
        * 3
    )

    def fake_run(cmd, *, capture_output, check, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=fake_md.encode(), stderr=b"")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/pandoc")
    monkeypatch.setattr("subprocess.run", fake_run)

    text, meta = pandoc.pandoc_to_markdown(b"dummy body", fmt="docx")
    assert text == fake_md
    assert meta["word_count"] > 0
    assert "short_content" not in meta["warnings"]


def test_pandoc_nonzero_exit_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, *, capture_output, check, timeout):
        return subprocess.CompletedProcess(cmd, 1, stdout=b"partial", stderr=b"err")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/pandoc")
    monkeypatch.setattr("subprocess.run", fake_run)

    text, meta = pandoc.pandoc_to_markdown(b"body", fmt="epub")
    assert "pandoc_nonzero_exit" in meta["warnings"]
    assert "short_content" in meta["warnings"]


def test_pandoc_short_content_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, *, capture_output, check, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=b"tiny", stderr=b"")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/pandoc")
    monkeypatch.setattr("subprocess.run", fake_run)

    _, meta = pandoc.pandoc_to_markdown(b"body", fmt="odt")
    assert "short_content" in meta["warnings"]


def test_pandoc_metadata_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, *, capture_output, check, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=b"text", stderr=b"")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/pandoc")
    monkeypatch.setattr("subprocess.run", fake_run)

    _, meta = pandoc.pandoc_to_markdown(b"body", fmt="rtf")
    for key in ("title", "byline", "published", "modified", "language", "site_name"):
        assert key in meta
    assert isinstance(meta["word_count"], int)
    assert isinstance(meta["quality"], dict)
    assert isinstance(meta["warnings"], list)


# ---------------------------------------------------------------------------
# Detection function tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.com/report.docx", "docx"),
        ("https://example.com/book.epub", "epub"),
        ("https://example.com/doc.odt", "odt"),
        ("https://example.com/letter.rtf", "rtf"),
        ("https://example.com/page.html", None),
        ("https://example.com/file.pdf", None),
        ("https://example.com/path/no-ext", None),
        ("https://example.com/REPORT.DOCX", "docx"),
    ],
)
def test_pandoc_format_from_url(url: str, expected: str | None) -> None:
    assert fetch_mod._pandoc_format(url, None) == expected


@pytest.mark.parametrize(
    "mime,expected",
    [
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
        ),
        ("application/epub+zip", "epub"),
        ("application/vnd.oasis.opendocument.text", "odt"),
        ("application/rtf", "rtf"),
        ("text/rtf", "rtf"),
        ("text/html", None),
        ("application/pdf", None),
    ],
)
def test_pandoc_format_from_content_type(mime: str, expected: str | None) -> None:
    headers = {"Content-Type": mime}
    result = fetch_mod._pandoc_format("https://example.com/download", headers)
    assert result == expected


# ---------------------------------------------------------------------------
# Fetch pipeline integration tests
# ---------------------------------------------------------------------------


def _disable_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NopPool:
        async def fetch(self, *a: Any, **kw: Any) -> tuple[str, int, dict[str, str]]:
            return "", 0, {}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(browser_mod, "_pool", None, raising=False)
    monkeypatch.setattr(browser_mod, "get_browser", lambda cfg=None: _NopPool())


def _stub_http(html: str, status: int = 200, headers: dict | None = None):
    async def _fake(
        url: str,
        *,
        deadline_monotonic: float,
        cfg: Any | None = None,
        profile: str = "browser",
    ) -> tuple[str, int, dict[str, str]]:
        hdrs = dict(headers or {})
        hdrs.setdefault("_url_final", url)
        return html, status, hdrs

    return _fake


def test_docx_url_routes_to_pandoc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_md = (
        "# Converted\n\nDocument body with enough words to be meaningful content for the test to pass."
        * 3
    )

    def fake_pandoc(body: bytes, *, fmt: str) -> tuple[str, dict]:
        return fake_md, {
            "title": None,
            "byline": None,
            "published": None,
            "modified": None,
            "language": None,
            "site_name": None,
            "word_count": len(fake_md.split()),
            "quality": {},
            "warnings": [],
        }

    monkeypatch.setattr(pandoc, "pandoc_to_markdown", fake_pandoc)
    _disable_browser(monkeypatch)

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"PK\x03\x04fake-docx")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", patched_client)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        result = asyncio.run(
            fetch_mod.fetch_one(
                "https://example.com/report.docx",
                cache=cache,
                deadline=10.0,
            )
        )
        assert result["mode_used"] == "pandoc"
        assert result["markdown"] == fake_md
        assert "failure" not in result
    finally:
        cache.close()


def test_pandoc_redirect_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """URL has no extension, but redirect lands on .epub — should route to pandoc."""
    fake_md = "# Book content with enough words to be meaningful." * 3

    def fake_pandoc(body: bytes, *, fmt: str) -> tuple[str, dict]:
        assert fmt == "epub"
        return fake_md, {
            "title": None,
            "byline": None,
            "published": None,
            "modified": None,
            "language": None,
            "site_name": None,
            "word_count": len(fake_md.split()),
            "quality": {},
            "warnings": [],
        }

    monkeypatch.setattr(pandoc, "pandoc_to_markdown", fake_pandoc)
    _disable_browser(monkeypatch)

    redirect_url = "https://example.com/downloads/book.epub"
    monkeypatch.setattr(
        core_mod,
        "_http_fetch",
        _stub_http("", 200, {"_url_final": redirect_url}),
    )

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        result = asyncio.run(
            fetch_mod.fetch_one(
                "https://example.com/get/12345",
                cache=cache,
                deadline=10.0,
            )
        )
        assert result["mode_used"] == "pandoc"
        assert result["markdown"] == fake_md
    finally:
        cache.close()


def test_pandoc_missing_returns_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_pandoc(body: bytes, *, fmt: str) -> tuple[str, dict]:
        raise FileNotFoundError("pandoc not found on PATH; cannot convert docx")

    monkeypatch.setattr(pandoc, "pandoc_to_markdown", fake_pandoc)
    _disable_browser(monkeypatch)

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"PK\x03\x04fake")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", patched_client)

    cache = Cache(str(tmp_path / "cache.db"))
    try:
        result = asyncio.run(
            fetch_mod.fetch_one(
                "https://example.com/report.docx",
                cache=cache,
                deadline=10.0,
            )
        )
        assert result["failure"]["reason"] == "unsupported_content_type"
    finally:
        cache.close()
