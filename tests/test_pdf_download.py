# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A .pdf URL that doesn't serve a PDF must fail honestly, not cache an empty success."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from vasco import fetch as fetch_mod
from vasco.cache import Cache
from vasco.converters import pdf
from vasco.fetch import documents


def _serve(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real_client = httpx.AsyncClient

    def patched_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(documents.httpx, "AsyncClient", patched_client)


def _fetch(url: str, tmp_path: Path) -> dict[str, Any]:
    cache = Cache(str(tmp_path / "cache.db"))
    try:
        return asyncio.run(fetch_mod.fetch_one(url, cache=cache, deadline=10.0))
    finally:
        cache.close()


@pytest.mark.parametrize(
    "status, reason",
    [
        (403, "blocked_cloudflare"),
        (404, "not_found"),
        (429, "server_error"),
        (503, "server_error"),
    ],
)
def test_error_status_is_a_failure(
    status: int,
    reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(
        monkeypatch,
        lambda request: httpx.Response(status, html="<html>nope</html>"),
    )
    env = _fetch("https://example.com/r.pdf", tmp_path)
    assert env["failure"]["reason"] == reason
    assert env["http_status"] == status
    assert env["mode_used"] == "pdf"


def test_html_at_pdf_url_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(200, html="<p>hello</p>"))
    env = _fetch("https://example.com/r.pdf", tmp_path)
    assert env["failure"]["reason"] == "unsupported_content_type"
    assert "not a PDF" in env["failure"]["message"]


def test_real_pdf_still_converts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(200, content=b"%PDF-1.7 body"))
    monkeypatch.setattr(
        pdf,
        "pdf_to_text",
        lambda body, **kw: ("Some text. " * 40, {"word_count": 80, "quality": {}}),
    )
    env = _fetch("https://example.com/r.pdf", tmp_path)
    assert "failure" not in env
    assert env["word_count"] == 80


def test_timeout_is_not_reported_as_dns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _serve(monkeypatch, handler)
    env = _fetch("https://example.com/r.pdf", tmp_path)
    assert env["failure"]["reason"] == "timeout"
