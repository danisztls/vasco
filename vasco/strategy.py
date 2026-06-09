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
    # MercadoLivre serves a bot-challenge shell on the http tier across every
    # surface; start at the browser. registered_domain collapses www./lista./
    # produto. to mercadolivre.com.br, so this bare-domain key covers product +
    # search + category routes via seed_strategy's prefix match.
    "mercadolivre.com.br": "browser",
    # AliExpress runs Alibaba's baxia/x5sec punish stack: the http tier only ever
    # gets the `_____tmd_____/punish` stub, so start at the browser for every route
    # (pt./www./m. collapse to aliexpress.com via the bare-domain prefix match).
    # NB: success depends on the persistent browser profile holding an earned
    # x5secdata clearance — a cold profile gets the nc slider (which bot_detect now
    # flags BLOCKED_CAPTCHA so the manual-VNC solve flow can re-earn it). Both the
    # global domain (pt./www./m.) and the BR country domain are seeded.
    "aliexpress.com": "browser",
    "aliexpress.com.br": "browser",
    # Shopee serves a bot-challenge SPA shell on the http tier; the Product
    # JSON-LD spine only lands once the browser tier renders the page. The
    # bare-domain key prefix-matches every product route under shopee.com.br.
    "shopee.com.br": "browser",
    # NOT seeded on purpose: sites the browser tier *also* fails on (by default).
    # Seeding only helps where the browser succeeds — spending the expensive tier
    # on a doomed fetch and skipping the cheap http fail-fast is a net loss. Leave
    # them at the default (http first) so the chain fails cheap and the per-reason
    # negative cache backs off.
    #   - poder360.com.br / jornalfolha1.com.br: Cloudflare-Turnstile/interstitial.
    #     When `browser.solve_turnstile` is enabled (real virtual-display browser +
    #     humanized checkbox click, see browser_server._maybe_solve_turnstile) the
    #     browser tier CAN clear these — but the auto chain escalates http→browser
    #     on its own, so the solve still runs there without a seed. Seeding is left
    #     off so the default (solve disabled) doesn't waste the browser tier; flip
    #     a route to "browser" here only to skip the doomed http hop once solving
    #     is on and proven for that site.
    #   - arstechnica.com: JS/consent wall — even the rendered browser gets a
    #     "requires JavaScript" shell (→ js_app_needs_interaction at the browser),
    #     a different class from Turnstile; verified empirically with `--mode browser`.
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
