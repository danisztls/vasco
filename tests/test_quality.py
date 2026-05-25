"""Tests for vasco.quality — heuristics, blocklist, and integration."""

from __future__ import annotations

from pathlib import Path

from vasco.quality import blocklist, heuristics, score
from vasco.quality.heuristics import HeuristicSignals


# ── Heuristics ──────────────────────────────────────────────────────


HUMAN_TEXT = """\
The quick brown fox jumps over the lazy dog. She walked to the store
and bought some milk. Yesterday was quite cold, but today the sun is
shining through thin clouds.

I called my friend about the concert tickets. He said they were sold
out already, which was disappointing. We ended up watching a movie
at home instead — not a bad evening at all.

The old house on the corner has been vacant for three years now. Nobody
knows who owns it. Some kids say it's haunted, but I think the roof
just leaks too much for anyone to want to live there.

My garden needs weeding again. The tomatoes are coming in nicely though,
and the basil smells incredible when you brush against it. I should
make pesto this weekend.
"""

SLOP_TEXT = """\
In today's fast-paced world, it's important to note that the landscape
of digital transformation is multifaceted and ever-evolving. Let's delve
into the intricacies of this pivotal paradigm shift.

Furthermore, leveraging cutting-edge synergies can facilitate
groundbreaking outcomes. The tapestry of modern innovation encompasses
a myriad of holistic approaches that underscore the importance of
streamlining your endeavors.

Moreover, harnessing the interplay between these multifaceted elements
serves as a testament to the overarching vision. It is crucial to
navigate the complexities of this landscape with meticulous attention.

Additionally, the cornerstone of any comprehensive guide to this realm
is understanding how to utilize these tools effectively. This isn't just
about technology — it's about fostering a wholesome ecosystem that
resonates with stakeholders at every level.

Consequently, by embarking on this transformative journey, you can unlock
the potential of these revolutionary approaches and elevate your
organization to unprecedented heights.
"""


class TestHeuristics:
    def test_human_text_low_slop(self):
        signals = heuristics.compute(HUMAN_TEXT)
        score = heuristics.composite_score(signals)
        assert score < 0.2, f"Human text scored {score}, expected < 0.2"

    def test_slop_text_high_slop(self):
        signals = heuristics.compute(SLOP_TEXT)
        score = heuristics.composite_score(signals)
        assert score > 0.5, f"Slop text scored {score}, expected > 0.5"

    def test_slop_vocab_detected(self):
        signals = heuristics.compute(SLOP_TEXT)
        assert signals.slop_vocab_ratio > 0.02
        assert signals.slop_phrase_count >= 3

    def test_human_vocab_clean(self):
        signals = heuristics.compute(HUMAN_TEXT)
        assert signals.slop_vocab_ratio < 0.005
        assert signals.slop_phrase_count == 0

    def test_short_text_neutral(self):
        signals = heuristics.compute("Hello world.")
        assert signals == HeuristicSignals(
            slop_vocab_ratio=0.0,
            slop_phrase_count=0,
            sentence_length_cv=0.5,
            em_dash_density=0.0,
            transition_start_ratio=0.0,
            type_token_ratio=1.0,
        )

    def test_empty_text(self):
        signals = heuristics.compute("")
        score = heuristics.composite_score(signals)
        assert score < 0.2

    def test_sentence_cv_uniform_text(self):
        # Sentences of nearly identical length → low CV
        uniform = ". ".join(["The cat sat on the mat"] * 20) + "."
        signals = heuristics.compute(uniform)
        assert signals.sentence_length_cv < 0.15

    def test_transition_starts(self):
        text = "\n\n".join(
            [
                "Furthermore, this is a point about something.",
                "Moreover, here is another paragraph with words in it.",
                "Additionally, we should consider the implications of this.",
                "Consequently, the outcome was not what we expected today.",
                "Normal paragraph with no transition word at the start.",
            ]
        )
        signals = heuristics.compute(text)
        assert signals.transition_start_ratio >= 0.6

    def test_em_dash_density(self):
        text = "This — is — a — text — with — many — em — dashes — everywhere. " * 10
        signals = heuristics.compute(text)
        assert signals.em_dash_density > 0.01


