from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class SearchCfg:
    region: str = "us-en"
    max_results: int = 10


@dataclass(frozen=True)
class FetchCfg:
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
class OcrCfg:
    """OCR fallback for image-only PDF pages (Tesseract). A page whose native
    `pdftotext` text is below `min_page_chars` is rasterized and OCR'd; digital
    pages are kept verbatim, so a fully digital PDF is untouched."""

    enabled: bool = True  # OCR fallback for image-only PDF pages
    language: str = (
        "eng"  # tesseract -l code(s), e.g. "eng+por" (needs traineddata installed)
    )
    dpi: int = 200  # pdftoppm rasterization DPI (300 = best/slow)
    max_pages: int = 50  # cap pages OCR'd (cost/latency guard)
    min_page_chars: int = 16  # a page's native text below this ⇒ OCR that page


@dataclass(frozen=True)
class YouTubeCfg:
    cookies_from_browser: str = ""  # "" disables; e.g. "firefox", "chrome", "brave"
    # yt-dlp remote components to allow (space-separated), enabling YouTube's JS
    # challenge solver. "" = off (opt-in): fetches+runs solver code from GitHub.
    # e.g. "ejs:github" (needs a JS runtime like deno on PATH).
    remote_components: str = ""
    # Cap on videos listed for a channel/playlist/search URL. Flat extraction
    # stops after this many entries (yt-dlp `playlistend`).
    max_videos: int = 50


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
class ProviderCfg:
    """One answer backend: a provider + its model (+ api_key for HTTP providers).

    `provider` is ``"deepseek"`` (OpenAI-compatible HTTP; endpoint from the
    built-in ``deepseek.PROVIDER_ENDPOINTS`` registry) or ``"claude_cli"`` (shell
    out to ``claude -p`` on the user's Claude Code subscription via OAuth — no API
    key; ``claude`` must be on PATH). `model` is required.
    """

    provider: str = ""  # "deepseek" | "claude_cli"
    model: str = ""  # model id/alias for the provider
    api_key: str = ""  # deepseek only; or DEEPSEEK_API_KEY / VASCO_ANSWER_API_KEY


@dataclass(frozen=True)
class AnswerCfg:
    """Backend(s) for the `answer` command (fetch + LLM answer over a page).

    `providers` is an **ordered chain**: the first entry is the primary, the rest
    are fallbacks tried in order when an entry is unavailable or fails. There is no
    default — an empty chain disables the capability. A single-provider env
    override (``VASCO_ANSWER_PROVIDER``/``MODEL``/``API_KEY``) replaces the whole
    chain (see ``_load_answer``).
    """

    providers: tuple[ProviderCfg, ...] = ()


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
    en_US-style code with an underscore). `reviews_page_size` caps how many top
    reviews a product page fetches from feedback.aliexpress.com. AliExpress is a
    global marketplace, so the defaults are US/English."""

    currency: str = "USD"
    language: str = "en_US"
    country: str = "US"
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
    descriptions/genres returned by the JSON APIs. Steam is a global storefront,
    so the defaults are US/English. `itad_api_key` (or `VASCO_ITAD_API_KEY`) is
    the **only** switch for IsThereAnyDeal historical-price enrichment on app
    pages — set it to enable, leave empty to disable (the currency follows
    `country`)."""

    country: str = "US"
    language: str = "english"
    itad_api_key: str = ""


@dataclass(frozen=True)
class PhabricatorCfg:
    """Phabricator/Phorge adapter knobs. `domains` extends the built-in known
    host set (``phabricator.wikimedia.org``) so the same Phorge-markup scraper
    works on other public instances; `max_comments` caps how many timeline
    comments a task page parses (a cost guard on very long tasks)."""

    domains: tuple[str, ...] = ()
    max_comments: int = 50


@dataclass(frozen=True)
class GitLabCfg:
    """GitLab adapter knobs (public REST API, no auth). `domains` extends the
    built-in known-host set (`gitlab.com`) so a self-hosted instance is served
    without a probe; `autodetect` lets a claimable URL on an *unknown* host be
    probed against the `/api/v4` endpoint (Shopify-style, falling through on a
    miss); `max_comments` caps how many issue/MR notes (comments) are parsed."""

    domains: tuple[str, ...] = ()
    autodetect: bool = True
    max_comments: int = 20


@dataclass(frozen=True)
class AdaptersCfg:
    """Per-source content-adapter knobs, grouped so site-scraping config stays
    separate from the global infrastructure sections. Each sub-section is the
    adapter's own dataclass; YAML nests under ``adapters:`` and env vars use the
    ``VASCO_ADAPTERS_<SUB>_<FIELD>`` prefix."""

    shopping: ShoppingCfg = field(default_factory=ShoppingCfg)
    aliexpress: AliExpressCfg = field(default_factory=AliExpressCfg)
    mercadolivre: MercadolivreCfg = field(default_factory=MercadolivreCfg)
    shopify: ShopifyCfg = field(default_factory=ShopifyCfg)
    shopee: ShopeeCfg = field(default_factory=ShopeeCfg)
    steam: SteamCfg = field(default_factory=SteamCfg)
    phabricator: PhabricatorCfg = field(default_factory=PhabricatorCfg)
    gitlab: GitLabCfg = field(default_factory=GitLabCfg)
    youtube: YouTubeCfg = field(default_factory=YouTubeCfg)
    wikimedia: WikimediaCfg = field(default_factory=WikimediaCfg)


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
class DomainCfg:
    """Per-domain fetch overrides keyed by host. `headers` picks the HTTP header
    profile the http tier sends — ``browser`` (default modern-Chrome shape) or
    ``honest`` (minimal client headers, for WAFs that 403 the half-fingerprint).
    A unified home for per-domain strategy; ``tier``/``adapter`` directives may
    join `headers` later."""

    host: str
    headers: str = "browser"


