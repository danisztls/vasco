"""Paywall detection — a diagnostic quality signal, not a bypass.

Scans a page's *raw HTML* for fingerprints of known paywall / metering SaaS
vendors (Piano/tinypass, Poool, Zephr, Pelcro, …). A match means the page is
served by paywall/metering infrastructure and its content may be truncated, so a
research agent can fall back to wayback or skip the result instead of ingesting a
stub. This is a *site-level* heuristic: presence of a vendor does not prove this
particular URL is gated, only that the site meters access. The bundled list is
limited to *dedicated* paywall vendors — general analytics/CDP/tag managers are
excluded to keep false positives low.

We never defeat the paywall — vasco's sanctioned fallback for gated content stays
the Wayback Machine (`vasco.adapters.wayback`).

Mirrors `vasco.fetch.netblock`: reuses the loader + remote-consolidation
machinery in `vasco.quality.blocklist` with a *separate* consolidated cache file
(`paywall_vendors.txt`) so it never clobbers the quality or netblock lists. On by
default with a bundled vendor list; point `quality.paywall_vendor_paths` at local
files or remote URLs to extend it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from importlib import resources
from pathlib import Path
from typing import Any

from .blocklist import load_blocklist

# Separate consolidation file from the quality ("blocklist.txt") and netblock
# ("netblock.txt") lists.
_PAYWALL_CONSOLIDATED = "paywall_vendors.txt"

# Non-domain script markers the domain-oriented blocklist loader would reject.
# AMP paywalls load `amp-access`/`amp-subscriptions` extensions from
# cdn.ampproject.org; matching the bare domain would flag every AMP page, so we
# match the access-control extension names instead.
_SCRIPT_MARKERS = frozenset({"amp-access", "amp-subscriptions"})

_vendors: frozenset[str] | None = None


def _bundled_default_path() -> Path:
    """Filesystem path to the bundled vendor fingerprint list."""
    return Path(str(resources.files("vasco.quality") / "data" / "paywall_vendors.txt"))


def load_paywall_vendors(paths: Sequence[str | Path]) -> frozenset[str]:
    """Resolve the vendor fingerprint set from the configured paths.

    Configured `paths` (local or remote) win; with none set, the bundled default
    is used. The non-domain script markers are always included. May perform I/O
    (file reads, remote consolidation) — call off the event loop.
    """
    sources: list[str | Path] = list(paths) if paths else [_bundled_default_path()]
    domains = load_blocklist(sources, consolidated_name=_PAYWALL_CONSOLIDATED)
    return domains | _SCRIPT_MARKERS


def get_paywall_vendors(quality_cfg: Any | None = None) -> frozenset[str]:
    """Return the cached vendor set, loading on first call.

    `quality_cfg` is the `QualityCfg` section (the same object `quality.score`
    receives), read for `paywall_vendor_paths`. Cached as a singleton; call
    `reset()` to reload.
    """
    global _vendors
    if _vendors is None:
        paths: tuple[str, ...] = ()
        if quality_cfg is not None:
            with contextlib.suppress(Exception):
                paths = tuple(quality_cfg.paywall_vendor_paths)
        _vendors = load_paywall_vendors(paths)
    return _vendors


def reset() -> None:
    """Clear the cached vendor set (for testing or config reload)."""
    global _vendors
    _vendors = None


def detect_paywall(raw_html: str | None, vendors: frozenset[str]) -> str | None:
    """Return the first matching vendor fingerprint in `raw_html`, else None.

    Pure function — no I/O. Best-effort substring scan; iterates in sorted order
    so the reported vendor is deterministic when several match.
    """
    if not raw_html or not vendors:
        return None
    haystack = raw_html.lower()
    for vendor in sorted(vendors):
        if vendor in haystack:
            return vendor
    return None
