# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vasco.adapters import npm as N
from vasco.errors import AdapterParseError, FailureReason

BODY = (Path(__file__).parent / "fixtures" / "npm" / "left_pad.json").read_text("utf-8")
URL = "https://www.npmjs.com/package/left-pad"
DOWNLOADS = '{"downloads": 1234567, "package": "left-pad"}'


@pytest.mark.parametrize(
    "url, expected",
    [
        (URL, ("left-pad", None)),
        ("https://npmjs.com/package/left-pad/", ("left-pad", None)),
        (f"{URL}/v/1.0.0", ("left-pad", "1.0.0")),
        ("https://www.npmjs.com/package/@types/node", ("@types/node", None)),
        ("https://www.npmjs.com/package/%40types%2Fnode", ("@types/node", None)),
        ("https://www.npmjs.com/package/@types/node/v/1.2.3", ("@types/node", "1.2.3")),
        (f"{URL}?activeTab=versions", ("left-pad", None)),
        ("https://www.npmjs.com/search?q=react", None),
        ("https://www.npmjs.com/~someone", None),
        ("https://www.npmjs.com/package/", None),
        ("https://example.com/package/left-pad", None),
        ("", None),
    ],
)
def test_claim(url: str, expected: tuple | None) -> None:
    assert N._claim(url) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("git+https://github.com/a/b.git", "https://github.com/a/b"),
        ({"url": "git://github.com/a/b.git"}, "https://github.com/a/b"),
        ({"url": "ssh://git@github.com/a/b.git"}, "https://github.com/a/b"),
        ("git@github.com:a/b.git", "https://github.com/a/b"),
        (None, None),
    ],
)
def test_repository_url(value: object, expected: str | None) -> None:
    assert N._repository_url(value) == expected


def test_parse_packument_rejects_other_shapes() -> None:
    with pytest.raises(AdapterParseError):
        N._parse_packument("<html>", None)
    with pytest.raises(AdapterParseError):
        N._parse_packument('{"error": "nope"}', None)


def _stub(monkeypatch: pytest.MonkeyPatch, main: tuple, downloads: tuple) -> list[str]:
    calls: list[str] = []

    async def fake(url: str, *, deadline: float):
        calls.append(url)
        return downloads if "api.npmjs.org" in url else main

    monkeypatch.setattr(N, "_api_get", fake)
    return calls


async def test_fetch_package(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub(
        monkeypatch, (BODY, 200, FailureReason.OK), (DOWNLOADS, 200, FailureReason.OK)
    )
    env = await N.fetch_npm(URL)
    assert "failure" not in env
    assert env["mode_used"] == "npm"
    pkg = env["quality"]["package"]
    assert (pkg["version"], pkg["latest"]) == ("1.3.0", "1.3.0")
    assert pkg["weekly_downloads"] == 1234567
    assert pkg["license"] == "WTFPL"
    assert "readme" not in pkg
    assert "- Weekly downloads: 1,234,567" in env["markdown"]
    assert "## README" in env["markdown"]
    assert env["warnings"] == ["deprecated"]
    assert env["quality"]["result_count"] == 1
    assert len(calls) == 2


async def test_scoped_package_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub(
        monkeypatch, (BODY, 200, FailureReason.OK), ("", 500, FailureReason.OK)
    )
    await N.fetch_npm("https://www.npmjs.com/package/@types/node")
    assert "https://registry.npmjs.org/@types%2Fnode" in calls
    assert "https://api.npmjs.org/downloads/point/last-week/@types/node" in calls


async def test_older_version_flags_the_readme(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(
        monkeypatch, (BODY, 200, FailureReason.OK), (DOWNLOADS, 200, FailureReason.OK)
    )
    env = await N.fetch_npm(f"{URL}/v/1.0.0")
    assert env["quality"]["package"]["version"] == "1.0.0"
    assert env["quality"]["package"]["latest"] == "1.3.0"
    assert "readme_from_latest" in env["warnings"]


async def test_download_failure_keeps_the_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, (BODY, 200, FailureReason.OK), ("", 0, FailureReason.TIMEOUT))
    env = await N.fetch_npm(URL)
    assert "failure" not in env
    assert env["quality"]["package"]["weekly_downloads"] is None
    assert "Weekly downloads" not in env["markdown"]


def _without_readme(**repository: str) -> str:
    doc = json.loads(BODY)
    doc["readme"] = ""
    doc["versions"]["1.3.0"]["repository"] = repository
    return json.dumps(doc)


async def test_empty_readme_comes_from_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _without_readme(url="git+https://github.com/a/b.git", directory="packages/c")
    raw = "https://raw.githubusercontent.com/a/b/HEAD/packages/c/README.md"

    async def fake(url: str, *, deadline: float):
        if url == raw:
            return "# From the repo", 200, FailureReason.OK
        if "api.npmjs.org" in url:
            return DOWNLOADS, 200, FailureReason.OK
        return body, 200, FailureReason.OK

    monkeypatch.setattr(N, "_api_get", fake)
    env = await N.fetch_npm(URL)
    assert env["markdown"].endswith("# From the repo")
    assert env["warnings"] == ["deprecated", "readme_from_repository"]


async def test_no_readme_anywhere_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _without_readme(url="https://example.org/a/b")
    calls = _stub(
        monkeypatch, (body, 200, FailureReason.OK), (DOWNLOADS, 200, FailureReason.OK)
    )
    env = await N.fetch_npm(URL)
    assert env["warnings"] == ["deprecated", "no_readme"]
    assert len(calls) == 2  # a non-GitHub repo isn't guessed at


@pytest.mark.parametrize(
    "url, main, reason",
    [
        (
            URL,
            ('{"error":"not found"}', 404, FailureReason.OK),
            FailureReason.NOT_FOUND,
        ),
        (f"{URL}/v/9.9.9", (BODY, 200, FailureReason.OK), FailureReason.NOT_FOUND),
        (URL, ("<html>", 200, FailureReason.OK), FailureReason.PARSE_FAILED),
        (URL, ("", 503, FailureReason.OK), FailureReason.SERVER_ERROR),
        (URL, ("", 0, FailureReason.TIMEOUT), FailureReason.TIMEOUT),
    ],
)
async def test_failures(
    monkeypatch: pytest.MonkeyPatch, url: str, main: tuple, reason: FailureReason
) -> None:
    _stub(monkeypatch, main, (DOWNLOADS, 200, FailureReason.OK))
    env = await N.fetch_npm(url)
    assert env["failure"]["reason"] == reason.value
