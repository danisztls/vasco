from __future__ import annotations

from pathlib import Path

import pytest

from vasco import strategy
from vasco.cache import Cache
from vasco.config import Config, DomainCfg, load_config
from vasco.fetch import core


# --- header builders --------------------------------------------------------


def test_browser_profile_has_full_chrome_shape() -> None:
    h = core._build_request_headers("browser", None)
    assert "Sec-Fetch-Mode" in h and "Upgrade-Insecure-Requests" in h
    assert h["Accept-Language"]


def test_honest_profile_is_minimal_and_no_sec_fetch() -> None:
    h = core._build_request_headers("honest", None)
    assert "Sec-Fetch-Mode" not in h
    assert "Upgrade-Insecure-Requests" not in h
    assert h["User-Agent"] == core._HONEST_USER_AGENT


def test_browser_profile_uses_configured_ua_but_honest_does_not() -> None:
    from vasco.config import FetchCfg

    cfg = Config(fetch=FetchCfg(user_agent="CustomUA/9"))
    assert core._build_request_headers("browser", cfg)["User-Agent"] == "CustomUA/9"
    # honest deliberately ignores the (Chrome-default) configured UA
    assert (
        core._build_request_headers("honest", cfg)["User-Agent"]
        == core._HONEST_USER_AGENT
    )


# --- seed -------------------------------------------------------------------


@pytest.mark.parametrize(
    "host,expected",
    [
        ("gitlab.wikimedia.org", "honest"),
        ("gitlab.com", "honest"),
        ("www.gitlab.com", "honest"),  # registered-domain match
        ("salsa.debian.org", "honest"),
        ("example.com", None),
        ("", None),
    ],
)
def test_seed_header_profile(host: str, expected: str | None) -> None:
    assert strategy.seed_header_profile(host) == expected


# --- resolver precedence ----------------------------------------------------


def test_resolve_default_is_browser() -> None:
    assert (
        core._resolve_header_profile(
            "https://plain.example/a/b", "plain.example/a", None, Config()
        )
        == "browser"
    )


def test_resolve_seed_applies() -> None:
    assert (
        core._resolve_header_profile(
            "https://gitlab.wikimedia.org/x/y", "wikimedia.org/x", None, Config()
        )
        == "honest"
    )


def test_resolve_user_config_wins_over_seed_and_default() -> None:
    cfg = Config(domains=(DomainCfg(host="news.example", headers="honest"),))
    assert (
        core._resolve_header_profile(
            "https://news.example/a/b", "news.example/a", None, cfg
        )
        == "honest"
    )
    # exact host beats a registered-domain rule
    cfg2 = Config(
        domains=(
            DomainCfg(host="example.com", headers="honest"),
            DomainCfg(host="api.example.com", headers="browser"),
        )
    )
    assert (
        core._resolve_header_profile(
            "https://api.example.com/x/y", "example.com/x", None, cfg2
        )
        == "browser"
    )


def test_resolve_learned_beats_seed_absent(tmp_path: Path) -> None:
    cache = Cache(str(tmp_path / "c.db"))
    try:
        cache.set_header_profile("plain.example/a", "honest")
        assert (
            core._resolve_header_profile(
                "https://plain.example/a/b", "plain.example/a", cache, Config()
            )
            == "honest"
        )
    finally:
        cache.close()


def test_resolve_user_config_wins_over_learned(tmp_path: Path) -> None:
    cache = Cache(str(tmp_path / "c.db"))
    try:
        cache.set_header_profile("x.example/a", "honest")
        cfg = Config(domains=(DomainCfg(host="x.example", headers="browser"),))
        assert (
            core._resolve_header_profile(
                "https://x.example/a/b", "x.example/a", cache, cfg
            )
            == "browser"
        )
    finally:
        cache.close()


# --- cache roundtrip --------------------------------------------------------


def test_header_profile_cache_roundtrip(tmp_path: Path) -> None:
    db = str(tmp_path / "c.db")
    cache = Cache(db)
    try:
        assert cache.get_header_profile("r/k") is None
        cache.set_header_profile("r/k", "honest")
        assert cache.get_header_profile("r/k") == "honest"
    finally:
        cache.close()
    # survives reopen (table migration on an existing DB)
    cache2 = Cache(db)
    try:
        assert cache2.get_header_profile("r/k") == "honest"
    finally:
        cache2.close()


# --- config loading ---------------------------------------------------------


def test_load_domains_map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_dir = tmp_path / "vasco"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(
        "domains:\n"
        "  gitlab.wikimedia.org:\n"
        "    headers: honest\n"
        "  shop.example: honest\n"  # shorthand
        "  bad.example:\n"
        "    headers: nonsense\n",  # invalid → skipped
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = load_config()
    hosts = {d.host: d.headers for d in cfg.domains}
    assert hosts.get("gitlab.wikimedia.org") == "honest"
    assert hosts.get("shop.example") == "honest"
    assert "bad.example" not in hosts  # invalid headers value dropped
