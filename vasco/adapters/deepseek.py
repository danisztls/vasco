# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Any

# OpenAI-compatible providers: name → chat-completions base URL. Replaces the old
# free-form `answer.base_url`, so each provider is a known identity (clean usage
# attribution). Adding another OpenAI-compatible provider is a one-line entry.
PROVIDER_ENDPOINTS: dict[str, str] = {"deepseek": "https://api.deepseek.com/v1"}


def usage_from_response(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize an OpenAI-compatible ``usage`` block to vasco's usage shape.

    OpenAI-compatible providers report token counts but no dollar cost, so
    ``cost_usd`` is always ``None`` here. DeepSeek's ``prompt_cache_hit_tokens``
    maps to ``cache_read_input_tokens`` when present.
    """
    usage = data.get("usage") or {}
    return {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "cache_read_input_tokens": usage.get("prompt_cache_hit_tokens"),
        "cache_creation_input_tokens": None,
        "cost_usd": None,
    }


class DeepSeekClient:
    """Async client for an OpenAI-compatible chat-completions endpoint.

    Defaults target DeepSeek, but any provider exposing
    ``POST {base_url}/chat/completions`` works. Raises on HTTP/transport
    error so the caller can fall back to returning full content. After a call,
    ``last_usage`` holds the normalized token usage (see ``usage_from_response``).
    """

    def __init__(self, *, api_key: str, base_url: str, model: str) -> None:
        if not api_key:
            raise ValueError(
                "Answer API key not configured. "
                "Set DEEPSEEK_API_KEY or answer.api_key in config.yaml."
            )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self.last_usage: dict[str, Any] | None = None

    async def complete(
        self,
        *,
        system: str,
        user: str,
        timeout: float = 30.0,
    ) -> str:
        import httpx

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        self.last_usage = usage_from_response(data)
        return (data["choices"][0]["message"]["content"] or "").strip()
