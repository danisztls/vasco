"""Tests for `vasco.logstats.summarize`."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vasco import logstats
from vasco.config import Config, LoggingCfg


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def test_summarize_empty_dir_returns_zeros(tmp_path: Path) -> None:
    cfg = Config(logging=LoggingCfg(enabled=True, path=str(tmp_path / "logs")))
    summary = logstats.summarize(cfg)
    assert summary["total_events"] == 0
    assert summary["by_tool"] == {}
    assert summary["cache_hit_ratio"] == 0.0


def test_summarize_counts_per_tool_and_outcome(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _write_jsonl(
        log_dir / f"{_today()}.jsonl",
        [
            {"tool": "fetch", "outcome": "ok", "mode_used": "http", "from_cache": True},
            {"tool": "fetch", "outcome": "ok", "mode_used": "http", "from_cache": False},
            {"tool": "fetch", "outcome": "ok", "mode_used": "browser", "from_cache": False},
            {"tool": "fetch", "outcome": "fail", "failure_reason": "blocked_bot"},
            {"tool": "search", "outcome": "ok", "result_count": 5},
            {"tool": "extract", "outcome": "empty", "empty_passages": True},
        ],
    )
    cfg = Config(logging=LoggingCfg(enabled=True, path=str(log_dir)))
    summary = logstats.summarize(cfg)

    assert summary["total_events"] == 6
    assert summary["by_tool"]["fetch"] == {"ok": 3, "fail": 1}
    assert summary["by_tool"]["search"] == {"ok": 1}
    assert summary["by_tool"]["extract"] == {"empty": 1}
    assert summary["modes_used"] == {"http": 2, "browser": 1}
    assert summary["cache_hit_ratio"] == pytest.approx(1 / 3, abs=1e-4)
    assert summary["cache_observations"] == 3
    assert summary["failures"] == {"blocked_bot": 1}


def test_summarize_computes_duration_percentiles(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _write_jsonl(
        log_dir / f"{_today()}.jsonl",
        [
            {"tool": "fetch", "outcome": "ok", "duration_ms": d}
            for d in [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
        ],
    )
    cfg = Config(logging=LoggingCfg(enabled=True, path=str(log_dir)))
    summary = logstats.summarize(cfg)
    fetch_d = summary["duration_ms"]["fetch"]
    assert fetch_d["count"] == 10
    # Implementation uses int(round(p * (n-1))); Python 3 uses banker's
    # rounding, so for 10 sorted values: p50 → round(4.5) = 4 → 500.
    assert fetch_d["p50"] == 500
    assert fetch_d["p95"] == 1000
    assert fetch_d["p99"] == 1000


def test_summarize_filters_by_day_window(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _write_jsonl(
        log_dir / f"{_today()}.jsonl",
        [{"tool": "fetch", "outcome": "ok"}],
    )
    _write_jsonl(
        log_dir / f"{_yesterday()}.jsonl",
        [{"tool": "search", "outcome": "ok"}],
    )
    older = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    _write_jsonl(
        log_dir / f"{older}.jsonl",
        [{"tool": "map", "outcome": "ok"}],
    )
    cfg = Config(logging=LoggingCfg(enabled=True, path=str(log_dir)))

    one_day = logstats.summarize(cfg, days=1)
    assert one_day["total_events"] == 1
    assert "fetch" in one_day["by_tool"]
    assert "search" not in one_day["by_tool"]

    two_days = logstats.summarize(cfg, days=2)
    assert two_days["total_events"] == 2
    assert {"fetch", "search"} <= set(two_days["by_tool"])
    # The 10-day-old map record is still excluded.
    assert "map" not in two_days["by_tool"]


def test_summarize_infers_outcome_for_legacy_records(tmp_path: Path) -> None:
    """Records written before the `outcome` field existed should still classify."""
    log_dir = tmp_path / "logs"
    _write_jsonl(
        log_dir / f"{_today()}.jsonl",
        [
            {"tool": "fetch", "failure_reason": "blocked_bot"},  # → fail
            {"tool": "extract", "empty_passages": True},  # → empty
            {"tool": "map", "exception": "OSError: x"},  # → exception
            {"tool": "search"},  # → ok
        ],
    )
    cfg = Config(logging=LoggingCfg(enabled=True, path=str(log_dir)))
    summary = logstats.summarize(cfg)
    assert summary["by_tool"]["fetch"] == {"fail": 1}
    assert summary["by_tool"]["extract"] == {"empty": 1}
    assert summary["by_tool"]["map"] == {"exception": 1}
    assert summary["by_tool"]["search"] == {"ok": 1}


def test_summarize_phase_percentiles(tmp_path: Path) -> None:
    """Per-tool p50/p95/p99 for network_ms, parse_ms, cache_write_ms."""
    log_dir = tmp_path / "logs"
    _write_jsonl(
        log_dir / f"{_today()}.jsonl",
        [
            {
                "tool": "fetch",
                "outcome": "ok",
                "duration_ms": 100 + i,
                "network_ms": 50 + i,
                "parse_ms": 30 + i,
                "cache_write_ms": 5,
            }
            for i in range(10)
        ],
    )
    cfg = Config(logging=LoggingCfg(enabled=True, path=str(log_dir)))
    summary = logstats.summarize(cfg)

    pp = summary["phase_percentiles"]["fetch"]
    assert pp["network_ms"]["count"] == 10
    assert pp["parse_ms"]["count"] == 10
    assert pp["cache_write_ms"]["count"] == 10
    # Nearest-rank with banker's rounding on 10 sorted [50..59]: p50→idx 4 → 54.
    assert pp["network_ms"]["p50"] == 54
    assert pp["network_ms"]["p95"] == 59
    assert pp["parse_ms"]["p50"] == 34
    # cache_write_ms is constant at 5.
    assert pp["cache_write_ms"]["p50"] == 5
    assert pp["cache_write_ms"]["p95"] == 5


def test_summarize_escalation_rate(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _write_jsonl(
        log_dir / f"{_today()}.jsonl",
        [
            {"tool": "fetch", "outcome": "ok", "mode_used": "http"},
            {"tool": "fetch", "outcome": "ok", "mode_used": "http"},
            {
                "tool": "fetch",
                "outcome": "ok",
                "mode_used": "browser",
                "escalated_from": "http",
            },
            {
                "tool": "fetch",
                "outcome": "ok",
                "mode_used": "browser",
                "escalated_from": "http",
            },
            # Failures and cache hits without escalation shouldn't be counted
            # as "no escalation" either — escalation_rate uses successful
            # fetches as the denominator.
            {"tool": "fetch", "outcome": "fail", "failure_reason": "blocked_bot"},
        ],
    )
    cfg = Config(logging=LoggingCfg(enabled=True, path=str(log_dir)))
    summary = logstats.summarize(cfg)
    # 2 escalations out of 4 successful fetches = 0.5.
    assert summary["escalation_rate"]["fetch"] == 0.5


def test_summarize_no_escalation_no_key(tmp_path: Path) -> None:
    """When no fetch event was escalated, the tool isn't in escalation_rate."""
    log_dir = tmp_path / "logs"
    _write_jsonl(
        log_dir / f"{_today()}.jsonl",
        [{"tool": "fetch", "outcome": "ok", "mode_used": "http"}],
    )
    cfg = Config(logging=LoggingCfg(enabled=True, path=str(log_dir)))
    summary = logstats.summarize(cfg)
    assert summary["escalation_rate"] == {}


