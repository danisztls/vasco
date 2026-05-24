from __future__ import annotations

import os
from pathlib import Path

import pytest

from vasco.config import BrowserCfg, Config, YouTubeCfg, load_config


def _write_yaml(tmp_path: Path, body: str) -> Path:
    cfg_dir = tmp_path / "vasco"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text(body, encoding="utf-8")
    return cfg_path


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pin XDG_CONFIG_HOME to tmp_path and strip stray VASCO_* env vars so the
    process environment can't leak into these tests."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for key in list(os.environ):
        if key.startswith("VASCO_"):
            monkeypatch.delenv(key, raising=False)


def test_missing_file_returns_defaults() -> None:
    cfg = load_config()
    assert cfg == Config()
    assert cfg.fetch.workers == 4
    assert cfg.youtube.cookies_from_browser == ""


def test_reads_yaml_overrides(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path,
        "fetch:\n  workers: 7\n  deadline_seconds: 22.5\nyoutube:\n  cookies_from_browser: firefox\n",
    )
    cfg = load_config()
    assert cfg.fetch.workers == 7
    assert cfg.fetch.deadline_seconds == 22.5
    assert cfg.youtube.cookies_from_browser == "firefox"
    # Untouched sections keep defaults
    assert cfg.search.default_backend == "ddg"


def test_malformed_yaml_falls_back_to_defaults(tmp_path: Path) -> None:
    _write_yaml(tmp_path, ":::\n- not: [valid")
    cfg = load_config()
    assert cfg == Config()


def test_non_mapping_top_level_falls_back_to_defaults(tmp_path: Path) -> None:
    # A bare string parses successfully but isn't a mapping; loader must ignore it.
    _write_yaml(tmp_path, "just a string")
    cfg = load_config()
    assert cfg == Config()


def test_env_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_yaml(tmp_path, "fetch:\n  workers: 4\n")
    monkeypatch.setenv("VASCO_FETCH_WORKERS", "9")
    cfg = load_config()
    assert cfg.fetch.workers == 9


def test_youtube_cookies_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VASCO_YOUTUBE_COOKIES_FROM_BROWSER", "chrome")
    cfg = load_config()
    assert cfg.youtube == YouTubeCfg(cookies_from_browser="chrome")


def test_unknown_section_is_ignored(tmp_path: Path) -> None:
    _write_yaml(tmp_path, "bogus:\n  hello: world\nfetch:\n  workers: 3\n")
    cfg = load_config()
    assert cfg.fetch.workers == 3


def test_browser_user_data_dir_from_yaml(tmp_path: Path) -> None:
    _write_yaml(tmp_path, "browser:\n  user_data_dir: /var/lib/vasco/profile\n")
    cfg = load_config()
    assert cfg.browser == BrowserCfg(user_data_dir="/var/lib/vasco/profile")


def test_browser_user_data_dir_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VASCO_BROWSER_USER_DATA_DIR", "/tmp/vasco-profile")
    cfg = load_config()
    assert cfg.browser.user_data_dir == "/tmp/vasco-profile"
