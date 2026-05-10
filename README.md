# mmt-cli

Free, internal-use CLI for extracting hotel listings from
[makemytrip.com](https://www.makemytrip.com/hotels/). Built for Simplotel client
onboarding to eliminate strikethrough-corruption-on-copy-paste — the live MMT
listing UI shows discount markdowns as struck-through prices, which copy-pasted
as the wrong value into onboarding sheets.

**Status — three components, three different maturity levels.** Read this
before you trust any number out of this repo.

| Component | Path | Status |
|-----------|------|--------|
| Listing extractor (DOM scrape, strikethrough-safe) | `extractor/mmt_extract.py` | **Production.** Validated offline (15/15 cards, 3/3 expected names, no strikethrough leaks). Live works when the egress IP is not Akamai-flagged. |
| URL discovery (Bing/DDG → MMT detail URL) | `extractor/discover_urls.py` | **Experimental.** Brand-strip variants + multi-query Bing recover ~25–30% on small chains. Properties not listed on MMT can't be discovered. |
| Detail-page enrichment, curl_cffi path | `extractor/mmt_detail.py`, `batch_detail.py` | **Research.** TLS-impersonation (chrome110/safari17_0) + warmup chain. Fast (~1s/row) but Akamai hardens the egress within minutes-to-hours; HTTP/2 INTERNAL_ERROR on warmup is the typical failure mode. |
| Detail-page enrichment, Playwright path | `extractor/batch_detail_pw.py` | **Research.** Real Firefox tab + same warmup chain. Slower (~6s/row) but works when the curl path is blocked. Same parse pipeline as the curl path; only the fetcher swaps. |

The "production" line is listing extraction. The other two are useful as
building blocks but should not be load-bearing in client onboarding without
a residential-proxy or commercial-scraping backstop.

## What it does

1. Loads a MakeMyTrip listing URL in a real Firefox browser via Playwright.
2. Lets the page hydrate, scrolls to trigger lazy renders.
3. Extracts hotel cards directly from the rendered DOM, using
   `getComputedStyle(node).textDecoration` to filter `line-through` nodes
   out of the displayed-price field. Strikethrough prices are kept in a
   separate `strikethroughPrices[]` array for audit.
4. Outputs structured JSON. Each card carries: `name`, `location`,
   `price` (post-filter), `strikethroughPrices[]`, `rating`, `ratingLabel`,
   `reviewCount`, `hotelId`, `photoCount`, `badges[]`, `detailUrl`.

## Why DOM scrape (not API replay)

MMT's internal XHR endpoint
(`/api/hotels-search/listing/v3/search-hotels`) is gated by Akamai Bot
Manager. The endpoint returns a 6-byte `200-OK` sentinel string until the
calling browser session has been validated through Akamai's `bm_sv` sensor
flow. The `_abck` cookie state is the easiest tell:

| `_abck` length | Meaning |
|----------------|---------|
| ~531 bytes | Bootstrap (cookie issued, sensor not yet validated) |
| ~799 bytes | Sensor-validated (real data flows) |

Replaying the request from `urllib`, `curl`, or `fetch()` inside a
freshly-launched Playwright context **fails consistently** — the Akamai
signal includes browser-fingerprint and timing telemetry the replay cannot
reproduce. We tested headless+headed across Firefox, WebKit, and Chromium;
the gating is not a head/headless discriminator alone.

So we skipped the API rabbit hole and went DOM-first. The listing page
itself renders the hotel cards via SSR + hydration, no XHR needed.

## Quickstart

```bash
# install deps
pip install -r requirements.txt
playwright install firefox

# extract Goa hotels
python3 extractor/mmt_extract.py \
    --city goa \
    --checkin 2026-05-15 --checkout 2026-05-16 \
    --max-cards 20 \
    --out test-fixtures/goa.json \
    --screenshot test-fixtures/goa.png \
    --headless
```

Output is JSON; one object per card.

## Offline self-test

The repo ships a saved 788KB MMT listing fixture and a validator that
proves the parser works without live Akamai:

```bash
python3 extractor/validate_offline.py
```

Asserts: ≥3 cards extracted, three expected hotel names present
(SinQ Beach Resort, Natures Nest Goa, Ronil Goa part of Hyatt), and no
strikethrough price leaks into the displayed-price field.

This is the regression test. **Run it after any change to `EXTRACT_JS`.**

## Akamai IP-flag caveat

After sustained development hammering from the same egress, Akamai may
silently throttle the source IP. Symptoms:

- `goto` succeeds but the listing HTML stays under ~500KB
- DOM extracts 0 cards even though the page looks fine in a real browser
- `_abck` cookie stays at ~531 bytes through scrolling

**Recovery:**
- Cool down 2–6 hours and retry
- Run from a different egress (mobile hotspot, residential VPN)
- The offline fixture (`test-fixtures/offline_extract.json`) and
  `validate_offline.py` keep the parser reproducible regardless of
  live access

## Repo layout

```
mmt-cli/
├── extractor/
│   ├── mmt_extract.py        # production: listing extractor
│   ├── validate_offline.py   # production: offline regression test
│   ├── parser_smoke_test.py  # CI: offline tests for parser internals
│   ├── discover_urls.py      # experimental: Bing/DDG URL discovery
│   ├── mmt_detail.py         # research: detail extractor (curl_cffi fetch + parsers)
│   ├── batch_detail.py       # research: curl_cffi batch wrapper
│   └── batch_detail_pw.py    # research: Playwright batch wrapper (when curl is blocked)
├── test-fixtures/
│   ├── offline_extract.json     # 15-card listing fixture for validate_offline
│   ├── goa.json / goa.png       # last live listing run
│   └── gdhotels_properties.json # curated 22-property seed list (input only)
├── experiments/
│   ├── gdhotels_2026-05-10/     # discovery + curl_cffi detail run (1/6 ok)
│   ├── gdhotels_2026-05-11/     # curl_cffi rerun, egress hardened (0/6, structured-fail proof)
│   └── gdhotels_2026-05-11_pw/  # Playwright detail run (5/6 ok)
├── .github/workflows/ci.yml  # compileall + parser smoke test on every push
├── requirements.txt          # playwright(+stealth), curl_cffi
└── docs/LEARNINGS.md         # design notes, dead ends, what worked
```

## Experiments vs fixtures

`test-fixtures/` is for **deterministic inputs** (curated property lists,
saved HTML used by offline tests). `experiments/<date>/` is for **runtime
outputs** of one run, captured for inspection. Re-running discovery or
detail extraction will produce different numbers; do not treat experiment
outputs as regression baselines. See e.g. `experiments/gdhotels_2026-05-10/README.md`.

## CI

`.github/workflows/ci.yml` runs on every push/PR:

- `python -m compileall -q extractor` — syntax check the whole package
- `python -m extractor.parser_smoke_test` — offline tests for
  `parse_initial_state`, `parse_jsonld_hotel`, `_decode_bing_ck`. No
  network, finishes in <1s.
- `ruff check extractor` (best-effort, non-blocking)

The 788KB live-listing fixture and the full `validate_offline.py` test
are intentionally NOT in CI yet — they need the fixture to be committed
or downloaded from a release artifact. Run them locally before shipping
listing-extractor changes.

## Integration with cli-printing-press

This extractor is registered in the printing-press catalog at
`catalog/makemytrip.yaml` as a wrapper-only entry with
`integration_mode: subprocess`. Pattern matches `google-flights.yaml`
(which wraps `punitarani/fli` the same way). Surfaced via:

```bash
./printing-press catalog show makemytrip
./printing-press catalog search travel
```

Wrapper-only catalog entries are advisory — pp does not auto-generate Go
scaffolding for them. The contract is "the catalog tells the user (or an
agent) which wrapper library to invoke." Same pathway as `google-flights`.

