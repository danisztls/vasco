"""Semantic ranking integration. The real sentence-transformers path is
skipped unless the `semantic` extra is installed; the wiring is verified with
a stub ranker.
"""

from __future__ import annotations

import asyncio
import importlib.util
from typing import Any

import pytest

from vasco import extract as extract_mod
from vasco import fetch as fetch_mod
from vasco import semantic


HAS_SENTENCE_TRANSFORMERS = importlib.util.find_spec("sentence_transformers") is not None


class _StubModel:
    """Deterministic stand-in mirroring sentence-transformers' contract: every
    ``encode`` call returns a 2D numpy array with a fixed dimensionality.

    Uses the hashing trick (zlib.adler32 → slot index) so the same token maps
    to the same slot across passage and query encode calls. Cosine similarity
    of normalized vectors then reflects token overlap.
    """

    _DIM = 64

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        pass

    @staticmethod
    def _slot(token: str) -> int:
        import zlib

        return zlib.adler32(token.encode()) % _StubModel._DIM

    def encode(self, texts: list[str], normalize_embeddings: bool = True) -> Any:  # noqa: ARG002
        import numpy as np

        out = np.zeros((len(texts), self._DIM), dtype=float)
        for i, text in enumerate(texts):
            for tok in text.lower().split():
                out[i, self._slot(tok)] = 1.0
            norm = float(np.linalg.norm(out[i])) or 1.0
            out[i] /= norm
        return out


def _patch_with_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the lazy importer with a stub model class so tests run without
    the real `sentence-transformers` dep.
    """
    monkeypatch.setattr(semantic, "_import_sentence_transformers", lambda: _StubModel)
    # Reset the module-level singleton so each test starts clean.
    monkeypatch.setattr(semantic, "_ranker", None, raising=False)


def test_semantic_ranker_ranks_by_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_with_stub(monkeypatch)
    ranker = semantic.SemanticRanker()
    passages = ["alpha beta", "gamma delta", "alpha gamma", "epsilon"]
    ranked = ranker.rank(passages, "alpha", top=5)
    # Only passages that share the "alpha" token score > 0; the ranker drops
    # the rest (parity with BM25, which also filters score > 0).
    indices = [i for i, _ in ranked]
    assert indices == [0, 2] or indices == [2, 0]
    assert 1 not in indices  # "gamma delta" — no overlap
    assert 3 not in indices  # "epsilon" — no overlap


def test_semantic_ranker_empty_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_with_stub(monkeypatch)
    ranker = semantic.SemanticRanker()
    assert ranker.rank([], "anything", top=5) == []
    assert ranker.rank(["a"], "", top=5) == []


def test_get_ranker_reuses_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_with_stub(monkeypatch)
    a = semantic.get_ranker()
    b = semantic.get_ranker()
    assert a is b


def test_get_ranker_replaces_on_model_change(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_with_stub(monkeypatch)
    a = semantic.get_ranker("model-a")
    b = semantic.get_ranker("model-b")
    assert a is not b


def test_extract_passes_rank_to_semantic(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rank parameter routes through _rank_semantic without touching BM25."""
    _patch_with_stub(monkeypatch)

    async def fake_fetch_one(url: str, **kwargs: Any) -> dict:
        return {
            "url_requested": url,
            "url_final": url,
            "url_canonical": url,
            "title": "T",
            "byline": None,
            "published": None,
            "mode_used": "http",
            "markdown": "Alpha beta gamma sentence one.\n\nAnother paragraph about delta epsilon zeta words here for length.\n\nThird passage with alpha and gamma in it for ranking purposes properly.",
            "warnings": [],
        }

    monkeypatch.setattr(fetch_mod, "fetch_one", fake_fetch_one)

    result = asyncio.run(
        extract_mod.extract(
            "https://example.com/x",
            query="alpha",
            top=3,
            rank="semantic",
            use_cache=False,
        )
    )
    assert result["ranker"] == "semantic"
    assert "failure" not in result
    assert len(result["passages"]) >= 1


def test_extract_rejects_unknown_rank() -> None:
    with pytest.raises(ValueError, match="unknown rank backend"):
        asyncio.run(
            extract_mod.extract(
                "https://example.com/x", query="q", rank="lexical", use_cache=False
            )
        )


def test_semantic_unavailable_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When sentence-transformers is not installed, the lazy importer raises a
    typed error with installation instructions.
    """

    def fake_import() -> Any:
        raise semantic.SemanticRankerUnavailable(
            "Semantic ranking requires the 'semantic' extra."
        )

    monkeypatch.setattr(semantic, "_import_sentence_transformers", fake_import)
    monkeypatch.setattr(semantic, "_ranker", None, raising=False)
    ranker = semantic.SemanticRanker()
    with pytest.raises(semantic.SemanticRankerUnavailable):
        ranker.rank(["a"], "a", top=1)


@pytest.mark.skipif(
    not HAS_SENTENCE_TRANSFORMERS,
    reason="real sentence-transformers not installed (install with --extra semantic)",
)
def test_semantic_ranker_real_model_smoke() -> None:
    """Smoke test that the real sentence-transformers path loads and ranks.
    Skipped unless the extra is installed.
    """
    ranker = semantic.SemanticRanker()
    passages = ["The cat sat on the mat.", "Quantum mechanics is hard."]
    ranked = ranker.rank(passages, "feline animal", top=2)
    assert len(ranked) == 2
    # The cat passage should rank higher than the quantum one.
    assert ranked[0][0] == 0
