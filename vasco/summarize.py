# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""LLM answering/summarization over fetched page content.

Powers the `answer` command (CLI + MCP): fetch a page, then have a cheap LLM
produce an answer to a question, or a generic summary when no question is given.
Never raises: on any failure it returns ``None`` so the caller can surface a
clean error instead of crashing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from vasco import fetch as _fetch
from vasco.adapters.claude_cli import DEFAULT_BINARY, ClaudeCliClient, binary_available
from vasco.adapters.deepseek import PROVIDER_ENDPOINTS, DeepSeekClient

log = logging.getLogger("vasco.answer")

_GENERIC_SYSTEM = (
    "You summarize web page content for another AI agent. Produce a concise, "
    "faithful summary capturing the page's purpose, key facts, figures, and "
    "conclusions. Lead with the most important point. Use plain prose or tight "
    "bullets — no title, no headings, no preamble. Do not add commentary or "
    "invent details; omit navigation/boilerplate. If the page has no substantive "
    "content (a redirect or paywall stub), say so in one line."
)

_QUESTION_SYSTEM = (
    "You answer a specific question for another AI agent, using only the provided "
    "web page content. Answer directly first, then the key supporting facts or "
    "figures (quote sparingly). Use plain prose or tight bullets — no headings, no "
    "preamble, do not restate the question. If the page does not contain the "
    "answer, say so explicitly in one line. Do not invent details."
)


def resolve_api_key(pcfg: Any) -> str:
    """Resolve a provider's API key from env → its config entry, in that order."""
    env_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get(
        "VASCO_ANSWER_API_KEY"
    )
    if env_key:
        return env_key
    return getattr(pcfg, "api_key", "") or ""


def _backend_status(pcfg: Any) -> tuple[str | None, str | None]:
    """Readiness of one provider-chain entry.

    Returns ``(error_code, message)`` for why this entry can't run, or
    ``(None, None)`` when it's ready. There is no default provider/model.
    """
    provider = (getattr(pcfg, "provider", "") or "").strip()
    if provider in PROVIDER_ENDPOINTS:  # OpenAI-compatible HTTP providers
        if not resolve_api_key(pcfg):
            return "no_api_key", (
                "No API key for the answer provider. "
                "Set DEEPSEEK_API_KEY or its api_key in config.yaml."
            )
    elif provider == "claude_cli":
        if not binary_available(DEFAULT_BINARY):
            return "claude_cli_unavailable", (
                "claude binary not found. Install Claude Code and ensure `claude` "
                "is on PATH (including the vascod service's PATH)."
            )
    else:
        return "answer_not_configured", (
            "No answer provider configured. Add an answer.providers entry with "
            "provider 'deepseek' or 'claude_cli'."
        )
    if not getattr(pcfg, "model", ""):
        return "answer_not_configured", "Set a model for the answer provider."
    return None, None


@dataclass
class _AnswerResult:
    """Outcome of running the answer provider chain over a page."""

    text: str | None = None
    usage: dict[str, Any] | None = None
    provider: str | None = None  # the provider that actually served the answer
    model: str | None = None
    fell_back: bool = False  # a non-primary chain entry served it
    error: str | None = None  # error code when text is None
    message: str | None = None


async def _run_backend(
    system: str, user: str, pcfg: Any
) -> tuple[str | None, dict[str, Any] | None]:
    """Call one ready provider; return ``(text, usage)`` or ``(None, None)``."""
    provider = (getattr(pcfg, "provider", "") or "").strip()
    try:
        if provider == "claude_cli":
            client: Any = ClaudeCliClient(
                model=pcfg.model, effort=getattr(pcfg, "effort", "") or ""
            )
        else:
            client = DeepSeekClient(
                api_key=resolve_api_key(pcfg),
                base_url=PROVIDER_ENDPOINTS[provider],
                model=pcfg.model,
            )
        text = await client.complete(system=system, user=user)
    except Exception as exc:
        log.warning("answer call failed (%s): %s", provider, exc)
        return None, None
    return (text or None), getattr(client, "last_usage", None)


async def _generate(
    markdown: str, question: str | None, cfg: Any | None
) -> _AnswerResult:
    """Run the answer provider chain over ``markdown``.

    Tries ``cfg.answer.providers`` in order: skips entries that aren't ready,
    calls ready ones until one returns an answer (``fell_back`` set when a
    non-first entry served it). Never raises.
    """
    ac = getattr(cfg, "answer", None)
    if not markdown or cfg is None or ac is None:
        return _AnswerResult()
    providers = tuple(getattr(ac, "providers", ()) or ())
    if not providers:
        return _AnswerResult(
            error="answer_not_configured",
            message=(
                "No answer provider configured. Set answer.providers in config.yaml."
            ),
        )

    if question:
        system = _QUESTION_SYSTEM
        user = f"Question: {question}\n\nPage content:\n\n{markdown}"
    else:
        system = _GENERIC_SYSTEM
        user = f"Page content:\n\n{markdown}"

    first_status: tuple[str | None, str | None] | None = None
    any_ready = False
    for index, pcfg in enumerate(providers):
        err, msg = _backend_status(pcfg)
        if first_status is None:
            first_status = (err, msg)
        if err is not None:
            log.warning(
                "answer provider %r not ready: %s",
                getattr(pcfg, "provider", ""),
                err,
            )
            continue
        any_ready = True
        text, usage = await _run_backend(system, user, pcfg)
        if text is not None:
            return _AnswerResult(
                text=text,
                usage=usage,
                provider=(pcfg.provider or "").strip() or None,
                model=pcfg.model or None,
                fell_back=index > 0,
            )
        log.warning("answer provider %r failed; trying next", pcfg.provider)

    if not any_ready:
        err, msg = first_status or ("answer_not_configured", None)
        return _AnswerResult(error=err, message=msg)
    return _AnswerResult(
        error="answer_failed",
        message="All configured answer providers failed (API error or empty content).",
    )


async def summarize(
    markdown: str,
    *,
    question: str | None = None,
    cfg: Any | None = None,
) -> str | None:
    """Answer ``question`` over ``markdown``, or summarize it generically.

    Returns the answer text, or ``None`` if it's unavailable or fails for any
    reason (no provider configured, API error, empty content).
    """
    return (await _generate(markdown, question, cfg)).text


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
      - a result dict with ``answer`` set on success (plus the **served**
        ``provider``/``model``, a ``usage`` block — token counts and, for
        claude_cli, ``cost_usd`` — and ``fell_back`` when a non-primary entry
        served it);
      - a result dict with ``error`` and ``answer=None`` when the LLM step can't
        run: ``answer_not_configured`` (empty chain / missing model),
        ``no_api_key`` / ``claude_cli_unavailable`` (no entry ready), or
        ``answer_failed`` (every ready provider returned nothing).
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

    result = await _generate(env.get("markdown") or "", question, cfg)
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
        "model": result.model,
        "provider": result.provider,
        "question": question,
        "answer": result.text,
    }

    if result.text is None:
        base["error"] = result.error or "answer_failed"
        base["message"] = result.message or (
            "The answer model returned no result (API error or empty content)."
        )
        return base

    base["usage"] = result.usage
    base["fell_back"] = result.fell_back
    return base
