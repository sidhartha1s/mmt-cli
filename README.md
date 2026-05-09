# mmt-cli

Free, internal-use CLI for extracting hotel listings from
[makemytrip.com](https://www.makemytrip.com/hotels/). Built for Simplotel client
onboarding to eliminate strikethrough-corruption-on-copy-paste — the live MMT
listing UI shows discount markdowns as struck-through prices, which copy-pasted
as the wrong value into onboarding sheets.

**Status:** parser is production-ready (validated offline, 15/15 cards, 3/3
expected names, strikethrough filter clean). Live extraction works when the
egress IP is not Akamai-flagged.

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
pip install playwright playwright-stealth
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
│   ├── mmt_extract.py       # production extractor, multi-engine fallback
│   └── validate_offline.py  # offline regression test
├── test-fixtures/
│   ├── offline_extract.json # 15 hotel cards from saved fixture
│   ├── goa.json             # last live run (if any)
│   └── goa.png              # screenshot from last live run
└── docs/
    └── LEARNINGS.md         # design notes, dead ends, what worked
```

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

2. **Symlink from this repo**: keep the canonical YAML in
   `mmt-cli/catalog/makemytrip.yaml`, symlink it into the pp tree, rebuild.
   The symlink survives `git pull` (still untracked from pp's POV).

3. **Fork pp**: push `simplotel/cli-printing-press` and land the entry on a
   long-lived branch.

Until one of these is done, treat the catalog entry as ephemeral and re-add
from this repo if needed.

## License

MIT — internal use within Simplotel.
