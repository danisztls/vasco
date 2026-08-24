from __future__ import annotations

import pytest

from vasco.adapters import wikimedia
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
        ("https://ja.wikipedia.org/wiki/%E6%97%A5%E6%9C%AC", True),
        ("https://en.wiktionary.org/wiki/hello", True),
        ("https://en.m.wiktionary.org/wiki/hello", True),
        ("https://fr.wikisource.org/wiki/Les_Fleurs_du_mal", True),
        ("https://en.wikiquote.org/wiki/Albert_Einstein", True),
        ("https://en.wikibooks.org/wiki/Haskell", True),
        ("https://en.wikivoyage.org/wiki/Paris", True),
        ("https://en.wikiversity.org/wiki/Topic:Physics", True),
        ("https://en.wikinews.org/wiki/Main_Page", True),
        ("https://simple.wikipedia.org/wiki/Dog", True),
        ("https://simple.m.wikipedia.org/wiki/Dog", True),
        ("https://zh-min-nan.wikipedia.org/wiki/T东西", True),
        ("https://be-tarask.wikipedia.org/wiki/Минск", True),
        # Plain index.php?title= article views are matched (folded to /wiki/).
        ("https://en.wikipedia.org/w/index.php?title=Foo", True),
        ("https://en.wikipedia.org/w/index.php?title=Foo&redirect=no", True),
        # Non-article index.php variants stay unmatched (HTTP fallback):
        ("https://en.wikipedia.org/w/index.php?title=Foo&action=edit", False),
        ("https://en.wikipedia.org/w/index.php?title=Foo&action=history", False),
        ("https://en.wikipedia.org/w/index.php?title=Foo&oldid=12345", False),
        ("https://en.wikipedia.org/w/index.php?title=Foo&diff=prev&oldid=9", False),
        ("https://en.wikipedia.org/w/index.php?curid=12345", False),
        ("https://en.wikipedia.org/w/index.php", False),  # no title
        # Not matched:
        ("https://commons.wikimedia.org/wiki/File:Foo.jpg", False),
        ("https://www.wikidata.org/wiki/Q42", False),
        ("https://example.com/wiki/Foo", False),
        ("", False),
    ],
)
def test_is_wikimedia_url(url: str, matches: bool) -> None:
    assert wikimedia.is_wikimedia_url(url) is matches


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://en.wikipedia.org/wiki/Python_(programming_language)",
            ("en", "wikipedia", "Python_(programming_language)"),
        ),
        (
            "https://en.m.wikipedia.org/wiki/Python_(programming_language)",
            ("en", "wikipedia", "Python_(programming_language)"),
        ),
        (
            "https://en.wiktionary.org/wiki/hello",
            ("en", "wiktionary", "hello"),
        ),
        (
            "https://simple.wikipedia.org/wiki/Dog",
            ("simple", "wikipedia", "Dog"),
        ),
        (
            "https://zh-min-nan.wikipedia.org/wiki/Tang",
            ("zh-min-nan", "wikipedia", "Tang"),
        ),
        (
            "https://en.wikipedia.org/w/index.php?title=New_York_City",
            ("en", "wikipedia", "New_York_City"),
        ),
        (
            "https://en.wikipedia.org/w/index.php?title=New%20York&redirect=no",
            ("en", "wikipedia", "New_York"),
        ),
        # action=edit disqualifies → not an article URL.
        ("https://en.wikipedia.org/w/index.php?title=Foo&action=edit", None),
        (
            "https://fr.wikisource.org/wiki/Les%20Fleurs",
            ("fr", "wikisource", "Les_Fleurs"),
        ),
        ("https://example.com/foo", None),
        ("", None),
    ],
)
def test_extract_article_info(url: str, expected: tuple[str, str, str] | None) -> None:
    assert wikimedia.extract_article_info(url) == expected


# ---------------------------------------------------------------------------
# Enterprise project ID mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lang, project, expected",
    [
        ("en", "wikipedia", "enwiki"),
        ("de", "wikipedia", "dewiki"),
        ("en", "wiktionary", "enwiktionary"),
        ("fr", "wikisource", "frwikisource"),
        ("en", "wikibooks", "enwikibooks"),
        ("simple", "wikipedia", "simplewiki"),
    ],
)
def test_enterprise_project_id(lang: str, project: str, expected: str) -> None:
    assert wikimedia._enterprise_project_id(lang, project) == expected


# ---------------------------------------------------------------------------
# has_credentials
# ---------------------------------------------------------------------------


def test_has_credentials_false_without_config() -> None:
    assert wikimedia.has_credentials(None) is False


