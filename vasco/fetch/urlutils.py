"""URL / header / content-type helpers, tier budget constants, and the
per-tier deadline clamp shared across the fetch path.

Pure leaf module: no fetch-package siblings imported, no network. The
escalation chain (`core.py`), the document fetchers (`documents.py`), and the
dispatcher (`__init__.py`) all pull their constants and small helpers from here.
"""

from __future__ import annotations

import importlib.util
import time
from typing import Any
from urllib.parse import urlsplit

from vasco.converters import pandoc
from vasco.errors import FailureReason


def _supported_accept_encoding() -> str:
    """Build an ``Accept-Encoding`` value from encodings we can actually decode.

    Advertising an encoding httpx can't decode (e.g. ``zstd`` without the
    ``zstandard`` package) makes the server send it and httpx hand back the
    raw compressed bytes — silently corrupting ``.text`` so extraction yields
    nothing. gzip/deflate are always available via stdlib zlib; br and zstd
    depend on optional packages (declared as deps, but probed here so a
    minimal env degrades gracefully instead of corrupting).
    """
    encodings = ["gzip", "deflate"]
    if importlib.util.find_spec("brotli") or importlib.util.find_spec("brotlicffi"):
        encodings.append("br")
    if importlib.util.find_spec("zstandard"):
        encodings.append("zstd")
    return ", ".join(encodings)


_ACCEPT_ENCODING = _supported_accept_encoding()


# Minimum remaining deadline (seconds) before we'll bother escalating from
# http tier to browser tier. Below this floor we return DEADLINE_EXCEEDED
# rather than spawn Firefox for nothing.
BROWSER_MIN_BUDGET: float = 3.0

# Same idea for the post-browser recovery tiers in the auto chain. Mobile
# re-uses the running Camoufox instance, so the floor matches browser.
# Wayback adds an Availability API round-trip on top of the snapshot fetch,
# so it needs slightly more headroom.
MOBILE_MIN_BUDGET: float = 3.0
WAYBACK_MIN_BUDGET: float = 4.0

# Per-tier wall-clock caps. These are the *primary* budget contract — each
# tier runs for up to its cap, and the chain naturally takes up to the sum
# (≈28s for http→browser→mobile→wayback). The caller-supplied `deadline`
# is a kill-switch hard upper bound, defaulted generously so the per-tier
# caps are what users feel in practice. Each tier's effective deadline is
# `min(global_kill_switch, now + tier_cap)`.
HTTP_MAX_BUDGET: float = 5.0
# 12s (not 8) gives heavy-but-loadable pages a fair shot at reaching
# domcontentloaded. The chain still fits the 30s kill-switch: 5+12+5+6 = 28s,
# and the MIN-budget gates below self-truncate mobile/wayback when little time
# remains. Don't raise past 12 without also bumping the default deadline.
BROWSER_MAX_BUDGET: float = 12.0
MOBILE_MAX_BUDGET: float = 5.0
WAYBACK_MAX_BUDGET: float = 6.0


def _tier_deadline(global_deadline: float, tier_max: float) -> float:
    """Clamp a per-tier deadline so a hung tier can't starve the next one."""
    return min(global_deadline, time.monotonic() + tier_max)


# Failure reasons that justify spending budget on mobile/wayback recovery.
# Other failures (NOT_FOUND, DNS_FAIL, etc.) won't change with a new tier.
_RECOVERABLE_REASONS: frozenset[FailureReason] = frozenset(
    {
        FailureReason.BLOCKED_BOT,
        FailureReason.BLOCKED_CAPTCHA,
        FailureReason.BLOCKED_CLOUDFLARE,
    }
)

# Default request timeout floor (seconds) for httpx within an outer deadline.
_HTTP_TIMEOUT_FLOOR = 1.0


def _parse_retry_after(headers: dict[str, str] | None) -> int | None:
    if not headers:
        return None
    for k, v in headers.items():
        if str(k).lower() == "retry-after":
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
    return None


def _is_pdf(url: str, headers: dict[str, str] | None) -> bool:
    path = urlsplit(url).path.lower()
    if path.endswith(".pdf"):
        return True
    if not headers:
        return False
    for k, v in headers.items():
        if str(k).lower() == "content-type":
            return "application/pdf" in str(v).lower()
    return False


def _pandoc_format(url: str, headers: dict[str, str] | None) -> str | None:
    ext = (
        urlsplit(url).path.rsplit(".", 1)[-1].lower()
        if "." in urlsplit(url).path
        else ""
    )
    if ext in pandoc.FORMAT_BY_EXT:
        return pandoc.FORMAT_BY_EXT[ext]
    if not headers:
        return None
    for k, v in headers.items():
        if str(k).lower() == "content-type":
            ct = str(v).split(";", 1)[0].strip().lower()
            if ct in pandoc.FORMAT_BY_MIME:
                return pandoc.FORMAT_BY_MIME[ct]
    return None


def _content_type(headers: dict[str, str] | None, default: str) -> str:
    if not headers:
        return default
    for k, v in headers.items():
        if str(k).lower() == "content-type":
            return str(v).split(";", 1)[0].strip() or default
    return default


# Bare content-types whose body is already human-readable text. They must NOT
# be run through trafilatura (an HTML *article* extractor — structureless text
# has no DOM, so it's discarded → empty result); the body is passed through
# verbatim instead. (RTF is text-ish but a pandoc format, so it's caught by
# `_pandoc_format` upstream and never reaches here.)
_PLAINTEXT_TYPES: frozenset[str] = frozenset(
    {"text/plain", "text/markdown", "text/x-markdown", "text/x-rst"}
)


def _is_plaintext_response(content_type: str | None, body: str) -> bool:
    """True when a 200 body should be passed through verbatim rather than
    HTML-extracted.

    Two conditions: its declared type is a plain-text family, **and** it doesn't
    actually sniff as HTML. Some servers mislabel an HTML document as
    ``text/plain``; those still want trafilatura, so the sniff keeps the "only
    HTML reaches trafilatura, raw text passes through" split honest.
    """
    if not content_type:
        return False
    if content_type.split(";", 1)[0].strip().lower() not in _PLAINTEXT_TYPES:
        return False
    head = (body or "").lstrip()[:256].lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        return False
    return True


def _normalize_url(url: str, cache: Any | None) -> str | None:
    if cache is not None and hasattr(cache, "normalize_url"):
        try:
            return cache.normalize_url(url)
        except Exception:
            return None
    try:
        from vasco import urls

        return urls.normalize_url(url)
    except Exception:
        return url if isinstance(url, str) and "://" in url else None


def _registered_domain(url: str) -> str:
    try:
        from vasco import urls

        return urls.registered_domain(url)
    except Exception:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host


def _route_key(url: str) -> str:
    """Per-route strategy key (registered domain + first path segment).

    Falls back to the bare registered domain if `urls.route_key` is
    unavailable for any reason.
    """
    try:
        from vasco import urls

        return urls.route_key(url)
    except Exception:
        return _registered_domain(url)
