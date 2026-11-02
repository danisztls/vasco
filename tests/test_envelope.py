# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import pytest

from vasco.envelope import failure_envelope
from vasco.errors import FailureReason


@pytest.mark.parametrize(
    "reason", [FailureReason.BLOCKED_CAPTCHA, FailureReason.BLOCKED_CLOUDFLARE]
)
def test_cached_block_says_how_to_retry(reason: FailureReason) -> None:
    env = failure_envelope(base={}, reason=reason, message="blocked")
    assert "refresh=true" in env["failure"]["hint"]
    assert env["failure"]["message"] == "blocked"


def test_other_failures_have_no_hint() -> None:
    env = failure_envelope(base={}, reason=FailureReason.NOT_FOUND, message="gone")
    assert "hint" not in env["failure"]
