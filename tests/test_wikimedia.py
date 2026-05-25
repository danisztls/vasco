from __future__ import annotations

import pytest

from vasco import wikimedia
from vasco.errors import FailureReason


# ---------------------------------------------------------------------------
# URL detection + article info parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, matches",
    [
        ("https://en.wikipedia.org/wiki/Python_(programming_language)", True),
        ("https://en.m.wikipedia.org/wiki/Python_(programming_language)", True),
        ("https://de.wikipedia.org/wiki/Berlin", True),
        ("https://fr.m.wikipedia.org/wiki/Paris", True),
        ("https://pt.wikipedia.org/wiki/Brasil", True),
        ("https://ja.wikipedia.org/wiki/%E6%97%A5%E6%9C%AC", True),
        ("https://en.wikipedia.org/wiki/Main_Page", True),
        # Not article URLs:
        ("https://en.wikipedia.org/w/index.php?title=Foo", False),
        ("https://commons.wikimedia.org/wiki/File:Foo.jpg", False),
        ("https://en.wiktionary.org/wiki/hello", False),
        ("https://example.com/wiki/Foo", False),
        ("", False),
    ],
)
def test_is_wikipedia_url(url: str, matches: bool) -> None:
    assert wikimedia.is_wikipedia_url(url) is matches


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://en.wikipedia.org/wiki/Python_(programming_language)",
            ("en", "Python_(programming_language)"),
        ),
        (
            "https://en.m.wikipedia.org/wiki/Python_(programming_language)",
            ("en", "Python_(programming_language)"),
        ),
        ("https://de.wikipedia.org/wiki/Berlin", ("de", "Berlin")),
        ("https://ja.wikipedia.org/wiki/%E6%97%A5%E6%9C%AC", ("ja", "日本")),
        ("https://en.wikipedia.org/wiki/New%20York%20City", ("en", "New_York_City")),
        ("https://en.wikipedia.org/wiki/Foo#section?bar=1", ("en", "Foo")),
        ("https://example.com/foo", None),
        ("", None),
    ],
)
def test_extract_article_info(url: str, expected: tuple[str, str] | None) -> None:
    assert wikimedia.extract_article_info(url) == expected


# ---------------------------------------------------------------------------
# has_credentials
# ---------------------------------------------------------------------------


def test_has_credentials_false_without_config() -> None:
    assert wikimedia.has_credentials(None) is False


def test_has_credentials_true(monkeypatch: pytest.MonkeyPatch) -> None:
    from vasco.config import Config, WikimediaCfg

    cfg = Config(wikimedia=WikimediaCfg(username="user", password="pass"))
    assert wikimedia.has_credentials(cfg) is True


# ---------------------------------------------------------------------------
# Structured Contents → markdown
# ---------------------------------------------------------------------------

_SAMPLE_STRUCTURED = {
    "name": "Python (programming language)",
    "abstract": "Python is a high-level programming language.",
    "description": "General-purpose programming language",
    "date_created": "2001-01-15",
    "date_modified": "2026-05-20T10:00:00Z",
    "in_language": {"identifier": "en", "name": "English"},
    "main_entity": {"identifier": "Q28865"},
    "sections": [
        {
            "name": "History",
            "type": "section",
            "has_parts": [
                {
                    "type": "paragraph",
                    "value": "Python was conceived in the late 1980s.",
                },
            ],
        },
        {
            "name": "Design philosophy",
            "type": "section",
            "has_parts": [
                {
                    "type": "paragraph",
                    "value": "Python emphasizes code readability.",
                },
                {
                    "type": "list",
                    "values": ["Simple is better", "Explicit is better"],
                },
            ],
        },
    ],
    "infoboxes": [
        {
            "name": "Infobox programming language",
            "has_parts": [
                {"name": "Paradigm", "type": "field", "value": "Multi-paradigm"},
            ],
        }
    ],
    "version": {
        "maintenance_tags": {"citation_needed_count": 5},
        "scores": {"revertrisk": {"prediction": False}},
    },
}


def test_structured_to_fields() -> None:
    markdown, meta = wikimedia._structured_to_fields(_SAMPLE_STRUCTURED)
    assert "Python is a high-level programming language." in markdown
    assert "## History" in markdown
    assert "Python was conceived in the late 1980s." in markdown
    assert "## Design philosophy" in markdown
    assert "- Simple is better" in markdown
    assert meta["title"] == "Python (programming language)"
    assert meta["language"] == "en"
    assert meta["modified"] == "2026-05-20T10:00:00Z"
    assert meta["quality"]["description"] == "General-purpose programming language"
    assert meta["quality"]["infoboxes"] is not None
    assert meta["quality"]["maintenance_tags"]["citation_needed_count"] == 5
    assert meta["quality"]["wikidata"] == "Q28865"
    assert meta["quality"]["scores"]["revertrisk"]["prediction"] is False


# ---------------------------------------------------------------------------
# Standard articles → markdown
# ---------------------------------------------------------------------------

_SAMPLE_STANDARD = {
    "name": "日本",
    "abstract": "Japan is an island country in East Asia.",
    "date_modified": "2026-05-19T08:00:00Z",
    "in_language": {"identifier": "ja", "name": "Japanese"},
    "main_entity": {"identifier": "Q17"},
    "article_body": {
        "html": "<html><body><p>Japan is an island country.</p></body></html>",
    },
    "version": {
        "maintenance_tags": {"citation_needed_count": 2},
    },
}


