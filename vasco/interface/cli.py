from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from dataclasses import asdict, is_dataclass
from time import monotonic as _monotonic
from typing import TYPE_CHECKING, Annotated, Any

import typer

# Heavy `vasco.*` submodules (fetch stack → trafilatura/httpx/bs4, etc.) are
# imported lazily inside the command bodies, not at module load, so `vasco
# --help` and the light commands don't pay for the whole fetch pipeline. Only
# stdlib + typer are imported up front. See plan: startup-speed lazy imports.
if TYPE_CHECKING:
    from vasco.cache import Cache
    from vasco.config import Config

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


def _open_cache(cfg: Config) -> Cache:
    from vasco import cache as _cache

    path = cfg.cache.path or None
    return _cache.Cache(path)


# Shared output-format options. ``--human`` forces the rich (pretty) path even
# when piped; ``--json`` forces machine output even on a terminal; default is
# auto by TTY. See ``vasco.io.resolve_human``.
HumanOpt = Annotated[
    bool,
    typer.Option("--human", "-H", help="Force human-readable output even when piped."),
]
JsonOpt = Annotated[
    bool,
    typer.Option(
        "--json", help="Force machine output (JSON/NDJSON) even on a terminal."
    ),
]


def _resolve_output(human: bool, json_: bool) -> bool:
    """True → render human/pretty output; False → machine. Guards exclusivity."""
    if human and json_:
        raise typer.BadParameter("--human and --json are mutually exclusive")
    from vasco import io as _io

    return _io.resolve_human(human, json_)


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
    human: HumanOpt = False,
) -> None:
    """Query the web and stream title/url/snippet records."""
    from vasco import config as _config
    from vasco import search as _search
    from vasco import telemetry as _telemetry

    cfg = _config.load_config()
    effective_backend = backend or cfg.search.default_backend
    started = _monotonic()
    try:
        searcher = _search.get_searcher(effective_backend, cfg=cfg)
        kwargs: dict[str, Any] = {
            "max_results": max_ if max_ is not None else cfg.search.max_results,
            "region": region or cfg.search.region,
        }
        if time is not None:
            kwargs["time"] = time
        if site is not None:
            kwargs["site"] = site

        results = list(searcher.search(query, **kwargs))
    except Exception as exc:
        _telemetry.record_exception(
            cfg, "search", exc, query=query, site=site, backend=effective_backend
        )
        raise
    _telemetry.record_success(
        cfg,
        "search",
        query=query,
        backend=effective_backend,
        site=site,
        result_count=len(results),
        duration_ms=int((_monotonic() - started) * 1000),
    )

    rows = [_result_to_dict(r) for r in results]

    if _resolve_output(human, json_):
        from vasco import render as _render

        _render.render_search(rows)
        return

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
    is_human: bool,
    cache: Any | None,
    cfg: Config,
) -> None:
    from vasco import fetch as _fetch
    from vasco import io as _io
    from vasco import telemetry as _telemetry

    con = None
    if is_human:
        from vasco import render as _render

        con = _render.make_console()

    envelopes: list[dict[str, Any]] = []
    first = True
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
        if "failure" in env:
            _telemetry.record_failure(cfg, "fetch_many", env)
        else:
            _telemetry.record_success(
                cfg, "fetch_many", **_telemetry.fetch_success_fields(env)
            )
        if is_human:
            if not first:
                con.rule(style="dim")
            _render.render_fetch(env, con)
            first = False
        elif concat:
            envelopes.append(env)
        else:
            _io.write_ndjson(env)
            sys.stdout.flush()

    if concat and not is_human:
        chunks = [(e.get("markdown") or "") for e in envelopes]
        sys.stdout.write("\n---\n\n".join(chunks))
        if chunks and not chunks[-1].endswith("\n"):
            sys.stdout.write("\n")


