# gdhotels.in batch detail run — 2026-05-11 (Playwright path)

Snapshot of one experimental run, NOT a fixture. Numbers will drift on
re-run.

## What changed

The curl_cffi path (`mmt_detail.py` + `batch_detail.py`) was egress-blocked
on this date — Akamai returned HTTP/2 INTERNAL_ERROR on warmup against
`https://www.makemytrip.com/`. So this run uses a **Playwright Firefox**
fetcher (`extractor/batch_detail_pw.py`) instead, with the same
homepage→/hotels/ warmup chain that the production listing extractor
(`mmt_extract.py`) already uses.

The parse pipeline is unchanged — `parse_initial_state` and
`parse_jsonld_hotel` from `mmt_detail.py` are reused. Only the fetcher
swaps.

## Inputs

- `../gdhotels_2026-05-10/discovery.json` — 22 seed properties, 6 of
  which Bing/DDG resolved to MMT detail URLs.

## Outputs

- `details/index.json` — per-slug status summary (status, fetch
  metadata, error_class)
- `details/<slug>.json` — full extracted record per success
- `batch.log` — stdout/stderr from the run

## Result

```
total       : 22
no_candidate: 16  (rows whose discovery returned no URL)
ok          :  5  (full record written)
empty_record:  1  (Triton Suites; the discovered URL is an
                   `amenities-of-…` interstitial, not a real detail
                   page — discovery problem, not extraction)
```

End-to-end coverage on this run: **5/22 ≈ 22.7%**, up from 1/22 (4.5%)
on the curl_cffi run from 2026-05-10. The bottleneck moves back to
discovery recall.

## Reproduce

```bash
python3 extractor/batch_detail_pw.py \
    --in experiments/gdhotels_2026-05-10/discovery.json \
    --out-dir experiments/gdhotels_2026-05-11_pw/details/
```

Add `--limit N` to stop after N candidate-bearing rows.

## Caveats

- Slower than curl_cffi (~6s/row vs ~1s/row) because every fetch boots
  a real Firefox tab and waits for hydration.
- Still not "production". Akamai can still block Playwright sessions if
  the egress IP is sufficiently flagged — symptoms then are the same as
  in the listing extractor (DOM stays under ~500KB, `_abck` cookie at
  ~531 bytes). Recovery is the same: cool down or change egress.
