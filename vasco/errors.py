from __future__ import annotations

from enum import StrEnum


class FailureReason(StrEnum):
    OK = "ok"
    BLOCKED_CLOUDFLARE = "blocked_cloudflare"
    BLOCKED_CAPTCHA = "blocked_captcha"
    BLOCKED_BOT = "blocked_bot"
    PAYWALL_HARD = "paywall_hard"
    PAYWALL_SOFT_WITH_PARTIAL = "paywall_soft_with_partial"
    LOGIN_REQUIRED = "login_required"
    NOT_FOUND = "not_found"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    JS_APP_NEEDS_INTERACTION = "js_app_needs_interaction"
    DNS_FAIL = "dns_fail"
    ROBOTS_DISALLOW = "robots_disallow"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    INVALID_URL = "invalid_url"
    # Adapter-produced: HTML fetched fine (200 OK) but the source-specific
    # parser could not locate its structural anchor — i.e. the site changed its
    # markup. Distinct from UNSUPPORTED_CONTENT_TYPE (a genuinely unhandleable
    # content type) so it can carry a short, self-healing negative-cache TTL.
    # `bot_detect.classify` never emits this; only the content adapters do.
    PARSE_FAILED = "parse_failed"


class AdapterParseError(Exception):
    """A content adapter's parser could not find its structural anchor.

    Raised at the anchor-locator seam (e.g. the ``__NEXT_DATA__`` script is
    missing, no ``Product`` JSON-LD is present) to signal scraper-rot, as
    opposed to an anchor that is present but legitimately empty (zero results).
    The adapter's ``fetch_*`` turns this into a ``PARSE_FAILED`` failure
    envelope with this message — short and informative, never raw HTML.
    """
