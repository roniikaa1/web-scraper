#!/usr/bin/env python3
"""
Árukereső price scraper (Playwright + BeautifulSoup)

Features
  * Multiple products per run (positional URLs, --url, --urls-file)
  * Three extraction strategies: JSON-LD -> strict DOM -> loose DOM
  * Per-offer scoping (no more "all digits in a giant div" garbage prices)
  * Price range / one-offer-per-shop / top-N filters
  * SQLite price history with change detection (vs. last run and all-time low)
  * Alerts: --alert-below, new all-time low, optional webhook (Slack/Discord)
  * Watch mode (--watch MINUTES) for continuous monitoring
  * JSON + CSV export, debug HTML dump when nothing is found / page is blocked

Install:
    pip install playwright beautifulsoup4
    playwright install chromium

Examples:
    python arukereso_scraper.py
    python arukereso_scraper.py URL1 URL2 --min-price 20000 --one-per-shop
    python arukereso_scraper.py --alert-below 45000 --webhook https://hooks.slack.com/...
    python arukereso_scraper.py --watch 30 --alert-below 45000
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sqlite3
import sys
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

# ============================================================
# CONFIGURATION & LOGGING
# ============================================================

DEFAULT_PRODUCT_URL = (
    "https://eger.arukereso.hu/logitech/g-pro-x-superlight-2-910-006630-p996544828/?sgst=1"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# URL fragments that identify an outgoing "go to the shop" redirect
REDIRECT_PATTERNS = ("/goto/", "/redirect/", "offerid=", "/store/", "out.php")

# Hosts that are never shops (used by the loose fallback strategy)
IGNORED_HOSTS = (
    "arukereso.hu", "compari.ro", "google.", "facebook.", "instagram.", "youtube.",
    "twitter.", "x.com", "linkedin.", "apple.com", "gstatic.", "doubleclick.",
)

SHOP_SELECTORS = (
    ".shop-name", ".shopName", ".merchant-name", ".merchantName",
    "[class*='shop-name']", "[class*='store-name']", "[class*='merchant']", "[class*='shop']",
)

# Link texts that are buttons, not shop names
GENERIC_LINK_TEXT = re.compile(
    r"^(tovább|megveszem|vásárlás|vásárol|ugrás|részletek|ajánlat|kosár|megnézem|"
    r"go to|buy|visit|view|shop)", re.IGNORECASE,
)

# Context that means "this Ft amount is not the product price"
NON_PRICE_CONTEXT = re.compile(r"szállít|postak|előleg|részlet|/\s*hó|havi|törlesztő", re.IGNORECASE)
STALE_CLASS_TOKENS = {"old", "original", "was", "strike", "crossed", "previous"}

PRICE_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:[ \u00a0\u202f.,]\d{3})+|\d{3,9})\s*(?:Ft|HUF)\b", re.IGNORECASE
)
MIN_SANE_PRICE = 100  # anything below this is parsing noise

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("arukereso")

# ============================================================
# DATA MODEL & HELPERS
# ============================================================


def format_huf(price: int, signed: bool = False) -> str:
    """49990 -> '49 990 Ft' (optionally with +/- sign for deltas)."""
    sign = ""
    if signed:
        sign = "+" if price > 0 else "-" if price < 0 else "±"
        price = abs(price)
    return f"{sign}{price:,}".replace(",", " ") + " Ft"


@dataclass
class Offer:
    price: int
    link: str
    shop: Optional[str] = None
    currency: str = "HUF"
    source: str = "dom"  # jsonld | dom | dom-loose

    @property
    def formatted_price(self) -> str:
        return format_huf(self.price)


def slugify_url(url: str) -> str:
    segment = urlparse(url).path.strip("/").split("/")[-1] or "product"
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", segment)[:80]


# ============================================================
# PARSING & EXTRACTION
# ============================================================


def find_prices(text: str) -> list[int]:
    """Return every HUF amount found in text, in order of appearance."""
    prices = []
    for match in PRICE_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group(1))
        if digits and int(digits) >= MIN_SANE_PRICE:
            prices.append(int(digits))
    return prices


def parse_hungarian_price(text: str) -> Optional[int]:
    """First HUF price in text (handles ranges like '49 990 Ft - 52 000 Ft' correctly)."""
    prices = find_prices(text)
    return prices[0] if prices else None


def _is_stale_or_noise(string: NavigableString, stop: Tag) -> bool:
    """True if the text node is a crossed-out / old price or shipping/installment info."""
    if NON_PRICE_CONTEXT.search(str(string)):
        return True
    for parent in string.parents:
        if parent is stop:
            break
        if parent.name in ("del", "s", "strike", "script", "style"):
            return True
        classes = parent.get("class", []) if isinstance(parent, Tag) else []
        for cls in classes:
            if STALE_CLASS_TOKENS & set(re.split(r"[-_\s]", cls.lower())):
                return True
    return False


def price_text(node: Tag) -> str:
    """Visible text of node minus old prices, shipping costs, installments."""
    return " ".join(
        str(s).strip() for s in node.find_all(string=True) if not _is_stale_or_noise(s, node)
    )


def _registrable(host: str) -> str:
    return ".".join(host.lower().split(".")[-2:])


def is_shop_offer_url(url: Optional[str], base_url: str = "", strict: bool = True) -> bool:
    """Is this an outgoing shop link? strict=False also accepts any external host."""
    if not url:
        return False
    low = url.lower()
    if low.startswith(("javascript:", "mailto:", "tel:", "#")):
        return False
    if any(p in low for p in REDIRECT_PATTERNS):
        return True
    if strict:
        return False
    host = urlparse(url).netloc
    if not host or _registrable(host) == _registrable(urlparse(base_url).netloc):
        return False
    return not any(bad in host.lower() for bad in IGNORED_HOSTS)


def _offer_links(node: Tag, base_url: str, strict: bool) -> set[str]:
    links = set()
    for a in ([node] if node.name == "a" else []) + node.find_all("a", href=True):
        href = a.get("href")
        if href and is_shop_offer_url(urljoin(base_url, href), base_url, strict):
            links.add(urljoin(base_url, href))
    return links


def _is_page_root(node: Optional[Tag]) -> bool:
    return node is None or node.name in ("body", "html", "[document]")


def find_price_node(anchor: Tag, base_url: str, strict: bool, max_depth: int = 6) -> Optional[Tag]:
    """Smallest ancestor (or self) holding a price - rejected if it spans several offers."""
    node: Optional[Tag] = anchor
    for _ in range(max_depth):
        if _is_page_root(node):
            return None
        if find_prices(price_text(node)):
            return node if len(_offer_links(node, base_url, strict)) <= 1 else None
        node = node.parent
    return None


def find_offer_scope(anchor: Tag, base_url: str, strict: bool, max_depth: int = 4) -> Tag:
    """Largest ancestor that still belongs to exactly one offer (used for shop name)."""
    scope, node = anchor, anchor.parent
    for _ in range(max_depth):
        if _is_page_root(node) or len(_offer_links(node, base_url, strict)) > 1:
            break
        scope, node = node, node.parent
    return scope


def _plausible_shop(name: str, product_name: str = "") -> bool:
    name = (name or "").strip()
    if not (2 <= len(name) <= 80):
        return False
    if find_prices(name) or GENERIC_LINK_TEXT.match(name):
        return False
    if product_name and name.lower() in product_name.lower():
        return False
    return True


def extract_shop_name(scope: Tag, anchor: Tag, product_name: str = "") -> Optional[str]:
    """Shop name from class hints, then logo alt/title, then link text."""
    for selector in SHOP_SELECTORS:
        for node in scope.select(selector):
            text = node.get_text(" ", strip=True)
            if _plausible_shop(text, product_name):
                return text
    for img in scope.find_all("img"):
        for attr in ("alt", "title"):
            value = (img.get(attr) or "").strip()
            if _plausible_shop(value, product_name):
                return value
    for node in scope.find_all(["b", "strong"]):
        text = node.get_text(" ", strip=True)
        if _plausible_shop(text, product_name):
            return text
    for a in [anchor] + scope.find_all("a", href=True):
        for value in (a.get("title") or "", a.get_text(" ", strip=True)):
            if _plausible_shop(value, product_name):
                return value[:80]
    return None


def extract_dom_offers(
    soup: BeautifulSoup, base_url: str, product_name: str = "", strict: bool = True
) -> list[Offer]:
    """Walk every shop-redirect link and pair it with its own price and shop name."""
    offers: list[Offer] = []
    seen: set[tuple[int, str]] = set()

    for anchor in soup.find_all("a", href=True):
        link = urljoin(base_url, anchor["href"])
        if not is_shop_offer_url(link, base_url, strict):
            continue
        price_node = find_price_node(anchor, base_url, strict)
        if price_node is None:
            continue
        price = parse_hungarian_price(price_text(price_node))
        if price is None or (price, link) in seen:
            continue
        seen.add((price, link))
        scope = find_offer_scope(anchor, base_url, strict)
        offers.append(
            Offer(price, link, extract_shop_name(scope, anchor, product_name),
                  source="dom" if strict else "dom-loose")
        )
    return offers


def _walk_json(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_json(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_json(item)


def extract_jsonld_offers(soup: BeautifulSoup, base_url: str) -> tuple[list[Offer], dict]:
    """Offers from schema.org JSON-LD (most stable source when present).

    Returns (offers, aggregate) where aggregate may hold the page's own
    lowPrice/offerCount so we can cross-check our result.
    """
    offers: list[Offer] = []
    aggregate: dict = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk_json(data):
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            if "AggregateOffer" in types:
                aggregate = {"low": node.get("lowPrice"), "count": node.get("offerCount")}
            if "Offer" not in types:
                continue
            if str(node.get("priceCurrency", "HUF")).upper() != "HUF":
                continue
            try:
                price = int(round(float(str(node.get("price")).replace(" ", "").replace(",", "."))))
            except (TypeError, ValueError):
                continue
            if price < MIN_SANE_PRICE:
                continue
            seller = node.get("seller")
            shop = seller.get("name") if isinstance(seller, dict) else seller
            link = urljoin(base_url, node.get("url") or base_url)
            offers.append(Offer(price, link, shop if isinstance(shop, str) else None, source="jsonld"))
    return offers, aggregate


def extract_offers(soup: BeautifulSoup, base_url: str, product_name: str = "") -> tuple[list[Offer], dict]:
    """Try strategies from most to least reliable."""
    offers, aggregate = extract_jsonld_offers(soup, base_url)
    if offers:
        logger.info(f"JSON-LD strategy: {len(offers)} offers")
        return offers, aggregate

    offers = extract_dom_offers(soup, base_url, product_name, strict=True)
    if offers:
        logger.info(f"Strict DOM strategy: {len(offers)} offers")
        return offers, aggregate

    logger.info("No redirect links matched; trying loose external-link strategy...")
    offers = extract_dom_offers(soup, base_url, product_name, strict=False)
    if offers:
        logger.warning(f"Loose DOM strategy: {len(offers)} offers (verify results!)")
    return offers, aggregate


# ============================================================
# BROWSER & SCRAPING
# ============================================================

COOKIE_BUTTONS = (
    "button:has-text('Elfogadom')", "button:has-text('Elfogadom mindet')",
    "button:has-text('Rendben')", "button:has-text('Összes elfogadása')",
    "button:has-text('Accept all')", "button:has-text('Accept')",
)
OFFER_LINK_SELECTOR = ", ".join(f"a[href*='{p}']" for p in REDIRECT_PATTERNS)
BLOCK_MARKERS = ("captcha", "access denied", "are you a robot", "unusual traffic", "cf-challenge")


@contextmanager
def browser_session(headless: bool = True, block_media: bool = True):
    """One browser + context shared by all URLs; always cleaned up."""
    from playwright.sync_api import sync_playwright  # lazy: parsing works without Playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
                locale="hu-HU",
                viewport={"width": 1440, "height": 1000},
                extra_http_headers={"Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8"},
            )
            if block_media:  # faster; logo <img alt> text stays in the DOM
                context.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type in {"image", "font", "media"}
                    else route.continue_(),
                )
            yield context
        finally:
            browser.close()


def load_page(page, url: str, retries: int = 3, timeout: int = 60_000) -> str:
    """Load the page with retries; wait for offer links rather than sleeping blindly."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            logger.info(f"Loading page (attempt {attempt}/{retries})...")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)

            for selector in COOKIE_BUTTONS:  # dismiss consent banner if present
                try:
                    page.locator(selector).first.click(timeout=1500)
                    break
                except Exception:
                    continue

            try:
                page.wait_for_selector(OFFER_LINK_SELECTOR, timeout=8_000)
            except PlaywrightTimeoutError:
                logger.debug("No offer links appeared within 8s; continuing.")
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightTimeoutError:
                logger.debug("networkidle timeout; continuing.")

            # Scroll in steps to trigger lazy-loaded offer lists
            for _ in range(4):
                page.mouse.wheel(0, 800)
                page.wait_for_timeout(300)

            return page.content()
        except Exception as exc:
            last_error = exc
            logger.warning(f"Page load failed: {exc}")
            if attempt < retries:
                time.sleep(2 * attempt)

    raise RuntimeError(f"Unable to load page after {retries} attempts") from last_error