@app.command()
def fetch(
    urls: Annotated[list[str], typer.Argument(help="One or more URLs to fetch.")],
    mode: Annotated[
        str,
        typer.Option(help="Fetch mode: auto|http|browser|mobile|wayback."),
    ] = "auto",
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
    human: HumanOpt = False,
) -> None:
    """Fetch one or more URLs and emit envelopes."""
    from vasco import config as _config
    from vasco import fetch as _fetch
    from vasco import telemetry as _telemetry

    cfg = _config.load_config()
    is_human = _resolve_output(human, json_)
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
            if "failure" in env:
                _telemetry.record_failure(cfg, "fetch", env)
            else:
                _telemetry.record_success(
                    cfg, "fetch", **_telemetry.fetch_success_fields(env)
                )
            if is_human:
                from vasco import render as _render

                _render.render_fetch(env)
            else:
                from vasco import io as _io

                _io.write_json(env)
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
                is_human=is_human,
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
    mode: Annotated[
        str,
        typer.Option(help="Fetch mode: auto|http|browser|mobile|wayback."),
    ] = "auto",
    rank: Annotated[
        str, typer.Option("--rank", help="Ranking: bm25|semantic.")
    ] = "bm25",
    deadline: Annotated[str | None, typer.Option(help="Deadline e.g. 15s, 1m.")] = None,
    human: HumanOpt = False,
    json_: JsonOpt = False,
) -> None:
    """Fetch a URL and print ranked passages."""
    if rank not in ("bm25", "semantic"):
        raise typer.BadParameter("--rank must be one of: bm25, semantic")
    is_human = _resolve_output(human, json_)
    from vasco import config as _config
    from vasco import extract as _extract
    from vasco import telemetry as _telemetry

    cfg = _config.load_config()
    deadline_seconds = (
        parse_duration(deadline) if deadline is not None else cfg.fetch.deadline_seconds
    )
    cache = _open_cache(cfg)
    try:
        started = _monotonic()
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
            _telemetry.record_exception(cfg, "extract", exc, url=url, query=query)
            raise
        duration_ms = int((_monotonic() - started) * 1000)
        passages = result.get("passages") if isinstance(result, dict) else None
        if not passages:
            _telemetry.log_event(
                cfg,
                {
                    "tool": "extract",
                    "outcome": "empty",
                    "url": url,
                    "query": query,
                    "rank": rank,
                    "empty_passages": True,
                    "duration_ms": duration_ms,
                },
            )
        else:
            _telemetry.record_success(
                cfg,
                "extract",
                url=url,
                query=query,
                rank=rank,
                passage_count=len(passages),
                duration_ms=duration_ms,
            )
        if is_human:
            from vasco import render as _render

            _render.render_extract(result)
        else:
            json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
            sys.stdout.write("\n")
    finally:
        cache.close()


# ---------------------------------------------------------------------------
# answer
# ---------------------------------------------------------------------------


@app.command()
def answer(
    url: Annotated[str, typer.Argument(help="URL to answer over.")],
    question: Annotated[
        str | None,
        typer.Option(
            "--question", "-q", help="Question to answer; omit for a generic summary."
        ),
    ] = None,
    mode: Annotated[
        str, typer.Option(help="Fetch mode: auto|http|browser|mobile|wayback.")
    ] = "auto",
    deadline: Annotated[str | None, typer.Option(help="Deadline e.g. 15s, 1m.")] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Ignore cache on read; still write.")
    ] = False,
    human: HumanOpt = False,
    json_: JsonOpt = False,
) -> None:
    """Fetch a URL and print an LLM answer/summary over its content."""
    from vasco import config as _config
    from vasco import summarize as _summarize
    from vasco import telemetry as _telemetry

    cfg = _config.load_config()
    is_human = _resolve_output(human, json_)
    deadline_seconds = (
        parse_duration(deadline) if deadline is not None else cfg.fetch.deadline_seconds
    )
    cache = _open_cache(cfg)
    try:
        started = _monotonic()
        try:
            result = asyncio.run(
                _summarize.answer(
                    url,
                    question=question,
                    mode=mode,
                    deadline=deadline_seconds,
                    refresh=refresh,
                    cache=cache,
                    cfg=cfg,
                )
            )
        except Exception as exc:
            _telemetry.record_exception(cfg, "answer", exc, url=url, question=question)
            raise
        duration_ms = int((_monotonic() - started) * 1000)
        if "failure" in result:
            _telemetry.record_failure(cfg, "answer", result)
        elif result.get("error"):
            _telemetry.log_event(
                cfg,
                {
                    "tool": "answer",
                    "outcome": "fail",
                    "url": url,
                    "error": result["error"],
                    "duration_ms": duration_ms,
                },
            )
        else:
            _telemetry.record_success(
                cfg,
                "answer",
                url=url,
                question=question,
                from_cache=result.get("from_cache"),
                duration_ms=duration_ms,
            )
        if is_human:
            from vasco import render as _render

            _render.render_answer(result)
        else:
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
    source: Annotated[
        str, typer.Option(help="llmstxt|sitemap|feeds|spider|all.")
    ] = "all",
    limit: Annotated[int, typer.Option(help="Maximum URLs to emit.")] = 1000,
    exclude: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude",
            help="Substring(s) to filter out (repeatable). E.g. --exclude /team/ --exclude /tag/.",
        ),
    ] = None,
    human: HumanOpt = False,
    json_: JsonOpt = False,
) -> None:
    """Discover URLs on a site and stream the records."""
    from vasco import config as _config
    from vasco import io as _io
    from vasco import map as _map
    from vasco import telemetry as _telemetry

    cfg = _config.load_config()
    is_human = _resolve_output(human, json_)
    started = _monotonic()
    count = 0
    try:
        records = _map.map_site(url, source=source, limit=limit, exclude=exclude)
        if is_human:
            from vasco import render as _render

            count = _render.render_map(records)
        else:
            for record in records:
                _io.write_ndjson(record)
                sys.stdout.flush()
                count += 1
    except Exception as exc:
        _telemetry.record_exception(cfg, "map", exc, url=url, source=source)
        raise
    _telemetry.record_success(
        cfg,
        "map",
        url=url,
        source=source,
        result_count=count,
        duration_ms=int((_monotonic() - started) * 1000),
    )


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


