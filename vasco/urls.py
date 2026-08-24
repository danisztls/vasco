"""URL canonicalization: the single home for vasco's URL identity rules.

`normalize_url` is the cache key and therefore load-bearing — changing it
invalidates every cached entry. `registered_domain` (PSL eTLD+1) and
`route_key` (registered_domain + first structural path segment) key the
per-route fetch strategy, the coordinator's rate limiter, and the browser
tier's first-party checks.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

# Curated denylist of query params that are never content-bearing, so dropping
# them folds the same page's tracked variants onto one cache row. Conservative
# by design: ambiguous params that are sometimes load-bearing (`ref`, `source`,
# `si`, `id`, `page`, `sort`, `aff_*`) are intentionally kept — over-stripping
# would silently collapse distinct pages and serve wrong cached content.
_TRACKING_PREFIXES = ("utm_", "mtm_")  # utm_*: GA/most; mtm_*: Matomo
_TRACKING_EXACT = {
    # Mailchimp / generic campaign + ad-click IDs
    "fbclid",
    "gclid",
    "gclsrc",
    "dclid",
    "gbraid",
    "wbraid",
    "gad_source",
    "msclkid",
    "yclid",
    # Social share IDs
    "igshid",
    "igsh",
    "ttclid",
    "twclid",
    "mibextid",
    # Email / CRM
    "mc_eid",
    "mc_cid",
    "mc_tc",
    "mkt_tok",  # Marketo
    "_hsenc",  # HubSpot
    "_hsmi",
    "__hssc",
    "__hstc",
    "__hsfp",
    "hsCtaTracking",
    # Analytics linkers / listing-click IDs
    "_ga",  # Google Analytics cross-domain linker
    "_gl",
    "_openstat",  # Yandex / openstat
    "srsltid",  # Google Merchant listing-click ID (lands on shopping click-throughs)
    # Single-purpose referral / campaign / impression tags
    "fb_source",  # Facebook referral
    "fb_ref",
    "itm_campaign",  # internal traffic monitoring (utm's site-internal cousin)
    "itm_medium",
    "itm_source",
    "vero_id",  # Vero email
    "vero_conv",
    "__twitter_impression",
    # Alibaba / AliExpress (query-only tracking; adapters key off the URL path)
    "spm",
    "scm",
}

# Host-scoped tracking params. The global _TRACKING_EXACT list above is
# deliberately conservative — generic names (`page`, `sid`, `tab_id`, …) are
# load-bearing on *some* site, so they're kept globally. But the same name can be
# unambiguous tracking on one specific site while content-bearing elsewhere;
# these rules drop such params only on the matching host, mirroring
# _REDIRECT_RULES' host-scoping below (and ClearURLs' per-provider catalog). Each
# rule is (host_regex, frozenset_of_param_names); matched against the decoded key.
_HOST_TRACKING_RULES: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    # MercadoLibre/Livre: the catalog/product id lives in the path
    # (/p/MLB…, /up/MLBU…), so `pdp_filters` (offer selector) and `sid` (click
    # source, e.g. =bookmarks) only carry click context — fold onto one cache
    # row. (ClearURLs #1249.) Matches the international TLDs (.com.br/.com.ar/…).
    (
        re.compile(r"^(?:[a-z0-9-]+\.)*mercadoli(?:vre|bre)\.com(?:\.[a-z]{2})?$"),
        frozenset({"pdp_filters", "sid"}),
    ),
    # OLX Brazil homefeed / recommendation click context. Pagination on OLX is
    # `?o=N` (not `page`), so `page=home` here is a click-source tag, droppable
    # on this host only — globally `page` stays (it's pagination everywhere else).
    (
        re.compile(r"^(?:[a-z0-9-]+\.)*olx\.com\.br$"),
        frozenset(
            {"rec", "custom_tag", "gallery_id", "tab_id", "is_fallback", "page", "lis"}
        ),
    ),
)

# AMP query params stripped to fold AMP variants into the canonical row.
# `?amp=1` is the vivareal / generic-CMS form; `?output=amp` is Twitter/X
# and several news sites. We only drop `output` when its value is the
# AMP sentinel — `output=json` etc. is meaningful elsewhere.
_AMP_AMP_VALUES = frozenset({"", "1", "true", "amp"})
_AMP_OUTPUT_VALUES = frozenset({"amp"})

WIKIMEDIA_PROJECTS = (
    "wikibooks",
    "wikinews",
    "wikipedia",
    "wikiquote",
    "wikisource",
    "wikiversity",
    "wikivoyage",
    "wiktionary",
)

_WIKIMEDIA_RE = re.compile(
    r"^https?://(?P<lang>[a-z]{2,3}(?:-[a-z0-9]+)*|simple)(?:\.m)?\."
    r"(?P<project>" + "|".join(WIKIMEDIA_PROJECTS) + r")"
    r"\.org/wiki/(?P<title>.+)",
    re.IGNORECASE,
)

# Plain `/w/index.php?title=Foo` views fold to `/wiki/Foo`; revision/diff/edit
# variants (see _INDEX_NON_ARTICLE_PARAMS) are left untouched so they keep
# their own cache rows. Mirrors vasco.adapters.wikimedia._parse_index_php.
_WIKIMEDIA_INDEX_RE = re.compile(
    r"^https?://(?P<lang>[a-z]{2,3}(?:-[a-z0-9]+)*|simple)(?:\.m)?\."
    r"(?P<project>" + "|".join(WIKIMEDIA_PROJECTS) + r")\.org/w/index\.php",
    re.IGNORECASE,
)
_INDEX_NON_ARTICLE_PARAMS = frozenset(
    {"action", "veaction", "oldid", "diff", "curid", "diffonly", "undo", "undoafter"}
)

# Matches any YouTube URL that points to a specific video, capturing the
# video ID. One of three named groups will be set per match:
#   - id_short: youtu.be/<id>
#   - id_query: <yt-host>/watch?[...&]v=<id>
#   - id_path:  <yt-host>/(embed|shorts|v|live)/<id>
# Used both for cache normalization (collapse every variant to /watch?v=<id>)
# and for extract_video_id in vasco.youtube.
YT_VIDEO_ID_RE = re.compile(
    r"^https?://"
    r"(?:"
    r"(?:www\.)?youtu\.be/(?P<id_short>[A-Za-z0-9_-]+)"
    r"|"
    r"(?:[a-z0-9-]+\.)*(?:youtube\.com|youtube-nocookie\.com)(?:\.[a-z]{2,})?"
    r"/(?:"
    r"watch\?(?:[^#]*?&)?v=(?P<id_query>[A-Za-z0-9_-]+)"
    r"|"
    r"(?:embed|shorts|v|live)/(?P<id_path>[A-Za-z0-9_-]+)"
    r")"
    r")",
    re.IGNORECASE,
)

# Matches any YouTube host that isn't a video URL (channel pages, playlists,
# homepage). Used as a fallback after YT_VIDEO_ID_RE so non-video YouTube URLs
# still get host canonicalization.
_YT_HOST_RE = re.compile(
    r"^(?:[a-z0-9-]+\.)*(?:youtube\.com|youtube-nocookie\.com)(?:\.[a-z]{2,})?$",
    re.IGNORECASE,
)


def _is_tracking_param(key: str) -> bool:
    if key in _TRACKING_EXACT:
        return True
    return any(key.startswith(p) for p in _TRACKING_PREFIXES)


def _host_tracking_params(host: str) -> frozenset[str]:
    """Params to drop for `host` via the host-scoped rules (empty if none match)."""
    for host_re, params in _HOST_TRACKING_RULES:
        if host_re.match(host):
            return params
    return frozenset()


def _is_amp_param(key: str, value: str) -> bool:
    """Is this query param an AMP-mode marker we should drop?"""
    if key == "amp":
        return value.lower() in _AMP_AMP_VALUES
    if key == "output":
        return value.lower() in _AMP_OUTPUT_VALUES
    return False


def _strip_amp_path(path: str) -> str:
    """Collapse common AMP path patterns (`/amp` suffix, `/amp/` segment).

    Examples:
      /article/amp        → /article
      /imovel/amp/foo     → /imovel/foo
      /amp/foo            → /foo
      /amphibian/x        → /amphibian/x   (full segment match only)
    """
    if "/amp" not in path:
        return path
    segments = path.split("/")
    cleaned = [s for s in segments if s != "amp"]
    if cleaned == segments:
        return path
    new_path = "/".join(cleaned)
    if path.startswith("/") and not new_path.startswith("/"):
        new_path = "/" + new_path
    return new_path or "/"


def _canonicalize_youtube_host(raw: str) -> str:
    """Collapse every YouTube URL form for one video to a single canonical
    ``https://youtube.com/watch?v=<id>`` so they share a cache row.

    Covers: youtu.be short links, www./m./music. subdomains, country-local
    TLDs (youtube.com.br), the privacy embed domain youtube-nocookie.com, and
    the alternate video paths (/embed/, /shorts/, /v/, /live/). Non-video
    YouTube URLs (playlists, channels, homepage) get only host canonicalization.
    """
    m = YT_VIDEO_ID_RE.match(raw)
    if m:
        video_id = m.group("id_short") or m.group("id_query") or m.group("id_path")
        parts = urlsplit(raw)
        other = "&".join(
            p for p in parts.query.split("&") if p and not p.startswith("v=")
        )
        new_query = f"v={video_id}" + (f"&{other}" if other else "")
        return urlunsplit(("https", "youtube.com", "/watch", new_query, ""))

    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if host and _YT_HOST_RE.match(host):
        return urlunsplit(("https", "youtube.com", parts.path, parts.query, ""))

    return raw


def _index_php_article(raw: str) -> tuple[str, str, str] | None:
    """``(lang, project, title)`` for a plain ``/w/index.php?title=`` view, else None."""
    m = _WIKIMEDIA_INDEX_RE.match(raw)
    if not m:
        return None
    qs = parse_qs(urlsplit(raw).query)
    if _INDEX_NON_ARTICLE_PARAMS & qs.keys():
        return None
    title = (qs.get("title") or [""])[0]
    if not title:
        return None
    return m.group("lang").lower(), m.group("project").lower(), title.replace(" ", "_")


def _canonicalize_wikimedia(raw: str) -> str:
    """Collapse Wikimedia project URL variants to a canonical form.

    Strips mobile subdomains and normalizes the title (spaces → underscores,
    first char uppercase) so different encodings share a cache row.
    Works for all Wikimedia projects: wikipedia, wiktionary, wikibooks, etc.
    """
    m = _WIKIMEDIA_RE.match(raw)
    if m:
        lang = m.group("lang").lower()
        project = m.group("project").lower()
        title_raw = m.group("title").split("#")[0].split("?")[0]
        title = unquote(title_raw).replace(" ", "_")
    else:
        info = _index_php_article(raw)
        if info is None:
            return raw
        lang, project, title = info
    if title:
        title = title[0].upper() + title[1:]
    return (
        f"https://{lang}.{project}.org/wiki/{quote(title, safe="/:@!$&'()*+,;=-._~")}"
    )


# Redirect / link-unwrapping rules. Many platforms route outbound links through
# a tracking redirector that carries the true destination in a query param
# (Facebook l.php?u=, Google /url?q=, out.reddit.com?url=, …). Fetching the
# wrapper yields an interstitial and caches under a useless key, so we extract
# the inner URL up front. Each rule is (host_regex, required_path_prefix,
# param_candidates); the first candidate present whose decoded value is an
# absolute http(s) URL wins. Matching is host-scoped (and path-scoped where the
# host is also a normal site, e.g. google.com/url vs google.com/search), so a
# regular page carrying a ``?url=`` param is never unwrapped.
_REDIRECT_RULES: tuple[tuple[re.Pattern[str], str | None, tuple[str, ...]], ...] = (
    (
        re.compile(r"^(?:l|lm|m)\.(?:facebook|messenger|instagram)\.com$"),
        "/l.php",
        ("u",),
    ),
    (re.compile(r"^(?:www\.)?google\.[a-z.]+$"), "/url", ("q", "url")),
    (re.compile(r"^(?:www\.)?youtube\.com$"), "/redirect", ("q", "url")),
    (re.compile(r"^out\.reddit\.com$"), None, ("url",)),
    (re.compile(r"^steamcommunity\.com$"), "/linkfilter/", ("url",)),
    (re.compile(r"^away\.vk\.com$"), "/away.php", ("to",)),
    (re.compile(r"^t\.umblr\.com$"), "/redirect", ("z",)),
    (re.compile(r"^(?:www\.)?linkedin\.com$"), "/redir/redirect", ("url",)),
    (re.compile(r"^disq\.us$"), "/url", ("url",)),
)
_MAX_UNWRAP_DEPTH = 3


def _query_param(query: str, name: str) -> str | None:
    """First ``name=`` value from a raw query string, percent-decoded.

    Uses ``unquote`` (not ``unquote_plus``) to mirror JS ``decodeURIComponent``
    — a literal ``+`` in the target stays a ``+`` rather than becoming a space.
    """
    for tok in query.split("&"):
        k, eq, v = tok.partition("=")
        if eq and k == name:
            return unquote(v)
    return None


def _redirect_target(raw: str) -> str | None:
    """The wrapped destination of a single redirector URL, or None."""
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if not host:
        return None
    path = parts.path or ""
    for host_re, path_prefix, params in _REDIRECT_RULES:
        if not host_re.match(host):
            continue
        if path_prefix is not None and not path.startswith(path_prefix):
            continue
        for name in params:
            target = _query_param(parts.query, name)
            if target and target.startswith(("http://", "https://")):
                return target
        # Known redirector but no usable target — don't try other rules.
        return None
    return None


def _unwrap_redirect(raw: str) -> str:
    """Unwrap nested redirect wrappers to the real destination (bounded depth)."""
    seen: set[str] = set()
    for _ in range(_MAX_UNWRAP_DEPTH):
        if raw in seen:
            break
        seen.add(raw)
        target = _redirect_target(raw)
        if target is None:
            break
        raw = target
    return raw


def normalize_url(url: str) -> str:
    """Normalize a URL.

    Rules:
      - Lowercase scheme and host
      - Drop fragment
      - Drop default ports (80/http, 443/https)
      - Drop trailing slash from non-root paths
      - Sort query params alphabetically (preserving order of repeated keys)
      - Drop a curated denylist of tracking params (utm_*/mtm_* prefixes plus
        ad-click, social-share, email, and Alibaba spm/scm IDs — see
        _TRACKING_EXACT/_TRACKING_PREFIXES), plus host-scoped tracking params
        for sites where a generic name is unambiguous tracking (MercadoLivre
        pdp_filters/sid, OLX homefeed tags — see _HOST_TRACKING_RULES)
      - Drop AMP markers: ``?amp=`` (any AMP-ish value) and ``?output=amp``;
        strip ``/amp/`` segments and ``/amp`` suffix from the path
      - Leave percent-encoded characters alone
      - Collapse YouTube variants (``youtu.be``, ``m./music./www.youtube.com``,
        local TLDs like ``youtube.com.br``) to bare ``youtube.com``
      - Unwrap known redirect wrappers (Facebook ``l.php``, Google ``/url``,
        ``out.reddit.com``, …) to the real destination — see _REDIRECT_RULES
    """
    if not url:
        return url
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw

    raw = _unwrap_redirect(raw)
    raw = _canonicalize_youtube_host(raw)
    raw = _canonicalize_wikimedia(raw)
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    host = host.lower()

    port = parts.port
    if port is not None and (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        port = None

    userinfo = ""
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo += ":" + parts.password
        userinfo += "@"

    netloc = userinfo + host
    if port is not None:
        netloc += f":{port}"

    path = _strip_amp_path(parts.path)
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"

    query = ""
    if parts.query:
        host_drop = _host_tracking_params(host)
        tokens = [tok for tok in parts.query.split("&") if tok != ""]
        triples: list[tuple[str, str, bool]] = []
        for tok in tokens:
            if "=" in tok:
                k, _, v = tok.partition("=")
                has_eq = True
            else:
                k, v = tok, ""
                has_eq = False
            k_dec = unquote(k)
            if _is_tracking_param(k_dec) or k_dec in host_drop:
                continue
            if _is_amp_param(k_dec, unquote(v)):
                continue
            triples.append((k_dec, v, has_eq))
        triples.sort(key=lambda t: t[0])
        encoded_parts = []
        for k_dec, v_raw, has_eq in triples:
            k_enc = quote(k_dec, safe="")
            if has_eq:
                encoded_parts.append(f"{k_enc}={v_raw}")
            else:
                encoded_parts.append(k_enc)
        query = "&".join(encoded_parts)

    return urlunsplit((scheme, netloc, path, query, ""))


# Public Suffix List lookups via the bundled snapshot only: `suffix_list_urls=()`
# disables network fetches (deterministic, offline-safe) and `cache_dir=None`
# disables disk caching. The eTLD+1 (registered domain) is the base of the
# strategy key, so we want a real PSL — not a hand-rolled second-level guess.
_TLD_EXTRACT = None  # lazily built on first registered_domain() call


def _tld_extract():
    """Return the process-wide TLDExtract singleton, importing tldextract on
    first use. Deferring the import (~56ms) keeps importing this module cheap —
    it's pulled in by nearly the whole codebase, but the PSL lookup is only
    needed on real fetches/coordination, not at CLI import or ``--help``."""
    global _TLD_EXTRACT
    if _TLD_EXTRACT is None:
        import tldextract

        _TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
    return _TLD_EXTRACT


def registered_domain(url: str) -> str:
    """Registered domain (eTLD+1) via the Public Suffix List.

    e.g. ``www.foo.example.co.uk`` → ``example.co.uk``. Hosts with no public
    suffix (``localhost``, raw IPs, internal names) fall back to the bare host
    minus a leading ``www.``.
    """
    if not url:
        return ""
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    ext = _tld_extract()(raw)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}".lower()
    host = (urlsplit(raw).hostname or "").lower()
    return host.removeprefix("www.")


# An id-ish slug: has a hyphen and at least one digit (e.g. "apto-2q-id-12345").
_ID_SLUG_RE = re.compile(r"-.*\d|\d.*-")


def _is_variable_segment(seg: str) -> bool:
    """A path segment that varies per-item (id, long slug) rather than naming
    a stable route class. Such segments are wildcarded in `route_key`."""
    if any(c.isdigit() for c in seg):
        return True
    if len(seg) > 24:
        return True
    return bool(_ID_SLUG_RE.search(seg))


def route_key(url: str) -> str:
    """Strategy key: registered_domain + first structural path segment.

    Distinguishes page-types within a domain (e.g. vivareal ``/aluguel`` list vs
    ``/imovel`` detail) while collapsing per-city slugs and per-listing ids so
    learning accumulates. Degrades to the bare domain for homepages.

    Keeping only the *first* path segment literal is deliberate: it avoids
    fragmenting learning per-city/state (all ``/aluguel/<state>/<city>`` share
    one key) while still separating route classes that lead with a different
    first segment.
    """
    dom = registered_domain(url)
    if not dom:
        return ""
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    segs = [s for s in urlsplit(raw).path.lower().split("/") if s]
    if not segs:
        return dom
    first = segs[0]
    if _is_variable_segment(first):
        return f"{dom}/*"
    if len(segs) == 1:
        return f"{dom}/{first}"
    return f"{dom}/{first}/*"