def parse_html(
    html: str, url: str, min_price: Optional[int] = None, max_price: Optional[int] = None,
    one_per_shop: bool = False,
) -> dict:
    """HTML -> result dict. Pure function, easy to unit-test."""
    soup = BeautifulSoup(html, "html.parser")
    h1, title = soup.find("h1"), soup.find("title")
    product_name = (
        h1.get_text(" ", strip=True) if h1
        else title.get_text(" ", strip=True) if title else "Unknown Product"
    )

    offers, aggregate = extract_offers(soup, url, product_name)
    offers = [
        o for o in offers
        if (min_price is None or o.price >= min_price) and (max_price is None or o.price <= max_price)
    ]
    offers.sort(key=lambda o: o.price)

    if one_per_shop:  # keep cheapest offer per known shop (list is already sorted)
        seen_shops: set[str] = set()
        deduped = []
        for o in offers:
            key = (o.shop or "").lower()
            if key and key in seen_shops:
                continue
            seen_shops.add(key)
            deduped.append(o)
        offers = deduped

    if aggregate.get("low") and offers:
        try:
            if int(float(aggregate["low"])) != offers[0].price:
                logger.warning(
                    f"Page reports lowPrice {aggregate['low']} but scraped lowest is {offers[0].price}"
                )
        except (TypeError, ValueError):
            pass

    return {
        "product": product_name,
        "url": url,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "offer_count": len(offers),
        "lowest_price": offers[0].price if offers else None,
        "lowest_shop": offers[0].shop if offers else None,
        "offers": offers,
    }


