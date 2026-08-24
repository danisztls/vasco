# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Document → Markdown via pandoc shell adapter.

Handles DOCX, EPUB, ODT, RTF. Binary container formats (DOCX, EPUB, ODT)
are ZIP archives that pandoc cannot read from stdin, so all formats go
through a temp file for uniformity.

Raises FileNotFoundError if the pandoc binary is not on PATH — the caller
maps this to UNSUPPORTED_CONTENT_TYPE, same contract as pdf.py.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from typing import Any

_PANDOC = "pandoc"

FORMAT_BY_EXT: dict[str, str] = {
    "docx": "docx",
    "epub": "epub",
    "odt": "odt",
    "rtf": "rtf",
}

FORMAT_BY_MIME: dict[str, str] = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/epub+zip": "epub",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
}


def pandoc_to_markdown(body: bytes, *, fmt: str) -> tuple[str, dict[str, Any]]:
    """Convert document bytes to Markdown via pandoc.

    Returns (markdown, metadata) with the same metadata shape as pdf.pdf_to_text.
    """
    if shutil.which(_PANDOC) is None:
        raise FileNotFoundError(f"{_PANDOC} not found on PATH; cannot convert {fmt}")

    warnings: list[str] = []
    suffix = f".{fmt}"

    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        os.write(fd, body)
        os.close(fd)

        proc = subprocess.run(
            [_PANDOC, "-f", fmt, "-t", "markdown", "--wrap=none", tmp],
            capture_output=True,
            check=False,
            timeout=30,
        )
        text = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        if proc.returncode != 0:
            warnings.append("pandoc_nonzero_exit")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)

    word_count = len(text.split()) if text else 0
    if len(text.strip()) < 200:
        warnings.append("short_content")

    metadata: dict[str, Any] = {
        "title": None,
        "byline": None,
        "published": None,
        "modified": None,
        "language": None,
        "site_name": None,
        "word_count": word_count,
        "quality": {},
        "warnings": warnings,
    }
    return text, metadata
