#!/usr/bin/env python3
"""Offline smoke-test for pure-parser code paths.

Network-free. Validates the brace-balanced JSON extractor, the JSON-LD
``Hotel`` filter, and the Bing ``ck/a`` redirect decoder against
hand-crafted minimal inputs. Run in CI to catch regressions in the
non-fetching code without needing live MMT or a 788KB HTML fixture.

Run:
    python -m extractor.parser_smoke_test
"""
from __future__ import annotations

import sys

from extractor.discover_urls import _decode_bing_ck
from extractor.mmt_detail import parse_initial_state, parse_jsonld_hotel


FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    status = "OK  " if cond else "FAIL"
    print(f"  [{status}] {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILURES.append(label)


def test_parse_initial_state_balanced() -> None:
    print("parse_initial_state — balanced braces inside string literals")
    html = (
        '<html><head><script>'
        'window.__INITIAL_STATE__ = {"a": "}{}{", "nested": {"x": 1}};'
        'var other = 1;</script></head></html>'
    )
    state = parse_initial_state(html)
    check("returns dict", isinstance(state, dict))
    check("a", state and state.get("a") == "}{}{", repr(state and state.get("a")))
    check("nested.x", state and state.get("nested", {}).get("x") == 1)


def test_parse_initial_state_missing() -> None:
    print("parse_initial_state — absent assignment returns None")
    check("None on no marker", parse_initial_state("<html>nothing</html>") is None)


def test_parse_jsonld_hotel() -> None:
    print("parse_jsonld_hotel — picks Hotel out of mixed array")
    html = (
        '<script type="application/ld+json">'
        '[{"@type":"BreadcrumbList"},{"@type":"Hotel","name":"Foo"}]'
        '</script>'
    )
    hotel = parse_jsonld_hotel(html)
    check("returns dict", isinstance(hotel, dict))
    check("name=Foo", hotel and hotel.get("name") == "Foo")


def test_decode_bing_ck() -> None:
    print("_decode_bing_ck — base64url decode after a1 prefix")
    import base64

    target = "https://www.makemytrip.com/hotels/foo-details-bar.html"
    raw = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    href = f"https://www.bing.com/ck/a?u=a1{raw}&p=1"
    check("decodes target", _decode_bing_ck(href) == target)
    check("None on non-ck", _decode_bing_ck("https://example.com/foo") is None)


def main() -> int:
    test_parse_initial_state_balanced()
    test_parse_initial_state_missing()
    test_parse_jsonld_hotel()
    test_decode_bing_ck()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}", file=sys.stderr)
        return 1
    print("All parser smoke-tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
