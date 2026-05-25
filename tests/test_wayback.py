"""Unit tests for the Wayback snapshot discovery helpers.

`find_snapshot` is exercised against stubbed httpx clients so no real
network call is made.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from vasco.adapters import wayback


# -----------------------------------------------------------------------------
# Pure helpers (no I/O)
# -----------------------------------------------------------------------------


def test_inject_if_modifier_basic() -> None:
    url = "https://web.archive.org/web/20240501123045/https://example.com/foo"
    out = wayback._inject_if_modifier(url)
    assert (
        out == "https://web.archive.org/web/20240501123045if_/https://example.com/foo"
    )


def test_inject_if_modifier_idempotent_when_modifier_already_present() -> None:
    # Already has `if_`: leave alone.
    url = "https://web.archive.org/web/20240501123045if_/https://example.com/"
    assert wayback._inject_if_modifier(url) == url
    # Already has `id_`: also leave alone.
    url2 = "https://web.archive.org/web/20240501123045id_/https://example.com/"
    assert wayback._inject_if_modifier(url2) == url2


def test_inject_if_modifier_non_matching_input() -> None:
    assert wayback._inject_if_modifier("https://example.com/") == "https://example.com/"
    # Missing slash after timestamp — malformed, leave alone.
    bare = "https://web.archive.org/web/20240501123045"
    assert wayback._inject_if_modifier(bare) == bare


def test_normalize_scheme_upgrades_http_archive() -> None:
    out = wayback._normalize_scheme(
        "http://web.archive.org/web/20240501123045/https://example.com/"
    )
    assert out.startswith("https://web.archive.org/")


# -----------------------------------------------------------------------------
# find_snapshot — happy paths and failure modes
# -----------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Minimal stand-in for httpx.AsyncClient that returns canned responses."""

    def __init__(
        self, *, response: _FakeResponse | None = None, raises: Exception | None = None
    ) -> None:
        self._response = response
        self._raises = raises
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def get(self, url: str, params: dict | None = None) -> _FakeResponse:
        self.calls.append((url, params or {}))
        if self._raises is not None:
            raise self._raises
        assert self._response is not None
        return self._response


def _install_fake_httpx(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    """Replace httpx.AsyncClient with a callable returning our fake client."""

    class _Factory:
        def __call__(self, *a: Any, **kw: Any) -> _FakeClient:
            return client

    fake_module = type("FakeHttpx", (), {"AsyncClient": _Factory()})
    monkeypatch.setattr(wayback, "httpx", fake_module)


def test_find_snapshot_returns_url_with_if_modifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "archived_snapshots": {
            "closest": {
                "available": True,
                "status": "200",
                "timestamp": "20240501123045",
                "url": "https://web.archive.org/web/20240501123045/https://example.com/",
            }
        }
    }
    client = _FakeClient(response=_FakeResponse(status_code=200, payload=payload))
    _install_fake_httpx(monkeypatch, client)

    deadline = time.monotonic() + 30.0
    out = asyncio.run(
        wayback.find_snapshot("https://example.com/", deadline_monotonic=deadline)
    )
    assert out == "https://web.archive.org/web/20240501123045if_/https://example.com/"
    # The API call should include the requested URL.
    assert len(client.calls) == 1
    assert client.calls[0][1]["url"] == "https://example.com/"


def test_find_snapshot_none_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"archived_snapshots": {}}  # no closest at all
    client = _FakeClient(response=_FakeResponse(status_code=200, payload=payload))
    _install_fake_httpx(monkeypatch, client)

    deadline = time.monotonic() + 30.0
    out = asyncio.run(
        wayback.find_snapshot("https://example.com/", deadline_monotonic=deadline)
    )
    assert out is None


def test_find_snapshot_none_when_available_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"archived_snapshots": {"closest": {"available": False}}}
    client = _FakeClient(response=_FakeResponse(status_code=200, payload=payload))
    _install_fake_httpx(monkeypatch, client)

    deadline = time.monotonic() + 30.0
    out = asyncio.run(
        wayback.find_snapshot("https://example.com/", deadline_monotonic=deadline)
    )
    assert out is None


def test_find_snapshot_rejects_non_2xx_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the snapshot itself captured a 4xx/5xx, no point recovering it."""
    payload = {
        "archived_snapshots": {
            "closest": {
                "available": True,
                "status": "404",
                "timestamp": "20240501123045",
                "url": "https://web.archive.org/web/20240501123045/https://example.com/",
            }
        }
    }
    client = _FakeClient(response=_FakeResponse(status_code=200, payload=payload))
    _install_fake_httpx(monkeypatch, client)

    deadline = time.monotonic() + 30.0
    out = asyncio.run(
        wayback.find_snapshot("https://example.com/", deadline_monotonic=deadline)
    )
    assert out is None


def test_find_snapshot_swallows_network_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(raises=RuntimeError("boom"))
    _install_fake_httpx(monkeypatch, client)

    deadline = time.monotonic() + 30.0
    out = asyncio.run(
        wayback.find_snapshot("https://example.com/", deadline_monotonic=deadline)
    )
    assert out is None


def test_find_snapshot_skips_retry_on_authoritative_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 with empty archived_snapshots is final — don't try the
    trailing-slash variant."""
    payload = {"archived_snapshots": {}}
    client = _FakeClient(response=_FakeResponse(status_code=200, payload=payload))
    _install_fake_httpx(monkeypatch, client)

    deadline = time.monotonic() + 30.0
    out = asyncio.run(
        wayback.find_snapshot("https://example.com/foo", deadline_monotonic=deadline)
    )
    assert out is None
    assert len(client.calls) == 1  # no trailing-slash retry


def test_find_snapshot_retries_on_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-200 / exception on the first variant should still trigger a retry."""

    class _FlakyClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []
            self._first = True

        async def __aenter__(self) -> "_FlakyClient":
            return self

        async def __aexit__(self, *a: Any) -> None: ...

        async def get(self, url: str, params: dict | None = None):
            self.calls.append((url, params or {}))
            if self._first:
                self._first = False
                return _FakeResponse(status_code=503, payload={})
            return _FakeResponse(
                status_code=200,
                payload={
                    "archived_snapshots": {
                        "closest": {
                            "available": True,
                            "status": "200",
                            "url": "https://web.archive.org/web/20240501/https://example.com/foo",
                        }
                    }
                },
            )

    client = _FlakyClient()

    class _Factory:
        def __call__(self, *a: Any, **kw: Any) -> _FlakyClient:
            return client

    fake_module = type("FakeHttpx", (), {"AsyncClient": _Factory()})
    monkeypatch.setattr(wayback, "httpx", fake_module)

    deadline = time.monotonic() + 30.0
    out = asyncio.run(
        wayback.find_snapshot("https://example.com/foo", deadline_monotonic=deadline)
    )
    assert out is not None
    assert len(client.calls) == 2  # retry happened


def test_find_snapshot_none_when_deadline_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even with a "working" client, an elapsed deadline should short-circuit.
    payload = {
        "archived_snapshots": {
            "closest": {
                "available": True,
                "status": "200",
                "url": "https://web.archive.org/web/20240501/https://example.com/",
            }
        }
    }
    client = _FakeClient(response=_FakeResponse(status_code=200, payload=payload))
    _install_fake_httpx(monkeypatch, client)

    out = asyncio.run(
        wayback.find_snapshot(
            "https://example.com/",
            deadline_monotonic=time.monotonic() - 1.0,
        )
    )
    assert out is None
    assert client.calls == []  # never made the request
