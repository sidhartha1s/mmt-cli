#!/usr/bin/env python3
"""
mmt_extract.py — MakeMyTrip hotel listing extractor (DOM-scrape).

Why this exists:
  MMT search-hotels JSON API is gated by Akamai Bot Manager. Even with
  fresh Firefox cookies and a perfect TLS fingerprint (page.evaluate inside
  Playwright), the XHR endpoint returns a 6-byte sentinel for unvalidated
  sessions. The HTML listing page, however, ships fully hydrated — every
  hotel card is in the DOM, including the strikethrough "old prices" that
  corrupt copy-paste. So: scrape the rendered DOM, filter out line-through
  nodes via getComputedStyle, output clean JSON.

Anti-block tactics (in order of effectiveness):
  1. Persistent browser profile (--profile-dir) — Akamai trust accumulates
     across runs; first run may be blocked, third run almost always passes.
  2. playwright_stealth — patches navigator.webdriver and other tells.
  3. Sentinel detection ("200-OK" 6-byte page) -> auto-retry with backoff.
  4. Engine fallback (--engine firefox|chromium|webkit) — different TLS
     fingerprints; if one is flagged, another may pass.

Usage:
  ./mmt_extract.py --city goa --checkin 2026-05-15 --checkout 2026-05-16
  ./mmt_extract.py --listing-url "https://..."
  ./mmt_extract.py --city bangalore --checkin 2026-06-10 --checkout 2026-06-12 \\
       --engine firefox --profile-dir ~/.mmt-profile --max-cards 30
"""
import argparse, json, sys, re, time, pathlib, datetime, os
from typing import Any
from playwright.sync_api import sync_playwright
try:
    from playwright_stealth import Stealth
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

CITY_CODES = {
    "goa":       {"city": "CTGOI", "lat": "15.5439", "lng": "73.7553", "label": "Goa, India"},
    "bangalore": {"city": "CTBLR", "lat": "12.9716", "lng": "77.5946", "label": "Bangalore, India"},
    "bengaluru": {"city": "CTBLR", "lat": "12.9716", "lng": "77.5946", "label": "Bengaluru, India"},
    "mumbai":    {"city": "CTBOM", "lat": "19.0760", "lng": "72.8777", "label": "Mumbai, India"},
    "delhi":     {"city": "CTDEL", "lat": "28.6139", "lng": "77.2090", "label": "Delhi, India"},
    "jaipur":    {"city": "CTJAI", "lat": "26.9124", "lng": "75.7873", "label": "Jaipur, India"},
    "udaipur":   {"city": "CTUDR", "lat": "24.5854", "lng": "73.7125", "label": "Udaipur, India"},
    "kolkata":   {"city": "CTCCU", "lat": "22.5726", "lng": "88.3639", "label": "Kolkata, India"},
    "chennai":   {"city": "CTMAA", "lat": "13.0827", "lng": "80.2707", "label": "Chennai, India"},
    "hyderabad": {"city": "CTHYD", "lat": "17.3850", "lng": "78.4867", "label": "Hyderabad, India"},
    "manali":    {"city": "CTMNL", "lat": "32.2396", "lng": "77.1887", "label": "Manali, India"},
    "shimla":    {"city": "CTSML", "lat": "31.1048", "lng": "77.1734", "label": "Shimla, India"},
    "ooty":      {"city": "CTOTY", "lat": "11.4064", "lng": "76.6932", "label": "Ooty, India"},
    "pondicherry": {"city": "CTPNY", "lat": "11.9416", "lng": "79.8083", "label": "Pondicherry, India"},
}

SENTINEL_TEXT = "200-OK"
SENTINEL_MAX_LEN = 200  # if rendered body text is shorter than this AND contains the sentinel

def log(*a, **k):
    print(*a, file=sys.stderr, flush=True, **k)

def build_listing_url(city: str, checkin: str, checkout: str) -> str:
    if city.lower() not in CITY_CODES:
        raise SystemExit(f"unknown city '{city}'. known: {sorted(CITY_CODES)}")
    c = CITY_CODES[city.lower()]
    def fmt(d: str) -> str:
        try:
            dt = datetime.datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            raise SystemExit(f"bad date '{d}'; want YYYY-MM-DD")
        return dt.strftime("%m%d%Y")
    label = c["label"].replace(" ", "%20").replace(",", "%2C")
    return (
        "https://www.makemytrip.com/hotels/hotel-listing/"
        f"?checkin={fmt(checkin)}&checkout={fmt(checkout)}"
        f"&city={c['city']}&country=IN&lat={c['lat']}&lng={c['lng']}"
        f"&locusId={c['city']}&locusType=city"
        f"&searchText={label}&type=hotel"
    )