def test_has_credentials_true() -> None:
    from vasco.config import AdaptersCfg, Config, WikimediaCfg

    cfg = Config(
        adapters=AdaptersCfg(wikimedia=WikimediaCfg(username="user", password="pass"))
    )
    assert wikimedia.has_credentials(cfg) is True


# ---------------------------------------------------------------------------
# CJK-aware word counting
# ---------------------------------------------------------------------------


def test_word_count_english() -> None:
    assert wikimedia._word_count("hello world foo bar") == 4


def test_word_count_japanese() -> None:
    assert wikimedia._word_count("日本は島国である") == 8


def test_word_count_mixed() -> None:
    # 4 CJK chars (each counted as a word) + 3 space-delimited words
    assert wikimedia._word_count("日本 is a country 島国") == 7


def test_word_count_empty() -> None:
    assert wikimedia._word_count("") == 0


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------


def test_render_table() -> None:
    table = {
        "identifier": "t1",
        "headers": [[{"value": "Country"}, {"value": "GDP"}]],
        "rows": [
            [{"value": "US"}, {"value": "25T"}],
            [{"value": "China"}, {"value": "18T"}],
        ],
    }
    parts: list[str] = []
    wikimedia._render_table(table, parts)
    text = "\n".join(parts)
    assert "| Country | GDP |" in text
    assert "| --- | --- |" in text
    assert "| US | 25T |" in text


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
            "name": "Abstract",
            "type": "section",
            "has_parts": [
                {
                    "type": "paragraph",
                    "value": "Python is a high-level programming language.",
                },
            ],
        },
        {
            "name": "History",
            "type": "section",
            "has_parts": [
                {
                    "type": "paragraph",
                    "value": "Python was conceived in the late 1980s.",
                },
                {
                    "type": "table",
                    "table_references": [{"identifier": "history_table1"}],
                },
            ],
        },
    ],
    "tables": [
        {
            "identifier": "history_table1",
            "headers": [[{"value": "Version"}, {"value": "Year"}]],
            "rows": [[{"value": "1.0"}, {"value": "1994"}]],
        }
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
    assert "## History" in markdown
    assert "| Version | Year |" in markdown
    assert meta["title"] == "Python (programming language)"
    assert meta["quality"]["wikidata"] == "Q28865"


def test_structured_no_lead_duplication() -> None:
    markdown, _ = wikimedia._structured_to_fields(_SAMPLE_STRUCTURED)
    count = markdown.count("Python is a high-level programming language.")
    assert count == 1


def test_structured_no_sections_uses_abstract() -> None:
    article = {**_SAMPLE_STRUCTURED, "sections": []}
    markdown, _ = wikimedia._structured_to_fields(article)
    assert markdown == "Python is a high-level programming language."


# ---------------------------------------------------------------------------
# Standard articles → markdown
# ---------------------------------------------------------------------------

_SAMPLE_STANDARD = {
    "name": "hello",
    "abstract": "Hello is a greeting.",
    "date_modified": "2026-05-19T08:00:00Z",
    "in_language": {"identifier": "en", "name": "English"},
    "main_entity": {"identifier": "Q1066689"},
    "article_body": {
        "html": "<html><body><p>Hello is a salutation or greeting.</p></body></html>",
    },
    "version": {"maintenance_tags": {"citation_needed_count": 1}},
}


def test_standard_to_fields() -> None:
    markdown, meta = wikimedia._standard_to_fields(_SAMPLE_STANDARD)
    assert len(markdown) > 0
    assert meta["title"] == "hello"
    assert meta["quality"]["wikidata"] == "Q1066689"


def test_standard_to_fields_no_html() -> None:
    article = {**_SAMPLE_STANDARD, "article_body": {}}
    markdown, _ = wikimedia._standard_to_fields(article)
    assert markdown == "Hello is a greeting."


# ---------------------------------------------------------------------------
# fetch_wikimedia — end-to-end with mocked Enterprise
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
        endpoint, title, project_id, token, *, deadline_monotonic
    ) -> dict | None:
        if endpoint == "structured-contents":
            return structured
        return standard

    async def fake_redirect(lang, project, title, *, deadline_monotonic):
        return title

    monkeypatch.setattr(wikimedia, "_ensure_token", fake_token)
    monkeypatch.setattr(wikimedia, "_enterprise_request", fake_request)
    monkeypatch.setattr(wikimedia, "_resolve_redirect", fake_redirect)


