from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any


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
    deadline_seconds: float = 15.0
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )


@dataclass(frozen=True)
class BrowserCfg:
    headless: bool = True
    locale: str = "en-US"


@dataclass(frozen=True)
class CacheCfg:
    path: str = ""


@dataclass(frozen=True)
class Config:
    search: SearchCfg = field(default_factory=SearchCfg)
    fetch: FetchCfg = field(default_factory=FetchCfg)
    browser: BrowserCfg = field(default_factory=BrowserCfg)
    cache: CacheCfg = field(default_factory=CacheCfg)


_SECTIONS: dict[str, type] = {
    "search": SearchCfg,
    "fetch": FetchCfg,
    "browser": BrowserCfg,
    "cache": CacheCfg,
}


def _config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "vasco" / "config.toml"


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


def _apply_dict(section: Any, data: dict[str, Any]) -> Any:
    overrides: dict[str, Any] = {}
    field_types = {f.name: f.type for f in fields(section)}
    for key, val in data.items():
        if key in field_types:
            ftype = field_types[key]
            if isinstance(ftype, str):
                ftype = {"str": str, "int": int, "float": float, "bool": bool}.get(ftype, str)
            overrides[key] = _coerce(val, ftype)
    return replace(section, **overrides) if overrides else section


def _apply_env(section: Any, section_name: str) -> Any:
    prefix = f"VASCO_{section_name.upper()}_"
    overrides: dict[str, Any] = {}
    field_types = {f.name: f.type for f in fields(section)}
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        field_name = env_key[len(prefix) :].lower()
        if field_name in field_types:
            ftype = field_types[field_name]
            if isinstance(ftype, str):
                ftype = {"str": str, "int": int, "float": float, "bool": bool}.get(ftype, str)
            overrides[field_name] = _coerce(env_val, ftype)
    return replace(section, **overrides) if overrides else section


def load_config() -> Config:
    """Load config from $XDG_CONFIG_HOME/vasco/config.toml then apply VASCO_* env overrides.

    Missing file is not an error: defaults are returned. Env var pattern is
    VASCO_<SECTION>_<FIELD>, e.g. VASCO_FETCH_WORKERS=8.
    """
    cfg = Config()
    path = _config_path()
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            data = {}

    sections: dict[str, Any] = {}
    for name, _ in _SECTIONS.items():
        current = getattr(cfg, name)
        section_data = data.get(name, {})
        if isinstance(section_data, dict) and section_data:
            current = _apply_dict(current, section_data)
        current = _apply_env(current, name)
        sections[name] = current

    return Config(**sections)
