"""GitLab adapter (public REST API, no auth).

GitLab's web UI is a Vue SPA, so trafilatura gets little from a project / issue /
merge-request page. But GitLab exposes a clean **public REST API** (``/api/v4``)
that returns structured JSON with no token for public projects. Unlike
:mod:`vasco.adapters.steam` / :mod:`vasco.adapters.shopify` (which ride the
injected ``fetch_html``), this adapter fetches the API through its **own
minimal-header httpx client** (like :mod:`vasco.adapters.itad`): the shared
escalation chain's "modern-Chrome" header set (``Sec-Fetch-*`` etc.) trips
self-hosted GitLab WAFs into a 403, and a JSON endpoint gains nothing from the
browser tier (which would wrap it in Firefox's JSON viewer) or the wayback tail.
A minimal GET sails through, so the adapter parses the JSON anchor directly.

Scope is the "not in git" layer — it is **not** a substitute for ``git`` (no
code / tree / commit / pipeline fetching). Three page types are claimed:

- **Project** (a bare ``/<namespace>/<project>`` path, nested groups allowed) →
  ``/api/v4/projects/<url-encoded-path>`` metadata (stars, forks, topics,
  license, default branch, activity, description), enriched **best-effort** with
  the rendered README (``readme_url`` rewritten ``/-/blob/`` → ``/-/raw/``).
- **Issue** (``/-/issues/<iid>``) → ``/api/v4/projects/<enc>/issues/<iid>`` plus
  its notes (comments), best-effort.
- **Merge request** (``/-/merge_requests/<iid>``) → the analogous MR endpoint +
  notes.

Host coverage: ``gitlab.com`` and any host listed in
``cfg.adapters.gitlab.domains`` are *known* (served without a probe). The
``/-/issues|merge_requests/`` markers are GitLab-distinctive, and a bare-project
shape is broad, so an **unknown** host on a claimable URL is **probed** (like
Shopify): the API call doubles as the probe — valid GitLab JSON confirms the
host (memoized in the ``adapter_probe`` table, **keyed by full host** since a
GitLab instance lives on a specific subdomain), a non-JSON body marks the host
*not* GitLab, and anything ambiguous raises :class:`NotGitLab` so the dispatcher
**falls through to a normal fetch** (a non-GitLab lookalike is never a failure).

Rot contract (per the project invariants): on a *known* host a non-JSON API body
→ ``PARSE_FAILED``; a JSON 404 / not-an-object body → ``NOT_FOUND``; otherwise a
parsed object → ``success``. The adapter never raises except :class:`NotGitLab`
(a probe miss, caught by the dispatcher).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .. import envelope
from ..errors import AdapterParseError, FailureReason
from . import _common
from ._common import compact as _compact

log = logging.getLogger(__name__)

_PROVIDER = "gitlab"  # adapter_probe key namespace
_SITE_NAME = "GitLab"
_CONTENT_TYPE = "application/x-gitlab"

# Hosts known to be GitLab without a probe; extended via cfg.adapters.gitlab.domains.
_DEFAULT_HOSTS: frozenset[str] = frozenset({"gitlab.com"})

# First path segment values that are GitLab reserved routes, never a namespace —
# so a bare two-segment path under one of these is not a project candidate.
_RESERVED_FIRST: frozenset[str] = frozenset(
    {
        "users",
        "groups",
        "dashboard",
        "explore",
        "help",
        "api",
        "admin",
        "search",
        "projects",
        "-",
        "s",
        "topics",
        "public",
        "assets",
        "uploads",
        "favicon.ico",
        "robots.txt",
        "sitemap.xml",
    }
)

# Hostname *labels* that hint at a code forge, gating the bare-project probe.
# The `/-/issues|merge_requests/` markers are GitLab-distinctive enough to probe
# on ANY host, but a bare `/<ns>/<repo>` shape matches most of the web — probing
# every 2-segment URL would spray `/api/v4/projects/` at every site and add a
# round-trip on first contact with each. So a bare-project URL on an unknown host
# is only probed when a hostname label looks forge-y (catches gitlab.com,
# gitlab.*, git.*, code.*, …); other self-hosted instances (salsa.debian.org,
# invent.kde.org) are reached via `cfg.adapters.gitlab.domains` or their issue/MR
# URLs. Matched per-label (not substring) so "digital"/"barcode" don't false-fire.
_FORGE_HINTS: frozenset[str] = frozenset(
    {"gitlab", "git", "forge", "scm", "code", "vcs", "repo", "dev"}
)

_README_MAX_CHARS = 20_000

# In-process front for the probe verdict; durable backing is the SQLite
# `adapter_probe` table (Cache.get_probe/set_probe), shared by CLI + vascod, so a
# host is probed at most once ever. Keyed by **full host** (not registered domain
# like shopify): a GitLab instance is subdomain-specific — gitlab.wikimedia.org is
# GitLab, www.wikimedia.org is not. (cache.purge_domain(<eTLD+1>) clears these
# host-scoped verdicts too — it matches both the apex and `%.<eTLD+1>`.)
_probe_memo: dict[str, bool] = {}  # host -> is_gitlab


def _reset_for_tests() -> None:
    """Clear the process-lifetime probe memo. Does not touch the persistent
    ``adapter_probe`` table — pass a fresh ``Cache`` in persistence tests."""
    _probe_memo.clear()


class NotGitLab(Exception):
    """A probe of a candidate URL did not confirm a GitLab instance.

    Raised by ``fetch_gitlab(probe=True)`` when the API endpoint is absent or not
    GitLab-shaped (or the verdict is ambiguous). The dispatcher catches it and
    **falls through to a normal fetch** — a probe miss is not a failure.
    """


class _Failure(Exception):
    """Internal: carries a ready failure envelope out of a parse/fetch step on a
    *known* host (never escapes ``fetch_gitlab``)."""

    def __init__(self, env: dict[str, Any]) -> None:
        super().__init__()
        self.envelope = env


# ---------------------------------------------------------------------------
# Probe memo (persisted, host-keyed)
# ---------------------------------------------------------------------------


def _probe_state(host: str, cache: Any | None) -> bool | None:
    """Cached probe verdict for ``host``: ``True`` (GitLab), ``False`` (not), or
    ``None`` (unknown). In-process memo first, then the persistent table."""
    if host in _probe_memo:
        return _probe_memo[host]
    if cache is not None and hasattr(cache, "get_probe"):
        try:
            val = cache.get_probe(_PROVIDER, host)
        except Exception:  # a cache hiccup must never break detection
            val = None
        if val is not None:
            _probe_memo[host] = val
            return val
    return None


def _set_probe(host: str, value: bool, cache: Any | None) -> None:
    """Record a probe verdict in the memo and the persistent table (best-effort)."""
    _probe_memo[host] = value
    if cache is not None and hasattr(cache, "set_probe"):
        try:
            cache.set_probe(_PROVIDER, host, value)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# URL detection (pure, host-agnostic shape)
# ---------------------------------------------------------------------------


def _known_hosts(cfg: Any | None) -> frozenset[str]:
    extra = getattr(
        getattr(getattr(cfg, "adapters", None), "gitlab", None), "domains", ()
    )
    return _DEFAULT_HOSTS | frozenset(str(h).lower() for h in (extra or ()) if h)


def _reserved(project_path: str) -> bool:
    first = project_path.split("/", 1)[0].lower()
    return first in _RESERVED_FIRST


def _forge_hint(host: str) -> bool:
    """True if a hostname label looks like a code forge (gitlab/git/code/…) — the
    gate for probing a *bare-project* URL on an unknown host."""
    labels = host.split(".")
    return any(label in _FORGE_HINTS or label.startswith("gitlab") for label in labels)


def _claim(url: str) -> tuple[str, str, str | None] | None:
    """Map a GitLab URL to ``(page_type, project_path, ident)`` or ``None``.

    ``("issue"|"merge_request", path, iid)`` for ``/-/issues|merge_requests/<n>``;
    ``("project", path, None)`` for a bare ``/<ns>/<project>`` path (nested groups
    allowed). Other ``/-/...`` routes (tree/blob/commits/wikis/snippets), single
    segments (user/group), reserved-prefixed and non-http URLs return ``None``.
    """
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not (parts.hostname or ""):
        return None
    path = parts.path or "/"

    if "/-/" in path:
        before, _, after = path.partition("/-/")
        project_path = before.strip("/")
        if not project_path or _reserved(project_path):
            return None
        segs = [s for s in after.split("/") if s]
        if len(segs) >= 2 and segs[1].isdigit():
            if segs[0] == "issues":
                return "issue", project_path, segs[1]
            if segs[0] == "merge_requests":
                return "merge_request", project_path, segs[1]
        return None

    segs = [s for s in path.split("/") if s]
    if len(segs) < 2 or segs[0].lower() in _RESERVED_FIRST:
        return None
    return "project", "/".join(segs), None


def is_gitlab_url(url: str, cfg: Any | None = None, cache: Any | None = None) -> bool:
    """Certain match: a claimable GitLab URL on a known host (built-in/config) or
    a host a prior probe confirmed (memo or persistent ``adapter_probe`` table)."""
    if not url or _claim(url) is None:
        return False
    host = (urlsplit(url).hostname or "").lower()
    if host in _known_hosts(cfg):
        return True
    return _probe_state(host, cache) is True


def is_gitlab_candidate(
    url: str, cfg: Any | None = None, cache: Any | None = None
) -> bool:
    """Probe-worthy: a claimable shape on an *unknown* host with autodetect on and
    no cached verdict yet. A True/False verdict means we don't probe (True is
    served by ``is_gitlab_url``, False stays a plain fetch), so a host is probed at
    most once across all processes.

    An issue/MR URL (the GitLab-distinctive ``/-/`` marker) is a candidate on any
    host; a **bare-project** URL only on a forge-hinted host (``_forge_hint``),
    since its ``/<ns>/<repo>`` shape matches most of the web — otherwise vasco
    would probe every domain's first 2-segment URL.
    """
    claim = _claim(url)
    if claim is None:
        return False
    if not getattr(
        getattr(getattr(cfg, "adapters", None), "gitlab", None), "autodetect", True
    ):
        return False
    host = (urlsplit(url).hostname or "").lower()
    if host in _known_hosts(cfg):
        return False
    if claim[0] == "project" and not _forge_hint(host):
        return False
    return _probe_state(host, cache) is None


# ---------------------------------------------------------------------------
# Value normalization helpers (pure)
# ---------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (float, str)):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    return None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _clean(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _day(value: Any) -> str | None:
    """The ``YYYY-MM-DD`` prefix of an ISO timestamp, for compact rendering."""
    return value[:10] if isinstance(value, str) and len(value) >= 10 else None


# ---------------------------------------------------------------------------
# Parsers (pure) — take parsed JSON, raise AdapterParseError on a missing anchor
# ---------------------------------------------------------------------------


def _parse_project(data: Any) -> dict[str, Any]:
    """Parse a ``/projects/<id>`` body → a normalized project dict. Anchor =
    ``id`` + ``path_with_namespace`` (a non-GitLab / 404 body lacks both)."""
    if (
        not isinstance(data, dict)
        or data.get("id") is None
        or not isinstance(data.get("path_with_namespace"), str)
    ):
        raise AdapterParseError(
            "projects: not a GitLab project object (no id/path_with_namespace)"
        )
    lic = data.get("license") if isinstance(data.get("license"), dict) else {}
    return _compact(
        {
            "id": _int(data.get("id")),
            "path_with_namespace": data["path_with_namespace"],
            "name": _clean(data.get("name")),
            "name_with_namespace": _clean(data.get("name_with_namespace")),
            "description": _clean(data.get("description")),
            "web_url": _clean(data.get("web_url")),
            "default_branch": _clean(data.get("default_branch")),
            "visibility": _clean(data.get("visibility")),
            "star_count": _int(data.get("star_count")),
            "forks_count": _int(data.get("forks_count")),
            "open_issues_count": _int(data.get("open_issues_count")),
            "topics": _str_list(data.get("topics") or data.get("tag_list")),
            "license": _clean(lic.get("name")),
            "created_at": _clean(data.get("created_at")),
            "last_activity_at": _clean(data.get("last_activity_at")),
            "archived": data.get("archived")
            if isinstance(data.get("archived"), bool)
            else None,
            "readme_url": _clean(data.get("readme_url")),
        }
    )


def _parse_thread(data: Any) -> dict[str, Any]:
    """Shared issue/MR fields. Anchor = ``iid`` + non-empty ``title``."""
    if (
        not isinstance(data, dict)
        or data.get("iid") is None
        or not _clean(data.get("title"))
    ):
        raise AdapterParseError("issue/MR: not a GitLab object (no iid/title)")
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    return _compact(
        {
            "iid": _int(data.get("iid")),
            "title": data["title"].strip(),
            "state": _clean(data.get("state")),
            "description": _clean(data.get("description")),
            "author": _clean(author.get("username")),
            "labels": _str_list(data.get("labels")),
            "web_url": _clean(data.get("web_url")),
            "created_at": _clean(data.get("created_at")),
            "updated_at": _clean(data.get("updated_at")),
            "upvotes": _int(data.get("upvotes")),
            "downvotes": _int(data.get("downvotes")),
            "user_notes_count": _int(data.get("user_notes_count")),
        }
    )


def _parse_issue(data: Any) -> dict[str, Any]:
    return _parse_thread(data)


def _parse_mr(data: Any) -> dict[str, Any]:
    obj = _parse_thread(data)
    obj.update(
        _compact(
            {
                "source_branch": _clean(data.get("source_branch")),
                "target_branch": _clean(data.get("target_branch")),
                "merge_status": _clean(data.get("merge_status")),
                "draft": data.get("draft")
                if isinstance(data.get("draft"), bool)
                else None,
            }
        )
    )
    return obj


def _parse_notes(result: Any, limit: int) -> list[dict[str, Any]]:
    """Parse a notes fetch result → ``{author, created_at, body}`` comments,
    best-effort. ``result`` is a fetch tuple or an exception (from ``gather``);
    a non-array body (e.g. ``{"message": "401 Unauthorized"}`` — some instances
    gate notes anonymously) yields ``[]``. System notes are dropped."""
    if isinstance(result, BaseException) or not isinstance(result, tuple):
        return []
    try:
        body, status, reason = result
    except (ValueError, TypeError):
        return []
    if reason != FailureReason.OK or status >= 400 or not body:
        return []
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for note in data:
        if not isinstance(note, dict) or note.get("system") is True:
            continue
        text = _clean(note.get("body"))
        if not text:
            continue
        author = note.get("author") if isinstance(note.get("author"), dict) else {}
        out.append(
            _compact(
                {
                    "author": _clean(author.get("username")),
                    "created_at": _clean(note.get("created_at")),
                    "body": text,
                }
            )
        )
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_project(proj: dict[str, Any], readme: str | None) -> str:
    title = (
        proj.get("name_with_namespace")
        or proj.get("name")
        or proj.get("path_with_namespace")
        or "?"
    )
    parts = [f"# {title}"]
    facts: list[str] = []
    if proj.get("star_count") is not None:
        facts.append(f"★ {proj['star_count']:,}")
    if proj.get("forks_count") is not None:
        facts.append(f"{proj['forks_count']:,} forks")
    if proj.get("open_issues_count") is not None:
        facts.append(f"{proj['open_issues_count']:,} open issues")
    if proj.get("default_branch"):
        facts.append(f"default: {proj['default_branch']}")
    if proj.get("license"):
        facts.append(proj["license"])
    if proj.get("visibility") and proj["visibility"] != "public":
        facts.append(proj["visibility"])
    if proj.get("archived"):
        facts.append("archived")
    if _day(proj.get("last_activity_at")):
        facts.append(f"updated {_day(proj['last_activity_at'])}")
    if facts:
        parts += ["", " · ".join(facts)]
    if proj.get("topics"):
        parts += ["", "**Topics:** " + ", ".join(proj["topics"])]
    if proj.get("description"):
        parts += ["", proj["description"]]
    if readme:
        parts += ["", "## README", "", readme]
    return "\n".join(parts)


def _render_thread(
    page_type: str, obj: dict[str, Any], comments: list[dict[str, Any]]
) -> str:
    head = f"# {obj.get('title', '')}".rstrip()
    if obj.get("state"):
        head += f" ({obj['state']})"
    parts = [head]
    facts: list[str] = []
    if obj.get("author"):
        facts.append(f"by {obj['author']}")
    if (
        page_type == "merge_request"
        and obj.get("source_branch")
        and obj.get("target_branch")
    ):
        facts.append(f"{obj['source_branch']} → {obj['target_branch']}")
    if _day(obj.get("created_at")):
        facts.append(f"opened {_day(obj['created_at'])}")
    if obj.get("labels"):
        facts.append("labels: " + ", ".join(obj["labels"]))
    if facts:
        parts += ["", " · ".join(facts)]
    if obj.get("description"):
        parts += ["", obj["description"]]
    if comments:
        parts += ["", f"## Comments ({len(comments)})"]
        for c in comments:
            byline = f"**{c.get('author') or 'unknown'}**"
            if _day(c.get("created_at")):
                byline += f" · {_day(c['created_at'])}"
            parts += ["", byline]
            if c.get("body"):
                parts.append(c["body"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Fetch + envelope
# ---------------------------------------------------------------------------

_base_envelope, _failure_envelope = _common.envelope_builders(_PROVIDER, _CONTENT_TYPE)


def _success_envelope(
    url: str,
    *,
    page_type: str,
    status: int,
    markdown: str,
    quality_extra: dict[str, Any],
    title: str | None,
) -> dict[str, Any]:
    from .. import io as io_mod

    quality = _compact({"provider": _PROVIDER, "page_type": page_type, **quality_extra})
    return envelope.success_envelope(
        base=_base_envelope(url, http_status=status or 200),
        markdown=markdown,
        metadata={
            "title": title,
            "byline": None,
            "published": None,
            "modified": None,
            "language": None,
            "site_name": _SITE_NAME,
            "image": None,
            "word_count": len(markdown.split()),
            "quality": quality,
            "warnings": [],
        },
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )


def _max_comments(cfg: Any | None) -> int:
    val = getattr(
        getattr(getattr(cfg, "adapters", None), "gitlab", None), "max_comments", 20
    )
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 20


# An API getter: ``(url) -> (body, status, reason)``. ``reason`` is OK whenever an
# HTTP response arrives (the status is then authoritative); TIMEOUT/SERVER_ERROR
# mark a transport failure (status 0). The default is `_api_get`; tests inject one.
ApiGetter = Callable[[str], Awaitable[tuple[str, int, FailureReason]]]


# An honest API-client UA. NOT a spoofed browser UA: self-hosted GitLab WAFs 403
# a "Mozilla/…Chrome/…Safari" UA arriving without the full browser header set
# (it reads as a headless bot), but accept a plain client UA. Also deliberately
# omits the escalation chain's `Sec-Fetch-*`/`Upgrade-Insecure-Requests` headers,
# which trip the same WAFs. (Verified against gitlab.wikimedia.org + gitlab.com.)
_USER_AGENT = "vasco/0.1 (GitLab API client)"


async def _api_get(
    url: str, *, deadline: float, cfg: Any | None
) -> tuple[str, int, FailureReason]:
    """GET ``url`` with a minimal header set → ``(body, status, reason)``. Never
    raises: a timeout → ``("", 0, TIMEOUT)``; any other transport error →
    ``("", 0, SERVER_ERROR)``. This is the monkeypatch seam for tests."""
    headers = {"User-Agent": _USER_AGENT, "Accept": "*/*"}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=max(1.0, float(deadline))
        ) as client:
            resp = await client.get(url, headers=headers)
        return resp.text, resp.status_code, FailureReason.OK
    except httpx.TimeoutException:
        return "", 0, FailureReason.TIMEOUT
    except Exception:
        return "", 0, FailureReason.SERVER_ERROR


async def _safe_get(get: ApiGetter, target: str) -> Any:
    """Await one getter call, returning the tuple or the raised exception (so
    :func:`_resolve_main` handles both uniformly, like ``gather``)."""
    try:
        return await get(target)
    except Exception as exc:
        return exc


def _resolve_main(
    url: str,
    result: Any,
    parser: Any,
    *,
    probe: bool,
    host: str,
    cache: Any | None,
) -> tuple[dict[str, Any], int]:
    """Interpret a main API result (``(body, status, reason)`` or exception) →
    ``(obj, status)``.

    On a *known* host raises :class:`_Failure` (a ready failure envelope); on a
    *probe* raises :class:`NotGitLab` (fall through), recording the host verdict
    only when conclusive: a stable non-JSON response (HTTP < 500) → not GitLab;
    a parsed object → GitLab. Transport errors and 5xx leave the verdict unset.
    """
    if isinstance(result, BaseException):
        if probe:
            raise NotGitLab()
        reason = (
            FailureReason.TIMEOUT
            if isinstance(result, asyncio.TimeoutError)
            else FailureReason.SERVER_ERROR
        )
        raise _Failure(
            _failure_envelope(
                url, reason, f"gitlab: API fetch failed: {type(result).__name__}"
            )
        )

    body, status, reason = result
    if reason != FailureReason.OK:  # transport timeout / connection error
        if probe:
            raise NotGitLab()
        raise _Failure(
            _failure_envelope(
                url, reason, "gitlab: API fetch failed", http_status=status
            )
        )

    try:
        data = json.loads(body) if body else None
    except (json.JSONDecodeError, TypeError):
        data = None
    if not isinstance(data, (dict, list)):
        # Non-JSON body (e.g. an nginx 403/404 page, an SPA shell, empty).
        if probe:
            if status and status < 500:  # a stable verdict; 5xx is transient
                _set_probe(host, False, cache)
            raise NotGitLab()
        if status == 404:
            raise _Failure(
                _failure_envelope(
                    url,
                    FailureReason.NOT_FOUND,
                    "gitlab: not found",
                    http_status=status,
                )
            )
        raise _Failure(
            _failure_envelope(
                url,
                FailureReason.PARSE_FAILED,
                f"gitlab: non-JSON API response (HTTP {status})",
                http_status=status,
            )
        )

    try:
        obj = parser(data)
    except AdapterParseError as exc:
        if probe:  # JSON but not a GitLab object (incl. 404 message) → ambiguous
            raise NotGitLab()
        if status == 404 or (
            isinstance(data, dict) and isinstance(data.get("message"), str)
        ):
            raise _Failure(
                _failure_envelope(
                    url, FailureReason.NOT_FOUND, f"gitlab: {exc}", http_status=status
                )
            )
        raise _Failure(
            _failure_envelope(
                url, FailureReason.PARSE_FAILED, f"gitlab: {exc}", http_status=status
            )
        )

    if probe:
        _set_probe(host, True, cache)
    return obj, status


async def _fetch_readme(proj: dict[str, Any], get: ApiGetter) -> str | None:
    """Best-effort raw README (``readme_url`` ``/-/blob/`` → ``/-/raw/``), capped.
    Returns ``None`` on any miss — README is enrichment, never fails the fetch."""
    readme_url = proj.get("readme_url")
    if not isinstance(readme_url, str) or "/-/blob/" not in readme_url:
        return None
    raw_url = readme_url.replace("/-/blob/", "/-/raw/", 1)
    result = await _safe_get(get, raw_url)
    if isinstance(result, BaseException) or not isinstance(result, tuple):
        return None
    body, status, reason = result
    if reason != FailureReason.OK or status >= 400 or not isinstance(body, str):
        return None
    text = body.strip()
    if not text:
        return None
    if len(text) > _README_MAX_CHARS:
        text = text[:_README_MAX_CHARS].rstrip() + "\n\n… (README truncated)"
    return text


async def _fetch_project(
    url: str,
    project_path: str,
    get: ApiGetter,
    *,
    api: str,
    probe: bool,
    host: str,
    cache: Any | None,
) -> dict[str, Any]:
    enc = quote(project_path, safe="")
    result = await _safe_get(get, f"{api}/projects/{enc}?license=true")
    proj, status = _resolve_main(
        url, result, _parse_project, probe=probe, host=host, cache=cache
    )
    readme = await _fetch_readme(proj, get)
    return _success_envelope(
        url,
        page_type="project",
        status=status,
        markdown=_render_project(proj, readme),
        quality_extra={"host": host, "result_count": 1, "project": proj},
        title=proj.get("name_with_namespace") or proj.get("name") or project_path,
    )


async def _fetch_thread(
    url: str,
    project_path: str,
    iid: str,
    *,
    page_type: str,
    endpoint: str,
    parser: Any,
    get: ApiGetter,
    api: str,
    probe: bool,
    host: str,
    cache: Any | None,
    cfg: Any | None,
) -> dict[str, Any]:
    enc = quote(project_path, safe="")
    limit = _max_comments(cfg)
    main_t = f"{api}/projects/{enc}/{endpoint}/{iid}"
    notes_t = (
        f"{api}/projects/{enc}/{endpoint}/{iid}/notes?sort=asc&per_page={max(1, limit)}"
    )
    # Main + notes concurrently; only the main object can fail/probe the fetch.
    main_res, notes_res = await asyncio.gather(
        _safe_get(get, main_t),
        _safe_get(get, notes_t),
        return_exceptions=True,
    )
    obj, status = _resolve_main(
        url, main_res, parser, probe=probe, host=host, cache=cache
    )
    comments = _parse_notes(notes_res, limit) if limit else []
    return _success_envelope(
        url,
        page_type=page_type,
        status=status,
        markdown=_render_thread(page_type, obj, comments),
        quality_extra={
            "host": host,
            "result_count": 1,
            page_type: obj,
            "comments": comments,
        },
        title=obj.get("title"),
    )


async def fetch_gitlab(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
    cache: Any | None = None,
    probe: bool = False,
    _get: ApiGetter | None = None,
) -> dict[str, Any]:
    """Fetch a GitLab project / issue / MR URL → a structured envelope.

    The ``/api/v4`` JSON is fetched via the adapter's own minimal-header httpx
    client (``_get`` overrides it in tests). With ``probe=True`` (an unknown host)
    a miss raises :class:`NotGitLab` for the dispatcher to fall through; otherwise
    it returns a failure envelope and never raises.
    """
    claim = _claim(url)
    if claim is None:  # defensive — dispatch only calls us on a claimed URL
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, "gitlab: unrecognized URL shape"
        )

    page_type, project_path, ident = claim
    parts = urlsplit(url)
    api = f"{parts.scheme}://{parts.netloc}/api/v4"
    host = (parts.hostname or "").lower()
    get: ApiGetter = _get or (lambda u: _api_get(u, deadline=deadline, cfg=cfg))

    try:
        if page_type == "issue":
            return await _fetch_thread(
                url,
                project_path,
                ident or "",
                page_type="issue",
                endpoint="issues",
                parser=_parse_issue,
                get=get,
                api=api,
                probe=probe,
                host=host,
                cache=cache,
                cfg=cfg,
            )
        if page_type == "merge_request":
            return await _fetch_thread(
                url,
                project_path,
                ident or "",
                page_type="merge_request",
                endpoint="merge_requests",
                parser=_parse_mr,
                get=get,
                api=api,
                probe=probe,
                host=host,
                cache=cache,
                cfg=cfg,
            )
        return await _fetch_project(
            url, project_path, get, api=api, probe=probe, host=host, cache=cache
        )
    except _Failure as f:
        return f.envelope
    except AdapterParseError as exc:  # defensive — anchor checks live in parsers
        log.warning("gitlab parse anchor missing (%s): %s", page_type, exc)
        return _failure_envelope(url, FailureReason.PARSE_FAILED, f"gitlab {exc}")
    # NotGitLab intentionally propagates to the dispatcher (probe fall-through).
