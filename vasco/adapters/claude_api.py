# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Claude API answer backend — the Messages API via the official Anthropic SDK.

Billed per token against an API key, unlike `claude_cli` (which runs on the Claude
Code subscription). `ClaudeApiClient.complete` mirrors the other backends' surface
so `summarize()` treats them symmetrically. It never returns partial junk: on a
refusal, a truncated answer or an API error it raises, and the provider chain
moves on to the next entry.
"""

from __future__ import annotations

import asyncio
from typing import Any

# Bare family names, as `claude_cli` accepts them; the API only takes full ids.
MODEL_ALIASES: dict[str, str] = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
    "fable": "claude-fable-5-1",
}

# Models whose safety classifiers can decline benign page content and that have a
# server-side fallback. Haiku has none, so the parameter is withheld there and a
# refusal goes to the next entry of the provider chain instead.
_SERVER_FALLBACK_MODELS = frozenset(
    {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
)
_SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Streaming keeps a long page from hitting an HTTP timeout. Thinking counts toward
# this cap, so it sits well above what a short answer needs.
_MAX_TOKENS = 16000
# The `answer` deadline only covers the page fetch, so this is the real upper bound
# on the LLM step, SDK retries included.
DEFAULT_TIMEOUT = 120.0


def resolve_model(model: str) -> str:
    """Expand a family alias to its model id; anything else passes through."""
    return MODEL_ALIASES.get(model, model)


def usage_from_message(message: Any) -> dict[str, Any]:
    """Normalize a Messages API ``usage`` to vasco's usage shape.

    The API reports token counts but no dollar cost, so ``cost_usd`` is ``None``.
    """
    usage = message.usage
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cost_usd": None,
    }


class ClaudeApiClient:
    """Async client for one Messages API call. After a call, ``last_usage`` holds
    the normalized usage and ``last_model`` the model that served it (which a
    server-side fallback can make differ from the requested one)."""

    def __init__(self, *, api_key: str, model: str, effort: str = "") -> None:
        if not api_key:
            raise ValueError(
                "Claude API key not configured. "
                "Set ANTHROPIC_API_KEY or its api_key in config.yaml."
            )
        self._api_key = api_key
        self._model = resolve_model(model)
        self._effort = effort
        self.last_usage: dict[str, Any] | None = None
        self.last_model: str | None = None

    async def complete(
        self, *, system: str, user: str, timeout: float = DEFAULT_TIMEOUT
    ) -> str:
        import anthropic

        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": _MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if self._effort:
            params["output_config"] = {"effort": self._effort}

        async with (
            asyncio.timeout(timeout),
            anthropic.AsyncAnthropic(api_key=self._api_key) as client,
        ):
            if self._model in _SERVER_FALLBACK_MODELS:
                stream = client.beta.messages.stream(
                    **params, betas=[_SERVER_FALLBACK_BETA], fallbacks="default"
                )
            else:
                stream = client.messages.stream(**params)
            async with stream as events:
                message = await events.get_final_message()

        self.last_usage = usage_from_message(message)
        self.last_model = message.model
        if message.stop_reason == "refusal":
            category = getattr(message.stop_details, "category", None)
            raise RuntimeError(f"Claude declined the request (category={category!r})")
        if message.stop_reason == "max_tokens":
            raise RuntimeError(f"Claude answer truncated at {_MAX_TOKENS} tokens")
        return "".join(b.text for b in message.content if b.type == "text").strip()
