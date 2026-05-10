# MMT-CLI Build Learnings

What we tried, what worked, what didn't, and why. Captured for future
hotel-OTA scrapers (Agoda, Booking, Goibibo) and for anyone who later
asks "why didn't we just hit the API?"

## TL;DR

- MMT's hotel-listing API is gated by Akamai Bot Manager. Replay fails.
- The listing **page** is fully SSR-rendered. DOM-scrape works.
- Strikethrough prices are filtered using `getComputedStyle().textDecoration`.
- Hotel IDs live in the `?hotelId=` query string of the detail anchor,
  not in the URL path as initial inspection suggested.
- Akamai IP-flags get worse the more you hammer it. Build with the
  offline fixture; only burn live reqs at the end.

## What we tried before settling on DOM scrape

### 1. Direct API replay (HAR-derived urllib)
**Result:** 6-byte `"200-OK"` response.
**Why:** Akamai's `_abck` cookie was at 531 bytes (bootstrap). The
endpoint returns the sentinel until `_abck` reaches ~799 bytes via the
`bm_sv` sensor flow.

### 2. Playwright + harvest + replay
**Plan:** drive Firefox to homepage → hotels landing → listing, harvest
the now-validated cookies, replay the API call with `urllib`.
**Result:** still 6-byte sentinel. `_abck` stayed at 531 bytes even
after several minutes of headed browsing. Akamai's sensor never
fired in our test sessions.

### 3. `page.evaluate(fetch)` from inside the browser context
**Plan:** make the same XHR call from inside Firefox's JS runtime, so
the request carries the real browser fingerprint.
**Result:** still 6-byte sentinel.
**Hypothesis:** Akamai is not just checking cookies — it's checking
that the XHR was triggered by the page's own JS, not by an external
caller. The `bm_sv` sensor probably encodes a chain-of-trust signal
that's broken when an outside agent invokes `fetch()`.

### 4. Headed-mode discriminator (`DISPLAY=:0`)
**Plan:** confirm whether headless detection is the gate. Run the same
flow with `headless=False` against a real X server.
**Result:** identical to headless. `_abck` stayed at 531b in both
modes. **Conclusion:** head/headless is not the gate; some combination
of IP reputation, ASN, fingerprint, and sensor-trigger timing is.

### 5. WebKit + Chromium engines
**Result:** all three engines hit the same Akamai gate. Engine
diversity does not bypass it. (Multi-engine fallback is still useful
for resilience against per-engine bugs, just not for Akamai bypass.)

### 6. The pivot: DOM-scrape feasibility test
**Plan:** stop fighting the API. Just `page.content()` the listing and
see if the hotel data is in the rendered HTML.
**Result:** 788KB of HTML. Hotel cards present under
`[class*="listingRow"]`. 15+ cards rendered before any scrolling.
**This is the path we shipped.**

## Engineering nuances that bit us

### Strikethrough prices

MMT shows discount markdowns as struck-through prices next to the
real displayed price. Naïvely grabbing the first `₹` token in card
text picks up the struck-through value. Result: clients receive the
*pre-discount* price during onboarding, which is wrong on its face
and obviously wrong against the live site. This was the original
business motivation for the project.

Fix: `getComputedStyle(node).textDecoration.includes('line-through')`
filter. We collect strikethrough nodes' text into a separate
`strikethroughPrices[]` for audit, then iterate the card's price
candidates and skip any that match.

The `validate_offline.py` test asserts no strikethrough leaks into
the displayed price. Don't ship without that test passing.

### Junk in `name` field

