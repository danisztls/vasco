"""Heuristic classification of fetch responses into FailureReasons.

Pure function module. Never raises. Returns a `FailureReason` derived from
status code, HTML body markers, and response headers.
"""

from __future__ import annotations

import re

from vasco.errors import FailureReason

# --- Marker sets (lowercase) --------------------------------------------------

_CLOUDFLARE_MARKERS: tuple[str, ...] = (
    "cf-mitigated",
    "just a moment...",
    "challenge-platform",
    "/cdn-cgi/challenge-platform/",
    "attention required! | cloudflare",
    "cf_chl_opt",
    "__cf_chl_",
)

_CAPTCHA_MARKERS: tuple[str, ...] = (
    'class="h-captcha"',
    "class='h-captcha'",
    'class="g-recaptcha"',
    "class='g-recaptcha'",
    "challenges.cloudflare.com/turnstile",
)
# Loading recaptcha/hcaptcha api.js alone is NOT a captcha challenge: tons of
# normal sites embed these libraries for contact-form anti-spam, with the
# widget rendered only after submit. The class markers above plus a Turnstile
# script URL are the real signal. If neither fires but the api.js does, treat
# the page as content unless it's small + challenge-shaped (handled below).

_LOGIN_MARKERS: tuple[str, ...] = (
    "log in to continue",
    "login to continue",
    "sign in to continue",
    "please log in",
    "you must be logged in",
    'action="/login"',
    "action='/login'",
    'action="/signin"',
    "action='/signin'",
    'action="/account/login"',
    'action="/auth/login"',
    'action="/users/sign_in"',
)

_PAYWALL_MARKERS: tuple[str, ...] = (
    "subscribe to continue",
    "subscribe to read",
    "subscribers only",
    "this article is for subscribers",
    "members only",
    'class="paywall"',
    "class='paywall'",
    'id="paywall',
    "id='paywall",
    "data-paywall",
)

_HARD_PAYWALL_SCHEMA = re.compile(
    r"""isAccessibleForFree["']?\s*[:=]\s*["']?False""",
    re.IGNORECASE,
)

# Explicit "you need JavaScript" notices an SPA shell renders as its no-JS
# fallback. Very high precision — real content pages don't surface these in their
# visible text — so any one of them flags an unrendered shell on its own.
#
# We deliberately do NOT classify shells from bare mount-point markers
# (`id="root"`/`id="app"`/`__next_data__`/…) anymore: that rule both over-fired
# (a server-rendered VitePress page mounts into `id="app"` yet has real content)
# and under-fired (Facebook ships ~1.2 MB of obfuscated script with no marker we
# enumerate). The robust "did anything render" signal is trafilatura's
# post-conversion `word_count`, which the fetch chain uses to escalate
# marker-less empty shells to the browser tier. This list stays because it
# catches the case word_count can't: a shell whose only text is boilerplate
# (MercadoLivre's no-JS notice converts to ~60 junk words, indistinguishable by
# count from a real thin page — but the explicit notice is unambiguous).
_JS_REQUIRED_MARKERS: tuple[str, ...] = (
    "requires javascript",
    "please enable javascript",
    "enable javascript to continue",
    "you need to enable javascript",
    "javascript is required",
    "javascript to be enabled",
)

# A very rough "visible text" estimate: strip tags and whitespace-collapse.
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_WS_RE = re.compile(r"\s+")

# Threshold (chars of visible text) below which a paywall page with
# pre-modal text is classified hard rather than soft.
_SOFT_PAYWALL_MIN_VISIBLE_CHARS = 400

# Real CF challenge pages have almost no visible text (just "Just a moment..."
# or "Attention Required!"). Pages with CF monitoring JS but substantial
# content are legitimate.
_CF_CHALLENGE_MAX_VISIBLE_CHARS = 2000

# A 200 carrying an SPA mount-point marker but under this many chars of visible
# text is an unrendered shell, not content — escalate to the browser tier. Real
# content pages that embed a framework marker have far more visible text; an
# unrendered shell has only its "loading…"/"enable JavaScript" fallback.
_JS_SHELL_MAX_VISIBLE_CHARS = 600


def _visible_text(html: str) -> str:
    stripped = _SCRIPT_STYLE_RE.sub(" ", html)
    stripped = _TAG_RE.sub(" ", stripped)
    return _WS_RE.sub(" ", stripped).strip()


