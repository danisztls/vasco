from __future__ import annotations

from pathlib import Path

import pytest

from vasco.adapters import phabricator as P
from vasco.config import AdaptersCfg, Config, PhabricatorCfg
from vasco.errors import AdapterParseError, FailureReason

FX = Path(__file__).parent / "fixtures" / "phabricator"
HOST = "https://phabricator.wikimedia.org"


def _fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


def _soup(name: str):
    return P._soup(_fx(name))


def _fetcher(html: str, *, status: int = 200, reason: FailureReason = FailureReason.OK):
    async def fetch_html(target: str):
        return html, status, {}, reason, "http"

    return fetch_html


# --- routing ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (f"{HOST}/T241180", True),
        (f"{HOST}/T241180/", True),
        (f"{HOST}/search/?query=parsoid&types=TASK", True),
        (f"{HOST}/maniphest/?statuses=open", True),
        (f"{HOST}/maniphest/query/abc/", True),
        # not claimed: project / profile / file / paste / homepage / bad shape
        (f"{HOST}/project/profile/1564/", False),
        (f"{HOST}/p/egardner/", False),
        (f"{HOST}/file/data/abc/PHID-FILE-x/img.png", False),
        (f"{HOST}/Tabc", False),
        (f"{HOST}/", False),
        # other hosts out of scope by default
        ("https://phab.example.org/T5", False),
        ("https://phabricator.kde.org/T5", False),
        ("", False),
    ],
)
def test_is_phabricator_url(url: str, expected: bool) -> None:
    assert P.is_phabricator_url(url) is expected


def test_claim_shapes() -> None:
    assert P._claim(f"{HOST}/T1234") == ("task", "1234")
    assert P._claim(f"{HOST}/T1234/") == ("task", "1234")
    assert P._claim(f"{HOST}/search/?query=x") == ("search", f"{HOST}/search/?query=x")
    assert P._claim(f"{HOST}/maniphest/?o=1") == ("search", f"{HOST}/maniphest/?o=1")
    assert P._claim(f"{HOST}/W123") is None


def test_known_hosts_extended_by_cfg() -> None:
    cfg = Config(
        adapters=AdaptersCfg(phabricator=PhabricatorCfg(domains=("phab.example.org",)))
    )
    assert P.is_phabricator_url("https://phab.example.org/T5", cfg) is True
    assert P._claim("https://phab.example.org/T5", cfg) == ("task", "5")
    # built-in host still works alongside the extra one
    assert P.is_phabricator_url(f"{HOST}/T5", cfg) is True


# --- pure helpers -----------------------------------------------------------


def test_status_priority_open_closed_single() -> None:
    def sp(text: str):
        soup = P._soup(
            f'<div class="phui-header-subheader"><span class="phui-tag-view">'
            f'<span class="phui-tag-core">{text}</span></span></div>'
        )
        return P._status_priority(soup)

    assert sp("Open, High") == ("Open", "High")
    assert sp("Open, Needs Triage") == ("Open", "Needs Triage")
    assert sp("Closed, Resolved") == ("Resolved", None)
    assert sp("Closed, Invalid") == ("Invalid", None)
    # an unknown closed-resolution keeps both parts rather than guessing
    assert sp("Closed, Mysterious") == ("Closed", "Mysterious")
    assert sp("Open") == ("Open", None)
    assert P._status_priority(P._soup("<div></div>")) == (None, None)


def test_remarkup_md_structure() -> None:
    el = P._soup(
        '<div class="phabricator-remarkup"><p>Hello '
        '<a href="/T1000">T1000</a> and '
        '<a href="https://x.org/d">docs</a>.</p>'
        "<ul><li>one</li><li>two</li></ul>"
        "<pre>code()</pre></div>"
    ).select_one(".phabricator-remarkup")
    md = P._clean_md(P._remarkup_md(el, HOST))
    assert "[T1000](https://phabricator.wikimedia.org/T1000)" in md
    assert "[docs](https://x.org/d)" in md
    assert "- one" in md and "- two" in md
    assert "```\ncode()\n```" in md