EXTRACT_JS = r"""
() => {
    const cards = [];
    const seen = new Set();
    const cardEls = document.querySelectorAll('[class*="listingRow"]');
    function visibleText(el) {
        const out = [];
        const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
        let n;
        while ((n = walker.nextNode())) {
            const t = (n.nodeValue || '').trim();
            if (!t) continue;
            const parent = n.parentElement;
            if (!parent) continue;
            const td = getComputedStyle(parent).textDecoration || '';
            if (td.includes('line-through')) continue;
            out.push(t);
        }
        return out;
    }
    function strikePrices(el) {
        const arr = [];
        el.querySelectorAll('p, span, div').forEach(n => {
            const td = getComputedStyle(n).textDecoration || '';
            if (td.includes('line-through')) {
                const t = (n.innerText || '').trim();
                if (t) arr.push(t);
            }
        });
        return arr;
    }
    for (const el of cardEls) {
        const key = el.innerText.slice(0, 120);
        if (seen.has(key)) continue;
        seen.add(key);
        const tokens = visibleText(el);
        const fullText = tokens.join('\n');

        const isJunkName = (t) => !t
            || t.length < 3 || t.length > 100
            || /^[\d\s|]+$/.test(t)
            || /Image\s+\d+/i.test(t)
            || /Photos?\s*(?:&|and)?\s*Videos?/i.test(t)
            || /^This property is part of/i.test(t)
            || /Spotlight program/i.test(t);
        let name = null;
        for (let i = 0; i < tokens.length; i++) {
            if (/\d+\s*Photos?\s*(?:&|and)?\s*Videos?/i.test(tokens[i])) {
                for (let j = i + 1; j < tokens.length; j++) {
                    if (!isJunkName(tokens[j])) { name = tokens[j]; break; }
                }
                break;
            }
        }
        if (!name) for (const t of tokens) { if (!isJunkName(t)) { name = t; break; } }
        let location = null;
        if (name) {
            const idx = tokens.indexOf(name);
            for (let j = idx + 1; j < Math.min(idx + 4, tokens.length); j++) {
                const cand = tokens[j];
                if (cand && cand.length < 200 && !/^\|$/.test(cand)) {
                    location = cand.replace(/^\|\s*/, '').trim();
                    break;
                }
            }
        }
        let price = null;
        for (const t of tokens) {
            const m = t.match(/(?:₹|Rs\.?|INR)\s*([\d][\d,]{2,})/);
            if (m) {
                const digits = m[1].replace(/,/g, '');
                if (parseInt(digits, 10) >= 100) { price = m[0]; break; }
            }
        }
        // Rating: tokens often split as ['Excellent', '4.5']. Try joined-pairs first.
        let rating = null, ratingLabel = null;
        const labelRe = /^(Exceptional|Excellent|Very Good|Good|Average|Pleasant|Decent)$/i;
        for (let i = 0; i < tokens.length; i++) {
            const m = tokens[i].match(/(Exceptional|Excellent|Very Good|Good|Average|Pleasant|Decent)\s*([\d.]+)/i);
            if (m) { ratingLabel = m[1]; rating = parseFloat(m[2]); break; }
            if (labelRe.test(tokens[i]) && i + 1 < tokens.length) {
                const next = tokens[i + 1];
                if (/^\d\.\d$/.test(next) || /^\d$/.test(next)) {
                    ratingLabel = tokens[i]; rating = parseFloat(next); break;
                }
            }
        }
        // Review count: search joined text for "(N Ratings)" — tokens often split.
        let reviewCount = null;
        const rcMatch = fullText.replace(/\s+/g, ' ').match(/\(\s*([\d,]+)\s*Ratings?\s*\)/i);
        if (rcMatch) reviewCount = parseInt(rcMatch[1].replace(/,/g, ''), 10);
        let detailUrl = null, hotelId = null;
        const a = el.querySelector('a[href*="hotelId="], a[href*="hotel-details"]');
        if (a) {
            detailUrl = a.href;
            let m = detailUrl.match(/[?&]hotelId=([^&]+)/);
            if (!m) m = detailUrl.match(/\/hotel-details\/([^/?]+)/);
            if (m) hotelId = m[1];
        }
        let photoCount = null;
        const m = fullText.match(/(\d+)\s*Photos?\s*(?:&|and)?\s*Videos?/i);
        if (m) photoCount = parseInt(m[1], 10);
        const strikes = strikePrices(el);
        const badges = [];
        if (name) {
            const startIdx = tokens.indexOf(name) + 2;
            const ratingFullRe = /(Exceptional|Excellent|Very Good|Good|Average|Pleasant|Decent)/i;
            for (let j = startIdx; j < tokens.length; j++) {
                const t = tokens[j];
                if (ratingFullRe.test(t)) break;
                if (/^(?:₹|Rs\.?|INR)\s*[\d,]+/.test(t)) break;
                if (/Ratings?/i.test(t)) break;
                if (/^[\d.]+$/.test(t)) continue;
                if (/^\|+$/.test(t)) continue;
                if (/^\(+$/.test(t)) continue;
                if (t.length > 80) continue;
                if (/^[\d\s]+$/.test(t)) continue;
                badges.push(t);
                if (badges.length >= 6) break;
            }
        }
        cards.push({
            name, location, price, rating, ratingLabel, reviewCount,
            detailUrl, hotelId, photoCount, badges,
            strikethroughPrices: strikes,
        });
    }
    return cards;
}
"""

