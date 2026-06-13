from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from vasco.adapters import gitlab as G
from vasco.config import AdaptersCfg, Config, GitLabCfg
from vasco.errors import AdapterParseError, FailureReason

FX = Path(__file__).parent / "fixtures" / "gitlab"

GL = "https://gitlab.com"
SELF = "https://gitlab.wikimedia.org"
PROJECT = "egardner/mcp-phabricator"


def _fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_memo() -> None:
    """The probe memo is process-lifetime — clear between tests."""
    G._reset_for_tests()
    yield
    G._reset_for_tests()


def _getter(routes: dict[str, tuple[str, int, FailureReason]]):
    """Build an injected API getter dispatching by endpoint substring. `routes`
    maps a substring → (body, status, reason); the first match wins."""

    async def _get(target: str):
        for needle, payload in routes.items():
            if needle in target:
                return payload
        raise AssertionError(f"unexpected target {target}")

    return _get


# --- routing / claim --------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (f"{GL}/gitlab-org/gitlab", ("project", "gitlab-org/gitlab", None)),
        (f"{GL}/group/sub/project", ("project", "group/sub/project", None)),
        (f"{GL}/gitlab-org/gitlab/-/issues/123", ("issue", "gitlab-org/gitlab", "123")),
        (
            f"{GL}/gitlab-org/gitlab/-/merge_requests/45",
            ("merge_request", "gitlab-org/gitlab", "45"),
        ),
        (f"{SELF}/{PROJECT}", ("project", PROJECT, None)),
        (f"{GL}/gitlab-org/gitlab/-/tree/main/src", None),
        (f"{GL}/gitlab-org/gitlab/-/issues/notanid", None),
        (f"{GL}/explore", None),
        (f"{GL}/users/foo", None),
        (f"{GL}/gitlab-org", None),  # single segment = group/user, not a project
        (f"{GL}/", None),
        ("ftp://gitlab.com/a/b", None),
        ("", None),
    ],
)
def test_claim(url: str, expected) -> None:
    assert G._claim(url) == expected


def test_is_gitlab_url_known_vs_unknown() -> None:
    cfg = Config()
    # gitlab.com is a built-in known host.
    assert G.is_gitlab_url(f"{GL}/a/b", cfg) is True
    # A self-hosted host is not known without config/probe → not certain, but it
    # *is* a probe candidate.
    assert G.is_gitlab_url(f"{SELF}/{PROJECT}", cfg) is False
    assert G.is_gitlab_candidate(f"{SELF}/{PROJECT}", cfg) is True
    # A known host is never a *candidate* (it's served directly).
    assert G.is_gitlab_candidate(f"{GL}/a/b", cfg) is False


def test_config_domains_make_host_known() -> None:
    cfg = Config(
        adapters=AdaptersCfg(gitlab=GitLabCfg(domains=("gitlab.wikimedia.org",)))
    )
    assert G.is_gitlab_url(f"{SELF}/{PROJECT}", cfg) is True
    assert G.is_gitlab_candidate(f"{SELF}/{PROJECT}", cfg) is False


def test_autodetect_off_disables_candidate() -> None:
    cfg = Config(adapters=AdaptersCfg(gitlab=GitLabCfg(autodetect=False)))
    assert G.is_gitlab_candidate(f"{SELF}/{PROJECT}", cfg) is False


def test_bare_project_probe_gated_by_forge_hint() -> None:
    cfg = Config()
    # A plain host's bare-project URL is NOT a probe candidate (would otherwise
    # spray /api/v4 at the whole web); forge-hinted hosts are.
    assert G.is_gitlab_candidate("https://news.example/world/story", cfg) is False
    assert G.is_gitlab_candidate("https://git.example/group/repo", cfg) is True
    assert G.is_gitlab_candidate("https://code.videolan.org/v/vlc", cfg) is True
    # But an issue/MR URL (GitLab-distinctive /-/ marker) is a candidate on ANY
    # host, hint or not.
    assert (
        G.is_gitlab_candidate("https://news.example/team/app/-/issues/5", cfg) is True
    )


def test_forge_hint_matches_labels_not_substrings() -> None:
    assert G._forge_hint("gitlab.wikimedia.org") is True
    assert G._forge_hint("git.kernel.org") is True
    assert G._forge_hint("code.qt.io") is True
    # substring-only matches must not fire ("git" inside "digital", "code" in "barcode")
    assert G._forge_hint("digital.example.com") is False
    assert G._forge_hint("barcode.example.com") is False


# --- parsers ----------------------------------------------------------------


