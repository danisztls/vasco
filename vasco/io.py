from __future__ import annotations

import json
import sys
from typing import TextIO


def is_tty(stream: TextIO = sys.stdout) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def resolve_human(human: bool, machine: bool, stream: TextIO = sys.stdout) -> bool:
    """Decide whether to emit human-readable (rich) output.

    ``--human`` wins; ``--json``/machine forces machine output; otherwise auto by
    TTY (pretty on a terminal, machine when piped).
    """
    if human:
        return True
    if machine:
        return False
    return is_tty(stream)


def write_markdown(envelope: dict, stream: TextIO = sys.stdout) -> None:
    markdown = envelope.get("markdown", "") or ""
    stream.write(markdown)
    if not markdown.endswith("\n"):
        stream.write("\n")


def write_json(envelope: dict, stream: TextIO = sys.stdout) -> None:
    json.dump(envelope, stream, indent=2, ensure_ascii=False, sort_keys=False)
    stream.write("\n")


def write_ndjson(envelope: dict, stream: TextIO = sys.stdout) -> None:
    stream.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
    stream.write("\n")


def estimate_tokens(text: str) -> int:
    """Estimate token count using tiktoken cl100k_base when available, else len // 4."""
    if not text:
        return 0
    try:
        import tiktoken

        try:
            enc = tiktoken.encoding_for_model("gpt-4o")
        except Exception:
            enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return len(text) // 4
