# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""npm package adapter (public registry JSON, no auth).

``www.npmjs.com`` sits behind Cloudflare and its page yields little structure,
while ``registry.npmjs.org`` and ``api.npmjs.org`` are public JSON APIs. Only
package pages are claimed (``/package/<name>``, optionally ``/v/<version>``);
everything else on the site falls through to a normal fetch.

Fetched through its own minimal httpx client, like :mod:`vasco.adapters.gitlab`:
a JSON API gains nothing from the browser tier or the wayback tail. The registry
is authoritative, so a 404 or an unknown version is an honest ``NOT_FOUND``. The
download count is best-effort. Never raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from .. import envelope
from ..errors import AdapterParseError, FailureReason
from . import _common

log = logging.getLogger(__name__)

_REGISTRY = "https://registry.npmjs.org"
_DOWNLOADS = "https://api.npmjs.org/downloads/point/last-week"
_HOSTS = frozenset({"npmjs.com", "www.npmjs.com"})
_PACKAGE_RE = re.compile(r"^/package/((?:@[^/]+/)?[^/@]+)(?:/v/([^/]+))?/?$")
_NO_README = "ERROR: No README data found!"
_USER_AGENT = "vasco"

_base_envelope, _failure_envelope = _common.envelope_builders(
    "npm", "application/x-npm"
)


def _claim(url: str) -> tuple[str, str | None] | None:
    """``(package, version)`` for a package page, else None."""
    parts = urlsplit(url or "")
    if (parts.hostname or "").lower() not in _HOSTS:
        return None
    match = _PACKAGE_RE.match(unquote(parts.path or ""))
    return (match.group(1), match.group(2)) if match else None


def is_npm_url(url: str) -> bool:
    return _claim(url) is not None


async def _api_get(url: str, *, deadline: float) -> tuple[str, int, FailureReason]:
    """GET ``url`` → ``(body, status, reason)``. Never raises; the test seam."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=max(1.0, float(deadline))
        ) as client:
            resp = await client.get(
                url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"}
            )
        return resp.text, resp.status_code, FailureReason.OK
    except httpx.TimeoutException:
        return "", 0, FailureReason.TIMEOUT
    except Exception:
        return "", 0, FailureReason.SERVER_ERROR


def _repository_url(value: Any) -> str | None:
    raw = value.get("url") if isinstance(value, Mapping) else value
    if not isinstance(raw, str) or not raw:
        return None
    url = re.sub(r"^git\+", "", raw)
    url = re.sub(r"^git://", "https://", url)
    url = re.sub(r"^ssh://git@", "https://", url)
    url = re.sub(r"^git@([^:]+):", r"https://\1/", url)
    return re.sub(r"\.git$", "", url)


def _license(value: Any) -> str | None:
    found = value.get("type") if isinstance(value, Mapping) else value
    return found if isinstance(found, str) and found else None


def _repo_readme_url(pkg: dict[str, Any]) -> str | None:
    """Raw README location on GitHub, including a monorepo package's directory."""
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)", pkg["repository"] or "")
    if not match:
        return None
    directory = (pkg["repository_directory"] or "").strip("/")
    path = f"{directory}/README.md" if directory else "README.md"
    return f"https://raw.githubusercontent.com/{match.group(1)}/HEAD/{quote(path)}"


def _parse_packument(body: str, version: str | None) -> dict[str, Any] | None:
    """Registry document → package fields, or None for an unknown version.

    Raises ``AdapterParseError`` when the body isn't a package document.
    """
    try:
        doc = json.loads(body)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AdapterParseError("registry: response was not JSON") from exc
    versions = doc.get("versions") if isinstance(doc, dict) else None
    tags = doc.get("dist-tags") if isinstance(doc, dict) else None
    if not (isinstance(doc, dict) and doc.get("name") and isinstance(versions, dict)):
        raise AdapterParseError("registry: response has no package name or versions")
    tags = tags if isinstance(tags, dict) else {}
    latest = tags.get("latest")
    chosen = version or latest
    info = versions.get(chosen)
    if not isinstance(info, dict):
        return None
    times = doc.get("time") if isinstance(doc.get("time"), dict) else {}
    dist = info.get("dist") if isinstance(info.get("dist"), dict) else {}
    deps = info.get("dependencies")
    readme = doc.get("readme") if isinstance(doc.get("readme"), str) else ""
    repository = info.get("repository") or doc.get("repository")
    return {
        "name": doc["name"],
        "version": chosen,
        "latest": latest,
        "description": info.get("description") or doc.get("description") or None,
        "license": _license(info.get("license") or doc.get("license")),
        "homepage": info.get("homepage") or doc.get("homepage") or None,
        "repository": _repository_url(repository),
        "repository_directory": repository.get("directory")
        if isinstance(repository, Mapping)
        else None,
        "keywords": [k for k in (doc.get("keywords") or []) if isinstance(k, str)],
        "maintainers": [
            m["name"]
            for m in (doc.get("maintainers") or [])
            if isinstance(m, dict) and isinstance(m.get("name"), str)
        ],
        "dependencies": sorted(deps) if isinstance(deps, dict) else [],
        "dist_tags": tags,
        "version_count": len(versions),
        "unpacked_size": dist.get("unpackedSize"),
        "published": times.get(chosen),
        "modified": times.get("modified"),
        "deprecated": info.get("deprecated") or None,
        "weekly_downloads": None,
        "readme": "" if readme.strip() == _NO_README else readme.strip(),
    }


