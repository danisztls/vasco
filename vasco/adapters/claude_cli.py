"""`claude -p` answer backend — shell out to the Claude Code CLI (print mode).

An alternative to the OpenAI-compatible `DeepSeekClient` for the `answer`
command. Its point is auth: it runs on the user's Claude Code **subscription**
(OAuth) rather than a per-token API key, so `answer` can use a stronger model at
no marginal cost.

`ClaudeCliClient.complete` mirrors `DeepSeekClient.complete` so `summarize()`
treats the two backends symmetrically. It never returns partial junk: on any
failure (missing binary, non-zero exit, error result, timeout) it raises, and the
caller falls back to a clean ``None``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

# Flags that turn `claude -p` into a clean, single-shot completion. All three
# are independent and all required — none subsumes another:
#   --safe-mode             strips CLAUDE.md/memory, MCP servers, hooks, skills
#                           and plugins **while keeping OAuth** (unlike --bare,
#                           which forces an API key). Does NOT strip tool
#                           definitions or slash commands.
#   --no-session-persistence  don't write the session to disk.
#   --tools ""              removes the ~29 built-in tool DEFINITIONS. This is the
#                           real lever: --allowedTools "" only denies *permission*
#                           to call tools — their schemas still ship (~11.7K input
#                           tokens of agentic-coding context the answer/summarize
#                           task never needs, framing the model as a shell-running
#                           coder). --tools "" drops the per-call overhead to ~160.
#   --disable-slash-commands  removes the ~27 slash-command descriptions.
_HERMETIC_FLAGS: tuple[str, ...] = (
    "--safe-mode",
    "--no-session-persistence",
    "--tools",
    "",
    "--disable-slash-commands",
)

# Removed from the child environment so `claude` authenticates via the Max
# subscription OAuth credential. A stray key here would silently bill the API
# instead of the subscription — the exact thing this backend exists to avoid.
_AUTH_ENV_STRIP: tuple[str, ...] = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# The CLI is resolved on PATH (must include the vascod service's PATH). Not
# configurable — install `claude` somewhere on PATH or symlink it.
DEFAULT_BINARY = "claude"
# `claude -p` is slower than an HTTP call (process spawn + model latency over a
# possibly large page), so bound the subprocess generously. The `answer` deadline
# only covers the page fetch, not this call, so this is the real upper bound.
DEFAULT_TIMEOUT = 120.0


def binary_available(binary: str) -> bool:
    """True if the `claude` binary resolves (on PATH or as an existing file)."""
    return shutil.which(binary) is not None or Path(binary).is_file()


def usage_from_result(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize a ``claude -p --output-format json`` result to vasco's usage shape.

    Unlike the HTTP providers, ``claude -p`` returns a ``total_cost_usd`` — the
    *equivalent* API cost (a proxy for subscription quota consumed), surfaced as
    ``cost_usd``.
    """
    usage = data.get("usage") or {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cost_usd": data.get("total_cost_usd"),
    }


class ClaudeCliClient:
    """Async client that drives `claude -p` and returns its text result."""

    def __init__(
        self,
        *,
        binary: str = DEFAULT_BINARY,
        model: str = "",
        effort: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not binary_available(binary):
            raise FileNotFoundError(
                f"claude binary not found: {binary!r}. Install Claude Code or set "
                "answer.claude_binary to its path."
            )
        self._binary = binary
        self._model = model
        # Effort level (low|medium|high|xhigh|max). The CLI has no thinking-off
        # switch — unset means it applies its own default (high/xhigh in Claude
        # Code) and the model thinks adaptively. answer/summarize is a grounded
        # extraction task that needs no reasoning, so we pin `low` (the floor)
        # to clamp that default down rather than enabling reasoning.
        self._effort = effort
        self._timeout = timeout
        self.last_usage: dict[str, Any] | None = None

    async def complete(
        self, *, system: str, user: str, timeout: float | None = None
    ) -> str:
        args = [
            self._binary,
            "-p",
            "--output-format",
            "json",
            *_HERMETIC_FLAGS,
            "--system-prompt",
            system,
        ]
        if self._model:
            args += ["--model", self._model]
        if self._effort:
            args += ["--effort", self._effort]

        env = {k: v for k, v in os.environ.items() if k not in _AUTH_ENV_STRIP}

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        deadline = self._timeout if timeout is None else timeout
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=user.encode("utf-8")), timeout=deadline
            )
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"claude -p timed out after {deadline}s")

        if proc.returncode != 0:
            detail = (err or b"").decode("utf-8", "replace").strip()[:500]
            raise RuntimeError(
                f"claude -p exited {proc.returncode}: {detail or '(no stderr)'}"
            )

        data = json.loads(out.decode("utf-8"))
        if data.get("is_error") or data.get("subtype") != "success":
            raise RuntimeError(
                f"claude -p returned a non-success result: subtype={data.get('subtype')!r}"
            )
        self.last_usage = usage_from_result(data)
        return (data.get("result") or "").strip()