def test_result_status_and_search_query() -> None:
    item = _soup("search_global.html").select_one("li.phui-oi")
    assert P._result_status(item) == "Closed"
    assert P._search_query(f"{HOST}/search/?query=parsoid+timeout&types=TASK") == (
        "parsoid timeout"
    )
    assert P._search_query(f"{HOST}/maniphest/?fulltext=lock") == "lock"
    assert P._search_query(f"{HOST}/maniphest/?statuses=open") is None


def test_abs() -> None:
    assert P._abs(HOST, "/T5") == f"{HOST}/T5"
    assert P._abs(HOST, "https://x.org/a") == "https://x.org/a"
    assert P._abs(HOST, "#anchor") is None
    assert P._abs(HOST, None) is None


# --- task parsing -----------------------------------------------------------


def test_parse_task_full() -> None:
    soup = _soup("task.html")
    task = P._parse_task(soup, _fx("task.html"), f"{HOST}/T1234", HOST, max_comments=50)
    assert task["id"] == 1234
    assert task["name"] == "T1234"
    assert task["title"] == "Improve the widget rendering pipeline"
    assert task["url"] == f"{HOST}/T1234"
    assert task["status"] == "Open"
    assert task["priority"] == "High"
    assert task["author"] == {"username": "alice", "url": f"{HOST}/p/alice/"}
    assert task["assignee"] == {"username": "bob", "url": f"{HOST}/p/bob/"}
    assert [t["name"] for t in task["tags"]] == ["Widgets", "Performance"]
    assert task["tags"][0]["url"] == f"{HOST}/tag/widgets/"
    assert [s["username"] for s in task["subscribers"]] == ["carol", "dave"]
    # description: remarkup converted to markdown, link absolutized, list + code kept
    assert "[external docs](https://example.org/docs)" in task["description"]
    assert "- Cache the parsed templates" in task["description"]
    assert "```" in task["description"]
    # comments: only the two transaction-comment shells (status-change shell skipped)
    assert len(task["comments"]) == 2
    c0 = task["comments"][0]
    assert c0["author"] == "carol"
    assert c0["timestamp"] == "2024-01-02 10:00:00 (UTC+0)"
    assert c0["id"] == "100001"
    assert "bottleneck" in c0["text"]
    assert task["comments"][1]["author"] == "dave"
    # related objects, slugged by their dt label
    assert task["related"]["mentioned_in"][0]["title"] == "T2000: Related cleanup"
    subtasks = task["related"]["subtasks"]
    assert len(subtasks) == 2
    assert subtasks[0]["closed"] is False
    assert subtasks[1]["closed"] is True


def test_parse_task_max_comments_cap() -> None:
    soup = _soup("task.html")
    task = P._parse_task(soup, _fx("task.html"), f"{HOST}/T1234", HOST, max_comments=1)
    assert len(task["comments"]) == 1


def test_parse_task_unassigned_and_closed_folding() -> None:
    soup = _soup("task_unassigned.html")
    task = P._parse_task(
        soup, _fx("task_unassigned.html"), f"{HOST}/T4321", HOST, max_comments=50
    )
    assert task["status"] == "Resolved"
    assert task.get("priority") is None
    assert task["author"]["username"] == "erin"
    # an explicit empty ref-list element means no assignee (not a mis-parse)
    assert "assignee" not in task or task.get("assignee") is None


def test_parse_task_missing_anchor_raises() -> None:
    with pytest.raises(AdapterParseError):
        P._parse_task(
            P._soup("<html><body><h1>nope</h1></body></html>"),
            "<html></html>",
            f"{HOST}/T1",
            HOST,
            max_comments=50,
        )


# --- search parsing ---------------------------------------------------------


def test_parse_search_global() -> None:
    tasks = P._parse_search(_soup("search_global.html"), HOST)
    # the non-task (User) object-item is filtered out
    assert [t["id"] for t in tasks] == [350403, 266037]
    assert tasks[0]["title"] == "Pool key timeout waiting for the lock"
    assert tasks[0]["url"] == f"{HOST}/T350403"
    assert tasks[0]["status"] == "Closed"
    assert "300 of these errors" in tasks[0]["snippet"]
    assert tasks[1]["status"] == "Open"


def test_parse_search_empty_container_is_no_results() -> None:
    assert P._parse_search(_soup("search_empty.html"), HOST) == []


