#!/usr/bin/env python3
"""
Apartment hunter for 's-Hertogenbosch
Scrapes Funda and Pararius, sends Telegram notifications for new listings.
Filters: max €1250/month, min 50m², 1+ bedroom or studio, unfurnished.
"""

import json
import os
import re
import time

import cloudscraper
from bs4 import BeautifulSoup

# ── Config ───────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SEEN_FILE = "seen.json"

# cloudscraper handles anti-bot / Cloudflare challenges automatically
scraper = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "windows", "mobile": False}
)

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
        f"🏠 <b>{listing['title']}</b>\n"
        f"💶 {listing['price']}\n"
        f"📐 {listing['size']}\n"
        f"📍 {listing['address']}\n"
        f"🔗 <a href=\"{listing['url']}\">Bekijk op {listing['source']}</a>"
    )
    import requests as req
    if listing.get("image"):
        resp = req.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "photo": listing["image"],
                "caption": text,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        if resp.ok:
            return
        listing = {**listing, "image": None}

    req.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        },
        timeout=10,
    )


# ── Funda ─────────────────────────────────────────────────────────────────────

def scrape_funda() -> list:
    listings = []
    url = "https://www.funda.nl/huur/s-hertogenbosch/0-1250/+50woonopp/"
    try:
        r = scraper.get(url, timeout=20)
        print(f"[Funda] HTTP {r.status_code}")
        soup = BeautifulSoup(r.text, "lxml")

        # Print page title so we can spot captcha/block pages
        title_tag = soup.find("title")
        print(f"[Funda] Page title: {title_tag.get_text() if title_tag else 'none'}")

        # Modern Funda embeds all data in a Next.js __NEXT_DATA__ script tag
        script = soup.find("script", {"id": "__NEXT_DATA__"})
        if script:
            data = json.loads(script.string)
            props = data.get("props", {}).get("pageProps", {})
            print(f"[Funda] pageProps keys: {list(props.keys())}")

            result = props.get("searchResult") or props.get("initialData") or {}
            print(f"[Funda] result keys: {list(result.keys()) if isinstance(result, dict) else type(result)}")

            properties = result.get("Properties") or result.get("properties") or []
            print(f"[Funda] Raw property count: {len(properties)}")

            for p in properties:
                item = _funda_from_json(p)
                if item:
                    listings.append(item)
        else:
            print("[Funda] __NEXT_DATA__ not found — trying HTML fallback")
            for card in soup.select('[data-test-id="search-result-item"]'):
                item = _funda_from_html(card)
                if item:
                    listings.append(item)

    except Exception as e:
        print(f"[Funda] Error: {e}")

    print(f"[Funda] Found {len(listings)} listings")
    return listings


def _funda_from_json(p: dict) -> dict:
    try:
        relative_url = p.get("RelativeUrl", "")
        gid = p.get("GlobalId") or p.get("Id")
        if not gid:
            m = re.search(r"/(\d+)/", relative_url)
            gid = m.group(1) if m else None
        if not gid:
            return None

        listing_id = f"funda_{gid}"
        url = "https://www.funda.nl" + relative_url
        street  = f"{p.get('Street', '')} {p.get('HouseNumber', '')}".strip()
        city    = f"{p.get('ZipCode', '')} {p.get('City', '')}".strip()
        address = f"{street}, {city}".strip(", ")
        price   = p.get("PriceFormatted") or f"€ {p.get('Price', '?')}/mnd"
        size    = f"{p.get('LivingArea', '?')} m²"
        photos  = p.get("Photos") or []
        image   = p.get("MainPhotoUrl") or (photos[0] if photos else None)

        return {
            "id":      listing_id,
            "title":   street or address,
            "price":   price,
            "size":    size,
            "address": address,
            "url":     url,
            "image":   image,
            "source":  "Funda",
        }
    except Exception:
        return None


def _funda_from_html(card) -> dict:
    try:
        link = card.select_one("a[href*='/huur/']")
        if not link:
            return None
        href = link["href"]
        m = re.search(r"/(\d+)/", href)
        if not m:
            return None

        listing_id = f"funda_{m.group(1)}"
        url = "https://www.funda.nl" + href

        title_el = (
            card.select_one('[data-test-id="street"]')
            or card.select_one("h2")
        )
        title = title_el.get_text(strip=True) if title_el else "Onbekend"

        price_el = (
            card.select_one('[data-test-id="price-rent"]')
            or card.select_one(".search-result-price")
        )
        price = price_el.get_text(strip=True) if price_el else "?"

        size_el = card.select_one(".search-result-kenmerken")
        size = size_el.get_text(strip=True) if size_el else "?"

        img = card.select_one("img")
        image = (img.get("src") or img.get("data-src")) if img else None

        return {
            "id":      listing_id,
            "title":   title,
            "price":   price,
            "size":    size,
            "address": title,
            "url":     url,
            "image":   image,
            "source":  "Funda",
        }
    except Exception:
        return None


# ── Pararius ──────────────────────────────────────────────────────────────────

def scrape_pararius() -> list:
    listings = []
    url = "https://www.pararius.nl/huurwoningen/s-hertogenbosch/0-1250/50m2"
    try:
        r = scraper.get(url, timeout=20)
        print(f"[Pararius] HTTP {r.status_code}")
        soup = BeautifulSoup(r.text, "lxml")

        for item in soup.select("li.search-list__item--listing"):
            listing = _pararius_from_html(item)
            if listing:
                listings.append(listing)

    except Exception as e:
        print(f"[Pararius] Error: {e}")

    print(f"[Pararius] Found {len(listings)} listings")
    return listings


def _pararius_from_html(item) -> dict:
    try:
        link = item.select_one("a.listing-search-item__link--title")
        if not link:
            return None
        href = link["href"]
        url = "https://www.pararius.nl" + href
        listing_id = "pararius_" + href.strip("/").split("/")[-1]
        title = link.get_text(strip=True)

        price_el = item.select_one(".listing-search-item__price")
        price = price_el.get_text(strip=True) if price_el else "?"

        size_el = item.select_one(".illustrated-features__item--surface-area")
        size = size_el.get_text(strip=True) if size_el else "?"

        addr_el = item.select_one(".listing-search-item__location")
        address = addr_el.get_text(strip=True) if addr_el else "?"

        img = item.select_one("img")
        image = (img.get("src") or img.get("data-src")) if img else None

        return {
            "id":      listing_id,
            "title":   title,
            "price":   price,
            "size":    size,
            "address": address,
            "url":     url,
            "image":   image,
            "source":  "Pararius",
        }
    except Exception:
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    seen = load_seen()
    new_count = 0

    all_listings = scrape_funda() + scrape_pararius()
    print(f"Total listings found: {len(all_listings)}")

    for listing in all_listings:
        if listing["id"] not in seen:
            send_telegram(listing)
            seen.add(listing["id"])
            new_count += 1
            time.sleep(1)

    save_seen(seen)
    print(f"New notifications sent: {new_count}")


if __name__ == "__main__":
    main()
