"""PDF → text via `pdftotext` and metadata via `pdfinfo`.

Both binaries are shelled out. If `pdftotext` is missing on PATH, raises
FileNotFoundError so the caller can map it to UNSUPPORTED_CONTENT_TYPE.

Image-only pages (scanned PDFs) carry no embedded text layer, so `pdftotext`
returns ~nothing for them. When OCR is enabled (`OcrOptions`), such pages are
rasterized with `pdftoppm` and OCR'd with `tesseract` — *per page*: a page whose
native text already extracted fine is kept verbatim, and the text is only rebuilt
when at least one page was actually OCR'd, so a fully digital PDF is untouched.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any

_PDFTOTEXT = "pdftotext"
_PDFINFO = "pdfinfo"
_PDFTOPPM = "pdftoppm"
_TESSERACT = "tesseract"


@dataclass(frozen=True)
class OcrOptions:
    """Tesseract OCR fallback knobs (mirror of config.OcrCfg; kept local so the
    converter stays free of any `vasco.config` import)."""

    enabled: bool = True
    language: str = "eng"
    dpi: int = 200
    max_pages: int = 50
    min_page_chars: int = 16


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


def _ocr_page(pdf_bytes: bytes, page: int, opts: OcrOptions) -> tuple[str, bool]:
    """Rasterize one 1-based page with `pdftoppm` and OCR it with `tesseract`.

    Returns (text, ok). Never raises — a nonzero exit, missing traineddata, or
    any exception yields ("", False) so one bad/encrypted page can't kill the
    whole conversion.
    """
    try:
        rc, png, _ = _run(
            [
                _PDFTOPPM,
                "-png",
                "-r",
                str(opts.dpi),
                "-f",
                str(page),
                "-l",
                str(page),
                "-singlefile",
                "-",
            ],
            stdin=pdf_bytes,
        )
        if rc != 0 or not png:
            return "", False
        rc, out, _ = _run(
            [_TESSERACT, "stdin", "stdout", "-l", opts.language],
            stdin=png,
        )
        if rc != 0:
            return "", False
        return out.decode("utf-8", errors="replace").strip(), True
    except Exception:
        return "", False


def _maybe_ocr(
    pdf_bytes: bytes,
    text: str,
    info: dict[str, Any],
    opts: OcrOptions,
    deadline_monotonic: float | None,
    warnings: list[str],
) -> str:
    """Per-page selective OCR. Returns the (possibly rebuilt) text and mutates
    `warnings`. The original `text` is preserved verbatim for every page that
    `pdftotext` already handled, and is only replaced when OCR actually added
    content."""
    if shutil.which(_PDFTOPPM) is None or shutil.which(_TESSERACT) is None:
        warnings.append("ocr_unavailable")
        return text

    # `pdftotext` separates pages with a form-feed, so its existing output splits
    # into per-page native text for free.
    native_pages = text.split("\f")
    try:
        page_count = int(info["pages"]) if info.get("pages") else len(native_pages)
    except (ValueError, TypeError):
        page_count = len(native_pages)
    page_count = min(page_count, opts.max_pages)
    if page_count <= 0:
        return text

    def _thin(i: int) -> bool:
        native = native_pages[i] if i < len(native_pages) else ""
        return len(native.strip()) < opts.min_page_chars

    # Common digital case: every page has real text → no work, no subprocess.
    if not any(_thin(i) for i in range(page_count)):
        return text

    rebuilt: list[str] = []
    did_ocr = False
    ocr_failed = False
    truncated = False
    for i in range(page_count):
        native = native_pages[i] if i < len(native_pages) else ""
        if not _thin(i):
            rebuilt.append(native)
            continue
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            truncated = True
            rebuilt.append(native)
            rebuilt.extend(native_pages[i + 1 : page_count])
            break
        page_text, ok = _ocr_page(pdf_bytes, i + 1, opts)
        if not ok:
            ocr_failed = True
        rebuilt.append(page_text if page_text.strip() else native)
        if page_text.strip():
            did_ocr = True

    if truncated:
        warnings.append("ocr_truncated")
    if ocr_failed:
        warnings.append("ocr_failed")

    new_text = "\f".join(rebuilt)
    if did_ocr and len(new_text.strip()) > len(text.strip()):
        warnings.append("ocr_fallback")
        return new_text
    return text


def pdf_to_text(
    pdf_bytes: bytes,
    *,
    ocr: OcrOptions | None = None,
    deadline_monotonic: float | None = None,
) -> tuple[str, dict]:
    """Convert PDF bytes to plain text and return (text, metadata).

    When `ocr` is given and enabled, image-only pages are OCR'd (see `_maybe_ocr`).
    `ocr=None` reproduces the pre-OCR behavior exactly.
    """
    if shutil.which(_PDFTOTEXT) is None:
        raise FileNotFoundError(
            f"{_PDFTOTEXT} not found on PATH; cannot extract PDF text"
        )

    warnings: list[str] = []
    rc, stdout, _stderr = _run(
        [_PDFTOTEXT, "-layout", "-enc", "UTF-8", "-", "-"], stdin=pdf_bytes
    )
    text = stdout.decode("utf-8", errors="replace") if stdout else ""
    if rc != 0:
        warnings.append("pdftotext_nonzero_exit")

    info = _extract_metadata(pdf_bytes)

    if ocr is not None and ocr.enabled:
        text = _maybe_ocr(pdf_bytes, text, info, ocr, deadline_monotonic, warnings)

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
        "quality": {},
        "warnings": warnings,
    }
    return text, metadata
