# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Scholar adapter — scientific articles via open scholarly APIs, no Cloudflare.

Publisher article pages (ScienceDirect/Elsevier being the motivating case) are
hard Cloudflare-gated: a plain fetch of ``www.sciencedirect.com`` returns a 403
challenge, and even the DOI meta-tag is behind the wall. But the paper's
*metadata and open-access copies* live behind clean, unauthenticated JSON APIs
that Cloudflare never touches. So this adapter never scrapes the gated frontend:
it resolves the paper to a **DOI** and fans out across the open ecosystem,
merging the results into one normalized record.

Like :mod:`vasco.adapters.gitlab` / :mod:`vasco.adapters.itad` (and unlike the
marketplace adapters that ride the injected ``fetch_html``), it fetches through
its **own minimal-header httpx client** — the APIs are plain GETs that gain
nothing from the browser tier and would only be slowed by the escalation chain.

Entry URLs it claims (a tight, deterministic host set — no probe):

- ``doi.org`` / ``dx.doi.org`` → the DOI directly.
- ``sciencedirect.com`` ``/pii/<PII>`` and ``linkinghub.elsevier.com`` → the
  Elsevier **PII**, resolved to a DOI *keyless* via Crossref's ``alternative-id``
  filter (the SD page's own DOI is Cloudflare-walled; Crossref indexes the PII).
- ``pubmed.ncbi.nlm.nih.gov/<pmid>`` → PMID (→ DOI via OpenAlex).
- ``ncbi.nlm.nih.gov`` / ``europepmc.org`` ``…/PMC<id>`` → PMCID (→ DOI via
  Europe PMC).
- ``arxiv.org/abs/<id>`` → the deterministic arXiv DataCite DOI.

Sources (all keyless except the opt-in Elsevier/S2 keys), each authoritative for
a slice; the assembler picks per-field by precedence:

- **Crossref** — bibliographic spine (title, journal, authors, dates, license,
  reference/citation counts) + the PII↔DOI resolver.
- **OpenAlex** — all OA locations, ``is_retracted``, topics/MeSH, citations, the
  ID crosswalk, and a reconstructable abstract.
- **Unpaywall** — canonical "is there a legal free PDF and where" (``oa_status``,
  ``best_oa_location``). Needs an email; skipped when unset (→ OpenAlex OA).
- **Semantic Scholar** — the abstract (covers the gap where Elsevier deposits
  none to Crossref/OpenAlex) + a one-line TLDR.
- **Europe PMC** — biomedical: the actual full-text body (a render-PDF URL) when
  a PMCID exists.

When ``adapters.scholar.fetch_full_text`` is on and a clean OA copy exists, the
chosen OA PDF is downloaded + converted into the envelope markdown (reusing the
core fetch's PDF path); otherwise the envelope carries metadata + abstract + a
``full_text_url`` link.

Failure contract (per the project invariants): an unrecognized URL shape →
``PARSE_FAILED`` (defensive — dispatch only calls us on a claimed URL); an
identifier that resolves to no known work anywhere → ``NOT_FOUND``; a resolved
paper with no OA copy → ``success`` + a ``paywalled`` warning (the metadata is
still useful, mirroring the AliExpress walled-PDP exception). The adapter never
raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .. import envelope
from ..errors import FailureReason
from . import _common
from ._common import compact as _compact

log = logging.getLogger(__name__)

_PROVIDER = "scholar"
_SITE_NAME = "Scholar"
_CONTENT_TYPE = "text/scholar"

_CROSSREF = "https://api.crossref.org"
_OPENALEX = "https://api.openalex.org"
_UNPAYWALL = "https://api.unpaywall.org/v2"
_S2 = "https://api.semanticscholar.org/graph/v1"
_EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"

# An honest API-client UA (see gitlab): a plain client identifier, not a spoofed
# browser. The polite pools (Crossref/OpenAlex) also read the mailto from the
# request; we pass it as a query param where each API supports it.
_USER_AGENT = "vasco/0.1 (scholar; https://github.com/; mailto:{email})"

_DEFAULT_MAX_AUTHORS = 30
_FULL_TEXT_MAX_CHARS = 60_000

# Semantic Scholar field selector — everything the assembler reads from S2.
_S2_FIELDS = "title,abstract,tldr,year,venue,externalIds,openAccessPdf,publicationTypes"

# A DOI: `10.` + registrant + `/` + suffix. Suffix chars per the DOI spec are
# broad; we stop at whitespace / the usual URL delimiters and trim trailing
# punctuation in `_norm_doi`.
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)
# An Elsevier PII as it appears in a ScienceDirect/linkinghub path: `S` or `B`
# followed by the identifier body (digits / X / hyphens).
_PII_RE = re.compile(r"^[SB][0-9X-]{9,}$", re.IGNORECASE)
_JATS_TAG_RE = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------------------
# URL detection (pure)
# ---------------------------------------------------------------------------


def _segments(url: str) -> list[str]:
    return [s for s in (urlsplit(url).path or "").split("/") if s]


def _claim(url: str) -> tuple[str, str] | None:
    """Map a scholarly URL to ``(kind, ident)`` or ``None`` if unclaimable.

    ``kind`` ∈ {``doi``, ``pii``, ``pmid``, ``pmcid``, ``arxiv``}; ``ident`` is
    the raw identifier. The host set is closed and deterministic — no probe, no
    over-claim of arbitrary URLs that merely mention a DOI.
    """
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    host = host.removeprefix("www.")
    path = parts.path or "/"
    segs = _segments(url)

    if host in ("doi.org", "dx.doi.org"):
        m = _DOI_RE.search(path)
        return ("doi", _norm_doi(m.group())) if m else None

    if host in ("sciencedirect.com", "linkinghub.elsevier.com"):
        pii = _pii_from_segments(segs)
        return ("pii", pii) if pii else None

    if host == "pubmed.ncbi.nlm.nih.gov":
        # /<pmid> (optionally with a trailing slug segment)
        if segs and segs[0].isdigit():
            return "pmid", segs[0]
        return None

    if host in ("ncbi.nlm.nih.gov", "pmc.ncbi.nlm.nih.gov", "europepmc.org"):
        pmc = _pmcid_from_segments(segs)
        return ("pmcid", pmc) if pmc else None

    if host == "arxiv.org":
        # /abs/<id> or /pdf/<id>[.pdf|vN]
        if len(segs) >= 2 and segs[0] in ("abs", "pdf"):
            ident = segs[-1]
            ident = re.sub(r"\.pdf$", "", ident, flags=re.IGNORECASE)
            # old-style ids carry a category prefix across two segments
            if len(segs) >= 3 and segs[0] in ("abs", "pdf"):
                ident = "/".join(segs[1:]).removesuffix(".pdf")
            return ("arxiv", ident) if ident else None
        return None

    return None


def _pii_from_segments(segs: list[str]) -> str | None:
    """The PII in a ScienceDirect/linkinghub path.

    ``/science/article/pii/<PII>``, ``/science/article/abs/pii/<PII>``, or
    ``/retrieve/pii/<PII>`` — the segment right after ``pii``, else the last
    segment if it looks like a PII.
    """
    for i, s in enumerate(segs):
        if s.lower() == "pii" and i + 1 < len(segs):
            cand = segs[i + 1].upper()
            return cand if _PII_RE.match(cand) else None
    if segs and _PII_RE.match(segs[-1].upper()):
        return segs[-1].upper()
    return None


def _pmcid_from_segments(segs: list[str]) -> str | None:
    """The bare PMC numeric id from a ``…/PMC<digits>`` path (no ``PMC`` prefix)."""
    for s in segs:
        m = re.fullmatch(r"PMC(\d+)", s, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def is_scholar_url(url: str) -> bool:
    """Certain match: a URL on the closed scholarly-host set that we can claim."""
    return _claim(url) is not None


def classify_scholar_url(url: str) -> tuple[str, str] | None:
    """Public alias for :func:`_claim` → ``(kind, ident)`` or ``None``."""
    return _claim(url)


# ---------------------------------------------------------------------------
# Config resolution (defensive getattr — works before ScholarCfg exists)
# ---------------------------------------------------------------------------


def _scholar_cfg(cfg: Any | None) -> Any | None:
    return getattr(getattr(cfg, "adapters", None), "scholar", None)


def _email(cfg: Any | None) -> str:
    return str(getattr(_scholar_cfg(cfg), "email", "") or "").strip()


def _s2_key(cfg: Any | None) -> str:
    return str(getattr(_scholar_cfg(cfg), "s2_api_key", "") or "").strip()


def _fetch_full_text(cfg: Any | None) -> bool:
    val = getattr(_scholar_cfg(cfg), "fetch_full_text", True)
    return bool(val) if val is not None else True


def _max_authors(cfg: Any | None) -> int:
    try:
        return max(
            1, int(getattr(_scholar_cfg(cfg), "max_authors", _DEFAULT_MAX_AUTHORS))
        )
    except (TypeError, ValueError):
        return _DEFAULT_MAX_AUTHORS


# ---------------------------------------------------------------------------
# Value normalization helpers (pure)
# ---------------------------------------------------------------------------


def _clean(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (float, str)):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    return None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _norm_doi(doi: Any) -> str | None:
    """Canonical DOI: lowercased, unwrapped from a doi.org URL, trailing
    punctuation trimmed. The DOI is the cross-source join key."""
    if not isinstance(doi, str):
        return None
    s = doi.strip()
    if not s:
        return None
    m = re.search(r"10\.\d{4,9}/\S+", s, flags=re.IGNORECASE)
    if not m:
        return None
    out = m.group().lower().rstrip(".,;)]}>\"'")
    return out or None


def _orcid_id(value: Any) -> str | None:
    """Bare ORCID (strip the ``https://orcid.org/`` prefix)."""
    s = _clean(value)
    if not s:
        return None
    m = re.search(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", s, flags=re.IGNORECASE)
    return m.group(1).upper() if m else None


def _reconstruct_abstract(inverted: Any) -> str | None:
    """Rebuild plain text from OpenAlex's ``abstract_inverted_index``
    (``{word: [positions]}``)."""
    if not isinstance(inverted, dict) or not inverted:
        return None
    positioned: list[tuple[int, str]] = []
    for word, idxs in inverted.items():
        if not isinstance(idxs, list):
            continue
        positioned.extend((i, word) for i in idxs if isinstance(i, int))
    if not positioned:
        return None
    positioned.sort(key=lambda t: t[0])
    return " ".join(w for _i, w in positioned).strip() or None


def _strip_jats(value: Any) -> str | None:
    """Crossref abstracts are JATS XML fragments — drop the tags."""
    s = _clean(value)
    if not s:
        return None
    return _clean(_JATS_TAG_RE.sub(" ", s).replace("  ", " "))


def _date_from_parts(node: Any) -> tuple[str | None, int | None]:
    """Crossref ``{"date-parts": [[Y, M, D]]}`` → ``(iso, year)``."""
    if not isinstance(node, dict):
        return None, None
    dp = node.get("date-parts")
    if not isinstance(dp, list) or not dp or not isinstance(dp[0], list) or not dp[0]:
        return None, None
    parts = [p for p in dp[0] if isinstance(p, int)]
    if not parts:
        return None, None
    year = parts[0]
    iso = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts))
    return iso, year


def _first(*values: Any) -> Any:
    """First non-empty value (None / "" / [] / {} skipped)."""
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None


# ---------------------------------------------------------------------------
# API GET seam (never raises; the monkeypatch point for tests — like gitlab)
# ---------------------------------------------------------------------------

# `(url) -> (body, status, reason)`. reason is OK whenever an HTTP response
# arrives (status then authoritative); TIMEOUT/SERVER_ERROR mark a transport
# failure with status 0.
ApiGetter = Callable[[str], Awaitable[tuple[str, int, FailureReason]]]


async def _api_get(
    url: str, *, deadline: float, email: str
) -> tuple[str, int, FailureReason]:
    """GET ``url`` with a minimal header set → ``(body, status, reason)``.

    Never raises: a timeout → ``("", 0, TIMEOUT)``; any other transport error →
    ``("", 0, SERVER_ERROR)``.
    """
    headers = {
        "User-Agent": _USER_AGENT.format(email=email or "anonymous"),
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=max(1.0, float(deadline))
        ) as client:
            resp = await client.get(url, headers=headers)
        return resp.text, resp.status_code, FailureReason.OK
    except httpx.TimeoutException:
        return "", 0, FailureReason.TIMEOUT
    except Exception:
        return "", 0, FailureReason.SERVER_ERROR


async def _safe_get(get: ApiGetter, target: str) -> Any:
    """Await one getter call, returning the tuple or the raised exception."""
    try:
        return await get(target)
    except Exception as exc:
        return exc


def _json_body(result: Any) -> Any:
    """Parse the JSON body from a getter result, swallowing every failure
    (exception, transport error, HTTP >= 400, bad JSON) → ``None``. Enrichment
    is optional, so any miss simply drops that source."""
    if isinstance(result, BaseException) or not isinstance(result, tuple):
        return None
    try:
        body, status, reason = result
    except (ValueError, TypeError):
        return None
    if reason != FailureReason.OK or status >= 400 or not body:
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None


def _mailto(url: str, email: str) -> str:
    """Append ``mailto=`` (Crossref/OpenAlex polite pool) when we have an email."""
    if not email:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}mailto={quote(email)}"


