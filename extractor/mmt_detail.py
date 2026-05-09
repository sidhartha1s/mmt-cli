#!/usr/bin/env python3
"""Extract a MakeMyTrip hotel detail page to structured JSON.

Bypasses Akamai Bot Manager via curl_cffi TLS-fingerprint impersonation
(safari17_0 profile) plus a homepage→/hotels/ warm-up chain. Parses the
embedded ``window.__INITIAL_STATE__`` blob and the JSON-LD ``Hotel`` block
into a flat record.

Usage:
    python3 mmt_detail.py --url <mmt_detail_url> --out hotel.json
    python3 mmt_detail.py --url <mmt_detail_url>           # stdout
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from curl_cffi import requests

WARMUP_URLS = [
    "https://www.makemytrip.com/",
    "https://www.makemytrip.com/hotels/",
]
IMPERSONATE_PROFILES = ["chrome110", "safari17_0"]
TIMEOUT = 25
WARMUP_PAUSE = 1.0
MAX_RETRIES = 3
RETRY_BACKOFF = 3.0

INITIAL_STATE_ASSIGN_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*\{",
)
JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _get_with_retry(s: requests.Session, url: str) -> Any:
    """GET with bounded retries on transient HTTP/2 stream errors."""
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return s.get(url, timeout=TIMEOUT, allow_redirects=True)
        except Exception as e:  # curl_cffi raises HTTPError for stream errors
            last_err = e
            time.sleep(RETRY_BACKOFF * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {MAX_RETRIES} retries: {last_err}")


def warm_session(s: requests.Session) -> None:
    for w in WARMUP_URLS:
        _get_with_retry(s, w)
        time.sleep(WARMUP_PAUSE)


def make_session() -> tuple[requests.Session, str]:
    """Create a warmed-up session, trying impersonation profiles in order.

    Akamai sometimes blacklists individual TLS fingerprints; the fallback
    chain keeps the extractor working when the primary profile is rejected.
    """
    last_err: Exception | None = None
    for profile in IMPERSONATE_PROFILES:
        try:
            s = requests.Session(impersonate=profile)
            warm_session(s)
            return s, profile
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(
        f"All impersonation profiles failed warm-up: {IMPERSONATE_PROFILES}: {last_err}"
    )


def fetch(url: str, session: requests.Session | None = None) -> bytes:
    """Fetch ``url`` through a warmed-up impersonated session and return body bytes.

    Raises ``RuntimeError`` if the response is the 6-byte Akamai sentinel or
    otherwise too small to parse.
    """
    if session is None:
        session, _ = make_session()
    r = _get_with_retry(session, url)
    body = r.content
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} for {url}")
    if len(body) < 5000:
        raise RuntimeError(
            f"Akamai sentinel or empty body ({len(body)}b): {body[:120]!r}"
        )
    return body


def parse_initial_state(html: str) -> dict[str, Any] | None:
    """Locate ``window.__INITIAL_STATE__`` in HTML and return the parsed dict.

    The page contains four references to the symbol; only the first is the
    assignment we want. The JSON payload is large and ends well before
    ``</script>``, so we extract by brace-balancing from the opening ``{``.
    """
    m = INITIAL_STATE_ASSIGN_RE.search(html)
    if not m:
        return None
    start = m.end() - 1  # position of the opening '{'
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(html)):
        ch = html[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse_jsonld_hotel(html: str) -> dict[str, Any] | None:
    """Return the first JSON-LD block whose ``@type`` is ``Hotel``."""
    for m in JSONLD_RE.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for c in candidates:
            if isinstance(c, dict) and c.get("@type") == "Hotel":
                return c
    return None


def flatten_amenities(amenities: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Flatten the nested category→facilities tree into a single deduped list."""
    if not amenities:
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for cat in amenities:
        cat_name = cat.get("name") or cat.get("pillTitle") or ""
        for f in cat.get("facilities") or []:
            code = f.get("code") or f.get("name") or ""
            if code in seen:
                continue
            seen.add(code)
            out.append(
                {
                    "name": f.get("name"),
                    "code": code,
                    "category": cat_name,
                    "subText": f.get("subText"),
                }
            )
    return out


def flatten_rooms(room_info_map: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not room_info_map:
        return []
    out = []
    for code, room in room_info_map.items():
        if not isinstance(room, dict):
            continue
        out.append(
            {
                "roomCode": room.get("roomCode") or code,
                "roomName": room.get("roomName"),
                "maxGuest": room.get("maxGuest"),
                "roomSize": room.get("roomSize"),
                "roomViewName": room.get("roomViewName"),
                "beds": room.get("beds") or [],
                "amenities": [
                    a.get("name") for a in (room.get("amenities") or []) if a.get("name")
                ],
                "imageCount": len(room.get("images") or []),
            }
        )
    return out


def build_record(url: str, state: dict[str, Any], jsonld: dict[str, Any] | None) -> dict[str, Any]:
    """Build the flat output record from the parsed page artifacts."""
    detail = (
        state.get("hotelDetail", {}).get("staticDetail", {}) if state else {}
    )
    hd = detail.get("hotelDetails", {}) or {}
    ugc = detail.get("ugcSummary", {}) or {}
    rooms = flatten_rooms(detail.get("roomInfoMap"))

    addr = hd.get("address") or {}
    jl_addr = (jsonld or {}).get("address") or {}
    jl_rating = (jsonld or {}).get("aggregateRating") or {}

    return {
        "url": url,
        "hotelId": hd.get("id"),
        "name": hd.get("name") or (jsonld or {}).get("name"),
        "starRating": hd.get("starRating"),
        "propertyType": hd.get("propertyType"),
        "checkinTime": hd.get("checkinTime"),
        "checkoutTime": hd.get("checkoutTime"),
        "lat": hd.get("lat"),
        "lng": hd.get("lng"),
        "pinCode": hd.get("pinCode"),
        "address": {
            "line1": addr.get("line1") or jl_addr.get("streetAddress"),
            "city": jl_addr.get("addressRegion"),
            "postalCode": jl_addr.get("postalCode") or hd.get("pinCode"),
            "country": jl_addr.get("addressCountry"),
        },
        "shortDesc": hd.get("shortDesc"),
        "longDesc": hd.get("longDesc"),
        "rating": ugc.get("cumulativeRating") or jl_rating.get("ratingValue"),
        "reviewCount": ugc.get("reviewCount") or jl_rating.get("reviewCount"),
        "ratingBreakup": ugc.get("ratingBreakup"),
        "amenities": flatten_amenities(hd.get("amenities")),
        "highlightedAmenities": [
            a if isinstance(a, str) else a.get("name")
            for a in (hd.get("highlightedAmenities") or [])
            if (isinstance(a, str) and a) or (isinstance(a, dict) and a.get("name"))
        ],
        "rooms": rooms,
        "image": (jsonld or {}).get("image"),
        "houseRules": hd.get("houseRules"),
        "locationDetail": hd.get("locationDetail"),
    }


def extract(url: str, session: requests.Session | None = None) -> dict[str, Any]:
    body = fetch(url, session=session)
    html = body.decode("utf-8", "ignore")
    state = parse_initial_state(html)
    jsonld = parse_jsonld_hotel(html)
    if not state and not jsonld:
        raise RuntimeError("Neither __INITIAL_STATE__ nor JSON-LD Hotel block found")
    return build_record(url, state or {}, jsonld)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True, help="MakeMyTrip hotel detail URL")
    ap.add_argument("--out", help="Output JSON path (default: stdout)")
    ap.add_argument(
        "--raw-html", help="If set, also save the raw HTML body to this path"
    )
    args = ap.parse_args()

    session, profile = make_session()
    print(f"using TLS profile: {profile}", file=sys.stderr)

    body = fetch(args.url, session=session)
    if args.raw_html:
        Path(args.raw_html).write_bytes(body)

    html = body.decode("utf-8", "ignore")
    state = parse_initial_state(html)
    jsonld = parse_jsonld_hotel(html)
    if not state and not jsonld:
        print("ERROR: no parseable data on page", file=sys.stderr)
        return 2

    record = build_record(args.url, state or {}, jsonld)
    payload = json.dumps(record, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(
            f"OK: {record.get('name')!r} ({record.get('hotelId')}) → {args.out}",
            file=sys.stderr,
        )
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
