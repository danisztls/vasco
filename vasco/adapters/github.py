# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GitHub repository adapter (public REST API + raw README).

A repo page converts to its README but loses the numbers an agent asks for, like
stars and forks. Only bare repo pages (``/<owner>/<repo>``) are claimed; issues,
pull requests and trees fall through to a normal fetch.

The anonymous API allows 60 requests an hour, so each repo spends one: the README
comes from ``raw.githubusercontent.com``, which that limit doesn't cover. An
optional ``adapters.github.token`` raises the limit and is sent only to
``api.github.com``.

The page itself still works without the API, so any API miss (rate limit, a
reserved path that isn't a repo, a private repo, a timeout) raises
:class:`GitHubUnavailable` and the dispatcher falls through to a normal fetch.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from .. import envelope
from . import _common

log = logging.getLogger(__name__)

_API = "https://api.github.com/repos"
_RAW = "https://raw.githubusercontent.com"
_HOSTS = frozenset({"github.com", "www.github.com"})
_USER_AGENT = "vasco"
_README_NAMES = ("README.md", "README.rst", "readme.md", "README", "Readme.md")

# First path segments that are GitHub's own pages, not owners. Skipping them
# saves the anonymous API budget; one that slips through just falls through.
_RESERVED_OWNERS = frozenset(
    {
        "about",
        "apps",
        "codespaces",
        "collections",
        "contact",
        "customer-stories",
        "dashboard",
        "enterprise",
        "events",
        "explore",
        "features",
        "issues",
        "join",
        "login",
        "marketplace",
        "new",
        "nonprofit",
        "notifications",
        "orgs",
        "organizations",
        "pricing",
        "pulls",
        "readme",
        "resources",
        "search",
        "security",
        "settings",
        "site",
        "solutions",
        "sponsors",
        "stars",
        "team",
        "topics",
        "trending",
        "users",
        "watching",
    }
)

# A Markdown link or image target that is relative to the repo root.
_RELATIVE_LINK_RE = re.compile(r"(\]\()(?![a-zA-Z][a-zA-Z0-9+.-]*:|#|/)([^)\s]+)")

_base_envelope, _ = _common.envelope_builders("github", "application/x-github")


class GitHubUnavailable(Exception):
    """The API can't serve this URL; the dispatcher does a normal fetch instead."""


def _claim(url: str) -> tuple[str, str] | None:
    """``(owner, repo)`` for a bare repo page, else None."""
    parts = urlsplit(url or "")
    if (parts.hostname or "").lower() not in _HOSTS:
        return None
    segments = [s for s in (parts.path or "").split("/") if s]
    if len(segments) != 2 or segments[0].lower() in _RESERVED_OWNERS:
        return None
    return segments[0], re.sub(r"\.git$", "", segments[1])


def is_github_url(url: str) -> bool:
    return _claim(url) is not None


def _token(cfg: Any | None) -> str:
    adapters = getattr(cfg, "adapters", None)
    return getattr(getattr(adapters, "github", None), "token", "") or ""


async def _http_get(
    url: str, headers: dict[str, str], *, deadline: float
) -> tuple[str, int]:
    """GET ``url`` → ``(body, status)``, status 0 on a transport error. Never
    raises; the test seam."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=max(1.0, float(deadline))
        ) as client:
            resp = await client.get(url, headers={"User-Agent": _USER_AGENT, **headers})
        return resp.text, resp.status_code
    except Exception:
        return "", 0


async def _fetch_repo(owner: str, repo: str, *, deadline: float, token: str) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body, status = await _http_get(f"{_API}/{owner}/{repo}", headers, deadline=deadline)
    if status != 200:
        log.info("github api answered %s for %s/%s", status, owner, repo)
        raise GitHubUnavailable(f"github api answered {status}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise GitHubUnavailable("github api answered non-JSON") from exc
    if not (isinstance(data, dict) and data.get("full_name")):
        raise GitHubUnavailable("github api answered without a full_name")
    return data


def _absolute_links(readme: str, full_name: str, branch: str) -> str:
    return _RELATIVE_LINK_RE.sub(
        lambda m: (
            f"{m.group(1)}https://github.com/{full_name}/blob/{branch}/{m.group(2)}"
        ),
        readme,
    )


async def _fetch_readme(data: dict[str, Any], *, deadline: float) -> str:
    full_name, branch = data["full_name"], data.get("default_branch") or "HEAD"
    for name in _README_NAMES:
        body, status = await _http_get(
            f"{_RAW}/{full_name}/{branch}/{name}", {}, deadline=deadline
        )
        if status == 200 and body.strip():
            return _absolute_links(body.strip(), full_name, branch)
    return ""


def _repo_fields(data: dict[str, Any]) -> dict[str, Any]:
    license_info = data.get("license") if isinstance(data.get("license"), dict) else {}
    spdx = license_info.get("spdx_id")
    return {
        "full_name": data["full_name"],
        "description": data.get("description") or None,
        "stars": data.get("stargazers_count"),
        "forks": data.get("forks_count"),
        "watchers": data.get("subscribers_count"),
        "open_issues_and_prs": data.get("open_issues_count"),
        "language": data.get("language"),
        "license": spdx if spdx and spdx != "NOASSERTION" else license_info.get("name"),
        "topics": [t for t in (data.get("topics") or []) if isinstance(t, str)],
        "default_branch": data.get("default_branch"),
        "homepage": data.get("homepage") or None,
        "created_at": data.get("created_at"),
        "pushed_at": data.get("pushed_at"),
        "archived": bool(data.get("archived")),
        "fork": bool(data.get("fork")),
    }


def _count(value: Any) -> str | None:
    return f"{value:,}" if isinstance(value, int) else None


def _render_markdown(repo: dict[str, Any], readme: str) -> str:
    rows = [
        ("Stars", _count(repo["stars"])),
        ("Forks", _count(repo["forks"])),
        ("Watchers", _count(repo["watchers"])),
        ("Open issues and pull requests", _count(repo["open_issues_and_prs"])),
        ("Language", repo["language"]),
        ("License", repo["license"]),
        ("Topics", ", ".join(repo["topics"])),
        ("Homepage", repo["homepage"]),
        ("Last push", repo["pushed_at"]),
        ("Archived", "yes" if repo["archived"] else None),
        ("Fork", "yes" if repo["fork"] else None),
    ]
    lines = [f"# {repo['full_name']}", ""]
    if repo["description"]:
        lines += [repo["description"], ""]
    lines += [f"- {label}: {value}" for label, value in rows if value]
    if readme:
        lines += ["", "## README", "", readme]
    return "\n".join(lines)


async def fetch_github(
    url: str, *, deadline: float = 30.0, cfg: Any | None = None
) -> dict[str, Any]:
    """Fetch a repo's data → a structured envelope.

    Raises :class:`GitHubUnavailable` when the API can't serve it.
    """
    claim = _claim(url)
    if claim is None:  # pragma: no cover - dispatch only calls on a claimed URL
        raise GitHubUnavailable("not a repo URL")
    data = await _fetch_repo(*claim, deadline=deadline, token=_token(cfg))
    readme = await _fetch_readme(data, deadline=deadline)
    repo = _repo_fields(data)

    warnings: list[str] = []
    if repo["archived"]:
        warnings.append("archived")
    if not readme:
        warnings.append("no_readme")

    from .. import io as io_mod

    markdown = _render_markdown(repo, readme)
    return envelope.success_envelope(
        base=_base_envelope(url, http_status=200),
        markdown=markdown,
        metadata={
            "title": repo["full_name"],
            "byline": None,
            "published": repo["created_at"],
            "modified": repo["pushed_at"],
            "language": None,
            "site_name": "GitHub",
            "image": None,
            "word_count": len(markdown.split()),
            "quality": {
                "provider": "github",
                "page_type": "repo",
                "result_count": 1,
                "repo": repo,
            },
            "warnings": warnings,
        },
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )
