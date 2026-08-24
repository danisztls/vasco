from __future__ import annotations

from typing import Any, ClassVar

import pytest

from vasco import summarize as _sum
from vasco.config import AnswerCfg, Config, ProviderCfg


def _chain(*entries: ProviderCfg) -> Config:
    return Config(answer=AnswerCfg(providers=entries))


def _cfg(**kw: Any) -> Config:
    # A ready single-entry deepseek chain; individual tests override fields.
    kw.setdefault("provider", "deepseek")
    kw.setdefault("model", "deepseek-v4-flash")
    kw.setdefault("api_key", "k")
    return _chain(ProviderCfg(**kw))


class _FakeClient:
    """Captures the prompts passed to .complete and returns a canned answer."""

    last: ClassVar[dict[str, Any]] = {}

    def __init__(self, **kw: Any) -> None:
        _FakeClient.last["init"] = kw

    async def complete(self, *, system: str, user: str, timeout: float = 30.0) -> str:
        _FakeClient.last["system"] = system
        _FakeClient.last["user"] = user
        # HTTP providers report tokens but no dollar cost.
        self.last_usage = {"input_tokens": 3, "output_tokens": 2, "cost_usd": None}
        return "THE ANSWER"


class _FakeCliClient:
    """Stand-in for ClaudeCliClient with the same .complete surface."""

    last: ClassVar[dict[str, Any]] = {}

    def __init__(self, **kw: Any) -> None:
        _FakeCliClient.last["init"] = kw

    async def complete(
        self, *, system: str, user: str, timeout: float | None = None
    ) -> str:
        _FakeCliClient.last["system"] = system
        _FakeCliClient.last["user"] = user
        # claude_cli reports a native cost.
        self.last_usage = {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01}
        return "CLI ANSWER"


class _Boom:
    """A client whose .complete always raises (a failing backend)."""

    def __init__(self, **kw: Any) -> None:
        pass

    async def complete(self, **kw: Any) -> str:
        raise RuntimeError("api down")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("VASCO_ANSWER_API_KEY", raising=False)
    _FakeClient.last = {}
    _FakeCliClient.last = {}


async def test_generic_summary_uses_generic_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sum, "DeepSeekClient", _FakeClient)
    out = await _sum.summarize("page body", question=None, cfg=_cfg())
    assert out == "THE ANSWER"
    assert _FakeClient.last["system"] == _sum._GENERIC_SYSTEM
    assert "page body" in _FakeClient.last["user"]


async def test_question_uses_question_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sum, "DeepSeekClient", _FakeClient)
    out = await _sum.summarize("page body", question="what is the price?", cfg=_cfg())
    assert out == "THE ANSWER"
    assert _FakeClient.last["system"] == _sum._QUESTION_SYSTEM
    assert "what is the price?" in _FakeClient.last["user"]


async def test_api_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_sum, "DeepSeekClient", _Boom)
    assert await _sum.summarize("body", cfg=_cfg()) is None


async def test_no_api_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_sum, "DeepSeekClient", _FakeClient)
    cfg = _chain(ProviderCfg(provider="deepseek", model="m", api_key=""))
    assert await _sum.summarize("body", cfg=cfg) is None


async def test_empty_markdown_returns_none() -> None:
    assert await _sum.summarize("", cfg=_cfg()) is None


def test_resolve_api_key_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    pcfg = ProviderCfg(api_key="from-cfg")
    assert _sum.resolve_api_key(pcfg) == "from-env"
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert _sum.resolve_api_key(pcfg) == "from-cfg"


# --- answer() orchestrator ---


def _stub_fetch_one(env: dict[str, Any]):  # type: ignore[no-untyped-def]
    async def _fake(url: str, **kwargs: Any) -> dict[str, Any]:
        return {**env, "url_requested": url}

    return _fake


