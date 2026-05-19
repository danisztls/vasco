"""Heuristic classification of fetch responses into FailureReasons.

Pure function module. Never raises. Returns a `FailureReason` derived from
status code, HTML body markers, and response headers.
"""

from __future__ import annotations

import re

from .errors import FailureReason

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
    "www.google.com/recaptcha/api.js",
    "hcaptcha.com/1/api.js",
    "challenges.cloudflare.com/turnstile",
)

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
    "id=\"paywall",
    "id='paywall",
    "data-paywall",
)

_HARD_PAYWALL_SCHEMA = re.compile(
    r"""isAccessibleForFree["']?\s*[:=]\s*["']?False""",
    re.IGNORECASE,
)

_JS_APP_MARKERS: tuple[str, ...] = (
    "__next_data__",
    "ng-version",
    'id="root"></div>',
    "id='root'></div>",
    'id="app"></div>',
    "id='app'></div>",
    "__nuxt__",
    'data-reactroot=""',
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
    hdr_lc: dict[str, str] = {
        str(k).lower(): str(v) for k, v in headers.items()
    }

    # --- Sentinel statuses for connection-layer failures ----------------------
    if status == 0:
        hint = hdr_lc.get("_failure_hint", "").lower()
        if hint == "timeout":
            return FailureReason.TIMEOUT
        if hint == "dns_fail":
            return FailureReason.DNS_FAIL
        # Default to DNS_FAIL when we got literally nothing.
        return FailureReason.DNS_FAIL

    # --- Hard HTTP statuses ---------------------------------------------------
    if status == 404:
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
        # Cloudflare challenge served with a 200 status is common.
        if (
            _has_any(body_lc, _CLOUDFLARE_MARKERS)
            or "cf-mitigated" in hdr_lc
        ):
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

        # Empty-ish HTML on 200 with SPA framework markers.
        if len(body) < 1024 and _has_any(body_lc, _JS_APP_MARKERS):
            return FailureReason.JS_APP_NEEDS_INTERACTION

        return FailureReason.OK

    # Anything else (e.g. 3xx that didn't follow, exotic codes).
    return FailureReason.SERVER_ERROR
