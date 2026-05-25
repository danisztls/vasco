"""Wikipedia article fetcher via Wikimedia Enterprise On-demand API.

Structured Contents endpoint for the 9 beta languages (parsed sections,
infoboxes, tables, quality signals).  Standard articles endpoint for all
other languages (HTML body converted to markdown, plus metadata).

Redirects are resolved via the free MediaWiki API before calling Enterprise.

When no Enterprise credentials are configured the shortcut is skipped
entirely — Wikipedia URLs fall through to the normal HTTP fetch pipeline.

The envelope uses ``mode_used="wikipedia"`` and ``content_type="text/wikipedia"``.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import quote, unquote

from .errors import FailureReason

log = logging.getLogger(__name__)

_STRUCTURED_LANGS = frozenset({"en", "de", "fr", "es", "pt", "it", "nl", "cy", "id"})

_WIKIPEDIA_RE = re.compile(
    r"^https?://(?P<lang>[a-z]{2,3})(?:\.m)?\.wikipedia\.org/wiki/(?P<title>[^#?]+)",
    re.IGNORECASE,
)

_AUTH_URL = "https://auth.enterprise.wikimedia.com/v1/login"
_ENTERPRISE_BASE = "https://api.enterprise.wikimedia.com/v2"

# Module-level token cache (survives across MCP calls).
_access_token: str | None = None
_token_expires_at: float = 0.0


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def is_wikipedia_url(url: str) -> bool:
    return bool(_WIKIPEDIA_RE.match(url or ""))


def extract_article_info(url: str) -> tuple[str, str] | None:
    """Return ``(lang, title)`` from a Wikipedia URL, or ``None``."""
    if not url:
        return None
    m = _WIKIPEDIA_RE.match(url)
    if not m:
        return None
    lang = m.group("lang").lower()
    title = unquote(m.group("title")).replace(" ", "_")
    return lang, title


def has_credentials(cfg: Any | None) -> bool:
    username, password = _get_credentials(cfg)
    return bool(username and password)


# ---------------------------------------------------------------------------
# Enterprise auth
# ---------------------------------------------------------------------------


def _get_credentials(cfg: Any | None) -> tuple[str, str]:
    username = ""
    password = ""
    if cfg is not None:
        username = getattr(getattr(cfg, "wikimedia", None), "username", "") or ""
        password = getattr(getattr(cfg, "wikimedia", None), "password", "") or ""
    return username.strip(), password.strip()


async def _ensure_token(cfg: Any | None, deadline_monotonic: float) -> str | None:
    global _access_token, _token_expires_at

    if _access_token and time.monotonic() < _token_expires_at:
        return _access_token

    username, password = _get_credentials(cfg)
    if not username or not password:
        return None

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return None

    import httpx

    try:
        async with httpx.AsyncClient(timeout=min(10.0, remaining)) as client:
            resp = await client.post(
                _AUTH_URL,
                json={"username": username, "password": password},
            )
            resp.raise_for_status()
            data = resp.json()
            _access_token = data.get("access_token")
            expires_in = int(data.get("expires_in", 86400))
            _token_expires_at = time.monotonic() + expires_in - 300
            return _access_token
    except Exception as exc:
        log.warning("Wikimedia Enterprise auth failed: %s", exc)
        return None


def _reset_token_for_tests() -> None:
    global _access_token, _token_expires_at
    _access_token = None
    _token_expires_at = 0.0


# ---------------------------------------------------------------------------
# Redirect resolution (free MediaWiki API, no auth needed)
# ---------------------------------------------------------------------------


async def _resolve_redirect(lang: str, title: str, *, deadline_monotonic: float) -> str:
    """Resolve a Wikipedia redirect via the MediaWiki API.

    Returns the canonical title (may be the same if not a redirect).
    """
    import httpx

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return title

    api_url = f"https://{lang}.wikipedia.org/w/api.php"
    try:
        headers = {"User-Agent": "Vasco/0.1 (web research CLI)"}
        async with httpx.AsyncClient(
            timeout=min(5.0, remaining), headers=headers
        ) as client:
            resp = await client.get(
                api_url,
                params={
                    "action": "query",
                    "titles": title.replace("_", " "),
                    "redirects": "1",
                    "format": "json",
                    "formatversion": "2",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            redirects = data.get("query", {}).get("redirects") or []
            if redirects:
                return redirects[-1].get("to", title).replace(" ", "_")
    except Exception as exc:
        log.debug("MediaWiki redirect resolution failed: %s", exc)
    return title


# ---------------------------------------------------------------------------
# Enterprise API helpers
# ---------------------------------------------------------------------------


async def _enterprise_request(
    endpoint: str,
    title: str,
    project: str,
    token: str,
    *,
    deadline_monotonic: float,
) -> dict[str, Any] | None:
    import httpx

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return None

    url = f"{_ENTERPRISE_BASE}/{endpoint}/{quote(title, safe='')}"

    try:
        async with httpx.AsyncClient(timeout=min(15.0, remaining)) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "filters": [{"field": "is_part_of.identifier", "value": project}],
                    "limit": 1,
                },
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]
            return None
    except Exception as exc:
        log.warning("Wikimedia Enterprise %s failed: %s", endpoint, exc)
        return None


# ---------------------------------------------------------------------------
# Structured Contents → markdown (9 beta languages)
# ---------------------------------------------------------------------------


def _render_table(table: dict[str, Any], parts: list[str]) -> None:
    headers = table.get("headers") or []
    rows = table.get("rows") or []
    if not headers and not rows:
        return

    if headers:
        header_row = headers[0] if headers else []
        cells = [cell.get("value", "") for cell in header_row]
        parts.append("| " + " | ".join(cells) + " |")
        parts.append("| " + " | ".join("---" for _ in cells) + " |")

    for row in rows:
        cells = [cell.get("value", "") for cell in row]
        parts.append("| " + " | ".join(cells) + " |")

    parts.append("")


def _render_section(
    section: dict[str, Any],
    parts: list[str],
    depth: int,
    tables_by_id: dict[str, dict[str, Any]],
) -> None:
    name = section.get("name", "")
    if name and name != "Abstract":
        parts.append(f"{'#' * depth} {name}")
        parts.append("")

    for part in section.get("has_parts") or []:
        ptype = part.get("type", "")
        if ptype == "paragraph":
            value = (part.get("value") or "").strip()
            if value:
                parts.append(value)
                parts.append("")
        elif ptype in ("list", "list_item"):
            for item in part.get("values") or part.get("has_parts") or []:
                if isinstance(item, str):
                    parts.append(f"- {item}")
                elif isinstance(item, dict):
                    parts.append(f"- {item.get('value', '')}")
            parts.append("")
        elif ptype == "table":
            for ref in part.get("table_references") or []:
                tid = ref.get("identifier", "")
                if tid in tables_by_id:
                    _render_table(tables_by_id[tid], parts)
        elif ptype == "section":
            _render_section(
                part, parts, depth=min(depth + 1, 6), tables_by_id=tables_by_id
            )


def _structured_to_fields(article: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    tables_by_id: dict[str, dict[str, Any]] = {}
    for table in article.get("tables") or []:
        tid = table.get("identifier")
        if tid:
            tables_by_id[tid] = table

    parts: list[str] = []
    sections = article.get("sections") or []

    # Skip abstract when sections exist — the first section ("Abstract")
    # already contains the lead paragraphs.
    if not sections:
        abstract = article.get("abstract") or ""
        if abstract:
            parts.append(abstract)
            parts.append("")

    for section in sections:
        _render_section(section, parts, depth=2, tables_by_id=tables_by_id)

    markdown = "\n".join(parts).strip()

    quality = _extract_quality(article)
    infoboxes = article.get("infoboxes")
    if infoboxes:
        quality["infoboxes"] = infoboxes

    return markdown, _meta(article, quality)


# ---------------------------------------------------------------------------
# Standard articles → markdown (all languages)
# ---------------------------------------------------------------------------


def _standard_to_fields(article: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    from . import convert

    html = (article.get("article_body") or {}).get("html") or ""
    if html:
        markdown, conv_meta = convert.html_to_markdown(html, url="")
        links = conv_meta.get("links", [])
    else:
        markdown = article.get("abstract") or ""
        links = []

    quality = _extract_quality(article)
    meta = _meta(article, quality)
    meta["links"] = links
    return markdown, meta


# ---------------------------------------------------------------------------
# Shared metadata extraction
# ---------------------------------------------------------------------------


def _extract_quality(article: dict[str, Any]) -> dict[str, Any]:
    quality: dict[str, Any] = {}
    description = article.get("description") or article.get("abstract")
    if description:
        quality["description"] = description
    wikidata = (article.get("main_entity") or {}).get("identifier")
    if wikidata:
        quality["wikidata"] = wikidata
    maintenance = (article.get("version") or {}).get("maintenance_tags")
    if maintenance:
        quality["maintenance_tags"] = maintenance
    scores = (article.get("version") or {}).get("scores")
    if scores:
        quality["scores"] = scores
    return quality


def _meta(article: dict[str, Any], quality: dict[str, Any]) -> dict[str, Any]:
    in_lang = article.get("in_language") or {}
    return {
        "title": article.get("name"),
        "byline": None,
        "published": article.get("date_created"),
        "modified": article.get("date_modified"),
        "language": in_lang.get("identifier"),
        "quality": quality,
        "links": [],
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# Word counting (CJK-aware)
# ---------------------------------------------------------------------------

_CJK_RANGES = (
    "一-鿿"  # CJK Unified Ideographs
    "㐀-䶿"  # CJK Extension A
    "぀-ゟ"  # Hiragana
    "゠-ヿ"  # Katakana
    "가-힯"  # Hangul Syllables
)
_CJK_RE = re.compile(f"[{_CJK_RANGES}]")


def _word_count(text: str) -> int:
    """Count words, treating each CJK character as one word."""
    cjk_chars = len(_CJK_RE.findall(text))
    non_cjk = _CJK_RE.sub("", text)
    space_words = len(non_cjk.split())
    return cjk_chars + space_words


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------


def _base_envelope(url: str, *, http_status: int = 0) -> dict[str, Any]:
    return {
        "url_requested": url,
        "url_final": url,
        "url_canonical": url,
        "http_status": http_status,
        "mode_used": "wikipedia",
        "fetched_at": int(time.time()),
        "from_cache": False,
        "cache_age_seconds": 0,
        "content_type": "text/wikipedia",
    }


def _failure_envelope(
    url: str,
    reason: FailureReason,
    message: str,
    *,
    http_status: int = 0,
) -> dict[str, Any]:
    env = _base_envelope(url, http_status=http_status)
    env["failure"] = {
        "reason": str(reason),
        "retry_after_seconds": None,
        "message": message,
    }
    env["markdown"] = ""
    env["warnings"] = []
    return env


def _success_envelope(
    url: str,
    markdown: str,
    meta: dict[str, Any],
    *,
    lang: str,
    http_status: int = 200,
) -> dict[str, Any]:
    from . import io as io_mod

    env = _base_envelope(url, http_status=http_status)
    env.update(
        {
            "title": meta.get("title"),
            "byline": None,
            "published": meta.get("published"),
            "modified": meta.get("modified"),
            "language": meta.get("language") or lang,
            "site_name": "Wikipedia",
            "word_count": _word_count(markdown),
            "token_count_estimate": io_mod.estimate_tokens(markdown),
            "quality": meta.get("quality", {}),
            "links": meta.get("links", []),
            "markdown": markdown,
            "warnings": meta.get("warnings", []),
        }
    )
    return env


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def fetch_wikipedia(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """Fetch a Wikipedia article via Wikimedia Enterprise and return an envelope."""
    deadline_monotonic = time.monotonic() + max(0.0, float(deadline))

    info = extract_article_info(url)
    if not info:
        return _failure_envelope(
            url, FailureReason.INVALID_URL, "could not parse Wikipedia article URL"
        )
    lang, title = info

    token = await _ensure_token(cfg, deadline_monotonic)
    if not token:
        return _failure_envelope(
            url, FailureReason.LOGIN_REQUIRED, "Wikimedia Enterprise auth failed"
        )

    # Resolve redirects via the free MediaWiki API before calling Enterprise.
    resolved = await _resolve_redirect(
        lang, title, deadline_monotonic=deadline_monotonic
    )

    project = f"{lang}wiki"

    # Structured Contents for the 9 beta languages.
    if lang in _STRUCTURED_LANGS:
        article = await _enterprise_request(
            "structured-contents",
            resolved,
            project,
            token,
            deadline_monotonic=deadline_monotonic,
        )
        if article:
            markdown, meta = _structured_to_fields(article)
            if markdown:
                return _success_envelope(url, markdown, meta, lang=lang)

    # Standard articles endpoint (all languages, returns HTML body).
    article = await _enterprise_request(
        "articles",
        resolved,
        project,
        token,
        deadline_monotonic=deadline_monotonic,
    )
    if not article:
        return _failure_envelope(
            url, FailureReason.NOT_FOUND, "article not found", http_status=404
        )

    markdown, meta = _standard_to_fields(article)
    if not markdown:
        return _failure_envelope(
            url,
            FailureReason.UNSUPPORTED_CONTENT_TYPE,
            "no text extracted from Wikipedia article",
        )

    return _success_envelope(url, markdown, meta, lang=lang)
