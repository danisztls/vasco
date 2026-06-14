from __future__ import annotations

import pytest

from vasco.adapters import youtube
from vasco.errors import FailureReason


# ---------------------------------------------------------------------------
# URL detection + ID parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, matches",
    [
        ("https://www.youtube.com/watch?v=abc", True),
        ("http://youtube.com/watch?v=abc", True),
        ("https://m.youtube.com/watch?v=abc", True),
        ("https://music.youtube.com/watch?v=abc", True),
        ("https://www.youtube.com.br/watch?v=abc", True),
        ("https://youtu.be/abc", True),
        ("https://www.youtu.be/abc", True),
        ("https://www.youtube.com/shorts/abc", True),
        ("https://www.youtube-nocookie.com/embed/abc", True),
        ("https://youtube-nocookie.com/embed/abc", True),
        ("https://example.com/youtube.com/foo", False),
        ("https://notyoutube.com/foo", False),
        ("https://example.com/", False),
        ("", False),
    ],
)
def test_is_youtube_url(url: str, matches: bool) -> None:
    assert youtube.is_youtube_url(url) is matches


@pytest.mark.parametrize(
    "url, video_id",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=42", "dQw4w9WgXcQ"),
        ("https://www.youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/abcXYZ_-12", "abcXYZ_-12"),
        ("https://www.youtube.com/embed/abcdef", "abcdef"),
        ("https://www.youtube.com/live/livestream1", "livestream1"),
        # Nocookie embeds resolve to the same video ID.
        ("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtube-nocookie.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        # v= after another query param.
        (
            "https://www.youtube.com/watch?list=PL123&v=dQw4w9WgXcQ",
            "dQw4w9WgXcQ",
        ),
        ("https://www.youtube.com/", None),
        ("https://www.youtube.com/watch", None),
        ("https://example.com/foo", None),
        ("", None),
    ],
)
def test_extract_video_id(url: str, video_id: str | None) -> None:
    assert youtube.extract_video_id(url) == video_id


def test_canonical_url() -> None:
    assert youtube.canonical_url("abc") == "https://youtube.com/watch?v=abc"


# ---------------------------------------------------------------------------
# VTT parser
# ---------------------------------------------------------------------------


_SAMPLE_VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.500 align:start position:0%
hello<00:00:01.000><c> world</c>

00:00:02.500 --> 00:00:02.510 align:start position:0%
hello world

00:00:02.510 --> 00:00:05.000 align:start position:0%
hello world
this<00:00:03.000><c> is</c><00:00:04.000><c> a</c><00:00:04.500><c> test</c>

00:00:05.000 --> 00:00:05.010 align:start position:0%
this is a test

00:00:05.010 --> 00:00:07.000 align:start position:0%
this is a test
final<00:00:06.000><c> line</c>

00:00:07.000 --> 00:00:07.010 align:start position:0%
final line
"""


def test_parse_vtt_keeps_only_transition_blocks() -> None:
    cues = youtube.parse_vtt(_SAMPLE_VTT)
    texts = [text for _, text in cues]
    # Three transition blocks: "hello world", "this is a test", "final line"
    assert texts == ["hello world", "this is a test", "final line"]


def test_parse_vtt_dedups_within_block() -> None:
    vtt = """WEBVTT

