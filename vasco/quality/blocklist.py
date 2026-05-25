"""Domain blocklist: load community-curated lists and check URLs against them.

Supports two formats:
- Plain domain list (one domain per line, comments with # or !)
- uBlacklist format (*://*.domain.com/* patterns)

Sources can be local file paths or HTTP(S) URLs. Remote lists are fetched,
merged, deduplicated, and cached as a consolidated file at
$XDG_CACHE_HOME/vasco/blocklist.txt. The consolidated file is reused on
subsequent loads; call `refresh()` to re-download.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

_UBLACKLIST_RE = re.compile(r"^\*://\*?\.?([^/\*]+)")
_COMMENT_RE = re.compile(r"^\s*[#!]|^\s*$")

_blocklist: frozenset[str] | None = None

# Re-download remote lists if the consolidated file is older than this.
_REFRESH_INTERVAL_SECONDS = 604800  # 7 days


def _cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(xdg) / "vasco"


def _consolidated_path() -> Path:
    return _cache_dir() / "blocklist.txt"


def _parse_line(line: str) -> str | None:
    """Extract a domain from a blocklist line. Returns lowercase domain or None."""
    line = line.strip()
    if not line or _COMMENT_RE.match(line):
        return None
    # uBlacklist pattern
    m = _UBLACKLIST_RE.match(line)
    if m:
        return m.group(1).lower()
    # Plain domain (may have inline comment)
    domain = line.split("#", 1)[0].split("!", 1)[0].strip().lower()
    # Reject lines that look like URLs or have spaces
    if " " in domain or "/" in domain or ":" in domain:
        return None
    if not domain or "." not in domain:
        return None
    return domain


def _is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def _fetch_remote(url: str) -> str:
    """Download a remote blocklist. Returns text or empty string on failure."""
    if httpx is None:
        return ""
    try:
        resp = httpx.get(url, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except Exception:
        return ""


def _parse_sources(sources: list[str | Path]) -> set[str]:
    """Parse all sources (local files + remote URLs) into a set of domains."""
    domains: set[str] = set()
    for source in sources:
        source_str = str(source)
        if _is_url(source_str):
            text = _fetch_remote(source_str)
        else:
            p = Path(source_str).expanduser()
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
        for line in text.splitlines():
            domain = _parse_line(line)
            if domain:
                domains.add(domain)
    return domains


def _needs_refresh(consolidated: Path) -> bool:
    if not consolidated.is_file():
        return True
    age = time.time() - consolidated.stat().st_mtime
    return age > _REFRESH_INTERVAL_SECONDS


def _write_consolidated(domains: set[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sorted_domains = sorted(domains)
    path.write_text("\n".join(sorted_domains) + "\n", encoding="utf-8")


def _read_consolidated(path: Path) -> frozenset[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return frozenset()
    return frozenset(line.strip() for line in text.splitlines() if line.strip())


def load_blocklist(sources: list[str | Path]) -> frozenset[str]:
    """Load blocklist from sources. Uses consolidated cache when fresh."""
    if not sources:
        return frozenset()

    has_remote = any(_is_url(str(s)) for s in sources)
    consolidated = _consolidated_path()

    if has_remote and not _needs_refresh(consolidated):
        return _read_consolidated(consolidated)

    domains = _parse_sources(sources)

    if has_remote and domains:
        _write_consolidated(domains, consolidated)

    return frozenset(domains)


def refresh(sources: list[str | Path]) -> frozenset[str]:
    """Force re-download of remote sources and rebuild the consolidated file."""
    consolidated = _consolidated_path()
    if consolidated.is_file():
        consolidated.unlink()
    return load_blocklist(sources)


def get_blocklist(paths: list[str | Path] | None = None) -> frozenset[str]:
    """Return the cached blocklist, loading from paths on first call."""
    global _blocklist
    if _blocklist is None:
        _blocklist = load_blocklist(paths or [])
    return _blocklist


def reset() -> None:
    """Clear the cached blocklist (for testing or config reload)."""
    global _blocklist
    _blocklist = None


def is_blocked(url: str, blocklist: frozenset[str] | None = None) -> bool:
    """Check if a URL's domain (or any parent domain) is in the blocklist."""
    bl = blocklist if blocklist is not None else get_blocklist()
    if not bl:
        return False
    try:
        hostname = urlparse(url).hostname
    except Exception:
        return False
    if not hostname:
        return False
    hostname = hostname.lower().rstrip(".")
    # Check the full hostname and all parent domains.
    parts = hostname.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in bl:
            return True
    return False
