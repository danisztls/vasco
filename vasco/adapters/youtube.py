"""YouTube transcript fetcher.

Public surface:
- ``is_youtube_url(url)`` — match youtube.com (all TLDs), m.youtube.com,
  music.youtube.com, youtu.be (with or without www.).
- ``extract_video_id(url)`` — parse a video ID from any supported URL form.
- ``fetch_youtube(url, *, deadline, cfg=None)`` — return a v0.1 envelope.

The envelope uses ``mode_used="youtube"`` and ``content_type="text/youtube"``.
On any caption/network failure it returns a failure envelope rather than raising.

yt-dlp is imported lazily inside the worker helpers so the module import stays cheap.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import re
import tempfile
import time
from typing import Any

from .. import envelope
from ..cache import YT_VIDEO_ID_RE
from ..errors import FailureReason

log = logging.getLogger(__name__)

# Detector regex — true for any YouTube URL (video, channel, playlist, etc.).
# Video-ID extraction reuses the shared YT_VIDEO_ID_RE from cache.py.
_YOUTUBE_RE = re.compile(
    r"^https?://"
    r"(?:"
    r"(?:[a-z0-9-]+\.)*(?:youtube\.com|youtube-nocookie\.com)(?:\.[a-z]{2,})?"
    r"|(?:www\.)?youtu\.be"
    r")"
    r"(?:/|$)",
    re.IGNORECASE,
)

_VTT_CUE_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->")
_VTT_TAGS_RE = re.compile(r"<[^>]+>")
_VTT_META_RE = re.compile(r"^(WEBVTT|Kind:|Language:)")

_SPONSORBLOCK_URL = "https://sponsor.ajay.app/api/skipSegments"
_SPONSORBLOCK_CATEGORIES = '["sponsor","selfpromo","interaction","intro","outro"]'


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def is_youtube_url(url: str) -> bool:
    return bool(_YOUTUBE_RE.match(url or ""))


def extract_video_id(url: str) -> str | None:
    """Return the YouTube video ID from any supported URL form, or None.

    Covers: ``youtu.be/<id>``, ``<yt-host>/watch?v=<id>``, and ``<yt-host>/
    (embed|shorts|v|live)/<id>`` for every host variant (subdomains, local
    TLDs, ``youtube-nocookie.com``).
    """
    if not url:
        return None
    m = YT_VIDEO_ID_RE.match(url)
    if not m:
        return None
    return m.group("id_short") or m.group("id_query") or m.group("id_path")


def canonical_url(video_id: str) -> str:
    return f"https://youtube.com/watch?v={video_id}"


# ---------------------------------------------------------------------------
# VTT parsing + SponsorBlock filtering (ported from claudinho/process/summarize.py)
# ---------------------------------------------------------------------------


def parse_vtt(content: str) -> list[tuple[float, str]]:
    """Extract ``(start_seconds, text)`` cues from a WebVTT file.

    YouTube auto-captions use a rolling-window format: each regular cue carries
    over the previous sentence plus partial new words tagged with inline
    ``<00:00:01.234>`` timing tags, and short "transition" cues hold one clean
    completed sentence with no tags. We drop the tagged blocks (they would
    duplicate every line several times) and emit only the clean ones.
    """
    cues: list[tuple[float, str]] = []
    current_start: float | None = None
    current_lines: list[str] = []
    current_has_tags: bool = False

    def _flush() -> None:
        nonlocal current_start, current_lines, current_has_tags
        if current_start is None or not current_lines or current_has_tags:
            return
        deduped: list[str] = []
        for ln in current_lines:
            if not deduped or ln != deduped[-1]:
                deduped.append(ln)
        text = " ".join(deduped)
        if text:
            cues.append((current_start, text))

    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            _flush()
            current_start = None
            current_lines = []
            current_has_tags = False
            continue
        if _VTT_META_RE.match(line) or line.isdigit():
            continue
        m = _VTT_CUE_RE.match(line)
        if m:
            _flush()
            h, mn, s, ms = (
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
                int(m.group(4)),
            )
            current_start = h * 3600 + mn * 60 + s + ms / 1000
            current_lines = []
            current_has_tags = False
            continue
        if "<" in line:
            current_has_tags = True
        cleaned = _VTT_TAGS_RE.sub("", line).strip()
        if cleaned:
            current_lines.append(cleaned)

    _flush()
    return cues


def apply_sponsorblock(
    cues: list[tuple[float, str]], segments: list[dict[str, Any]]
) -> list[tuple[float, str]]:
    """Drop cues whose start time falls inside any SponsorBlock segment."""
    if not segments:
        return cues
    blocked: list[tuple[float, float]] = []
    for seg in segments:
        rng = seg.get("segment")
        if isinstance(rng, list) and len(rng) == 2:
            blocked.append((float(rng[0]), float(rng[1])))
    if not blocked:
        return cues
    return [
        (t, text)
        for t, text in cues
        if not any(start <= t < end for start, end in blocked)
    ]


def cues_to_text(cues: list[tuple[float, str]]) -> str:
    """Join cue texts with consecutive-duplicate suppression."""
    deduped: list[str] = []
    for _, text in cues:
        if not deduped or text != deduped[-1]:
            deduped.append(text)
    return " ".join(deduped)


# ---------------------------------------------------------------------------
# yt-dlp + SponsorBlock workers (lazy imports kept inside)
# ---------------------------------------------------------------------------


def _ytdlp_base_opts(cfg: Any | None) -> dict[str, Any]:
    """Build the common yt-dlp options dict, threading in browser cookies if configured.

    yt-dlp's ``cookiesfrombrowser`` expects a tuple
    ``(browser_name, profile, keyring, container)`` — we only set the browser name,
    leaving the rest defaulted. Reads the user's real on-disk browser profile, not
    Camoufox.
    """
    # `extractor_retries=0`: yt-dlp's default of 3 retries is wasteful for
    # extractor errors, which are almost always terminal (login_required,
    # private, age-gated, removed). Network-level retries (`retries`) are
    # untouched — those still benefit from yt-dlp's default backoff.
    opts: dict[str, Any] = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "extractor_retries": 0,
    }
    browser = ""
    if cfg is not None:
        browser = (
            (getattr(getattr(cfg, "youtube", None), "cookies_from_browser", "") or "")
            .strip()
            .lower()
        )
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


def _extract_info(url: str, cfg: Any | None) -> dict[str, Any]:
    import yt_dlp

    with yt_dlp.YoutubeDL(_ytdlp_base_opts(cfg)) as ydl:
        return ydl.extract_info(url, download=False) or {}


def _download_vtt(url: str, lang: str, is_auto: bool, cfg: Any | None) -> str | None:
    import yt_dlp

    with tempfile.TemporaryDirectory() as tmpdir:
        opts = _ytdlp_base_opts(cfg) | {
            "writesubtitles": not is_auto,
            "writeautomaticsub": is_auto,
            "subtitlesformat": "vtt",
            "subtitleslangs": [lang],
            "outtmpl": str(pathlib.Path(tmpdir) / "sub"),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        vtt_files = list(pathlib.Path(tmpdir).glob("*.vtt"))
        return vtt_files[0].read_text() if vtt_files else None


async def _fetch_sponsorblock(video_id: str) -> list[dict[str, Any]] | None:
    """Return SponsorBlock segments, ``[]`` for "no segments", or ``None`` on
    network failure (caller should warn)."""
    import httpx

    url = (
        f"{_SPONSORBLOCK_URL}?videoID={video_id}&categories={_SPONSORBLOCK_CATEGORIES}"
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("SponsorBlock lookup failed for %s: %s", video_id, exc)
        return None


# ---------------------------------------------------------------------------
# Caption-language selection
# ---------------------------------------------------------------------------


def _select_language(
    subs: dict[str, Any], auto: dict[str, Any]
) -> tuple[str | None, bool]:
    """Prefer human subtitles (en → en-orig → any), then auto-captions
    (*-orig → en → any). Returns (lang, is_auto)."""
    for lang in ("en", "en-orig"):
        if lang in subs:
            return lang, False
    for lang in subs:
        return lang, False
    orig_keys = [k for k in auto if k.endswith("-orig")]
    for lang in (*orig_keys, "en"):
        if lang in auto:
            return lang, True
    for lang in auto:
        return lang, True
    return None, False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _base_envelope(url: str, *, http_status: int = 0) -> dict[str, Any]:
    return envelope.base_envelope(
        url_requested=url,
        url_normalized=url,
        url_final=url,
        http_status=http_status,
        mode_used="youtube",
        content_type="text/youtube",
    )


def _failure_envelope(
    url: str, reason: FailureReason, message: str, *, http_status: int = 0
) -> dict[str, Any]:
    return envelope.failure_envelope(
        base=_base_envelope(url, http_status=http_status),
        reason=reason,
        message=message,
    )


def _classify_ytdlp_error(exc: BaseException) -> FailureReason:
    msg = str(exc).lower()
    if "private" in msg or "members-only" in msg or "sign in" in msg or "age" in msg:
        return FailureReason.LOGIN_REQUIRED
    if "unavailable" in msg or "removed" in msg or "404" in msg or "not exist" in msg:
        return FailureReason.NOT_FOUND
    if "timed out" in msg or "timeout" in msg:
        return FailureReason.TIMEOUT
    return FailureReason.SERVER_ERROR


async def fetch_youtube(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """Fetch a YouTube transcript and return a Vasco envelope."""
    deadline_monotonic = time.monotonic() + max(0.0, float(deadline))

    video_id = extract_video_id(url)
    if not video_id:
        return _failure_envelope(
            url, FailureReason.INVALID_URL, "could not parse YouTube video ID"
        )

    # Step 1: metadata + caption listing via yt-dlp.
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return _failure_envelope(
            url, FailureReason.DEADLINE_EXCEEDED, "deadline elapsed before metadata"
        )
    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(_extract_info, url, cfg), timeout=remaining
        )
    except asyncio.TimeoutError:
        return _failure_envelope(url, FailureReason.TIMEOUT, "yt-dlp metadata timeout")
    except Exception as exc:
        return _failure_envelope(
            url, _classify_ytdlp_error(exc), f"{type(exc).__name__}: {exc}"
        )

    title = info.get("title") or None
    uploader = info.get("uploader") or info.get("channel") or None
    upload_date = info.get("upload_date")  # YYYYMMDD
    published = None
    if isinstance(upload_date, str) and len(upload_date) == 8 and upload_date.isdigit():
        published = f"{upload_date[0:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
    duration = info.get("duration")

    selected_lang, is_auto = _select_language(
        info.get("subtitles") or {}, info.get("automatic_captions") or {}
    )
    if not selected_lang:
        return _failure_envelope(
            url, FailureReason.UNSUPPORTED_CONTENT_TYPE, "video has no captions"
        )

    # Step 2: VTT download + SponsorBlock segments concurrently.
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return _failure_envelope(
            url,
            FailureReason.DEADLINE_EXCEEDED,
            "deadline elapsed before caption download",
        )

    try:
        vtt_content, sb_result = await asyncio.wait_for(
            asyncio.gather(
                asyncio.to_thread(_download_vtt, url, selected_lang, is_auto, cfg),
                _fetch_sponsorblock(video_id),
            ),
            timeout=remaining,
        )
    except asyncio.TimeoutError:
        return _failure_envelope(url, FailureReason.TIMEOUT, "caption download timeout")
    except Exception as exc:
        return _failure_envelope(
            url,
            _classify_ytdlp_error(exc),
            f"caption download failed: {type(exc).__name__}: {exc}",
        )

    if not vtt_content:
        return _failure_envelope(
            url, FailureReason.UNSUPPORTED_CONTENT_TYPE, "yt-dlp wrote no VTT file"
        )

    cues = parse_vtt(vtt_content)
    if not cues:
        return _failure_envelope(
            url,
            FailureReason.UNSUPPORTED_CONTENT_TYPE,
            "no text extracted from captions",
        )

    warnings: list[str] = []
    if sb_result is None:
        warnings.append("sponsorblock_unavailable")
    elif sb_result:
        cues = apply_sponsorblock(cues, sb_result)

    transcript = cues_to_text(cues)
    if not transcript:
        return _failure_envelope(
            url,
            FailureReason.UNSUPPORTED_CONTENT_TYPE,
            "no transcript text after filtering",
        )

    from .. import io as io_mod

    return envelope.success_envelope(
        base=_base_envelope(url, http_status=200),
        markdown=transcript,
        metadata={
            "title": title,
            "byline": uploader,
            "published": published,
            "modified": None,
            "language": selected_lang,
            "site_name": "YouTube",
            "word_count": len(transcript.split()),
            "quality": {"video_duration_seconds": int(duration)} if duration else {},
            "warnings": warnings,
        },
        token_count_estimate=io_mod.estimate_tokens(transcript),
    )
