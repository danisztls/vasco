"""YouTube adapter: video transcripts + channel/playlist/search video listings.

Public surface:
- ``is_youtube_url(url)`` — match youtube.com (all TLDs), m.youtube.com,
  music.youtube.com, youtu.be (with or without www.).
- ``classify_youtube_url(url)`` — ``video`` / ``channel`` / ``playlist`` /
  ``search`` / ``other``; chooses which branch ``fetch_youtube`` takes.
- ``extract_video_id(url)`` — parse a video ID from any supported URL form.
- ``fetch_youtube(url, *, deadline, cfg=None)`` — return a v0.1 envelope.

The envelope uses ``mode_used="youtube"`` and ``content_type="text/youtube"``.
A *video* URL yields a transcript (or, captionless, its description + metadata);
a *channel*/*playlist*/*search* URL yields a video listing in ``quality.videos``
(mirroring the content-adapter listing convention). On any failure it returns a
failure envelope rather than raising.

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
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .. import envelope
from ..urls import YT_VIDEO_ID_RE
from ..errors import FailureReason
from . import _common

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

# Pseudo-"subtitle" keys yt-dlp lists under ``subtitles`` that are chat replays
# (JSON only), not caption text — never selectable as a transcript source.
_NON_CAPTION_SUBS = frozenset({"live_chat", "rechat"})


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


# Channel path forms: /@handle, /channel/UC..., /c/Name, /user/Name (+ tabs).
_YT_CHANNEL_PATH_RE = re.compile(
    r"^/(?:@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)(?:/|$)",
    re.IGNORECASE,
)

# Channel tab segments that already point at a listing — don't append /videos.
_YT_CHANNEL_TABS = frozenset(
    {
        "videos",
        "shorts",
        "streams",
        "live",
        "playlists",
        "featured",
        "community",
        "about",
    }
)


def classify_youtube_url(url: str) -> str:
    """Classify a YouTube URL into the branch ``fetch_youtube`` should take.

    Returns one of ``video`` / ``channel`` / ``playlist`` / ``search`` /
    ``other``. ``video`` wins whenever a video ID parses (so a
    ``/watch?v=…&list=…`` video-in-playlist stays a transcript, not a listing).
    ``other`` is the homepage/feed/unhandled tail.
    """
    if extract_video_id(url):
        return "video"
    parts = urlsplit(url or "")
    path = (parts.path or "/").rstrip("/") or "/"
    query = parse_qs(parts.query)
    if path == "/results" and query.get("search_query"):
        return "search"
    if path == "/playlist" and query.get("list"):
        return "playlist"
    if _YT_CHANNEL_PATH_RE.match(parts.path or ""):
        return "channel"
    return "other"


def _channel_listing_url(url: str) -> str:
    """Normalize a channel URL to its videos-tab listing.

    A bare channel root (``/@handle``, ``/channel/UC…``) lists *tab playlists*
    under flat extraction, not videos — appending ``/videos`` makes yt-dlp
    return the uploads directly. A URL already on a listing tab (``/videos``,
    ``/streams``, …) is left untouched. Query/fragment are dropped.
    """
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    if segments and segments[-1].lower() in _YT_CHANNEL_TABS:
        new_path = parts.path
    else:
        new_path = parts.path.rstrip("/") + "/videos"
    return urlunsplit((parts.scheme or "https", parts.netloc, new_path, "", ""))


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
    #
    # `ignore_no_formats_error=True`: we only ever pull subtitles (never a video
    # format), but yt-dlp's info extraction otherwise raises "Requested format
    # is not available" when its format/signature solving fails — which it now
    # does whenever the JS-challenge solver (deno/EJS remote-components) is
    # absent, even though captions are fully available. Ignoring the no-formats
    # error lets metadata + caption extraction succeed regardless.
    opts: dict[str, Any] = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "extractor_retries": 0,
        "ignore_no_formats_error": True,
    }
    browser = ""
    components = ""
    if cfg is not None:
        yt = getattr(getattr(cfg, "adapters", None), "youtube", None)
        browser = (getattr(yt, "cookies_from_browser", "") or "").strip().lower()
        components = (getattr(yt, "remote_components", "") or "").strip()
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    # Opt-in (default off): allow yt-dlp to fetch+run its remote JS challenge
    # solver (e.g. "ejs:github"), which resolves YouTube's signed formats under a
    # JS runtime. Off by default because it fetches+executes remote code; the
    # subtitle path stays functional without it via `ignore_no_formats_error`.
    if components:
        opts["remote_components"] = components.split()
    return opts


def _extract_info(url: str, cfg: Any | None) -> dict[str, Any]:
    import yt_dlp

    with yt_dlp.YoutubeDL(_ytdlp_base_opts(cfg)) as ydl:
        return ydl.extract_info(url, download=False) or {}


def _max_videos(cfg: Any | None) -> int:
    if cfg is None:
        return 50
    yt = getattr(getattr(cfg, "adapters", None), "youtube", None)
    return max(1, int(getattr(yt, "max_videos", 50) or 50))


def _extract_flat_info(url: str, cfg: Any | None, limit: int) -> dict[str, Any]:
    """Flat-extract a channel/playlist/search URL into a playlist info dict.

    ``extract_flat="in_playlist"`` lists entries (id/title/url/duration/views)
    without resolving each video — fast, and it sidesteps the per-video
    format/JS-challenge solving that the transcript path has to guard against.
    ``playlistend`` caps how many entries are pulled.
    """
    import yt_dlp

    opts = _ytdlp_base_opts(cfg) | {
        "extract_flat": "in_playlist",
        "playlistend": max(1, int(limit)),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False) or {}


def _fmt_date(yyyymmdd: Any) -> str | None:
    """``YYYYMMDD`` → ``YYYY-MM-DD``; anything else → None."""
    if isinstance(yyyymmdd, str) and len(yyyymmdd) == 8 and yyyymmdd.isdigit():
        return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
    return None


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
    (*-orig → en → any). Returns (lang, is_auto).

    yt-dlp lists ``live_chat`` (and ``rechat``) under ``subtitles``, but those
    are chat-replay tracks served only as JSON — not caption text. Selecting one
    makes ``_download_vtt`` write a ``.live_chat.json`` and no ``.vtt``, which we
    would surface as the misleading "wrote no VTT file". Drop them so a video
    with *only* a chat replay falls through to its real auto-captions.
    """
    subs = {k: v for k, v in subs.items() if k not in _NON_CAPTION_SUBS}
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


