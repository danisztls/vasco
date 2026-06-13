"""Wikimedia Phabricator (Phorge) adapter.

Phabricator/Phorge task pages and search results are **server-rendered HTML**
served on the plain http tier (no JS app, no bot challenge), but the default
trafilatura pipeline flattens the curtain sidebar, the timeline, and the remarkup
description into lossy prose. This adapter parses the stable Phorge markup into
structured task data instead.

Two page types are claimed (on a known Phabricator host):

- **Task** (``/T<id>``): the ``og:title`` meta — ``"T<id> <title>"`` — is the
  structural anchor. Status/priority come from the header subheader tag, the
  description from the property-list remarkup (converted to Markdown),
  author/assignee/tags/subscribers from the curtain panels, comments (with author
  and timestamp) from the timeline, and related objects (mentioned-in / here,
  subtasks, parents, duplicates, …) from the property lists.
- **Search / list** (``/search/?query=…&types=TASK`` or ``/maniphest/?…``): the
  ``ul.phui-oi-list-view`` object-item list is the anchor → a list of
  ``{id, title, url, status, snippet}``. Both endpoints serve over **GET** (a
  read needs no CSRF token).

The Conduit API requires an auth token on Wikimedia Phabricator (anonymous calls
get ``ERR-INVALID-SESSION``), so this is intentionally an **unauthenticated HTML
scraper** — it can only ever read *public* data, which is also a safety property
(prompt injection can't escalate to restricted tasks). A restricted task — which
redirects an anonymous user to the login wall or returns a policy-exception page
— is surfaced as a clear ``LOGIN_REQUIRED`` failure, never silently.

Modeled on Eric Gardner's ``mcp-phabricator`` scraper backend
(gitlab.wikimedia.org/egardner/mcp-phabricator).

Scope: task pages + task search on a known Phabricator host (the built-in
``phabricator.wikimedia.org`` ∪ ``cfg.adapters.phabricator.domains``). Project /
workboard / file URLs fall through to a normal fetch — they need the Conduit API
for useful structured data. The adapter never raises; it returns a failure
envelope.

Rot contract (per the project invariants): a task page whose ``og:title`` anchor
is absent and which is *not* an auth wall → :class:`AdapterParseError` →
``PARSE_FAILED``; a search page with no ``ul.phui-oi-list-view`` container →
``PARSE_FAILED``; a search whose container is present but holds zero task items →
``success`` + ``["no_results"]``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urljoin, urlsplit

from .. import envelope
from ..errors import AdapterParseError, FailureReason
from . import _common
from ._common import (
    HtmlFetcher,
    compact as _compact,
    soup as _soup,
)

if TYPE_CHECKING:
    from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL detection / routing
# ---------------------------------------------------------------------------

# The instance this adapter ships for. Other public Phorge instances (same
# markup) can be added via ``cfg.adapters.phabricator.domains``.
_DEFAULT_HOSTS: tuple[str, ...] = ("phabricator.wikimedia.org",)

_TASK_PATH_RE = re.compile(r"^/T(\d+)/?$")
# Result/list endpoints that share the .phui-oi object-item markup and serve
# over GET: global search (?query=…&types=TASK) and Maniphest (?subscribers=…).
_SEARCH_SEGMENTS: frozenset[str] = frozenset({"search", "maniphest"})

# og:title on a task page is "T<id> <title>"; the leading token is the anchor.
_TASK_OGTITLE_RE = re.compile(r"^T(\d+)\b\s*(.*)$", re.DOTALL)
# Strip the "T<id>: " prefix Phabricator puts on result-list link titles.
_TASK_LINK_PREFIX_RE = re.compile(r"^T\d+:\s*")
_TASK_HREF_RE = re.compile(r"^/T(\d+)$")


def _known_hosts(cfg: Any | None) -> frozenset[str]:
    extra = getattr(
        getattr(getattr(cfg, "adapters", None), "phabricator", None), "domains", ()
    )
    return frozenset(str(h).lower() for h in (*_DEFAULT_HOSTS, *(extra or ())) if h)


def _claim(url: str, cfg: Any | None = None) -> tuple[str, str] | None:
    """Map a Phabricator URL to ``(page_type, key)`` or ``None`` if unclaimable.

    ``("task", task_id)`` for ``/T<id>``; ``("search", url)`` for a
    ``/search`` or ``/maniphest`` results URL (the whole URL is the fetch
    target). Anything else on the host (project/workboard/file/homepage) returns
    ``None`` → normal fetch.
    """
    if not url:
        return None
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in _known_hosts(cfg):
        return None
    path = parts.path or "/"
    m = _TASK_PATH_RE.match(path)
    if m:
        return "task", m.group(1)
    segs = [s for s in path.split("/") if s]
    if segs and segs[0] in _SEARCH_SEGMENTS:
        return "search", url
    return None


def is_phabricator_url(url: str, cfg: Any | None = None) -> bool:
    """A Phabricator task or search URL on a known host that we claim."""
    return _claim(url, cfg) is not None


def _base_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _task_url(base: str, task_id: str) -> str:
    return f"{base}/T{task_id}"


def _abs(base: str, href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith("#"):
        return None
    return urljoin(base + "/", href)


# ---------------------------------------------------------------------------
# Small DOM helpers (pure)
# ---------------------------------------------------------------------------


def _node_text(node: Any) -> str | None:
    if node is None:
        return None
    txt = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
    return txt or None


def _og_title(soup: BeautifulSoup) -> str | None:
    meta = soup.select_one('meta[property="og:title"]')
    if meta is None:
        return None
    content = meta.get("content")
    return content.strip() if isinstance(content, str) and content.strip() else None


# ---------------------------------------------------------------------------
# Remarkup → Markdown (port of mcp-phabricator's remarkupToText, Markdown'd)
# ---------------------------------------------------------------------------


def _remarkup_md(node: Any, base: str) -> str:
    """Convert a ``.phabricator-remarkup`` fragment to Markdown.

    Preserves the meaningful structure (links, lists, code, blockquotes, line
    breaks) while dropping presentational markup, so a task description / comment
    reads cleanly and cheaply. Recursive; defensive — unknown tags just recurse.
    """
    if node is None:
        return ""
    parts: list[str] = []
    for child in getattr(node, "children", []):
        name = getattr(child, "name", None)
        if name is None:  # text / comment node
            if type(child).__name__ == "Comment":
                continue
            parts.append(str(child))
            continue
        tag = name.lower()
        if tag == "br":
            parts.append("\n")
        elif tag in ("p", "div"):
            parts.append("\n" + _remarkup_md(child, base) + "\n")
        elif tag == "a":
            text = (child.get_text() or "").strip()
            href = child.get("href") or ""
            if href and text and not href.startswith("#") and text != href:
                parts.append(f"[{text}]({_abs(base, href) or href})")
            else:
                parts.append(text)
        elif tag in ("ul", "ol"):
            for i, li in enumerate(child.find_all("li", recursive=False)):
                prefix = f"{i + 1}. " if tag == "ol" else "- "
                parts.append("\n" + prefix + _remarkup_md(li, base).strip())
            parts.append("\n")
        elif tag == "pre":
            parts.append("\n```\n" + (child.get_text() or "") + "\n```\n")
        elif tag == "code":
            parts.append("`" + (child.get_text() or "") + "`")
        elif tag == "blockquote":
            inner = _remarkup_md(child, base).strip()
            quoted = "\n".join("> " + ln for ln in inner.split("\n"))
            parts.append("\n" + quoted + "\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = "#" * int(tag[1])
            parts.append("\n" + level + " " + _remarkup_md(child, base).strip() + "\n")
        else:
            parts.append(_remarkup_md(child, base))
    return "".join(parts)


def _clean_md(text: str) -> str:
    """Collapse the runaway blank lines the recursive walk leaves behind."""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------------------
# Task page parsing
# ---------------------------------------------------------------------------

# Closed statuses Phabricator folds into "Closed, <status>" in the subheader.
_CLOSED_STATUSES: frozenset[str] = frozenset(
    {"Resolved", "Invalid", "Declined", "Wontfix", "Spite", "Duplicate"}
)
# Status words recognised in a search-result item's attribute list.
_STATUS_WORDS: frozenset[str] = frozenset(
    {
        "open",
        "closed",
        "resolved",
        "invalid",
        "declined",
        "wontfix",
        "stalled",
        "duplicate",
        "spite",
        "in progress",
    }
)


def _status_priority(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    """``(status, priority)`` from the header subheader tag.

    Phabricator renders a combined "Open, Needs Triage" / "Closed, Resolved"
    string; an *open* task splits into (status, priority), a *closed* one folds
    the resolution into the status and drops the priority.
    """
    tag = soup.select_one(".phui-header-subheader .phui-tag-view")
    text = _node_text(tag) or ""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) >= 2:
        if parts[0] == "Closed" and parts[1] in _CLOSED_STATUSES:
            return parts[1], None
        return parts[0], parts[1]
    return (parts[0] if parts else None), None


def _description(soup: BeautifulSoup, base: str) -> str | None:
    box = soup.select_one(".phui-property-list-text-content")
    el = box.select_one(".phabricator-remarkup") if box else None
    if el is None:
        return None
    md = _clean_md(_remarkup_md(el, base))
    return md or None


def _person(container: Any, base: str) -> dict[str, Any] | None:
    """A ``{username, url}`` ref from the first person link in ``container``."""
    if container is None:
        return None
    link = container.select_one(".phui-link-person")
    if link is None:
        return None
    username = _node_text(link)
    if not username:
        return None
    return _compact({"username": username, "url": _abs(base, link.get("href"))})


def _curtain_panels(soup: BeautifulSoup, base: str) -> dict[str, Any]:
    """Author, assignee, tags, and subscribers from the sidebar curtain panels."""
    result: dict[str, Any] = {
        "author": None,
        "assignee": None,
        "tags": [],
        "subscribers": [],
    }
    for panel in soup.select(".phui-curtain-panel"):
        header = (
            _node_text(panel.select_one(".phui-curtain-panel-header")) or ""
        ).lower()
        body = panel.select_one(".phui-curtain-panel-body")
        if body is None:
            continue
        if header == "authored by":
            result["author"] = _person(body, base)
        elif header == "assigned to":
            # An unassigned task renders an explicit "empty" ref-list element.
            if body.select_one(".phui-curtain-object-ref-list-view-empty") is None:
                result["assignee"] = _person(body, base)
        elif "tags" in header:  # live markup is "Project Tags"; older is "Tags"
            for item in body.select(".phabricator-handle-tag-list-item"):
                tag = item.select_one(".phui-tag-view")
                if tag is None:
                    continue
                name = _node_text(tag.select_one(".phui-tag-core")) or _node_text(tag)
                if name:
                    result["tags"].append(
                        _compact({"name": name, "url": _abs(base, tag.get("href"))})
                    )
        elif header == "subscribers":
            seen: set[str] = set()
            for link in body.select(".phui-link-person"):
                username = _node_text(link)
                if not username or username in seen:
                    continue
                seen.add(username)
                result["subscribers"].append(
                    _compact(
                        {"username": username, "url": _abs(base, link.get("href"))}
                    )
                )
    return result


def _comments(soup: BeautifulSoup, base: str, limit: int) -> list[dict[str, Any]]:
    """Comment transactions from the timeline, with author + timestamp metadata.

    Only comment transactions (``[data-sigil="transaction-comment"]``) are kept;
    status-change / edit transactions are skipped.
    """
    out: list[dict[str, Any]] = []
    for shell in soup.select(".phui-timeline-shell"):
        comment_el = shell.select_one('[data-sigil="transaction-comment"]')
        if comment_el is None:
            continue
        author = _node_text(
            shell.select_one(".phui-handle.phui-link-person")
        ) or _node_text(shell.select_one(".phui-link-person"))
        timestamp = _node_text(shell.select_one(".print-only"))
        anchor = shell.select_one(".phabricator-anchor-view")
        anchor_id = anchor.get("id") if anchor is not None else None
        content = _clean_md(
            _remarkup_md(comment_el.select_one(".phabricator-remarkup"), base)
        )
        out.append(
            _compact(
                {
                    "id": anchor_id,
                    "url": _abs(base, f"#{anchor_id}") if anchor_id else None,
                    "author": author,
                    "timestamp": timestamp,
                    "text": content or None,
                }
            )
        )
        if len(out) >= limit:
            break
    return out


def _related(soup: BeautifulSoup, base: str) -> dict[str, list[dict[str, Any]]]:
    """Related-object handle lists from the property lists (mentioned-in/here,
    subtasks, parents, duplicates, …), keyed by a slug of each ``dt`` label.

    Only ``dt``/``dd`` pairs that carry object handle links are kept, so plain
    text properties are ignored — what's left is exactly the "related objects".
    """
    related: dict[str, list[dict[str, Any]]] = {}
    for dl in soup.select(".phui-property-list-properties"):
        for dt in dl.select("dt"):
            dd = dt.find_next_sibling("dd")
            if dd is None:
                continue
            links: list[dict[str, Any]] = []
            for a in dd.select("a.phui-handle"):
                title = _node_text(a)
                if not title:
                    continue
                classes = a.get("class") or []
                links.append(
                    _compact(
                        {
                            "url": _abs(base, a.get("href")),
                            "title": title,
                            "closed": "handle-status-closed" in classes,
                        }
                    )
                )
            if not links:
                continue
            key = re.sub(r"[^a-z0-9]+", "_", (_node_text(dt) or "").lower()).strip("_")
            if key:
                related.setdefault(key, []).extend(links)
    return related


def _parse_task(
    soup: BeautifulSoup, html: str, url: str, base: str, *, max_comments: int
) -> dict[str, Any]:
    """Parse a task page → a structured task dict.

    Raises :class:`AdapterParseError` when the ``og:title`` task anchor is absent
    (scraper-rot — the caller has already ruled out an auth wall).
    """
    og = _og_title(soup)
    m = _TASK_OGTITLE_RE.match(og) if og else None
    if m is None:
        raise AdapterParseError(
            "task page: no `T<id> …` og:title anchor — Phabricator markup changed"
        )
    task_id = int(m.group(1))
    title = (m.group(2) or "").strip() or _node_text(
        soup.select_one(".phui-header-header")
    )
    status, priority = _status_priority(soup)
    panels = _curtain_panels(soup, base)
    return _compact(
        {
            "id": task_id,
            "name": f"T{task_id}",
            "title": title,
            "url": _task_url(base, str(task_id)),
            "status": status,
            "priority": priority,
            "author": panels["author"],
            "assignee": panels["assignee"],
            "tags": panels["tags"],
            "subscribers": panels["subscribers"],
            "description": _description(soup, base),
            "comments": _comments(soup, base, max_comments),
            "related": _related(soup, base),
        }
    )


# ---------------------------------------------------------------------------
# Search / list page parsing
# ---------------------------------------------------------------------------


def _search_query(url: str) -> str | None:
    qs = parse_qs(urlsplit(url).query)
    for key in ("query", "fulltext", "q"):
        vals = qs.get(key)
        if vals and vals[0].strip():
            return vals[0].strip()
    return None


def _result_status(item: Tag) -> str | None:
    for attr in item.select(".phui-oi-attribute"):
        text = (_node_text(attr) or "").lstrip("·").strip()
        if text.lower() in _STATUS_WORDS:
            return text
    return None


def _parse_search(soup: BeautifulSoup, base: str) -> list[dict[str, Any]]:
    """Parse a results page → task records. Raises :class:`AdapterParseError`
    when the ``ul.phui-oi-list-view`` container is absent (an empty container is
    a legitimate no-results and returns ``[]``)."""
    if soup.select_one("ul.phui-oi-list-view") is None:
        raise AdapterParseError(
            "search page: no `phui-oi-list-view` results container — not a "
            "Phabricator results page (or markup changed)"
        )
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in soup.select("li.phui-oi"):
        link = item.select_one("a.phui-oi-link")
        if link is None:
            continue
        href = (link.get("href") or "").strip()
        m = _TASK_HREF_RE.match(href)
        if m is None:  # non-task object in a mixed result set
            continue
        task_id = int(m.group(1))
        if task_id in seen:
            continue
        seen.add(task_id)
        full = link.get("title") or _node_text(link) or ""
        title = _TASK_LINK_PREFIX_RE.sub("", full).strip()
        out.append(
            _compact(
                {
                    "id": task_id,
                    "name": f"T{task_id}",
                    "title": title or None,
                    "url": _abs(base, href),
                    "status": _result_status(item),
                    "snippet": _node_text(item.select_one(".phui-source-fragment")),
                }
            )
        )
    return out


# ---------------------------------------------------------------------------
# Auth-wall detection
# ---------------------------------------------------------------------------

# Markers checked ONLY on an anchor-less page (no valid task/results), so a
# public task whose comment text merely discusses permissions never false-fires.
_AUTH_WALL_MARKERS: tuple[str, ...] = (
    "you do not have permission",  # Phorge policy-exception page
    "you shall not pass",  # Phorge policy-exception heading
    "phabricator-login",  # the auth/login form
)

_AUTH_MESSAGE = (
    "This Phabricator content is restricted and requires authentication to view. "
    "Vasco reads only public Phabricator data (no login)."
)


def _is_auth_wall(html: str, soup: BeautifulSoup) -> bool:
    lc = html.lower()
    if any(marker in lc for marker in _AUTH_WALL_MARKERS):
        return True
    title = _node_text(soup.select_one("title"))
    return bool(title) and title.strip().lower() == "login"


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_task(task: dict[str, Any]) -> str:
    parts = [f"# {task['name']}: {task.get('title') or ''}".rstrip()]
    facts: list[str] = []
    if task.get("status"):
        facts.append(f"**Status:** {task['status']}")
    if task.get("priority"):
        facts.append(f"**Priority:** {task['priority']}")
    author = task.get("author") or {}
    if author.get("username"):
        facts.append(f"**Author:** {author['username']}")
    assignee = task.get("assignee") or {}
    if assignee.get("username"):
        facts.append(f"**Assigned:** {assignee['username']}")
    if facts:
        parts += ["", " · ".join(facts)]
    tags = [t.get("name") for t in task.get("tags") or [] if t.get("name")]
    if tags:
        parts += ["", "**Tags:** " + ", ".join(tags)]
    if task.get("description"):
        parts += ["", "## Description", "", task["description"]]
    related = task.get("related") or {}
    if related:
        rel_lines = []
        for key, links in related.items():
            label = key.replace("_", " ").title()
            names = ", ".join(ln.get("title", "") for ln in links if ln.get("title"))
            if names:
                rel_lines.append(f"- **{label}:** {names}")
        if rel_lines:
            parts += ["", "## Related", "", *rel_lines]
    comments = task.get("comments") or []
    if comments:
        parts += ["", f"## Comments ({len(comments)})"]
        for c in comments:
            head = f"**{c.get('author') or 'unknown'}**"
            if c.get("timestamp"):
                head += f" · {c['timestamp']}"
            parts += ["", head]
            if c.get("text"):
                parts.append(c["text"])
    return "\n".join(parts)


def _render_search(tasks: list[dict[str, Any]], query: str | None) -> str:
    label = f'"{query}"' if query else "tasks"
    if not tasks:
        return f"# Phabricator search: {label}\n\nNo results."
    parts = [f"# Phabricator search: {label} — {len(tasks)} results", ""]
    for t in tasks:
        head = f"{t['name']}"
        if t.get("status"):
            head += f" ({t['status']})"
        head += f" — {t.get('title') or ''}".rstrip()
        parts.append(head)
        if t.get("snippet"):
            parts.append(f"  {t['snippet']}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Fetch + envelope
# ---------------------------------------------------------------------------

_base_envelope, _failure_envelope = _common.envelope_builders(
    "phabricator", "application/x-phabricator"
)


def _success_envelope(
    url: str,
    *,
    page_type: str,
    status: int,
    markdown: str,
    quality_extra: dict[str, Any],
    title: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    from .. import io as io_mod

    quality = _compact(
        {
            "provider": "phabricator",
            "page_type": page_type,
            **quality_extra,
        }
    )
    return envelope.success_envelope(
        base=_base_envelope(url, http_status=status or 200),
        markdown=markdown,
        metadata={
            "title": title,
            "byline": None,
            "published": None,
            "modified": None,
            "language": None,
            "site_name": "Phabricator",
            "image": None,
            "word_count": len(markdown.split()),
            "quality": quality,
            "warnings": warnings,
        },
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )


def _max_comments(cfg: Any | None) -> int:
    val = getattr(
        getattr(getattr(cfg, "adapters", None), "phabricator", None),
        "max_comments",
        50,
    )
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 50


async def fetch_phabricator(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
    fetch_html: HtmlFetcher | None = None,
) -> dict[str, Any]:
    """Fetch a Phabricator task or search URL → a structured envelope.

    HTML is obtained via ``fetch_html`` (the shared ``http → browser`` escalation
    chain, minus the wayback tail). Phabricator is server-rendered, so it resolves
    on the http tier with no strategy seed. Never raises — returns a failure
    envelope on any fetch/parse failure; a restricted task surfaces a clear
    ``LOGIN_REQUIRED``.
    """
    claim = _claim(url, cfg)
    if claim is None:  # defensive — dispatch only calls us on a claimed URL
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, "phabricator: unrecognized URL shape"
        )
    page_type, key = claim
    base = _base_url(url)
    target = _task_url(base, key) if page_type == "task" else url

    try:
        html, status, _headers, reason, mode_used = await _common.fetch_with_fallback(
            target, fetch_html=fetch_html, deadline=deadline, cfg=cfg
        )
    except asyncio.TimeoutError:
        return _failure_envelope(url, FailureReason.TIMEOUT, "fetch deadline elapsed")
    except Exception as exc:
        return _failure_envelope(
            url,
            _common.classify_browser_error(exc),
            f"fetch failed: {type(exc).__name__}: {exc}",
        )

    # 404 → honest not-found (the chain already classified the status). A
    # restricted task that *redirects* to login arrives as a 200 login page and
    # is handled by the auth-wall check below, not here.
    if reason == FailureReason.NOT_FOUND:
        return _failure_envelope(
            url,
            FailureReason.NOT_FOUND,
            f"phabricator: {target} not found (404)",
            http_status=status,
        )
    if not html:
        if reason != FailureReason.OK:
            return _failure_envelope(
                url, reason, f"fetch failed via {mode_used} tier", http_status=status
            )
        return _failure_envelope(
            url, FailureReason.EMPTY_BODY, "empty response body", http_status=status
        )

    soup = _soup(html)
    try:
        if page_type == "task":
            return _build_task(url, html, soup, base, status=status, cfg=cfg)
        return _build_search(url, html, soup, base, status=status)
    except AdapterParseError as exc:
        # The anchor is gone. If the page is an auth wall, that's the honest
        # reason; otherwise it's genuine scraper-rot.
        if _is_auth_wall(html, soup):
            return _failure_envelope(
                url, FailureReason.LOGIN_REQUIRED, _AUTH_MESSAGE, http_status=status
            )
        log.warning("phabricator parse anchor missing (%s): %s", page_type, exc)
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, f"phabricator {exc}", http_status=status
        )
    except Exception as exc:  # defensive — never raise out of an adapter
        log.warning("phabricator parse failed (%s): %s", page_type, exc)
        return _failure_envelope(
            url,
            FailureReason.PARSE_FAILED,
            f"phabricator parse failed: {type(exc).__name__}: {exc}",
            http_status=status,
        )


def _build_task(
    url: str,
    html: str,
    soup: BeautifulSoup,
    base: str,
    *,
    status: int,
    cfg: Any | None,
) -> dict[str, Any]:
    task = _parse_task(soup, html, url, base, max_comments=_max_comments(cfg))
    markdown = _render_task(task)
    return _success_envelope(
        url,
        page_type="task",
        status=status,
        markdown=markdown,
        quality_extra={"result_count": 1, "task": task},
        title=f"{task['name']}: {task.get('title') or ''}".rstrip(),
        warnings=[],
    )


def _build_search(
    url: str,
    html: str,
    soup: BeautifulSoup,
    base: str,
    *,
    status: int,
) -> dict[str, Any]:
    tasks = _parse_search(soup, base)
    query = _search_query(url)
    markdown = _render_search(tasks, query)
    return _success_envelope(
        url,
        page_type="search",
        status=status,
        markdown=markdown,
        quality_extra={"result_count": len(tasks), "query": query, "tasks": tasks},
        title=(f"Phabricator search: {query}" if query else "Phabricator search"),
        warnings=[] if tasks else ["no_results"],
    )
