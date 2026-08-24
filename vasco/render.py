# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Rich human-readable rendering for the Vasco CLI.

This module is the single home for the *pretty* (terminal) output path. It is
imported lazily from ``vasco.interface.cli`` — only inside the human branch of a
command — so ``vasco --help`` and every machine (JSON/NDJSON) path never pay for
importing ``rich``. Renderers are presentation-only: they take an already-built
result/envelope and must never raise on failure/empty/partial data.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterable, Iterator
from typing import Any, TextIO

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# console + small formatting helpers
# ---------------------------------------------------------------------------


def make_console(stream: TextIO | None = None) -> Console:
    """Build a Console for the human path.

    ``force_terminal=True`` because we only ever build this in human mode — that
    keeps ANSI styling when ``--human`` is piped (e.g. into ``less -R``).
    """
    return Console(file=stream or sys.stdout, force_terminal=True, soft_wrap=False)


def _first(d: dict[str, Any], *keys: str) -> Any:
    """First key in ``d`` whose value is meaningful (not None/""/[])."""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return None


def _attr(listing: dict[str, Any], *keys: str) -> Any:
    """Pick a field from a listing, falling back to its ``attributes`` bag."""
    attrs = listing.get("attributes") or {}
    for k in keys:
        v = listing.get(k)
        if v not in (None, "", []):
            return v
        v = attrs.get(k)
        if v not in (None, "", []):
            return v
    return None


# Currency code → display symbol for the compact price column. Absent / unknown
# codes fall back to the bare code (or "R$" when no currency is known, preserving
# the original Brazilian-adapter output).
_CURRENCY_SYMBOLS = {"BRL": "R$", "USD": "$", "EUR": "€", "GBP": "£"}


def _fmt_price(value: Any, currency: str | None = None) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return str(value)
    symbol = _CURRENCY_SYMBOLS.get(currency or "BRL", currency or "R$")
    if isinstance(value, (int, float)):
        return f"{symbol} {value:,.0f}".replace(",", ".")
    return str(value)


def _fmt_age(epoch: Any) -> str:
    try:
        delta = max(0, int(time.time()) - int(epoch))
    except (TypeError, ValueError):
        return ""
    for unit, secs in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= secs:
            return f"{delta // secs}{unit} ago"
    return f"{delta}s ago"


def _link(text: str, url: str | None) -> str:
    """Render ``text`` as a terminal hyperlink to ``url`` when present."""
    if not url:
        return text
    return f"[link={url}]{text}[/link]"


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def render_fetch(env: dict[str, Any], console: Console | None = None) -> None:
    con = console or make_console()

    if env.get("failure"):
        _render_failure(env["failure"], con, url=env.get("url_requested"))
        return

    quality = env.get("quality") or {}
    if isinstance(quality, dict) and quality.get("listings"):
        _render_quality_header(quality, con)
        render_listings(quality, con)
        return
    if isinstance(quality, dict) and quality.get("products"):
        _render_quality_header(quality, con)
        render_products(quality, con)
        return

    _render_page_header(env, con)
    markdown = env.get("markdown") or ""
    if markdown.strip():
        con.print(Markdown(markdown))
    else:
        con.print("[dim](no extracted content)[/dim]")


def _render_page_header(env: dict[str, Any], con: Console) -> None:
    title = env.get("title") or env.get("url_final") or env.get("url_requested") or ""
    url = env.get("url_final") or env.get("url_requested")
    con.print(Text(str(title), style="bold"))
    if url:
        con.print(_link(str(url), str(url)), style="cyan")

    meta: list[str] = []
    if env.get("mode_used"):
        meta.append(str(env["mode_used"]))
    if env.get("word_count"):
        meta.append(f"{env['word_count']} words")
    quality = env.get("quality") or {}
    if isinstance(quality, dict) and isinstance(
        quality.get("slop_score"), (int, float)
    ):
        meta.append(f"slop {quality['slop_score']:.2f}")
    if env.get("from_cache"):
        meta.append("cached")
    if meta:
        con.print("  ·  ".join(meta), style="dim")
    for warn in env.get("warnings") or []:
        con.print(f"⚠ {warn}", style="yellow")
    con.print()


