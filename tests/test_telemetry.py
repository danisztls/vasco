"""Tests for `vasco.telemetry.log_event`.

Verifies: appends JSONL to today's file, honors config disable, honors path
override, never raises on bad input.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vasco import telemetry
from vasco.config import Config, LoggingCfg


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_log_event_writes_jsonl_to_default_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config()  # logging.path = "" → default path under XDG_DATA_HOME

    telemetry.log_event(cfg, {"tool": "fetch", "url": "https://x.test"})

    log_file = tmp_path / "vasco" / "logs" / f"{_today()}.jsonl"
    assert log_file.is_file()
    records = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["tool"] == "fetch"
    assert records[0]["url"] == "https://x.test"
    assert "ts" in records[0]


def test_log_event_appends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config()

    telemetry.log_event(cfg, {"tool": "fetch", "n": 1})
    telemetry.log_event(cfg, {"tool": "fetch", "n": 2})

    log_file = tmp_path / "vasco" / "logs" / f"{_today()}.jsonl"
    records = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert [r["n"] for r in records] == [1, 2]


def test_log_event_honors_path_override(tmp_path: Path) -> None:
    target = tmp_path / "custom" / "place"
    cfg = Config(logging=LoggingCfg(enabled=True, path=str(target)))

    telemetry.log_event(cfg, {"tool": "search"})

    log_file = target / f"{_today()}.jsonl"
    assert log_file.is_file()


def test_log_event_disabled_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config(logging=LoggingCfg(enabled=False))

    telemetry.log_event(cfg, {"tool": "fetch"})

    log_dir = tmp_path / "vasco" / "logs"
    assert not log_dir.exists() or not any(log_dir.iterdir())


def test_log_event_no_cfg_uses_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    telemetry.log_event(None, {"tool": "fetch"})

    log_file = tmp_path / "vasco" / "logs" / f"{_today()}.jsonl"
    assert log_file.is_file()


def test_log_event_swallows_write_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only path should be silently ignored, not raise."""
    target = tmp_path / "ro"
    target.mkdir()
    target.chmod(0o500)  # read+execute only
    cfg = Config(logging=LoggingCfg(enabled=True, path=str(target)))

    try:
        telemetry.log_event(cfg, {"tool": "fetch"})
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"telemetry must never raise: {exc!r}")
    finally:
        target.chmod(0o700)


def test_log_event_stamps_ts_if_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config()

    telemetry.log_event(cfg, {"tool": "fetch"})

    log_file = tmp_path / "vasco" / "logs" / f"{_today()}.jsonl"
    record = json.loads(log_file.read_text().splitlines()[0])
    assert "ts" in record
    # Round-trips as a valid ISO 8601 datetime.
    datetime.fromisoformat(record["ts"])


def test_log_event_preserves_explicit_ts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config()

    telemetry.log_event(cfg, {"tool": "fetch", "ts": "2020-01-01T00:00:00+00:00"})

    log_file = tmp_path / "vasco" / "logs" / f"{_today()}.jsonl"
    record = json.loads(log_file.read_text().splitlines()[0])
    assert record["ts"] == "2020-01-01T00:00:00+00:00"


def test_fetch_success_fields_surfaces_adapter_quality() -> None:
    """Content-adapter envelopes carry provider/page_type/result_count so a
    zero-result success (the rot fingerprint) is visible in the log."""
    env = {
        "url_requested": "https://www.olx.com.br/imoveis/estado-sp",
        "mode_used": "olx",
        "quality": {"provider": "olx", "page_type": "list", "result_count": 0},
    }
    fields = telemetry.fetch_success_fields(env)
    assert fields["provider"] == "olx"
    assert fields["page_type"] == "list"
    assert fields["result_count"] == 0


def test_fetch_success_fields_omits_quality_for_plain_fetch() -> None:
    """A normal fetch has no provider, so the adapter keys stay absent."""
    env = {"url_requested": "https://example.com", "mode_used": "http", "quality": {}}
    fields = telemetry.fetch_success_fields(env)
    assert "provider" not in fields
    assert "result_count" not in fields