def dump_debug_html(html: str, url: str, debug_dir: Path) -> Path:
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / f"{slugify_url(url)}_{datetime.now():%Y%m%d_%H%M%S}.html"
    path.write_text(html, encoding="utf-8")
    return path


def scrape_product(context, url: str, args: argparse.Namespace) -> dict:
    page = context.new_page()
    try:
        html = load_page(page, url, retries=args.retries, timeout=args.timeout * 1000)
    finally:
        page.close()

    if any(marker in html.lower() for marker in BLOCK_MARKERS):
        logger.warning("Page looks like a bot-check/captcha. Try --show-browser or a longer --delay.")

    result = parse_html(html, url, args.min_price, args.max_price, args.one_per_shop)
    if not result["offers"]:
        path = dump_debug_html(html, url, Path(args.debug_dir))
        logger.warning(f"No offers found; raw HTML saved to {path} for inspection.")
    return result


# ============================================================
# PRICE HISTORY & ALERTS
# ============================================================


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS history (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               ts TEXT NOT NULL, url TEXT NOT NULL, product TEXT,
               lowest INTEGER, lowest_shop TEXT, offer_count INTEGER)"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_url ON history(url, id)")
    conn.commit()
    return conn


def record_history(conn: sqlite3.Connection, result: dict) -> dict:
    """Store this run; return {'prev': ..., 'prev_ts': ..., 'all_time_low': ...} from BEFORE this run."""
    row = conn.execute(
        "SELECT lowest, ts FROM history WHERE url=? AND lowest IS NOT NULL ORDER BY id DESC LIMIT 1",
        (result["url"],),
    ).fetchone()
    low = conn.execute(
        "SELECT MIN(lowest) FROM history WHERE url=? AND lowest IS NOT NULL", (result["url"],)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO history (ts, url, product, lowest, lowest_shop, offer_count) VALUES (?,?,?,?,?,?)",
        (result["scraped_at"], result["url"], result["product"], result["lowest_price"],
         result["lowest_shop"], result["offer_count"]),
    )
    conn.commit()
    return {"prev": row[0] if row else None, "prev_ts": row[1] if row else None, "all_time_low": low}


