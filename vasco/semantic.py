# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Semantic passage ranker for ``vasco extract --rank semantic``.

Wraps ``sentence-transformers`` (optional dep — install with the ``semantic``
extra: ``uv sync --extra semantic``). The first ``rank()`` call pays the
model-load tax (~2–4s on CPU); subsequent calls in the same process are fast.

The ranker singleton is module-level so the MCP server pays the cost once.
"""

from __future__ import annotations

import math
import threading
from typing import Any

_DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class SemanticRankerUnavailable(RuntimeError):
    """Raised when the ``semantic`` extra is not installed."""


def _import_sentence_transformers() -> Any:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise SemanticRankerUnavailable(
            "Semantic ranking requires the 'semantic' extra. "
            "Install it with: uv sync --extra semantic"
        ) from exc
    return SentenceTransformer


class SemanticRanker:
    """Cosine-similarity ranker over a sentence-transformers model.

    Constructed lazily — the model is loaded on the first ``.rank()`` call,
    not at ``__init__``, so import is cheap. Loading is guarded by a lock so
    callers that wrap ``rank`` in ``asyncio.to_thread`` can't trigger duplicate
    model loads.
    """

    def __init__(self, model: str = _DEFAULT_MODEL) -> None:
        self.model_name = model
        self._model: Any = None
        self._load_lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is None:
                cls = _import_sentence_transformers()
                self._model = cls(self.model_name)

    def rank(
        self, passages: list[str], query: str, *, top: int
    ) -> list[tuple[int, float]]:
        """Return ``[(passage_index, score), ...]`` sorted by score descending.

        Passages with non-positive similarity are dropped (parity with the
        BM25 ranker, which also filters scores ``> 0``).
        """
        if not passages or not query:
            return []
        self._ensure_loaded()
        import numpy as np

        passage_vecs = self._model.encode(passages, normalize_embeddings=True)
        query_vec = self._model.encode([query], normalize_embeddings=True)[0]
        scores_arr = np.asarray(passage_vecs) @ np.asarray(query_vec)
        scored: list[tuple[int, float]] = []
        for i, s in enumerate(scores_arr.tolist()):
            score = float(s)
            if math.isnan(score) or score <= 0:
                continue
            scored.append((i, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[: max(0, top)]


_ranker: SemanticRanker | None = None


def get_ranker(model: str = _DEFAULT_MODEL) -> SemanticRanker:
    """Return the process-singleton ranker. Reuses a loaded model across calls."""
    global _ranker
    if _ranker is None or _ranker.model_name != model:
        _ranker = SemanticRanker(model)
    return _ranker