_base_envelope, _failure_envelope = _common.envelope_builders("youtube", "text/youtube")


def _classify_ytdlp_error(exc: BaseException) -> FailureReason:
    msg = str(exc).lower()
    if "private" in msg or "members-only" in msg or "sign in" in msg or "age" in msg:
        return FailureReason.LOGIN_REQUIRED
    if "unavailable" in msg or "removed" in msg or "404" in msg or "not exist" in msg:
        return FailureReason.NOT_FOUND
    if "timed out" in msg or "timeout" in msg:
        return FailureReason.TIMEOUT
    return FailureReason.SERVER_ERROR


# yt-dlp error signatures that mean its *extractor* failed to resolve a format
# — never that the caller asked for a bad one. YouTube changes its player/format
# signing on a roughly monthly cadence; an extractor that hasn't caught up (or
# can't run the JS challenge solver) then resolves no formats and surfaces
# "Requested format is not available" (and the siblings below). We pull only
# subtitles, so `_ytdlp_base_opts` sets `ignore_no_formats_error` to suppress
# this in the normal path; these markers are the backstop for when it leaks out
# anyway, rewritten to name the real cause + fix instead of reading like a
# wrong-format request.
_STALE_YTDLP_MARKERS = (
    "requested format is not available",
    "failed to extract any player response",
    "nsig extraction failed",
    "unable to extract player",
)


def _ytdlp_version() -> str:
    try:
        import yt_dlp.version as _v

        return _v.__version__
    except Exception:  # pragma: no cover - version module always present
        return "unknown"


def _ytdlp_failure(url: str, exc: BaseException, *, prefix: str = "") -> dict[str, Any]:
    """Build a failure envelope for a yt-dlp exception.

    Detects the stale-yt-dlp signature and rewrites the message to name the
    real cause (and the one-line fix) so it isn't mistaken for a wrong-format
    or broken-video error. Classification stays ``SERVER_ERROR`` (transient,
    short negative-cache TTL) so it self-heals once yt-dlp is bumped.
    """
    detail = f"{type(exc).__name__}: {exc}"
    if any(m in str(exc).lower() for m in _STALE_YTDLP_MARKERS):
        detail = (
            f"yt-dlp ({_ytdlp_version()}) could not resolve any format — a yt-dlp "
            "extraction failure, not a wrong-format request. Usual cause: a stale "
            "yt-dlp (YouTube breaks it ~monthly) — bump it with "
            "`uv lock --upgrade-package yt-dlp && uv sync`; if it's already "
            "current, enable YouTube's JS challenge solver via "
            "`adapters.youtube.remote_components: ejs:github` (needs a JS runtime "
            "like deno on PATH). "
            f"[original: {detail}]"
        )
    if prefix:
        detail = f"{prefix}: {detail}"
    return _failure_envelope(url, _classify_ytdlp_error(exc), detail)


