import json
import os
import random
import re
import time
import requests
from playwright.sync_api import sync_playwright

try:
    from playwright_stealth import stealth_sync
except ImportError:
    stealth_sync = None

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
PRODUCTS_FILE = "products.json"
HISTORY_FILE = "history.json"


def send_telegram(message: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram credentials missing.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")


def normalize_amazon_eg_url(url: str) -> tuple[str, str]:
    """Extracts ASIN and forces clean English amazon.eg URL for consistent parsing."""
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    if match:
        asin = match.group(1)
        return asin, f"https://www.amazon.eg/-/en/dp/{asin}"
    return url, url


def parse_price(text: str) -> float | None:
    if not text:
        return None
    arabic_digits = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    text = text.translate(arabic_digits)
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    if cleaned.count(".") > 1:
        parts = cleaned.split(".")
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


def extract_price(page) -> tuple[float | None, str]:
    """Returns (price, status) where status is 'ok', 'oos', or 'blocked'."""
    content = page.content().lower()
    if "validatecaptcha" in content or "type the characters you see in this image" in content:
        return None, "blocked"

    avail_el = page.query_selector("#availability")
    if avail_el:
        avail_text = avail_el.inner_text().lower()
        if "currently unavailable" in avail_text or "غير متوفر" in avail_text:
            return None, "oos"

    selectors = [
        "#corePriceDisplay_desktop_feature_div .a-price.priceToPay .a-offscreen",
        "#corePrice_feature_div .a-price .a-offscreen",
        "#apex_desktop .a-price .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
    ]

    for selector in selectors:
        el = page.query_selector(selector)
        if el:
            price = parse_price(el.inner_text())
            if price:
                return price, "ok"

    whole_el = page.query_selector("#corePriceDisplay_desktop_feature_div .a-price-whole")
    if whole_el:
        frac_el = page.query_selector("#corePriceDisplay_desktop_feature_div .a-price-fraction")
        whole = whole_el.inner_text().strip().replace(".", "")
        frac = frac_el.inner_text().strip() if frac_el else "00"
        price = parse_price(f"{whole}.{frac}")
        if price:
            return price, "ok"

    return None, "unknown"


def main():
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        products = json.load(f)

    history = {}
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)

    blocked_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/129.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Africa/Cairo",
            viewport={"width": 1366, "height": 768},
        )
        page = context.new_page()
        if stealth_sync:
            stealth_sync(page)

        for idx, item in enumerate(products):
            name = item.get("name", "منتج")
            raw_url = item["url"]
            target_price = item.get("target_price")
            key, clean_url = normalize_amazon_eg_url(raw_url)

            price = None
            status = "unknown"

            for attempt in range(2):
                try:
                    page.goto(clean_url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(random.randint(2500, 4500))
                    price, status = extract_price(page)
                    if status in ("ok", "oos"):
                        break
                except Exception as e:
                    print(f"[{name}] Error on attempt {attempt + 1}: {e}")
                time.sleep(5)

            print(f"[{name}] Status: {status} | Price: {price}")

            if status == "blocked":
                blocked_count += 1
            elif status == "ok" and price is not None:
                prev_record = history.get(key, {})
                last_price = prev_record.get("last_price")

                should_notify = False
                reason = ""

                if target_price is not None:
                    if price <= float(target_price) and (last_price is None or price < last_price):
                        should_notify = True
                        reason = f"وصل للسعر المستهدف ({target_price:,.0f} ج.م)"
                else:
                    if last_price is not None and price < last_price:
                        should_notify = True
                        diff = last_price - price
                        reason = f"انخفض بمقدار {diff:,.0f} ج.م (كان {last_price:,.0f} ج.م)"

                if should_notify:
                    msg = (
                        f"📉 <b>تخفيض جديد على أمازون مصر!</b>\n\n"
                        f"📦 <b>المنتج:</b> {name}\n"
                        f"💰 <b>السعر الحالي:</b> {price:,.2f} ج.م\n"
                        f"📌 <b>التفاصيل:</b> {reason}\n\n"
                        f"🔗 <a href='{clean_url}'>فتح صفحة المنتج</a>"
                    )
                    send_telegram(msg)

                history[key] = {
                    "name": name,
                    "last_price": price,
                    "lowest_price": min(price, prev_record.get("lowest_price", price)),
                }

            if idx < len(products) - 1:
                time.sleep(random.uniform(4, 8))

        browser.close()

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    if len(products) > 0 and blocked_count == len(products):
        send_telegram("⚠️ <b>تنبيه:</b> أمازون حظر محاولة الفحص الحالية بصفحة Captcha، سيعاود النظام المحاولة في الدورة القادمة.")


if __name__ == "__main__":
    main()
