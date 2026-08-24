"""Tests for `vasco.bot_detect.classify`.

Drives each fixture HTML file through the classifier with the expected
status code and asserts the resulting FailureReason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vasco.errors import FailureReason
from vasco.fetch.bot_detect import classify

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


def test_full_page_embedding_turnstile_widget_is_not_a_challenge() -> None:
    """A content-rich 200 that merely *embeds* a Turnstile/captcha widget (e.g. a
    login form) rendered fine and must NOT be flagged BLOCKED_CAPTCHA. Regression
    for jornalfolha1.com.br, whose real homepage bundles a Turnstile login widget;
    without a content-size guard it was wrongly blocked forever."""
    article = "<p>" + ("Noticia real de Baixo Guandu. " * 200) + "</p>"
    html = (
        "<html><body><nav>Home Politica Esporte</nav>"
        + article
        + '<form><div class="cf-turnstile" data-sitekey="x"></div>'
        '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js">'
        "</script></form></body></html>"
    )
    assert classify(200, html, {}) == FailureReason.OK


def test_aliexpress_punish_page_is_blocked_captcha() -> None:
    """AliExpress (Alibaba 'baxia'/'x5sec') serves a `_____tmd_____/punish` nc
    slider as a 200. It must classify as BLOCKED_CAPTCHA so the chain stops
    caching the junk shell as success and the manual-VNC solve flow can fire."""
    html = (FIXTURES / "aliexpress_punish.html").read_text()
    assert classify(200, html, {}) == FailureReason.BLOCKED_CAPTCHA


def test_aliexpress_punish_markers_fire_regardless_of_size() -> None:
    """The rendered slider is large (~230 KB) and would slip past the thin-text
    guard the generic captcha branch uses; the Alibaba punish markers are
    size-independent on purpose."""
    big = "<div>" + ("filler text " * 5000) + "</div>"
    stub = '<script>var u="//x/_____tmd_____/punish?x5secdata=tok";</script>'
    assert classify(200, stub + big, {}) == FailureReason.BLOCKED_CAPTCHA


def test_aliexpress_punish_at_403_is_blocked_captcha() -> None:
    """Markers mean 'challenge' under any status, not just 200."""
    html = '<html><body><div class="baxia-punish slidetounlock"></div></body></html>'
    assert classify(403, html, {}) == FailureReason.BLOCKED_CAPTCHA


def test_amazon_robot_check_is_blocked_captcha() -> None:
    """Amazon's homegrown robot check (not h-captcha/recaptcha/turnstile) must
    classify as BLOCKED_CAPTCHA so the chain escalates http → browser and the
    Amazon adapter surfaces an honest block, not a misleading PARSE_FAILED."""
    html = (FIXTURES / "amazon_robot.html").read_text()
    assert classify(200, html, {}) == FailureReason.BLOCKED_CAPTCHA
    # Amazon may serve it as a 503 too — the markers fire regardless of status.
    assert classify(503, html, {}) == FailureReason.BLOCKED_CAPTCHA


def test_amazon_robot_markers_fire_regardless_of_size() -> None:
    """The validateCaptcha/support-email/opfcaptcha markers are specific to the
    interstitial, so they fire size-independently (like Alibaba's punish page)."""
    big = "<div>" + ("filler text " * 5000) + "</div>"
    stub = '<form action="/errors/validateCaptcha"></form>'
    assert classify(200, stub + big, {}) == FailureReason.BLOCKED_CAPTCHA


def test_mercadolivre_account_wall_at_200_is_login_required() -> None:
    """MercadoLivre serves its `/gz/account-verification` interstitial as a 200
    once it flags the session. It must classify as LOGIN_REQUIRED so the adapter
    surfaces an honest reason (not the misleading PARSE_FAILED its JSON-LD parser
    would emit) and the browser server's cookie-clear recovery can fire."""
    html = _load("mercadolivre/account_verification.html")
    assert classify(200, html, {}) == FailureReason.LOGIN_REQUIRED


def test_mercadolivre_real_pages_are_not_account_wall() -> None:
    """Marker-specificity guard: real ML search/product pages — thin on *visible*
    text because the payload lives in JSON-LD scripts — must NOT be mistaken for
    the account wall. They lack the interstitial's markers."""
    for name in ("mercadolivre/search.html", "mercadolivre/product.html"):
        assert classify(200, _load(name), {}) != FailureReason.LOGIN_REQUIRED


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


def test_markerless_mount_point_shell_is_ok_defers_to_word_count() -> None:
    """A bare `<div id="root">` shell with no "requires JavaScript" notice now
    classifies OK — bot_detect only sees raw HTML and a mount-point marker is not
    a reliable shell signal (real SSG pages mount into `id="root"`/`id="app"`).
    Such empty shells are escalated downstream by the post-conversion
    `word_count == 0` check in the fetch chain, not by `classify`."""
    shell = (
        "<html><head><title>App</title></head><body>"
        '<div id="root"></div>'
        "<script>" + ("x=1;" * 1000) + "</script>"
        "</body></html>"
    )
    assert len(shell) > 1024
    assert classify(200, shell, {}) == FailureReason.OK


def test_facebook_style_markerless_shell_is_ok() -> None:
    """A large obfuscated shell (Facebook-shaped: lots of inline script, almost no
    visible text, none of our markers) is OK at the classify layer — it has no
    "requires JavaScript" notice. The word_count==0 escalation catches it later."""
    shell = (
        "<html><head><title>Big App</title></head><body>"
        "<span>Loading</span>"
        "<script>" + ("var a=" + "0," * 60000 + "1;") + "</script>"
        "</body></html>"
    )
    assert len(shell) > 100_000  # heavy like the real thing
    assert classify(200, shell, {}) == FailureReason.OK


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