@app.command()
def normalize(url: Annotated[str, typer.Argument(help="URL to canonicalize.")]) -> None:
    """Print the canonical form used as the cache key."""
    from vasco import cache as _cache

    print(_cache.normalize_url(url))


# ---------------------------------------------------------------------------
# cache subcommands
# ---------------------------------------------------------------------------


cache_app = typer.Typer(
    no_args_is_help=True, help="Inspect and manage the fetch cache."
)
app.add_typer(cache_app, name="cache")


@cache_app.command("list")
def cache_list(
    human: HumanOpt = False,
    json_: JsonOpt = False,
) -> None:
    """List cache entries (NDJSON when piped, styled lines on a terminal)."""
    from vasco import config as _config
    from vasco import io as _io

    cfg = _config.load_config()
    is_human = _resolve_output(human, json_)
    c = _open_cache(cfg)
    try:
        if is_human:
            from vasco import render as _render

            _render.render_cache_list(c.list_entries())
        else:
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
    domain: Annotated[
        str | None,
        typer.Option(
            "--domain",
            help="Drop all entries for a domain (e.g. vivareal.com.br); matches subdomains.",
        ),
    ] = None,
) -> None:
    """Delete cached entries, by domain and/or older than a duration."""
    from vasco import config as _config

    cfg = _config.load_config()
    c = _open_cache(cfg)
    try:
        if domain is not None:
            n = c.purge_domain(domain)
            print(f"Deleted {n} entries for {domain}")
        else:
            seconds = parse_duration(older_than) if older_than is not None else None
            n = c.purge(
                older_than_seconds=int(seconds) if seconds is not None else None
            )
            print(f"Deleted {n} entries")
    finally:
        c.close()


