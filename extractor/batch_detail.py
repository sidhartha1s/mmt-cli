#!/usr/bin/env python3
"""Batch-extract MMT detail JSON for properties in a discovery mapping.

Reads the output of ``discover_urls.py`` (a list of objects with
``slug``, ``name``, and ``candidates``), uses a single warmed-up
``mmt_detail`` session to scrape each property's first candidate URL,
and writes per-slug JSON to ``--out-dir``. Properties with no
candidates are recorded in the summary but not fetched.

The summary index (``index.json`` in ``--out-dir``) records per-slug
fetch metadata for failure post-mortem: HTTP status, body size, body
signature, profile used, session age (seconds since warmup), retry
count, warmup status, error class, and timestamps.

Usage:
    python3 extractor/batch_detail.py \\
        --in experiments/<run>/discovery.json \\
        --out-dir experiments/<run>/details/
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from mmt_detail import (
    WARMUP_URLS,
    build_record,
    make_session,
    parse_initial_state,
    parse_jsonld_hotel,
)

PAUSE = 4.0


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _body_signature(body: bytes) -> str:
    """Cheap fingerprint of a response body for grouping similar failures."""
    if len(body) <= 32:
        return f"short:{body[:32]!r}"
    head = body[:120]
    m = _TITLE_RE.search(head.decode("utf-8", "ignore"))
    if m:
        return f"title:{m.group(1).strip()[:80]}"
    return f"head:{head[:80]!r}"


def _classify(status: int, body: bytes) -> str:
    """Bucket a fetch outcome into a short error class for the summary."""
    if status != 200:
        return f"http_{status}"
    if len(body) < 100:
        return "akamai_sentinel"
    if len(body) < 5000:
        return "short_body"
    return "parse_failed"


def _check_warmup(session: Any) -> dict[str, bool]:
    """Probe the warmup URLs on an existing session; return per-URL ok flags."""
    out: dict[str, bool] = {}
    for w in WARMUP_URLS:
        try:
            r = session.get(w, timeout=15, allow_redirects=True)
            out[w] = r.status_code == 200 and len(r.content) > 5000
        except Exception:
            out[w] = False
    return out


def _attempt_fetch(session: Any, url: str) -> dict[str, Any]:
    """One GET attempt. Returns rich metadata, never raises."""
    started = time.monotonic()
    rec: dict[str, Any] = {
        "ts": _now_iso(),
        "status": None,
        "body_size": 0,
        "body_signature": None,
        "exception_class": None,
        "exception_msg": None,
        "elapsed_ms": None,
    }
    try:
        r = session.get(url, timeout=25, allow_redirects=True)
        body = r.content
        rec["status"] = r.status_code
        rec["body_size"] = len(body)
        rec["body_signature"] = _body_signature(body)
        rec["body"] = body
    except Exception as e:
        rec["exception_class"] = type(e).__name__
        rec["exception_msg"] = str(e)[:300]
    rec["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return rec


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
    session_started = time.monotonic()
    print(f"using TLS profile: {profile}", file=sys.stderr)

    summary: list[dict] = []
    for i, r in enumerate(rows, 1):
        slug = r.get("slug") or f"row{i}"
        name = r.get("name")
        candidates = r.get("candidates") or []
        if not candidates:
            print(f"[{i}/{len(rows)}] SKIP {slug} (no candidates)")
            summary.append(
                {
                    "slug": slug,
                    "name": name,
                    "mmt_url": None,
                    "status": "no_candidate",
                    "ts": _now_iso(),
                }
            )
            continue

        url = candidates[0]
        attempts: list[dict[str, Any]] = []
        record: dict[str, Any] | None = None
        error_class: str | None = None

        for retry in range(2):  # original + one session-rebuild retry
            session_age = round(time.monotonic() - session_started, 1)
            attempt = _attempt_fetch(session, url)
            attempt["retry"] = retry
            attempt["profile"] = profile
            attempt["session_age_s"] = session_age

            body = attempt.pop("body", b"")
            if attempt["status"] == 200 and len(body) >= 5000:
                html = body.decode("utf-8", "ignore")
                state = parse_initial_state(html)
                jsonld = parse_jsonld_hotel(html)
                if state or jsonld:
                    record = build_record(url, state or {}, jsonld)
                    attempts.append(attempt)
                    break
                error_class = "parse_failed"
            else:
                error_class = _classify(attempt["status"] or 0, body)

            attempts.append(attempt)
            if retry == 0 and error_class in {"akamai_sentinel", "short_body"}:
                print(f"  {error_class}, rebuilding session...", file=sys.stderr)
                session, profile = make_session()
                session_started = time.monotonic()
                print(f"  new TLS profile: {profile}", file=sys.stderr)
                continue
            break

        warmup_ok = _check_warmup(session)

        if record is not None:
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
                    "attempts": attempts,
                    "warmup_ok": warmup_ok,
                }
            )
        else:
            last = attempts[-1] if attempts else {}
            print(
                f"[{i}/{len(rows)}] ERR  {slug}: "
                f"{error_class} status={last.get('status')} "
                f"size={last.get('body_size')} sig={last.get('body_signature')}",
                file=sys.stderr,
            )
            summary.append(
                {
                    "slug": slug,
                    "name": name,
                    "mmt_url": url,
                    "status": "error",
                    "error_class": error_class,
                    "attempts": attempts,
                    "warmup_ok": warmup_ok,
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
