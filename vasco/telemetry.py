"""Structured event log for the MCP server.

Appends JSONL records to `$VASCO_LOGGING_PATH` or
`$XDG_DATA_HOME/vasco/logs/YYYY-MM-DD.jsonl`. One file per day, append-only.

This module is best-effort: any failure to write (missing dir, permission
error, disk full) is swallowed silently. Telemetry must never break a tool
call.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _default_log_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "vasco" / "logs"


def _resolve_dir(cfg: Any | None) -> Path:
    if cfg is not None:
        try:
            override = cfg.logging.path
        except Exception:
            override = ""
        if override:
            return Path(override).expanduser()
    return _default_log_dir()


def _today_file(cfg: Any | None) -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _resolve_dir(cfg) / f"{today}.jsonl"


def log_event(cfg: Any | None, event: dict[str, Any]) -> None:
    """Append one JSONL line. Never raises.

    The caller is responsible for the event shape; this module just stamps it
    with a UTC timestamp (if absent) and writes the line.
    """
    if cfg is not None:
        try:
            if not cfg.logging.enabled:
                return
        except Exception:
            pass

    record = dict(event)
    record.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))

    try:
        path = _today_file(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return
