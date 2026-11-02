# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Binary-document fetchers: PDF (pdftotext) and pandoc-supported formats.

These download bytes directly via httpx and convert them out-of-band — they do
not go through the html escalation chain. Kept module-level (and httpx as a
module attribute) so the same `httpx.AsyncClient` patch used for the http tier
applies here too.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

try:  # pragma: no cover - httpx is an optional dep at import time.
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from vasco import io as io_mod
from vasco.converters import pandoc, pdf
from vasco.envelope import (
    failure_envelope as _failure_envelope,
)
from vasco.envelope import (
    success_envelope as _success_envelope,
)
from vasco.errors import FailureReason

from . import bot_detect
from .phases import _ms_since, _Phases
from .urlutils import _HTTP_TIMEOUT_FLOOR, _parse_retry_after

# How much of a response body to look at when classifying a failed download.
_CLASSIFY_BYTES = 256 * 1024
# PDF readers accept junk before the header, so don't insist on offset 0.
_PDF_MAGIC_WINDOW = 1024


def _download_exception_failure(
    exc: Exception, *, base: dict[str, Any], label: str
) -> dict[str, Any]:
    if isinstance(exc, httpx.TimeoutException):
        reason = FailureReason.TIMEOUT
    elif isinstance(exc, httpx.ConnectError):
        reason = FailureReason.DNS_FAIL
    else:
        reason = FailureReason.SERVER_ERROR
    return _failure_envelope(
        base=base,
        reason=reason,
        message=f"{label} fetch error: {type(exc).__name__}",
    )


def _download_failure(
    *, base: dict[str, Any], status: int, body: bytes, headers: dict[str, str]
) -> dict[str, Any] | None:
    """A failure envelope when the download isn't the document, else None.

    Without this an error page goes to the converter and comes back as a cached
    success with zero words.
    """
    if 200 <= status < 300:
        return None
    text = body[:_CLASSIFY_BYTES].decode("utf-8", "replace")
    reason = bot_detect.classify(status, text, headers)
    if reason == FailureReason.OK:
        reason = FailureReason.SERVER_ERROR
    return _failure_envelope(
        base=base,
        reason=reason,
        message=f"document download returned HTTP {status}",
        retry_after=_parse_retry_after(headers),
    )


def _not_a_pdf_failure(
    *, base: dict[str, Any], status: int, body: bytes, headers: dict[str, str]
) -> dict[str, Any] | None:
    """A failure envelope when a 2xx body isn't a PDF (e.g. a block page), else None."""
    if b"%PDF" in body[:_PDF_MAGIC_WINDOW]:
        return None
    text = body[:_CLASSIFY_BYTES].decode("utf-8", "replace")
    reason = bot_detect.classify(status, text, headers)
    if reason == FailureReason.OK:
        reason = FailureReason.UNSUPPORTED_CONTENT_TYPE
    return _failure_envelope(
        base=base,
        reason=reason,
        message=f"response is not a PDF ({headers.get('content-type') or 'no content type'})",
    )


