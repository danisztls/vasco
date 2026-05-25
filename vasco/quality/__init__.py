"""Content quality scoring for fetch envelopes.

Three layers:
1. Domain blocklist — community-curated lists of known slop/farm domains.
2. Text heuristics — lightweight slop vocabulary and structural analysis.
3. Optional classifier — fastText model for higher accuracy (opt-in dep).

The main entry point is `score()`, which returns a dict to merge into the
envelope's `quality` field alongside existing trafilatura signals.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from . import blocklist, classifier, heuristics

if TYPE_CHECKING:
    from vasco.config import QualityCfg


def score(
    markdown: str,
    *,
    url: str | None = None,
    cfg: "QualityCfg | None" = None,
) -> dict:
    """Score content quality. Returns dict with slop_score, domain_flagged, signals.

    Designed to run in <10ms for typical pages (no I/O, no model loading on
    the hot path unless classifier is enabled).
    """
    result: dict = {}

    # Layer 1: domain blocklist.
    bl = None
    if cfg and cfg.blocklist_paths:
        paths = [Path(p).expanduser() for p in cfg.blocklist_paths]
        bl = blocklist.get_blocklist(paths)
    else:
        bl = blocklist.get_blocklist()

    domain_flagged = False
    if url:
        domain_flagged = blocklist.is_blocked(url, bl)
    result["domain_flagged"] = domain_flagged

    # Layer 2: text heuristics.
    signals = heuristics.compute(markdown)
    slop_score = heuristics.composite_score(signals)
    result["slop_score"] = slop_score
    result["signals"] = asdict(signals)

    # Layer 3: classifier (enabled when model path is set).
    model_path = cfg.classifier_model_path if cfg else ""
    if model_path:
        result["classifier_quality"] = classifier.classify(
            markdown, model_path=model_path
        )
    else:
        result["classifier_quality"] = None

    return result
