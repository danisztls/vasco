from __future__ import annotations

from typing import Any


class DeepSeekClient:
    """Async client for an OpenAI-compatible chat-completions endpoint.

    Defaults target DeepSeek, but any provider exposing
    ``POST {base_url}/chat/completions`` works. Raises on HTTP/transport
    error so the caller can fall back to returning full content.
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
        return (data["choices"][0]["message"]["content"] or "").strip()
