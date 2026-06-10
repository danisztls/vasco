"""Network request blocklist for the browser tier.

Third-party tracker/ad request interception. When the browser tier loads a page,
a `page.route` handler aborts requests whose *registered domain* differs from the
page's **and** that match a tracker/ad hostlist; first-party resources (same
registered domain, incl. same-site CDNs/subdomains) are never blocked. Blocking
only third-party trackers makes the headless browser look like a real adblock /
PiHole user, so it aids rather than hurts the tier's anti-bot purpose.

Reuses the loader + parent-domain matcher in `vasco.quality.blocklist`, but keeps
its own consolidated cache file and singleton: a domain may be quality-flagged yet
still need to serve resources, so the two lists must stay independent.

On by default with a bundled conservative list (Peter Lowe's ad/tracking servers);
point `browser.network_blocklist_paths` at local files or remote URLs for more
coverage — those flow through the same consolidate + 7-day-refresh machinery.
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import resources
from pathlib import Path
from typing import Any

from ..urls import registered_domain
from ..quality.blocklist import is_blocked, load_blocklist

# Separate consolidation file from the quality list's "blocklist.txt".
_NETBLOCK_CONSOLIDATED = "netblock.txt"

_netblock: frozenset[str] | None = None


def _bundled_default_path() -> Path:
    """Filesystem path to the bundled conservative default list."""
    return Path(str(resources.files("vasco.fetch") / "data" / "netblock_default.txt"))


def load_netblock(block_ads: bool, paths: Sequence[str | Path]) -> frozenset[str]:
    """Resolve the network blocklist from the browser config knobs.

    `block_ads` off → empty (interception disabled). Otherwise configured
    `paths` (local or remote) win; with none set, the bundled default is used.
    May perform I/O (file reads, remote consolidation) — call off the event loop.
    """
    if not block_ads:
        return frozenset()
    sources: list[str | Path] = list(paths) if paths else [_bundled_default_path()]
    return load_blocklist(sources, consolidated_name=_NETBLOCK_CONSOLIDATED)


def get_netblock(cfg: Any | None = None) -> frozenset[str]:
    """Return the cached network blocklist, loading from `cfg.browser` on first call."""
    global _netblock
    if _netblock is None:
        block_ads = True
        paths: tuple[str, ...] = ()
        if cfg is not None:
            try:
                block_ads = bool(cfg.browser.block_ads)
                paths = tuple(cfg.browser.network_blocklist_paths)
            except Exception:
                pass
        _netblock = load_netblock(block_ads, paths)
    return _netblock


def reset() -> None:
    """Clear the cached network blocklist (for testing or config reload)."""
    global _netblock
    _netblock = None


def should_block(request_url: str, page_domain: str, blocklist: frozenset[str]) -> bool:
    """True if `request_url` is a third-party request matching the blocklist.

    First-party requests (same registered domain as the page) are never blocked,
    so same-site CDNs/subdomains always load. Pure function — no I/O.
    """
    if not blocklist:
        return False
    if registered_domain(request_url) == page_domain:
        return False
    return is_blocked(request_url, blocklist)
