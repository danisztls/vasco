from __future__ import annotations

from enum import StrEnum


class FailureReason(StrEnum):
    OK = "ok"
    BLOCKED_CLOUDFLARE = "blocked_cloudflare"
    BLOCKED_CAPTCHA = "blocked_captcha"
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
