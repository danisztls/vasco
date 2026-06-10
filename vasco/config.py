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
    # Camoufox OS persona for the fingerprint (windows/macos/linux). Empty = match
    # the host OS, so the spoofed fonts/UA/platform line up with what's actually
    # installed instead of claiming e.g. Windows on a Linux box (a coherence tell).
    spoof_os: str = ""
    prewarm: bool = False
    user_data_dir: str = ""  # "" disables persistent profile
    block_ads: bool = True  # abort third-party ad/tracker requests to cut page weight (faster renders)
    # Hostlists for ad/tracker blocking (local files or remote URLs); empty uses
    # the bundled conservative default.
    network_blocklist_paths: tuple[str, ...] = ()
    # On a login wall, clear that domain's cookies and retry once (single-shot,
    # cooldown-guarded). Only acts on a persistent profile; domain-scoped so other
    # sites' clearances (e.g. AliExpress x5secdata) are preserved.
    clear_cookies_on_wall: bool = True
    # --- Cloudflare Turnstile solving (opt-in; off preserves today's behavior) --
    # virtual_display needs Xvfb installed on the host; it launches a *real*
    # (non-headless) Firefox inside an in-memory X display so headless detection
    # goes away while still running on a headless box.
    virtual_display: bool = False  # launch with headless="virtual" (Xvfb)
    humanize: bool = False  # human-like (Bezier) cursor movement; needed to click
    disable_coop: bool = False  # allow clicking the cross-origin Turnstile iframe
    block_images: bool = False  # skip image loading to cut headful render cost
    window: tuple[int, int] = ()  # fixed window size; () = Camoufox random
    solve_turnstile: bool = False  # attempt the in-page challenge solve
    # --- Manual (human-in-the-loop) captcha solving via VNC ---------------------
    # When on: vasco runs its own sized Xvfb + x11vnc (loopback), and a challenge
    # the auto-solve can't clear escalates to a notify-send + a budget-suspended
    # hold so you can VNC in and solve it by hand. Implies the headful path.
    manual_solve: bool = False
    manual_solve_timeout: float = 60.0  # human window before resuming as normal
    vnc_display_size: tuple[int, int] = (1280, 720)  # managed Xvfb framebuffer
    vnc_display: str = ":99"  # managed Xvfb display number (auto-fallback if taken)
    vnc_port: int = 5900  # x11vnc loopback port


@dataclass(frozen=True)
class CacheCfg:
    path: str = ""


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
    detect_paywall: bool = True  # flag pages served by known paywall/metering vendors
    # Vendor fingerprint lists (local files or remote URLs); empty uses the
    # bundled default. Detection only — vasco never bypasses paywalls.
    paywall_vendor_paths: tuple[str, ...] = ()


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
class AliExpressCfg:
    """AliExpress adapter knobs. `currency`/`language`/`country` label the envelope
    and drive the reviews endpoint locale (note: `language` is the AliExpress
    pt_BR-style code with an underscore). `reviews_page_size` caps how many top
    reviews a product page fetches from feedback.aliexpress.com."""

    currency: str = "BRL"
    language: str = "pt_BR"
    country: str = "BR"
    reviews_page_size: int = 6


@dataclass(frozen=True)
class MercadolivreCfg:
    """MercadoLivre adapter knobs. `relevance_filter` relevance-sorts search
    results so keyword matches rise and off-keyword items (MercadoLivre's
    premium-ad placement) sink. By default off-keyword results are *demoted*
    (sorted to the bottom, not removed) — strict dropping also loses legitimate
    synonym matches (a MacBook/laptop on a "notebook" search). Set
    `drop_off_query` to hard-drop results below `min_query_token_coverage`
    distinct matched query tokens (1 = drop only zero-keyword results)."""

    relevance_filter: bool = True
    drop_off_query: bool = False
    min_query_token_coverage: int = 1


@dataclass(frozen=True)
class ShopifyCfg:
    """Generic Shopify adapter knobs. `domains` extends the built-in known set
    (any registered domain listed here is treated as a Shopify store);
    `autodetect` lets unknown product/collection URLs be probed against the
    platform JSON endpoints; `collection_limit` caps products.json page size."""

    domains: tuple[str, ...] = ()
    autodetect: bool = True
    collection_limit: int = 250


@dataclass(frozen=True)
class ShopeeCfg:
    """Shopee adapter knobs. `currency`/`language` only label the envelope; the
    parser reads the page's own schema.org Product JSON-LD. Scope is product
    pages only (search/category pages carry no embeddable structured data)."""

    currency: str = "BRL"
    language: str = "pt-BR"


@dataclass(frozen=True)
class SteamCfg:
    """Steam store adapter knobs. `country`/`language` are Steam's storefront
    region selectors (`cc`/`l`): they set the price currency and the locale of
    descriptions/genres returned by the JSON APIs. Defaults target Brazil."""

    country: str = "BR"
    language: str = "portuguese"


@dataclass(frozen=True)
class ServiceCfg:
    """vascod (`vasco serve`) coordination knobs. Single-flight is pure upside;
    per-domain pacing (rate limit + concurrency cap) is a politeness/anti-bot
    policy — on by default so a burst at one origin doesn't look like a bot.
    Both pacing knobs gate *before* the per-fetch deadline starts, so queue wait
    never eats a fetch's budget. 0 / non-positive disables a knob."""

    single_flight: bool = True
    rate_limit_rps: float = (
        1.0  # max network fetch *starts*/sec per registered domain; 0 = off
    )
    max_concurrent_per_domain: int = 2  # max simultaneous in-flight network fetches per registered domain; 0 = unlimited


@dataclass(frozen=True)
class Config:
    search: SearchCfg = field(default_factory=SearchCfg)
    fetch: FetchCfg = field(default_factory=FetchCfg)
    browser: BrowserCfg = field(default_factory=BrowserCfg)
    cache: CacheCfg = field(default_factory=CacheCfg)
    logging: LoggingCfg = field(default_factory=LoggingCfg)
    youtube: YouTubeCfg = field(default_factory=YouTubeCfg)
    wikimedia: WikimediaCfg = field(default_factory=WikimediaCfg)
    quality: QualityCfg | None = field(default_factory=QualityCfg)
    answer: AnswerCfg = field(default_factory=AnswerCfg)
    shopping: ShoppingCfg = field(default_factory=ShoppingCfg)
    aliexpress: AliExpressCfg = field(default_factory=AliExpressCfg)
    mercadolivre: MercadolivreCfg = field(default_factory=MercadolivreCfg)
    shopify: ShopifyCfg = field(default_factory=ShopifyCfg)
    shopee: ShopeeCfg = field(default_factory=ShopeeCfg)
    steam: SteamCfg = field(default_factory=SteamCfg)
    service: ServiceCfg = field(default_factory=ServiceCfg)


_SECTIONS: dict[str, type] = {
    "search": SearchCfg,
    "fetch": FetchCfg,
    "browser": BrowserCfg,
    "cache": CacheCfg,
    "logging": LoggingCfg,
    "youtube": YouTubeCfg,
    "wikimedia": WikimediaCfg,
    "quality": QualityCfg,
    "answer": AnswerCfg,
    "shopping": ShoppingCfg,
    "aliexpress": AliExpressCfg,
    "mercadolivre": MercadolivreCfg,
    "shopify": ShopifyCfg,
    "shopee": ShopeeCfg,
    "steam": SteamCfg,
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
