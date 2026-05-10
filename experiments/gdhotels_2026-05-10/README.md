# gdhotels.in batch detail run — 2026-05-10

Snapshot of one experimental run, NOT a fixture. Re-running discovery or
extraction will produce different numbers (Bing recall drifts; Akamai
state on the egress IP varies).

## Inputs

- Seed properties: `../../test-fixtures/gdhotels_properties.json` (22
  hand-curated MySpace/Vybe/Ezzenza properties from gdhotels.in)

## Outputs

- `discovery.json` — output of `discover_urls.py` against the seed list.
  6/22 properties resolved to MMT detail URLs; the rest are likely not
  MMT-listed at all (the chain is small and several properties don't
  appear on Bing under any plausible name variant).
- `details/` — output of `batch_detail.py` against the resolved URLs.
  `index.json` summarises status per slug; per-slug JSON files contain
  the full extracted record (name, address, amenities, rooms, etc).

## What this run actually demonstrates

- Listing extraction is the production-supported path. Detail extraction
  worked for **1/4** attempted on this run before Akamai sentinel-blocked
  the egress IP. Treat as research, not a finished pipeline.
- Discovery recall caps at ~25-30% on this seed. Brand rebrands
  (Vybe→Mastiff Select, Ezzenza→Devlok Himachal Swarg) are recoverable
  with brand-prefix stripping; properties that simply aren't MMT-listed
  are not.

## To reproduce

```bash
python3 extractor/discover_urls.py \
    --in test-fixtures/gdhotels_properties.json \
    --out experiments/gdhotels_2026-05-10/discovery.json

python3 extractor/batch_detail.py \
    --in experiments/gdhotels_2026-05-10/discovery.json \
    --out-dir experiments/gdhotels_2026-05-10/details/
```

Expect different numbers each run. Akamai cools off slowly; allow
hours-to-days between hammered runs.
