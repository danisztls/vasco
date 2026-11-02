# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Any

import pytest

from vasco import search
from vasco.adapters import ddgs as ddgs_mod
from vasco.adapters.ddgs import DdgsBackend


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the retry delay instead of waiting it out."""
    delays: list[float] = []
    monkeypatch.setattr(ddgs_mod, "sleep", delays.append)
    return delays


def _scripted_ddgs(monkeypatch: pytest.MonkeyPatch, answers: list[Any]) -> list[str]:
    """Stub DDGS to give one answer per call (an exception is raised); returns
    the list of queries it was called with."""
    calls: list[str] = []

    class _DDGS:
        def __enter__(self) -> _DDGS:
            return self

        def __exit__(self, *a: Any) -> None: ...

        def text(self, query: str, **kw: Any) -> Any:
            calls.append(query)
            answer = answers[len(calls) - 1]
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr("ddgs.DDGS", _DDGS)
    return calls


_HIT = {"title": "t", "href": "https://example.com/", "body": "b"}


def test_empty_answer_is_retried_once(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    from ddgs.exceptions import DDGSException

    calls = _scripted_ddgs(monkeypatch, [DDGSException("No results found."), [_HIT]])
    results = list(DdgsBackend().search("q", site="example.com"))
    assert [r.url for r in results] == ["https://example.com/"]
    assert calls == ["site:example.com q"] * 2
    assert sleeps == [ddgs_mod._EMPTY_RETRY_DELAY_SECONDS]


def test_empty_twice_gives_up(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    from ddgs.exceptions import DDGSException

    empty = DDGSException("No results found.")
    calls = _scripted_ddgs(monkeypatch, [empty, empty])
    assert list(DdgsBackend().search("q")) == []
    assert len(calls) == 2
    assert len(sleeps) == 1


def test_failed_retry_keeps_the_empty_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    from ddgs.exceptions import DDGSException, TimeoutException

    answers = [DDGSException("No results found."), TimeoutException("timed out")]
    _scripted_ddgs(monkeypatch, answers)
    assert list(DdgsBackend().search("q")) == []


def test_results_are_not_retried(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    calls = _scripted_ddgs(monkeypatch, [[_HIT]])
    assert len(list(DdgsBackend().search("q"))) == 1
    assert len(calls) == 1
    assert sleeps == []


def test_rows_without_a_url_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted_ddgs(monkeypatch, [[{"title": "no url", "href": ""}, _HIT]])
    assert [r.url for r in DdgsBackend().search("q")] == ["https://example.com/"]


def test_only_urlless_rows_counts_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scripted_ddgs(monkeypatch, [[{"title": "no url", "href": ""}], [_HIT]])
    assert len(list(DdgsBackend().search("q"))) == 1
    assert len(calls) == 2


def test_pinned_backends_are_all_enabled_in_ddgs() -> None:
    """ddgs silently skips (with a warning) an engine it has disabled, so a
    dropped engine should fail here rather than quietly shrink coverage."""
    from ddgs.engines import ENGINES

    pinned = set(ddgs_mod._DDGS_TEXT_BACKENDS.split(","))
    assert pinned <= set(ENGINES["text"])


def test_no_results_yields_empty_not_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DDGS raises DDGSException('No results found.') for zero hits — the
    backend should swallow it and yield nothing instead of propagating."""
    from ddgs.exceptions import DDGSException

    class _DDGS:
        def __enter__(self) -> _DDGS:
            return self

        def __exit__(self, *a: Any) -> None: ...

        def text(self, *a: Any, **kw: Any) -> Any:
            raise DDGSException("No results found.")

    monkeypatch.setattr("ddgs.DDGS", _DDGS)
    assert list(DdgsBackend().search("nonexistent query")) == []


def test_other_ddgs_exception_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Genuine DDGS errors (network, rate limit) still raise."""
    from ddgs.exceptions import DDGSException

    class _DDGS:
        def __enter__(self) -> _DDGS:
            return self

        def __exit__(self, *a: Any) -> None: ...

        def text(self, *a: Any, **kw: Any) -> Any:
            raise DDGSException("rate limited")

    monkeypatch.setattr("ddgs.DDGS", _DDGS)
    with pytest.raises(DDGSException):
        list(DdgsBackend().search("anything"))


def test_get_searcher_returns_guarded_ddgs_backend() -> None:
    """The factory wraps every backend in the ASCII-smuggling guard, so a future
    backend inherits it instead of having to remember it."""
    for name in ("ddg", "ddgs", "duckduckgo"):
        searcher = search.get_searcher(name)
        assert isinstance(searcher, search._GuardedSearcher)
        assert isinstance(searcher._inner, DdgsBackend)


def test_get_searcher_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown search backend"):
        search.get_searcher("kagi")
