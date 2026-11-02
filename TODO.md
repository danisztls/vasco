# TODO

## Bugs from the WebFetch comparison

**Status:** open, not fixed. Found 2026-11-02 at `c816c38` while comparing vasco with Claude Code's WebFetch. Causes aren't investigated unless stated.

### Reddit titles come back translated

`r/Python` post titles and bodies arrive in Portuguese, so an agent would quote text the original post doesn't contain. The cause is the `pt-BR` browser locale: the posts are marked `post-language="en"`, and `r/france` titles get translated the same way, while `r/brasil` stays in the original.

There's no fix through the URL. `?tl=en` left `r/Python` in Portuguese and translated `r/brasil` into English. The `.json` listing answers 403. Post permalinks keep the original language in their slug, but only as a slug.

**Decide:** the options are changing `browser.locale`, which could affect the pt-BR sites, or giving Reddit its own browser context with an English locale. A context only helps English posts, so translated posts in other languages would still need flagging, which `post-language` makes possible.

### Stack Overflow is blocked and loses the question and scores

A fresh fetch of `https://stackoverflow.com/questions/11227809` and of `https://stackoverflow.com/questions/231767` returned `blocked_cloudflare` at about 20:45 UTC on 2026-11-02, in the browser tier, though the first URL had fetched fine at 19:53 UTC. The cause of the change is unknown. When a fetch does get through, the Markdown starts at the answer, without the question body or the scores (`boilerplate_ratio` 0.94).

The keyless Stack Exchange API returned the title, score, answer count and question body for the first URL (`api.stackexchange.com/2.3/questions/<id>?site=stackoverflow&filter=withbody`). **Proposal:** an adapter like npm's. Its daily limit without a key and the answers endpoint are still untested, and other Stack Exchange sites could share it.

### Wikipedia drops the closing sections

The Wikimedia route ends at "Languages influenced by Python", without the article's References, Further reading and External links. This may be deliberate, so decide before changing it.

## Improve Reddit, Reuters and Associated Press

**Status:** to do. Vasco fetches all three, but each misses what an agent would ask for. The ideas below are untested unless stated.

- **Reddit:** `r/Python` extracts poorly (`trafilatura_confidence` 0.40, `boilerplate_ratio` 0.68), and listings have no structure. The rendered page has `shreddit-post` elements with title, score and language attributes, so an adapter can get structured posts. Fix the translation bug above with it.
- **Reuters:** `/technology/` returns story summaries, not headlines (`trafilatura_confidence` 0.45, `boilerplate_ratio` 0.76).
- **Associated Press:** articles are fine, with 878 words and `trafilatura_confidence` 1.0 through the browser tier. Hub pages aren't: `/hub/technology` returned 157 words with `boilerplate_ratio` 0.98, and its raw HTML holds 95 article links that extraction drops. The home page returned 150 words.
- **Shared approach:** Reuters and AP have the same news section page problem, so one approach may cover both. AP's `PagePromo-title` elements are a possible anchor there, but a generic one needs a design decision.

## Test more search backends

**Status:** blocked on Serper, which isn't accepting new registrations as of 2026-11-02. A Brave API key is available, so use Brave for the replay below instead.

**Question to answer:** does Brave find real pages for the queries `ddgs` can't answer? If yes, consider it as a fallback. Serper would answer the same question for Google, if it reopens.

**Evidence so far (2026-11-02):**

- Empty searches rose from 0.2% in May to 32% in October (365 of 1,135), going by the telemetry log's `empty` outcome.
- In a replay of 40 October empties, 26 returned results on retry and 14 failed both attempts. Those 14 are mostly long, quoted or very specific queries, several in less common languages, and some may have no good answer anywhere.
- Bing doesn't help with them: over HTTP and in the real browser, it mostly returned unrelated pages for this kind of query.

**How to test:**

- Replay the queries that still come back empty after the retry, plus a sample that work, against Brave and `ddgs`. Compare how many get results, which domains appear, and pt-BR coverage.
- Telemetry's `empty` rows hold the query and site, so the replay set can come from there.

**If it wins:** use it through the `Searcher` interface in `vasco/search.py`, falling back to `ddgs` when it fails or returns nothing, or when no key is set. Check current pricing first, since the figures found so far date from April 2026.

## Decide whether Brave should be more than opt-in

**Status:** a possibility, not decided. The backend works through `--backend brave`, tested against the live API on 2026-11-02.

**Weigh first:** a pricing comparison from April 2026 calls its coverage narrower than Google's, which is the same weakness that got Tavily dropped. The replay above answers this.

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