00:00:00.000 --> 00:00:01.000
same line
same line
same line
"""
    cues = youtube.parse_vtt(vtt)
    assert cues == [(0.0, "same line")]


def test_parse_vtt_empty() -> None:
    assert youtube.parse_vtt("WEBVTT\n\n") == []


def test_cues_to_text_dedups_consecutive() -> None:
    cues = [(0.0, "a"), (1.0, "a"), (2.0, "b"), (3.0, "a")]
    assert youtube.cues_to_text(cues) == "a b a"


# ---------------------------------------------------------------------------
# SponsorBlock filter
# ---------------------------------------------------------------------------


def test_apply_sponsorblock_filters_in_range() -> None:
    cues = [(0.0, "a"), (5.0, "b"), (10.0, "c"), (15.0, "d")]
    segments = [{"segment": [4.0, 11.0]}]
    result = youtube.apply_sponsorblock(cues, segments)
    assert [t for _, t in result] == ["a", "d"]


def test_apply_sponsorblock_empty_segments_noop() -> None:
    cues = [(0.0, "a"), (1.0, "b")]
    assert youtube.apply_sponsorblock(cues, []) == cues


def test_apply_sponsorblock_malformed_segment_ignored() -> None:
    cues = [(0.0, "a"), (5.0, "b")]
    # No "segment" key, or wrong shape — should be skipped, not raise.
    assert (
        youtube.apply_sponsorblock(cues, [{"foo": "bar"}, {"segment": "nope"}]) == cues
    )


# ---------------------------------------------------------------------------
# _select_language — caption-track selection
# ---------------------------------------------------------------------------


def test_select_language_prefers_human_english() -> None:
    assert youtube._select_language({"en": [], "de": []}, {"en": []}) == ("en", False)


def test_select_language_falls_back_to_auto() -> None:
    assert youtube._select_language({}, {"fr-orig": [], "en": []}) == ("fr-orig", True)


def test_select_language_skips_live_chat_for_auto_captions() -> None:
    """``live_chat``/``rechat`` are JSON chat replays yt-dlp files under
    ``subtitles`` — they must not be picked as a transcript source, or
    ``_download_vtt`` writes no ``.vtt`` and we mislabel a captioned video as
    having no usable captions. A video with *only* a chat replay must fall
    through to its real auto-captions (regression: youtube.com/watch?v=wKXgeNwNRJ4)."""
    lang, is_auto = youtube._select_language({"live_chat": []}, {"en": []})
    assert (lang, is_auto) == ("en", True)


def test_select_language_no_tracks_returns_none() -> None:
    assert youtube._select_language({"live_chat": []}, {}) == (None, False)


# ---------------------------------------------------------------------------
# fetch_youtube — end-to-end with mocked workers
# ---------------------------------------------------------------------------


def _patch_workers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    info: dict | None = None,
    vtt: str | None = _SAMPLE_VTT,
    sb: list[dict] | None = None,
    info_exc: Exception | None = None,
    vtt_exc: Exception | None = None,
) -> None:
    def fake_info(url: str, cfg: object | None = None) -> dict:
        if info_exc:
            raise info_exc
        return (
            info
            if info is not None
            else {
                "title": "Test Video",
                "id": "abc123",
                "uploader": "Test Channel",
                "upload_date": "20251101",
                "duration": 123,
                "subtitles": {"en": [{"url": "x"}]},
                "automatic_captions": {},
            }
        )

    def fake_vtt(
        url: str, lang: str, is_auto: bool, cfg: object | None = None
    ) -> str | None:
        if vtt_exc:
            raise vtt_exc
        return vtt

    async def fake_sb(video_id: str) -> list[dict] | None:
        return sb

    monkeypatch.setattr(youtube, "_extract_info", fake_info)
    monkeypatch.setattr(youtube, "_download_vtt", fake_vtt)
    monkeypatch.setattr(youtube, "_fetch_sponsorblock", fake_sb)


@pytest.mark.asyncio
async def test_fetch_youtube_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_workers(monkeypatch, sb=[])
    env = await youtube.fetch_youtube("https://youtu.be/abc123", deadline=10.0)
    assert "failure" not in env
    assert env["mode_used"] == "youtube"
    assert env["content_type"] == "text/youtube"
    assert env["title"] == "Test Video"
    assert env["byline"] == "Test Channel"
    assert env["published"] == "2025-11-01"
    assert env["language"] == "en"
    assert env["site_name"] == "YouTube"
    # fetch_youtube preserves the caller's URL verbatim; canonicalization is
    # applied by fetch.fetch_one (see test_fetch_integration).
    assert env["url_canonical"] == "https://youtu.be/abc123"
    assert env["markdown"] == "hello world this is a test final line"
    assert env["word_count"] == 8
    assert env["quality"] == {"video_duration_seconds": 123}
    assert env["warnings"] == []


@pytest.mark.asyncio
async def test_fetch_youtube_sponsorblock_filters_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Transition-block cues land at t=2.5 ("hello world"), t=5.0 ("this is a test"),
    # and t=7.0 ("final line"). A SponsorBlock range of [4.0, 6.0) drops the
    # middle cue only.
    _patch_workers(monkeypatch, sb=[{"segment": [4.0, 6.0]}])
    env = await youtube.fetch_youtube(
        "https://www.youtube.com/watch?v=abc123", deadline=10.0
    )
    assert "failure" not in env
    assert env["markdown"] == "hello world final line"


@pytest.mark.asyncio
async def test_fetch_youtube_sponsorblock_unavailable_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_workers(monkeypatch, sb=None)  # None means API failed
    env = await youtube.fetch_youtube("https://youtu.be/abc123", deadline=10.0)
    assert "failure" not in env
    assert env["warnings"] == ["sponsorblock_unavailable"]


@pytest.mark.asyncio
async def test_fetch_youtube_invalid_url() -> None:
    env = await youtube.fetch_youtube("https://www.youtube.com/")
    assert env["failure"]["reason"] == FailureReason.INVALID_URL.value


@pytest.mark.asyncio
async def test_fetch_youtube_no_captions(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_workers(
        monkeypatch,
        info={
            "title": "x",
            "id": "abc123",
            "subtitles": {},
            "automatic_captions": {},
        },
        sb=[],
    )
    env = await youtube.fetch_youtube("https://youtu.be/abc123", deadline=10.0)
    assert env["failure"]["reason"] == FailureReason.UNSUPPORTED_CONTENT_TYPE.value
    assert "no captions" in env["failure"]["message"]


@pytest.mark.asyncio
async def test_fetch_youtube_metadata_error_age_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_workers(monkeypatch, info_exc=RuntimeError("Sign in to confirm your age"))
    env = await youtube.fetch_youtube("https://youtu.be/abc123", deadline=10.0)
    assert env["failure"]["reason"] == FailureReason.LOGIN_REQUIRED.value


@pytest.mark.asyncio
async def test_fetch_youtube_metadata_error_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_workers(monkeypatch, info_exc=RuntimeError("Video unavailable"))
    env = await youtube.fetch_youtube("https://youtu.be/abc123", deadline=10.0)
    assert env["failure"]["reason"] == FailureReason.NOT_FOUND.value


@pytest.mark.asyncio
async def test_fetch_youtube_vtt_download_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_workers(monkeypatch, vtt=None, sb=[])
    env = await youtube.fetch_youtube("https://youtu.be/abc123", deadline=10.0)
    assert env["failure"]["reason"] == FailureReason.UNSUPPORTED_CONTENT_TYPE.value


@pytest.mark.asyncio
async def test_fetch_youtube_stale_ytdlp_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'Requested format is not available' is rewritten to name the stale
    yt-dlp cause and the bump command, not left looking like a bad format."""
    _patch_workers(
        monkeypatch,
        info_exc=RuntimeError("ERROR: Requested format is not available"),
    )
    env = await youtube.fetch_youtube("https://youtu.be/abc123", deadline=10.0)
    msg = env["failure"]["message"].lower()
    assert env["failure"]["reason"] == FailureReason.SERVER_ERROR.value
    assert "stale" in msg
    assert "upgrade-package yt-dlp" in msg
    # the original error is preserved for debugging
    assert "requested format is not available" in msg


