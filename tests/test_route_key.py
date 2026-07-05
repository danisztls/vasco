"""Table-driven tests for `urls.route_key` — the per-route strategy key.

These cases ARE the spec: list vs detail must differ; cities and listing-ids
must collapse; homepages degrade to the bare domain.
"""

from __future__ import annotations

import pytest

from vasco.urls import route_key


@pytest.mark.parametrize(
    "url,expected",
    [
        # vivareal: list pages collapse across cities/states ...
        (
            "https://www.vivareal.com.br/aluguel/sp/sao-carlos/",
            "vivareal.com.br/aluguel/*",
        ),
        (
            "https://www.vivareal.com.br/aluguel/mg/belo-horizonte/",
            "vivareal.com.br/aluguel/*",
        ),
        # ... a different transaction type is a distinct route ...
        ("https://www.vivareal.com.br/venda/sp/campinas/", "vivareal.com.br/venda/*"),
        # ... and detail pages collapse across listing ids, distinct from lists.
        (
            "https://www.vivareal.com.br/imovel/apartamento-2-quartos-id-12345/",
            "vivareal.com.br/imovel/*",
        ),
        (
            "https://www.vivareal.com.br/imovel/casa-3-quartos-id-99999/",
            "vivareal.com.br/imovel/*",
        ),
        # homepage / empty path → bare domain
        ("https://www.vivareal.com.br/", "vivareal.com.br"),
        ("https://www.vivareal.com.br", "vivareal.com.br"),
        # single structural segment keeps the segment literal (no trailing /*)
        (
            "https://www.example.com/imoveis/",
            "example.com/imoveis",
        ),
        # digit-leading first segment is variable → wildcarded
        ("https://news.example.co.uk/2024/07/some-story", "example.co.uk/*"),
        # eTLD+1 with secondary suffix is preserved
        ("https://shop.example.co.uk/products/widget", "example.co.uk/products/*"),
        # www. stripping is consistent with registered_domain
        ("https://www.example.com/blog/post-1", "example.com/blog/*"),
        # bare host, no scheme
        ("example.com/foo/bar", "example.com/foo/*"),
        # empty input
        ("", ""),
    ],
)
def test_route_key(url: str, expected: str) -> None:
    assert route_key(url) == expected


def test_list_and_detail_keys_differ() -> None:
    """The motivating invariant: list and detail routes never share a key."""
    lst = route_key("https://www.vivareal.com.br/aluguel/sp/sao-carlos/")
    detail = route_key("https://www.vivareal.com.br/imovel/casa-id-7/")
    assert lst != detail
