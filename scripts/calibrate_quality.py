#!/usr/bin/env python3
"""Calibrate quality heuristic weights by fetching known-good and known-bad URLs.

Fetches sample pages through Vasco, collects all quality signals, and
dumps a CSV + summary statistics showing signal distributions per group.

Usage:
    uv run scripts/calibrate_quality.py
    uv run scripts/calibrate_quality.py --out results.csv
    uv run scripts/calibrate_quality.py --jobs 8

Reads the consolidated blocklist from $XDG_CACHE_HOME/vasco/blocklist.txt
to pick bad-group URLs. Good-group URLs are hardcoded below.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vasco.config import load_config
from vasco.fetch import fetch_one


# ── Known-good URLs (diverse: docs, journalism, essays, specs, academic) ──

GOOD_URLS = [
    # Technical documentation
    "https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Promise",
    "https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API/Using_Fetch",
    "https://docs.python.org/3/library/asyncio-task.html",
    "https://docs.python.org/3/library/pathlib.html",
    "https://docs.python.org/3/library/dataclasses.html",
    "https://go.dev/doc/effective_go",
    "https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html",
    "https://doc.rust-lang.org/book/ch10-02-traits.html",
    "https://htmx.org/docs/",
    "https://sqlite.org/lang_select.html",
    "https://redis.io/docs/latest/develop/data-types/",
    "https://nginx.org/en/docs/http/ngx_http_proxy_module.html",
    # Specs and standards
    "https://www.w3.org/TR/WCAG22/",
    "https://datatracker.ietf.org/doc/html/rfc7231",
    "https://html.spec.whatwg.org/multipage/scripting.html",
    # Long-form essays
    "https://paulgraham.com/greatwork.html",
    "https://paulgraham.com/taste.html",
    "https://www.joelonsoftware.com/2000/08/09/the-joel-test-12-steps-to-better-code/",
    "https://www.kalzumeus.com/2012/01/23/salary-negotiation/",
    "https://www.gwern.net/Scaling-hypothesis",
    # Journalism
    "https://www.reuters.com/technology/artificial-intelligence/",
    "https://www.nytimes.com/section/technology",
    "https://arstechnica.com/science/",
    "https://www.theverge.com/tech",
    # Academic / research
    "https://arxiv.org/abs/1706.03762",
    "https://arxiv.org/abs/2005.14165",
    "https://distill.pub/2021/gnn-intro/",
    "https://colah.github.io/posts/2015-08-Understanding-LSTMs/",
    # Reference
    "https://en.cppreference.com/w/cpp/container/vector",
    "https://www.postgresql.org/docs/current/sql-select.html",
]

# ── Bad URLs: sample from blocklist + known AI slop farms ──

KNOWN_BAD_URLS = [
    "https://www.analyticsinsight.net/artificial-intelligence/top-10-ai-tools",
    "https://www.makeuseof.com/what-is-chatgpt/",
]


def _blocklist_path() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(xdg) / "vasco" / "blocklist.txt"


def _sample_bad_urls(n: int = 30) -> list[str]:
    """Pick random domains from the consolidated blocklist."""
    bl_path = _blocklist_path()
    if not bl_path.is_file():
        print(f"warning: no blocklist at {bl_path}, using only hardcoded bad URLs")
        return KNOWN_BAD_URLS

    domains = [
        line.strip() for line in bl_path.read_text().splitlines() if line.strip()
    ]
    random.shuffle(domains)
    urls = []
    for domain in domains[:n]:
        urls.append(f"https://{domain}/")
    return KNOWN_BAD_URLS + urls


def _extract_row(envelope: dict, group: str) -> dict:
    """Pull a flat row of signals from a fetch envelope."""
    quality = envelope.get("quality", {})
    signals = quality.get("signals", {})
    return {
        "group": group,
        "url": envelope.get("url_final", envelope.get("url_requested", "")),
        "http_status": envelope.get("http_status", 0),
        "failed": "failure" in envelope,
        "mode_used": envelope.get("mode_used", ""),
        "word_count": envelope.get("word_count", 0),
        "boilerplate_ratio": quality.get("boilerplate_ratio", ""),
        "trafilatura_confidence": quality.get("trafilatura_confidence", ""),
        "domain_flagged": quality.get("domain_flagged", ""),
        "slop_score": quality.get("slop_score", ""),
        "slop_vocab_ratio": signals.get("slop_vocab_ratio", ""),
        "slop_phrase_count": signals.get("slop_phrase_count", ""),
        "sentence_length_cv": signals.get("sentence_length_cv", ""),
        "em_dash_density": signals.get("em_dash_density", ""),
        "transition_start_ratio": signals.get("transition_start_ratio", ""),
        "type_token_ratio": signals.get("type_token_ratio", ""),
        "has_byline": bool(envelope.get("byline")),
        "has_date": bool(envelope.get("published")),
        "title": (envelope.get("title") or "")[:80],
    }


FIELDS = [
    "group",
    "url",
    "http_status",
    "failed",
    "mode_used",
    "word_count",
    "boilerplate_ratio",
    "trafilatura_confidence",
    "domain_flagged",
    "slop_score",
    "slop_vocab_ratio",
    "slop_phrase_count",
    "sentence_length_cv",
    "em_dash_density",
    "transition_start_ratio",
    "type_token_ratio",
    "has_byline",
    "has_date",
    "title",
]

NUMERIC_FIELDS = [
    "word_count",
    "boilerplate_ratio",
    "trafilatura_confidence",
    "slop_score",
    "slop_vocab_ratio",
    "slop_phrase_count",
    "sentence_length_cv",
    "em_dash_density",
    "transition_start_ratio",
    "type_token_ratio",
]


async def _fetch_batch(urls: list[str], group: str, cfg, jobs: int) -> list[dict]:
    """Fetch URLs concurrently and return rows."""
    sem = asyncio.Semaphore(jobs)
    rows = []

    async def _fetch(url: str) -> dict | None:
        async with sem:
            try:
                envelope = await fetch_one(url, cfg=cfg, refresh=True)
            except Exception as exc:
                print(f"  error {url}: {exc}")
                return None
            failed = "failure" in envelope
            status = "FAIL" if failed else "ok"
            wc = envelope.get("word_count", 0)
            slop = envelope.get("quality", {}).get("slop_score", "n/a")
            print(f"  [{status}] {url[:70]}  wc={wc} slop={slop}")
            return _extract_row(envelope, group)

    tasks = [_fetch(url) for url in urls]
    results = await asyncio.gather(*tasks)
    for row in results:
        if row is not None:
            rows.append(row)
    return rows


def _print_summary(rows: list[dict]) -> None:
    """Print per-group percentiles for numeric signals."""
    from collections import defaultdict

    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if not row["failed"]:
            groups[row["group"]].append(row)

    print("\n" + "=" * 80)
    print("SIGNAL DISTRIBUTIONS (successful fetches only)")
    print("=" * 80)

    for field in NUMERIC_FIELDS:
        print(f"\n── {field} ──")
        for group_name in ("good", "bad"):
            vals = []
            for row in groups.get(group_name, []):
                v = row.get(field)
                if v != "" and v is not None:
                    try:
                        vals.append(float(v))
                    except (ValueError, TypeError):
                        pass
            if not vals:
                print(f"  {group_name:5s}: no data")
                continue
            vals.sort()
            n = len(vals)
            p25 = vals[n // 4] if n >= 4 else vals[0]
            p50 = vals[n // 2]
            p75 = vals[3 * n // 4] if n >= 4 else vals[-1]
            mean = sum(vals) / n
            print(
                f"  {group_name:5s}: n={n:3d}  "
                f"mean={mean:.4f}  "
                f"p25={p25:.4f}  p50={p50:.4f}  p75={p75:.4f}  "
                f"min={vals[0]:.4f}  max={vals[-1]:.4f}"
            )

    # Boolean signals
    for field in ("has_byline", "has_date"):
        print(f"\n── {field} ──")
        for group_name in ("good", "bad"):
            group_rows = [r for r in groups.get(group_name, []) if not r["failed"]]
            if not group_rows:
                print(f"  {group_name:5s}: no data")
                continue
            true_count = sum(1 for r in group_rows if r[field])
            pct = true_count / len(group_rows) * 100
            print(f"  {group_name:5s}: {true_count}/{len(group_rows)} ({pct:.0f}%)")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate quality heuristics")
    parser.add_argument("--out", default="calibration.csv", help="Output CSV path")
    parser.add_argument("--jobs", type=int, default=4, help="Concurrent fetches")
    parser.add_argument(
        "--bad-sample",
        type=int,
        default=30,
        help="Number of blocklist domains to sample",
    )
    args = parser.parse_args()

    cfg = load_config()

    bad_urls = _sample_bad_urls(args.bad_sample)
    print(f"Good URLs: {len(GOOD_URLS)}")
    print(f"Bad URLs: {len(bad_urls)}")

    print("\nFetching good URLs...")
    good_rows = await _fetch_batch(GOOD_URLS, "good", cfg, args.jobs)

    print("\nFetching bad URLs...")
    bad_rows = await _fetch_batch(bad_urls, "bad", cfg, args.jobs)

    all_rows = good_rows + bad_rows

    # Write CSV
    out_path = Path(args.out)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"\nWrote {len(all_rows)} rows to {out_path}")

    _print_summary(all_rows)


if __name__ == "__main__":
    asyncio.run(main())
