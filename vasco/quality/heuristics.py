# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

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


def composite_score(
    signals: HeuristicSignals,
    *,
    boilerplate_ratio: float = 0.0,
    has_byline: bool = True,
    has_date: bool = True,
    word_count: int = 500,
) -> float:
    """Combine text signals + envelope metadata into a single 0-1 slop score.

    Higher = more likely slop. Weights calibrated against ~60 real fetches
    (scripts/calibrate_quality.py). Metadata signals dominate because text
    heuristics show near-zero separation on 2026-era content.
    """
    score = 0.0

    # ── Text heuristics (15% weight) ──
    # Calibration showed slop vocab/phrases are the only text signals with
    # meaningful separation; sentence CV fires weakly on very uniform text.
    # Em-dash density and transition starts showed no separation at all, so
    # they're left out of the composite (the raw signals are still computed
    # and exposed in HeuristicSignals for consumers that want them).

    # Slop vocabulary: good p50=0.0000, bad p50=0.0000 but bad has a
    # longer tail (bad max=0.0154 vs good max=0.0006). Weak but worth
    # keeping for the worst offenders.
    score += min(1.0, signals.slop_vocab_ratio * 30) * 0.06

    # Slop phrases: similarly weak on real content but catches the
    # blatant "let's delve into" style when it appears.
    score += min(1.0, signals.slop_phrase_count / 3) * 0.06

    # Sentence CV: bad group skews low (p50=0.5 vs good p50=1.02).
    # Only fires on very uniform text.
    cv_signal = max(0.0, 1.0 - (signals.sentence_length_cv / 0.25))
    score += cv_signal * 0.03

    # ── Envelope metadata (85% weight) ──
    # Calibration showed these are the real separators.

    # Boilerplate ratio: strongest signal. Good p50=0.00, bad p50=0.96.
    # Threshold at 0.5 catches most bad content cleanly.
    score += min(1.0, boilerplate_ratio / 0.5) * 0.35

    # Thin content: good p50=4338 words, bad p50=57.
    # Graduated scale for the range that matters.
    if word_count < 50:
        score += 0.25
    elif word_count < 150:
        score += 0.15
    elif word_count < 400:
        score += 0.08

    # No publication date: good 93% have dates, bad only 52%.
    if not has_date:
        score += 0.15

    # No byline: weak signal (good 31% vs bad 23%) but still
    # directionally correct. Low weight.
    if not has_byline:
        score += 0.10

    return round(min(1.0, score), 4)
