import argparse
import csv
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# ============================================================
# CONFIGURATION & LOGGING
# ============================================================

DEFAULT_PRODUCT_URL = "https://eger.arukereso.hu/logitech/g-pro-x-superlight-2-910-006630-p996544828/?sgst=1"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================================
# DATA MODEL & HELPERS
# ============================================================

def format_huf(price: int) -> str:
    """Format integer price to Hungarian string format (e.g., 49 990 Ft)."""
    return f"{price:,}".replace(",", " ") + " Ft"


@dataclass
class Offer:
    price: int
    link: str
    shop: Optional[str] = None
    currency: str = "HUF"

    @property
    def formatted_price(self) -> str:
        return format_huf(self.price)


# ============================================================
# PARSING & EXTRACTION
# ============================================================

def parse_hungarian_price(text: str) -> Optional[int]:
    """Convert Hungarian price strings into integers."""
    if not text:
        return None

    text = text.strip()
    if "Ft" not in text and "HUF" not in text.upper():
        return None

    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def is_shop_offer_url(url: Optional[str]) -> bool:
    """Check if the URL is an outgoing shop redirect link (Broadened)."""
    if not url:
        return False
    
    url_lower = url.lower()
    # Check for common Árukereső redirect patterns, not just /goto/
    patterns = ["/goto/", "/redirect/", "offerid=", "/store/", "out.php"]
    return any(pattern in url_lower for pattern in patterns)


def extract_shop_name(element: Tag) -> Optional[str]:
    """Try several common locations to extract the shop name relative to the price element."""
    selectors = [
        ".shop-name", ".shopName", ".merchant-name", ".merchantName",
        "[class*='shop']", "[class*='merchant']", "img[alt]"
    ]

    container = element.find_parent(["li", "tr", "div", "article"])
    if not container:
        return None

    # First attempt: Look for text in shop classes
    for selector in selectors:
        node = container.select_one(selector)
        if node:
            # If it's an image (logo), try to get the alt text
            if node.name == "img":
                name = node.get("alt", "").strip()
            else:
                name = node.get_text(" ", strip=True)
            
            if name and "Ft" not in name and len(name) > 1:
                return name[:200]

    # Second attempt: inspect nearby links for text
    for a_tag in container.find_all("a", href=True):
        text = a_tag.get_text(" ", strip=True)
        if text and "Ft" not in text and len(text) < 100:
            return text

    return None


def extract_offer_link(element: Tag, base_url: str) -> Optional[str]:
    """Find the closest valid outgoing shop URL."""
    # 1. The element itself is an <a>
    if element.name == "a" and is_shop_offer_url(element.get("href")):
        return urljoin(base_url, element.get("href"))

    # 2. Search parent <a>
    parent_a = element.find_parent("a", href=True)
    if parent_a and is_shop_offer_url(parent_a.get("href")):
        return urljoin(base_url, parent_a.get("href"))

    # 3. Search the surrounding offer container
    container = element.find_parent(["li", "tr", "div", "article"])
    if container:
        for a_tag in container.find_all("a", href=True):
            href = a_tag.get("href")
            if is_shop_offer_url(href):
                return urljoin(base_url, href)

    return None


# ============================================================
# BROWSER & SCRAPING
# ============================================================

def load_page(page: Page, url: str, retries: int = 3, timeout: int = 60_000) -> str:
    """Load the page with Playwright, implementing retries and wait states."""
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            logger.info(f"Loading page (attempt {attempt}/{retries})...")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)

            # Auto-click cookie banners if they exist (prevents them from blocking the DOM)
            try:
                page.locator("button:has-text('Elfogadom'), button:has-text('Rendben'), button:has-text('Accept')").first.click(timeout=2000)
            except Exception:
                pass # No cookie banner found, just continue

            # Wait for network to quiet down so dynamic prices load
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeoutError:
                logger.debug("networkidle timeout; continuing.")

            # Scroll down slightly to trigger lazy-loaded elements
            page.evaluate("window.scrollBy(0, 500)")
            time.sleep(1)

            return page.content()

        except Exception as exc:
            last_error = exc
            logger.warning(f"Page load failed: {exc}")
            if attempt < retries:
                time.sleep(2 * attempt)

    raise RuntimeError(f"Unable to load page after {retries} attempts") from last_error


