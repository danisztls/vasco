"""Binary-document fetchers: PDF (pdftotext) and pandoc-supported formats.

These download bytes directly via httpx and convert them out-of-band — they do
not go through the html escalation chain. Kept module-level (and httpx as a
module attribute) so the same `httpx.AsyncClient` patch used for the http tier
applies here too.
"""

from __future__ import annotations

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
    success_envelope as _success_envelope,
)
from vasco.errors import FailureReason

from .phases import _Phases, _ms_since
from .urlutils import _HTTP_TIMEOUT_FLOOR


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
            base["url_final"] = str(resp.url)
            base["http_status"] = int(resp.status_code)
    except Exception as exc:
        phases.network_ms += _ms_since(t0)
        phases.attempts += 1
        return _failure_envelope(
            base=base,
            reason=FailureReason.DNS_FAIL,
            message=f"pdf fetch error: {type(exc).__name__}",
        )
    phases.network_ms += _ms_since(t0)
    phases.attempts += 1

    t_parse = time.monotonic()
    try:
        text, meta = pdf.pdf_to_text(body)
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
            base["url_final"] = str(resp.url)
            base["http_status"] = int(resp.status_code)
    except Exception as exc:
        phases.network_ms += _ms_since(t0)
        phases.attempts += 1
        return _failure_envelope(
            base=base,
            reason=FailureReason.DNS_FAIL,
            message=f"document fetch error: {type(exc).__name__}",
        )
    phases.network_ms += _ms_since(t0)
    phases.attempts += 1

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
