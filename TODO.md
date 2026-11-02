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

## Decide whether Brave should be more than opt-in

**Status:** a possibility, not decided. The backend works through `--backend brave`, tested against the live API on 2026-11-02.

**Weigh first:** a pricing comparison from April 2026 calls its coverage narrower than Google's, which is the same weakness that got Tavily dropped. Include it in the Serper replay.

## Maybe: Yandex as an opt-in search backend

**Status:** a possibility, low priority. If built, it should be opt-in through `--backend yandex`, not part of the default search.

`ddgs` disables its Yandex engine, so it was removed from `_DDGS_TEXT_BACKENDS` on 2026-11-02. Calling the `ddgs.engines.yandex.Yandex` class directly returned 10 real results on each of 3 test queries, though one top result for an English query was Russian (`ozon.ru`).

**Size:** about 20 lines plus tests: a `yandex` branch in `get_searcher` and a direct call to the class, reusing the retry and the empty-URL filter. Call the class rather than adding it to `ddgs`'s engine registry, which would change global state.

**Limits:**

- It depends on `ddgs` internals, and the reason upstream disabled it is unknown.
- The engine ignores `region` and `time`, so `--time` would need to be rejected or documented.
- The `site:` filter is untested on its endpoint.

## Maybe: semantic ranking for `extract`

**Status:** a possibility, not installed. `rank="semantic"` is wired up, but the `semantic` extra isn't on the deployed tool. Install it only if `extract` keeps missing passages that are worded differently from the query. The BM25 tokenizer fix of 2026-11-02 removed the main reason agents reached for it.

**Measured cost (2026-11-02, CPU):**

- The default install downloads 3.06 GB across 41 packages, and 2.45 GB of that is NVIDIA CUDA libraries, which an AMD GPU can't use.
- A CPU-only `torch` takes 1.1 GB on disk, against 408 MB for the whole vasco tool today.
- The model adds 88 MB, downloaded from Hugging Face on first use.
- Peak memory was about 450 to 585 MB once loaded, and the daemon keeps the model until it restarts.
- With everything on disk, the first call took about 3s to import and load. Encoding 300 short passages took 0.4s.

**Before installing:**

- Force CPU-only `torch` with a CPU index under `[tool.uv.sources]`. This was tested with `uv pip` and index flags, not through `uv tool install`.
- Replace the default `all-MiniLM-L6-v2`, which is mainly suited to English. A multilingual model fits vasco's queries better, but its size is unmeasured.
- Move `_rank_semantic` off the event loop in `vasco/extract.py`. It runs directly in the async `extract`, so loading the model and encoding would block every other daemon request.

**Lighter option:** an ONNX library like `fastembed` should avoid `torch` altogether. It's untested and would mean changing `vasco/semantic.py`.
