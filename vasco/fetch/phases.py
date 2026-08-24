# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Phase-timing primitives for the fetch path.

`_Phases` is an accumulator threaded through a single fetch to break its
`duration_ms` into network/parse/cache_write components; `_HtmlOutcome` is the
terminal result of the html fetch state machine. Both are stamped onto the
envelope at the boundary of the single-fetch body. Kept in one place so the
seam (`core.py`) and the dispatcher (`__init__.py`) share the same types.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from vasco.converters import convert
from vasco.errors import FailureReason


@dataclass
class _Phases:
    """Accumulator threaded through a single fetch to break duration into parts.

    Fields are stamped onto the success/failure envelope at the boundary of
    `_fetch_one_inner` so callers (telemetry, tests) can distinguish a slow
    network from a slow parse from a 2-attempt escalation.
    """

    network_ms: int = 0
    parse_ms: int = 0
    cache_write_ms: int = 0
    attempts: int = 0
    escalated_from: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class _HtmlOutcome:
    """Terminal result of the html fetch state machine (`_do_fetch_html`).

    Carries the raw tier result. For a *kept* http-tier success in auto/http mode
    it also carries the trafilatura conversion (`markdown`/`meta`) so the caller
    reuses it instead of converting the same html twice — the common-path
    optimization that keeps word_count escalation "basically free". Every other
    tier leaves these None and is converted once downstream.
    """

    html: str
    status: int
    headers: dict[str, str]
    reason: FailureReason
    mode_used: str
    browser_started: bool
    markdown: str | None = None
    meta: dict[str, Any] | None = None


def _ms_since(monotonic_started: float) -> int:
    return int((time.monotonic() - monotonic_started) * 1000)


def _convert_html(html: str, url: str, phases: _Phases) -> tuple[str, dict[str, Any]]:
    """Convert html→markdown with parse-phase timing; never raises.

    On any conversion error returns ``("", {"word_count": 0})`` so callers treat
    it as empty content (and escalate) rather than crash.
    """
    t0 = time.monotonic()
    try:
        markdown, meta = convert.html_to_markdown(html, url=url)
    except Exception:
        markdown, meta = "", {"word_count": 0}
    phases.parse_ms += _ms_since(t0)
    return markdown, meta


def _convert_text(
    text: str, content_type: str | None, phases: _Phases
) -> tuple[str, dict[str, Any]]:
    """Pass a plain-text body through verbatim (no HTML extraction).

    Mirrors `_convert_html`'s signature/timing so the OK branch can pick between
    them on content-type. Parse cost is negligible (nothing is parsed), but it's
    still accounted so `parse_ms` stays meaningful. Never raises.
    """
    t0 = time.monotonic()
    try:
        markdown, meta = convert.text_to_markdown(text, content_type=content_type)
    except Exception:
        markdown, meta = text or "", {"word_count": len((text or "").split())}
    phases.parse_ms += _ms_since(t0)
    return markdown, meta


def _stamp_phases(
    envelope: dict[str, Any],
    *,
    started_monotonic: float,
    phases: _Phases | None,
) -> dict[str, Any]:
    """Write duration_ms + phase fields onto the envelope in place.

    When `phases` is not None, all timing/attempt fields are stamped — even
    when zero — so a value of 0 unambiguously means "this phase ran fast"
    rather than "this phase was skipped." When `phases` is None (cache hit,
    invalid URL, YouTube), only `duration_ms` is stamped.
    """
    envelope["duration_ms"] = _ms_since(started_monotonic)
    if phases is None:
        return envelope
    envelope["network_ms"] = phases.network_ms
    envelope["parse_ms"] = phases.parse_ms
    envelope["cache_write_ms"] = phases.cache_write_ms
    envelope["attempts"] = phases.attempts
    if phases.escalated_from is not None:
        envelope["escalated_from"] = phases.escalated_from
    return envelope