# ---------------------------------------------------------------------------
# DOI resolution (entry kind → canonical DOI)
# ---------------------------------------------------------------------------


async def _resolve_doi(kind: str, ident: str, get: ApiGetter, email: str) -> str | None:
    """Turn an entry identifier into a canonical DOI.

    ``doi`` passes through; ``arxiv`` maps to the deterministic DataCite DOI;
    ``pii`` uses Crossref's ``alternative-id`` filter (keyless); ``pmid`` /
    ``pmcid`` bridge through OpenAlex / Europe PMC. Returns ``None`` when the
    identifier resolves to nothing.
    """
    if kind == "doi":
        return _norm_doi(ident)
    if kind == "arxiv":
        return _norm_doi(f"10.48550/arXiv.{ident}")
    if kind == "pii":
        url = _mailto(
            f"{_CROSSREF}/works?filter=alternative-id:{quote(ident)}&rows=1&select=DOI",
            email,
        )
        data = _json_body(await _safe_get(get, url))
        items = data.get("message", {}).get("items") if isinstance(data, dict) else None
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return _norm_doi(items[0].get("DOI"))
        return None
    if kind == "pmid":
        url = _mailto(f"{_OPENALEX}/works/pmid:{quote(ident)}?select=doi", email)
        data = _json_body(await _safe_get(get, url))
        return _norm_doi(data.get("doi")) if isinstance(data, dict) else None
    if kind == "pmcid":
        url = (
            f"{_EUROPEPMC}/search?query=PMCID:PMC{quote(ident)}"
            "&format=json&resultType=lite"
        )
        data = _json_body(await _safe_get(get, url))
        results = (
            data.get("resultList", {}).get("result") if isinstance(data, dict) else None
        )
        if isinstance(results, list) and results and isinstance(results[0], dict):
            return _norm_doi(results[0].get("doi"))
        return None
    return None


