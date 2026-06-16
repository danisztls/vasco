from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from vasco.adapters import claude_cli as cc


@pytest.fixture(autouse=True)
def _binary_present(monkeypatch: pytest.MonkeyPatch) -> None:
    # The constructor checks binary_available; pretend the binary exists.
    monkeypatch.setattr(cc, "binary_available", lambda _b: True)


class _FakeProc:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        hang: bool = False,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.killed = False
        self.stdin_input: bytes | None = None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_input = input
        if self._hang:
            await asyncio.sleep(10)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def _patch_exec(monkeypatch: pytest.MonkeyPatch, proc: _FakeProc) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProc:
        captured["args"] = list(args)
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(cc.asyncio, "create_subprocess_exec", fake_exec)
    return captured


def _result_json(text: str) -> bytes:
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": text}
    ).encode()


async def test_success_returns_result(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(stdout=_result_json("  hello  "))
    captured = _patch_exec(monkeypatch, proc)
    client = cc.ClaudeCliClient(binary="claude", model="opus")
    out = await client.complete(system="SYS", user="USER")

    assert out == "hello"  # trimmed
    args = captured["args"]
    assert "--safe-mode" in args
    assert "--no-session-persistence" in args
    assert args[args.index("--system-prompt") + 1] == "SYS"
    assert args[args.index("--tools") + 1] == ""  # remove tool DEFINITIONS
    assert "--disable-slash-commands" in args  # remove slash-command descriptions
    assert (
        "--allowedTools" not in args
    )  # the wrong lever: permission only, schemas ship
    assert args[args.index("--model") + 1] == "opus"
    assert proc.stdin_input == b"USER"  # full prompt on stdin, no prompt arg


async def test_success_populates_last_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(
        stdout=json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "ok",
                "total_cost_usd": 0.0185,
                "usage": {
                    "input_tokens": 2247,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 260,
                },
            }
        ).encode()
    )
    _patch_exec(monkeypatch, proc)
    client = cc.ClaudeCliClient(binary="claude")
    await client.complete(system="S", user="U")
    assert client.last_usage == {
        "input_tokens": 2247,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 260,
        "cost_usd": 0.0185,
    }


def test_usage_from_result_handles_missing_fields() -> None:
    # No usage block / no cost → all-None normalized dict, never KeyErrors.
    assert cc.usage_from_result({}) == {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "cost_usd": None,
    }


async def test_model_omitted_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(stdout=_result_json("ok"))
    captured = _patch_exec(monkeypatch, proc)
    client = cc.ClaudeCliClient(binary="claude", model="")
    await client.complete(system="S", user="U")
    assert "--model" not in captured["args"]


async def test_env_strips_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("PATH", "/usr/bin")
    proc = _FakeProc(stdout=_result_json("ok"))
    captured = _patch_exec(monkeypatch, proc)
    await cc.ClaudeCliClient(binary="claude").complete(system="S", user="U")

    env = captured["kwargs"]["env"]
    assert "ANTHROPIC_API_KEY" not in env  # billed to subscription, not the API
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env.get("PATH") == "/usr/bin"  # the rest of the env is preserved


async def test_is_error_result_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(
        stdout=json.dumps(
            {"subtype": "error_during_execution", "is_error": True, "result": ""}
        ).encode()
    )
    _patch_exec(monkeypatch, proc)
    with pytest.raises(RuntimeError):
        await cc.ClaudeCliClient(binary="claude").complete(system="S", user="U")


async def test_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(stdout=b"", stderr=b"boom", returncode=1)
    _patch_exec(monkeypatch, proc)
    with pytest.raises(RuntimeError):
        await cc.ClaudeCliClient(binary="claude").complete(system="S", user="U")


async def test_timeout_kills_and_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(hang=True)
    _patch_exec(monkeypatch, proc)
    client = cc.ClaudeCliClient(binary="claude", timeout=0.01)
    with pytest.raises(TimeoutError):
        await client.complete(system="S", user="U")
    assert proc.killed is True


def test_missing_binary_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc, "binary_available", lambda _b: False)
    with pytest.raises(FileNotFoundError):
        cc.ClaudeCliClient(binary="/nope/claude")