def evaluate_alerts(result: dict, hist: dict, alert_below: Optional[int]) -> list[str]:
    lowest = result["lowest_price"]
    if lowest is None:
        return []
    shop = f" @ {result['lowest_shop']}" if result["lowest_shop"] else ""
    alerts = []
    if alert_below is not None and lowest <= alert_below:
        alerts.append(f"{result['product']}: {format_huf(lowest)}{shop} is at/below your target {format_huf(alert_below)}")
    if hist["all_time_low"] is not None and lowest < hist["all_time_low"]:
        alerts.append(f"{result['product']}: NEW ALL-TIME LOW {format_huf(lowest)}{shop} (was {format_huf(hist['all_time_low'])})")
    return alerts


def send_webhook(url: str, message: str) -> None:
    payload = json.dumps({"text": message, "content": message}).encode("utf-8")  # Slack + Discord
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
        logger.info("Webhook notification sent.")
    except Exception as exc:
        logger.warning(f"Webhook failed: {exc}")


# ============================================================
# I/O & DISPLAY
# ============================================================


def save_json(result: dict, filename: Path) -> None:
    data = {**result, "offers": [asdict(o) for o in result["offers"]]}
    filename.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Saved JSON: {filename}")


def save_csv(offers: list[Offer], filename: Path) -> None:
    with open(filename, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=["price", "currency", "shop", "link", "source"])
        writer.writeheader()
        for offer in offers:
            writer.writerow(asdict(offer))
    logger.info(f"Saved CSV: {filename}")


