from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import asdict, is_dataclass
from typing import Annotated, Any

import typer

from vasco import cache as _cache
from vasco import config as _config
from vasco import extract as _extract
from vasco import fetch as _fetch
from vasco import io as _io
from vasco import map as _map
from vasco import search as _search

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Vasco — web research CLI for agents.",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


_DURATION_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|s|m|h|d)?\s*$", re.IGNORECASE
)


def parse_duration(text: str) -> float:
    """Parse a duration string like ``"15s"``, ``"1m"``, ``"1500ms"``, ``"1.5h"``,
    ``"7d"``.

    Bare numbers are interpreted as seconds.
    """
    if text is None:
        raise typer.BadParameter("duration cannot be empty")
    match = _DURATION_RE.match(str(text))
    if not match:
        raise typer.BadParameter(f"invalid duration: {text!r}")
    value = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    return (
        value
        * {
            "ms": 0.001,
            "s": 1.0,
            "m": 60.0,
            "h": 3600.0,
            "d": 86400.0,
        }[unit]
    )


def _result_to_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if is_dataclass(result):
        return asdict(result)
    return {
        "title": getattr(result, "title", None),
        "url": getattr(result, "url", None),
        "snippet": getattr(result, "snippet", None),
    }


def _open_cache(cfg: _config.Config) -> _cache.Cache:
    path = cfg.cache.path or None
    return _cache.Cache(path)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search query.")],
    max_: Annotated[int | None, typer.Option("--max", help="Max results.")] = None,
    region: Annotated[str | None, typer.Option(help="Region code, e.g. us-en.")] = None,
    time: Annotated[str | None, typer.Option(help="Time filter: d|w|m|y.")] = None,
    site: Annotated[str | None, typer.Option(help="Restrict to a domain.")] = None,
    backend: Annotated[str | None, typer.Option(help="Search backend.")] = None,
    json_: Annotated[bool, typer.Option("--json", help="Emit a JSON array.")] = False,
) -> None:
    """Query the web and stream title/url/snippet records."""
    cfg = _config.load_config()
    searcher = _search.get_searcher(backend or cfg.search.default_backend, cfg=cfg)
    kwargs: dict[str, Any] = {
        "max_results": max_ if max_ is not None else cfg.search.max_results,
        "region": region or cfg.search.region,
    }
    if time is not None:
        kwargs["time"] = time
    if site is not None:
        kwargs["site"] = site

    results = list(searcher.search(query, **kwargs))
    rows = [_result_to_dict(r) for r in results]

    if json_:
        json.dump(rows, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return

    for row in rows:
        title = row.get("title") or ""
        url = row.get("url") or ""
        snippet = row.get("snippet") or ""
        sys.stdout.write(f"{title}\n{url}\n{snippet}\n\n")


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


async def _run_fetch_many(
    urls: list[str],
    *,
    mode: str,
    workers: int,
    use_cache: bool,
    refresh: bool,
    deadline: float,
    raw: bool,
    concat: bool,
    json_: bool,
    cache: Any | None,
    cfg: _config.Config,
) -> None:
    envelopes: list[dict[str, Any]] = []
    async for env in _fetch.fetch_many(
        urls,
        workers=workers,
        mode=mode,
        deadline=deadline,
        use_cache=use_cache,
        refresh=refresh,
        raw=raw,
        cache=cache,
        cfg=cfg,
    ):
        if concat:
            envelopes.append(env)
        else:
            _io.write_ndjson(env)
            sys.stdout.flush()

    if concat:
        chunks = [(e.get("markdown") or "") for e in envelopes]
        sys.stdout.write("\n---\n\n".join(chunks))
        if chunks and not chunks[-1].endswith("\n"):
            sys.stdout.write("\n")


@app.command()
def fetch(
    urls: Annotated[list[str], typer.Argument(help="One or more URLs to fetch.")],
    mode: Annotated[str, typer.Option(help="Fetch mode: auto|http|browser.")] = "auto",
    workers: Annotated[int | None, typer.Option(help="Concurrent fetches.")] = None,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Skip cache reads and writes.")
    ] = False,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Ignore cache on read; still write.")
    ] = False,
    deadline: Annotated[
        str | None, typer.Option(help="Deadline e.g. 15s, 1m, 1500ms.")
    ] = None,
    raw: Annotated[
        bool, typer.Option("--raw", help="Return raw HTML alongside markdown.")
    ] = False,
    json_: Annotated[
        bool, typer.Option("--json", help="Force JSON output for single URL.")
    ] = False,
    concat: Annotated[
        bool, typer.Option("--concat", help="Concatenate markdown for multi-URL.")
    ] = False,
) -> None:
    """Fetch one or more URLs and emit envelopes."""
    cfg = _config.load_config()
    deadline_seconds = (
        parse_duration(deadline) if deadline is not None else cfg.fetch.deadline_seconds
    )
    workers_n = workers if workers is not None else cfg.fetch.workers
    use_cache = not no_cache
    cache = _open_cache(cfg) if use_cache else None

    try:
        if len(urls) == 1:
            url = urls[0]
            env = asyncio.run(
                _fetch.fetch_one(
                    url,
                    mode=mode,
                    deadline=deadline_seconds,
                    use_cache=use_cache,
                    refresh=refresh,
                    raw=raw,
                    cache=cache,
                    cfg=cfg,
                )
            )
            if json_ or not _io.is_tty():
                _io.write_json(env)
            else:
                _io.write_markdown(env)
            return

        asyncio.run(
            _run_fetch_many(
                urls,
                mode=mode,
                workers=workers_n,
                use_cache=use_cache,
                refresh=refresh,
                deadline=deadline_seconds,
                raw=raw,
                concat=concat,
                json_=json_,
                cache=cache,
                cfg=cfg,
            )
        )
    finally:
        if cache is not None:
            cache.close()


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