async def fetch_youtube(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """Route a YouTube URL to the transcript or listing branch.

    A *video* URL returns a transcript (captionless → description + metadata);
    a *channel*/*playlist*/*search* URL returns a video listing. Anything else
    (homepage, feed) is not a fetchable resource → ``INVALID_URL``.
    """
    deadline_monotonic = time.monotonic() + max(0.0, float(deadline))
    kind = classify_youtube_url(url)

    if kind == "video":
        video_id = extract_video_id(url)
        if not video_id:  # pragma: no cover - classify guarantees a video ID here
            return _failure_envelope(
                url, FailureReason.INVALID_URL, "could not parse YouTube video ID"
            )
        return await _fetch_video(
            url, video_id, deadline_monotonic=deadline_monotonic, cfg=cfg
        )

    if kind in ("channel", "playlist", "search"):
        return await _fetch_collection(
            url, kind, deadline_monotonic=deadline_monotonic, cfg=cfg
        )

    return _failure_envelope(
        url,
        FailureReason.INVALID_URL,
        "unsupported YouTube URL — not a video, channel, playlist, or search "
        "result (homepage/feed pages aren't fetchable)",
    )


# ---------------------------------------------------------------------------
# Video transcript branch
# ---------------------------------------------------------------------------


async def _fetch_video(
    url: str,
    video_id: str,
    *,
    deadline_monotonic: float,
    cfg: Any | None,
) -> dict[str, Any]:
    """Fetch a single video's transcript (or, captionless, its metadata)."""
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
        return _ytdlp_failure(url, exc)

    title = info.get("title") or None
    uploader = info.get("uploader") or info.get("channel") or None
    published = _fmt_date(info.get("upload_date"))
    duration = info.get("duration")

    selected_lang, is_auto = _select_language(
        info.get("subtitles") or {}, info.get("automatic_captions") or {}
    )
    if not selected_lang:
        # No captions — fall back to the description + chapters yt-dlp already
        # fetched, rather than failing outright. Only fail when there's nothing.
        return _captionless_video_envelope(
            url, info, title=title, uploader=uploader, published=published
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
        return _ytdlp_failure(url, exc, prefix="caption download failed")

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


def _captionless_video_envelope(
    url: str,
    info: dict[str, Any],
    *,
    title: str | None,
    uploader: str | None,
    published: str | None,
) -> dict[str, Any]:
    """Build a video envelope from description + chapters when there are no
    captions. Fails only when the video carries no usable text at all."""
    description = (info.get("description") or "").strip()
    chapters_md = _render_chapters(info.get("chapters") or [])
    body = "\n\n".join(p for p in (description, chapters_md) if p).strip()
    if not body:
        return _failure_envelope(
            url,
            FailureReason.UNSUPPORTED_CONTENT_TYPE,
            "video has no captions and no description",
        )

    from .. import io as io_mod

    duration = info.get("duration")
    quality = _common.compact(
        {
            "video_duration_seconds": int(duration)
            if isinstance(duration, (int, float))
            else None,
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
        }
    )
    return envelope.success_envelope(
        base=_base_envelope(url, http_status=200),
        markdown=body,
        metadata={
            "title": title,
            "byline": uploader,
            "published": published,
            "modified": None,
            "language": info.get("language") or None,
            "site_name": "YouTube",
            "word_count": len(body.split()),
            "quality": quality,
            "warnings": ["no_captions"],
        },
        token_count_estimate=io_mod.estimate_tokens(body),
    )


# ---------------------------------------------------------------------------
# Channel / playlist / search listing branch
# ---------------------------------------------------------------------------


async def _fetch_collection(
    url: str,
    kind: str,
    *,
    deadline_monotonic: float,
    cfg: Any | None,
) -> dict[str, Any]:
    """Flat-extract a channel/playlist/search URL into a video-listing envelope."""
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return _failure_envelope(
            url, FailureReason.DEADLINE_EXCEEDED, "deadline elapsed before listing"
        )
    limit = _max_videos(cfg)
    target = _channel_listing_url(url) if kind == "channel" else url
    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(_extract_flat_info, target, cfg, limit), timeout=remaining
        )
    except asyncio.TimeoutError:
        return _failure_envelope(url, FailureReason.TIMEOUT, "yt-dlp listing timeout")
    except Exception as exc:
        return _ytdlp_failure(url, exc)

    return _build_collection_envelope(url, kind, info, limit)


def _video_entry(e: dict[str, Any]) -> dict[str, Any]:
    """Normalize one flat-extracted entry into a listing record."""
    vid = e.get("id")
    duration = e.get("duration")
    return _common.compact(
        {
            "id": vid,
            "title": e.get("title"),
            "url": e.get("url")
            or e.get("webpage_url")
            or (canonical_url(vid) if vid else None),
            "duration_seconds": int(duration)
            if isinstance(duration, (int, float))
            else None,
            "view_count": e.get("view_count"),
            "uploader": e.get("uploader") or e.get("channel"),
            "upload_date": _fmt_date(e.get("upload_date")),
        }
    )


def _build_collection_envelope(
    url: str, kind: str, info: dict[str, Any], limit: int
) -> dict[str, Any]:
    """Assemble the listing envelope from a yt-dlp playlist info dict.

    No ``entries`` key means the listing anchor is gone (rot) → ``PARSE_FAILED``;
    an empty ``entries`` is a genuinely empty collection → success + no_results.
    """
    raw_entries = info.get("entries")
    if raw_entries is None:
        return _failure_envelope(
            url,
            FailureReason.PARSE_FAILED,
            f"expected a {kind} video listing but yt-dlp returned no entries",
        )

    videos: list[dict[str, Any]] = []
    for e in raw_entries:
        if not isinstance(e, dict):
            continue
        record = _video_entry(e)
        if record.get("id") or record.get("url"):
            videos.append(record)
        if len(videos) >= limit:
            break

    name = info.get("title") or info.get("channel") or info.get("uploader")
    uploader = info.get("uploader") or info.get("channel")
    markdown = _render_videos_markdown(kind, name, videos)
    quality: dict[str, Any] = {
        "provider": "youtube",
        "page_type": kind,
        "result_count": len(videos),
        "videos": videos,
    }
    if info.get("channel_follower_count"):
        quality["subscriber_count"] = info["channel_follower_count"]

    from .. import io as io_mod

    return envelope.success_envelope(
        base=_base_envelope(url, http_status=200),
        markdown=markdown,
        metadata={
            "title": name or f"YouTube {kind}",
            "byline": uploader,
            "published": None,
            "modified": None,
            "language": None,
            "site_name": "YouTube",
            "word_count": len(markdown.split()),
            "quality": quality,
            "warnings": ["no_results"] if not videos else [],
        },
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _fmt_duration(seconds: Any) -> str | None:
    """Seconds → ``H:MM:SS`` / ``M:SS``; non-numeric or negative → None."""
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _render_chapters(chapters: Any) -> str:
    """Render a yt-dlp chapter list as a Markdown bullet list (empty → "")."""
    if not isinstance(chapters, list):
        return ""
    lines: list[str] = []
    for ch in chapters:
        if not isinstance(ch, dict) or not ch.get("title"):
            continue
        ts = _fmt_duration(ch.get("start_time"))
        lines.append(f"- {ts + ' ' if ts else ''}{ch['title']}")
    return "## Chapters\n" + "\n".join(lines) if lines else ""


def _render_videos_markdown(
    kind: str, name: str | None, videos: list[dict[str, Any]]
) -> str:
    """Render a video listing as a numbered Markdown list."""
    lines = [f"# {name or f'YouTube {kind}'}", ""]
    if not videos:
        lines.append("_No videos found._")
        return "\n".join(lines)
    for i, v in enumerate(videos, 1):
        bits = [v.get("title") or "(untitled)"]
        dur = _fmt_duration(v.get("duration_seconds"))
        if dur:
            bits.append(dur)
        views = v.get("view_count")
        if isinstance(views, int):
            bits.append(f"{views:,} views")
        line = f"{i}. " + " — ".join(bits)
        if v.get("url"):
            line += f"\n   {v['url']}"
        lines.append(line)
    return "\n".join(lines)
