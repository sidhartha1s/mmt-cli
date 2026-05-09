#!/usr/bin/env python3
"""Batch-extract MMT detail JSON for properties in a discovery mapping.

Reads the output of ``discover_urls.py`` (a list of objects with
``slug``, ``name``, and ``candidates``), uses a single warmed-up
``mmt_detail`` session to scrape each property's first candidate URL,
and writes per-slug JSON to ``--out-dir``. Properties with no
candidates are recorded in the summary but not fetched.

A summary index (``index.json`` in ``--out-dir``) maps slug→
{name, mmt_url, hotel_id, status, error}.

Usage:
    python3 batch_detail.py \\
        --in test-fixtures/gdhotels_mmt_urls.json \\
        --out-dir test-fixtures/gdhotels_mmt_details/
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from mmt_detail import extract, make_session

PAUSE = 4.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="inp", required=True, help="Discovery mapping JSON")
    ap.add_argument("--out-dir", required=True, help="Directory for per-slug JSON")
    args = ap.parse_args()

    with open(args.inp) as f:
        rows = json.load(f)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    session, profile = make_session()
    print(f"using TLS profile: {profile}", file=sys.stderr)

    def _extract_with_refresh(url: str):
        """Extract; on Akamai sentinel, rebuild session once and retry.

        Akamai blacklists a warmed-up session after a few detail fetches.
        Re-warming with a fresh impersonation gets us another window.
        """
        nonlocal session, profile
        try:
            return extract(url, session=session)
        except RuntimeError as e:
            if "sentinel" not in str(e).lower():
                raise
            print(f"  sentinel hit, rebuilding session...", file=sys.stderr)
            session, profile = make_session()
            print(f"  new TLS profile: {profile}", file=sys.stderr)
            return extract(url, session=session)

    summary: list[dict] = []
    for i, r in enumerate(rows, 1):
        slug = r.get("slug") or f"row{i}"
        name = r.get("name")
        candidates = r.get("candidates") or []
        if not candidates:
            print(f"[{i}/{len(rows)}] SKIP {slug} (no candidates)")
            summary.append(
                {"slug": slug, "name": name, "mmt_url": None, "status": "no_candidate"}
            )
            continue

        url = candidates[0]
        try:
            record = _extract_with_refresh(url)
            (out_dir / f"{slug}.json").write_text(
                json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(
                f"[{i}/{len(rows)}] OK   {slug:<40} → "
                f"{record.get('name')!r} ({record.get('hotelId')})"
            )
            summary.append(
                {
                    "slug": slug,
                    "name": name,
                    "mmt_url": url,
                    "hotel_id": record.get("hotelId"),
                    "mmt_name": record.get("name"),
                    "status": "ok",
                }
            )
        except Exception as e:
            print(f"[{i}/{len(rows)}] ERR  {slug}: {e}", file=sys.stderr)
            summary.append(
                {
                    "slug": slug,
                    "name": name,
                    "mmt_url": url,
                    "status": "error",
                    "error": str(e),
                }
            )
        time.sleep(PAUSE)

    (out_dir / "index.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    ok = sum(1 for s in summary if s["status"] == "ok")
    print(
        f"\nExtracted {ok}/{len(summary)} → {out_dir}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
