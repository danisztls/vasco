"""Content quality scoring for fetch envelopes.

Two layers:
1. Domain blocklist — community-curated lists of known slop/farm domains.
2. Text heuristics — lightweight slop vocabulary and structural analysis,
   plus envelope metadata signals (boilerplate, byline, date, word count).

Skipped for sources with their own quality signals (Wikimedia, YouTube).

The main entry point is `score()`, which returns a dict to merge into the
envelope's `quality` field alongside existing trafilatura signals.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from vasco.adapters import wikimedia, youtube

from . import blocklist, heuristics, paywall

if TYPE_CHECKING:
    from vasco.config import QualityCfg

# Sources with their own quality signals — heuristics would add noise.
_SKIP_HEURISTICS_CHECKERS = (wikimedia.is_wikimedia_url, youtube.is_youtube_url)


def score(
    markdown: str,
    *,
    url: str | None = None,
    cfg: "QualityCfg | None" = None,
    existing_quality: dict | None = None,
    metadata: dict | None = None,
    raw_html: str | None = None,
) -> dict:
    """Score content quality. Returns dict with slop_score, domain_flagged, signals.

    existing_quality: the quality dict from html_to_markdown (has boilerplate_ratio).
    metadata: the full metadata dict (has byline, published, word_count).
    raw_html: the un-converted HTML; needed for paywall-vendor detection because
        trafilatura strips the <script> tags those fingerprints live in.
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

    # Paywall detection (diagnostic only). Runs before the heuristics skip so
    # paywalled Wikimedia/YouTube would still be flagged. Best-effort, never
    # raises — a detection failure must not break a fetch.
    detect = getattr(cfg, "detect_paywall", True) if cfg is not None else True
    paywall_vendor = None
    if detect:
        try:
            paywall_vendor = paywall.detect_paywall(
                raw_html, paywall.get_paywall_vendors(cfg)
            )
        except Exception:
            paywall_vendor = None
    result["paywalled"] = paywall_vendor is not None
    result["paywall_vendor"] = paywall_vendor

    # Skip heuristics for sources with their own quality signals.
    if url and any(check(url) for check in _SKIP_HEURISTICS_CHECKERS):
        return result

    # Layer 2: text heuristics + envelope metadata signals.
    boilerplate_ratio = (existing_quality or {}).get("boilerplate_ratio", 0.0)
    has_byline = bool((metadata or {}).get("byline"))
    has_date = bool((metadata or {}).get("published"))
    word_count = (metadata or {}).get("word_count", 0)

    signals = heuristics.compute(markdown)
    slop_score = heuristics.composite_score(
        signals,
        boilerplate_ratio=boilerplate_ratio,
        has_byline=has_byline,
        has_date=has_date,
        word_count=word_count,
    )
    result["slop_score"] = slop_score
    result["signals"] = asdict(signals)

    return result
