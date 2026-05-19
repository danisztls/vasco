"""Shared pytest fixtures.

`_isolate_user_state` redirects XDG dirs to a per-test tmp_path so that tests
which exercise tools through the MCP server (e.g. the `extract` tool's
empty-passages branch) cannot bleed telemetry into the user's real
`~/.local/share/vasco/logs/` file.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_user_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.delenv("VASCO_LOGGING_PATH", raising=False)