def test_summarize_phase_percentiles_skips_cache_hits(tmp_path: Path) -> None:
    """Cache hits have no phase fields, so they should not pull the p50 toward 0."""
    log_dir = tmp_path / "logs"
    _write_jsonl(
        log_dir / f"{_today()}.jsonl",
        [
            # Two fresh fetches with real timings…
            {"tool": "fetch", "outcome": "ok", "network_ms": 100, "parse_ms": 50},
            {"tool": "fetch", "outcome": "ok", "network_ms": 200, "parse_ms": 80},
            # …and two cache hits with no phase fields.
            {"tool": "fetch", "outcome": "ok", "from_cache": True, "duration_ms": 0},
            {"tool": "fetch", "outcome": "ok", "from_cache": True, "duration_ms": 0},
        ],
    )
    cfg = Config(logging=LoggingCfg(enabled=True, path=str(log_dir)))
    summary = logstats.summarize(cfg)
    pp = summary["phase_percentiles"]["fetch"]
    # Counts only include records that actually carried the phase field.
    assert pp["network_ms"]["count"] == 2
    assert pp["parse_ms"]["count"] == 2


def test_summarize_tolerates_malformed_lines(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    path = log_dir / f"{_today()}.jsonl"
    path.write_text(
        '\n'
        '{"tool": "fetch", "outcome": "ok"}\n'
        'not json at all\n'
        '{"tool": "search", "outcome": "ok"}\n'
        '\n',
        encoding="utf-8",
    )
    cfg = Config(logging=LoggingCfg(enabled=True, path=str(log_dir)))
    summary = logstats.summarize(cfg)
    assert summary["total_events"] == 2
