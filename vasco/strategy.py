"""Centralized, declarative seed strategies for the fetch escalation chain.

This is the single place known per-route tier knowledge lives. Content adapters
(Google Shopping, real-estate) no longer hardcode "always use the browser" in
their own fetch code — they route through the shared `http → browser → mobile →
wayback` chain and rely on the seeds here to pick the right *starting* tier.

`SEED_STRATEGIES` maps a route key (a `cache.route_key` value, or a path-prefix
of one) to the tier the auto-mode chain should start at. These are **seeds, not
overrides**: a seed only applies when no learned strategy row exists yet
(`cache.get_strategy` returns None). Once the chain records an outcome the
learned value wins and can still flip the tier via the normal 3-strike logic —
so a site that changes its protection self-heals.
"""

from __future__ import annotations

SEED_STRATEGIES: dict[str, str] = {
    # Google Shopping is a JS-rendered SPA on every surface; the http tier only
    # ever returns an empty shell, so the chain must start at the browser.
    "google.com/search": "browser",
    "google.com.br/search": "browser",
    "google.com/shopping": "browser",
    "google.com.br/shopping": "browser",
    # vivareal listing *detail* pages are bot-protected and need the browser;
    # list pages (`/aluguel`, `/venda`, ...) are not and are left to be learned
    # (they resolve at the cheap http tier).
    "vivareal.com.br/imovel": "browser",
    # OLX is Cloudflare-protected site-wide; the http tier reliably gets a 403
    # challenge, so start at the browser for every OLX route (list + every
    # regional detail subdomain — the bare-domain key covers them all).
    "olx.com.br": "browser",
}


def seed_strategy(route_key: str) -> str | None:
    """Return the seeded starting tier for a route, or None if unseeded.

    Matches the longest seed key that equals the route or is a path-prefix of
    it, so ``vivareal.com.br/imovel`` also covers ``vivareal.com.br/imovel/*``
    and ``google.com/shopping`` covers ``google.com/shopping/<category>``. The
    trailing-slash guard prevents partial-segment false matches (``/search``
    never matches ``/searchx``).
    """
    if not route_key:
        return None
    best: tuple[int, str] | None = None
    for key, tier in SEED_STRATEGIES.items():
        if route_key == key or route_key.startswith(key + "/"):
            if best is None or len(key) > best[0]:
                best = (len(key), tier)
    return best[1] if best else None