def extract_offers(soup: BeautifulSoup, base_url: str) -> list[Offer]:
    """Parse all valid offers from the BeautifulSoup object using dual-methods."""
    offers = []
    seen = set()

    # METHOD 1: Try finding by broad class names
    price_elements = soup.find_all(
        lambda tag: tag.name in {"a", "div", "span", "strong", "p"}
        and tag.has_attr("class")
        and any(kw in cls.lower() for cls in tag.get("class", []) for kw in ["price", "p-value", "offer", "val"])
    )

    # METHOD 2 (FALLBACK): If CSS classes changed, scan ALL text on the page for prices
    if not price_elements:
        logger.info("Standard classes not found. Using text-based regex extraction...")
        # Matches formats like "49 990 Ft", "49.990Ft", etc.
        texts = soup.find_all(string=re.compile(r"\d{1,3}(?:[ \.,]\d{3})*\s*(?:Ft|HUF)", re.IGNORECASE))
        price_elements = [text.parent for text in texts if text.parent]

    for element in price_elements:
        raw_text = element.get_text(" ", strip=True)
        price = parse_hungarian_price(raw_text)
        
        if price is None:
            continue

        link = extract_offer_link(element, base_url)
        if not link or not is_shop_offer_url(link):
            continue

        unique_key = (price, link)
        if unique_key in seen:
            continue

        seen.add(unique_key)
        shop = extract_shop_name(element)
        offers.append(Offer(price=price, link=link, shop=shop))

    return offers


def scrape_product(url: str, headless: bool = True, min_price: Optional[int] = None, max_price: Optional[int] = None) -> dict:
    """Main scraping pipeline: load page, parse DOM, clean data."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        with browser.new_context(
            user_agent=USER_AGENT,
            locale="hu-HU",
            viewport={"width": 1440, "height": 1000},
            extra_http_headers={"Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8"}
        ) as context:
            page = context.new_page()
            html = load_page(page, url)

    soup = BeautifulSoup(html, "html.parser")
    
    # Extract Product Name
    h1 = soup.find("h1")
    title = soup.find("title")
    product_name = h1.get_text(" ", strip=True) if h1 else (title.get_text(" ", strip=True) if title else "Unknown Product")

    # Extract & Filter Offers
    raw_offers = extract_offers(soup, url)
    cleaned_offers = []
    
    for offer in raw_offers:
        if min_price and offer.price < min_price: continue
        if max_price and offer.price > max_price: continue
        cleaned_offers.append(offer)

    cleaned_offers.sort(key=lambda o: o.price)

    return {
        "product": product_name,
        "url": url,
        "offer_count": len(cleaned_offers),
        "lowest_price": cleaned_offers[0].price if cleaned_offers else None,
        "offers": cleaned_offers,
    }

# ============================================================
# I/O & DISPLAY
# ============================================================

def save_json(result: dict, filename: str):
    data = {**result, "offers": [asdict(o) for o in result["offers"]]}
    Path(filename).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Saved JSON: {filename}")


def save_csv(offers: list[Offer], filename: str):
    with open(filename, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=["price", "currency", "shop", "link"])
        writer.writeheader()
        for offer in offers:
            writer.writerow(asdict(offer))
    logger.info(f"Saved CSV: {filename}")


def print_results(result: dict):
    print(f"\n{'=' * 70}\nPRODUCT\n{'=' * 70}\n{result['product']}\n")

    if not result["offers"]:
        print("❌ No shop offers found.\nThe page may have changed or the product currently has no offers.")
        return

    print(f"🏪 Offers found: {result['offer_count']}\n\n💰 LOWEST PRICE\n{'-' * 70}")
    
    cheapest = result["offers"][0]
    print(f"✅ {cheapest.formatted_price}")
    if cheapest.shop:
        print(f"🏪 Shop: {cheapest.shop}")
    print(f"🔗 {cheapest.link}\n\n📊 ALL OFFERS\n{'-' * 70}")

    for idx, offer in enumerate(result["offers"], start=1):
        shop = offer.shop or "Unknown shop"
        print(f"{idx:02d}. {offer.formatted_price:>14} | {shop}\n    {offer.link}")

    # Display Statistics
    prices = [o.price for o in result["offers"]]
    if prices:
        print(f"\n📈 PRICE STATISTICS\n{'-' * 70}")
        print(f"Lowest:  {format_huf(min(prices))}")
        print(f"Highest: {format_huf(max(prices))}")
        print(f"Average: {format_huf(sum(prices) // len(prices))}")
        print(f"Spread:  {format_huf(max(prices) - min(prices))}")


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Scrape product prices from Árukereső.")
    parser.add_argument("--url", default=DEFAULT_PRODUCT_URL, help="Target Árukereső product URL")
    parser.add_argument("--json", default="price_results.json", help="Output JSON filepath")
    parser.add_argument("--csv", default="price_results.csv", help="Output CSV filepath")
    parser.add_argument("--show-browser", action="store_true", help="Run Playwright in non-headless mode")
    args = parser.parse_args()

    result = scrape_product(url=args.url, headless=not args.show_browser)
    print_results(result)
    
    if result["offers"]:
        save_json(result, args.json)
        save_csv(result["offers"], args.csv)


if __name__ == "__main__":
    main()