@pytest.mark.asyncio
async def test_fetch_youtube_auto_captions_used_when_no_subs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_workers(
        monkeypatch,
        info={
            "title": "x",
            "id": "abc123",
            "subtitles": {},
            "automatic_captions": {"en": [{"url": "x"}]},
        },
        sb=[],
    )
    env = await youtube.fetch_youtube("https://youtu.be/abc123", deadline=10.0)
    assert "failure" not in env
    assert env["language"] == "en"


@pytest.mark.asyncio
async def test_fetch_youtube_deadline_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_workers(monkeypatch, sb=[])
    env = await youtube.fetch_youtube("https://youtu.be/abc123", deadline=0.0)
    assert env["failure"]["reason"] == FailureReason.DEADLINE_EXCEEDED.value


# ---------------------------------------------------------------------------
# _ytdlp_base_opts — cookie plumbing
# ---------------------------------------------------------------------------


def test_ytdlp_base_opts_omits_cookies_when_unset() -> None:
    from vasco.config import Config

    assert "cookiesfrombrowser" not in youtube._ytdlp_base_opts(None)
    assert "cookiesfrombrowser" not in youtube._ytdlp_base_opts(Config())


def test_ytdlp_base_opts_disables_extractor_retries() -> None:
    """Extractor failures (login_required, private, removed) are terminal —
    yt-dlp's default 3 retries just wastes time before we surface them."""
    assert youtube._ytdlp_base_opts(None)["extractor_retries"] == 0


def test_ytdlp_base_opts_ignores_no_formats_error() -> None:
    """We only pull subtitles, so a video-format resolution failure (e.g. the JS
    challenge solver is absent) must not abort metadata + caption extraction."""
    assert youtube._ytdlp_base_opts(None)["ignore_no_formats_error"] is True


def test_ytdlp_base_opts_remote_components_off_by_default() -> None:
    """The JS challenge solver fetches+runs remote code, so it's opt-in:
    absent from the opts unless explicitly configured."""
    from vasco.config import Config

    assert "remote_components" not in youtube._ytdlp_base_opts(None)
    assert "remote_components" not in youtube._ytdlp_base_opts(Config())


def test_ytdlp_base_opts_remote_components_opt_in() -> None:
    from vasco.config import AdaptersCfg, Config, YouTubeCfg

    cfg = Config(
        adapters=AdaptersCfg(youtube=YouTubeCfg(remote_components="ejs:github"))
    )
    opts = youtube._ytdlp_base_opts(cfg)
    # yt-dlp wants a list; the colon-bearing token is kept whole, not split
    assert opts["remote_components"] == ["ejs:github"]


def test_ytdlp_base_opts_passes_cookies_from_browser() -> None:
    from vasco.config import AdaptersCfg, Config, YouTubeCfg

    cfg = Config(
        adapters=AdaptersCfg(youtube=YouTubeCfg(cookies_from_browser="Firefox"))
    )
    opts = youtube._ytdlp_base_opts(cfg)
    # yt-dlp wants a tuple, lowercased
    assert opts["cookiesfrombrowser"] == ("firefox",)
    # Other base options are preserved
    assert opts["skip_download"] is True
    assert opts["quiet"] is True