def print_results(result: dict, hist: Optional[dict] = None, top: Optional[int] = None) -> None:
    print(f"\n{'=' * 70}\nPRODUCT\n{'=' * 70}\n{result['product']}\n{result['url']}\n")

    if not result["offers"]:
        print("❌ No shop offers found.\nThe page may have changed, been blocked, or has no offers.")
        return

    offers = result["offers"]
    cheapest = offers[0]
    print(f"🏪 Offers found: {result['offer_count']}\n\n💰 LOWEST PRICE\n{'-' * 70}")
    print(f"✅ {cheapest.formatted_price}")
    if cheapest.shop:
        print(f"🏪 Shop: {cheapest.shop}")
    print(f"🔗 {cheapest.link}")

    if hist and hist["prev"] is not None:
        delta = cheapest.price - hist["prev"]
        arrow = "📉" if delta < 0 else "📈" if delta > 0 else "➖"
        print(f"{arrow} Since last check ({hist['prev_ts']}): {format_huf(delta, signed=True)}")
        if hist["all_time_low"] is not None:
            print(f"🏆 All-time low tracked: {format_huf(min(hist['all_time_low'], cheapest.price))}")

    shown = offers[:top] if top else offers
    print(f"\n📊 ALL OFFERS{f' (top {top})' if top else ''}\n{'-' * 70}")
    for idx, offer in enumerate(shown, start=1):
        print(f"{idx:02d}. {offer.formatted_price:>14} | {offer.shop or 'Unknown shop'}\n    {offer.link}")

    prices = [o.price for o in offers]
    median = sorted(prices)[len(prices) // 2]
    print(f"\n📈 PRICE STATISTICS\n{'-' * 70}")
    print(f"Lowest:  {format_huf(min(prices))}")
    print(f"Highest: {format_huf(max(prices))}")
    print(f"Average: {format_huf(sum(prices) // len(prices))}")
    print(f"Median:  {format_huf(median)}")
    print(f"Spread:  {format_huf(max(prices) - min(prices))}")


# ============================================================
# MAIN
# ============================================================


def collect_urls(args: argparse.Namespace) -> list[str]:
    urls = list(args.urls)
    if args.url:
        urls.append(args.url)
    if args.urls_file:
        lines = Path(args.urls_file).read_text(encoding="utf-8").splitlines()
        urls += [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]
    return list(dict.fromkeys(urls)) or [DEFAULT_PRODUCT_URL]  # dedupe, keep order


def run_once(urls: list[str], args: argparse.Namespace, conn: Optional[sqlite3.Connection]) -> int:
    """Scrape all URLs once. Returns the number of failures."""
    failures = 0
    out_dir = Path(args.out_dir)
    if not args.no_save:
        out_dir.mkdir(parents=True, exist_ok=True)

    with browser_session(headless=not args.show_browser, block_media=not args.load_images) as context:
        for i, url in enumerate(urls):
            if i:
                time.sleep(args.delay + random.uniform(0, 1.5))  # be polite
            try:
                result = scrape_product(context, url, args)
            except Exception as exc:
                failures += 1
                logger.error(f"Failed to scrape {url}: {exc}")
                continue

            hist = record_history(conn, result) if conn and result["offers"] else None
            print_results(result, hist, args.top)

            if result["offers"]:
                if not args.no_save:
                    base = out_dir / slugify_url(url)
                    save_json(result, base.with_suffix(".json"))
                    save_csv(result["offers"], base.with_suffix(".csv"))
                for alert in evaluate_alerts(result, hist or {"all_time_low": None}, args.alert_below):
                    print(f"\n🚨 ALERT: {alert}")
                    if args.webhook:
                        send_webhook(args.webhook, f"🚨 {alert}\n{result['offers'][0].link}")
            else:
                failures += 1
    return failures


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scrape and track product prices from Árukereső.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("urls", nargs="*", help="Product URL(s)")
    p.add_argument("--url", help="Single product URL (kept for backwards compatibility)")
    p.add_argument("--urls-file", help="Text file with one URL per line (# comments allowed)")

    f = p.add_argument_group("filtering")
    f.add_argument("--min-price", type=int, help="Ignore offers below this price (HUF)")
    f.add_argument("--max-price", type=int, help="Ignore offers above this price (HUF)")
    f.add_argument("--one-per-shop", action="store_true", help="Keep only the cheapest offer per shop")
    f.add_argument("--top", type=int, help="Only print the N cheapest offers")

    o = p.add_argument_group("output")
    o.add_argument("--out-dir", default="output", help="Directory for <product>.json / .csv")
    o.add_argument("--no-save", action="store_true", help="Don't write JSON/CSV files")
    o.add_argument("--debug-dir", default="debug", help="Where raw HTML is dumped when no offers are found")
    o.add_argument("-v", "--verbose", action="store_true", help="Debug logging")

    h = p.add_argument_group("history & alerts")
    h.add_argument("--db", default="price_history.db", help="SQLite history file")
    h.add_argument("--no-history", action="store_true", help="Disable price history tracking")
    h.add_argument("--alert-below", type=int, help="Alert when lowest price <= this (HUF)")
    h.add_argument("--webhook", help="Slack/Discord-compatible webhook URL for alerts")
    h.add_argument("--watch", type=float, metavar="MINUTES", help="Re-check every N minutes until Ctrl+C")

    b = p.add_argument_group("browser")
    b.add_argument("--show-browser", action="store_true", help="Run Playwright non-headless")
    b.add_argument("--load-images", action="store_true", help="Don't block images/fonts (slower)")
    b.add_argument("--retries", type=int, default=3, help="Page load attempts")
    b.add_argument("--timeout", type=int, default=60, help="Page load timeout (seconds)")
    b.add_argument("--delay", type=float, default=2.0, help="Seconds between URLs")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    urls = collect_urls(args)
    conn = None if args.no_history else init_db(args.db)

    try:
        while True:
            failures = run_once(urls, args, conn)
            if not args.watch:
                return 1 if failures else 0
            wait = args.watch * 60 + random.uniform(0, 30)
            logger.info(f"Next check in {wait / 60:.1f} min (Ctrl+C to stop)")
            time.sleep(wait)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())