def _parse_downloads(result: Any) -> int | None:
    if not isinstance(result, tuple) or result[1] != 200:
        return None
    try:
        count = json.loads(result[0]).get("downloads")
    except (json.JSONDecodeError, AttributeError):
        return None
    return count if isinstance(count, int) else None


def _render_markdown(pkg: dict[str, Any]) -> str:
    rows = [
        ("Weekly downloads", f"{pkg['weekly_downloads']:,}")
        if pkg["weekly_downloads"] is not None
        else None,
        ("Latest version", pkg["latest"]),
        ("Published", pkg["published"]),
        ("License", pkg["license"]),
        ("Repository", pkg["repository"]),
        ("Homepage", pkg["homepage"]),
        ("Dependencies", ", ".join(pkg["dependencies"]) or "none"),
        ("Maintainers", ", ".join(pkg["maintainers"])),
        ("Deprecated", pkg["deprecated"]),
    ]
    lines = [f"# {pkg['name']} {pkg['version']}", ""]
    if pkg["description"]:
        lines += [pkg["description"], ""]
    lines += [f"- {label}: {value}" for label, value in filter(None, rows) if value]
    if pkg["readme"]:
        lines += ["", "## README", "", pkg["readme"]]
    return "\n".join(lines)


async def fetch_npm(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
    fetch_html: Any | None = None,
) -> dict[str, Any]:
    """Fetch an npm package page's data → a structured envelope. Never raises."""
    claim = _claim(url)
    if claim is None:  # pragma: no cover - dispatch only calls on a claimed URL
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, "npm: unrecognized URL shape"
        )
    name, version = claim
    main, downloads = await asyncio.gather(
        _api_get(f"{_REGISTRY}/{quote(name, safe='@')}", deadline=deadline),
        _api_get(f"{_DOWNLOADS}/{quote(name, safe='@/')}", deadline=deadline),
        return_exceptions=True,
    )
    if isinstance(main, BaseException):
        return _failure_envelope(url, FailureReason.SERVER_ERROR, f"npm: {main}")
    body, status, reason = main
    if reason != FailureReason.OK:
        return _failure_envelope(url, reason, "npm: registry request failed")
    if status == 404:
        return _failure_envelope(
            url,
            FailureReason.NOT_FOUND,
            f"npm: no package named {name}",
            http_status=404,
        )
    if status != 200:
        return _failure_envelope(
            url,
            FailureReason.SERVER_ERROR,
            f"npm: registry returned {status}",
            http_status=status,
        )
    try:
        # Popular packages' documents run to several MB, so keep the parse off
        # the event loop that vascod shares with every other request.
        pkg = await asyncio.to_thread(_parse_packument, body, version)
    except AdapterParseError as exc:
        log.warning("npm parse anchor missing: %s", exc)
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, f"npm {exc}", http_status=status
        )
    if pkg is None:
        return _failure_envelope(
            url,
            FailureReason.NOT_FOUND,
            f"npm: {name} has no version {version}",
            http_status=404,
        )
    pkg["weekly_downloads"] = _parse_downloads(downloads)

    warnings: list[str] = []
    if pkg["deprecated"]:
        warnings.append("deprecated")
    if pkg["readme"]:
        if pkg["version"] != pkg["latest"]:
            warnings.append("readme_from_latest")  # the registry keeps only that one
    else:
        # Some packages (react) publish no README; the repo usually has one.
        repo_readme = _repo_readme_url(pkg)
        if repo_readme:
            text, repo_status, _ = await _api_get(repo_readme, deadline=deadline)
            if repo_status == 200 and text.strip():
                pkg["readme"] = text.strip()
                warnings.append("readme_from_repository")
        if not pkg["readme"]:
            warnings.append("no_readme")

    from .. import io as io_mod

    markdown = _render_markdown(pkg)
    return envelope.success_envelope(
        base=_base_envelope(url, http_status=status),
        markdown=markdown,
        metadata={
            "title": f"{pkg['name']} {pkg['version']}",
            "byline": None,
            "published": pkg["published"],
            "modified": pkg["modified"],
            "language": None,
            "site_name": "npm",
            "image": None,
            "word_count": len(markdown.split()),
            "quality": {
                "provider": "npm",
                "page_type": "package",
                "result_count": 1,
                "package": {k: v for k, v in pkg.items() if k != "readme"},
            },
            "warnings": warnings,
        },
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )
