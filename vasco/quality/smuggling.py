# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""ASCII-smuggling detection — encoded prompt payloads hidden in invisible text.

A page can carry instructions that a human reader never sees but an LLM reads
verbatim, by encoding them in code points that render to nothing: the Unicode
**Tags** block (U+E0000-E007F, an invisible mirror of ASCII), **variation
selectors** (U+FE00-FE0F + U+E0100-E01EF — 256 values, i.e. one arbitrary byte
each), or a binary encoding over **zero-width** characters. vasco hands fetched
text straight to an agent, so a smuggled payload is a prompt-injection channel.

**Detection keys on decodability, not on invisibility.** Measured over a 16.5k
page cache, ~0.7% of real pages contain some invisible character and *every*
occurrence was benign: `pdftotext` emitting runs of U+200B for PDF bullet
layout, machine-translated marketplace titles carrying the Google-Translate
``\\u200b\\u200b`` pair, MathML U+2061 on Wikipedia, Wingdings PUA bullets,
decorative Tag characters in a Steam username. A presence-based filter is
therefore useless — 100% false positives at a rate high enough to matter. What
separates an attack is that the run *decodes to a payload*: it is long enough to
carry a message, and (for the Tags block) the decode is printable ASCII. The
thresholds below fire on all three synthetic attack families and on **zero** of
those 16,531 real pages.

Deliberately out of scope: homoglyph/confusable substitution (no clean decoder,
and the false-positive rate on multilingual text is prohibitive), and
*visible* injection — white-on-white CSS, ``display:none``, HTML comments,
"ignore previous instructions" written in ordinary prose. This module defends
one narrow channel and should not be mistaken for general injection defense.

The decoded payload never enters the fetch envelope: writing it there would turn
a smuggled attack into a plainly visible one and hand it to the agent being
defended. `envelope_report` returns only safe metadata (kind, lengths, offsets,
digest); `escaped_payload` renders the evidence for the on-disk log, which is a
human-inspection artifact correlated to the envelope by ``sha256``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Payload families
#
# Each pattern's {n,} floor is the "long enough to be a message" bound; the
# per-family predicate applied in `scan_text` is the "actually decodes" bound.
# ---------------------------------------------------------------------------

# Unicode Tags block: subtract 0xE0000 to recover the ASCII character.
_TAG_RE = re.compile(r"[\U000e0000-\U000e007f]{6,}")

# Variation selectors: VS1-16 plus the supplement — 256 values, one byte each.
_VS_RE = re.compile("[\\ufe00-\\ufe0f\\U000e0100-\\U000e01ef]{6,}")

# Zero-width / invisible formatting characters used as binary encoding symbols:
# ZWSP/ZWNJ/ZWJ + the LTR/RTL marks (U+200B-200F), word joiner and the invisible
# math operators (U+2060-2064), and the BOM (U+FEFF).
#
# Every class in this module is written with explicit escapes on purpose: a
# literal invisible character in the source would be unreviewable, and this is
# the one file where that matters most.
_ZW_RE = re.compile("[\\u200b-\\u200f\\u2060-\\u2064\\ufeff]{16,}")

# A Tags run counts only if it decodes to (near-)printable ASCII.
_PRINTABLE_RE = re.compile(r"[ -~\n\t]")
_PRINTABLE_MIN_RATIO = 0.9

# A byte payload varies; a decorative or artifact run does not.
_VS_MIN_DISTINCT = 3
# A binary encoding needs at least two symbols — PDF layout artifacts are one
# character repeated hundreds of times, which this rejects.
_ZW_MIN_DISTINCT = 2

# Cap the evidence written to the log; a payload longer than this is truncated.
_MAX_LOGGED_CHARS = 512


@dataclass(frozen=True, slots=True)
class SmuggledPayload:
    """One decodable invisible-text run.

    ``raw`` and ``decoded`` hold attacker-controlled text and must never be
    copied into the envelope — use `envelope_report` for that, and
    `escaped_payload` for the log.
    """

    kind: str  # "tag_block" | "variation_selector" | "zero_width"
    field: str  # which envelope field it was found in
    offset: int  # character offset within that field
    char_count: int  # length of the raw run
    raw: str
    decoded: str | None  # Tags block only; the other families carry opaque bytes

    @property
    def sha256(self) -> str:
        """Digest of the payload — the envelope <-> log correlation key."""
        material = self.decoded if self.decoded is not None else self.raw
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _decode_tags(run: str) -> str:
    return "".join(chr(ord(ch) - 0xE0000) for ch in run)


def _is_printable_payload(decoded: str) -> bool:
    if not decoded:
        return False
    return len(_PRINTABLE_RE.findall(decoded)) / len(decoded) >= _PRINTABLE_MIN_RATIO


def enabled(cfg: Any | None) -> bool:
    """Whether the guard is active for this config (``quality.detect_smuggling``).

    Lives here so the fetch, search and map call sites resolve the gate the same
    way. Disabling quality scoring wholesale (``quality: false``) disables it too;
    an absent config means enabled, matching the dataclass default.
    """
    if cfg is None:
        return True
    quality_cfg = getattr(cfg, "quality", None)
    if quality_cfg is None:
        return False
    return bool(getattr(quality_cfg, "detect_smuggling", True))


def scan_text(text: str | None, *, field: str = "markdown") -> list[SmuggledPayload]:
    """Find decodable smuggled payloads in one string. Pure; never raises."""
    if not text:
        return []

    hits: list[SmuggledPayload] = []

    for match in _TAG_RE.finditer(text):
        run = match.group()
        decoded = _decode_tags(run)
        if _is_printable_payload(decoded):
            hits.append(
                SmuggledPayload(
                    kind="tag_block",
                    field=field,
                    offset=match.start(),
                    char_count=len(run),
                    raw=run,
                    decoded=decoded,
                )
            )

    for match in _VS_RE.finditer(text):
        run = match.group()
        if len(set(run)) >= _VS_MIN_DISTINCT:
            hits.append(
                SmuggledPayload(
                    kind="variation_selector",
                    field=field,
                    offset=match.start(),
                    char_count=len(run),
                    raw=run,
                    decoded=None,
                )
            )

    for match in _ZW_RE.finditer(text):
        run = match.group()
        if len(set(run)) >= _ZW_MIN_DISTINCT:
            hits.append(
                SmuggledPayload(
                    kind="zero_width",
                    field=field,
                    offset=match.start(),
                    char_count=len(run),
                    raw=run,
                    decoded=None,
                )
            )

    return hits


def scan_fields(fields: dict[str, Any]) -> list[SmuggledPayload]:
    """Scan several named strings, keeping the field name on each hit.

    Non-string values are skipped, so a caller can pass an envelope slice
    without pre-filtering.
    """
    hits: list[SmuggledPayload] = []
    for name, value in fields.items():
        if isinstance(value, str):
            hits.extend(scan_text(value, field=name))
    return hits


def scan_structure(
    value: object, *, field: str = "quality", _depth: int = 0
) -> list[SmuggledPayload]:
    """Recursively scan the strings inside a nested dict/list.

    Adapter payloads live in ``quality.products`` / ``listings`` / ``videos``
    rather than in ``markdown``, so the adapter call site needs to walk them.
    Depth-bounded; dict keys are not scanned (they are adapter-authored).
    """
    if _depth > 6:
        return []
    if isinstance(value, str):
        return scan_text(value, field=field)
    hits: list[SmuggledPayload] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            hits.extend(scan_structure(sub, field=f"{field}.{key}", _depth=_depth + 1))
    elif isinstance(value, list | tuple):
        for index, sub in enumerate(value):
            hits.extend(
                scan_structure(sub, field=f"{field}[{index}]", _depth=_depth + 1)
            )
    return hits


def escaped_payload(hit: SmuggledPayload) -> str:
    """Render one payload for the on-disk log: safe to `cat`, still readable.

    Printable ASCII survives literally — the point of the log is that a human
    can read *what* was smuggled, and a decoded Tags payload is ASCII prose.
    Everything else (invisible characters, control characters, newlines, the
    raw run of an opaque byte payload) becomes ``\\uXXXX``, so the evidence
    cannot itself carry an invisible channel into the log, break the JSONL
    line, or emit terminal escapes.
    """
    material = hit.decoded if hit.decoded is not None else hit.raw
    material = material[:_MAX_LOGGED_CHARS]
    out: list[str] = []
    for ch in material:
        code = ord(ch)
        if 0x20 <= code <= 0x7E:
            out.append(ch)
        elif code <= 0xFFFF:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    escaped = "".join(out)
    if len(material) < len(hit.decoded if hit.decoded is not None else hit.raw):
        escaped += "...[truncated]"
    return escaped


def log_records(hits: list[SmuggledPayload]) -> list[dict[str, Any]]:
    """Escaped, log-safe records for `telemetry.record_smuggling`.

    Unlike `envelope_report` these DO carry the payload — that is the point of
    the evidence log — but only ever through `escaped_payload`, and they must
    never be copied back into an envelope or a tool result.
    """
    return [
        {
            "kind": hit.kind,
            "field": hit.field,
            "offset": hit.offset,
            "char_count": hit.char_count,
            "sha256": hit.sha256,
            "payload": escaped_payload(hit),
        }
        for hit in hits
    ]


def envelope_report(hits: list[SmuggledPayload]) -> dict[str, Any]:
    """Payload-free summary safe to place in the failure envelope.

    Carries no attacker-controlled text — only where each payload was, how big
    it was, and its digest, which is what correlates the envelope to the full
    evidence in the log.
    """
    return {
        "payload_count": len(hits),
        "kinds": sorted({hit.kind for hit in hits}),
        "payloads": [
            {
                "kind": hit.kind,
                "field": hit.field,
                "offset": hit.offset,
                "char_count": hit.char_count,
                "sha256": hit.sha256,
            }
            for hit in hits
        ],
    }


def redact(text: str) -> str:
    """Strip detected payload runs out of a string.

    Used on the surfaces that have no failure channel to fall back on — search
    snippets and llms.txt bodies — where dropping the *whole* result would cost
    more than removing the payload.
    """
    if not text:
        return text
    out = text
    for pattern in (_TAG_RE, _VS_RE, _ZW_RE):
        out = pattern.sub("", out)
    return out


def summary_message(hits: list[SmuggledPayload]) -> str:
    """The agent-facing failure message. Describes the payload; never quotes it."""
    kinds = ", ".join(sorted({hit.kind for hit in hits}))
    total = sum(hit.char_count for hit in hits)
    plural = "s" if len(hits) != 1 else ""
    return (
        f"content withheld: {len(hits)} hidden-text payload{plural} "
        f"({kinds}) totalling {total} invisible characters were embedded in this "
        "page. This is an ASCII-smuggling prompt-injection channel, so the "
        "content is not returned. The decoded payload is recorded in the vasco "
        "event log (correlate by sha256); the raw HTML remains in the cache for "
        "inspection."
    )
