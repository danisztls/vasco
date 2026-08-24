# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

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


def _content_length(headers: dict[str, str] | None) -> int | None:
    """The ``Content-Length`` header as an int, or ``None`` if absent/unparseable."""
    if not headers:
        return None
    for k, v in headers.items():
        if str(k).lower() == "content-length":
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
    return not (head.startswith(("<!doctype html", "<html")))


# Binary major types vasco can't turn into text (no extractor / converter).
_BINARY_MAJOR_PREFIXES: tuple[str, ...] = ("image/", "audio/", "video/", "font/")

# Specific ``application/*`` subtypes that are binary blobs (archives,
# executables, disk images, raw bytes). ``application/pdf`` and the pandoc
# office formats are routed to their converters upstream (`_is_pdf` /
# `_pandoc_format`) and never reach this check.
_BINARY_APP_TYPES: frozenset[str] = frozenset(
    {
        "application/octet-stream",
        "application/zip",
        "application/gzip",
        "application/x-gzip",
        "application/x-bzip2",
        "application/x-xz",
        "application/x-tar",
        "application/x-7z-compressed",
        "application/x-rar-compressed",
        "application/vnd.rar",
        "application/x-msdownload",
        "application/x-executable",
        "application/x-sharedlib",
        "application/wasm",
        "application/java-archive",
        "application/vnd.android.package-archive",
        "application/x-iso9660-image",
        "application/x-apple-diskimage",
    }
)


def _looks_binary(text: str) -> bool:
    """Heuristic: does a decoded body look like binary rather than text?

    A binary blob decoded as text (httpx ``.text``) is littered with NUL / other
    control bytes and U+FFFD decode-replacement chars. Used to confirm a generic
    ``application/octet-stream`` (the 'unknown bytes' type, occasionally a
    *mislabeled* text file) really is binary before failing it.
    """
    if not text:
        return False
    sample = text[:4096]
    if "\x00" in sample:
        return True
    suspicious = sum(
        1 for ch in sample if ch == "�" or (ord(ch) < 32 and ch not in "\t\n\r\f\v")
    )
    return suspicious / len(sample) > 0.05


# How many bytes of an ambiguous body (``application/octet-stream``) to download
# before deciding binary-vs-text. Enough for a confident `_looks_binary` verdict
# without pulling the whole blob.
_SNIFF_BYTES: int = 8192


def _binary_type_skips_body(content_type: str | None) -> bool:
    """True for a content-type that is *definitely* binary from the header alone,
    so the response body needn't be downloaded at all to reject it.

    Everything `_is_binary_unsupported` recognizes **except** the ambiguous
    ``application/octet-stream`` (the generic 'unknown bytes' type, occasionally
    a mislabeled text file), which still needs a small content sniff. Lets the
    http tier skip pulling a large image / video / archive just to discard it.
    """
    if not content_type:
        return False
    ct = content_type.split(";", 1)[0].strip().lower()
    if not ct:
        return False
    if ct.startswith(_BINARY_MAJOR_PREFIXES):
        return True
    return ct in _BINARY_APP_TYPES and ct != "application/octet-stream"


def _is_binary_unsupported(content_type: str | None, body: str) -> bool:
    """True for a 200 body that is a binary blob vasco can't convert to text
    (image / audio / video / font / archive / executable / octet-stream).

    Such a blob must NOT reach trafilatura: it mojibakes to zero words, which
    reads as an unrendered shell and pointlessly escalates to the browser tier —
    which then tries to *download* the blob and times out. It fails fast as
    ``UNSUPPORTED_CONTENT_TYPE`` instead. PDFs and pandoc office docs are routed
    to their converters upstream, so they never reach here. SVG (``image/svg+xml``)
    counts as binary too: it's XML in theory but a *graphic* in practice (an icon /
    logo / diagram), which trafilatura extracts nothing useful from.
    ``application/octet-stream`` is treated as binary only when the decoded body
    actually looks binary (`_looks_binary`).
    """
    if not content_type:
        return False
    ct = content_type.split(";", 1)[0].strip().lower()
    if not ct:
        return False
    if ct.startswith(_BINARY_MAJOR_PREFIXES):
        return True
    if ct in _BINARY_APP_TYPES:
        if ct == "application/octet-stream":
            return _looks_binary(body)
        return True
    return False


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
        host = host.removeprefix("www.")
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