# ── Blocklist ───────────────────────────────────────────────────────


class TestBlocklist:
    def test_parse_plain_domain(self):
        assert blocklist._parse_line("example.com") == "example.com"
        assert blocklist._parse_line("  EXAMPLE.COM  ") == "example.com"

    def test_parse_ublacklist_pattern(self):
        assert blocklist._parse_line("*://*.example.com/*") == "example.com"
        assert blocklist._parse_line("*://example.com/*") == "example.com"

    def test_parse_comment(self):
        assert blocklist._parse_line("# comment") is None
        assert blocklist._parse_line("! ublock comment") is None
        assert blocklist._parse_line("") is None
        assert blocklist._parse_line("   ") is None

    def test_parse_inline_comment(self):
        assert blocklist._parse_line("example.com # known spam") == "example.com"

    def test_parse_rejects_junk(self):
        assert blocklist._parse_line("not a domain") is None
        assert blocklist._parse_line("https://example.com") is None
        assert blocklist._parse_line("localhost") is None

    def test_load_blocklist(self, tmp_path: Path):
        f = tmp_path / "block.txt"
        f.write_text("# Slop domains\nspam.com\nfarm.io\n*://*.junk.net/*\n")
        bl = blocklist.load_blocklist([f])
        assert bl == frozenset({"spam.com", "farm.io", "junk.net"})

    def test_load_missing_file(self, tmp_path: Path):
        bl = blocklist.load_blocklist([tmp_path / "nonexistent.txt"])
        assert bl == frozenset()

    def test_is_blocked_exact(self):
        bl = frozenset({"spam.com"})
        assert blocklist.is_blocked("https://spam.com/article", bl)

    def test_is_blocked_subdomain(self):
        bl = frozenset({"spam.com"})
        assert blocklist.is_blocked("https://www.spam.com/page", bl)
        assert blocklist.is_blocked("https://blog.spam.com/", bl)

    def test_not_blocked(self):
        bl = frozenset({"spam.com"})
        assert not blocklist.is_blocked("https://example.com/", bl)
        assert not blocklist.is_blocked("https://notspam.com/", bl)

    def test_is_blocked_empty_list(self):
        assert not blocklist.is_blocked("https://anything.com/", frozenset())

    def test_singleton_lifecycle(self, tmp_path: Path):
        blocklist.reset()
        f = tmp_path / "list.txt"
        f.write_text("test.com\n")
        bl = blocklist.get_blocklist([f])
        assert "test.com" in bl
        # Second call returns cached instance.
        assert blocklist.get_blocklist() is bl
        blocklist.reset()

    def test_load_multiple_files(self, tmp_path: Path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("spam.com\n")
        f2.write_text("farm.io\n")
        bl = blocklist.load_blocklist([f1, f2])
        assert bl == frozenset({"spam.com", "farm.io"})

    def test_consolidated_cache(self, tmp_path: Path, monkeypatch):
        """Remote sources produce a consolidated cache file."""
        monkeypatch.setattr(blocklist, "_cache_dir", lambda: tmp_path)
        monkeypatch.setattr(
            blocklist, "_fetch_remote", lambda url: "remote1.com\nremote2.com\n"
        )
        bl = blocklist.load_blocklist(["https://example.com/list.txt"])
        assert bl == frozenset({"remote1.com", "remote2.com"})
        # Consolidated file written.
        consolidated = tmp_path / "blocklist.txt"
        assert consolidated.is_file()
        content = consolidated.read_text()
        assert "remote1.com" in content
        assert "remote2.com" in content

    def test_consolidated_reused_when_fresh(self, tmp_path: Path, monkeypatch):
        """Fresh consolidated file is reused without re-fetching."""
        monkeypatch.setattr(blocklist, "_cache_dir", lambda: tmp_path)
        # Write a pre-existing consolidated file.
        consolidated = tmp_path / "blocklist.txt"
        consolidated.write_text("cached.com\n")

        fetch_called = []
        monkeypatch.setattr(
            blocklist,
            "_fetch_remote",
            lambda url: fetch_called.append(url) or "",
        )
        bl = blocklist.load_blocklist(["https://example.com/list.txt"])
        assert bl == frozenset({"cached.com"})
        assert fetch_called == []

    def test_consolidated_refreshed_when_stale(self, tmp_path: Path, monkeypatch):
        """Stale consolidated file triggers re-download."""
        monkeypatch.setattr(blocklist, "_cache_dir", lambda: tmp_path)
        consolidated = tmp_path / "blocklist.txt"
        consolidated.write_text("old.com\n")
        # Make it older than the 7-day refresh interval.
        import os

        old_time = consolidated.stat().st_mtime - 700000
        os.utime(consolidated, (old_time, old_time))

        monkeypatch.setattr(blocklist, "_fetch_remote", lambda url: "new.com\n")
        bl = blocklist.load_blocklist(["https://example.com/list.txt"])
        assert "new.com" in bl
        assert "old.com" not in bl

    def test_mixed_local_and_remote(self, tmp_path: Path, monkeypatch):
        """Local files and remote URLs are merged."""
        monkeypatch.setattr(blocklist, "_cache_dir", lambda: tmp_path)
        monkeypatch.setattr(blocklist, "_fetch_remote", lambda url: "remote.com\n")
        local = tmp_path / "local.txt"
        local.write_text("local.com\n")
        bl = blocklist.load_blocklist([str(local), "https://example.com/remote.txt"])
        assert bl == frozenset({"local.com", "remote.com"})

    def test_refresh_forces_redownload(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(blocklist, "_cache_dir", lambda: tmp_path)
        consolidated = tmp_path / "blocklist.txt"
        consolidated.write_text("stale.com\n")

        monkeypatch.setattr(blocklist, "_fetch_remote", lambda url: "fresh.com\n")
        bl = blocklist.refresh(["https://example.com/list.txt"])
        assert "fresh.com" in bl
        assert "stale.com" not in bl

    def test_deduplication(self, tmp_path: Path, monkeypatch):
        """Domains from multiple sources are deduplicated in consolidated file."""
        monkeypatch.setattr(blocklist, "_cache_dir", lambda: tmp_path)
        monkeypatch.setattr(
            blocklist, "_fetch_remote", lambda url: "dupe.com\nother.com\n"
        )
        local = tmp_path / "local.txt"
        local.write_text("dupe.com\nlocal.com\n")
        bl = blocklist.load_blocklist([str(local), "https://example.com/remote.txt"])
        assert bl == frozenset({"dupe.com", "other.com", "local.com"})
        # Consolidated has no duplicates.
        consolidated = tmp_path / "blocklist.txt"
        lines = [x for x in consolidated.read_text().splitlines() if x.strip()]
        assert len(lines) == len(set(lines))


# ── Integration (score function) ────────────────────────────────────


class TestScore:
    def setup_method(self):
        blocklist.reset()

    def test_score_returns_all_fields(self):
        result = score("Some text here. " * 50)
        assert "slop_score" in result
        assert "domain_flagged" in result
        assert "signals" in result
        assert "classifier_quality" in result
        assert isinstance(result["slop_score"], float)
        assert isinstance(result["domain_flagged"], bool)

    def test_score_with_blocked_domain(self, tmp_path: Path):
        from vasco.config import QualityCfg

        f = tmp_path / "block.txt"
        f.write_text("slop.example.com\n")
        cfg = QualityCfg(blocklist_paths=(str(f),))
        result = score(
            "Normal content here.",
            url="https://slop.example.com/article",
            cfg=cfg,
        )
        assert result["domain_flagged"] is True

    def test_score_no_config(self):
        result = score(HUMAN_TEXT)
        assert result["slop_score"] < 0.2
        assert result["domain_flagged"] is False
        assert result["classifier_quality"] is None

    def test_slop_detection_end_to_end(self):
        result = score(SLOP_TEXT)
        assert result["slop_score"] > 0.5
