# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from vasco.adapters import github as G

BODY = (Path(__file__).parent / "fixtures" / "github" / "flask.json").read_text("utf-8")
URL = "https://github.com/pallets/flask"
RAW = "https://raw.githubusercontent.com/pallets/flask/main"


@pytest.mark.parametrize(
    "url, expected",
    [
        (URL, ("pallets", "flask")),
        (f"{URL}/", ("pallets", "flask")),
        (f"{URL}.git", ("pallets", "flask")),
        (f"{URL}?tab=readme-ov-file", ("pallets", "flask")),
        ("https://www.github.com/pallets/flask", ("pallets", "flask")),
        (f"{URL}/issues/1", None),
        (f"{URL}/tree/main", None),
        ("https://github.com/pallets", None),
        ("https://github.com/topics/python", None),
        ("https://github.com/features/actions", None),
        ("https://gitlab.com/pallets/flask", None),
        ("", None),
    ],
)
def test_claim(url: str, expected: tuple | None) -> None:
    assert G._claim(url) == expected


def _stub(monkeypatch: pytest.MonkeyPatch, answers: dict[str, tuple[str, int]]) -> list:
    calls: list[tuple[str, dict]] = []

    async def fake(url: str, headers: dict, *, deadline: float):
        calls.append((url, headers))
        return answers.get(url, ("", 404))

    monkeypatch.setattr(G, "_http_get", fake)
    return calls


API = "https://api.github.com/repos/pallets/flask"


async def test_fetch_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub(
        monkeypatch,
        {
            API: (BODY, 200),
            f"{RAW}/README.md": ("# Flask\n\nSee [docs](docs/a.md).", 200),
        },
    )
    env = await G.fetch_github(URL)
    assert env["mode_used"] == "github"
    repo = env["quality"]["repo"]
    assert repo["stars"] == 75166
    assert repo["license"] == "BSD-3-Clause"
    assert "- Stars: 75,166" in env["markdown"]
    assert (
        "[docs](https://github.com/pallets/flask/blob/main/docs/a.md)"
        in env["markdown"]
    )
    assert env["warnings"] == []
    assert len(calls) == 2  # one API request, one README


async def test_tries_other_readme_names(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {API: (BODY, 200), f"{RAW}/README.rst": ("Flask docs", 200)})
    env = await G.fetch_github(URL)
    assert env["markdown"].endswith("Flask docs")


async def test_missing_readme_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {API: (BODY, 200)})
    env = await G.fetch_github(URL)
    assert env["warnings"] == ["no_readme"]


@pytest.mark.parametrize(
    "answer",
    [("rate limited", 403), ("", 404), ("", 0), ("<html>", 200), ("{}", 200)],
)
async def test_api_miss_falls_through(
    monkeypatch: pytest.MonkeyPatch, answer: tuple[str, int]
) -> None:
    _stub(monkeypatch, {API: answer})
    with pytest.raises(G.GitHubUnavailable):
        await G.fetch_github(URL)


async def test_token_reaches_only_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub(monkeypatch, {API: (BODY, 200), f"{RAW}/README.md": ("x", 200)})
    cfg = SimpleNamespace(adapters=SimpleNamespace(github=SimpleNamespace(token="t0")))
    await G.fetch_github(URL, cfg=cfg)
    sent = {url: headers.get("Authorization") for url, headers in calls}
    assert sent[API] == "Bearer t0"
    assert sent[f"{RAW}/README.md"] is None


@pytest.mark.parametrize(
    "readme, expected",
    [
        ("[a](docs/a.md)", "[a](https://github.com/o/r/blob/main/docs/a.md)"),
        ("![i](./i.png)", "![i](https://github.com/o/r/blob/main/./i.png)"),
        ("[a](https://x.y/z)", "[a](https://x.y/z)"),
        ("[a](#install)", "[a](#install)"),
        ("[a](mailto:a@b.c)", "[a](mailto:a@b.c)"),
    ],
)
def test_absolute_links(readme: str, expected: str) -> None:
    assert G._absolute_links(readme, "o/r", "main") == expected