@cache_app.command("stats")
def cache_stats(
    human: HumanOpt = False,
    json_: JsonOpt = False,
) -> None:
    """Print cache stats."""
    from vasco import config as _config

    cfg = _config.load_config()
    is_human = _resolve_output(human, json_)
    c = _open_cache(cfg)
    try:
        stats = c.stats()
    finally:
        c.close()
    if is_human:
        from vasco import render as _render

        _render.render_json(stats)
    else:
        json.dump(stats, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# config subcommands
# ---------------------------------------------------------------------------


config_app = typer.Typer(no_args_is_help=True, help="Inspect Vasco configuration.")
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show(
    human: HumanOpt = False,
    json_: JsonOpt = False,
) -> None:
    """Print the effective config (defaults + YAML + env overrides)."""
    from vasco import config as _config

    cfg = _config.load_config()
    data = asdict(cfg)
    if _resolve_output(human, json_):
        from vasco import render as _render

        _render.render_json(data)
    else:
        json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# logs subcommands
# ---------------------------------------------------------------------------


logs_app = typer.Typer(no_args_is_help=True, help="Inspect the telemetry event log.")
app.add_typer(logs_app, name="logs")


@logs_app.command("stats")
def logs_stats(
    days: Annotated[
        int, typer.Option("--days", help="Days of history to include (default 1).")
    ] = 1,
    human: HumanOpt = False,
    json_: JsonOpt = False,
) -> None:
    """Print a rollup of telemetry events."""
    from vasco import config as _config
    from vasco.telemetry import logstats as _logstats

    cfg = _config.load_config()
    summary = _logstats.summarize(cfg, days=days)
    if _resolve_output(human, json_):
        from vasco import render as _render

        _render.render_json(summary)
    else:
        json.dump(summary, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# mcp
# ---------------------------------------------------------------------------


@app.command("mcp")
def mcp() -> None:
    """Run the MCP server on stdio.

    Exposes search, fetch, fetch_many, extract, answer, and map as MCP tools
    for agent clients (Claude Desktop, Claude Code). The BrowserPool and any
    loaded semantic model stay warm for the server's lifetime.
    """
    from vasco.interface import mcp as _mcp

    _mcp.run()


@app.command("browser-server")
def browser_server() -> None:
    """Run a persistent Camoufox browser server on a UNIX socket.

    Other vasco consumers (MCP, CLI, library callers like claudinho) connect
    to the shared browser automatically — zero cold start, one Firefox process.
    Reads browser config (locale, persistent profile) from ~/.config/vasco/config.yaml.
    """
    import signal

    from vasco.config import load_config
    from vasco.fetch.browser_server import run_server

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    loop = asyncio.new_event_loop()
    task = loop.create_task(run_server(cfg))
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()


@app.command("browser-solve")
def browser_solve(
    url: Annotated[str, typer.Argument(help="URL to open for manual captcha solving.")],
    deadline: Annotated[
        str | None, typer.Option(help="Overall deadline, e.g. 2m, 180s.")
    ] = None,
    human: HumanOpt = False,
) -> None:
    """Open a URL in the browser tier and hold it for manual captcha solving via VNC.

    Requires the browser server running with `browser.manual_solve: true` (which
    starts a sized Xvfb + x11vnc on localhost). If the page is a challenge the
    auto-solver can't clear, vasco fires a desktop notification and holds the page
    open so you can connect a VNC viewer to localhost:<vnc_port> and solve it by
    hand; the resulting cf_clearance persists for later fetches.
    """
    from vasco import config as _config
    from vasco import fetch as _fetch
    from vasco import telemetry as _telemetry

    cfg = _config.load_config()
    deadline_seconds = parse_duration(deadline) if deadline is not None else 180.0
    port = getattr(cfg.browser, "vnc_port", 5900)
    typer.echo(
        f"Opening {url} in the browser tier. If a captcha appears you'll get a "
        f"notification — connect a VNC viewer to localhost:{port} to solve it.",
        err=True,
    )
    cache = _open_cache(cfg)
    try:
        env = asyncio.run(
            _fetch.fetch_one(
                url,
                mode="browser",
                deadline=deadline_seconds,
                use_cache=True,
                refresh=True,  # don't return a negative-cached blocked_captcha
                cache=cache,
                cfg=cfg,
            )
        )
    finally:
        cache.close()

    if "failure" in env:
        _telemetry.record_failure(cfg, "fetch", env)
    else:
        _telemetry.record_success(cfg, "fetch", **_telemetry.fetch_success_fields(env))

    if _resolve_output(human, False):
        from vasco import render as _render

        _render.render_fetch(env)
    else:
        from vasco import io as _io

        _io.write_json(env)


@app.command("serve")
def serve() -> None:
    """Run vascod — the resident vasco daemon — on a UNIX socket.

    Owns the full fetch pipeline (one Config + one Cache) and serves every local
    consumer (CLI, MCP, claudinho) over $XDG_RUNTIME_DIR/vasco/vascod.sock, adding
    cross-consumer single-flight + per-domain rate-limiting. Sits in front of the
    browser server (which it uses as a client, never owns). Local-only by
    construction: UNIX socket, mode 0600.
    """
    import signal

    from vasco.config import load_config
    from vasco.service.daemon import run_daemon

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    loop = asyncio.new_event_loop()
    task = loop.create_task(run_daemon(cfg))
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()


if __name__ == "__main__":  # pragma: no cover
    app()
