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


# ---------------------------------------------------------------------------
# Event-shape helpers
#
# Shared by `vasco.mcp` and `vasco.cli` so the JSONL schema stays identical
# regardless of entry point. The summarizer (`vasco logs stats`) relies on a
# consistent `outcome` discriminator: "ok" | "fail" | "exception" | "empty".
# ---------------------------------------------------------------------------


def record_success(cfg: Any | None, tool: str, **fields: Any) -> None:
    payload: dict[str, Any] = {"tool": tool, "outcome": "ok"}
    payload.update({k: v for k, v in fields.items() if v is not None})
    log_event(cfg, payload)


def record_failure(cfg: Any | None, tool: str, envelope: dict[str, Any]) -> None:
    failure = envelope.get("failure") if isinstance(envelope, dict) else None
    if not failure:
        return
    payload: dict[str, Any] = {
        "tool": tool,
        "outcome": "fail",
        "url": envelope.get("url_requested"),
        "failure_reason": failure.get("reason"),
        "message": failure.get("message"),
        "mode_used": envelope.get("mode_used"),
        "http_status": envelope.get("http_status"),
    }
    for key in (
        "duration_ms",
        "network_ms",
        "parse_ms",
        "attempts",
        "escalated_from",
    ):
        val = envelope.get(key)
        if val is not None:
            payload[key] = val
    log_event(cfg, payload)


def record_exception(
    cfg: Any | None, tool: str, exc: BaseException, **fields: Any
) -> None:
    log_event(
        cfg,
        {
            "tool": tool,
            "outcome": "exception",
            "exception": f"{type(exc).__name__}: {exc}",
            **fields,
        },
    )


def fetch_success_fields(env: dict[str, Any]) -> dict[str, Any]:
    """Standard success-event payload for fetch-shaped envelopes.

    Includes phase timings (`network_ms`, `parse_ms`, `cache_write_ms`,
    `attempts`, `escalated_from`) when present on the envelope, so a slow
    fetch can be triaged from the log alone.

    For content-adapter envelopes (olx, mercadolivre, realestate,
    google_shopping) it also surfaces `provider`, `page_type`, and
    `result_count` from the `quality` block. `result_count == 0` on a success
    is the silent-scraper-rot fingerprint — logging it lets `vasco logs stats`
    flag an adapter that is quietly returning nothing. Non-adapter fetches have
    no `provider`, so these keys stay absent there.
    """
    fields: dict[str, Any] = {
        "url": env.get("url_requested"),
        "mode_used": env.get("mode_used"),
        "from_cache": bool(env.get("from_cache")),
        "http_status": env.get("http_status"),
        "word_count": env.get("word_count"),
        "cache_age_seconds": env.get("cache_age_seconds"),
        "duration_ms": env.get("duration_ms"),
        "network_ms": env.get("network_ms"),
        "parse_ms": env.get("parse_ms"),
        "cache_write_ms": env.get("cache_write_ms"),
        "attempts": env.get("attempts"),
        "escalated_from": env.get("escalated_from"),
    }
    quality = env.get("quality")
    if isinstance(quality, dict) and quality.get("provider"):
        fields["provider"] = quality.get("provider")
        fields["page_type"] = quality.get("page_type")
        fields["result_count"] = quality.get("result_count")
    return fields
