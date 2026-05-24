from __future__ import annotations

import re
from typing import Any

from rank_bm25 import BM25Okapi

from vasco import fetch as _fetch

# Tokenization regex: lowercased alphanumerics.
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Paragraph boundary: one or more blank lines.
_PARA_SPLIT_RE = re.compile(r"\n\n+")
# Simple sentence boundary on . ! ? followed by whitespace.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Tunables (kept module-private — extract() signature stays per the contract).
_MAX_PARAGRAPH_CHARS = 500
_MIN_PASSAGE_CHARS = 30
_MIN_MERGED_SENTENCE_CHARS = 80


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _split_long_paragraph(paragraph: str, base_offset: int) -> list[tuple[str, int]]:
    """Split a too-long paragraph into sentence-grouped passages.

    Returns list of (text, char_offset_in_full_markdown) pairs. Adjacent short
    sentences are merged so we don't emit tiny fragments.
    """
    parts = _SENTENCE_SPLIT_RE.split(paragraph)
    if len(parts) <= 1:
        return [(paragraph, base_offset)]

    # Locate each sentence inside the original paragraph so offsets are exact.
    sentence_offsets: list[int] = []
    cursor = 0
    for sentence in parts:
        if not sentence:
            sentence_offsets.append(cursor)
            continue
        idx = paragraph.find(sentence, cursor)
        if idx < 0:
            idx = cursor
        sentence_offsets.append(idx)
        cursor = idx + len(sentence)

    results: list[tuple[str, int]] = []
    buf_text = ""
    buf_offset: int | None = None
    for sentence, off in zip(parts, sentence_offsets, strict=False):
        if not sentence.strip():
            continue
        if buf_offset is None:
            buf_text = sentence
            buf_offset = off
            continue
        # Merge if the running buffer is still short.
        if len(buf_text) < _MIN_MERGED_SENTENCE_CHARS:
            # Use whatever whitespace separated them in source.
            sep = paragraph[buf_offset + len(buf_text) : off] or " "
            buf_text = buf_text + sep + sentence
        else:
            results.append((buf_text, base_offset + buf_offset))
            buf_text = sentence
            buf_offset = off

    if buf_offset is not None and buf_text:
        results.append((buf_text, base_offset + buf_offset))
    return results


def _segment(markdown: str) -> list[tuple[str, int]]:
    """Segment markdown into (passage_text, char_offset) pairs."""
    if not markdown:
        return []

    passages: list[tuple[str, int]] = []
    cursor = 0
    for chunk in _PARA_SPLIT_RE.split(markdown):
        # Find this chunk's offset starting at cursor.
        if chunk:
            idx = markdown.find(chunk, cursor)
            if idx < 0:
                idx = cursor
            cursor = idx + len(chunk)
        else:
            idx = cursor

        stripped = chunk.strip()
        if not stripped:
            continue

        # Recompute offset to point at first non-whitespace char of the chunk.
        lead_ws = len(chunk) - len(chunk.lstrip())
        para_offset = idx + lead_ws

        if len(stripped) > _MAX_PARAGRAPH_CHARS:
            passages.extend(_split_long_paragraph(stripped, para_offset))
        else:
            passages.append((stripped, para_offset))

    # Filter out very short fragments.
    return [(t, o) for t, o in passages if len(t) >= _MIN_PASSAGE_CHARS]


def _rank_bm25(passages: list[str], query: str, *, top: int) -> list[tuple[int, float]]:
    corpus_tokens = [_tokenize(p) for p in passages]
    query_tokens = _tokenize(query)
    if not query_tokens or not any(corpus_tokens):
        return []
    bm25 = BM25Okapi(corpus_tokens)
    scores = bm25.get_scores(query_tokens)
    ranked = sorted(
        ((i, float(scores[i])) for i in range(len(passages))),
        key=lambda x: x[1],
        reverse=True,
    )
    return [(i, s) for i, s in ranked if s > 0][:top]


def _rank_semantic(
    passages: list[str], query: str, *, top: int
) -> list[tuple[int, float]]:
    from vasco import semantic

    ranker = semantic.get_ranker()
    return ranker.rank(passages, query, top=top)


async def extract(
    url: str,
    *,
    query: str,
    top: int = 5,
    context_chars: int = 400,
    mode: str = "auto",
    rank: str = "bm25",
    deadline: float = 30.0,
    use_cache: bool = True,
    refresh: bool = False,
    cache: Any = None,
    cfg: Any = None,
) -> dict:
    """Fetch ``url`` and return the top-K passages matching ``query``.

    ``rank`` selects the ranker: ``"bm25"`` (default, pure-Python, fast) or
    ``"semantic"`` (sentence-transformers, requires the ``semantic`` extra).
    """
    if rank not in ("bm25", "semantic"):
        raise ValueError(f"unknown rank backend: {rank!r}")

    env = await _fetch.fetch_one(
        url,
        mode=mode,
        deadline=deadline,
        use_cache=use_cache,
        refresh=refresh,
        cache=cache,
        cfg=cfg,
    )

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
        "ranker": rank,
        "query": query,
    }

    if env.get("failure"):
        base["failure"] = env["failure"]
        base["passages"] = []
        return base

    markdown = env.get("markdown") or ""
    segments = _segment(markdown)
    if not segments:
        base["passages"] = []
        return base

    passage_texts = [text for text, _ in segments]
    if rank == "semantic":
        ranked = _rank_semantic(passage_texts, query, top=top)
    else:
        ranked = _rank_bm25(passage_texts, query, top=top)

    passages: list[dict[str, Any]] = []
    md_len = len(markdown)
    for idx, score in ranked:
        text, offset = segments[idx]
        start = max(0, offset - context_chars)
        end = min(md_len, offset + len(text) + context_chars)
        passages.append(
            {
                "text": text,
                "score": round(score, 4),
                "offset": offset,
                "context": markdown[start:end],
            }
        )

    base["passages"] = passages
    return base