def detect_sentinel(page) -> bool:
    """Akamai degraded-bucket page: title or body is just '200-OK'."""
    try:
        title = page.title() or ""
        body_text = page.evaluate("() => document.body ? document.body.innerText.slice(0, 400) : ''") or ""
    except Exception:
        return False
    body_text = body_text.strip()
    if SENTINEL_TEXT in title and len(title) < 30:
        return True
    if SENTINEL_TEXT in body_text and len(body_text) < SENTINEL_MAX_LEN:
        return True
    return False

def make_context(p, engine: str, profile_dir: str | None, headless: bool):
    """Returns (browser_or_None, context). If profile_dir is set, uses
    launch_persistent_context which returns directly a context (no browser)."""
    engines = {
        "firefox": p.firefox,
        "chromium": p.chromium,
        "webkit": p.webkit,
    }
    if engine not in engines:
        raise SystemExit(f"unknown engine '{engine}'")
    bt = engines[engine]
    common_args = dict(
        viewport={"width": 1366, "height": 900},
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        java_script_enabled=True,
    )
    if profile_dir:
        pd = pathlib.Path(os.path.expanduser(profile_dir))
        pd.mkdir(parents=True, exist_ok=True)
        log(f"[ctx] persistent profile: {pd}")
        ctx = bt.launch_persistent_context(str(pd), headless=headless, **common_args)
        return None, ctx
    log("[ctx] ephemeral context")
    browser = bt.launch(headless=headless)
    ctx = browser.new_context(**common_args)
    return browser, ctx

