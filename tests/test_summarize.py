from __future__ import annotations

from typing import Any

import pytest

from vasco import summarize as _sum
from vasco.config import AnswerCfg, Config


def _cfg(**kw: Any) -> Config:
    return Config(answer=AnswerCfg(api_key="k", **kw))


class _FakeClient:
    """Captures the prompts passed to .complete and returns a canned answer."""

    last: dict[str, Any] = {}

    def __init__(self, **kw: Any) -> None:
        _FakeClient.last["init"] = kw

    async def complete(self, *, system: str, user: str, timeout: float = 30.0) -> str:
        _FakeClient.last["system"] = system
        _FakeClient.last["user"] = user
        return "THE ANSWER"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("VASCO_ANSWER_API_KEY", raising=False)
    _FakeClient.last = {}


async def test_generic_summary_uses_generic_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sum, "DeepSeekClient", _FakeClient)
    out = await _sum.summarize("page body", question=None, cfg=_cfg())
    assert out == "THE ANSWER"
    assert _sum._GENERIC_SYSTEM == _FakeClient.last["system"]
    assert "page body" in _FakeClient.last["user"]


async def test_question_uses_question_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sum, "DeepSeekClient", _FakeClient)
    out = await _sum.summarize("page body", question="what is the price?", cfg=_cfg())
    assert out == "THE ANSWER"
    assert _sum._QUESTION_SYSTEM == _FakeClient.last["system"]
    assert "what is the price?" in _FakeClient.last["user"]


async def test_api_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def __init__(self, **kw: Any) -> None:
            pass

        async def complete(self, **kw: Any) -> str:
            raise RuntimeError("api down")

    monkeypatch.setattr(_sum, "DeepSeekClient", _Boom)
    assert await _sum.summarize("body", cfg=_cfg()) is None


async def test_no_api_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_sum, "DeepSeekClient", _FakeClient)
    cfg = Config(answer=AnswerCfg(api_key=""))
    assert await _sum.summarize("body", cfg=cfg) is None


async def test_empty_markdown_returns_none() -> None:
    assert await _sum.summarize("", cfg=_cfg()) is None


def test_resolve_api_key_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    cfg = Config(answer=AnswerCfg(api_key="from-cfg"))
    assert _sum.resolve_api_key(cfg) == "from-env"
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert _sum.resolve_api_key(cfg) == "from-cfg"


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
    cfg = Config(answer=AnswerCfg(api_key=""))
    out = await _sum.answer("https://x", cfg=cfg)
    assert out["error"] == "no_api_key"
    assert out["answer"] is None


async def test_answer_llm_failure_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Boom:
        def __init__(self, **kw: Any) -> None:
            pass

        async def complete(self, **kw: Any) -> str:
            raise RuntimeError("api down")

    monkeypatch.setattr(_sum, "DeepSeekClient", _Boom)
    monkeypatch.setattr(_sum._fetch, "fetch_one", _stub_fetch_one({"markdown": "body"}))
    out = await _sum.answer("https://x", cfg=_cfg())
    assert out["error"] == "answer_failed"
    assert out["answer"] is None