@pytest.mark.asyncio
async def test_fetch_wikipedia_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_enterprise(monkeypatch)
    env = await wikimedia.fetch_wikimedia(
        "https://en.wikipedia.org/wiki/Python_(programming_language)",
        deadline=10.0,
    )
    assert "failure" not in env
    assert env["mode_used"] == "wikimedia"
    assert env["content_type"] == "text/wikimedia"
    assert env["site_name"] == "Wikipedia"
    assert "## History" in env["markdown"]


@pytest.mark.asyncio
async def test_fetch_wiktionary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_enterprise(monkeypatch)
    env = await wikimedia.fetch_wikimedia(
        "https://en.wiktionary.org/wiki/hello",
        deadline=10.0,
    )
    assert "failure" not in env
    assert env["site_name"] == "Wiktionary"
    assert env["title"] == "hello"


@pytest.mark.asyncio
async def test_fetch_wikisource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_enterprise(monkeypatch)
    env = await wikimedia.fetch_wikimedia(
        "https://fr.wikisource.org/wiki/Les_Fleurs_du_mal",
        deadline=10.0,
    )
    assert "failure" not in env
    assert env["site_name"] == "Wikisource"
    assert env["language"] == "en"  # from _SAMPLE_STANDARD fixture


@pytest.mark.asyncio
async def test_structured_only_for_wikipedia(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-Wikipedia projects skip structured-contents, go straight to standard."""
    endpoints_called: list[str] = []

    async def spy_request(
        endpoint, title, project_id, token, *, deadline_monotonic
    ) -> dict | None:
        endpoints_called.append(endpoint)
        if endpoint == "structured-contents":
            return _SAMPLE_STRUCTURED
        return _SAMPLE_STANDARD

    async def fake_token(cfg, deadline_monotonic):
        return "fake-jwt-token"

    async def fake_redirect(lang, project, title, *, deadline_monotonic):
        return title

    monkeypatch.setattr(wikimedia, "_ensure_token", fake_token)
    monkeypatch.setattr(wikimedia, "_enterprise_request", spy_request)
    monkeypatch.setattr(wikimedia, "_resolve_redirect", fake_redirect)

    env = await wikimedia.fetch_wikimedia(
        "https://en.wiktionary.org/wiki/hello", deadline=10.0
    )
    assert "failure" not in env
    assert "structured-contents" not in endpoints_called
    assert "articles" in endpoints_called


@pytest.mark.asyncio
async def test_fetch_resolves_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved_titles: list[str] = []

    async def fake_redirect(lang, project, title, *, deadline_monotonic):
        return "Color"

    async def fake_token(cfg, deadline_monotonic):
        return "fake-jwt-token"

    async def fake_request(
        endpoint, title, project_id, token, *, deadline_monotonic
    ) -> dict | None:
        resolved_titles.append(title)
        if endpoint == "structured-contents":
            return _SAMPLE_STRUCTURED
        return None

    monkeypatch.setattr(wikimedia, "_resolve_redirect", fake_redirect)
    monkeypatch.setattr(wikimedia, "_ensure_token", fake_token)
    monkeypatch.setattr(wikimedia, "_enterprise_request", fake_request)

    env = await wikimedia.fetch_wikimedia(
        "https://en.wikipedia.org/wiki/Colour", deadline=10.0
    )
    assert "failure" not in env
    assert resolved_titles[0] == "Color"


@pytest.mark.asyncio
async def test_fetch_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    wikimedia._reset_token_for_tests()

    async def fake_token(cfg, deadline_monotonic):
        return None

    monkeypatch.setattr(wikimedia, "_ensure_token", fake_token)
    env = await wikimedia.fetch_wikimedia(
        "https://en.wikipedia.org/wiki/Foo", deadline=10.0
    )
    assert env["failure"]["reason"] == FailureReason.LOGIN_REQUIRED.value


@pytest.mark.asyncio
async def test_fetch_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_enterprise(monkeypatch, structured=None, standard=None)
    env = await wikimedia.fetch_wikimedia(
        "https://en.wikipedia.org/wiki/Nonexistent_12345",
        deadline=10.0,
    )
    assert env["failure"]["reason"] == FailureReason.NOT_FOUND.value


@pytest.mark.asyncio
async def test_fetch_invalid_url() -> None:
    wikimedia._reset_token_for_tests()
    env = await wikimedia.fetch_wikimedia("https://example.com/foo")
    assert env["failure"]["reason"] == FailureReason.INVALID_URL.value


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
# Section rendering
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
    wikimedia._render_section(section, parts, depth=2, tables_by_id={})
    text = "\n".join(parts)
    assert "## Outer" in text
    assert "### Inner" in text
    assert "Nested content." in text