async def test_answer_returns_answer_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sum, "DeepSeekClient", _FakeClient)
    monkeypatch.setattr(
        _sum._fetch,
        "fetch_one",
        _stub_fetch_one(
            {
                "markdown": "the body",
                "title": "T",
                "url_final": "https://x",
                "word_count": 3,
            }
        ),
    )
    out = await _sum.answer("https://x", question="q?", cfg=_cfg())
    assert out["answer"] == "THE ANSWER"
    assert out["question"] == "q?"
    assert out["title"] == "T"
    assert out["fell_back"] is False
    assert "error" not in out


async def test_answer_propagates_fetch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _sum._fetch,
        "fetch_one",
        _stub_fetch_one({"failure": {"reason": "not_found", "message": "404"}}),
    )
    out = await _sum.answer("https://x", cfg=_cfg())
    assert "failure" in out
    assert out["failure"]["reason"] == "not_found"


async def test_answer_no_key_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_sum._fetch, "fetch_one", _stub_fetch_one({"markdown": "body"}))
    cfg = _chain(ProviderCfg(provider="deepseek", model="m", api_key=""))
    out = await _sum.answer("https://x", cfg=cfg)
    assert out["error"] == "no_api_key"
    assert out["answer"] is None


# --- provider selection: claude_cli + unconfigured ---


async def test_claude_cli_provider_needs_no_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sum, "ClaudeCliClient", _FakeCliClient)
    monkeypatch.setattr(_sum, "binary_available", lambda _b: True)
    cfg = _chain(ProviderCfg(provider="claude_cli", model="opus"))
    out = await _sum.summarize("page body", question="q?", cfg=cfg)
    assert out == "CLI ANSWER"
    assert _FakeCliClient.last["system"] == _sum._QUESTION_SYSTEM
    assert "page body" in _FakeCliClient.last["user"]


async def test_answer_claude_cli_reports_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sum, "ClaudeCliClient", _FakeCliClient)
    monkeypatch.setattr(_sum, "binary_available", lambda _b: True)
    monkeypatch.setattr(_sum._fetch, "fetch_one", _stub_fetch_one({"markdown": "body"}))
    cfg = _chain(ProviderCfg(provider="claude_cli", model="opus"))
    out = await _sum.answer("https://x", cfg=cfg)
    assert out["answer"] == "CLI ANSWER"
    assert out["model"] == "opus"
    assert out["provider"] == "claude_cli"


async def test_answer_claude_cli_missing_binary_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sum, "binary_available", lambda _b: False)
    monkeypatch.setattr(_sum._fetch, "fetch_one", _stub_fetch_one({"markdown": "body"}))
    cfg = _chain(ProviderCfg(provider="claude_cli", model="opus"))
    out = await _sum.answer("https://x", cfg=cfg)
    assert out["error"] == "claude_cli_unavailable"
    assert out["answer"] is None


async def test_answer_unconfigured_provider_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sum._fetch, "fetch_one", _stub_fetch_one({"markdown": "body"}))
    out = await _sum.answer("https://x", cfg=_chain())  # empty chain → disabled
    assert out["error"] == "answer_not_configured"
    assert out["answer"] is None


async def test_answer_deepseek_missing_model_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sum._fetch, "fetch_one", _stub_fetch_one({"markdown": "body"}))
    cfg = _chain(ProviderCfg(provider="deepseek", api_key="k", model=""))
    out = await _sum.answer("https://x", cfg=cfg)
    assert out["error"] == "answer_not_configured"


# --- usage/cost surfaced in the envelope ---


async def test_answer_claude_cli_envelope_carries_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sum, "ClaudeCliClient", _FakeCliClient)
    monkeypatch.setattr(_sum, "binary_available", lambda _b: True)
    monkeypatch.setattr(_sum._fetch, "fetch_one", _stub_fetch_one({"markdown": "body"}))
    cfg = _chain(ProviderCfg(provider="claude_cli", model="opus"))
    out = await _sum.answer("https://x", cfg=cfg)
    assert out["provider"] == "claude_cli"
    assert out["usage"]["input_tokens"] == 10
    assert out["usage"]["cost_usd"] == 0.01  # native cost from the CLI