@app.command()
def extract(
    url: Annotated[str, typer.Argument(help="URL to extract from.")],
    query: Annotated[
        str, typer.Option("--query", help="Query for passage ranking.")
    ] = ...,
    top: Annotated[int, typer.Option(help="Top K passages to return.")] = 5,
    context_chars: Annotated[
        int, typer.Option(help="Context chars around each passage.")
    ] = 400,
    mode: Annotated[str, typer.Option(help="Fetch mode: auto|http|browser.")] = "auto",
    rank: Annotated[
        str, typer.Option("--rank", help="Ranking: bm25|semantic.")
    ] = "bm25",
    deadline: Annotated[str | None, typer.Option(help="Deadline e.g. 15s, 1m.")] = None,
) -> None:
    """Fetch a URL and print ranked passages as pretty JSON."""
    if rank not in ("bm25", "semantic"):
        raise typer.BadParameter("--rank must be one of: bm25, semantic")
    cfg = _config.load_config()
    deadline_seconds = (
        parse_duration(deadline) if deadline is not None else cfg.fetch.deadline_seconds
    )
    cache = _open_cache(cfg)
    try:
        try:
            result = asyncio.run(
                _extract.extract(
                    url,
                    query=query,
                    top=top,
                    context_chars=context_chars,
                    mode=mode,
                    rank=rank,
                    deadline=deadline_seconds,
                    cache=cache,
                    cfg=cfg,
                )
            )
        except Exception as exc:  # surface SemanticRankerUnavailable cleanly
            from vasco.semantic import SemanticRankerUnavailable

            if isinstance(exc, SemanticRankerUnavailable):
                raise typer.BadParameter(str(exc)) from exc
            raise
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    finally:
        cache.close()


# ---------------------------------------------------------------------------
# map
# ---------------------------------------------------------------------------


@app.command("map")
def map_(
    url: Annotated[str, typer.Argument(help="Root URL of the site.")],
    source: Annotated[str, typer.Option(help="sitemap|feeds|spider|all.")] = "all",
    limit: Annotated[int, typer.Option(help="Maximum URLs to emit.")] = 1000,
    exclude: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude",
            help="Substring(s) to filter out (repeatable). E.g. --exclude /team/ --exclude /tag/.",
        ),
    ] = None,
) -> None:
    """Discover URLs on a site and stream NDJSON records."""
    for record in _map.map_site(url, source=source, limit=limit, exclude=exclude):
        _io.write_ndjson(record)
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


@app.command()
def normalize(url: Annotated[str, typer.Argument(help="URL to canonicalize.")]) -> None:
    """Print the canonical form used as the cache key."""
    print(_cache.normalize_url(url))


# ---------------------------------------------------------------------------
# cache subcommands
# ---------------------------------------------------------------------------


cache_app = typer.Typer(
    no_args_is_help=True, help="Inspect and manage the fetch cache."
)
app.add_typer(cache_app, name="cache")


@cache_app.command("list")
def cache_list() -> None:
    """Stream NDJSON of cache entries."""
    cfg = _config.load_config()
    c = _open_cache(cfg)
    try:
        for entry in c.list_entries():
            _io.write_ndjson(entry)
            sys.stdout.flush()
    finally:
        c.close()


@cache_app.command("purge")
def cache_purge(
    older_than: Annotated[
        str | None,
        typer.Option("--older-than", help="Drop entries older than e.g. 7d, 24h, 30m."),
    ] = None,
) -> None:
    """Delete cached entries, optionally older than a duration."""
    cfg = _config.load_config()
    seconds = parse_duration(older_than) if older_than is not None else None
    c = _open_cache(cfg)
    try:
        n = c.purge(older_than_seconds=int(seconds) if seconds is not None else None)
    finally:
        c.close()
    print(f"Deleted {n} entries")


@cache_app.command("stats")
def cache_stats() -> None:
    """Print cache stats as JSON."""
    cfg = _config.load_config()
    c = _open_cache(cfg)
    try:
        json.dump(c.stats(), sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    finally:
        c.close()


# ---------------------------------------------------------------------------
# mcp
# ---------------------------------------------------------------------------


@app.command("mcp")
def mcp() -> None:
    """Run the MCP server on stdio.

    Exposes search, fetch, fetch_many, extract, map, and normalize as MCP tools
    for agent clients (Claude Desktop, Claude Code). The BrowserPool and any
    loaded semantic model stay warm for the server's lifetime.
    """
    from vasco import mcp as _mcp

    _mcp.run()


if __name__ == "__main__":  # pragma: no cover
    app()
