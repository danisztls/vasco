"""LLM answering/summarization over fetched page content.

Powers the `answer` command (CLI + MCP): fetch a page, then have a cheap LLM
produce an answer to a question, or a generic summary when no question is given.
Never raises: on any failure it returns ``None`` so the caller can surface a
clean error instead of crashing.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from vasco import fetch as _fetch
from vasco.adapters.claude_cli import DEFAULT_BINARY, ClaudeCliClient, binary_available
from vasco.adapters.deepseek import PROVIDER_ENDPOINTS, DeepSeekClient

log = logging.getLogger("vasco.answer")

_GENERIC_SYSTEM = (
    "You summarize web page content for another AI agent. Produce a concise, "
    "faithful summary (a few short paragraphs or tight bullets) capturing the "
    "page's purpose, key facts, figures, and conclusions. Do not add "
    "commentary, do not invent details, and omit navigation/boilerplate."
)

_QUESTION_SYSTEM = (
    "You answer a specific question using only the provided web page content. "
    "Be concise and faithful; quote key facts and figures. If the page does "
    "not contain the answer, say so explicitly. Do not invent details."
)


def resolve_api_key(cfg: Any | None) -> str:
    """Resolve the answer API key from env → config, in that order."""
    env_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get(
        "VASCO_ANSWER_API_KEY"
    )
    if env_key:
        return env_key
    if cfg is not None:
        try:
            return cfg.answer.api_key or ""
        except AttributeError:
            return ""
    return ""


def _answer_backend_status(cfg: Any | None) -> tuple[str | None, str | None]:
    """Readiness of the configured answer backend.

    Returns ``(error_code, message)`` describing why the backend can't run, or
    ``(None, None)`` when it's ready to call. This drives both the
    unconfigured/disabled state (no provider selected) and the provider-specific
    requirements — there is no implicit default provider or model.
    """
    ac = getattr(cfg, "answer", None)
    provider = (getattr(ac, "provider", "") or "").strip()
    if provider in PROVIDER_ENDPOINTS:  # OpenAI-compatible HTTP providers
        if not resolve_api_key(cfg):
            return "no_api_key", (
                "No answer API key configured. "
                "Set DEEPSEEK_API_KEY or answer.api_key in config.yaml."
            )
    elif provider == "claude_cli":
        if not binary_available(DEFAULT_BINARY):
            return "claude_cli_unavailable", (
                "claude binary not found. Install Claude Code and ensure `claude` "
                "is on PATH (including the vascod service's PATH)."
            )
    else:
        return "answer_not_configured", (
            "No answer provider configured. Set answer.provider to "
            "'deepseek' or 'claude_cli' in config.yaml."
        )
    # Both providers require an explicit model (no default).
    if not getattr(ac, "model", ""):
        return "answer_not_configured", "Set answer.model for the configured provider."
    return None, None


async def _generate(
    markdown: str, question: str | None, cfg: Any | None
) -> tuple[str | None, dict[str, Any] | None]:
    """Run the configured answer backend over ``markdown``.

    Returns ``(text, usage)`` where ``usage`` is the backend's normalized token/
    cost dict (``None`` when unavailable). Returns ``(None, None)`` when the
    backend isn't ready or the call fails — callers translate that to a clean
    error. Never raises.
    """
    if not markdown or cfg is None:
        return None, None
    ac = getattr(cfg, "answer", None)
    if ac is None:
        return None, None

    err, _msg = _answer_backend_status(cfg)
    if err is not None:
        log.warning("answer requested but backend not ready: %s", err)
        return None, None

    if question:
        system = _QUESTION_SYSTEM
        user = f"Question: {question}\n\nPage content:\n\n{markdown}"
    else:
        system = _GENERIC_SYSTEM
        user = f"Page content:\n\n{markdown}"

    provider = (ac.provider or "").strip()
    try:
        if provider == "claude_cli":
            client: Any = ClaudeCliClient(model=ac.model)
        else:
            client = DeepSeekClient(
                api_key=resolve_api_key(cfg),
                base_url=PROVIDER_ENDPOINTS[provider],
                model=ac.model,
            )
        text = await client.complete(system=system, user=user)
    except Exception as exc:
        log.warning("answer call failed: %s", exc)
        return None, None
    return (text or None), getattr(client, "last_usage", None)


async def summarize(
    markdown: str,
    *,
    question: str | None = None,
    cfg: Any | None = None,
) -> str | None:
    """Answer ``question`` over ``markdown``, or summarize it generically.

    Returns the answer text, or ``None`` if it's unavailable or fails for any
    reason (backend not configured, API error, empty content).
    """
    text, _usage = await _generate(markdown, question, cfg)
    return text


async def answer(
    url: str,
    *,
    question: str | None = None,
    mode: str = "auto",
    deadline: float = 30.0,
    use_cache: bool = True,
    refresh: bool = False,
    cache: Any = None,
    cfg: Any = None,
) -> dict:
    """Fetch ``url`` then return an LLM answer to ``question`` (or a generic
    summary when ``question`` is None).

    Returns one of:
      - the fetch *failure* envelope (has ``failure``) if the page couldn't be
        fetched — propagated unchanged so callers record it;
      - a result dict with ``answer`` set on success (plus ``provider`` and a
        ``usage`` block: token counts and, for claude_cli, ``cost_usd``);
      - a result dict with ``error`` and ``answer=None`` when the LLM step can't
        run: ``answer_not_configured`` (no provider / missing model),
        ``no_api_key`` / ``claude_cli_unavailable`` (provider not ready), or
        ``answer_failed`` (the model returned nothing).
    """
    env = await _fetch.fetch_one(
        url,
        mode=mode,
        deadline=deadline,
        use_cache=use_cache,
        refresh=refresh,
        cache=cache,
        cfg=cfg,
    )
    if env.get("failure"):
        return env

    ac = getattr(cfg, "answer", None)
    provider = (getattr(ac, "provider", "") or "").strip()
    reported_model = getattr(ac, "model", None) or None
    final_url = (
        env.get("url_final")
        or env.get("url_canonical")
        or env.get("url_requested")
        or url
    )
    base: dict[str, Any] = {
        "url": final_url,
        "title": env.get("title"),
        "byline": env.get("byline"),
        "published": env.get("published"),
        "mode_used": env.get("mode_used"),
        "from_cache": env.get("from_cache", False),
        "word_count": env.get("word_count"),
        "model": reported_model,
        "question": question,
        "answer": None,
    }

    err, message = _answer_backend_status(cfg)
    if err is not None:
        base["error"] = err
        base["message"] = message
        return base

    text, usage = await _generate(env.get("markdown") or "", question, cfg)
    if text is None:
        base["error"] = "answer_failed"
        base["message"] = (
            "The answer model returned no result (API error or empty content)."
        )
        return base

    base["answer"] = text
    base["provider"] = provider or None
    base["usage"] = usage
    return base
