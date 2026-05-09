#!/usr/bin/env python3
"""Discover MakeMyTrip detail URLs for a list of named hotel properties.

Searches DuckDuckGo's HTML endpoint for ``"<hotel name>" makemytrip``,
extracts any ``/hotels/<slug>-details-<city>.html`` candidates from the
results, and writes the mapping (with multiple candidates per hotel) to
JSON.

Input shape (JSON list of objects):
    [{"brand_url": "...", "name": "MySpace Forest Keys", "slug": "..."}]

Output shape:
    [{"name": "...", "brand_url": "...", "candidates": ["mmt_url", ...]}]
"""
from __future__ import annotations

import argparse
import html as ihtml
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

from curl_cffi import requests

DDG = "https://html.duckduckgo.com/html/?q={}"
MMT_DETAIL_RE = re.compile(
    r"https?://(?:www\.)?makemytrip\.com/hotels/[a-z0-9_\-]+-details-[a-z0-9_\-]+\.html",
    re.IGNORECASE,
)
DDG_REDIRECT_RE = re.compile(r'/l/\?(?:[^"\']*&)?uddg=([^"\'&]+)')
PAUSE = 2.5


def search_mmt(session: requests.Session, name: str) -> list[str]:
    """Return MMT detail URL candidates for ``name`` (deduped, in result order)."""
    query = f'"{name}" makemytrip'
    r = session.get(DDG.format(urllib.parse.quote(query)), timeout=20)
    if r.status_code != 200:
        return []
    html = r.content.decode("utf-8", "ignore")

    seen: set[str] = set()
    found: list[str] = []

    # Direct hits (some DDG results expose target URLs raw)
    for m in MMT_DETAIL_RE.finditer(html):
        url = m.group(0)
        if url not in seen:
            seen.add(url)
            found.append(url)

    # DDG also exposes results through a /l/?uddg=<encoded> redirect; decode those.
    for m in DDG_REDIRECT_RE.finditer(html):
        decoded = urllib.parse.unquote(ihtml.unescape(m.group(1)))
        if not decoded.startswith("http"):
            decoded = "https://" + decoded.lstrip("/")
        m2 = MMT_DETAIL_RE.search(decoded)
        if m2 and m2.group(0) not in seen:
            seen.add(m2.group(0))
            found.append(m2.group(0))

    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="inp", required=True, help="Input properties JSON")
    ap.add_argument("--out", required=True, help="Output mapping JSON")
    ap.add_argument(
        "--limit", type=int, default=0, help="Cap number of properties (0=all)"
    )
    args = ap.parse_args()

    with open(args.inp) as f:
        props = json.load(f)
    if args.limit:
        props = props[: args.limit]

    s = requests.Session(impersonate="chrome110")
    results = []
    for i, p in enumerate(props):
        name = (p.get("name") or "").replace("\xa0", " ").strip()
        # Strip trailing location qualifiers that hurt search recall
        name_clean = re.sub(r"\s*,.*$", "", name)
        name_clean = re.sub(r"^Welcome to\s+", "", name_clean, flags=re.I)
        if not name_clean:
            print(f"[{i+1}/{len(props)}] SKIP (no name) {p.get('slug')}", file=sys.stderr)
            results.append({**p, "candidates": [], "name_searched": None})
            continue

        try:
            cands = search_mmt(s, name_clean)
        except Exception as e:
            print(f"  ERR {name_clean}: {e}", file=sys.stderr)
            cands = []

        results.append(
            {
                "brand_url": p.get("brand_url"),
                "slug": p.get("slug"),
                "name": name,
                "name_searched": name_clean,
                "candidates": cands,
            }
        )
        marker = cands[0] if cands else "(none)"
        print(f"[{i+1}/{len(props)}] {name_clean!s:<55} → {marker}")
        time.sleep(PAUSE)

    Path(args.out).write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    found = sum(1 for r in results if r["candidates"])
    print(
        f"\nDiscovered MMT URLs for {found}/{len(results)} properties → {args.out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
