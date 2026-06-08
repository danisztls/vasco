"""Tests for vasco.fetch.netblock — third-party tracker request interception."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from vasco.fetch import netblock


def _cfg(block_ads: bool = True, paths: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        browser=SimpleNamespace(
            block_ads=block_ads,
            network_blocklist_paths=tuple(paths),
        )
    )


class TestShouldBlock:
    def test_third_party_tracker_blocked(self):
        bl = frozenset({"tracker.com"})
        assert netblock.should_block("https://tracker.com/t.js", "example.com", bl)
        # subdomains of a listed tracker are blocked too
        assert netblock.should_block("https://cdn.tracker.com/t.js", "example.com", bl)

    def test_first_party_never_blocked(self):
        # The page's own registered domain is listed, yet first-party + same-site
        # CDN requests must still load.
        bl = frozenset({"example.com"})
        assert not netblock.should_block(
            "https://example.com/app.js", "example.com", bl
        )
        assert not netblock.should_block(
            "https://cdn.example.com/app.js", "example.com", bl
        )

    def test_third_party_non_tracker_allowed(self):
        bl = frozenset({"tracker.com"})
        assert not netblock.should_block(
            "https://cdn.jsdelivr.net/x.js", "example.com", bl
        )

    def test_empty_blocklist_allows_everything(self):
        assert not netblock.should_block(
            "https://tracker.com/t.js", "example.com", frozenset()
        )

    def test_garbage_url_is_safe(self):
        bl = frozenset({"tracker.com"})
        assert not netblock.should_block("not a url", "example.com", bl)
        assert not netblock.should_block("", "example.com", bl)


class TestLoadNetblock:
    def test_disabled_returns_empty(self):
        assert netblock.load_netblock(False, ()) == frozenset()
        assert netblock.load_netblock(False, ("/some/path",)) == frozenset()

    def test_configured_paths_win(self, tmp_path: Path):
        f = tmp_path / "net.txt"
        f.write_text("0.0.0.0 ads.example.com\nplain-tracker.net\n")
        bl = netblock.load_netblock(True, (str(f),))
        assert bl == frozenset({"ads.example.com", "plain-tracker.net"})

    def test_bundled_default_used_when_no_paths(self):
        bl = netblock.load_netblock(True, ())
        assert len(bl) > 100  # the bundled list is non-trivial
        assert "google-analytics.com" in bl


class TestGetNetblock:
    def test_resolution_branches(self, tmp_path: Path):
        netblock.reset()
        assert netblock.get_netblock(_cfg(block_ads=False)) == frozenset()

        netblock.reset()
        f = tmp_path / "net.txt"
        f.write_text("tracker.example\n")
        assert netblock.get_netblock(_cfg(paths=(str(f),))) == frozenset(
            {"tracker.example"}
        )

        netblock.reset()
        assert len(netblock.get_netblock(None)) > 100
        netblock.reset()

    def test_caches_first_result(self):
        netblock.reset()
        first = netblock.get_netblock(_cfg(block_ads=False))
        # A later call with different cfg returns the cached value until reset().
        assert netblock.get_netblock(_cfg(paths=("ignored",))) is first
        netblock.reset()
