from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class SearchCfg:
    default_backend: str = "ddg"
    region: str = "us-en"
    max_results: int = 10


@dataclass(frozen=True)
class FetchCfg:
    default_mode: str = "auto"
    workers: int = 4
    ttl_seconds: int = 86400
    failure_ttl_seconds: int = 900
    deadline_seconds: float = 30.0
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )


@dataclass(frozen=True)
class BrowserCfg:
    headless: bool = True
    locale: str = "en-US"
    prewarm: bool = False
    user_data_dir: str = ""  # "" disables persistent profile
    block_trackers: bool = True  # abort third-party tracker/ad requests in-browser
    # Hostlists for tracker blocking (local files or remote URLs); empty uses
    # the bundled conservative default.
    network_blocklist_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class CacheCfg:
    path: str = ""


@dataclass(frozen=True)
class TavilyCfg:
    api_key: str = ""


@dataclass(frozen=True)
class LoggingCfg:
    enabled: bool = True
    path: str = ""  # empty → $XDG_DATA_HOME/vasco/logs


@dataclass(frozen=True)
class YouTubeCfg:
    cookies_from_browser: str = ""  # "" disables; e.g. "firefox", "chrome", "brave"


@dataclass(frozen=True)
class WikimediaCfg:
    username: str = ""
    password: str = ""


@dataclass(frozen=True)
class QualityCfg:
    blocklist_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnswerCfg:
    """LLM endpoint used by the `answer` command (fetch + LLM answer over a page)."""

    model: str = "deepseek-v4-flash"
    base_url: str = "https://api.deepseek.com/v1"  # any OpenAI-compatible endpoint
    api_key: str = ""  # or DEEPSEEK_API_KEY / VASCO_ANSWER_API_KEY


@dataclass(frozen=True)
class ShoppingCfg:
    """Labels stamped on the Google Shopping envelope. The parser itself only
    understands PT-BR pages; these just control how the result is labelled."""

    currency: str = "BRL"
    language: str = "pt-BR"


@dataclass(frozen=True)
class ServiceCfg:
    """vascod (`vasco serve`) coordination knobs. Single-flight is pure upside
    and on by default; per-domain rate limiting is an opt-in politeness policy
    (0 disables it — no added latency unless asked)."""

    single_flight: bool = True
    rate_limit_rps: float = (
        0.0  # max network fetches/sec per registered domain; 0 = off
    )


@dataclass(frozen=True)
class Config:
    search: SearchCfg = field(default_factory=SearchCfg)
    fetch: FetchCfg = field(default_factory=FetchCfg)
    browser: BrowserCfg = field(default_factory=BrowserCfg)
    cache: CacheCfg = field(default_factory=CacheCfg)
    tavily: TavilyCfg = field(default_factory=TavilyCfg)
    logging: LoggingCfg = field(default_factory=LoggingCfg)
    youtube: YouTubeCfg = field(default_factory=YouTubeCfg)
    wikimedia: WikimediaCfg = field(default_factory=WikimediaCfg)
    quality: QualityCfg | None = field(default_factory=QualityCfg)
    answer: AnswerCfg = field(default_factory=AnswerCfg)
    shopping: ShoppingCfg = field(default_factory=ShoppingCfg)
    service: ServiceCfg = field(default_factory=ServiceCfg)


_SECTIONS: dict[str, type] = {
    "search": SearchCfg,
    "fetch": FetchCfg,
    "browser": BrowserCfg,
    "cache": CacheCfg,
    "tavily": TavilyCfg,
    "logging": LoggingCfg,
    "youtube": YouTubeCfg,
    "wikimedia": WikimediaCfg,
    "quality": QualityCfg,
    "answer": AnswerCfg,
    "shopping": ShoppingCfg,
    "service": ServiceCfg,
}


def _config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "vasco" / "config.yaml"


def _coerce(value: Any, target_type: type) -> Any:
    if target_type is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    if target_type is str:
        return str(value)
    return value


def _coerce_value(current: Any, ftype: Any, raw: Any) -> Any:
    """Coerce one override value to a field's type.

    `raw` is a parsed YAML value or an env-var string. Tuple fields accept a
    list (from YAML) or a colon-separated string (from an env var)."""
    if isinstance(current, tuple):
        if isinstance(raw, str):
            return tuple(s.strip() for s in raw.split(":") if s.strip())
        return tuple(raw)
    if isinstance(ftype, str):  # forward-ref (PEP 563) — resolve the common scalars
        ftype = {"str": str, "int": int, "float": float, "bool": bool}.get(ftype, str)
    return _coerce(raw, ftype)


def _apply_overrides(section: Any, raw_values: dict[str, Any]) -> Any:
    """Apply a {field_name: raw_value} mapping onto a config section. Shared by
    the YAML and env-var paths — they differ only in how they gather the map."""
    field_types = {f.name: f.type for f in fields(section)}
    overrides = {
        name: _coerce_value(getattr(section, name), field_types[name], raw)
        for name, raw in raw_values.items()
        if name in field_types
    }
    return replace(section, **overrides) if overrides else section


def _apply_env(section: Any, section_name: str) -> Any:
    prefix = f"VASCO_{section_name.upper()}_"
    raw_values = {
        env_key[len(prefix) :].lower(): env_val
        for env_key, env_val in os.environ.items()
        if env_key.startswith(prefix)
    }
    return _apply_overrides(section, raw_values)


def load_config() -> Config:
    """Load config from $XDG_CONFIG_HOME/vasco/config.yaml then apply VASCO_* env overrides.

    Missing file is not an error: defaults are returned. Env var pattern is
    VASCO_<SECTION>_<FIELD>, e.g. VASCO_FETCH_WORKERS=8.
    """
    cfg = Config()
    path = _config_path()
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            with path.open("r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError):
            loaded = None
        if isinstance(loaded, dict):
            data = loaded

    sections: dict[str, Any] = {}
    for name, _ in _SECTIONS.items():
        current = getattr(cfg, name)
        section_data = data.get(name, {})
        if isinstance(section_data, dict) and section_data:
            current = _apply_overrides(current, section_data)
        current = _apply_env(current, name)
        sections[name] = current

    # quality: None / false in YAML disables scoring entirely.
    quality_raw = data.get("quality")
    if quality_raw is not None and not quality_raw:
        sections["quality"] = None
    if os.environ.get("VASCO_QUALITY_ENABLED", "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        sections["quality"] = None

    return Config(**sections)
