"""Roll up JSONL telemetry events into a compact stats dict.

Reads from the directory `telemetry` writes to (default
`$XDG_DATA_HOME/vasco/logs/`, or `cfg.logging.path`). Pure read.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


_FETCH_TOOLS = {"fetch", "fetch_many"}

# Phase fields the summarizer turns into per-tool percentiles. `duration_ms`
# is kept at the top level (back-compat with v1 of the rollup); the others
# go under `phase_percentiles`. Only present on envelopes that came from a
# live fetch — cache hits and short-circuit paths emit only `duration_ms`.
_PHASE_FIELDS = ("network_ms", "parse_ms", "cache_write_ms")


def _log_dir(cfg: Any | None) -> Path:
    if cfg is not None:
        try:
            override = cfg.logging.path
        except Exception:
            override = ""
        if override:
            return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "vasco" / "logs"


def _files_for_window(directory: Path, days: int) -> list[Path]:
    if not directory.is_dir():
        return []
    today = datetime.now(timezone.utc).date()
    wanted = {(today - timedelta(days=offset)).isoformat() for offset in range(days)}
    return sorted(p for p in directory.glob("*.jsonl") if p.stem in wanted)


def _iter_records(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue


def _percentiles(values: list[int], ps: tuple[float, ...]) -> dict[str, int]:
    if not values:
        return {f"p{int(p * 100)}": 0 for p in ps}
    ordered = sorted(values)
    n = len(ordered)
    out: dict[str, int] = {}
    for p in ps:
        # Nearest-rank percentile — sufficient at our scale; avoids numpy.
        idx = max(0, min(n - 1, int(round(p * (n - 1)))))
        out[f"p{int(p * 100)}"] = int(ordered[idx])
    return out


def _infer_outcome(record: dict[str, Any]) -> str:
    """Derive outcome for records written before the `outcome` field existed."""
    explicit = record.get("outcome")
    if isinstance(explicit, str):
        return explicit
    if record.get("exception"):
        return "exception"
    if record.get("failure_reason"):
        return "fail"
    if record.get("empty_passages"):
        return "empty"
    return "ok"


def summarize(cfg: Any | None, *, days: int = 1) -> dict[str, Any]:
    """Read JSONL logs covering the last `days` days and return an aggregate dict."""
    days = max(1, int(days))
    directory = _log_dir(cfg)
    files = _files_for_window(directory, days)

    by_tool: dict[str, Counter[str]] = {}
    modes: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    cache_hits = 0
    cache_total = 0
    durations: dict[str, list[int]] = {}
    phase_values: dict[str, dict[str, list[int]]] = {}
    escalations: dict[str, int] = {}
    fetch_ok_total: dict[str, int] = {}
    total = 0

    for rec in _iter_records(files):
        tool = rec.get("tool")
        if not isinstance(tool, str):
            continue
        total += 1
        outcome = _infer_outcome(rec)
        by_tool.setdefault(tool, Counter())[outcome] += 1

        if outcome == "fail":
            reason = rec.get("failure_reason")
            if isinstance(reason, str):
                failures[reason] += 1

        if tool in _FETCH_TOOLS:
            mode = rec.get("mode_used")
            if isinstance(mode, str) and outcome == "ok":
                modes[mode] += 1
            if outcome == "ok":
                cache_total += 1
                if rec.get("from_cache"):
                    cache_hits += 1
                fetch_ok_total[tool] = fetch_ok_total.get(tool, 0) + 1
                if rec.get("escalated_from") is not None:
                    escalations[tool] = escalations.get(tool, 0) + 1

        ms = rec.get("duration_ms")
        if isinstance(ms, (int, float)) and outcome in ("ok", "empty"):
            durations.setdefault(tool, []).append(int(ms))

        if outcome == "ok":
            for field_name in _PHASE_FIELDS:
                val = rec.get(field_name)
                if isinstance(val, (int, float)):
                    phase_values.setdefault(tool, {}).setdefault(
                        field_name, []
                    ).append(int(val))

    duration_stats: dict[str, dict[str, int]] = {}
    for tool, vals in durations.items():
        pct = _percentiles(vals, (0.5, 0.95, 0.99))
        pct["count"] = len(vals)
        duration_stats[tool] = pct

    phase_percentiles: dict[str, dict[str, dict[str, int]]] = {}
    for tool, fields_map in phase_values.items():
        per_phase: dict[str, dict[str, int]] = {}
        for field_name, vals in fields_map.items():
            pct = _percentiles(vals, (0.5, 0.95, 0.99))
            pct["count"] = len(vals)
            per_phase[field_name] = pct
        if per_phase:
            phase_percentiles[tool] = per_phase

    escalation_rate: dict[str, float] = {}
    for tool, escalated_count in escalations.items():
        denom = fetch_ok_total.get(tool, 0)
        if denom:
            escalation_rate[tool] = round(escalated_count / denom, 4)

    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=days - 1)).isoformat()

    return {
        "days": days,
        "since": since,
        "log_dir": str(directory),
        "files_read": [str(p) for p in files],
        "total_events": total,
        "by_tool": {t: dict(c) for t, c in sorted(by_tool.items())},
        "modes_used": dict(modes.most_common()),
        "cache_hit_ratio": round(cache_hits / cache_total, 4) if cache_total else 0.0,
        "cache_observations": cache_total,
        "escalation_rate": escalation_rate,
        "failures": dict(failures.most_common()),
        "duration_ms": duration_stats,
        "phase_percentiles": phase_percentiles,
    }