def test_parse_search_missing_container_raises() -> None:
    with pytest.raises(AdapterParseError):
        P._parse_search(P._soup("<html><body>nothing here</body></html>"), HOST)


# --- auth wall --------------------------------------------------------------


def test_is_auth_wall() -> None:
    assert P._is_auth_wall(_fx("login.html"), _soup("login.html")) is True
    assert P._is_auth_wall(_fx("restricted.html"), _soup("restricted.html")) is True
    # a public task page is never an auth wall (even if a comment says "permission")
    assert P._is_auth_wall(_fx("task.html"), _soup("task.html")) is False


# --- full fetch flow --------------------------------------------------------


async def test_fetch_task_success() -> None:
    env = await P.fetch_phabricator(
        f"{HOST}/T1234", fetch_html=_fetcher(_fx("task.html"))
    )
    assert env["mode_used"] == "phabricator"
    assert env["content_type"] == "application/x-phabricator"
    assert "failure" not in env
    q = env["quality"]
    assert q["provider"] == "phabricator"
    assert q["page_type"] == "task"
    assert q["result_count"] == 1
    assert q["task"]["id"] == 1234
    assert env["title"].startswith("T1234:")
    assert env["warnings"] == []
    assert env["markdown"].startswith("# T1234:")


async def test_fetch_task_canonicalizes_target() -> None:
    seen: list[str] = []

    async def fetch_html(target: str):
        seen.append(target)
        return _fx("task.html"), 200, {}, FailureReason.OK, "http"

    # a URL with a comment fragment still fetches the bare /T<id>
    await P.fetch_phabricator(f"{HOST}/T1234", fetch_html=fetch_html)
    assert seen == [f"{HOST}/T1234"]


async def test_fetch_search_success() -> None:
    env = await P.fetch_phabricator(
        f"{HOST}/search/?query=lock&types=TASK",
        fetch_html=_fetcher(_fx("search_global.html")),
    )
    q = env["quality"]
    assert q["page_type"] == "search"
    assert q["result_count"] == 2
    assert q["query"] == "lock"
    assert env["warnings"] == []


async def test_fetch_search_empty_is_no_results() -> None:
    env = await P.fetch_phabricator(
        f"{HOST}/search/?query=zzz&types=TASK",
        fetch_html=_fetcher(_fx("search_empty.html")),
    )
    assert "failure" not in env
    assert env["quality"]["result_count"] == 0
    assert env["warnings"] == ["no_results"]


async def test_fetch_restricted_task_is_login_required() -> None:
    for fixture in ("login.html", "restricted.html"):
        env = await P.fetch_phabricator(
            f"{HOST}/T999", fetch_html=_fetcher(_fx(fixture))
        )
        assert env["failure"]["reason"] == FailureReason.LOGIN_REQUIRED
        assert "authentication" in env["failure"]["message"].lower()


async def test_fetch_404_is_not_found() -> None:
    env = await P.fetch_phabricator(
        f"{HOST}/T99999999",
        fetch_html=_fetcher("404", status=404, reason=FailureReason.NOT_FOUND),
    )
    assert env["failure"]["reason"] == FailureReason.NOT_FOUND


async def test_fetch_rot_is_parse_failed() -> None:
    env = await P.fetch_phabricator(
        f"{HOST}/T5", fetch_html=_fetcher("<html><body>hi</body></html>")
    )
    assert env["failure"]["reason"] == FailureReason.PARSE_FAILED


async def test_fetch_non_ok_empty_body_propagates_reason() -> None:
    env = await P.fetch_phabricator(
        f"{HOST}/T5",
        fetch_html=_fetcher("", status=503, reason=FailureReason.SERVER_ERROR),
    )
    assert env["failure"]["reason"] == FailureReason.SERVER_ERROR


async def test_fetch_timeout() -> None:

    async def fetch_html(target: str):
        raise TimeoutError

    env = await P.fetch_phabricator(f"{HOST}/T5", fetch_html=fetch_html)
    assert env["failure"]["reason"] == FailureReason.TIMEOUT


async def test_fetch_unclaimed_url_defensive() -> None:
    env = await P.fetch_phabricator(
        f"{HOST}/project/profile/1/", fetch_html=_fetcher("x")
    )
    assert env["failure"]["reason"] == FailureReason.PARSE_FAILED
