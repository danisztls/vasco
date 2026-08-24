# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the PDF converter and its per-page Tesseract OCR fallback.

The network is never touched: `subprocess.run` and `shutil.which` are stubbed so
the OCR decision logic (which pages get OCR'd, how warnings are stamped) is tested
without `pdftotext`/`pdftoppm`/`tesseract` actually running. The real argv was
verified end-to-end against a synthesized image-only PDF during development.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from vasco.converters import pdf
from vasco.converters.pdf import OcrOptions

# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


def _which(present: set[str]):
    return lambda name: f"/usr/bin/{name}" if name in present else None


def _run_factory(
    *,
    pdftotext_out: bytes = b"",
    pages: int = 1,
    ocr_text: bytes = b"OCR RECOVERED TEXT",
    ppm_ok: bool = True,
    tess_ok: bool = True,
    calls: list[list[str]] | None = None,
):
    """Build a fake `subprocess.run` dispatching by argv[0]."""

    def fake_run(cmd, *, input=None, capture_output=True, check=False):
        if calls is not None:
            calls.append(list(cmd))
        prog = cmd[0]
        if prog == "pdftotext":
            return subprocess.CompletedProcess(cmd, 0, pdftotext_out, b"")
        if prog == "pdfinfo":
            return subprocess.CompletedProcess(
                cmd, 0, f"Pages: {pages}\n".encode(), b""
            )
        if prog == "pdftoppm":
            return subprocess.CompletedProcess(
                cmd, 0 if ppm_ok else 1, b"\x89PNGfake" if ppm_ok else b"", b""
            )
        if prog == "tesseract":
            return subprocess.CompletedProcess(
                cmd, 0 if tess_ok else 1, ocr_text if tess_ok else b"", b""
            )
        raise AssertionError(f"unexpected subprocess: {prog}")

    return fake_run


def _progs(calls: list[list[str]]) -> list[str]:
    return [c[0] for c in calls]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pdftotext_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(FileNotFoundError, match="pdftotext not found"):
        pdf.pdf_to_text(b"%PDF-1.4")


def test_ocr_none_is_legacy_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    """ocr=None must never spawn an OCR subprocess (preserves old callers)."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "shutil.which", _which({"pdftotext", "pdfinfo", "pdftoppm", "tesseract"})
    )
    monkeypatch.setattr("subprocess.run", _run_factory(pdftotext_out=b"", calls=calls))

    text, meta = pdf.pdf_to_text(b"%PDF", ocr=None)
    assert text == ""
    assert "pdftoppm" not in _progs(calls)
    assert "tesseract" not in _progs(calls)
    assert "ocr_fallback" not in meta["warnings"]


def test_no_ocr_when_all_pages_have_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: a fully digital PDF is returned untouched, no OCR."""
    body = (
        b"Page one is full of perfectly good extracted words.\f"
        b"Page two also has plenty of real native text content."
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "shutil.which", _which({"pdftotext", "pdfinfo", "pdftoppm", "tesseract"})
    )
    monkeypatch.setattr(
        "subprocess.run", _run_factory(pdftotext_out=body, pages=2, calls=calls)
    )

    text, meta = pdf.pdf_to_text(b"%PDF", ocr=OcrOptions())
    assert text == body.decode()
    assert "pdftoppm" not in _progs(calls)
    assert "tesseract" not in _progs(calls)
    assert "ocr_unavailable" not in meta["warnings"]
    assert "ocr_fallback" not in meta["warnings"]


def test_mixed_page_ocrs_only_the_empty_one(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"Real page one has lots of native words to keep verbatim.\f"
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "shutil.which", _which({"pdftotext", "pdfinfo", "pdftoppm", "tesseract"})
    )
    monkeypatch.setattr(
        "subprocess.run",
        _run_factory(
            pdftotext_out=body, pages=2, ocr_text=b"Recovered page two\n", calls=calls
        ),
    )

    text, meta = pdf.pdf_to_text(b"%PDF", ocr=OcrOptions())
    assert "Real page one has lots of native words" in text
    assert "Recovered page two" in text
    assert "ocr_fallback" in meta["warnings"]
    # Only the empty page (2) was rasterized — never page 1.
    ppm_calls = [c for c in calls if c[0] == "pdftoppm"]
    assert len(ppm_calls) == 1
    assert "-f" in ppm_calls[0] and ppm_calls[0][ppm_calls[0].index("-f") + 1] == "2"


def test_fully_empty_pdf_ocrs_all_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "shutil.which", _which({"pdftotext", "pdfinfo", "pdftoppm", "tesseract"})
    )
    monkeypatch.setattr(
        "subprocess.run",
        _run_factory(pdftotext_out=b"", pages=2, ocr_text=b"scanned text", calls=calls),
    )

    text, meta = pdf.pdf_to_text(b"%PDF", ocr=OcrOptions())
    assert text.count("scanned text") == 2
    assert "ocr_fallback" in meta["warnings"]
    assert _progs(calls).count("tesseract") == 2


def test_ocr_unavailable_when_binaries_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", _which({"pdftotext", "pdfinfo"}))
    monkeypatch.setattr("subprocess.run", _run_factory(pdftotext_out=b"", pages=1))

    text, meta = pdf.pdf_to_text(b"%PDF", ocr=OcrOptions())
    assert text == ""
    assert "ocr_unavailable" in meta["warnings"]
    assert "ocr_fallback" not in meta["warnings"]


def test_ocr_failure_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tesseract nonzero exit on the empty page → ocr_failed, page 1 survives."""
    body = b"First page keeps its perfectly good native text here.\f"
    monkeypatch.setattr(
        "shutil.which", _which({"pdftotext", "pdfinfo", "pdftoppm", "tesseract"})
    )
    monkeypatch.setattr(
        "subprocess.run",
        _run_factory(pdftotext_out=body, pages=2, tess_ok=False),
    )

    text, meta = pdf.pdf_to_text(b"%PDF", ocr=OcrOptions())
    assert "First page keeps its perfectly good native text" in text
    assert "ocr_failed" in meta["warnings"]
    assert "ocr_fallback" not in meta["warnings"]


def test_deadline_already_passed_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "shutil.which", _which({"pdftotext", "pdfinfo", "pdftoppm", "tesseract"})
    )
    monkeypatch.setattr(
        "subprocess.run", _run_factory(pdftotext_out=b"", pages=3, calls=calls)
    )

    _text, meta = pdf.pdf_to_text(
        b"%PDF", ocr=OcrOptions(), deadline_monotonic=time.monotonic() - 1
    )
    assert "ocr_truncated" in meta["warnings"]
    assert "tesseract" not in _progs(calls)
    assert "pdftoppm" not in _progs(calls)


def test_metadata_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"A digital page with more than enough native words to skip OCR entirely."
    monkeypatch.setattr(
        "shutil.which", _which({"pdftotext", "pdfinfo", "pdftoppm", "tesseract"})
    )
    monkeypatch.setattr("subprocess.run", _run_factory(pdftotext_out=body, pages=1))

    _, meta = pdf.pdf_to_text(b"%PDF", ocr=OcrOptions())
    for key in ("title", "byline", "published", "modified", "language", "site_name"):
        assert key in meta
    assert isinstance(meta["word_count"], int)
    assert isinstance(meta["quality"], dict)
    assert isinstance(meta["warnings"], list)