def test_parse_project_fixture() -> None:
    p = G._parse_project(json.loads(_fx("project.json")))
    assert p["path_with_namespace"] == PROJECT
    assert p["name"] == "mcp-phabricator"
    assert p["star_count"] == 11
    assert p["forks_count"] == 4
    assert p["open_issues_count"] == 3
    assert p["topics"] == ["mcp", "phabricator"]
    assert p["license"] == "MIT License"
    assert p["default_branch"] == "main"
    assert p["readme_url"].endswith("/-/blob/main/README.md")


def test_parse_issue_fixture() -> None:
    i = G._parse_issue(json.loads(_fx("issue.json")))
    assert i["iid"] == 123
    assert i["title"].startswith("500 error")
    assert i["state"] == "closed"
    assert i["author"] == "jacobvosmaer"
    assert i["labels"] == ["backend", "bug"]
    assert i["user_notes_count"] == 35


def test_parse_mr_fixture() -> None:
    m = G._parse_mr(json.loads(_fx("merge_request.json")))
    assert m["iid"] == 45
    assert m["state"] == "merged"
    assert m["source_branch"] == "ldap-nested-groups"
    assert m["target_branch"] == "master"
    assert m["merge_status"] == "can_be_merged"
    assert m["draft"] is False


def test_parse_project_missing_anchor_raises() -> None:
    with pytest.raises(AdapterParseError):
        G._parse_project({"message": "404 Project Not Found"})
    with pytest.raises(AdapterParseError):
        G._parse_project([1, 2, 3])


def test_parse_thread_missing_anchor_raises() -> None:
    with pytest.raises(AdapterParseError):
        G._parse_issue({"message": "404 Not found"})
    with pytest.raises(AdapterParseError):
        G._parse_mr({"iid": 5})  # no title


def test_parse_notes_filters_system_and_caps() -> None:
    result = (_fx("issue_notes.json"), 200, FailureReason.OK)
    notes = G._parse_notes(result, limit=20)
    assert len(notes) == 2  # the system note is dropped
    assert notes[0]["author"] == "dzaporozhets"
    assert notes[1]["body"].startswith("Fixed in")
    # cap is honored
    assert len(G._parse_notes(result, limit=1)) == 1


def test_parse_notes_non_array_is_empty() -> None:
    # Some instances gate the notes API anonymously → 401 {"message": "..."}.
    result = ('{"message": "401 Unauthorized"}', 401, FailureReason.OK)
    assert G._parse_notes(result, limit=20) == []
    # a raised exception (from gather) is also tolerated
    assert G._parse_notes(RuntimeError("boom"), limit=20) == []


# --- fetch: project ---------------------------------------------------------


def test_fetch_project_success_with_readme() -> None:
    fetch = _getter(
        {
            "/api/v4/projects/": (_fx("project.json"), 200, FailureReason.OK),
            "/-/raw/main/README.md": (
                "# mcp-phabricator\n\nHello.",
                200,
                FailureReason.OK,
            ),
        }
    )
    env = asyncio.run(G.fetch_gitlab(f"{SELF}/{PROJECT}", _get=fetch, cfg=Config()))
    assert env["mode_used"] == "gitlab"
    assert "failure" not in env
    q = env["quality"]
    assert q["provider"] == "gitlab"
    assert q["page_type"] == "project"
    assert q["result_count"] == 1
    assert q["project"]["star_count"] == 11
    assert "## README" in env["markdown"]
    assert "Hello." in env["markdown"]


def test_fetch_project_readme_failure_is_tolerated() -> None:
    fetch = _getter(
        {
            "/api/v4/projects/": (_fx("project.json"), 200, FailureReason.OK),
            "/-/raw/": ("", 500, FailureReason.SERVER_ERROR),
        }
    )
    env = asyncio.run(G.fetch_gitlab(f"{SELF}/{PROJECT}", _get=fetch, cfg=Config()))
    assert "failure" not in env
    assert "## README" not in env["markdown"]
    assert env["quality"]["project"]["name"] == "mcp-phabricator"


# --- fetch: issue / MR ------------------------------------------------------


def test_fetch_issue_success_with_comments() -> None:
    fetch = _getter(
        {
            "/issues/123/notes": (_fx("issue_notes.json"), 200, FailureReason.OK),
            "/issues/123": (_fx("issue.json"), 200, FailureReason.OK),
        }
    )
    url = f"{GL}/gitlab-org/gitlab/-/issues/123"
    env = asyncio.run(G.fetch_gitlab(url, _get=fetch, cfg=Config()))
    assert env["mode_used"] == "gitlab"
    q = env["quality"]
    assert q["page_type"] == "issue"
    assert q["issue"]["iid"] == 123
    assert len(q["comments"]) == 2
    assert "## Comments (2)" in env["markdown"]


