"""Focused tests for vasco.fetch internals.

Cross-module behavior lives in test_fetch_integration.py — this file is for
single-function assertions on _http_fetch and friends.
"""

from __future__ import annotations

import time

import httpx
import pytest

from vasco import fetch as fetch_mod


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
        seen.update({k: v for k, v in request.headers.items()})
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


@pytest.mark.asyncio
async def test_http_fetch_user_agent_from_cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k: v for k, v in request.headers.items()})
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", patched_client)

    class _Cfg:
        class fetch:  # noqa: N801 — matches dataclass attribute name
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