async def _fetch_pdf(
    url: str,
    *,
    base: dict[str, Any],
    deadline_monotonic: float,
    cfg: Any | None,
    phases: _Phases,
) -> dict[str, Any]:
    if httpx is None:
        return _failure_envelope(
            base=base,
            reason=FailureReason.UNSUPPORTED_CONTENT_TYPE,
            message="httpx not available for PDF download",
        )

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return _failure_envelope(
            base=base,
            reason=FailureReason.DEADLINE_EXCEEDED,
            message="deadline elapsed before PDF download",
        )

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            timeout=max(_HTTP_TIMEOUT_FLOOR, remaining),
        ) as client:
            resp = await client.get(url)
            body = resp.content
            headers = dict(resp.headers)
            base["url_final"] = str(resp.url)
            base["http_status"] = int(resp.status_code)
    except Exception as exc:
        phases.network_ms += _ms_since(t0)
        phases.attempts += 1
        return _download_exception_failure(exc, base=base, label="pdf")
    phases.network_ms += _ms_since(t0)
    phases.attempts += 1

    status = base["http_status"]
    failure = _download_failure(
        base=base, status=status, body=body, headers=headers
    ) or _not_a_pdf_failure(base=base, status=status, body=body, headers=headers)
    if failure is not None:
        return failure

    t_parse = time.monotonic()
    ocr_cfg = getattr(cfg, "ocr", None)
    ocr_opts = (
        pdf.OcrOptions(
            enabled=ocr_cfg.enabled,
            language=ocr_cfg.language,
            dpi=ocr_cfg.dpi,
            max_pages=ocr_cfg.max_pages,
            min_page_chars=ocr_cfg.min_page_chars,
        )
        if ocr_cfg is not None
        else pdf.OcrOptions()
    )
    try:
        # OCR can run several subprocesses for seconds; offload so it never blocks
        # the event loop, and pass the deadline so the page loop stops in time.
        text, meta = await asyncio.to_thread(
            pdf.pdf_to_text,
            body,
            ocr=ocr_opts,
            deadline_monotonic=deadline_monotonic,
        )
    except FileNotFoundError as exc:
        phases.parse_ms += _ms_since(t_parse)
        return _failure_envelope(
            base=base,
            reason=FailureReason.UNSUPPORTED_CONTENT_TYPE,
            message=str(exc),
        )
    except Exception as exc:
        phases.parse_ms += _ms_since(t_parse)
        return _failure_envelope(
            base=base,
            reason=FailureReason.UNSUPPORTED_CONTENT_TYPE,
            message=f"pdf parse error: {type(exc).__name__}",
        )
    phases.parse_ms += _ms_since(t_parse)

    base["content_type"] = "application/pdf"
    return _success_envelope(
        base=base,
        markdown=text,
        metadata=meta,
        token_count_estimate=io_mod.estimate_tokens(text),
    )


async def _fetch_pandoc_doc(
    url: str,
    *,
    fmt: str,
    base: dict[str, Any],
    deadline_monotonic: float,
    cfg: Any | None,
    phases: _Phases,
) -> dict[str, Any]:
    if httpx is None:
        return _failure_envelope(
            base=base,
            reason=FailureReason.UNSUPPORTED_CONTENT_TYPE,
            message="httpx not available for document download",
        )

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return _failure_envelope(
            base=base,
            reason=FailureReason.DEADLINE_EXCEEDED,
            message="deadline elapsed before document download",
        )

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            timeout=max(_HTTP_TIMEOUT_FLOOR, remaining),
        ) as client:
            resp = await client.get(url)
            body = resp.content
            headers = dict(resp.headers)
            base["url_final"] = str(resp.url)
            base["http_status"] = int(resp.status_code)
    except Exception as exc:
        phases.network_ms += _ms_since(t0)
        phases.attempts += 1
        return _download_exception_failure(exc, base=base, label="document")
    phases.network_ms += _ms_since(t0)
    phases.attempts += 1

    failure = _download_failure(
        base=base, status=base["http_status"], body=body, headers=headers
    )
    if failure is not None:
        return failure

    t_parse = time.monotonic()
    try:
        text, meta = pandoc.pandoc_to_markdown(body, fmt=fmt)
    except FileNotFoundError as exc:
        phases.parse_ms += _ms_since(t_parse)
        return _failure_envelope(
            base=base,
            reason=FailureReason.UNSUPPORTED_CONTENT_TYPE,
            message=str(exc),
        )
    except Exception as exc:
        phases.parse_ms += _ms_since(t_parse)
        return _failure_envelope(
            base=base,
            reason=FailureReason.UNSUPPORTED_CONTENT_TYPE,
            message=f"pandoc convert error: {type(exc).__name__}",
        )
    phases.parse_ms += _ms_since(t_parse)

    mime = next(
        (m for m, f in pandoc.FORMAT_BY_MIME.items() if f == fmt),
        f"application/{fmt}",
    )
    base["content_type"] = mime
    return _success_envelope(
        base=base,
        markdown=text,
        metadata=meta,
        token_count_estimate=io_mod.estimate_tokens(text),
    )
