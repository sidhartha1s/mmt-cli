#!/usr/bin/env python3
"""Playwright-based batch detail extractor.

Same input/output contract as ``batch_detail.py`` but uses a real Firefox
browser instead of curl_cffi, so it works when Akamai has hardened the
egress against TLS-impersonation. The page renders the detail HTML
server-side and the ``window.__INITIAL_STATE__`` blob is in the rendered
DOM — same parsing pipeline as the curl_cffi path.

Usage:
    python3 extractor/batch_detail_pw.py \\
        --in experiments/<run>/discovery.json \\
        --out-dir experiments/<run>/details/ \\
        [--limit N] [--engine firefox|chromium]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from mmt_detail import build_record, parse_initial_state, parse_jsonld_hotel

try:
    from playwright_stealth import Stealth
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

PAGE_TIMEOUT_MS = 60000
HYDRATE_PAUSE_MS = 4000
PAUSE = 5.0


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def warm_up(page) -> None:
    """Hit homepage + /hotels/ to seed cookies, mirroring mmt_extract.py."""
    for url in ("https://www.makemytrip.com/", "https://www.makemytrip.com/hotels/"):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            page.wait_for_timeout(2000)
            page.evaluate("window.scrollBy(0, 600)")
            page.wait_for_timeout(1000)
        except Exception as e:
            print(f"  warmup {url}: {e}", file=sys.stderr)


def fetch_html(page, url: str) -> tuple[str, dict[str, Any]]:
    """Goto URL, hydrate, return (html, meta). Never raises."""
    started = time.monotonic()
    meta: dict[str, Any] = {
        "ts": _now_iso(),
        "exception_class": None,
        "exception_msg": None,
    }
    html = ""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(HYDRATE_PAUSE_MS)
        try:
            page.evaluate("window.scrollBy(0, 800)")
            page.wait_for_timeout(1500)
        except Exception:
            pass
        html = page.content()
    except Exception as e:
        meta["exception_class"] = type(e).__name__
        meta["exception_msg"] = str(e)[:300]
    meta["html_size"] = len(html)
    meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return html, meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="inp", required=True, help="Discovery mapping JSON")
    ap.add_argument("--out-dir", required=True, help="Directory for per-slug JSON")
    ap.add_argument("--limit", type=int, default=0, help="Stop after N candidate-bearing rows (0 = all)")
    ap.add_argument("--engine", default="firefox", choices=["firefox", "chromium"])
    ap.add_argument("--headless", action="store_true", default=True)
    args = ap.parse_args()

    with open(args.inp) as f:
        rows = json.load(f)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary: list[dict[str, Any]] = []
    fetched = 0

    with sync_playwright() as p:
        bt = p.firefox if args.engine == "firefox" else p.chromium
        browser = bt.launch(headless=args.headless)
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        if STEALTH_AVAILABLE:
            try:
                Stealth().apply_stealth_sync(ctx)
            except Exception as e:
                print(f"stealth warn: {e}", file=sys.stderr)
        page = ctx.new_page()

        print(f"warming up ({args.engine})...", file=sys.stderr)
        warm_up(page)

        for i, r in enumerate(rows, 1):
            slug = r.get("slug") or f"row{i}"
            name = r.get("name")
            candidates = r.get("candidates") or []
            if not candidates:
                print(f"[{i}/{len(rows)}] SKIP {slug} (no candidates)")
                summary.append({
                    "slug": slug, "name": name, "mmt_url": None,
                    "status": "no_candidate", "ts": _now_iso(),
                })
                continue

            if args.limit and fetched >= args.limit:
                print(f"[{i}/{len(rows)}] STOP {slug} (--limit {args.limit} reached)")
                summary.append({
                    "slug": slug, "name": name, "mmt_url": candidates[0],
                    "status": "skipped_limit", "ts": _now_iso(),
                })
                continue

            url = candidates[0]
            html, meta = fetch_html(page, url)
            fetched += 1

            record: dict[str, Any] | None = None
            error_class: str | None = None
            if meta["exception_class"]:
                error_class = "fetch_exception"
            elif meta["html_size"] < 5000:
                error_class = "short_body"
            else:
                state = parse_initial_state(html)
                jsonld = parse_jsonld_hotel(html)
                if state or jsonld:
                    candidate = build_record(url, state or {}, jsonld)
                    if candidate.get("name") or candidate.get("hotelId"):
                        record = candidate
                    else:
                        error_class = "empty_record"
                else:
                    error_class = "parse_failed"

            if record is not None:
                (out_dir / f"{slug}.json").write_text(
                    json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                print(f"[{i}/{len(rows)}] OK   {slug:<40} → {record.get('name')!r} ({record.get('hotelId')})")
                summary.append({
                    "slug": slug, "name": name, "mmt_url": url,
                    "hotel_id": record.get("hotelId"), "mmt_name": record.get("name"),
                    "status": "ok", "fetch": meta,
                })
            else:
                print(
                    f"[{i}/{len(rows)}] ERR  {slug}: {error_class} "
                    f"size={meta['html_size']}",
                    file=sys.stderr,
                )
                summary.append({
                    "slug": slug, "name": name, "mmt_url": url,
                    "status": "error", "error_class": error_class, "fetch": meta,
                })
            time.sleep(PAUSE)

        browser.close()

    (out_dir / "index.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    ok = sum(1 for s in summary if s["status"] == "ok")
    print(f"\nExtracted {ok}/{len(summary)} → {out_dir}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