# ---------------------------------------------------------------------------
# Per-source extractors — parsed JSON → a normalized {Paper-field: value} fragment
# ---------------------------------------------------------------------------


def _from_crossref(data: Any) -> dict[str, Any]:
    msg = data.get("message") if isinstance(data, dict) else None
    if not isinstance(msg, dict):
        return {}
    published, year = _date_from_parts(msg.get("issued") or msg.get("published"))
    authors: list[dict[str, Any]] = []
    for a in msg.get("author") or []:
        if not isinstance(a, dict):
            continue
        given, family = _clean(a.get("given")), _clean(a.get("family"))
        name = _clean(" ".join(x for x in (given, family) if x) or a.get("name"))
        if not name:
            continue
        affil = [
            _clean(aff.get("name"))
            for aff in a.get("affiliation") or []
            if isinstance(aff, dict) and _clean(aff.get("name"))
        ]
        authors.append(
            _compact(
                {
                    "name": name,
                    "given": given,
                    "family": family,
                    "orcid": _orcid_id(a.get("ORCID")),
                    "affiliation": affil,
                    "sequence": _clean(a.get("sequence")),
                }
            )
        )
    lic = msg.get("license")
    license_url = (
        _clean(lic[0].get("URL"))
        if isinstance(lic, list) and lic and isinstance(lic[0], dict)
        else None
    )
    title = msg.get("title")
    container = msg.get("container-title")
    pii = None
    for alt in msg.get("alternative-id") or []:
        if isinstance(alt, str) and _PII_RE.match(alt.upper()):
            pii = alt.upper()
            break
    return _compact(
        {
            "title": _clean(title[0]) if isinstance(title, list) and title else None,
            "authors": authors,
            "container": _clean(container[0])
            if isinstance(container, list) and container
            else None,
            "publisher": _clean(msg.get("publisher")),
            "type": _clean(msg.get("type")),
            "issn": _str_list(msg.get("ISSN")),
            "volume": _clean(msg.get("volume")),
            "issue": _clean(msg.get("issue")),
            "pages": _clean(msg.get("page")),
            "published": published,
            "year": year,
            "language": _clean(msg.get("language")),
            "abstract": _strip_jats(msg.get("abstract")),
            "reference_count": _int(msg.get("reference-count")),
            "cited_by_count": _int(msg.get("is-referenced-by-count")),
            "license": license_url,
            "pii": pii,
        }
    )


