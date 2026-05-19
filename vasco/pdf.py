"""PDF → text via `pdftotext` and metadata via `pdfinfo`.

Both binaries are shelled out. If `pdftotext` is missing on PATH, raises
FileNotFoundError so the caller can map it to UNSUPPORTED_CONTENT_TYPE.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any


_PDFTOTEXT = "pdftotext"
_PDFINFO = "pdfinfo"


def _run(cmd: list[str], *, stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
    proc = subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_pdfinfo(stdout: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip().lower()] = value.strip()
    return out


def _extract_metadata(pdf_bytes: bytes) -> dict[str, Any]:
    """Best-effort metadata via `pdfinfo`. Returns {} if unavailable."""
    if shutil.which(_PDFINFO) is None:
        return {}
    rc, stdout, _ = _run([_PDFINFO, "-"], stdin=pdf_bytes)
    if rc != 0:
        return {}
    try:
        return _parse_pdfinfo(stdout.decode("utf-8", errors="replace"))
    except Exception:
        return {}


def pdf_to_text(pdf_bytes: bytes) -> tuple[str, dict]:
    """Convert PDF bytes to plain text and return (text, metadata)."""
    if shutil.which(_PDFTOTEXT) is None:
        raise FileNotFoundError(
            f"{_PDFTOTEXT} not found on PATH; cannot extract PDF text"
        )

    warnings: list[str] = []
    rc, stdout, stderr = _run(
        [_PDFTOTEXT, "-layout", "-enc", "UTF-8", "-", "-"], stdin=pdf_bytes
    )
    text = stdout.decode("utf-8", errors="replace") if stdout else ""
    if rc != 0:
        warnings.append("pdftotext_nonzero_exit")

    info = _extract_metadata(pdf_bytes)
    title = info.get("title") or None
    byline = info.get("author") or None
    published = info.get("creationdate") or None
    # pdfinfo does not report language; leave None.
    language = None

    word_count = len(text.split()) if text else 0
    if len(text.strip()) < 200:
        warnings.append("short_content")

    metadata: dict[str, Any] = {
        "title": title,
        "byline": byline,
        "published": published,
        "modified": info.get("moddate") or None,
        "language": language,
        "site_name": None,
        "word_count": word_count,
        "links": [],
        "quality": {},
        "warnings": warnings,
    }
    return text, metadata