def _has_any(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(n in haystack for n in needles)


def classify(
    status: int,
    html: str | None,
    headers: dict[str, str] | None,
) -> FailureReason:
    """Classify a response into a FailureReason.

    Pure, defensive: never raises. Order of checks matters — DNS/TIMEOUT
    sentinels and explicit status codes are evaluated before HTML body
    signatures so a 404 with weird content is still NOT_FOUND.
    """
    headers = headers or {}
    body = html or ""
    body_lc = body.lower()

    # Normalize headers to a case-insensitive view (lowercase keys).
    hdr_lc: dict[str, str] = {str(k).lower(): str(v) for k, v in headers.items()}

    # --- Sentinel statuses for connection-layer failures ----------------------
    if status == 0:
        hint = hdr_lc.get("_failure_hint", "").lower()
        if hint == "timeout":
            return FailureReason.TIMEOUT
        if hint == "bot_blocked":
            return FailureReason.BLOCKED_BOT
        if hint == "browser_unavailable":
            return FailureReason.BROWSER_UNAVAILABLE
        if hint == "dns_fail":
            return FailureReason.DNS_FAIL
        # Default to DNS_FAIL when we got literally nothing.
        return FailureReason.DNS_FAIL

    # --- Hard HTTP statuses ---------------------------------------------------
    # 410 Gone is RFC 9110's "intentional 404": treat the same way so that
    # auto-mode escalation can short-circuit instead of pointlessly retrying
    # in the browser tier.
    if status in (404, 410):
        return FailureReason.NOT_FOUND

    if status in (401, 403):
        # Login forms / "log in to continue" → LOGIN_REQUIRED.
        if _has_any(body_lc, _LOGIN_MARKERS):
            return FailureReason.LOGIN_REQUIRED
        # Cloudflare body signatures.
        if _has_any(body_lc, _CLOUDFLARE_MARKERS) or "cf-mitigated" in hdr_lc:
            return FailureReason.BLOCKED_CLOUDFLARE
        if _has_any(body_lc, _CAPTCHA_MARKERS):
            return FailureReason.BLOCKED_CAPTCHA
        # Fall back to LOGIN_REQUIRED for 401, generic CF for 403.
        if status == 401:
            return FailureReason.LOGIN_REQUIRED
        return FailureReason.BLOCKED_CLOUDFLARE

    if status == 429:
        # Rate-limited; classify as BLOCKED_CLOUDFLARE if CF markers present,
        # else SERVER_ERROR is the closest enum member for "retry later".
        if _has_any(body_lc, _CLOUDFLARE_MARKERS):
            return FailureReason.BLOCKED_CLOUDFLARE
        return FailureReason.SERVER_ERROR

    if 500 <= status < 600:
        return FailureReason.SERVER_ERROR

    # --- 2xx / 3xx: examine body ---------------------------------------------
    if 200 <= status < 400:
        # Cloudflare challenge served with a 200 status is common, but CF
        # monitoring JS (challenge-platform, cf_chl_opt) also appears on
        # legitimate pages. Only classify as blocked if the page is thin —
        # real challenge pages are <20KB with minimal visible text.
        if _has_any(body_lc, _CLOUDFLARE_MARKERS) or "cf-mitigated" in hdr_lc:
            if len(_visible_text(body)) < _CF_CHALLENGE_MAX_VISIBLE_CHARS:
                return FailureReason.BLOCKED_CLOUDFLARE

        if _has_any(body_lc, _CAPTCHA_MARKERS):
            return FailureReason.BLOCKED_CAPTCHA

        # Paywalls: hard if schema marker or near-empty body around paywall,
        # otherwise soft.
        has_paywall = _has_any(body_lc, _PAYWALL_MARKERS)
        if _HARD_PAYWALL_SCHEMA.search(body):
            return FailureReason.PAYWALL_HARD
        if has_paywall:
            # If there's substantial pre-paywall content, treat as soft.
            visible = _visible_text(body)
            if len(visible) < _SOFT_PAYWALL_MIN_VISIBLE_CHARS:
                return FailureReason.PAYWALL_HARD
            return FailureReason.PAYWALL_SOFT_WITH_PARTIAL

        # A 200 carrying an explicit "enable/requires JavaScript" notice but almost
        # no visible text is an unrendered SPA shell rendering its no-JS fallback.
        # Escalating lets the auto chain render it in the browser tier instead of
        # caching an empty 200 as success. This handles the shell whose no-JS
        # boilerplate converts to a handful of junk words (e.g. MercadoLivre); the
        # marker-less empty shell (e.g. Facebook) is caught downstream by the
        # post-conversion `word_count == 0` escalation instead.
        if len(_visible_text(body)) < _JS_SHELL_MAX_VISIBLE_CHARS and _has_any(
            body_lc, _JS_REQUIRED_MARKERS
        ):
            return FailureReason.JS_APP_NEEDS_INTERACTION

        return FailureReason.OK

    # Anything else (e.g. 3xx that didn't follow, exotic codes).
    return FailureReason.SERVER_ERROR
