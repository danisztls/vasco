"""Tests for `vasco.bot_detect.classify`.

Drives each fixture HTML file through the classifier with the expected
status code and asserts the resulting FailureReason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vasco.bot_detect import classify
from vasco.errors import FailureReason


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_cloudflare_challenge_at_200_is_blocked_cloudflare() -> None:
    html = _load("cloudflare_challenge.html")
    assert classify(200, html, {}) == FailureReason.BLOCKED_CLOUDFLARE


def test_paywall_soft_at_200_is_soft_with_partial() -> None:
    html = _load("paywall_soft.html")
    assert (
        classify(200, html, {}) == FailureReason.PAYWALL_SOFT_WITH_PARTIAL
    )


def test_paywall_hard_at_200_is_hard() -> None:
    html = _load("paywall_hard.html")
    assert classify(200, html, {}) == FailureReason.PAYWALL_HARD


def test_clean_article_at_200_is_ok() -> None:
    html = _load("article_clean.html")
    assert classify(200, html, {}) == FailureReason.OK


def test_empty_html_at_404_is_not_found() -> None:
    assert classify(404, "", {}) == FailureReason.NOT_FOUND
    assert classify(404, None, None) == FailureReason.NOT_FOUND


def test_empty_html_at_503_is_server_error() -> None:
    assert classify(503, "", {}) == FailureReason.SERVER_ERROR


def test_login_form_at_401_is_login_required() -> None:
    html = (
        "<html><body>"
        '<form action="/login" method="post">'
        "  <label>Email</label><input name=\"email\" />"
        "  <label>Password</label><input name=\"password\" type=\"password\" />"
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
    assert (
        classify(0, "", {"_failure_hint": "dns_fail"})
        == FailureReason.DNS_FAIL
    )


def test_status_zero_timeout_hint_maps_to_timeout() -> None:
    assert (
        classify(0, "", {"_failure_hint": "timeout"})
        == FailureReason.TIMEOUT
    )


@pytest.mark.parametrize("status", [500, 502, 504])
def test_5xx_is_server_error(status: int) -> None:
    assert classify(status, "<html></html>", {}) == FailureReason.SERVER_ERROR