@dataclass(frozen=True)
class Config:
    search: SearchCfg = field(default_factory=SearchCfg)
    fetch: FetchCfg = field(default_factory=FetchCfg)
    browser: BrowserCfg = field(default_factory=BrowserCfg)
    cache: CacheCfg = field(default_factory=CacheCfg)
    logging: LoggingCfg = field(default_factory=LoggingCfg)
    ocr: OcrCfg = field(default_factory=OcrCfg)
    quality: QualityCfg | None = field(default_factory=QualityCfg)
    answer: AnswerCfg = field(default_factory=AnswerCfg)
    adapters: AdaptersCfg = field(default_factory=AdaptersCfg)
    service: ServiceCfg = field(default_factory=ServiceCfg)
    # Per-host overrides from the top-level `domains:` map (see DomainCfg).
    domains: tuple[DomainCfg, ...] = ()


# Global (top-level) sections. Adapter sections live under `adapters` (nested),
# handled separately in load_config so they don't mix with infrastructure config.
_SECTIONS: dict[str, type] = {
    "search": SearchCfg,
    "fetch": FetchCfg,
    "browser": BrowserCfg,
    "cache": CacheCfg,
    "logging": LoggingCfg,
    "ocr": OcrCfg,
    "quality": QualityCfg,
    # `answer` is handled by `_load_answer` (a provider chain, not flat scalars).
    "service": ServiceCfg,
}

# Per-source content-adapter sections, nested under the `adapters` key.
_ADAPTER_SECTIONS: dict[str, type] = {
    "shopping": ShoppingCfg,
    "aliexpress": AliExpressCfg,
    "mercadolivre": MercadolivreCfg,
    "shopify": ShopifyCfg,
    "shopee": ShopeeCfg,
    "steam": SteamCfg,
    "phabricator": PhabricatorCfg,
    "gitlab": GitLabCfg,
    "youtube": YouTubeCfg,
    "wikimedia": WikimediaCfg,
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
    VASCO_<SECTION>_<FIELD>, e.g. VASCO_FETCH_WORKERS=8; adapter sections nest
    under `adapters`, so their env prefix is VASCO_ADAPTERS_<SUB>_<FIELD>,
    e.g. VASCO_ADAPTERS_STEAM_COUNTRY=US.
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

    # Adapter sections, nested under the `adapters` key. Env vars use the
    # compound prefix VASCO_ADAPTERS_<SUB>_<FIELD>, mirroring the config path.
    adapters_data = data.get("adapters", {})
    if not isinstance(adapters_data, dict):
        adapters_data = {}
    adapter_sections: dict[str, Any] = {}
    for sub in _ADAPTER_SECTIONS:
        current = getattr(cfg.adapters, sub)
        sub_data = adapters_data.get(sub, {})
        if isinstance(sub_data, dict) and sub_data:
            current = _apply_overrides(current, sub_data)
        current = _apply_env(current, f"adapters_{sub}")
        adapter_sections[sub] = current
    sections["adapters"] = AdaptersCfg(**adapter_sections)

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

    sections["answer"] = _load_answer(data.get("answer"))
    sections["domains"] = _load_domains(data.get("domains"))

    return Config(**sections)


def _load_answer(raw: Any) -> AnswerCfg:
    """Parse the `answer:` section into an ordered provider chain.

    A single-provider env override takes precedence over the config file: if
    ``VASCO_ANSWER_PROVIDER`` is set, the whole chain becomes that one entry
    (with ``VASCO_ANSWER_MODEL`` / ``VASCO_ANSWER_API_KEY``). Otherwise the
    chain comes from ``answer.providers`` (a list of ``{provider, model,
    api_key}`` maps; malformed entries are skipped). YAML-only for the list —
    there is no per-field env override for a chain.
    """
    env_provider = os.environ.get("VASCO_ANSWER_PROVIDER")
    if env_provider:
        return AnswerCfg(
            providers=(
                ProviderCfg(
                    provider=env_provider.strip(),
                    model=os.environ.get("VASCO_ANSWER_MODEL", ""),
                    api_key=os.environ.get("VASCO_ANSWER_API_KEY", ""),
                ),
            )
        )
    if not isinstance(raw, dict):
        return AnswerCfg()
    entries = raw.get("providers")
    if not isinstance(entries, list):
        return AnswerCfg()
    providers: list[ProviderCfg] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        providers.append(
            ProviderCfg(
                provider=str(entry.get("provider", "")).strip(),
                model=str(entry.get("model", "")),
                api_key=str(entry.get("api_key", "")),
            )
        )
    return AnswerCfg(providers=tuple(providers))


_VALID_HEADER_PROFILES = frozenset({"browser", "honest"})


def _load_domains(raw: Any) -> tuple[DomainCfg, ...]:
    """Parse the top-level ``domains:`` YAML map → a tuple of `DomainCfg`.

    Accepts ``{host: {headers: honest}}`` (and a bare ``{host: honest}``
    shorthand). Hosts are lowercased; an unknown/invalid ``headers`` value is
    skipped (defaults stay ``browser``). YAML-only — there is no env override for
    a free-form map.
    """
    if not isinstance(raw, dict):
        return ()
    rules: list[DomainCfg] = []
    for host, spec in raw.items():
        if not isinstance(host, str) or not host.strip():
            continue
        if isinstance(spec, str):
            headers = spec
        elif isinstance(spec, dict):
            headers = spec.get("headers", "browser")
        else:
            continue
        if headers not in _VALID_HEADER_PROFILES:
            continue
        rules.append(DomainCfg(host=host.strip().lower(), headers=headers))
    return tuple(rules)
