"""Lightweight text-level heuristics for detecting AI-generated slop.

All functions operate on plain text (post-markdown-conversion) and return
float scores in [0, 1] where higher = more slop-like. Designed to run in
<10ms on typical page lengths (~5000 words).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .wordlists import SLOP_PHRASES, SLOP_WORDS, TRANSITION_STARTS

_WORD_RE = re.compile(r"\b[a-z][a-z'-]*[a-z]\b|\b[a-z]\b")
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]+")
_EM_DASH_RE = re.compile(r"—|---?")
_PARAGRAPH_RE = re.compile(r"\n\s*\n")


@dataclass(frozen=True, slots=True)
class HeuristicSignals:
    slop_vocab_ratio: float
    slop_phrase_count: int
    sentence_length_cv: float
    em_dash_density: float
    transition_start_ratio: float
    type_token_ratio: float


def _slop_vocab_ratio(words: list[str]) -> float:
    if not words:
        return 0.0
    hits = sum(1 for w in words if w in SLOP_WORDS)
    return hits / len(words)


def _slop_phrase_count(text_lower: str) -> int:
    return sum(1 for phrase in SLOP_PHRASES if phrase in text_lower)


def _sentence_length_cv(text: str) -> float:
    """Coefficient of variation of sentence lengths. AI text tends toward < 0.25."""
    sentences = _SENTENCE_RE.findall(text)
    if len(sentences) < 5:
        return 0.5  # not enough data, assume neutral
    lengths = [len(s.split()) for s in sentences]
    mean = sum(lengths) / len(lengths)
    if mean == 0:
        return 0.5
    variance = sum((x - mean) ** 2 for x in lengths) / len(lengths)
    return (variance**0.5) / mean


def _em_dash_density(text: str, word_count: int) -> float:
    if word_count == 0:
        return 0.0
    count = len(_EM_DASH_RE.findall(text))
    return count / word_count


def _transition_start_ratio(text: str) -> float:
    """Fraction of paragraphs starting with a transition adverb."""
    paragraphs = [p.strip() for p in _PARAGRAPH_RE.split(text) if p.strip()]
    if len(paragraphs) < 3:
        return 0.0
    hits = 0
    for para in paragraphs:
        first_word = para.split(None, 1)[0].lower().rstrip(".,;:") if para else ""
        if first_word in TRANSITION_STARTS:
            hits += 1
    return hits / len(paragraphs)


def _type_token_ratio(words: list[str], window: int = 50) -> float:
    """Moving-average type-token ratio (MATTR). Lower = more repetitive."""
    if len(words) < window:
        if not words:
            return 1.0
        return len(set(words)) / len(words)
    ratios: list[float] = []
    for i in range(len(words) - window + 1):
        chunk = words[i : i + window]
        ratios.append(len(set(chunk)) / window)
    return sum(ratios) / len(ratios)


def compute(text: str) -> HeuristicSignals:
    """Run all heuristics on the given text. Fast path for short/empty content."""
    if not text or len(text) < 200:
        return HeuristicSignals(
            slop_vocab_ratio=0.0,
            slop_phrase_count=0,
            sentence_length_cv=0.5,
            em_dash_density=0.0,
            transition_start_ratio=0.0,
            type_token_ratio=1.0,
        )

    text_lower = text.lower()
    words = _WORD_RE.findall(text_lower)
    word_count = len(words)

    return HeuristicSignals(
        slop_vocab_ratio=round(_slop_vocab_ratio(words), 4),
        slop_phrase_count=_slop_phrase_count(text_lower),
        sentence_length_cv=round(_sentence_length_cv(text), 4),
        em_dash_density=round(_em_dash_density(text, word_count), 4),
        transition_start_ratio=round(_transition_start_ratio(text), 4),
        type_token_ratio=round(_type_token_ratio(words), 4),
    )


def composite_score(signals: HeuristicSignals) -> float:
    """Combine signals into a single 0-1 slop score. Higher = more likely slop.

    Weights are calibrated so that a typical human-written page scores < 0.2
    and obvious AI slop scores > 0.6.
    """
    score = 0.0

    # Slop vocabulary: strongest single signal.
    # Human baseline: ~0.001; AI slop: 0.01-0.05+
    score += min(1.0, signals.slop_vocab_ratio * 30) * 0.30

    # Slop phrases: even one is suspicious, 3+ is damning.
    score += min(1.0, signals.slop_phrase_count / 4) * 0.25

    # Sentence length uniformity: AI < 0.25 CV; human > 0.4
    # Invert: low CV = high slop signal
    cv_signal = max(0.0, 1.0 - (signals.sentence_length_cv / 0.5))
    score += cv_signal * 0.15

    # Em-dash overuse: AI ~0.005+; human ~0.001
    score += min(1.0, signals.em_dash_density / 0.008) * 0.10

    # Transition paragraph starts: AI > 0.3; human < 0.1
    score += min(1.0, signals.transition_start_ratio / 0.4) * 0.10

    # Low type-token ratio (repetitive vocabulary)
    # Human ~0.75+; AI slop ~0.55-0.65
    ttr_signal = max(0.0, 1.0 - (signals.type_token_ratio / 0.8))
    score += ttr_signal * 0.10

    return round(min(1.0, score), 4)