First passes captured Spotlight tooltip prose ("This property is part
of the MakeMyTrip Spotlight program") as the hotel name on cards
where the tooltip rendered ahead of the headline.

Fix: `isJunkName()` filter that rejects:
- length < 3 or > 100
- pure-digit or pipe-only tokens
- "Image N", "N Photos & Videos"
- "This property is part of"
- "Spotlight program"

### Price false positives

"Book with ₹0 Payment" badge was being picked up as `price='₹0'`.

Fix: regex requires ≥3 digits AND `parseInt ≥ 100`:
`(?:₹|Rs\.?|INR)\s*([\d][\d,]{2,})`. Single-digit and double-digit
prices don't exist on MMT in practice.

### Rating extraction (split tokens)

Rating renders as adjacent inline elements: `<span>Excellent</span>
<span>4.5</span>`. `innerText` joins with whitespace; a single
`Excellent\s*([\d.]+)` regex sometimes matches and sometimes doesn't
depending on hydration timing.

Fix: tokenize the card text on whitespace, then for each label token
(`Excellent`, `Very Good`, `Good`, `Fair`, etc.) check if the **next**
token is `^\d\.\d$`. This is more robust than the regex against
any DOM-text-flattening quirk.

### Review count

Same split-token issue: `(11 Ratings)` may render as `(`, `11`,
`Ratings`, `)` across nodes. The text-collapsed regex
`/\(\s*([\d,]+)\s*Ratings?\s*\)/` against `fullText.replace(/\s+/g, ' ')`
handles both cases.

### HotelId

Initial assumption was `/hotel-details/{id}` in the URL path. **Wrong.**
The detail anchor uses a query string: `?hotelId=202402131726016131`.

Fix: try the query-string regex first, fall back to the path regex
for safety:
```js
let m = detailUrl.match(/[?&]hotelId=([^&]+)/);
if (!m) m = detailUrl.match(/\/hotel-details\/([^/?]+)/);
```

## Why we used a persistent browser context

`launch_persistent_context()` keeps cookies/storage between runs at
`/tmp/mmt-cli-pw-profile`. The first launch boots cold against
Akamai. Subsequent launches inherit the validated `_abck` and skip
the `bm_sv` sensor handshake entirely (when it works). This is also
what kept us from getting harder IP-flagged during development —
fewer fresh-cold-start sensor evaluations per day.

Side benefit: fast restart during iteration.

## Why we shipped with `firefox -> webkit -> chromium` fallback

Single-engine runs occasionally hit per-engine bugs (Firefox
networking timeouts, Chromium hydration races, WebKit missing
`libavif13` for AVIF image decode). The fallback chain keeps the
extractor running even if one engine misbehaves. Akamai gating is
**not** an engine-discriminator, but transient infra issues are.

## Akamai gating is per-URL, not per-session (proved 2026-05-09)

A persistent-context probe with `_abck=797` (validated state) was used to
fetch three MMT detail URLs in sequence:

- `sujan_the_serai-details-jaisalmer.html` → 670KB, full DOM, hotel data extracted
- `review-of-sujan_the_serai-details-jaisalmer.html` → 130-byte body containing
  the literal sentinel `<pre>200-OK</pre>` (6 bytes inside `<pre>`)
- `vividus-details-bangalore.html` → identical 130-byte sentinel response

Same context, same cookies, same session, same UA. Only URL 1 came back with
real content. Conclusion: Akamai's gate is URL-fingerprinted, not just
session-fingerprinted. A validated `_abck` is necessary but not sufficient;
some pages additionally require fresh sensor activity tied to *that* URL or
its referrer chain. Note: the `review-of-…-details-…` form is non-canonical
on MMT (canonical reviews path is `/hotels/reviews/<slug>.html`), which
likely contributes — but `vividus-details-bangalore.html` is a
canonical-shape URL and still hit the gate, so URL shape alone isn't the
explanation.

**Implication for detail-page enrichment:** can't rely on a single warm
context. Need either per-URL warm-up (homepage → search → click-through to
detail), longer cool-downs, or rotation. Document blocker, don't fabricate.

## Detail-page bypass attempts — all blocked (2026-05-09 late session)

Sustained extraction attempts on the same egress flipped the IP into
hard-throttle mode. Even the previously-working
`sujan_the_serai-details-jaisalmer.html` URL began returning the 6-byte
`200-OK` sentinel on a fresh fetch from the same persistent profile, and
the homepage warm-up (`https://www.makemytrip.com/`) started returning
`NS_ERROR_NET_INTERRUPT` mid-handshake — Akamai dropping the TCP
connection rather than answering with a sentinel page.

We tested every available bypass before giving up:

| Approach | Result |
|----------|--------|
| Local Playwright (warm persistent profile) | 130b sentinel `<pre>200-OK</pre>` |
| Local Playwright (fresh cold profile) | NS_ERROR_NET_INTERRUPT on homepage |
| `curl` with realistic UA | Akamai blocks by client fingerprint |
| Spider Cloud premium + stealth + IN residential proxy | 6-byte `200-OK` sentinel |
| Wayback Machine (archive.org) | No snapshots available for either URL |
| `m.makemytrip.com` (mobile subdomain) | DNS NXDOMAIN — subdomain doesn't exist |
| `/hotels/amp/...` (AMP variant) | 6-byte `200-OK` sentinel |
| Goibibo (same parent company, Akamai gate) | 8-byte sentinel |

**Verdict:** detail-page extraction is genuinely out of scope for the free
internal toolchain when the egress is in flagged state. The published
"What we will NOT do" list (Akamai sensor replay, commercial proxies as
in-house) holds. Three realistic paths remain when this matters:

1. **Cool down the egress** — wait 2–6h or rotate to mobile hotspot /
   residential VPN, then re-run with full per-URL warm-up chain.
2. **Pay for a vendor** — ScrapingBee or Bright Data with sensor-spoofing.
   ScrapingBee at $49/mo ≈ 100k requests; cost-justifiable only if
   onboarding volume is consistently high.
3. **Drop detail-page enrichment from MVP scope** — listing extraction
   already covers the core business problem (strikethrough corruption on
   copy-paste). Detail enrichment was always tagged "what we'd build next
   if we kept going."

The chosen path: stay with the listing-only MVP. Detail-page work is
parked behind a documented blocker, not pretended-resolved.

## Detail-page revisit — Playwright path works when curl_cffi doesn't (2026-05-11)

After the 2026-05-09 verdict above, we made one more pass on detail
extraction once the egress was no longer in hard-throttle. Two fetchers,
same parser, same warmup chain (`/` → `/hotels/`):

| Fetcher | Speed | Result on gdhotels.in seed (6 candidate URLs) |
|---------|-------|-----------------------------------------------|
| `batch_detail.py` (curl_cffi, chrome110/safari17_0) | ~1s/row | 1/6 on 2026-05-10; 0/6 next day after egress re-hardened (HTTP/2 INTERNAL_ERROR on warmup) |
| `batch_detail_pw.py` (Playwright Firefox + en-IN locale) | ~6s/row | 5/6 on 2026-05-11 — full `parse_initial_state` + `parse_jsonld_hotel` records extracted |

The 1 miss in the Playwright run was Triton Suites — its discovered URL
was an `amenities-of-…` interstitial, not a real detail page. Classifier
correctly flagged it `empty_record`; that's a discovery-side bug, not an
extraction failure.

**What this means for the verdict above:** the "all blocked" outcome
was egress-state-specific, not a permanent ceiling. Playwright with the
production warmup chain reproduces the listing-extractor's success
pattern on detail pages too — the listing page being SSR-rendered isn't
unique; detail pages are also SSR'd, the gate is just stricter. curl_cffi
TLS-impersonation works when fresh and dies first when Akamai hardens
the egress; the Firefox tab path takes longer to fall over.

Both paths are still **research-tier**, not production. Akamai can still
block Playwright sessions when the egress is sufficiently flagged
(symptoms: DOM stays under 500KB, `_abck` at ~531b). Recovery is
unchanged — cool down or change egress.

## What we'd build next if we kept going

- **Detail-page enrichment.** Research-tier scaffolding now exists
  (`mmt_detail.py`, `batch_detail.py`, `batch_detail_pw.py`). To
  productionize: discovery recall (currently ~25–30% on small chains),
  egress hardening (residential proxy or commercial backstop), and a
  retry/cooldown policy that doesn't keep hammering a flagged IP.
- **Multi-page pagination.** Current extractor stops at whatever
  renders on the first page. MMT shows ~30 cards per page; lazy-load
  triggers more on scroll. We trigger some lazy renders but not all.
- **Goibibo / Booking.com adapters.** Same `extract_*` pattern. Goibibo
  is also Akamai-gated (same parent company); Booking has its own
  bot manager (Imperva).
- **Cross-OTA price reconciliation.** Once we have N OTAs scraped on
  the same date, we can detect price-drift anomalies that suggest
  scraper drift.

## What we will NOT do

- **Solve Akamai sensor replay.** Out of scope for free internal
  tooling. Commercial residential proxies + sensor-spoofing are paid
  vendor territory; if onboarding volume justifies it later, evaluate
  ScrapingBee / Bright Data, not in-house.
- **Run continuous scheduled scrapes.** Anti-bot reputation is
  use-it-or-lose-it. One-shot, on-demand, per-onboarding. No cron.

## Useful artifacts

- `/tmp/mmt-replay/` — kitchen sink of dead ends from the API-replay
  phase (HAR captures, urllib probes, headed-mode tests). Worth
  keeping for the next time someone proposes API replay.
- `/tmp/mmt-replay/pw-out-dom/listing.html` — saved 788KB MMT listing
  used by `validate_offline.py`.
- `test-fixtures/offline_extract.json` — last passing offline
  extraction. Diff against this when you change `EXTRACT_JS`.
