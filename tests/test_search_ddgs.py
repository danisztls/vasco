from __future__ import annotations

from typing import Any

import pytest

from vasco import search
from vasco.adapters.ddgs import DdgsBackend


def test_no_results_yields_empty_not_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DDGS raises DDGSException('No results found.') for zero hits — the
    backend should swallow it and yield nothing instead of propagating."""
    from ddgs.exceptions import DDGSException

    class _DDGS:
        def __enter__(self) -> "_DDGS":
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
        def __enter__(self) -> "_DDGS":
            return self

        def __exit__(self, *a: Any) -> None: ...

        def text(self, *a: Any, **kw: Any) -> Any:
            raise DDGSException("rate limited")

    monkeypatch.setattr("ddgs.DDGS", _DDGS)
    with pytest.raises(DDGSException):
        list(DdgsBackend().search("anything"))


def test_get_searcher_returns_ddgs_backend() -> None:
    for name in ("ddg", "ddgs", "duckduckgo"):
        assert isinstance(search.get_searcher(name), DdgsBackend)


def test_get_searcher_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown search backend"):
        search.get_searcher("kagi")
