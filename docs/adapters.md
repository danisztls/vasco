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

## `vasco/adapters/steam.py`

Steam store (`store.steampowered.com`; other Steam hosts — `steamcommunity.com`, `steamdb.info` — are out of scope and fall through). Unlike the marketplace adapters it doesn't scrape page HTML: it fetches Steam's **public JSON APIs directly** through the injected `fetch_html` (like Shopify), so it serves on the plain http tier — **no strategy seed, no probe** (fixed domain, no bot challenge on the data path). Two page types: **app** (`/app/<id>`) and **search** (`/search/?term=`); bundle/sub/dlc/community URLs aren't claimed (`_claim` → `None`). Region (`cc`/`l`) comes from `SteamCfg` (`country`/`language`, default US/english), setting the price currency + description locale.

- **App** (`/app/<id>`): the storefront `appdetails` API (`/api/appdetails?appids=<id>`) is the **spine/anchor** — price (integer cents → float), genres, metacritic, release date, platforms, developers/publishers, dlc count, recommendations. Enriched **best-effort** (concurrent `asyncio.gather`, failures swallowed) by the public `appreviews` summary (`review_score_desc`, `total_reviews`/positive/negative), the live `GetNumberOfCurrentPlayers` count (`api.steampowered.com`, on by API design, no key), and — when an ITAD key is configured — IsThereAnyDeal historical pricing (`historical_low` + recent `price_history` + `itad_url`; see below). Only `appdetails` can fail the fetch.

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

uv run vasco fetch "https://store.steampowered.com/app/1145360/Hades/" \
  | jq '.mode_used, .quality.page_type, (.quality.products[0] | {title,price,currency,metacritic,review_score_desc,total_reviews,player_count,genres})'
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
```