### Persisting the catalog entry

`catalog/makemytrip.yaml` is currently **untracked** in the upstream pp
clone at `/home/sidhartha/cli-printing-press` (which tracks
`mvanhorn/cli-printing-press`). `git pull` on that repo will NOT delete
the untracked file, but `git clean -fdx` would, and an upstream collision
on the same filename would force a rename.

To make the entry durable, pick one:

1. **Local branch** (lightest):
   ```bash
   cd /home/sidhartha/cli-printing-press
   git checkout -b simplotel/mmt-catalog
   git add catalog/makemytrip.yaml
   git commit -m "feat(cli): add makemytrip wrapper-only catalog entry"
   ```
   Switch to this branch when rebuilding pp; rebase onto upstream `main` periodically.

2. **Hardlink from this repo** (what we ship): keep the canonical YAML in
   `mmt-cli/catalog/makemytrip.yaml`, hardlink it into the pp tree, rebuild.
   ```bash
   ln /home/sidhartha/mmt-cli/catalog/makemytrip.yaml \
      /home/sidhartha/cli-printing-press/catalog/makemytrip.yaml
   ```
   Hardlink (not symlink) because Go's `go:embed *.yaml` rejects symlinks
   with `cannot embed irregular file`. A hardlink shares the inode, so a
   single edit is visible in both repos and `go:embed` accepts it as a
   regular file. Recreate after a fresh pp clone (hardlinks don't survive
   git checkout into a new working tree).

3. **Fork pp**: push `simplotel/cli-printing-press` and land the entry on a
   long-lived branch.

Until one of these is done, treat the catalog entry as ephemeral and re-add
from this repo if needed.

## License

MIT — internal use within Simplotel.
