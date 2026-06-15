from __future__ import annotations

import os
from pathlib import Path

import pytest

from vasco.config import (
    AdaptersCfg,
    BrowserCfg,
    Config,
    SteamCfg,
    YouTubeCfg,
    load_config,
)


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
    assert cfg.adapters.youtube.cookies_from_browser == ""


def test_reads_yaml_overrides(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path,
        "fetch:\n  workers: 7\n  deadline_seconds: 22.5\n"
        "adapters:\n  youtube:\n    cookies_from_browser: firefox\n",
    )
    cfg = load_config()
    assert cfg.fetch.workers == 7
    assert cfg.fetch.deadline_seconds == 22.5
    assert cfg.adapters.youtube.cookies_from_browser == "firefox"
    # Untouched sections keep defaults
    assert cfg.search.max_results == 10


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
    monkeypatch.setenv("VASCO_ADAPTERS_YOUTUBE_COOKIES_FROM_BROWSER", "chrome")
    cfg = load_config()
    assert cfg.adapters.youtube == YouTubeCfg(cookies_from_browser="chrome")


def test_adapters_nested_yaml_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # YAML nests under `adapters`; env uses the VASCO_ADAPTERS_<SUB>_<FIELD> prefix.
    _write_yaml(
        tmp_path, "adapters:\n  steam:\n    country: US\n    language: english\n"
    )
    cfg = load_config()
    assert cfg.adapters.steam == SteamCfg(country="US", language="english")

    monkeypatch.setenv("VASCO_ADAPTERS_STEAM_COUNTRY", "PT")
    cfg = load_config()
    # Env overrides the YAML value; the untouched field keeps its YAML value.
    assert cfg.adapters.steam.country == "PT"
    assert cfg.adapters.steam.language == "english"


def test_unknown_section_is_ignored(tmp_path: Path) -> None:
    _write_yaml(tmp_path, "bogus:\n  hello: world\nfetch:\n  workers: 3\n")
    cfg = load_config()
    assert cfg.fetch.workers == 3


def test_answer_defaults() -> None:
    cfg = load_config()
    # No default provider chain — the capability is disabled until configured.
    assert cfg.answer.providers == ()


def test_answer_providers_chain_from_yaml(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path,
        "answer:\n"
        "  providers:\n"
        "    - {provider: claude_cli, model: sonnet}\n"
        "    - {provider: deepseek, model: my-model, api_key: sk-123}\n",
    )
    cfg = load_config()
    assert [p.provider for p in cfg.answer.providers] == ["claude_cli", "deepseek"]
    assert cfg.answer.providers[0].model == "sonnet"  # primary
    assert cfg.answer.providers[1].api_key == "sk-123"  # fallback


def test_answer_env_overrides_to_single_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A config chain is fully replaced by the VASCO_ANSWER_* single-provider env.
    _write_yaml(
        tmp_path,
        "answer:\n  providers:\n    - {provider: deepseek, model: cfg-model}\n",
    )
    monkeypatch.setenv("VASCO_ANSWER_PROVIDER", "claude_cli")
    monkeypatch.setenv("VASCO_ANSWER_MODEL", "env-model")
    cfg = load_config()
    assert len(cfg.answer.providers) == 1
    assert cfg.answer.providers[0].provider == "claude_cli"
    assert cfg.answer.providers[0].model == "env-model"


def test_browser_user_data_dir_from_yaml(tmp_path: Path) -> None:
    _write_yaml(tmp_path, "browser:\n  user_data_dir: /var/lib/vasco/profile\n")
    cfg = load_config()
    assert cfg.browser == BrowserCfg(user_data_dir="/var/lib/vasco/profile")


def test_browser_user_data_dir_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VASCO_BROWSER_USER_DATA_DIR", "/tmp/vasco-profile")
    cfg = load_config()
    assert cfg.browser.user_data_dir == "/tmp/vasco-profile"


def test_template_shows_real_defaults(tmp_path: Path) -> None:
    """config.yaml.template must display the *real* dataclass defaults: de-commenting
    every config line and loading it must round-trip to Config(). Guards the "show
    defaults" contract against drift when a field is added/renamed."""
    import re

    import yaml

    template = Path(__file__).resolve().parents[1] / "config.yaml.template"
    cfg_line = re.compile(r"^\s*(?:[a-z_][a-z0-9_]*\s*:|- )")
    lines = []
    in_domains = False  # `domains:` is a free-form example map, not defaults
    for raw in template.read_text(encoding="utf-8").splitlines():
        if not raw.startswith("#"):
            continue  # blank separator lines
        body = raw[1:]
        if body.startswith(" "):
            body = body[1:]
        if not cfg_line.match(body):  # prose, not a config line
            continue
        if not body.startswith(" "):  # a top-level section header
            in_domains = body.split(":", 1)[0].strip() == "domains"
            lines.append(body)  # keep `domains:` itself (an empty default map)
        elif not in_domains:  # skip the illustrative domains: subtree
            lines.append(body)
    decommented = "\n".join(lines) + "\n"

    # Sanity: every section is present (catches a section silently dropped).
    parsed = yaml.safe_load(decommented)
    assert set(parsed) >= set(Config().__dataclass_fields__)
    assert set(parsed["adapters"]) == set(AdaptersCfg().__dataclass_fields__)

    _write_yaml(tmp_path, decommented)
    assert load_config() == Config()
