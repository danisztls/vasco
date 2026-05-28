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
from vasco.adapters.deepseek import DeepSeekClient

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


async def summarize(
    markdown: str,
    *,
    question: str | None = None,
    cfg: Any | None = None,
) -> str | None:
    """Answer ``question`` over ``markdown``, or summarize it generically.

    Returns the answer text, or ``None`` if it's unavailable or fails for any
    reason (missing key, API error, empty content).
    """
    if not markdown or cfg is None:
        return None
    ac = getattr(cfg, "answer", None)
    if ac is None:
        return None

    api_key = resolve_api_key(cfg)
    if not api_key:
        log.warning("answer requested but no API key resolved; skipping")
        return None

    if question:
        system = _QUESTION_SYSTEM
        user = f"Question: {question}\n\nPage content:\n\n{markdown}"
    else:
        system = _GENERIC_SYSTEM
        user = f"Page content:\n\n{markdown}"

    try:
        client = DeepSeekClient(api_key=api_key, base_url=ac.base_url, model=ac.model)
        text = await client.complete(system=system, user=user)
    except Exception as exc:
        log.warning("answer call failed: %s", exc)
        return None
    return text or None


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
      - a result dict with ``answer`` set on success;
      - a result dict with ``error`` ("no_api_key" | "answer_failed") and
        ``answer=None`` when the LLM step can't run.
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
        "model": getattr(ac, "model", None),
        "question": question,
        "answer": None,
    }

    if not resolve_api_key(cfg):
        base["error"] = "no_api_key"
        base["message"] = (
            "No answer API key configured. "
            "Set DEEPSEEK_API_KEY or answer.api_key in config.yaml."
        )
        return base

    text = await summarize(env.get("markdown") or "", question=question, cfg=cfg)
    if text is None:
        base["error"] = "answer_failed"
        base["message"] = (
            "The answer model returned no result (API error or empty content)."
        )
        return base

    base["answer"] = text
    return base