def _render_quality_header(quality: dict[str, Any], con: Console) -> None:
    bits: list[str] = [
        str(quality[key])
        for key in ("provider", "vertical", "page_type")
        if quality.get(key)
    ]
    if quality.get("result_count") is not None:
        bits.append(f"{quality['result_count']} results")
    if bits:
        con.print("  ·  ".join(bits), style="bold")
    filtered = quality.get("filtered")
    if isinstance(filtered, dict) and filtered:
        drops = ", ".join(f"{k}={v}" for k, v in filtered.items())
        con.print(f"filtered: {drops}", style="dim")
    for warn in quality.get("warnings") or []:
        con.print(f"⚠ {warn}", style="yellow")
    con.print()


def _render_failure(
    failure: dict[str, Any], con: Console, url: str | None = None
) -> None:
    reason = failure.get("reason") or "failure"
    message = failure.get("message") or ""
    body = Text()
    if url:
        body.append(f"{url}\n", style="cyan")
    body.append(str(reason), style="bold red")
    if message:
        body.append(f"\n{message}")
    con.print(Panel(body, title="failure", border_style="red", expand=False))


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def render_search(rows: list[dict[str, Any]], console: Console | None = None) -> None:
    con = console or make_console()
    if not rows:
        con.print("[dim]no results[/dim]")
        return
    table = Table(show_lines=False, expand=True, header_style="bold")
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Title", style="bold", ratio=2)
    table.add_column("URL", style="cyan", ratio=2, overflow="fold")
    table.add_column("Snippet", style="dim", ratio=3, overflow="fold")
    for i, row in enumerate(rows, 1):
        title = str(row.get("title") or "")
        url = str(row.get("url") or "")
        snippet = str(row.get("snippet") or "")
        table.add_row(str(i), _link(title, url), url, snippet)
    con.print(table)


# ---------------------------------------------------------------------------
# adapter listings / products
# ---------------------------------------------------------------------------


def render_listings(quality: dict[str, Any], console: Console | None = None) -> None:
    con = console or make_console()
    listings = quality.get("listings") or []
    if not listings:
        con.print("[dim]no listings[/dim]")
        return
    table = Table(show_lines=False, expand=True, header_style="bold")
    table.add_column("Title", style="bold", ratio=3)
    table.add_column("Price", style="green", justify="right", no_wrap=True)
    table.add_column("Specs", ratio=1, overflow="fold")
    table.add_column("Location", style="dim", ratio=2, overflow="fold")
    currency = quality.get("currency")
    for item in listings:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("type") or "")
        url = item.get("url")
        price = _fmt_price(_first(item, "price"), currency)
        table.add_row(_link(title, url), price, _specs(item), _location(item))
    con.print(table)


def _specs(item: dict[str, Any]) -> str:
    parts: list[str] = []
    area = _attr(item, "area")
    if area:
        parts.append(f"{area}m²" if isinstance(area, (int, float)) else str(area))
    for key, suffix in (("bedrooms", "q"), ("bathrooms", "b"), ("parking", "g")):
        val = _attr(item, key)
        if val:
            parts.append(f"{val}{suffix}")
    return " · ".join(parts)


def _location(item: dict[str, Any]) -> str:
    place = _first(item, "neighborhood")
    city = _first(item, "city", "municipality")
    uf = _first(item, "uf")
    bits = [str(b) for b in (place, city, uf) if b]
    return ", ".join(bits)


def render_products(quality: dict[str, Any], console: Console | None = None) -> None:
    con = console or make_console()
    products = quality.get("products") or []
    if not products:
        con.print("[dim]no products[/dim]")
        return
    table = Table(show_lines=False, expand=True, header_style="bold")
    table.add_column("Title", style="bold", ratio=3)
    table.add_column("Price", style="green", justify="right", no_wrap=True)
    table.add_column("Store", style="dim", ratio=1, overflow="fold")
    table.add_column("Rating", justify="right", no_wrap=True)
    currency = quality.get("currency")
    for item in products:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        url = item.get("url")
        price = _fmt_price(_first(item, "price", "price_brl", "price_range"), currency)
        table.add_row(_link(title, url), price, _store(item), _rating(item))
    con.print(table)