def warm_up(page):
    """Hit homepage + hotels landing to seed cookies and look human."""
    log("[warm] homepage...")
    try:
        page.goto("https://www.makemytrip.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        page.evaluate("window.scrollBy(0, 600)")
        page.wait_for_timeout(1500)
    except Exception as e:
        log(f"[warm] homepage warn: {e}")
    log("[warm] hotels landing...")
    try:
        page.goto("https://www.makemytrip.com/hotels/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        page.evaluate("window.scrollBy(0, 400)")
        page.wait_for_timeout(1500)
    except Exception as e:
        log(f"[warm] hotels warn: {e}")

def hydrate_and_count(page, max_cards: int) -> int:
    page.wait_for_timeout(2500)
    last_count = -1
    plateau = 0
    for round_idx in range(20):
        try:
            page.evaluate("window.scrollBy(0, 1100)")
        except Exception:
            pass
        page.wait_for_timeout(1400)
        try:
            count = page.evaluate(
                """() => new Set(
                    Array.from(document.querySelectorAll('[class*="listingRow"]'))
                      .map(e => e.innerText.slice(0,120))
                   ).size"""
            )
        except Exception:
            count = -1
        log(f"[scroll] round={round_idx+1} unique-cards={count}")
        if count == last_count:
            plateau += 1
            if plateau >= 2 and count >= max_cards:
                break
            if plateau >= 4:
                break
        else:
            plateau = 0
        last_count = count
        if count >= max_cards:
            break
    return last_count

def run_once(p, listing_url: str, engine: str, profile_dir: str | None,
             headless: bool, max_cards: int, screenshot: str | None,
             save_html: str | None, do_warm: bool) -> dict:
    started = time.time()
    log(f"[run] engine={engine}  headless={headless}  warm={do_warm}")
    browser, ctx = make_context(p, engine, profile_dir, headless)
    if STEALTH_AVAILABLE:
        try:
            Stealth().apply_stealth_sync(ctx)
            log("[stealth] applied")
        except Exception as e:
            log(f"[stealth] warn: {e}")

    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    if do_warm:
        warm_up(page)

    log(f"[nav] {listing_url[:120]}...")
    try:
        page.goto(listing_url, wait_until="domcontentloaded", timeout=75000)
    except Exception as e:
        log(f"[nav] listing warn: {e}")

    page.wait_for_timeout(2000)
    sentinel = detect_sentinel(page)
    log(f"[gate] sentinel-detected={sentinel}")

    cards: list[Any] = []
    final_count = 0
    if not sentinel:
        final_count = hydrate_and_count(page, max_cards)
        if save_html:
            try:
                pathlib.Path(save_html).write_text(page.content())
                log(f"[html] saved {save_html}")
            except Exception as e:
                log(f"[html] err: {e}")
        log("[extract] running in-page extractor")
        try:
            cards = page.evaluate(EXTRACT_JS) or []
        except Exception as e:
            log(f"[extract] err: {e}")
        cards = cards[:max_cards]

    if screenshot:
        try:
            page.screenshot(path=screenshot, full_page=False)
            log(f"[shot] saved {screenshot}")
        except Exception as e:
            log(f"[shot] err: {e}")

    cookies = ctx.cookies()
    abck = next((c for c in cookies if c["name"] == "_abck"), None)
    meta = {
        "listing_url": listing_url,
        "captured_at_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "elapsed_seconds": round(time.time() - started, 2),
        "engine": engine,
        "headless": headless,
        "profile_dir": profile_dir,
        "card_count": len(cards),
        "sentinel_blocked": sentinel,
        "_abck_len": len(abck["value"]) if abck else 0,
        "cookie_count": len(cookies),
        "user_agent": page.evaluate("() => navigator.userAgent"),
    }

    try:
        ctx.close()
    except Exception:
        pass
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass

    return {"meta": meta, "hotels": cards}

def extract_with_retry(listing_url: str, engine_chain: list[str], profile_dir: str | None,
                       headless: bool, max_cards: int, screenshot: str | None,
                       save_html: str | None, max_retries: int) -> dict:
    """Try each engine, retrying with backoff if Akamai sentinel returns."""
    last_result = None
    with sync_playwright() as p:
        attempt = 0
        for engine in engine_chain:
            for retry in range(max_retries):
                attempt += 1
                do_warm = retry == 0  # warm only first try per engine
                shot = screenshot if attempt == 1 else None
                html_path = save_html if attempt == 1 else None
                result = run_once(p, listing_url, engine, profile_dir, headless,
                                  max_cards, shot, html_path, do_warm)
                last_result = result
                if not result["meta"]["sentinel_blocked"] and result["meta"]["card_count"] > 0:
                    return result
                # decide: retry, swap engine, or give up
                if result["meta"]["sentinel_blocked"]:
                    backoff = 30 + retry * 30  # 30, 60, 90s
                    log(f"[retry] sentinel blocked. waiting {backoff}s before retry...")
                    time.sleep(backoff)
                else:
                    # not blocked, just empty — engine selectors may not match.
                    # try a different engine immediately.
                    log("[retry] no sentinel but 0 cards; switching engine")
                    break
            log(f"[retry] giving up on engine={engine}, trying next")
    return last_result or {"meta": {"card_count": 0, "sentinel_blocked": True}, "hotels": []}

def main():
    ap = argparse.ArgumentParser(description="MMT hotel-listing extractor (DOM-scrape, strikethrough-aware)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--city")
    src.add_argument("--listing-url")
    ap.add_argument("--checkin")
    ap.add_argument("--checkout")
    ap.add_argument("--max-cards", type=int, default=40)
    ap.add_argument("--out")
    ap.add_argument("--headless", action="store_true", default=False)
    ap.add_argument("--engine", default="firefox", choices=["firefox", "chromium", "webkit"])
    ap.add_argument("--engine-chain", help="comma-separated fallback chain, e.g. firefox,webkit,chromium")
    ap.add_argument("--profile-dir", help="persistent browser profile dir (recommended for repeat runs)")
    ap.add_argument("--screenshot")
    ap.add_argument("--save-html", help="save raw page.content() to this path (debug)")
    ap.add_argument("--max-retries", type=int, default=2, help="retries per engine on sentinel block")
    args = ap.parse_args()

    if args.city:
        if not (args.checkin and args.checkout):
            ap.error("--city requires --checkin and --checkout")
        url = build_listing_url(args.city, args.checkin, args.checkout)
    else:
        url = args.listing_url

    headless = args.headless
    if not headless and not os.environ.get("DISPLAY"):
        headless = True
        log("[mode] no DISPLAY -> headless")

    engine_chain = [args.engine]
    if args.engine_chain:
        engine_chain = [e.strip() for e in args.engine_chain.split(",") if e.strip()]
    log(f"[engines] {engine_chain}")

    result = extract_with_retry(
        url, engine_chain, args.profile_dir, headless,
        args.max_cards, args.screenshot, args.save_html, args.max_retries,
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        pathlib.Path(args.out).write_text(payload)
        log(f"[done] wrote {args.out}  hotels={result['meta']['card_count']}")
    else:
        print(payload)
    log(f"[done] hotels={result['meta']['card_count']}  sentinel={result['meta'].get('sentinel_blocked')}")
    if result["meta"]["card_count"] == 0:
        sys.exit(2)

if __name__ == "__main__":
    main()