def test_fetch_mr_success() -> None:
    fetch = _getter(
        {
            "/merge_requests/45/notes": (_fx("mr_notes.json"), 200, FailureReason.OK),
            "/merge_requests/45": (_fx("merge_request.json"), 200, FailureReason.OK),
        }
    )
    url = f"{GL}/gitlab-org/gitlab/-/merge_requests/45"
    env = asyncio.run(G.fetch_gitlab(url, _get=fetch, cfg=Config()))
    q = env["quality"]
    assert q["page_type"] == "merge_request"
    assert q["merge_request"]["target_branch"] == "master"
    assert q["merge_request"]["state"] == "merged"
    assert "ldap-nested-groups → master" in env["markdown"]


# --- failure mapping (known host) -------------------------------------------


def test_known_host_404_is_not_found() -> None:
    fetch = _getter(
        {
            "/api/v4/projects/": (
                '{"message": "404 Project Not Found"}',
                404,
                FailureReason.OK,
            )
        }
    )
    env = asyncio.run(G.fetch_gitlab(f"{GL}/no/such-project", _get=fetch, cfg=Config()))
    assert env["failure"]["reason"] == FailureReason.NOT_FOUND


def test_known_host_non_json_is_parse_failed() -> None:
    fetch = _getter(
        {"/api/v4/projects/": ("<html>blocked</html>", 200, FailureReason.OK)}
    )
    env = asyncio.run(G.fetch_gitlab(f"{GL}/a/b", _get=fetch, cfg=Config()))
    assert env["failure"]["reason"] == FailureReason.PARSE_FAILED


def test_known_host_propagates_fetch_failure() -> None:
    # _api_get maps a transport error to ("", 0, SERVER_ERROR/TIMEOUT).
    fetch = _getter({"/api/v4/projects/": ("", 0, FailureReason.SERVER_ERROR)})
    env = asyncio.run(G.fetch_gitlab(f"{GL}/a/b", _get=fetch, cfg=Config()))
    assert env["failure"]["reason"] == FailureReason.SERVER_ERROR


def test_unclaimable_url_is_parse_failed() -> None:
    env = asyncio.run(G.fetch_gitlab(f"{GL}/explore", _get=_getter({})))
    assert env["failure"]["reason"] == FailureReason.PARSE_FAILED


# --- probe semantics (unknown host) -----------------------------------------


def test_probe_confirms_and_memoizes_gitlab() -> None:
    from vasco.cache import Cache

    fetch = _getter(
        {
            "/api/v4/projects/": (_fx("project.json"), 200, FailureReason.OK),
            "/-/raw/": ("# readme", 200, FailureReason.OK),
        }
    )
    cache = Cache(":memory:")
    try:
        env = asyncio.run(
            G.fetch_gitlab(
                f"{SELF}/{PROJECT}",
                _get=fetch,
                cfg=Config(),
                cache=cache,
                probe=True,
            )
        )
        assert env["mode_used"] == "gitlab"
        assert cache.get_probe("gitlab", "gitlab.wikimedia.org") is True
        # Now the host is certain → no longer a candidate.
        assert G.is_gitlab_url(f"{SELF}/other/repo", cache=cache) is True
    finally:
        cache.close()


def test_probe_non_json_memoizes_false_and_raises_notgitlab() -> None:
    from vasco.cache import Cache

    fetch = _getter(
        {"/api/v4/projects/": ("<html>not gitlab</html>", 200, FailureReason.OK)}
    )
    cache = Cache(":memory:")
    try:
        with pytest.raises(G.NotGitLab):
            asyncio.run(
                G.fetch_gitlab(
                    "https://example.com/foo/bar",
                    _get=fetch,
                    cfg=Config(),
                    cache=cache,
                    probe=True,
                )
            )
        assert cache.get_probe("gitlab", "example.com") is False
    finally:
        cache.close()


def test_probe_404_message_is_ambiguous_no_memo() -> None:
    from vasco.cache import Cache

    fetch = _getter(
        {
            "/api/v4/projects/": (
                '{"message": "404 Project Not Found"}',
                404,
                FailureReason.OK,
            )
        }
    )
    cache = Cache(":memory:")
    try:
        with pytest.raises(G.NotGitLab):
            asyncio.run(
                G.fetch_gitlab(
                    "https://maybe.example/foo/bar",
                    _get=fetch,
                    cfg=Config(),
                    cache=cache,
                    probe=True,
                )
            )
        # ambiguous JSON → no verdict recorded (re-probe later)
        assert cache.get_probe("gitlab", "maybe.example") is None
    finally:
        cache.close()
