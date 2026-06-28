# Content/marketplace adapters

Per-adapter detail for Vasco's content/marketplace adapters. The module map in [`CLAUDE.md`](../CLAUDE.md) points here; the scraper-rot **contract** (anchor-absent → `PARSE_FAILED`, anchor-present-but-empty → `success` + `no_results`) lives in CLAUDE.md's Invariants. The small core adapters that are part of the search/fetch/answer flows — `ddgs`, `wayback`, `youtube`, `wikimedia`, `deepseek` — stay in the CLAUDE.md module map.

Every adapter here fetches HTML via the shared escalation chain (an injected `fetch_html`, so it shares the strategy/seed/tier-learning system) and builds its **own envelope shape** through `vasco/envelope.py`. The chain is `http → browser → browser+mobile` **minus the wayback tail** (`allow_snapshot=False` in `fetch._make_adapter_fetcher`): adapters parse live structured data (prices, stock, listings), so an archived snapshot would be stale and its rewritten HTML breaks the structural anchor — a blocked adapter fetch returns the honest `BLOCKED_*` failure rather than a plausible-but-stale snapshot.

## `vasco/adapters/realestate.py`

Brazilian real-estate portals (vivareal) — HTML fetched via the shared escalation chain (injected `fetch_html`) then parsed per-provider into normalized listings (`url`, `title`, `type`, `price`, `condo_fee`, `area`, `bedrooms`, `bathrooms`, `parking`, `neighborhood`, `city`, `street`, `description`, `amenities`, `image`, `images`; `title`/`description` carry free-text when a provider lacks clean structured fields) in `quality.listings`; `list` pages (many, thumbnail) vs `detail` pages (one, full gallery); own envelope shape (`mode_used="realestate"`, `content_type="application/x-realestate"`).

## `vasco/adapters/google_shopping.py`

Google Shopping BR results (search + homepage) — HTML fetched via the shared escalation chain (injected `fetch_html`; routes seeded to the browser tier in `vasco/strategy.py` since the http tier only serves an empty JS shell) then parsed via `<product-viewer-entrypoint>` aria-labels into structured products (title, price, store, rating, discount, badges) in `quality.products`; filters used/refurb + international sellers + IQR outliers; own envelope shape (`mode_used="google_shopping"`, `content_type="application/x-google-shopping"`).

## `vasco/adapters/olx.py`

OLX.com.br classifieds — two verticals only: real estate (`/imoveis/`) + vehicles (`/autos-e-pecas/`); other categories aren't matched by `is_olx_url` and fall through to normal fetch. Bare category-landing **hub** pages (`/imoveis/estado-sp`, `/autos-e-pecas` — vertical + at most a single `estado-XX` segment, www host) are App-Router navigation pages with no embedded listing JSON; `_is_category_hub` short-circuits them to a `CATEGORY_LANDING` failure **before fetching** (clear/accurate, not the misleading `PARSE_FAILED`). HTML via the shared escalation chain (injected `fetch_html`); Cloudflare-protected so `olx.com.br` is seeded to the browser tier in `vasco/strategy.py`. Embedded JSON, not HTML scraping: **list** pages parse `<script id="__NEXT_DATA__">` → `props.pageProps.ads[]`; **detail** pages parse `<script id="initial-data" data-json>` → `.ad` (schema.org JSON-LD `Offer`/`Car` as fallback). Each ad's category-agnostic `properties[]` (name/value) is lifted into a per-vertical typed `attributes` bag (RE: type/area/bedrooms/bathrooms/parking/condo_fee/iptu/amenities; vehicles: brand/model/year/mileage/fuel/gearbox/cartype/color/doors/motorpower/features) on a common listing (`url`, `title`, `price`, `old_price`, `category`, `vertical`, `neighborhood`, `municipality`, `uf`, `image`, `images`, `description`, `date`) in `quality.listings`; own envelope shape (`mode_used="olx"`, `content_type="application/x-olx"`).

## `vasco/adapters/mercadolivre.py`

