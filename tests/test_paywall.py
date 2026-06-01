"""Tests for vasco.quality.paywall — paywall *detection* (not bypass)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vasco.config import QualityCfg
from vasco.quality import paywall, score

# A page that loads the Piano/tinypass paywall script.
_PAYWALLED_HTML = """
<html><head>
  <script src="https://cdn.tinypass.com/api/tinypass.min.js"></script>
</head><body><p>Subscribe to read more.</p></body></html>
"""

# An AMP page gated via the amp-access extension.
_AMP_PAYWALLED_HTML = """
<html amp><head>
  <script async custom-element="amp-access"
    src="https://cdn.ampproject.org/v0/amp-access-0.1.js"></script>
</head><body>teaser</body></html>
"""

_CLEAN_HTML = "<html><head></head><body><p>Free content for all.</p></body></html>"


@pytest.fixture(autouse=True)
def _reset_singleton():
    paywall.reset()
    yield
    paywall.reset()


class TestDetectPaywall:
    def test_vendor_script_detected(self):
        vendors = paywall.load_paywall_vendors(())
        assert paywall.detect_paywall(_PAYWALLED_HTML, vendors) == "tinypass.com"

    def test_amp_access_marker_detected(self):
        vendors = paywall.load_paywall_vendors(())
        assert paywall.detect_paywall(_AMP_PAYWALLED_HTML, vendors) == "amp-access"

    def test_clean_page_not_flagged(self):
        vendors = paywall.load_paywall_vendors(())
        assert paywall.detect_paywall(_CLEAN_HTML, vendors) is None

    def test_empty_and_none_are_safe(self):
        vendors = paywall.load_paywall_vendors(())
        assert paywall.detect_paywall("", vendors) is None
        assert paywall.detect_paywall(None, vendors) is None
        assert paywall.detect_paywall(_PAYWALLED_HTML, frozenset()) is None

    def test_deterministic_when_multiple_match(self):
        # Both "amp-access" and "tinypass.com" present → sorted order picks the
        # alphabetically-first ("amp-access"), and it's stable across calls.
        both = _PAYWALLED_HTML + _AMP_PAYWALLED_HTML
        vendors = paywall.load_paywall_vendors(())
        first = paywall.detect_paywall(both, vendors)
        assert first == "amp-access"
        assert paywall.detect_paywall(both, vendors) == first


class TestLoadPaywallVendors:
    def test_bundled_default_has_known_vendors_and_markers(self):
        vendors = paywall.load_paywall_vendors(())
        assert {"piano.io", "tinypass.com", "poool.fr"} <= vendors
        # non-domain script markers are always merged in
        assert {"amp-access", "amp-subscriptions"} <= vendors

    def test_configured_paths_win_but_markers_persist(self, tmp_path: Path):
        f = tmp_path / "vendors.txt"
        f.write_text("# custom\nmyvendor.example\n")
        vendors = paywall.load_paywall_vendors((str(f),))
        assert "myvendor.example" in vendors
        assert "amp-access" in vendors  # markers added regardless of source
        assert "piano.io" not in vendors  # bundled default replaced


class TestGetPaywallVendors:
    def test_caches_then_reset(self, tmp_path: Path):
        paywall.reset()
        first = paywall.get_paywall_vendors(QualityCfg())
        # Different cfg returns the cached value until reset().
        f = tmp_path / "vendors.txt"
        f.write_text("other.example\n")
        assert (
            paywall.get_paywall_vendors(QualityCfg(paywall_vendor_paths=(str(f),)))
            is first
        )
        paywall.reset()
        reloaded = paywall.get_paywall_vendors(
            QualityCfg(paywall_vendor_paths=(str(f),))
        )
        assert "other.example" in reloaded


class TestScoreIntegration:
    def test_score_flags_paywalled_page(self):
        result = score("teaser markdown", cfg=QualityCfg(), raw_html=_PAYWALLED_HTML)
        assert result["paywalled"] is True
        assert result["paywall_vendor"] == "tinypass.com"

    def test_score_clean_page_not_paywalled(self):
        result = score("free markdown", cfg=QualityCfg(), raw_html=_CLEAN_HTML)
        assert result["paywalled"] is False
        assert result["paywall_vendor"] is None

    def test_score_without_raw_html_is_not_paywalled(self):
        result = score("markdown", cfg=QualityCfg(), raw_html=None)
        assert result["paywalled"] is False
        assert result["paywall_vendor"] is None

    def test_detection_can_be_disabled(self):
        cfg = QualityCfg(detect_paywall=False)
        result = score("teaser", cfg=cfg, raw_html=_PAYWALLED_HTML)
        assert result["paywalled"] is False
        assert result["paywall_vendor"] is None
