# TODO

## OLX list pages fail with `parse_failed`

**Status:** open, not fixed. Found 2026-11-02 while testing the Camoufox 0.5.9 / Firefox 158 bump.

**Symptom:** OLX list pages return `parse_failed` with the message `olx list page: <script id='__NEXT_DATA__'> not found — site structure may have changed`.

Reproduce:

```bash
uv run vasco fetch "https://www.olx.com.br/imoveis/venda/estado-sp/sao-paulo-e-regiao/sao-paulo" --refresh --json | jq '.failure'
```

**Likely cause:** OLX moved these list pages to the Next.js App Router, which the category hubs already use. `_is_category_hub` in `vasco/adapters/olx.py` doesn't match them, so they reach the `__NEXT_DATA__` anchor in the list parser and fail there.

**What the browser tier receives for the URL above:**

- A full page of about 1 MB with status 200, and no wall or challenge markers.
- `self.__next_f` present, `__NEXT_DATA__` absent.
- 50 `listId` occurrences, so the listing data appears to be in the page.
- 8 JSON-LD blocks, including a `Product` type.

**Timeline:** this URL returned 57 listings on 2026-06-02. The last OLX success of any kind in the logs is 2026-07-07, and there is no OLX traffic between then and the bump, so the break could date from any point in that window.

**Not verified:**

- Firefox 158 is ruled out only by inference, because the page content points to a site change. There was no side-by-side run on Firefox 152, and the plain HTTP tier gets `blocked_cloudflare`, so it can't confirm this.
- Only `/imoveis/` list pages were tested. `/autos-e-pecas/` list pages are unknown.
- Detail pages are unknown. The one replayed returned `not_found`, which may just mean the ad was removed.

**Starting points for a fix:**

- Parse the `self.__next_f` payload, or the JSON-LD if it carries enough fields.
- Add a `tests/fixtures/` capture of the new page shape.
- Update the OLX anchor in the CLAUDE.md invariants and in `docs/adapters.md`.

## Test Serper as a search backend

**Status:** to do. Needs a Serper API key.

**Question to answer:** does Google find real pages for the queries `ddgs` can't answer? If yes, add Serper as a backend. If no, skip it.

**Evidence so far (2026-11-02):**

- Empty searches rose from 0.2% in May to 32% in October (365 of 1,135), going by the telemetry log's `empty` outcome.
- In a replay of 40 October empties, 26 returned results on retry and 14 failed both attempts. Those 14 are mostly long, quoted or very specific queries, several in less common languages, and some may have no good answer anywhere.
- Bing doesn't help with them: over HTTP and in the real browser, it mostly returned unrelated pages for this kind of query.

**How to test:**

- Replay the queries that still come back empty after the retry, plus a sample that work, against Serper and `ddgs`. Compare how many get results, which domains appear, and pt-BR coverage.
- Telemetry's `empty` rows hold the query and site, so the replay set can come from there.

**If it wins:** add it behind the `Searcher` interface in `vasco/search.py`, falling back to `ddgs` when it fails or returns nothing, or when no key is set. The key goes in `config.yaml`, because vascod doesn't see the shell environment. Check current pricing first, since the figures found so far date from April 2026.

## Maybe: Brave Search API as a backend

**Status:** a possibility, not decided. Needs a Brave API key.

Brave has its own index, so it would be the one source independent of Bing and Google. It was removed from `_DDGS_TEXT_BACKENDS` on 2026-11-02 because scraping it through `ddgs` got rate limited (429) and returned nothing when tested, so the API is the way back to it.

**Weigh first:** a pricing comparison from April 2026 calls its coverage narrower than Google's, which is the same weakness that got Tavily dropped. Include it in the Serper replay if a key is available.
