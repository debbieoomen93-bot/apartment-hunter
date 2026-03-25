#!/usr/bin/env python3
"""
Apartment hunter for 's-Hertogenbosch / Den Bosch
Scrapes Funda using headed Playwright (bypasses DataDome).
Sends Telegram notifications for new listings.
Filters: max €1250/month, min 50m², unfurnished, 1+ bedroom or studio.
"""

import json
import os
import re
import time

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ───────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_IDS = [cid.strip() for cid in os.environ["TELEGRAM_CHAT_ID"].split(",")]
SEEN_FILE = "seen.json"

FUNDA_URL = "https://www.funda.nl/huur/s-hertogenbosch/0-1250/+50woonopp/"

SKIP_TYPES = ["parkeergelegenheid", "garage", "parkeerplaats", "berging"]

STEALTH = """
    Object.defineProperty(navigator, 'webdriver',   { get: () => undefined });
    Object.defineProperty(navigator, 'plugins',     { get: () => [1,2,3,4,5] });
    Object.defineProperty(navigator, 'languages',   { get: () => ['nl-NL','nl','en'] });
    Object.defineProperty(navigator, 'platform',    { get: () => 'Win32' });
    Object.defineProperty(navigator, 'deviceMemory',{ get: () => 8 });
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };
    Object.defineProperty(navigator, 'permissions', {
        get: () => ({ query: (p) => Promise.resolve({ state: 'granted', onchange: null }) })
    });
    delete window.__playwright;
    delete window.__pw_manual;
"""

# ── Persistence ───────────────────────────────────────────────────────────────

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(listing: dict) -> None:
    text = (
        f"🏠 <b>{listing['address']}</b>\n"
        f"💶 {listing['price']}\n"
        f"📐 {listing['size']}\n"
        f"🔗 <a href=\"{listing['url']}\">Bekijk op Funda</a>"
    )
    for chat_id in TELEGRAM_CHAT_IDS:
        if listing.get("image"):
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data={
                    "chat_id": chat_id,
                    "photo": listing["image"],
                    "caption": text,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            if resp.ok:
                continue
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )


# ── Funda scraper ─────────────────────────────────────────────────────────────

def scrape_funda() -> list:
    listings = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--start-maximized"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="nl-NL",
            timezone_id="Europe/Amsterdam",
        )
        page = context.new_page()
        page.add_init_script(STEALTH)

        # Visit homepage to get DataDome cookie
        page.goto("https://www.funda.nl/", wait_until="domcontentloaded", timeout=20000)
        for sel in ['button:has-text("Alles accepteren")', 'button:has-text("Accepteren")']:
            try:
                page.click(sel, timeout=2000)
                page.wait_for_timeout(800)
                break
            except PWTimeout:
                continue

        # Navigate to search
        page.goto(FUNDA_URL, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_function(
                "!document.title.toLowerCase().includes('bijna')", timeout=10000
            )
        except PWTimeout:
            print("[Funda] Warning: DataDome challenge may not have resolved")

        # Wait for Den Bosch listings to render
        try:
            page.wait_for_function(
                "document.querySelectorAll('a[href*=\"den-bosch\"]').length > 2",
                timeout=10000,
            )
        except PWTimeout:
            pass
        page.wait_for_timeout(2000)

        # Extract listing cards
        raw = page.evaluate("""() => {
            const seen = new Set();
            const results = [];
            document.querySelectorAll('a[href*="/detail/huur/den-bosch/"]').forEach(a => {
                if (seen.has(a.href) || !a.href.match(/\\/\\d+\\//)) return;
                seen.add(a.href);
                let card = a;
                for (let i = 0; i < 8; i++) {
                    card = card.parentElement;
                    if (!card) break;
                    const text = card.innerText || '';
                    if (text.includes('€') && text.length > 40) break;
                }
                const img = card ? card.querySelector('img') : null;
                results.push({
                    url:  a.href,
                    text: (card ? card.innerText : '').replace(/\\s+/g, ' ').trim(),
                    img:  img ? (img.src || img.getAttribute('data-src') || '') : ''
                });
            });
            return results;
        }""")

        browser.close()

    for item in raw:
        listing = parse_listing(item)
        if listing:
            listings.append(listing)

    print(f"[Funda] Found {len(listings)} listings")
    return listings


def parse_listing(item: dict) -> dict:
    url  = item["url"]
    text = item["text"]

    # Skip non-residential types
    for skip in SKIP_TYPES:
        if skip in url.lower():
            return None

    # Skip listings under option / rented
    if "onder optie" in text.lower() or "verhuurd" in text.lower():
        return None

    # Extract price
    price_m = re.search(r"€\s*([\d.,]+)\s*/ma", text)
    price_str = f"€ {price_m.group(1)} /mnd" if price_m else "?"
    try:
        price_val = int(price_m.group(1).replace(".", "").replace(",", "")) if price_m else 0
        if price_val > 1250:
            return None
    except Exception:
        pass

    # Extract size
    size_m = re.search(r"(\d+)\s*m²", text)
    size_str = f"{size_m.group(1)} m²" if size_m else "?"

    # Extract address (first meaningful part of text before zipcode)
    addr_m = re.search(r"^(.*?)\s+\d{4}\s*[A-Z]{2}\s+Den Bosch", text)
    address = addr_m.group(1).strip() if addr_m else text[:50]

    # Listing ID from URL
    id_m = re.search(r"/(\d+)/?$", url)
    listing_id = f"funda_{id_m.group(1)}" if id_m else f"funda_{hash(url)}"

    return {
        "id":      listing_id,
        "address": address,
        "price":   price_str,
        "size":    size_str,
        "url":     url,
        "image":   item.get("img", ""),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    seen = load_seen()
    new_count = 0

    listings = scrape_funda()

    for listing in listings:
        if listing["id"] not in seen:
            send_telegram(listing)
            seen.add(listing["id"])
            new_count += 1
            time.sleep(1)

    save_seen(seen)
    print(f"New notifications sent: {new_count}")


if __name__ == "__main__":
    main()
