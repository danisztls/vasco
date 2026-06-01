# bypass-paywalls-chrome vs vasco (2026-06-01)

Evaluated the Bypass Paywalls browser extension for techniques vasco could absorb.
Two codebases examined:

- **`magnolia1234/bypass-paywalls-chrome-clean`** (via `csns1` mirror), MV2 v2.7.1.1,
  ~8.7k LOC — the actively-maintained "clean" fork. `sites.js` = 373 sites as a
  declarative per-site technique table over a generic `webRequest` engine.
- **`iamadamdev/bypass-paywalls-chrome`** v1.8.1 — the older original. Flat 171-line
  name→domain map; a strict subset of the magnolia1234 vendor coverage. Adds nothing.

The whole technique vocabulary is just 14 keys, overwhelmingly **paywall bypass**:
`block_regex` (block the publisher's own paywall JS, 214×), `allow_cookies` (default:
strip the `Cookie` header to reset metered counters, 243×), `useragent` (spoof
Googlebot/bingbot, 61×), `referer` (spoof google/facebook/twitter, 10×),
`remove_cookies_*` (drop counter cookies, 15×), `random_ip` (random `X-Forwarded-For`).

## Verdict: absorb the *detection* asset; do NOT absorb the *bypass* techniques

### ✅ Tier A — ABSORBED: paywall detection as a quality signal

The one piece worth taking is **not** a bypass — it's the vendor fingerprint list. The
manifest `permissions` + `block_regex` patterns enumerate every paywall-SaaS vendor.
vasco now uses that list to *detect* (not defeat) paywalls and emit `quality.paywalled`
+ `quality.paywall_vendor`, so a research agent can fall back to Wayback or skip a
truncated stub.

Implemented (2026-06-01): `vasco/quality/paywall.py` (loader mirrors `fetch/netblock.py`;
pure `detect_paywall(raw_html, vendors)` substring scan) + bundled
`vasco/quality/data/paywall_vendors.txt` + a new `raw_html=` arg threaded into
`quality.score()` (vendor scripts live in raw HTML, which trafilatura strips). Config:
`quality.detect_paywall` (default on), `quality.paywall_vendor_paths`.

Caveat baked into the signal's meaning: it's **site-level** (the site meters access),
not proof this URL is gated. To minimize false positives the bundled list is limited to
**dedicated** paywall/subscription/metering vendors (Piano/tinypass, Poool, Zephr, Qiota,
Pelcro, Wallkit, Flip Pay, Evolok, OneCount, Tribune DSS). General-purpose
analytics/CDP/tag managers the upstream extension also blocks (BlueConic, Cxense, Mather,
Ensighten) were **deliberately excluded** — they appear on many non-gated sites. Extend
via `quality.paywall_vendor_paths` if broader coverage is wanted.

### 🔴 Tier C — NOT IMPLEMENTED (this is the part to remember as "rejected")

These are the bypass techniques. They circumvent access controls, generally violate site
ToS, and raise copyright / CFAA / contract questions depending on use. Deliberately left
out; documented here so they aren't re-proposed.

1. **Crawler-UA + referer + XFF escalation tier.** Retry gated pages as
   `User-Agent: Googlebot` (or bingbot/msnbot) + `Referer: https://www.google.com/` +
   `X-Forwarded-For: 66.249.66.x`, exploiting publishers' SEO/"first-click-free" allowances.
   Could in principle slot into `fetch._do_fetch_html` as an opt-in tier after browser,
   before wayback. **Not building it because:**
   - it's paywall circumvention (ToS/legal exposure);
   - it's *decreasingly effective* — claiming Googlebot from a non-Google IP fails the
     reverse-DNS verification many publishers now run;
   - it contradicts vasco's stealth goal (present a *real* browser fingerprint), and
     spoofing a crawler is the opposite of blending in.

2. **Cookie stripping / selective cookie drop** (blank the `Cookie` header or drop named
   counter cookies like `TDNotesRead` to reset metered-paywall counts). Not building.

3. **Blocking the publisher's own paywall JS** (`block_regex` against first-party
   scripts). **Explicitly rejected** — it crosses the exact line `vasco/fetch/netblock.py`
   deliberately holds: netblock blocks **third-party trackers only and never first-party
   resources**. Blocking a site's own enforcement script would violate that invariant.

## Recommendation

Keep vasco a paywall **detector**, not a **bypasser**. The Wayback tier
(`vasco/adapters/wayback.py`) remains the sanctioned fallback for gated content. If a
bypass tier is ever reconsidered, it must be opt-in, off by default, and clearly scoped
to authorized/lawful use — and even then the reverse-DNS reality makes the Googlebot
trick low-ROI.
