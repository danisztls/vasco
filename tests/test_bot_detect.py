"""Tests for `vasco.bot_detect.classify`.

Drives each fixture HTML file through the classifier with the expected
status code and asserts the resulting FailureReason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vasco.fetch.bot_detect import classify
from vasco.errors import FailureReason


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_cloudflare_challenge_at_200_is_blocked_cloudflare() -> None:
    html = _load("cloudflare_challenge.html")
    assert classify(200, html, {}) == FailureReason.BLOCKED_CLOUDFLARE


def test_paywall_soft_at_200_is_soft_with_partial() -> None:
    html = _load("paywall_soft.html")
    assert classify(200, html, {}) == FailureReason.PAYWALL_SOFT_WITH_PARTIAL


def test_paywall_hard_at_200_is_hard() -> None:
    html = _load("paywall_hard.html")
    assert classify(200, html, {}) == FailureReason.PAYWALL_HARD


def test_clean_article_at_200_is_ok() -> None:
    html = _load("article_clean.html")
    assert classify(200, html, {}) == FailureReason.OK


def test_empty_html_at_404_is_not_found() -> None:
    assert classify(404, "", {}) == FailureReason.NOT_FOUND
    assert classify(404, None, None) == FailureReason.NOT_FOUND


def test_410_gone_is_not_found() -> None:
    assert classify(410, "", {}) == FailureReason.NOT_FOUND
    assert classify(410, None, None) == FailureReason.NOT_FOUND


def test_empty_html_at_503_is_server_error() -> None:
    assert classify(503, "", {}) == FailureReason.SERVER_ERROR


def test_login_form_at_401_is_login_required() -> None:
    html = (
        "<html><body>"
        '<form action="/login" method="post">'
        '  <label>Email</label><input name="email" />'
        '  <label>Password</label><input name="password" type="password" />'
        "  <button>Log in</button>"
        "</form>"
        "</body></html>"
    )
    assert classify(401, html, {}) == FailureReason.LOGIN_REQUIRED


def test_grecaptcha_at_200_is_blocked_captcha() -> None:
    html = (
        "<html><body>"
        "<h1>Verify you are human</h1>"
        '<div class="g-recaptcha" data-sitekey="abc"></div>'
        '<script src="https://www.google.com/recaptcha/api.js"></script>'
        "</body></html>"
    )
    assert classify(200, html, {}) == FailureReason.BLOCKED_CAPTCHA


# --- Bonus: sentinel statuses --------------------------------------------------


def test_status_zero_dns_hint_maps_to_dns_fail() -> None:
    assert classify(0, "", {"_failure_hint": "dns_fail"}) == FailureReason.DNS_FAIL


def test_status_zero_timeout_hint_maps_to_timeout() -> None:
    assert classify(0, "", {"_failure_hint": "timeout"}) == FailureReason.TIMEOUT


def test_status_zero_bot_blocked_hint_maps_to_blocked_bot() -> None:
    assert (
        classify(0, "", {"_failure_hint": "bot_blocked"}) == FailureReason.BLOCKED_BOT
    )


@pytest.mark.parametrize("status", [500, 502, 504])
def test_5xx_is_server_error(status: int) -> None:
    assert classify(status, "<html></html>", {}) == FailureReason.SERVER_ERROR


# --- JS-app shell detection ---------------------------------------------------


def test_mercadolivre_http_shell_is_js_app_needs_interaction() -> None:
    """The real ~8KB MercadoLivre http shell (a `<div id="root">` + "requires
    JavaScript" notice, no content) must escalate, not classify OK — even though
    it's well over the old 1KB length gate."""
    html = _load("mercadolivre/search_shell.html")
    assert len(html) > 1024  # would have slipped past the old raw-length gate
    assert classify(200, html, {}) == FailureReason.JS_APP_NEEDS_INTERACTION


def test_large_spa_shell_with_tiny_visible_text_is_js_app() -> None:
    """Body size is irrelevant: a multi-KB shell that's almost all inline script
    but renders no content is still an unrendered SPA shell."""
    shell = (
        "<html><head><title>App</title></head><body>"
        '<div id="root"></div>'
        "<script>" + ("x=1;" * 1000) + "</script>"
        "</body></html>"
    )
    assert len(shell) > 1024
    assert classify(200, shell, {}) == FailureReason.JS_APP_NEEDS_INTERACTION


def test_enable_javascript_notice_alone_is_js_app() -> None:
    html = (
        "<html><body><noscript>You need to enable JavaScript to run this app."
        "</noscript></body></html>"
    )
    assert classify(200, html, {}) == FailureReason.JS_APP_NEEDS_INTERACTION


def test_content_page_with_framework_marker_stays_ok() -> None:
    """A real content page that merely mounts into `<div id="root">` but has
    plenty of visible text is NOT a shell — the visible-text gate protects it."""
    paragraph = "<p>" + ("Real rendered article content. " * 40) + "</p>"
    html = f'<html><body><div id="root">{paragraph}</div></body></html>'
    assert classify(200, html, {}) == FailureReason.OK


def test_clean_article_fixture_stays_ok() -> None:
    # Regression guard: broadening the SPA markers must not reclassify real content.
    assert classify(200, _load("article_clean.html"), {}) == FailureReason.OK