def _store(item: dict[str, Any]) -> str:
    seller = _first(item, "seller", "brand")
    if seller:
        return str(seller)
    sellers = item.get("sellers")
    if isinstance(sellers, list) and sellers and isinstance(sellers[0], dict):
        return str(sellers[0].get("store") or "")
    return ""


def _rating(item: dict[str, Any]) -> str:
    rating = _first(item, "rating", "product_rating")
    if rating is None:
        return ""
    count = _first(item, "review_count", "product_review_count")
    return f"★ {rating}" + (f" ({count})" if count else "")


# ---------------------------------------------------------------------------
# answer
# ---------------------------------------------------------------------------


def render_answer(result: dict[str, Any], console: Console | None = None) -> None:
    con = console or make_console()
    if result.get("failure"):
        _render_failure(result["failure"], con, url=result.get("url"))
        return
    if result.get("error"):
        msg = result.get("message") or result["error"]
        con.print(
            Panel(
                str(msg),
                title=str(result["error"]),
                border_style="yellow",
                expand=False,
            )
        )
        return

    answer = result.get("answer") or ""
    if str(answer).strip():
        con.print(Markdown(str(answer)))
    else:
        con.print("[dim](no answer)[/dim]")

    footer: list[str] = []
    if result.get("model"):
        footer.append(str(result["model"]))
    if result.get("from_cache"):
        footer.append("cached")
    if result.get("url"):
        footer.append(str(result["url"]))
    con.print()
    if result.get("question"):
        con.print(f"Q: {result['question']}", style="dim italic")
    if footer:
        con.print("  ·  ".join(footer), style="dim")


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


def render_extract(result: dict[str, Any], console: Console | None = None) -> None:
    con = console or make_console()
    if result.get("failure"):
        _render_failure(result["failure"], con, url=result.get("url"))
        return

    header: list[str] = []
    if result.get("query"):
        header.append(f"query: {result['query']}")
    if result.get("ranker"):
        header.append(str(result["ranker"]))
    if result.get("url"):
        header.append(str(result["url"]))
    if header:
        con.print("  ·  ".join(header), style="dim")
        con.print()

    passages = result.get("passages") or []
    if not passages:
        con.print("[dim]no passages[/dim]")
        return
    for i, passage in enumerate(passages, 1):
        if not isinstance(passage, dict):
            continue
        score = passage.get("score")
        title = f"#{i}" + (f"  ·  score={score}" if score is not None else "")
        text = passage.get("text") or passage.get("context") or ""
        con.print(
            Panel(
                str(text),
                title=title,
                title_align="left",
                border_style="dim",
                expand=True,
            )
        )


# ---------------------------------------------------------------------------
# json (cache stats / config show / logs stats)
# ---------------------------------------------------------------------------


def render_json(obj: Any, console: Console | None = None) -> None:
    con = console or make_console()
    con.print_json(data=obj, default=str)


# ---------------------------------------------------------------------------
# streaming line renderers (map / cache list)
# ---------------------------------------------------------------------------


def render_map(
    records: Iterable[dict[str, Any]], console: Console | None = None
) -> int:
    """Stream styled lines for ``map`` records; returns the count emitted."""
    con = console or make_console()
    count = 0
    for record in records:
        source = record.get("source") or ""
        url = record.get("url") or ""
        lastmod = record.get("lastmod")
        line = Text()
        line.append(f"[{source}] ", style="magenta")
        line.append(str(url), style="cyan")
        if lastmod:
            line.append(f"  {lastmod}", style="dim")
        con.print(line)
        count += 1
    if count == 0:
        con.print("[dim]no urls found[/dim]")
    return count


def render_cache_list(
    entries: Iterator[dict[str, Any]], console: Console | None = None
) -> None:
    con = console or make_console()
    any_row = False
    for entry in entries:
        any_row = True
        url = entry.get("url") or ""
        status = entry.get("status")
        age = _fmt_age(entry.get("fetched_at"))
        line = Text()
        line.append(str(url), style="cyan")
        if status is not None:
            line.append(f"  {status}", style="dim")
        if age:
            line.append(f"  {age}", style="dim")
        con.print(line)
    if not any_row:
        con.print("[dim]cache is empty[/dim]")