async def test_answer_deepseek_envelope_usage_has_no_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sum, "DeepSeekClient", _FakeClient)
    monkeypatch.setattr(_sum._fetch, "fetch_one", _stub_fetch_one({"markdown": "body"}))
    out = await _sum.answer("https://x", cfg=_cfg())
    assert out["provider"] == "deepseek"
    assert out["usage"]["output_tokens"] == 2
    assert out["usage"]["cost_usd"] is None  # tokens-only for HTTP providers


async def test_answer_llm_failure_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sum, "DeepSeekClient", _Boom)
    monkeypatch.setattr(_sum._fetch, "fetch_one", _stub_fetch_one({"markdown": "body"}))
    out = await _sum.answer("https://x", cfg=_cfg())
    assert out["error"] == "answer_failed"
    assert out["answer"] is None


# --- fallback chain ---


async def test_fallback_serves_when_primary_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # primary claude_cli raises → deepseek fallback serves the answer
    monkeypatch.setattr(_sum, "ClaudeCliClient", _Boom)
    monkeypatch.setattr(_sum, "DeepSeekClient", _FakeClient)
    monkeypatch.setattr(_sum, "binary_available", lambda _b: True)
    monkeypatch.setattr(_sum._fetch, "fetch_one", _stub_fetch_one({"markdown": "body"}))
    cfg = _chain(
        ProviderCfg(provider="claude_cli", model="opus"),
        ProviderCfg(provider="deepseek", model="ds", api_key="k"),
    )
    out = await _sum.answer("https://x", cfg=cfg)
    assert out["answer"] == "THE ANSWER"
    assert out["provider"] == "deepseek"
    assert out["model"] == "ds"
    assert out["fell_back"] is True


async def test_fallback_when_primary_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # claude_cli binary missing (not ready) → deepseek fallback serves
    monkeypatch.setattr(_sum, "DeepSeekClient", _FakeClient)
    monkeypatch.setattr(_sum, "binary_available", lambda _b: False)
    monkeypatch.setattr(_sum._fetch, "fetch_one", _stub_fetch_one({"markdown": "body"}))
    cfg = _chain(
        ProviderCfg(provider="claude_cli", model="opus"),
        ProviderCfg(provider="deepseek", model="ds", api_key="k"),
    )
    out = await _sum.answer("https://x", cfg=cfg)
    assert out["provider"] == "deepseek"
    assert out["fell_back"] is True


async def test_primary_serves_without_fallback_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sum, "DeepSeekClient", _FakeClient)
    monkeypatch.setattr(_sum, "ClaudeCliClient", _Boom)
    monkeypatch.setattr(_sum, "binary_available", lambda _b: True)
    monkeypatch.setattr(_sum._fetch, "fetch_one", _stub_fetch_one({"markdown": "body"}))
    cfg = _chain(
        ProviderCfg(provider="deepseek", model="ds", api_key="k"),
        ProviderCfg(provider="claude_cli", model="opus"),
    )
    out = await _sum.answer("https://x", cfg=cfg)
    assert out["provider"] == "deepseek"
    assert out["fell_back"] is False


async def test_all_providers_fail_returns_answer_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sum, "DeepSeekClient", _Boom)
    monkeypatch.setattr(_sum, "ClaudeCliClient", _Boom)
    monkeypatch.setattr(_sum, "binary_available", lambda _b: True)
    monkeypatch.setattr(_sum._fetch, "fetch_one", _stub_fetch_one({"markdown": "body"}))
    cfg = _chain(
        ProviderCfg(provider="claude_cli", model="opus"),
        ProviderCfg(provider="deepseek", model="ds", api_key="k"),
    )
    out = await _sum.answer("https://x", cfg=cfg)
    assert out["error"] == "answer_failed"
    assert out["answer"] is None
