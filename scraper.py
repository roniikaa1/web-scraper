from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

def get_accurate_lowest_price():
    url = "https://eger.arukereso.hu/logitech/g-pro-x-superlight-2-910-006630-p996544828/?sgst=1"
    
    with sync_playwright() as p:
        print("Launching browser...")
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="hu-HU"
        )
        page = context.new_page()
        
        print("Navigating to Árukereső...")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        
        try:
            page.wait_for_selector("a.price, div.price, span.price", timeout=10000)
        except Exception:
            pass

        html_content = page.content()
        browser.close()

    soup = BeautifulSoup(html_content, 'html.parser')
    
    h1 = soup.find('h1')
    product_name = h1.text.strip() if h1 else "Unknown Product"
    print(f"\n🎯 Scraping Product: {product_name}")

    offers = []

    for element in soup.find_all(['a', 'div', 'span']):
        class_list = element.get('class', [])
        if class_list and any('price' in cls.lower() for cls in class_list):
            raw_price = element.text.strip()
            
            if 'Ft' in raw_price:
                clean_digits = ''.join(filter(str.isdigit, raw_price))
                if clean_digits:
                    price_val = int(clean_digits)
                    
                    link = None
                    if element.name == 'a' and element.get('href'):
                        link = element.get('href')
                    else:
                        parent_a = element.find_parent('a')
                        if parent_a and parent_a.get('href'):
                            link = parent_a.get('href')
                        else:
                            parent_container = element.find_parent(['div', 'tr', 'li'])
                            if parent_container:
                                a_tag = parent_container.find('a', href=True)
                                if a_tag:
                                    link = a_tag['href']
                    
                    # ---------------------------------------------------------
                    # THE FIX: Only save the offer if it is a "goto" store link
                    # ---------------------------------------------------------
                    if link and '/goto/' in link:
                        full_link = urljoin(url, link)
                        offers.append({
                            'price': price_val,
                            'link': full_link
                        })

    if offers:
        cheapest_offer = min(offers, key=lambda x: x['price'])
        formatted_price = f"{cheapest_offer['price']:,}".replace(',', ' ')
        
        print(f"✅ Lowest price found: {formatted_price} Ft")
        print(f"🔗 Link to offer: {cheapest_offer['link']}")
    else:
        print("❌ No shop offers found. (Double check if the product has available stores).")

if __name__ == "__main__":
    get_accurate_lowest_price()