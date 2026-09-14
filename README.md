# mmt-cli

A free, internal Python CLI that extracts hotel listings from [makemytrip.com](https://www.makemytrip.com/hotels/) for Simplotel client onboarding.

The live MakeMyTrip listing UI shows discount markdowns as struck-through prices; copy-pasting them into onboarding sheets grabs the wrong value. This tool extracts the post-filter price directly from the rendered DOM instead.

## What it does

1. Loads a MakeMyTrip listing URL in a real Firefox browser via Playwright.
2. Waits for hydration and scrolls to trigger lazy-rendered cards.
3. Extracts hotel cards from the DOM, using `getComputedStyle(node).textDecoration` to filter `line-through` nodes out of the displayed price. Struck-through prices are kept separately (`strikethroughPrices[]`) for audit.
4. Outputs structured JSON per card: `name`, `location`, `price`, `strikethroughPrices[]`, `rating`, `ratingLabel`, `reviewCount`, `hotelId`, `photoCount`, `badges[]`, `detailUrl`.

Status: the parser is validated offline (15/15 cards, 3/3 expected names, no strikethrough leaks). Live extraction works as long as the egress IP isn't Akamai-flagged.

## Install

```bash
pip install playwright playwright-stealth
playwright install firefox
```

## Usage

```bash
python3 extractor/mmt_extract.py \
    --city goa \
    --checkin 2026-05-15 --checkout 2026-05-16 \
    --max-cards 20 \
    --out test-fixtures/goa.json \
    --screenshot test-fixtures/goa.png \
    --headless
```

Output is one JSON object per hotel card.

### Offline regression test

```bash
python3 extractor/validate_offline.py
```

Runs the parser against a saved 788 KB listing fixture and asserts: at least 3 cards extracted, the 3 expected hotel names present (SinQ Beach Resort, Natures Nest Goa, Ronil Goa part of Hyatt), and no strikethrough price leaks into the displayed price. Run this after any change to `EXTRACT_JS`.

## Why DOM scrape, not API replay

MMT's internal search endpoint (`/api/hotels-search/listing/v3/search-hotels`) is gated by Akamai Bot Manager and returns a 6-byte sentinel string until the session passes Akamai's `bm_sv` sensor flow. The `_abck` cookie length is the tell: ~531 bytes means bootstrap (not yet validated), ~799 bytes means sensor-validated and real data flows. Replaying the request from `urllib`, `curl`, or `fetch()` inside a fresh Playwright context fails consistently across headless/headed Firefox, WebKit, and Chromium, so this tool renders the listing page itself (SSR + hydration) instead of hitting the XHR endpoint.

## Layout

| Path | Role |
|------|------|
| `extractor/mmt_extract.py` | Production listing extractor, multi-engine fallback |
| `extractor/mmt_detail.py` | Detail-page extractor, uses `curl_cffi` for TLS impersonation |
| `extractor/validate_offline.py` | Offline regression test against the saved fixture |
| `test-fixtures/offline_extract.json` | Saved 15-card fixture used by the offline test |
| `catalog/makemytrip.yaml` | Wrapper-only entry registering this tool in `cli-printing-press`'s catalog |
| `docs/LEARNINGS.md` | Design notes, dead ends, what worked |

## Notes / Gotchas

- **IP-flag symptom:** after sustained hammering from the same egress, Akamai may silently throttle it. Signs: `goto` succeeds but the listing HTML stays under ~500KB, DOM extracts 0 cards despite the page looking fine in a real browser, `_abck` stays at ~531 bytes through scrolling. Recovery: cool down 2-6 hours, or switch egress (mobile hotspot, residential VPN). The offline fixture and `validate_offline.py` keep the parser reproducible without live access.
- License: MIT, internal use within Simplotel.

## Related repos

Registered as a wrapper-only entry (`integration_mode: subprocess`) in the `cli-printing-press` catalog, surfaced via `printing-press catalog show makemytrip` / `catalog search travel`. The canonical `catalog/makemytrip.yaml` lives in this repo; it's hardlinked (not symlinked, since Go's `go:embed` rejects symlinks) into a local `cli-printing-press` checkout so edits here stay visible there. That hardlink must be recreated after any fresh `cli-printing-press` clone.