MercadoLivre BR marketplace (`mercadolivre.com.br`; Spanish-country MercadoLibre out of scope, falls through to normal fetch). HTML via the shared escalation chain (injected `fetch_html`); bot-challenged on the http tier so `mercadolivre.com.br` is seeded to the browser tier in `vasco/strategy.py` (bare-domain key covers www./lista./produto.). **schema.org JSON-LD is the robust spine** (survives ML's CSS class rotation): **search** pages parse the `@graph` of `Product` objects → many products (title, price, currency, url, brand, aggregateRating, image); **product** pages parse the single rich `Product` (offers.shippingDetails→`free_shipping`, itemCondition→`condition`, aggregateRating, brand, sku, color, description) + best-effort `ui-pdp-*`/`andes-*` HTML extras (`seller`, `sold_quantity`, `installments`, struck `original_price`, spec-table `attributes`) that never fail the parse. Products in `quality.products` (`page_type` search/product, `currency`, `result_count`); own envelope shape (`mode_used="mercadolivre"`, `content_type="application/x-mercadolivre"`). When ML's risk engine walls the persistent session (the `/gz/account-verification` interstitial), `bot_detect` classifies it `LOGIN_REQUIRED` and the browser server auto-recovers by clearing ML's cookies + retrying (see `browser_server.py`); only an unrecoverable wall surfaces as a `LOGIN_REQUIRED` failure.

## `vasco/adapters/aliexpress.py`

AliExpress (`aliexpress.com` global + `pt.`/`www.`/`m.` subdomains, and `aliexpress.com.br`). Runs Alibaba's `baxia`/`x5sec` **punish** stack: the http tier only gets the `_____tmd_____/punish` stub and a *cold* browser gets the `nc` slider, so `aliexpress.com`/`aliexpress.com.br` are seeded to the browser tier in `vasco/strategy.py` — success depends on the **warm persistent profile holding an earned `x5secdata` clearance**. Unlike the other adapters there is **no embedded JSON** (detail is a CSR `newDetail` app whose data loads via a *signed* mtop XHR), so it parses two robust surfaces: **search** (`/w/wholesale-*.html`, `/wholesale`) reads rendered `card-out-wrapper` cards — title (`<h3>`), price from the structural `decimal_point` spans (`_price_from_spans`, robust to AliExpress splitting each price fragment into its own node), discount-gated old price, rating, sold count, installments, image (CDN size-suffix stripped); **product** (`/item/<id>.html`) is spine'd on the URL `product_id` + the **open reviews endpoint** (`feedback.aliexpress.com/pc/searchEvaluation.do` → rating, count, star histogram, top reviews) + best-effort PDP DOM (title/price/gallery) that never fails. Product path is **resilient to a walled PDP**: a blocked/empty page still returns `product_id` + reviews (open API, no clearance needed) with a `page_blocked` warning; only a block with *no* reviews surfaces the failure. Products in `quality.products` (`page_type` search/product, `currency`, `result_count`); own envelope shape (`mode_used="aliexpress"`, `content_type="application/x-aliexpress"`).

## `vasco/adapters/shopify.py`

**Generic** Shopify storefront adapter — works on *any* Shopify store because it fetches the platform-level JSON endpoints (identical across themes), not theme HTML: **product** (`/products/<handle>` → `/products/<handle>.js`, prices in **cents**), **collection** (`/collections/<handle>` → `/collections/<handle>/products.json?limit=250&page=N`, decimal-string prices), **search** (`/search?q=` → `/search/suggest.json`, predictive, **≤10 results**). The injected `fetch_html` fetches the JSON endpoint *directly* (not embedded-in-HTML like the marketplace adapters). Detection has two tiers: `is_shopify_url` (*certain* — `*.myshopify.com` or a registered domain in the known set = built-in seed `simwooddenim.com` ∪ `cfg.adapters.shopify.domains` ∪ positive probe memo) and `is_shopify_candidate` (*probe-worthy* — unknown domain on a `/products|/collections/<handle>` page shape; `cfg.adapters.shopify.autodetect`). A probe miss raises `NotShopify` → dispatcher **falls through to a normal fetch** (not a failure) + negative-memoizes; a hit positive-memoizes the domain. **Probe verdicts persist** in the cache's `adapter_probe` table (in-process memo fronts it), so a domain is probed at most once *ever* — the CLI and vascod share the DB file, and a stale verdict (30-day TTL) is re-probed so a re-platformed site self-heals. Currency comes from a per-domain memoized `/cart.js`. Products in `quality.products` (`provider`/`shop`/`page_type`/`collection`/`query`/`currency`/`result_count`); own envelope shape (`mode_used="shopify"`, `content_type="application/x-shopify"`). No strategy seed — the JSON endpoints serve on the http tier.

## `vasco/adapters/shopee.py`

Shopee BR marketplace (`shopee.com.br`; other-country Shopee out of scope). **Product pages only** — `is_shopee_url` matches just the canonical `…-i.<shopId>.<itemId>` tail, so search/category/home URLs fall through to a normal fetch (Shopee's search results load via an anti-bot-signed internal API — `/api/v4/search/search_items` returns `error 90309999` even from a logged-in browser — and the category SPA embeds no listing JSON, so there's nothing structured to parse). HTML via the shared escalation chain (injected `fetch_html`); bot-challenged on the http tier so `shopee.com.br` is seeded to the browser tier in `vasco/strategy.py`. **schema.org `Product` JSON-LD is the spine** (embedded server-side for SEO, survives Shopee's CSS rotation): name, productID, image, brand, description, `offers` (price — **en-format dot-decimal, not BR comma** — priceCurrency, itemCondition, availability, nested `seller` Organization with the shop's aggregateRating) + a product-level aggregateRating; `ratingValue`/`ratingCount` come as **strings**. `shopId`/`itemId` are lifted from the URL tail; the category path is recovered from the page's `BreadcrumbList`. One product per page in `quality.products` (`page_type="product"`, `currency`, `result_count`); own envelope shape (`mode_used="shopee"`, `content_type="application/x-shopee"`).

## `vasco/adapters/amazon.py`

Amazon BR marketplace (`amazon.com.br`; other-country Amazon — `amazon.com`, `amazon.co.uk`, … — is out of scope and falls through, since prices/labels assume the pt-BR storefront). **Two page types** — **search** (`/s?k=<query>`) and **product** (`/dp/<ASIN>`, `/gp/product/<ASIN>`, `/gp/aw/d/<ASIN>`); non-listing Amazon URLs (homepage, `/b?node=` browse, `/gp/cart`, account, a `/s` with no `k=` keyword) are deliberately **not** matched by `is_amazon_url` (they carry no extractable listing) so they fall through to a normal fetch. HTML via the shared escalation chain (injected `fetch_html`). Unlike the bot-challenged marketplaces, Amazon serves full structured HTML on the **plain http tier**, so it is **not** seeded to the browser tier in `vasco/strategy.py`; the chain escalates http → browser on its own if Amazon throws its homegrown robot/captcha wall (recognised by `fetch.bot_detect` → `BLOCKED_CAPTCHA` via the `/errors/validateCaptcha` / `api-services-support@amazon` / `opfcaptcha-prod` markers, so the wall surfaces honestly instead of as a misleading `PARSE_FAILED`).

**Amazon embeds no schema.org JSON-LD** on search or product pages, so — like AliExpress — the robust spine is the **server-rendered DOM** Amazon ships in full (markup-stable across themes):

- **Search** (`/s?k=`): the `div[data-component-type="s-search-result"]` cards are the anchor (each carries a `data-asin`) → title (`h2`), `price` (current `.a-price`, **dropping the per-unit `.a-text-price`**), `original_price` (struck list price, kept only when **strictly above** the current price so a per-unit price never reads as a fake discount), `rating` (`.a-icon-alt` "4,8 de 5 estrelas"), `review_count` (the "8.399 classificações" aria-label), `image` (CDN size-suffix stripped), and a `sponsored` flag on ad placements. The per-card URL is rebuilt as the clean canonical `/dp/<ASIN>` form (Amazon's hrefs are tracking-laden `ref=` slugs).
- **Product** (`/dp/<ASIN>`): `#productTitle` is the anchor → `asin` (URL or `input#ASIN`), `price` (`#corePriceDisplay_desktop_feature_div`; the visible `.a-price-whole`/`.a-price-fraction` spans are reassembled because the screen-reader `.a-offscreen` is often empty on PDPs), `original_price`, `rating` (`#acrPopover` title), `review_count` (`#acrCustomerReviewText`), `brand` (`#bylineInfo`, "Marca:"/"Visite a loja" prefixes stripped), `availability`/`in_stock` (`#availability`), `image`/`images` (`#landingImage` hi-res + `#altImages`), `features` (the "Sobre este item" `#feature-bullets`), and `specs` (the detail/tech `<table>` + `#detailBullets_feature_div` lifted **best-effort** — a missing container just yields fewer keys).

Prices are PT-BR comma-decimal (`R$ 879,00`); ratings are isolated from the "X de 5" label before the comma→dot conversion (so they don't route through the dot-stripping money parser). Products in `quality.products` (`page_type` search/product, `currency`, `result_count`); own envelope shape (`mode_used="amazon"`, `content_type="application/x-amazon"`). Rot contract: no `s-search-result` cards **and** no results container (search) or no `#productTitle` (product) → `AdapterParseError` → `PARSE_FAILED`; a results container present but holding zero cards → `success` + `["no_results"]`.

## `vasco/adapters/petlove.py`

Petlove BR pet-supplies marketplace (`petlove.com.br`; other hosts out of scope). **Two page types** — **search** (`/busca?q=`) and **product** (`/<slug>/p`); category/brand/editorial URLs are deliberately **not** matched by `is_petlove_url` (the URL alone can't tell a listing category from an article, and a non-listing page must not become an adapter failure), so they fall through to a normal fetch. HTML via the shared escalation chain (injected `fetch_html`); Petlove sits behind Cloudflare's "Just a moment…" interstitial (the http tier gets a 403 challenge) so `petlove.com.br` is seeded to the **browser** tier in `vasco/strategy.py` (like OLX). **schema.org JSON-LD is the spine** (server-rendered for SEO, survives Nuxt's CSS rotation):

- **Search** (`/busca`): the `ItemList` is the anchor — its `itemListElement` is the list of result `Product` objects (title, url, sku, price — **en-format dot-decimal, not BR comma** — currency, brand, availability, image), and its `description` carries the catalogue `total_count` ("… com 69 produtos disponíveis."). The same Products are also emitted as standalone blocks, used as a fallback when the `ItemList` wrapper is absent.
- **Product** (`/<slug>/p`): the `ProductGroup` is the anchor (Petlove sells one product in several sizes) → one product carrying `product_id` (`productGroupID`), brand, `aggregateRating` (`reviewCount`, not `ratingCount`; the full-precision mean is rounded to 2 dp), category (from `BreadcrumbList`, "Home" crumb dropped), HTML-stripped description, a `variants` list (the **multiple size/price pairs** — per-size `sku`/`size`/`price`/`in_stock`/`url`/`image` from `hasVariant`), `price`/`price_max` (the variant range), and embedded `reviews` (author/rating/title/text/date, capped at `adapters.petlove.max_reviews`, default 10). A single-size item with no group falls back to a plain `Product`. Two fields the JSON-LD doesn't carry are lifted **best-effort from the rendered DOM** (never fatal if the markup moves): `specs` (the `section.product-specifications` `.properties__list` name/value table) and `list_price` (the struck "preço cheio" for the selected variant — the regular price is already the JSON-LD `price`).

Products in `quality.products` (`page_type` search/product, `currency`, `result_count`, plus `total_count` on search); own envelope shape (`mode_used="petlove"`, `content_type="application/x-petlove"`). Rot contract: no `ItemList`/`Product` (search) or no `ProductGroup`/`Product` (product) → `AdapterParseError` → `PARSE_FAILED`; an `ItemList` present but holding zero items → `success` + `["no_results"]`.

## `vasco/adapters/steam.py`

Steam store (`store.steampowered.com`; other Steam hosts — `steamcommunity.com`, `steamdb.info` — are out of scope and fall through). Unlike the marketplace adapters it doesn't scrape page HTML: it fetches Steam's **public JSON APIs directly** through the injected `fetch_html` (like Shopify), so it serves on the plain http tier — **no strategy seed, no probe** (fixed domain, no bot challenge on the data path). Two page types: **app** (`/app/<id>`) and **search** (`/search/?term=`); bundle/sub/dlc/community URLs aren't claimed (`_claim` → `None`). Region (`cc`/`l`) comes from `SteamCfg` (`country`/`language`, default US/english), setting the price currency + description locale.

- **App** (`/app/<id>`): the storefront `appdetails` API (`/api/appdetails?appids=<id>`) is the **spine/anchor** — price (integer cents → float), genres, `early_access` (a boolean from Steam's "Early Access" **genre id 70** — stable across locales, unlike the localized description), metacritic, release date, platforms, developers/publishers, dlc count, recommendations. Enriched **best-effort** (concurrent `asyncio.gather`, failures swallowed) by the public `appreviews` endpoint — the summary (`review_score_desc`, `total_reviews`/positive/negative) **plus up to `adapters.steam.max_reviews` individual review bodies** (default 10; `reviews[]` = `author`, `recommended` (the `voted_up` thumb), `text`, `language`, `playtime_hours` at review time, `votes_up`/`votes_funny`, `early_access` (written during EA), `date`; `language=all` so the summary totals stay full, `max_reviews: 0` keeps summary-only) — the live `GetNumberOfCurrentPlayers` count (`api.steampowered.com`, on by API design, no key), and — when an ITAD key is configured — IsThereAnyDeal historical pricing (`historical_low` + recent `price_history` + `itad_url`; see below). Only `appdetails` can fail the fetch.

### ITAD price-history enrichment (`vasco/adapters/itad.py`)

When `adapters.steam.itad_api_key` (or `VASCO_ITAD_API_KEY`) is set, Steam **app** pages are enriched with **Steam-only** historical pricing from the official [IsThereAnyDeal API v2](https://docs.isthereanydeal.com): a lookup (`GET /games/lookup/v1?appid=`) resolves the appid → ITAD game id, then `POST /games/storelow/v2` (`shops=61`) gives the all-time-low Steam price and `GET /games/history/v2` (`shops=61`) the recent price-change log. **The API key is the only knob** — its presence is the enable switch (no key = off). `shops=61` (Steam) is intrinsic; the `country` follows `adapters.steam.country` (e.g. US → USD, BR → BRL) so the historical low's currency matches the displayed store price; the recent price-log depth is fixed. The client is a thin httpx wrapper (the injected `fetch_html` is GET-only and can't carry the storelow POST body) and **never raises** — no key / transport error / a game ITAD doesn't track all degrade silently to store-only data. The call is scheduled in the same `gather` as the Steam fetches and short-circuits with **zero network** when no key is set. Adds `historical_low {price, currency, cut, regular_price, date}`, `price_history [{date, price, cut, currency}, …]` (most-recent first), and `itad_url` to the product. **Key must be in config for vascod** (the daemon doesn't inherit the shell/MCP env — same caveat as `answer.api_key`).
- **Search** (`/search/?term=`): the `storesearch` API (`/api/storesearch/?term=`) → a list of app cards (title, app_id, price, metascore→`metacritic`, platforms, image).

Rot contract: broken/non-JSON `appdetails` or a search response with **no `items` array** → `AdapterParseError` → `PARSE_FAILED`; an `appdetails` node with `success: false` (delisted/nonexistent appid — valid shape, no store page) → **`NOT_FOUND`** (not rot); a search with an empty `items` array → `success` + `["no_results"]`. One product per app page in `quality.products` (`page_type` app/search, `currency`, `result_count`, `app_id`/`query`); own envelope shape (`mode_used="steam"`, `content_type="application/x-steam"`).

## `vasco/adapters/phabricator.py`

Wikimedia Phabricator (Phorge) tasks + task search (`phabricator.wikimedia.org`, extensible to other public Phorge instances via `cfg.adapters.phabricator.domains`). Phabricator is **server-rendered HTML** on the plain http tier (no JS app, no bot challenge) — like Steam/Shopify it needs **no strategy seed and no probe** — but unlike them it **scrapes the HTML** rather than a JSON API (the Conduit API requires an auth token; anonymous calls get `ERR-INVALID-SESSION`, so this is an intentionally unauthenticated scraper that can only read **public** data — a safety property: prompt injection can't escalate to restricted tasks). HTML is obtained through the shared escalation chain via the injected `fetch_html`. Two page types:

- **Task** (`/T<id>`): the `og:title` meta (`"T<id> <title>"`) is the structural anchor. Parses status/priority (the header subheader tag, folding Phabricator's "Closed, Resolved" form into `status="Resolved"`), the description (the property-list `.phabricator-remarkup`, converted to Markdown — links/lists/code/blockquotes preserved), author/assignee/tags/subscribers (the curtain panels), comments **with metadata** (`id`, `author`, `timestamp`, `text` — only `transaction-comment` timeline shells, capped at `cfg.adapters.phabricator.max_comments`, default 50), and related objects (mentioned-in/here, subtasks, parents, duplicates — every `dt`/`dd` property-list pair carrying object handles, keyed by a slug of its label). The single task object lands in `quality.task` (`result_count: 1`).
- **Search / list** (`/search/?query=…&types=TASK` or `/maniphest/?…`): the `ul.phui-oi-list-view` object-item list is the anchor → a list of `{id, name, title, url, status, snippet}` in `quality.tasks` (`query`, `result_count`). Both endpoints serve over **GET** (a read needs no CSRF token); non-task object-items in a mixed result set are filtered out. Global `/search/` yields per-result status + snippet; Maniphest list status is best-effort (absent when the page only exposes it via the Javelin tooltip metadata).

A **restricted** task (anonymous users are redirected to the login wall or get the policy-exception page) is surfaced as a clear **`LOGIN_REQUIRED`** failure with an actionable message — never a misleading `PARSE_FAILED`. The auth-wall markers (`you do not have permission` / `you shall not pass` / the login form / a `<title>Login</title>`) are checked **only on an anchor-less page**, so a public task whose comment text merely discusses permissions never false-fires. Own envelope shape (`mode_used="phabricator"`, `content_type="application/x-phabricator"`).

Rot contract: a task page with no `og:title` `T<id>` anchor that is *not* an auth wall, or a search page with no `phui-oi-list-view` container → `AdapterParseError` → `PARSE_FAILED`; a 404 (nonexistent task) → **`NOT_FOUND`** (the chain classifies the status); a search whose container is present but holds zero task items → `success` + `["no_results"]`. Modeled on Eric Gardner's [`mcp-phabricator`](https://gitlab.wikimedia.org/egardner/mcp-phabricator) scraper backend.

## `vasco/adapters/gitlab.py`

GitLab projects, issues, and merge requests (`gitlab.com` + any self-hosted instance). It fetches the site's **public JSON API** (`/api/v4`, GET, no token for public data) rather than scraping the Vue SPA. Unlike Steam/Shopify (which ride the injected `fetch_html`), it uses its **own minimal-header httpx client** (like the ITAD client): vasco's escalation-chain "modern-Chrome" headers (`Sec-Fetch-*` etc.) trip self-hosted GitLab WAFs into a **403** (a minimal GET sails through), and a JSON endpoint gains nothing from the browser tier (which would wrap it in Firefox's JSON viewer) or the wayback tail — so it takes no `fetch_html` and never touches the browser pool. Scope is intentionally the **"not in git" layer**: no code/tree/commit/pipeline fetching (that's `git`'s job). Three page types, claimed by URL shape (the project path is everything before GitLab's `/-/` separator, nested groups allowed; it is URL-encoded as the API `:id`):

- **Project** (a bare `/<namespace>/<project>` path): `/api/v4/projects/<enc>?license=true` is the **spine/anchor** (`id` + `path_with_namespace`) → name, description, `star_count`, `forks_count`, `open_issues_count`, topics, license, `default_branch`, visibility, `last_activity_at`. Enriched **best-effort** with the rendered README — the project's `readme_url` (`…/-/blob/<branch>/<file>`) is rewritten to the raw route (`…/-/raw/…`) and included verbatim (capped at 20k chars); a README miss never fails the fetch.
- **Issue** (`/-/issues/<iid>`) / **Merge request** (`/-/merge_requests/<iid>`): the `/projects/<enc>/issues|merge_requests/<iid>` object is the anchor (`iid` + `title`) → state, author, labels, dates, vote/note counts (MR adds source/target branch, `merge_status`, `draft`). Comments come from the `…/<iid>/notes` endpoint **best-effort** (concurrent `asyncio.gather`; system notes dropped; capped at `cfg.adapters.gitlab.max_comments`, default 20) — a non-array body (some instances gate the notes API anonymously → `{"message": "401 …"}`) yields zero comments, not a failure.

Host coverage uses a **Shopify-style probe** (the user chose "detect any host"): `gitlab.com` ∪ `cfg.adapters.gitlab.domains` are *known* (served directly); a claimable URL on an **unknown** host is **probed** (the API call doubles as the probe) and the verdict is persisted in the `adapter_probe` table — **keyed by full host**, not registered domain, since a GitLab instance is subdomain-specific (`gitlab.wikimedia.org` is GitLab, `www.wikimedia.org` is not; `cache purge <eTLD+1>` clears these host-scoped verdicts too). Valid GitLab JSON confirms the host (memoized True); a **non-JSON** body marks it not-GitLab (memoized False); anything ambiguous (a JSON 404 / non-object) raises `NotGitLab` → the dispatcher **falls through to a normal fetch**, so a non-GitLab lookalike is never a failure. The branch runs **after** Shopify (a bare `/a/b` shape overlaps Shopify's `/products/x` candidate). `autodetect: false` disables probing (known hosts only). Own envelope shape (`mode_used="gitlab"`, `content_type="application/x-gitlab"`); `quality` carries `provider`/`page_type`/`host`/`result_count` (always 1 — a detail page) plus the `project`/`issue`/`merge_request` object and `comments`.

Rot contract (on a *known* host): a non-JSON API body → `PARSE_FAILED`; a JSON 404 / not-an-object body → **`NOT_FOUND`** (project/issue/MR absent or private); a parsed object → `success`. Authentication is never sent, so a private project simply 404s (honest `NOT_FOUND`), never an escalation surface.

## Verification recipes

```bash
uv run vasco fetch "https://www.vivareal.com.br/aluguel/sp/sao-carlos/" \
  | jq '.mode_used, .quality.provider, .quality.page_type, .quality.result_count, .quality.listings[0]'
# Real-estate adapter: normalized listings in quality.listings; routes vivareal by domain,
# list vs detail by URL (detail pages add the full photo gallery)

uv run vasco fetch "https://www.google.com/search?udm=28&q=kindle+paperwhite" \
  | jq '.mode_used, .quality.result_count, .quality.filtered, .quality.products[0:3]'
# Google Shopping adapter: structured products in quality.products, drops by reason in quality.filtered

uv run vasco fetch "https://www.olx.com.br/autos-e-pecas/carros-vans-e-utilitarios/estado-sp" \
  | jq '.mode_used, .quality.vertical, .quality.page_type, .quality.result_count, .quality.listings[0].attributes'
# OLX adapter: real-estate (/imoveis/) + vehicle (/autos-e-pecas/) verticals only; per-vertical typed
# fields in quality.listings[].attributes. Cloudflare-protected → starts at the browser tier (seeded).

uv run vasco fetch "https://lista.mercadolivre.com.br/notebook" \
  | jq '.mode_used, .quality.page_type, .quality.result_count, .quality.products[0]'
uv run vasco fetch "https://www.mercadolivre.com.br/<slug>/p/MLB43417665" \
  | jq '.mode_used, .quality.page_type, .quality.products[0] | {title,price,condition,seller,sold_quantity,attributes}'
# MercadoLivre BR adapter: JSON-LD spine (search @graph → many products; product page → one rich product),
# best-effort PDP extras (seller/sold_quantity/installments/original_price/attributes). Bot-challenged → browser tier (seeded).

uv run vasco fetch "https://pt.aliexpress.com/w/wholesale-kindle-paperwhite.html" \
  | jq '.mode_used, .quality.result_count, .quality.products[0]'
uv run vasco fetch "https://pt.aliexpress.com/item/1005008760568743.html" \
  | jq '.mode_used, .warnings, (.quality.products[0] | {product_id,title,price,rating,review_count,rating_histogram,reviews})'
# AliExpress adapter: search parses rendered card-out-wrapper cards (no embedded JSON); product spine = URL
# product_id + open reviews endpoint. baxia/x5sec-walled → browser tier (seeded); needs a warm persistent
# profile holding x5secdata clearance. A walled PDP still returns id + reviews with a "page_blocked" warning.

uv run vasco fetch "https://simwooddenim.com/collections/jeans" \
  | jq '.mode_used, .quality.page_type, .quality.result_count, .quality.currency, .quality.products[0]'
uv run vasco fetch "https://simwooddenim.com/products/ls07-14-2oz-elastic-washed-vintage-jeans" \
  | jq '.quality.products[0] | {title, price, original_price, available, variants: (.variants|length)}'
uv run vasco fetch "https://simwooddenim.com/search?q=jeans" | jq '.quality.page_type, .quality.query, .quality.result_count'
# Generic Shopify adapter: fetches platform JSON endpoints (.js / products.json / suggest.json) — works on
# ANY Shopify store. Known domains (seed + cfg.adapters.shopify.domains) dispatch directly; unknown product/collection
# URLs are auto-probed and fall through to a normal fetch on a miss (search ≤10, the predictive-API cap).
uv run vasco fetch "https://www.allbirds.com/products/mens-tree-runners" | jq '.mode_used, .quality.provider'
# Auto-probe: an unknown Shopify store (zero config) → shopify envelope; a non-Shopify lookalike → normal fetch.

uv run vasco fetch "https://shopee.com.br/<slug>-i.<shopId>.<itemId>" \
  | jq '.mode_used, .quality.page_type, (.quality.products[0] | {title,price,currency,condition,brand,rating,review_count,seller,category,shop_id,item_id})'
# Shopee BR adapter: Product JSON-LD spine (price is dot-decimal; rating/count are strings). Product pages
# only — search/category URLs fall through to a normal fetch. Bot-challenged → browser tier (seeded).

uv run vasco fetch "https://www.amazon.com.br/s?k=kindle+paperwhite" \
  | jq '.mode_used, .quality.result_count, (.quality.products[0] | {asin,title,price,original_price,rating,review_count,url})'
uv run vasco fetch "https://www.amazon.com.br/dp/B0CFPL6CFY" \
  | jq '.mode_used, .quality.page_type, (.quality.products[0] | {asin,title,price,rating,review_count,brand,in_stock,features:(.features|length)})'
# Amazon BR adapter: rendered-DOM spine (no JSON-LD) — search s-search-result cards; product #productTitle.
# Serves on the http tier (NOT seeded). Search + product pages only; non-listing URLs fall through. The
# homegrown robot/captcha wall → BLOCKED_CAPTCHA (chain escalates http → browser), not a misleading PARSE_FAILED.

uv run vasco fetch "https://www.petlove.com.br/busca?q=racao+golden" \
  | jq '.mode_used, .quality.result_count, .quality.total_count, (.quality.products[0] | {title,price,brand,sku})'
uv run vasco fetch "https://www.petlove.com.br/<slug>/p" \
  | jq '(.quality.products[0] | {title,product_id,price,price_max,list_price,rating,review_count,category,
        variants: [.variants[] | {size,price,in_stock}], specs, n_reviews: (.reviews|length)})'
# Petlove BR adapter: JSON-LD spine (ItemList → search products; ProductGroup → one product with the
# multiple size/price pairs in .variants + reviews); specs + struck list_price come best-effort from the
# rendered DOM. Search + product pages only. Cloudflare-walled → browser tier (seeded).

uv run vasco fetch "https://store.steampowered.com/app/1145360/Hades/" \
  | jq '.mode_used, .quality.page_type, (.quality.products[0] | {title,price,currency,early_access,metacritic,review_score_desc,total_reviews,player_count,genres,reviews:(.reviews[0])})'
# early_access (genre id 70) + up to adapters.steam.max_reviews review bodies (author/recommended/text/playtime_hours/date).
uv run vasco fetch "https://store.steampowered.com/search/?term=hades" \
  | jq '.mode_used, .quality.result_count, (.quality.products[0] | {title,app_id,price,metacritic})'
# Steam adapter: fetches Steam's public JSON APIs directly (appdetails spine + best-effort appreviews +
# live player count; storesearch for search). No seed/probe — JSON serves on http. Invalid appid → NOT_FOUND.

# With an ITAD key configured (adapters.steam.itad_api_key / VASCO_ITAD_API_KEY), Steam app pages add historical pricing:
VASCO_ITAD_API_KEY=<key> uv run vasco fetch "https://store.steampowered.com/app/1145360/Hades/" \
  | jq '.quality.products[0] | {price, currency, historical_low, price_history, itad_url}'
# → all-time-low Steam price + recent price-change log (Steam-only, shops=61; currency from adapters.steam.country).
# Best-effort: no key / game not on ITAD → fields simply absent (store data unaffected).

uv run vasco fetch "https://phabricator.wikimedia.org/T241180" \
  | jq '.mode_used, (.quality.task | {id, title, status, priority, author: .author.username,
        tags: [.tags[].name], n_comments: (.comments | length), related: (.related | keys)})'
uv run vasco fetch "https://phabricator.wikimedia.org/search/?query=parsoid+timeout&types=TASK" \
  | jq '.quality.page_type, .quality.query, .quality.result_count, (.quality.tasks[0] | {id, status, title})'
# Phabricator adapter: task pages → structured task in quality.task (status/priority/author/assignee/tags/
# subscribers/description/comments-with-metadata/related); /search & /maniphest → quality.tasks. Public data
# only (HTML scrape, no Conduit token) — a restricted task returns a LOGIN_REQUIRED failure, not PARSE_FAILED.

uv run vasco fetch "https://gitlab.wikimedia.org/egardner/mcp-phabricator" \
  | jq '.mode_used, (.quality.project | {path_with_namespace, star_count, forks_count, default_branch, license})'
uv run vasco fetch "https://gitlab.com/gitlab-org/gitlab/-/issues/1" \
  | jq '.quality.page_type, (.quality.issue | {iid, state, author, labels}), (.quality.comments | length)'
uv run vasco fetch "https://gitlab.com/gitlab-org/gitlab/-/merge_requests/1" \
  | jq '.quality.merge_request | {iid, state, source_branch, target_branch, merge_status}'
# GitLab adapter: public /api/v4 JSON (no auth). Self-hosted hosts auto-detected via a persisted probe
# (gitlab.com + cfg.adapters.gitlab.domains skip it). Project pages include the README; issues/MRs include
# best-effort comments. A non-GitLab /a/b lookalike falls through to a normal fetch (never a failure).
```
