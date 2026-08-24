# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Focused tests for vasco.fetch internals.

Cross-module behavior lives in test_fetch_integration.py — this file is for
single-function assertions on _http_fetch and friends.
"""

from __future__ import annotations

import time

import httpx
import pytest

from vasco import fetch as fetch_mod
from vasco.fetch.urlutils import _looks_binary


@pytest.mark.asyncio
async def test_http_fetch_sends_browser_shaped_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP tier must look like a real browser, not a stripped bot UA.

    Any one of the Sec-Fetch-* headers being absent is itself a signal that
    upstream WAFs use to short-circuit before we even reach the browser tier.
    We assert the load-bearing fingerprint of the request rather than the
    full dict — keep that brittleness for the regression check, not the spec.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers.items()))
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)

    real_async_client = httpx.AsyncClient

    def patched_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", patched_client)

    deadline = time.monotonic() + 5.0
    await fetch_mod._http_fetch("https://example.com/", deadline_monotonic=deadline)

    assert seen["sec-fetch-mode"] == "navigate"
    assert seen["sec-fetch-dest"] == "document"
    assert seen["sec-fetch-site"] == "none"
    assert seen["accept-language"].startswith("en-US")
    # The default UA must still flow through; cfg=None means we keep the
    # built-in placeholder.
    assert "vasco" in seen["user-agent"].lower()
    # Accept-Encoding must only advertise what we can decode (see below).
    assert seen["accept-encoding"] == fetch_mod._ACCEPT_ENCODING


def test_accept_encoding_only_advertises_decodable() -> None:
    """Regression: advertising zstd/br without the decoder package makes httpx
    return undecoded bytes, silently corrupting .text → empty extraction.

    gzip+deflate are always safe (stdlib zlib); br/zstd only when their
    packages are importable.
    """
    import importlib.util as _ilu

    real = _ilu.find_spec

    def fake(name: str, *a: object, **k: object):  # type: ignore[no-untyped-def]
        if name in ("brotli", "brotlicffi", "zstandard"):
            return None
        return real(name, *a, **k)

    # No optional decoders available → only gzip/deflate.
    import importlib

    monkey = pytest.MonkeyPatch()
    monkey.setattr(importlib.util, "find_spec", fake)
    try:
        assert fetch_mod._supported_accept_encoding() == "gzip, deflate"
    finally:
        monkey.undo()

    # With the decoders installed (declared deps), the full Chrome set appears.
    enc = fetch_mod._supported_accept_encoding()
    assert enc.startswith("gzip, deflate")
    assert "zstd" in enc  # zstandard is a declared dependency


@pytest.mark.asyncio
async def test_http_fetch_user_agent_from_cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers.items()))
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", patched_client)

    class _Cfg:
        class fetch:
            user_agent = "MyCustomUA/9.9"

    deadline = time.monotonic() + 5.0
    await fetch_mod._http_fetch(
        "https://example.com/", deadline_monotonic=deadline, cfg=_Cfg
    )

    assert seen["user-agent"] == "MyCustomUA/9.9"
    # Other browser-shape headers must still be present.
    assert "sec-fetch-mode" in seen


@pytest.mark.asyncio
async def test_http_fetch_returns_timeout_sentinel_when_deadline_passed() -> None:
    # Sanity check that the headers change didn't break the timeout short-circuit.
    text, status, hdrs = await fetch_mod._http_fetch(
        "https://example.com/", deadline_monotonic=time.monotonic() - 1.0
    )
    assert text == ""
    assert status == 0
    assert hdrs.get("_failure_hint") == "timeout"


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def patched(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", patched)


class _ExplodingStream(httpx.AsyncByteStream):
    """A response body that fails the test if anything tries to download it."""

    async def __aiter__(self):
        raise AssertionError("body must not be downloaded for a header-binary")
        yield b""  # pragma: no cover — unreachable, makes this an async generator

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_http_fetch_skips_body_for_header_detected_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A binary blob recognized from its Content-Type header is rejected WITHOUT
    downloading the body — answering 'do we fetch the whole file?' with 'no'."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png", "content-length": "9999999"},
            stream=_ExplodingStream(),
        )

    _patch_transport(monkeypatch, handler)

    deadline = time.monotonic() + 5.0
    text, status, hdrs = await fetch_mod._http_fetch(
        "https://cdn.example/big.png", deadline_monotonic=deadline
    )
    # Empty body returned (never read), but the headers came back for classifying.
    assert text == ""
    assert status == 200
    assert hdrs["content-type"] == "image/png"


@pytest.mark.asyncio
async def test_http_fetch_octet_stream_binary_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An octet-stream whose body looks binary comes back as the sniffed prefix,
    so the chain rejects it as UNSUPPORTED_CONTENT_TYPE downstream."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=b"\x00\x01\x02\x03\x04binary\x00\x00bytes",
        )

    _patch_transport(monkeypatch, handler)
    text, status, _ = await fetch_mod._http_fetch(
        "https://x.example/blob", deadline_monotonic=time.monotonic() + 5.0
    )
    assert status == 200
    assert _looks_binary(text) is True


@pytest.mark.asyncio
async def test_http_fetch_octet_stream_text_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An octet-stream that's actually text (mislabeled) is read in full."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=b"This is plain text mislabeled as octet-stream.\n" * 4,
        )

    _patch_transport(monkeypatch, handler)
    text, _, _ = await fetch_mod._http_fetch(
        "https://x.example/readme", deadline_monotonic=time.monotonic() + 5.0
    )
    assert "mislabeled as octet-stream" in text
    assert _looks_binary(text) is False