def _oa_location(node: Any) -> dict[str, Any] | None:
    if not isinstance(node, dict):
        return None
    src = node.get("source") if isinstance(node.get("source"), dict) else {}
    return (
        _compact(
            {
                "host_type": _clean(node.get("host_type")) or _clean(src.get("type")),
                "version": _clean(node.get("version")),
                "pdf_url": _clean(node.get("pdf_url"))
                or _clean(node.get("url_for_pdf")),
                "landing_url": _clean(node.get("landing_page_url"))
                or _clean(node.get("url")),
                "source": _clean(src.get("display_name"))
                or _clean(node.get("repository_institution")),
                "is_oa": node.get("is_oa")
                if isinstance(node.get("is_oa"), bool)
                else None,
            }
        )
        or None
    )


def _from_openalex(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not data.get("id"):
        return {}
    oa = data.get("open_access") if isinstance(data.get("open_access"), dict) else {}
    ids = data.get("ids") if isinstance(data.get("ids"), dict) else {}
    prim = (
        data.get("primary_location")
        if isinstance(data.get("primary_location"), dict)
        else {}
    )
    prim_src = prim.get("source") if isinstance(prim.get("source"), dict) else {}
    authors: list[dict[str, Any]] = []
    for au in data.get("authorships") or []:
        if not isinstance(au, dict):
            continue
        author = au.get("author") if isinstance(au.get("author"), dict) else {}
        name = _clean(author.get("display_name"))
        if not name:
            continue
        insts = [
            _clean(i.get("display_name"))
            for i in au.get("institutions") or []
            if isinstance(i, dict) and _clean(i.get("display_name"))
        ]
        authors.append(
            _compact(
                {
                    "name": name,
                    "orcid": _orcid_id(author.get("orcid")),
                    "affiliation": insts,
                    "sequence": _clean(au.get("author_position")),
                    "is_corresponding": True
                    if au.get("is_corresponding") is True
                    else None,
                }
            )
        )
    locations = [
        loc for loc in (_oa_location(n) for n in data.get("locations") or []) if loc
    ]
    topics = [
        _clean(t.get("display_name"))
        for t in data.get("topics") or []
        if isinstance(t, dict) and _clean(t.get("display_name"))
    ]
    mesh = [
        _clean(m.get("descriptor_name"))
        for m in data.get("mesh") or []
        if isinstance(m, dict) and _clean(m.get("descriptor_name"))
    ]
    src_ids = {}
    if _clean(ids.get("pmid")):
        src_ids["pmid"] = ids["pmid"].rstrip("/").rsplit("/", 1)[-1]
    if _clean(ids.get("pmcid")):
        src_ids["pmcid"] = ids["pmcid"].rstrip("/").rsplit("/", 1)[-1]
    if _clean(ids.get("mag")):
        src_ids["mag"] = str(ids["mag"])
    if _clean(data.get("id")):
        src_ids["openalex"] = data["id"].rstrip("/").rsplit("/", 1)[-1]
    return _compact(
        {
            "title": _clean(data.get("title") or data.get("display_name")),
            "authors": authors,
            "container": _clean(prim_src.get("display_name")),
            "type": _clean(data.get("type")),
            "issn": _str_list(prim_src.get("issn")),
            "published": _clean(data.get("publication_date")),
            "year": _int(data.get("publication_year")),
            "language": _clean(data.get("language")),
            "abstract": _reconstruct_abstract(data.get("abstract_inverted_index")),
            "cited_by_count": _int(data.get("cited_by_count")),
            "reference_count": _int(data.get("referenced_works_count")),
            "is_retracted": True if data.get("is_retracted") is True else None,
            "topics": topics,
            "mesh": mesh,
            "is_oa": oa.get("is_oa") if isinstance(oa.get("is_oa"), bool) else None,
            "oa_status": _clean(oa.get("oa_status")),
            "best_oa": _oa_location(data.get("best_oa_location")),
            "oa_locations": locations,
            "ids": src_ids,
        }
    )


def _from_unpaywall(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not data.get("doi"):
        return {}
    best = _oa_location(data.get("best_oa_location"))
    locations = [
        loc for loc in (_oa_location(n) for n in data.get("oa_locations") or []) if loc
    ]
    return _compact(
        {
            "title": _clean(data.get("title")),
            "container": _clean(data.get("journal_name")),
            "publisher": _clean(data.get("publisher")),
            "year": _int(data.get("year")),
            "type": _clean(data.get("genre")),
            "is_oa": data.get("is_oa") if isinstance(data.get("is_oa"), bool) else None,
            "oa_status": _clean(data.get("oa_status")),
            "best_oa": best,
            "oa_locations": locations,
            "license": _clean((data.get("best_oa_location") or {}).get("license"))
            if isinstance(data.get("best_oa_location"), dict)
            else None,
        }
    )


def _from_s2(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not data.get("paperId"):
        return {}
    ext = data.get("externalIds") if isinstance(data.get("externalIds"), dict) else {}
    oa_pdf = (
        data.get("openAccessPdf") if isinstance(data.get("openAccessPdf"), dict) else {}
    )
    tldr = data.get("tldr") if isinstance(data.get("tldr"), dict) else {}
    ptypes = data.get("publicationTypes")
    src_ids = {}
    if _clean(ext.get("PubMed")):
        src_ids["pmid"] = str(ext["PubMed"])
    if _clean(ext.get("PubMedCentral")):
        src_ids["pmcid"] = str(ext["PubMedCentral"])
    if _clean(ext.get("ArXiv")):
        src_ids["arxiv"] = str(ext["ArXiv"])
    if ext.get("CorpusId") is not None:
        src_ids["corpus_id"] = str(ext["CorpusId"])
    if _clean(data.get("paperId")):
        src_ids["s2"] = data["paperId"]
    return _compact(
        {
            "title": _clean(data.get("title")),
            "container": _clean(data.get("venue")),
            "year": _int(data.get("year")),
            "type": ptypes[0].lower()
            if isinstance(ptypes, list) and ptypes and isinstance(ptypes[0], str)
            else None,
            "abstract": _clean(data.get("abstract")),
            "tldr": _clean(tldr.get("text")),
            "s2_oa_pdf": _clean(oa_pdf.get("url")),
            "ids": src_ids,
        }
    )


def _from_europepmc(data: Any) -> dict[str, Any]:
    results = (
        data.get("resultList", {}).get("result") if isinstance(data, dict) else None
    )
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return {}
    r = results[0]
    src_ids = {}
    if _clean(r.get("pmid")):
        src_ids["pmid"] = str(r["pmid"])
    if _clean(r.get("pmcid")):
        # Europe PMC gives "PMC7159299"; keep the numeric id (assembler re-prefixes)
        src_ids["pmcid"] = re.sub(r"^PMC", "", str(r["pmcid"]), flags=re.IGNORECASE)
    full_text_urls = []
    ftl = r.get("fullTextUrlList")
    if isinstance(ftl, dict):
        full_text_urls.extend(
            _compact(
                {
                    "url": _clean(u.get("url")),
                    "style": _clean(u.get("documentStyle")),
                    "site": _clean(u.get("site")),
                    "availability": _clean(u.get("availability")),
                }
            )
            for u in ftl.get("fullTextUrl") or []
            if isinstance(u, dict) and _clean(u.get("url"))
        )
    return _compact(
        {
            "title": _clean(r.get("title")),
            "container": _clean(r.get("journalTitle")),
            "abstract": _clean(r.get("abstractText")),
            "is_oa": r.get("isOpenAccess") == "Y"
            if r.get("isOpenAccess") is not None
            else None,
            "in_epmc": r.get("inEPMC") == "Y",
            "has_pdf": r.get("hasPDF") == "Y",
            "epmc_full_text_urls": full_text_urls,
            "ids": src_ids,
        }
    )


# ---------------------------------------------------------------------------
# Merge / assemble → the normalized Paper record
# ---------------------------------------------------------------------------


def _merge_ids(*fragments: dict[str, Any]) -> dict[str, str]:
    """Union of every source's ID crosswalk (first non-empty per key wins)."""
    out: dict[str, str] = {}
    for frag in fragments:
        ids = frag.get("ids")
        if isinstance(ids, dict):
            for k, v in ids.items():
                if k not in out and isinstance(v, str) and v.strip():
                    out[k] = v.strip()
    return out


def _assemble(
    doi: str,
    *,
    pii: str | None,
    crossref: dict[str, Any],
    openalex: dict[str, Any],
    unpaywall: dict[str, Any],
    s2: dict[str, Any],
    europepmc: dict[str, Any],
    max_authors: int,
) -> dict[str, Any] | None:
    """Merge the per-source fragments into one Paper by field precedence.

    Returns ``None`` when no source yielded a title (the paper resolved to a DOI
    but nothing is actually known about it → the caller emits ``NOT_FOUND``).
    """
    title = _first(
        crossref.get("title"),
        openalex.get("title"),
        s2.get("title"),
        europepmc.get("title"),
        unpaywall.get("title"),
    )
    if not title:
        return None

    ids = _merge_ids(crossref, openalex, s2, europepmc)
    ids["doi"] = doi

    # abstract: S2 covers the Elsevier gap; then OpenAlex-reconstructed / EPMC / CR.
    abstract = _first(
        s2.get("abstract"),
        openalex.get("abstract"),
        europepmc.get("abstract"),
        crossref.get("abstract"),
    )
    abstract_source = None
    for name, frag in (
        ("semantic_scholar", s2),
        ("openalex", openalex),
        ("europepmc", europepmc),
        ("crossref", crossref),
    ):
        if frag.get("abstract") and frag["abstract"] == abstract:
            abstract_source = name
            break

    authors = _first(crossref.get("authors"), openalex.get("authors")) or []
    is_oa = bool(_first(unpaywall.get("is_oa"), openalex.get("is_oa")))
    best_oa = _first(unpaywall.get("best_oa"), openalex.get("best_oa"))
    oa_locations = (
        _first(unpaywall.get("oa_locations"), openalex.get("oa_locations")) or []
    )
    sources_used = [
        name
        for name, frag in (
            ("crossref", crossref),
            ("openalex", openalex),
            ("unpaywall", unpaywall),
            ("semantic_scholar", s2),
            ("europepmc", europepmc),
        )
        if frag
    ]

    return _compact(
        {
            "doi": doi,
            "pii": pii or crossref.get("pii"),
            "url": f"https://doi.org/{doi}",
            "ids": ids,
            "title": title,
            "authors": authors[:max_authors],
            "author_count": len(authors) or None,
            "container": _first(
                crossref.get("container"),
                openalex.get("container"),
                s2.get("container"),
                unpaywall.get("container"),
                europepmc.get("container"),
            ),
            "publisher": _first(
                crossref.get("publisher"),
                unpaywall.get("publisher"),
                openalex.get("publisher"),
            ),
            "type": _first(crossref.get("type"), openalex.get("type"), s2.get("type")),
            "issn": _first(crossref.get("issn"), openalex.get("issn")) or [],
            "volume": crossref.get("volume"),
            "issue": crossref.get("issue"),
            "pages": crossref.get("pages"),
            "published": _first(
                crossref.get("published"),
                openalex.get("published"),
            ),
            "year": _first(
                crossref.get("year"),
                openalex.get("year"),
                europepmc.get("year"),
                s2.get("year"),
                unpaywall.get("year"),
            ),
            "language": _first(openalex.get("language"), crossref.get("language")),
            "abstract": abstract,
            "abstract_source": abstract_source,
            "tldr": s2.get("tldr"),
            "is_oa": is_oa,
            "oa_status": _first(unpaywall.get("oa_status"), openalex.get("oa_status"))
            or ("closed" if not is_oa else None),
            "best_oa": best_oa,
            "oa_locations": oa_locations,
            "s2_oa_pdf": s2.get("s2_oa_pdf"),
            "epmc": _compact(
                {
                    "in_epmc": europepmc.get("in_epmc"),
                    "has_pdf": europepmc.get("has_pdf"),
                    "full_text_urls": europepmc.get("epmc_full_text_urls"),
                }
            ),
            "license": _first(crossref.get("license"), unpaywall.get("license")),
            "cited_by_count": _first(
                openalex.get("cited_by_count"), crossref.get("cited_by_count")
            ),
            "reference_count": _first(
                crossref.get("reference_count"), openalex.get("reference_count")
            ),
            "is_retracted": bool(openalex.get("is_retracted")),
            "topics": openalex.get("topics") or [],
            "mesh": _first(openalex.get("mesh"), europepmc.get("mesh")) or [],
            "sources_used": sources_used,
        }
    )


# ---------------------------------------------------------------------------
# Full-text acquisition
# ---------------------------------------------------------------------------

# `(pdf_url) -> markdown | None`. Default downloads + converts via the core
# fetch's PDF path; tests inject a stub.
DocFetcher = Callable[[str], Awaitable[str | None]]


def _pick_full_text(paper: dict[str, Any]) -> tuple[str, str] | None:
    """Choose the best OA source to download → ``(url, source_label)`` or None.

    Priority: Europe PMC render-PDF (biomedical, reliably a clean PDF) → the
    Unpaywall/OpenAlex ``best_oa`` PDF → any OA-location PDF → the S2 OA PDF.
    """
    pmcid = (paper.get("ids") or {}).get("pmcid")
    epmc = paper.get("epmc") or {}
    if pmcid and epmc.get("in_epmc") and epmc.get("has_pdf"):
        return f"https://europepmc.org/articles/PMC{pmcid}?pdf=render", "europepmc"
    best = paper.get("best_oa") or {}
    if best.get("pdf_url"):
        return best["pdf_url"], "unpaywall"
    for loc in paper.get("oa_locations") or []:
        if isinstance(loc, dict) and loc.get("pdf_url"):
            return loc["pdf_url"], "repository"
    if paper.get("s2_oa_pdf"):
        return paper["s2_oa_pdf"], "semantic_scholar"
    return None


async def _default_doc_fetch(
    url: str, *, deadline: float, cfg: Any | None
) -> str | None:
    """Download an OA PDF directly and convert it to text.

    Deliberately does **not** go through ``fetch_one``: some OA full-text URLs
    are themselves scholar-claimed (the Europe PMC render-PDF is a
    ``europepmc.org/articles/PMC<id>`` URL), so re-entering the dispatch would
    recurse endlessly. The adapter owns this fetch like its metadata calls —
    a plain GET + ``pdftotext``. Never raises → ``None`` on any miss (enrichment
    is optional). v1 handles PDFs only.
    """
    headers = {
        "User-Agent": _USER_AGENT.format(email=_email(cfg) or "anonymous"),
        "Accept": "application/pdf,*/*",
    }
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=max(1.0, float(deadline))
        ) as client:
            resp = await client.get(url, headers=headers)
        body = resp.content
        ctype = resp.headers.get("content-type", "").lower()
        status = resp.status_code
    except Exception as exc:
        log.debug("scholar full-text download failed for %s: %s", url, exc)
        return None
    if status >= 400 or not body:
        return None
    if "pdf" not in ctype and not body[:5].startswith(b"%PDF"):
        return None  # not a PDF (HTML viewer / login wall) — v1 handles PDFs only
    try:
        from ..converters import pdf as pdf_conv

        text, _meta = await asyncio.to_thread(pdf_conv.pdf_to_text, body)
    except Exception as exc:  # missing pdftotext / corrupt PDF / anything
        log.debug("scholar PDF convert failed for %s: %s", url, exc)
        return None
    text = (text or "").strip()
    return text or None


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _fmt_authors(authors: list[dict[str, Any]], total: int | None) -> str:
    names = [a.get("name") for a in authors if isinstance(a, dict) and a.get("name")]
    if not names:
        return ""
    shown = names[:12]
    line = ", ".join(shown)
    remaining = (total or len(names)) - len(shown)
    if remaining > 0:
        line += f", … (+{remaining})"
    return line


def _render_paper(paper: dict[str, Any], full_text: str | None) -> str:
    parts = [f"# {paper.get('title', '?')}"]
    authors = _fmt_authors(paper.get("authors") or [], paper.get("author_count"))
    if authors:
        parts += ["", authors]

    facts: list[str] = []
    venue = paper.get("container")
    if venue:
        cite = venue
        if paper.get("volume"):
            cite += f" {paper['volume']}"
        if paper.get("issue"):
            cite += f"({paper['issue']})"
        if paper.get("pages"):
            cite += f":{paper['pages']}"
        facts.append(cite)
    if paper.get("year"):
        facts.append(str(paper["year"]))
    if paper.get("publisher"):
        facts.append(paper["publisher"])
    if facts:
        parts += ["", " · ".join(facts)]

    status: list[str] = []
    oa = paper.get("oa_status")
    if paper.get("is_oa"):
        status.append(f"🔓 open access{f' ({oa})' if oa and oa != 'closed' else ''}")
    else:
        status.append("🔒 paywalled")
    if paper.get("cited_by_count") is not None:
        status.append(f"cited by {paper['cited_by_count']:,}")
    if paper.get("is_retracted"):
        status.append("⚠️ RETRACTED")
    parts += ["", " · ".join(status)]

    parts += ["", f"**DOI:** [{paper.get('doi')}]({paper.get('url')})"]
    if paper.get("topics"):
        parts += ["", "**Topics:** " + ", ".join(paper["topics"][:6])]

    if paper.get("tldr"):
        parts += ["", f"**TL;DR:** {paper['tldr']}"]
    if paper.get("abstract"):
        parts += ["", "## Abstract", "", paper["abstract"]]

    if full_text:
        body = full_text.strip()
        if len(body) > _FULL_TEXT_MAX_CHARS:
            body = body[:_FULL_TEXT_MAX_CHARS].rstrip() + "\n\n… (full text truncated)"
        parts += ["", "## Full text", "", body]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Fetch + envelope
# ---------------------------------------------------------------------------

_base_envelope, _failure_envelope = _common.envelope_builders(_PROVIDER, _CONTENT_TYPE)


def _success_envelope(
    url: str, *, paper: dict[str, Any], markdown: str, warnings: list[str]
) -> dict[str, Any]:
    from .. import io as io_mod

    quality = _compact(
        {
            "provider": _PROVIDER,
            "page_type": "paper",
            "result_count": 1,
            "doi": paper.get("doi"),
            "is_oa": paper.get("is_oa"),
            "oa_status": paper.get("oa_status"),
            "sources_used": paper.get("sources_used"),
            "paper": paper,
        }
    )
    authors = paper.get("authors") or []
    byline = (
        ", ".join(
            a["name"] for a in authors[:6] if isinstance(a, dict) and a.get("name")
        )
        or None
    )
    return envelope.success_envelope(
        base=_base_envelope(url, http_status=200),
        markdown=markdown,
        metadata={
            "title": paper.get("title"),
            "byline": byline,
            "published": paper.get("published"),
            "modified": None,
            "language": paper.get("language"),
            "site_name": _SITE_NAME,
            "image": None,
            "word_count": len(markdown.split()),
            "quality": quality,
            "warnings": warnings,
        },
        token_count_estimate=io_mod.estimate_tokens(markdown),
    )


async def fetch_scholar(
    url: str,
    *,
    deadline: float = 30.0,
    cfg: Any | None = None,
    _get: ApiGetter | None = None,
    _doc: DocFetcher | None = None,
) -> dict[str, Any]:
    """Fetch a scholarly URL → a merged, structured envelope. Never raises.

    Resolves the URL to a DOI, fans out across the open scholarly APIs via the
    adapter's own minimal-header httpx client (``_get`` overrides it in tests),
    and merges the results. When full-text fetch is enabled and a clean OA copy
    exists, the OA PDF is downloaded + converted into the envelope markdown
    (``_doc`` overrides that fetch in tests).
    """
    claim = _claim(url)
    if claim is None:  # defensive — dispatch only calls us on a claimed URL
        return _failure_envelope(
            url, FailureReason.PARSE_FAILED, "scholar: unrecognized URL shape"
        )
    kind, ident = claim
    email = _email(cfg)
    get: ApiGetter = _get or (lambda u: _api_get(u, deadline=deadline, email=email))

    try:
        doi = await _resolve_doi(kind, ident, get, email)
    except Exception as exc:  # defensive — resolution must never raise out
        log.warning("scholar DOI resolution failed (%s %s): %s", kind, ident, exc)
        doi = None
    if not doi:
        return _failure_envelope(
            url,
            FailureReason.NOT_FOUND,
            f"scholar: could not resolve {kind} '{ident}' to a DOI",
        )

    # Fan out across the sources by DOI. Unpaywall is skipped without an email
    # (it requires one); its OA data then falls back to OpenAlex in the merge.
    enc = quote(doi, safe="")
    targets: list[tuple[str, str]] = [
        ("crossref", _mailto(f"{_CROSSREF}/works/{enc}", email)),
        ("openalex", _mailto(f"{_OPENALEX}/works/doi:{enc}", email)),
        (
            "s2",
            f"{_S2}/paper/DOI:{enc}?fields={_S2_FIELDS}"
            + (f"&api_key={quote(_s2_key(cfg))}" if _s2_key(cfg) else ""),
        ),
        (
            "europepmc",
            f'{_EUROPEPMC}/search?query=DOI:"{doi}"&format=json&resultType=core',
        ),
    ]
    if email:
        targets.insert(2, ("unpaywall", f"{_UNPAYWALL}/{enc}?email={quote(email)}"))

    results = await asyncio.gather(*(_safe_get(get, t) for _name, t in targets))
    by_name = {
        name: _json_body(res) for (name, _t), res in zip(targets, results, strict=True)
    }

    crossref = _from_crossref(by_name.get("crossref"))
    openalex = _from_openalex(by_name.get("openalex"))
    unpaywall = _from_unpaywall(by_name.get("unpaywall"))
    s2 = _from_s2(by_name.get("s2"))
    europepmc = _from_europepmc(by_name.get("europepmc"))

    pii = ident if kind == "pii" else None
    paper = _assemble(
        doi,
        pii=pii,
        crossref=crossref,
        openalex=openalex,
        unpaywall=unpaywall,
        s2=s2,
        europepmc=europepmc,
        max_authors=_max_authors(cfg),
    )
    if paper is None:
        return _failure_envelope(
            url,
            FailureReason.NOT_FOUND,
            f"scholar: DOI {doi} resolved but no metadata was found in any source",
        )

    warnings: list[str] = []
    if kind == "pii":
        warnings.append("resolved_via_pii")
    if paper.get("is_retracted"):
        warnings.append("retracted")
    if not paper.get("abstract"):
        warnings.append("no_abstract")

    # Full-text acquisition (opt-out via config): download the best OA copy.
    full_text: str | None = None
    if _fetch_full_text(cfg) and paper.get("is_oa"):
        pick = _pick_full_text(paper)
        if pick is not None:
            ft_url, ft_source = pick
            doc: DocFetcher = _doc or (
                lambda u: _default_doc_fetch(u, deadline=deadline, cfg=cfg)
            )
            try:
                full_text = await doc(ft_url)
            except Exception as exc:  # defensive — enrichment never fails the fetch
                log.debug("scholar full-text fetch raised for %s: %s", ft_url, exc)
                full_text = None
            paper["full_text_url"] = ft_url
            paper["full_text_source"] = ft_source
            if not full_text:
                warnings.append("full_text_unavailable")
    if not paper.get("is_oa"):
        warnings.append("paywalled")

    return _success_envelope(
        url, paper=paper, markdown=_render_paper(paper, full_text), warnings=warnings
    )