def test_standard_to_fields() -> None:
    markdown, meta = wikimedia._standard_to_fields(_SAMPLE_STANDARD)
    assert len(markdown) > 0
    assert meta["title"] == "日本"
    assert meta["language"] == "ja"
    assert meta["quality"]["wikidata"] == "Q17"


def test_standard_to_fields_no_html() -> None:
    article = {**_SAMPLE_STANDARD, "article_body": {}}
    markdown, meta = wikimedia._standard_to_fields(article)
    assert markdown == "Japan is an island country in East Asia."


# ---------------------------------------------------------------------------
# fetch_wikipedia — end-to-end with mocked Enterprise
# ---------------------------------------------------------------------------


def _patch_enterprise(
    monkeypatch: pytest.MonkeyPatch,
    *,
    structured: dict | None = _SAMPLE_STRUCTURED,
    standard: dict | None = _SAMPLE_STANDARD,
) -> None:
    async def fake_token(cfg, deadline_monotonic):
        return "fake-jwt-token"

    async def fake_request(
        endpoint, title, project, token, *, deadline_monotonic
    ) -> dict | None:
        if endpoint == "structured-contents":
            return structured
        return standard

    monkeypatch.setattr(wikimedia, "_ensure_token", fake_token)
    monkeypatch.setattr(wikimedia, "_enterprise_request", fake_request)


@pytest.mark.asyncio
async def test_fetch_structured_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_enterprise(monkeypatch)
    env = await wikimedia.fetch_wikipedia(
        "https://en.wikipedia.org/wiki/Python_(programming_language)",
        deadline=10.0,
    )
    assert "failure" not in env
    assert env["mode_used"] == "wikipedia"
    assert env["content_type"] == "text/wikipedia"
    assert "Python is a high-level" in env["markdown"]
    assert "## History" in env["markdown"]
    assert env["quality"].get("infoboxes") is not None


@pytest.mark.asyncio
async def test_fetch_standard_for_non_beta_lang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_enterprise(monkeypatch)
    env = await wikimedia.fetch_wikipedia(
        "https://ja.wikipedia.org/wiki/%E6%97%A5%E6%9C%AC",
        deadline=10.0,
    )
    assert "failure" not in env
    assert env["title"] == "日本"
    assert env["language"] == "ja"


@pytest.mark.asyncio
async def test_structured_fallback_to_standard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Structured Contents returns None, fall back to standard articles."""
    _patch_enterprise(monkeypatch, structured=None)
    env = await wikimedia.fetch_wikipedia(
        "https://en.wikipedia.org/wiki/Python_(programming_language)",
        deadline=10.0,
    )
    assert "failure" not in env
    assert env["title"] == "日本"  # standard sample kicks in


@pytest.mark.asyncio
async def test_fetch_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    wikimedia._reset_token_for_tests()

    async def fake_token(cfg, deadline_monotonic):
        return None

    monkeypatch.setattr(wikimedia, "_ensure_token", fake_token)
    env = await wikimedia.fetch_wikipedia(
        "https://en.wikipedia.org/wiki/Foo", deadline=10.0
    )
    assert env["failure"]["reason"] == FailureReason.LOGIN_REQUIRED.value


@pytest.mark.asyncio
async def test_fetch_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_enterprise(monkeypatch, structured=None, standard=None)
    env = await wikimedia.fetch_wikipedia(
        "https://en.wikipedia.org/wiki/Nonexistent_Article_12345",
        deadline=10.0,
    )
    assert env["failure"]["reason"] == FailureReason.NOT_FOUND.value


@pytest.mark.asyncio
async def test_fetch_invalid_url() -> None:
    wikimedia._reset_token_for_tests()
    env = await wikimedia.fetch_wikipedia("https://example.com/foo")
    assert env["failure"]["reason"] == FailureReason.INVALID_URL.value


@pytest.mark.asyncio
async def test_fetch_mobile_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_enterprise(monkeypatch)
    env = await wikimedia.fetch_wikipedia(
        "https://en.m.wikipedia.org/wiki/Python_(programming_language)",
        deadline=10.0,
    )
    assert "failure" not in env


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


def test_reset_token_for_tests() -> None:
    wikimedia._access_token = "old"
    wikimedia._token_expires_at = 999.0
    wikimedia._reset_token_for_tests()
    assert wikimedia._access_token is None
    assert wikimedia._token_expires_at == 0.0


# ---------------------------------------------------------------------------
# Nested sections render correctly
# ---------------------------------------------------------------------------


def test_render_section_nested() -> None:
    section = {
        "name": "Outer",
        "type": "section",
        "has_parts": [
            {"type": "paragraph", "value": "Intro text."},
            {
                "name": "Inner",
                "type": "section",
                "has_parts": [
                    {"type": "paragraph", "value": "Nested content."},
                ],
            },
        ],
    }
    parts: list[str] = []
    wikimedia._render_section(section, parts, depth=2)
    text = "\n".join(parts)
    assert "## Outer" in text
    assert "### Inner" in text
    assert "Nested content." in text
