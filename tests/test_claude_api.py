# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest

from vasco.adapters import claude_api as ca


def _message(**kw: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "model": "claude-haiku-5-5",
        "stop_reason": "end_turn",
        "stop_details": None,
        "content": [
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text="  the answer\n"),
        ],
        "usage": SimpleNamespace(
            input_tokens=11,
            output_tokens=7,
            cache_read_input_tokens=None,
            cache_creation_input_tokens=None,
        ),
    }
    return SimpleNamespace(**{**defaults, **kw})


class _Stream:
    def __init__(self, message: Any, hang: bool = False) -> None:
        self._message = message
        self._hang = hang

    async def __aenter__(self) -> _Stream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get_final_message(self) -> Any:
        if self._hang:
            await asyncio.sleep(10)
        return self._message


def _patch_sdk(
    monkeypatch: pytest.MonkeyPatch, message: Any, hang: bool = False
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def _endpoint(name: str) -> Any:
        def stream(**kw: Any) -> _Stream:
            captured["endpoint"] = name
            captured["params"] = kw
            return _Stream(message, hang)

        return stream

    class _FakeAnthropic:
        def __init__(self, **kw: Any) -> None:
            captured["init"] = kw
            self.messages = SimpleNamespace(stream=_endpoint("messages"))
            self.beta = SimpleNamespace(
                messages=SimpleNamespace(stream=_endpoint("beta"))
            )

        async def __aenter__(self) -> _FakeAnthropic:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAnthropic)
    return captured


async def test_haiku_alias_request_shape_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_sdk(monkeypatch, _message())
    client = ca.ClaudeApiClient(api_key="k", model="haiku", effort="low")
    assert await client.complete(system="S", user="U") == "the answer"

    assert captured["init"] == {"api_key": "k"}
    # Haiku has no server-side fallback: plain endpoint, no `fallbacks`.
    assert captured["endpoint"] == "messages"
    assert captured["params"] == {
        "model": "claude-haiku-5-5",
        "max_tokens": ca._MAX_TOKENS,
        "system": "S",
        "messages": [{"role": "user", "content": "U"}],
        "output_config": {"effort": "low"},
    }
    assert client.last_model == "claude-haiku-5-5"
    assert client.last_usage == {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "cost_usd": None,
    }


@pytest.mark.parametrize("model", ["sonnet", "opus", "fable", "claude-opus-5"])
async def test_fallback_models_use_the_beta_endpoint(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    captured = _patch_sdk(monkeypatch, _message())
    await ca.ClaudeApiClient(api_key="k", model=model).complete(system="S", user="U")
    assert captured["endpoint"] == "beta"
    assert captured["params"]["fallbacks"] == "default"
    assert captured["params"]["betas"] == [ca._SERVER_FALLBACK_BETA]
    assert "output_config" not in captured["params"]  # no effort → model's default


def test_unknown_model_id_passes_through() -> None:
    assert ca.resolve_model("claude-opus-4-8") == "claude-opus-4-8"


async def test_refusal_raises_with_category(monkeypatch: pytest.MonkeyPatch) -> None:
    message = _message(
        stop_reason="refusal",
        stop_details=SimpleNamespace(category="cyber"),
        content=[],
    )
    _patch_sdk(monkeypatch, message)
    client = ca.ClaudeApiClient(api_key="k", model="haiku")
    with pytest.raises(RuntimeError, match="cyber"):
        await client.complete(system="S", user="U")


async def test_truncated_answer_raises_rather_than_returning_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sdk(monkeypatch, _message(stop_reason="max_tokens"))
    client = ca.ClaudeApiClient(api_key="k", model="haiku")
    with pytest.raises(RuntimeError, match="truncated"):
        await client.complete(system="S", user="U")


async def test_timeout_bounds_the_whole_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sdk(monkeypatch, _message(), hang=True)
    client = ca.ClaudeApiClient(api_key="k", model="haiku")
    with pytest.raises(TimeoutError):
        await client.complete(system="S", user="U", timeout=0.01)


def test_missing_key_raises() -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        ca.ClaudeApiClient(api_key="", model="haiku")
