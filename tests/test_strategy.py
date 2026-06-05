"""Tests for the centralized seed-strategy config (vasco/strategy.py)."""

from __future__ import annotations

import pytest

from vasco.cache import route_key
from vasco.strategy import SEED_STRATEGIES, seed_strategy


@pytest.mark.parametrize(
    "route,expected",
    [
        ("google.com/search", "browser"),
        ("google.com.br/search", "browser"),
        ("google.com/shopping", "browser"),
        # prefix match: category browse under /shopping
        ("google.com/shopping/electronics", "browser"),
        # prefix match: detail pages carry the /* suffix from route_key
        ("vivareal.com.br/imovel/*", "browser"),
        # list pages are deliberately NOT seeded (learned instead)
        ("vivareal.com.br/aluguel/*", None),
        # OLX is Cloudflare-protected site-wide: the bare-domain seed covers
        # list routes and every regional detail subdomain via prefix match.
        ("olx.com.br/imoveis/*", "browser"),
        ("olx.com.br/autos-e-pecas/*", "browser"),
        ("olx.com.br/sao-paulo-e-regiao/*", "browser"),
        ("olx.com.br", "browser"),
        # captcha/consent-walled sites are deliberately NOT seeded: the browser
        # tier fails on them too, so seeding would only waste the expensive tier.
        ("poder360.com.br/poder-brasil/*", None),
        ("jornalfolha1.com.br/2026/*", None),
        ("arstechnica.com/security/*", None),
        # unrelated routes
        ("example.com/foo", None),
        # trailing-slash guard: no partial-segment false match
        ("google.com/searchx", None),
        ("", None),
    ],
)
def test_seed_strategy(route: str, expected: str | None) -> None:
    assert seed_strategy(route) == expected


def test_seed_keys_match_real_route_keys() -> None:
    """Each seed key must equal (or prefix) the route_key of a representative
    URL, otherwise the seed silently never fires."""
    assert route_key("https://www.google.com/search?q=x&udm=28") == "google.com/search"
    assert (
        route_key("https://www.google.com.br/search?q=x&udm=28")
        == "google.com.br/search"
    )
    # vivareal detail → seed key is a prefix of the produced route_key
    detail = route_key("https://www.vivareal.com.br/imovel/apto-2q-id-12345/")
    assert detail == "vivareal.com.br/imovel/*"
    assert seed_strategy(detail) == "browser"


def test_every_seed_value_is_a_known_tier() -> None:
    assert set(SEED_STRATEGIES.values()) <= {"http", "browser"}
