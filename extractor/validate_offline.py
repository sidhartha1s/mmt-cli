#!/usr/bin/env python3
"""Offline validator: load the saved 788KB MMT listing fixture via file://
and run EXTRACT_JS against it. Proves the parser independent of Akamai."""
import json, pathlib, sys
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))
from mmt_extract import EXTRACT_JS

FIXTURE = pathlib.Path('/tmp/mmt-replay/pw-out-dom/listing.html')
OUT = ROOT.parent / 'test-fixtures' / 'offline_extract.json'

def main():
    if not FIXTURE.exists():
        print(f"FIXTURE MISSING: {FIXTURE}", file=sys.stderr)
        sys.exit(1)
    print(f"[fixture] {FIXTURE} ({FIXTURE.stat().st_size:,}b)")
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        page.goto(f"file://{FIXTURE.resolve()}", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        cards = page.evaluate(EXTRACT_JS) or []
        print(f"[extract] cards={len(cards)}")
        for i, c in enumerate(cards[:10], 1):
            print(f"  {i}. {c.get('name')!r} @ {c.get('location')!r}")
            print(f"     price={c.get('price')!r}  rating={c.get('rating')} ({c.get('ratingLabel')})  reviews={c.get('reviewCount')}")
            print(f"     hotelId={c.get('hotelId')!r}  photos={c.get('photoCount')}")
            print(f"     badges={c.get('badges')!r}")
            print(f"     strikethrough={c.get('strikethroughPrices')!r}")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(cards, indent=2, ensure_ascii=False))
        print(f"\n[saved] {OUT}")
        browser.close()

    # Validation assertions
    assert len(cards) >= 3, f"expected >=3 cards, got {len(cards)}"
    names = {c.get('name') for c in cards}
    expected = {"SinQ Beach Resort", "Natures Nest Goa", "Ronil Goa, part of Hyatt"}
    found = names & expected
    print(f"\n[assert] found {len(found)}/3 expected names: {sorted(found)}")
    assert found, f"none of {expected} found in {names}"
    # Strikethrough filter check: no displayed price should match a strikethrough
    for c in cards:
        if c.get('price') and c.get('strikethroughPrices'):
            assert c['price'] not in c['strikethroughPrices'], \
                f"strikethrough leaked into price for {c['name']}: {c['price']}"
    print("[assert] strikethrough filter OK — no struck-out price leaked into 'price' field")
    print("\n[PASS] Offline validation succeeded.")

if __name__ == "__main__":
    main()
