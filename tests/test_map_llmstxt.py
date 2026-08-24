# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for `vasco.map._fetch_llmstxt` — network fetch + on-disk persistence."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from vasco import map as _map


class _FakeResponse:
    def __init__(
        self, status_code: int = 200, text: str = "", content: bytes = b""
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.content = content or text.encode()


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, response: Any) -> MagicMock:
    mock = MagicMock(return_value=response)
    monkeypatch.setattr("vasco.map.httpx.get", mock)
    return mock


def test_fetch_success_returns_content(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(_map, "_llmstxt_dir", lambda: tmp_path)
    body = "# My Site\n- [Docs](https://example.com/docs)\n"
    _patch_httpx(monkeypatch, _FakeResponse(text=body))

    content, url = _map._fetch_llmstxt("https://example.com/some/page")
    assert content == body
    assert url == "https://example.com/llms.txt"


def test_fetch_persists_to_disk(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(_map, "_llmstxt_dir", lambda: tmp_path)
    body = "# Persisted"
    _patch_httpx(monkeypatch, _FakeResponse(text=body))

    _map._fetch_llmstxt("https://example.com/")
    assert (tmp_path / "example.com.txt").read_text() == body


def test_fetch_serves_from_disk_when_fresh(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(_map, "_llmstxt_dir", lambda: tmp_path)
    cached = "# Cached version"
    (tmp_path / "example.com.txt").write_text(cached)

    mock = _patch_httpx(monkeypatch, _FakeResponse(text="# Network version"))
    content, _url = _map._fetch_llmstxt("https://example.com/page")
    assert content == cached
    mock.assert_not_called()


def test_fetch_refetches_when_stale(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(_map, "_llmstxt_dir", lambda: tmp_path)
    cache_path = tmp_path / "example.com.txt"
    cache_path.write_text("# Old")
    stale_time = time.time() - 90000  # > 24h
    import os

    os.utime(cache_path, (stale_time, stale_time))

    fresh = "# Fresh from network"
    _patch_httpx(monkeypatch, _FakeResponse(text=fresh))
    content, _ = _map._fetch_llmstxt("https://example.com/x")
    assert content == fresh


def test_fetch_non_200_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    monkeypatch.setattr(_map, "_llmstxt_dir", lambda: tmp_path)
    _patch_httpx(monkeypatch, _FakeResponse(status_code=404, text="Not Found"))

    content, url = _map._fetch_llmstxt("https://example.com/")
    assert content is None
    assert url is None
    assert "404" in capsys.readouterr().err


def test_fetch_network_error_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    monkeypatch.setattr(_map, "_llmstxt_dir", lambda: tmp_path)
    import httpx

    monkeypatch.setattr(
        "vasco.map.httpx.get", MagicMock(side_effect=httpx.ConnectTimeout("timeout"))
    )

    content, _url = _map._fetch_llmstxt("https://example.com/")
    assert content is None
    assert "timeout" in capsys.readouterr().err


def test_fetch_oversized_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    monkeypatch.setattr(_map, "_llmstxt_dir", lambda: tmp_path)
    big_content = "x" * (512 * 1024 + 1)
    _patch_httpx(monkeypatch, _FakeResponse(text=big_content))

    content, _url = _map._fetch_llmstxt("https://example.com/")
    assert content is None
    assert "too large" in capsys.readouterr().err


def test_fetch_empty_body_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    monkeypatch.setattr(_map, "_llmstxt_dir", lambda: tmp_path)
    _patch_httpx(monkeypatch, _FakeResponse(text="   \n  "))

    content, _url = _map._fetch_llmstxt("https://example.com/")
    assert content is None
    assert "empty" in capsys.readouterr().err


def test_iter_llmstxt_yields_single_record(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(_map, "_llmstxt_dir", lambda: tmp_path)
    body = "# Example\n- [Page](https://example.com/page)"
    _patch_httpx(monkeypatch, _FakeResponse(text=body))

    records = list(_map._iter_llmstxt("https://example.com/"))
    assert len(records) == 1
    assert records[0] == {
        "url": "https://example.com/llms.txt",
        "source": "llmstxt",
        "content": body,
        "lastmod": None,
    }


def test_iter_llmstxt_yields_nothing_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr(_map, "_llmstxt_dir", lambda: tmp_path)
    _patch_httpx(monkeypatch, _FakeResponse(status_code=404, text=""))

    records = list(_map._iter_llmstxt("https://example.com/"))
    assert records == []
