#!/usr/bin/env python3
"""Discover MakeMyTrip detail URLs for a list of named hotel properties.

Searches Bing for ``"<hotel name>" makemytrip``, decodes the ``bing.com/ck/a``
result links (base64-encoded target URLs), and keeps any
``/hotels/<slug>-details-<city>.html`` candidates. Falls back to DuckDuckGo
if Bing returns nothing.

Why not Google? It refuses to render results without JS.
Why not DDG only? It rate-limits aggressively and serves HTTP 202 with
empty results once tripped.
Why Bing's ck/a redirects? Real target URLs are encoded as
``base64url(target)`` with a 2-byte ``a1`` prefix in the ``u=`` param.

Input shape (JSON list of objects):
    [{"brand_url": "...", "name": "MySpace Forest Keys", "slug": "..."}]

Output shape:
    [{"name": "...", "brand_url": "...", "candidates": ["mmt_url", ...]}]
"""
from __future__ import annotations

import argparse
import base64
import html as ihtml
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

from curl_cffi import requests

BING = "https://www.bing.com/search?q={}"
DDG = "https://html.duckduckgo.com/html/?q={}"
MMT_DETAIL_RE = re.compile(
    r"https?://(?:www\.)?makemytrip\.com/hotels/[a-z0-9_\-]+-details-[a-z0-9_\-]+\.html",
    re.IGNORECASE,
)
BING_H2_HREF_RE = re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"')
DDG_REDIRECT_RE = re.compile(r'/l/\?(?:[^"\']*&)?uddg=([^"\'&]+)')
PAUSE = 2.5


def _decode_bing_ck(href: str) -> str | None:
    """Decode a ``bing.com/ck/a?...&u=...`` link to its real target URL.

    Bing prefixes the base64-encoded target with the literal characters
    ``a1`` (the format/version marker). After stripping the prefix we
    base64url-decode and pad to a multiple of 4.
    """
    href = ihtml.unescape(href)
    parts = urllib.parse.urlparse(href)
    if "/ck/a" not in parts.path:
        return None
    raw = (urllib.parse.parse_qs(parts.query).get("u") or [None])[0]
    if not raw:
        return None
    if raw.startswith("a1"):
        raw = raw[2:]
    raw += "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(raw).decode("utf-8", "ignore")
    except Exception:
        return None


def _strip_tracking(url: str) -> str:
    """Drop msockid/utm/etc query params; MMT detail pages don't need them."""
    u = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(u._replace(query="", fragment=""))


def search_bing(session: requests.Session, query: str) -> list[str]:
    """Return MMT detail URL candidates from Bing for ``query``."""
    r = session.get(BING.format(urllib.parse.quote(query)), timeout=20)
    if r.status_code != 200:
        return []
    html = r.content.decode("utf-8", "ignore")

    seen: set[str] = set()
    found: list[str] = []
    for href in BING_H2_HREF_RE.findall(html):
        target = _decode_bing_ck(href)
        if not target:
            continue
        m = MMT_DETAIL_RE.search(target)
        if not m:
            continue
        url = _strip_tracking(m.group(0))
        if url not in seen:
            seen.add(url)
            found.append(url)
    return found


def search_ddg(session: requests.Session, query: str) -> list[str]:
    """Fallback: DuckDuckGo HTML endpoint."""
    r = session.get(DDG.format(urllib.parse.quote(query)), timeout=20)
    if r.status_code != 200:
        return []
    html = r.content.decode("utf-8", "ignore")
    seen: set[str] = set()
    found: list[str] = []
    for m in MMT_DETAIL_RE.finditer(html):
        url = _strip_tracking(m.group(0))
        if url not in seen:
            seen.add(url)
            found.append(url)
    for m in DDG_REDIRECT_RE.finditer(html):
        decoded = urllib.parse.unquote(ihtml.unescape(m.group(1)))
        if not decoded.startswith("http"):
            decoded = "https://" + decoded.lstrip("/")
        m2 = MMT_DETAIL_RE.search(decoded)
        if m2:
            url = _strip_tracking(m2.group(0))
            if url not in seen:
                seen.add(url)
                found.append(url)
    return found


_BRAND_PREFIX_RE = re.compile(r"^(?:MySpace|My\s*Space|Myspace|Vybe|Ezzenza)\s+", re.IGNORECASE)


def name_variants(name: str) -> list[str]:
    """Generate query variants to handle brand-renamed properties.

    GDH chains rebrand legacy hotels (e.g. ``MySpace Kenilworth`` may still
    be indexed on MMT as plain ``Kenilworth``). We try the name as-given
    first, then the prefix-stripped form.
    """
    variants = [name]
    stripped = _BRAND_PREFIX_RE.sub("", name).strip()
    if stripped and stripped.lower() != name.lower():
        variants.append(stripped)
    return variants


def queries_for(variant: str) -> list[str]:
    """Build search-query forms to try for one name variant.

    The bare ``"<v>" makemytrip`` form catches most cases. Adding the
    ``hotel`` keyword + ``site:`` operator helps when the name alone is
    ambiguous (e.g. ``Kadison Davanagere`` only resolves with ``hotel``
    appended — Bing otherwise returns unrelated results).
    """
    return [
        f'"{variant}" makemytrip',
        f'"{variant}" hotel site:makemytrip.com',
    ]


def discover(session: requests.Session, name: str) -> list[str]:
    """Try Bing (then DDG) across brand-stripped name variants."""
    seen: set[str] = set()
    out: list[str] = []
    for variant in name_variants(name):
        for query in queries_for(variant):
            for fn in (search_bing, search_ddg):
                for url in fn(session, query):
                    if url not in seen:
                        seen.add(url)
                        out.append(url)
                if out:
                    return out
                time.sleep(1.0)
    return out


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
        # Strip trailing location qualifiers and "Welcome to" prefix that hurt recall
        name_clean = re.sub(r"\s*,.*$", "", name)
        name_clean = re.sub(r"^Welcome to\s+", "", name_clean, flags=re.I)
        if not name_clean:
            print(f"[{i+1}/{len(props)}] SKIP (no name) {p.get('slug')}", file=sys.stderr)
            results.append({**p, "candidates": [], "name_searched": None})
            continue

        try:
            cands = discover(s, name_clean)
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
