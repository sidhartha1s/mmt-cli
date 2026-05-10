# gdhotels.in batch detail run — 2026-05-11

Snapshot of a follow-up run after Akamai egress-block recovery and bug
fixes to `batch_detail.py`. NOT a fixture.

## What this run demonstrates

The egress IP was hardened again by Akamai before this run. The
`make_session()` warmup against `https://www.makemytrip.com/` failed with
HTTP/2 INTERNAL_ERROR (curl 92) on both `chrome110` and `safari17_0`
profiles, after 3 retries each.

What's new vs. the 2026-05-10 run is that the script no longer crashes
at startup. With the fixes in this commit:

- Initial warmup failure is caught, logged as `startup_error`, and the
  whole batch short-circuits with each candidate-bearing row recorded as
  `status: blocked, error_class: warmup_rebuild_failed`.
- Each blocked row carries a `startup_error` field with the exception
  class, message, timestamp, and stage label — so post-mortem can trace
  cause from `index.json` alone.

## Index breakdown

```
total       : 22
no_candidate: 16  (rows whose discovery returned no URL — unchanged)
blocked     :  6  (egress hardened; carry startup_error metadata)
ok          :  0
```

## Why commit a 0/6 success run

Reviewer feedback explicitly asked for "better failure classification."
This run is the proof that the instrumentation handles the worst case —
total Akamai block — without losing audit data. A clean structured
failure log under real adversarial conditions is more valuable than a
silent crash.

The 1/4 success from the 2026-05-10 run remains the empirical ceiling
for this egress without a residential proxy.

## Reproduce

```bash
python3 extractor/batch_detail.py \
    --in experiments/gdhotels_2026-05-10/discovery.json \
    --out-dir experiments/gdhotels_2026-05-11/details/
```

Numbers will vary by Akamai state.
