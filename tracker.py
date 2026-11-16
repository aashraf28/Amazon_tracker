from datetime import datetime, timedelta, timezone
import html
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
EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "")
PRODUCTS_FILE = "products.json"
HISTORY_FILE = "history.json"
CAIRO_TZ = timezone(timedelta(hours=2))

ARABIC_MONTHS = {
    1: "يناير", 2: "فبراير", 3: "مارس", 4: "أبريل",
    5: "مايو", 6: "يونيو", 7: "يوليو", 8: "أغسطس",
    9: "سبتمبر", 10: "أكتوبر", 11: "نوفمبر", 12: "ديسمبر",
}

SELLER_LABELS = {
    "all": "الكل (أمازون وتجار)",
    "amazon": "أمازون فقط (Amazon.eg)",
    "warehouse": "مستودع أمازون (Warehouse)",
    "third_party": "تجار عاديين فقط",
}

REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "🔍 افحص الأسعار الآن"}, {"text": "📋 قائمة المنتجات"}],
        [{"text": "🗑️ حذف منتج"}, {"text": "🏪 فلتر البائع"}],
        [{"text": "❓ مساعدة"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def format_arabic_datetime(dt: datetime) -> str:
    month_name = ARABIC_MONTHS[dt.month]
    hour_12 = dt.hour % 12 or 12
    period = "ص" if dt.hour < 12 else "م"
    return f"{dt.day} {month_name} {dt.year}، {hour_12:02d}:{dt.minute:02d} {period}"


def tg_api(method: str, payload: dict):
    if not BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=15)
        return resp.json()
    except Exception as e:
        print(f"Telegram API error ({method}): {e}")
        return None


def send_telegram(message: str, reply_markup: dict | None = None):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram credentials missing.")
        return
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": reply_markup if reply_markup is not None else REPLY_KEYBOARD,
    }
    tg_api("sendMessage", payload)


def setup_bot_menu():
    """Registers the native dropdown command menu in Telegram."""
    commands = [
        {"command": "scan", "description": "🔍 تشغيل فحص الأسعار الآن"},
        {"command": "list", "description": "📋 عرض قائمة المنتجات والأسعار المسجلة"},
        {"command": "remove", "description": "🗑️ حذف منتج من المتابعة بالأزرار"},
        {"command": "seller", "description": "🏪 اختيار البائع (أمازون / ويرهاوس / الكل)"},
        {"command": "help", "description": "❓ طريقة إضافة منتج واستخدام البوت"},
    ]
    tg_api("setMyCommands", {"commands": commands})


def resolve_amazon_url(raw_url: str) -> tuple[str | None, str]:
    """Extracts ASIN and forces clean English amazon.eg URL."""
    url = raw_url.strip()
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    if match:
        asin = match.group(1)
        return asin, f"https://www.amazon.eg/-/en/dp/{asin}"
    return None, url


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


def extract_seller(page) -> tuple[str, str]:
    """Returns (seller_display_name, seller_type: 'amazon' | 'warehouse' | 'third_party' | 'unknown')."""
    seller_name = ""

    rows = page.query_selector_all("#tabular-buybox .tabular-buybox-text")
    for row in rows:
        attr = (row.get_attribute("tabular-attribute-name") or "").lower()
        if "sold by" in attr or "يباع" in attr:
            seller_name = row.inner_text().strip()
            break

    if not seller_name:
        el = page.query_selector("#merchantInfoFeature_feature_div .offer-display-feature-text-message")
        if el:
            seller_name = el.inner_text().strip()

    if not seller_name:
        el = page.query_selector("#sellerProfileTriggerId")
        if el:
            seller_name = el.inner_text().strip()

    if not seller_name:
        el = page.query_selector("#merchant-info")
        if el:
            txt = el.inner_text().strip()
            m = re.search(r"sold by\s+([^\.]+)", txt, re.IGNORECASE)
            if m:
                seller_name = m.group(1).strip()
            elif "amazon.eg" in txt.lower():
                seller_name = "Amazon.eg"

    if not seller_name:
        return "غير محدد", "unknown"

    low = seller_name.lower()
    if "warehouse" in low or "مستودع" in low or "resale" in low:
        return seller_name, "warehouse"
    if "amazon" in low or "أمازون" in low:
        return seller_name, "amazon"
    return seller_name, "third_party"


def extract_product_title(page) -> str | None:
    el = page.query_selector("#productTitle")
    if el:
        title = re.sub(r"\s+", " ", el.inner_text()).strip()
        return title[:70] + ("..." if len(title) > 70 else "")
    return None


def extract_other_offers_note(page) -> str:
    """Checks if Amazon page displays 'New & Used / Other Sellers' starting price."""
    for sel in ("#olp_feature_div", "#aod-ingress-link"):
        el = page.query_selector(sel)
        if el:
            txt = re.sub(r"\s+", " ", el.inner_text()).strip()
            price_match = re.search(r"(?:EGP|ج\.م)\s*([\d,]+(?:\.\d+)?)", txt, re.IGNORECASE)
            if price_match:
                return f"تبدأ من {price_match.group(1)} ج.م"
    return "شيّك على المخزون"


def extract_page_data(page) -> tuple[float | None, str, str, str, str | None, str]:
    """Returns (price, status, seller_name, seller_type, page_title, resale_note)."""
    content = page.content().lower()
    if "validatecaptcha" in content or "type the characters you see in this image" in content:
        return None, "blocked", "غير محدد", "unknown", None, "شيّك على المخزون"

    title = extract_product_title(page)
    resale_note = extract_other_offers_note(page)

    for oos_selector in ("#availability", "#outOfStock"):
        oos_el = page.query_selector(oos_selector)
        if oos_el:
            oos_text = oos_el.inner_text().lower()
            if any(phrase in oos_text for phrase in ("currently unavailable", "غير متوفر", "نفدت الكمية")):
                return None, "oos", "غير متوفر", "unknown", title, resale_note

    seller_name, seller_type = extract_seller(page)

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
                return price, "ok", seller_name, seller_type, title, resale_note

    whole_el = page.query_selector("#corePriceDisplay_desktop_feature_div .a-price-whole")
    if whole_el:
        frac_el = page.query_selector("#corePriceDisplay_desktop_feature_div .a-price-fraction")
        whole = whole_el.inner_text().strip().replace(".", "")
        frac = frac_el.inner_text().strip() if frac_el else "00"
        price = parse_price(f"{whole}.{frac}")
        if price:
            return price, "ok", seller_name, seller_type, title, resale_note

    return None, "unknown", seller_name, seller_type, title, resale_note


def get_drop_timeframe_label(records: list[dict], current_price: float, all_time_min: float, now_dt: datetime) -> str:
    if not records:
        return ""
    if current_price <= all_time_min:
        return "🏆 <b>أقل سعر تاريخي منذ بداية المتابعة!</b>"

    windows = [
        (60, "آخر شهرين"),
        (30, "آخر شهر"),
        (7, "آخر أسبوع"),
        (3, "آخر 3 أيام"),
    ]
    for days, label in windows:
        cutoff = now_dt - timedelta(days=days)
        window_prices = []
        for r in records:
            try:
                ts = datetime.fromisoformat(r["ts"])
                if ts >= cutoff and r.get("price"):
                    window_prices.append(float(r["price"]))
            except Exception:
                continue
        if len(window_prices) >= 2 and current_price <= min(window_prices):
            return f"🔥 <b>أقل سعر خلال {label}!</b>"
    return ""


def build_alert_card(
    header: str,
    name: str,
    asin: str | None,
    clean_url: str,
    price: float,
    price_subline: str,
    badge_line: str,
    seller_name: str,
    seller_filter_label: str,
    resale_note: str,
    now_dt: datetime,
) -> tuple[str, dict]:
    safe_name = html.escape(name)
    safe_seller = html.escape(seller_name)
    safe_filter = html.escape(seller_filter_label)
    offers_url = f"https://www.amazon.eg/-/en/dp/{asin}?aod=1" if asin else clean_url
    asin_line = f"\n└ 🆔 <code>{asin}</code>" if asin else ""
    sub_block = f"\n{price_subline}" if price_subline else ""
    badge_block = f"\n{badge_line}" if badge_line else ""

    msg = (
        f"{header}\n\n"
        f"📦 <b>{safe_name}</b>{asin_line}\n\n"
        f"💰 <b>السعر الحالي:</b> {price:,.2f} ج.م{sub_block}{badge_block}\n"
        f"🏢 <b>البائع:</b> {safe_seller}\n"
        f"🎛️ <b>فلتر البائع:</b> {safe_filter}\n\n"
        f"💡 <b>خيارات تانية:</b>\n"
        f"└ 📦 <a href='{offers_url}'>أمازون ريسيل ({resale_note})</a>\n\n"
        f"📅 السعر بتاريخ {format_arabic_datetime(now_dt)}"
    )

    inline_markup = {
        "inline_keyboard": [
            [{"text": "🛒 شوفه على أمازون", "url": clean_url}]
        ]
    }
    return msg, inline_markup


def send_products_summary(products: list[dict], prod_records: dict):
    """Sends the full updated product list automatically after a manual scan."""
    if not products:
        send_telegram("✅ <b>انتهى الفحص اليدوي!</b>\n📭 قائمتك فارغة حالياً. أرسل رابط أي منتج من أمازون مصر لإضافته فوراً.")
        return

    header = "✅ <b>انتهى الفحص اليدوي — قائمة المنتجات المحدّثة:</b>\n\n"
    blocks = []

    for idx, item in enumerate(products):
        asin, clean_url = resolve_amazon_url(item["url"])
        key = asin or clean_url
        rec = prod_records.get(key, {})
        status = rec.get("status")
        last_price = rec.get("last_price")
        safe_name = html.escape(item.get("name", f"منتج ({key})"))

        if status == "oos":
            curr = f"❌ غير متوفر حالياً (آخر سعر: {float(last_price):,.2f} ج.م)" if last_price else "❌ غير متوفر حالياً"
        elif last_price is not None:
            curr = f"{float(last_price):,.2f} ج.م"
        else:
            curr = "⏳ تعذّر جلب السعر في هذه الدورة"

        low = f"{float(rec['min_price']):,.2f} ج.م" if rec.get("min_price") is not None else "-"
        actual_seller = html.escape(rec.get("last_seller") or ("غير متوفر" if status == "oos" else "غير محدد"))
        seller_filter_label = html.escape(SELLER_LABELS.get(item.get("seller", "all"), "الكل (أمازون وتجار)"))
        target_note = f" | 🎯 التارجت: {float(item['target_price']):,.2f} ج.م" if item.get("target_price") is not None else ""

        blocks.append(
            f"{idx + 1}. <a href='{clean_url}'><b>{safe_name}</b></a>\n"
            f"   💰 السعر الحالي: {curr} | 📉 الأقل: {low}{target_note}\n"
            f"   🏢 البائع الحالي: {actual_seller}\n"
            f"   🎛️ فلتر البائع: {seller_filter_label}"
        )

    current_msg = header
    for block in blocks:
        if len(current_msg) + len(block) + 2 > 3800:
            send_telegram(current_msg.strip())
            current_msg = block + "\n\n"
        else:
            current_msg += block + "\n\n"

    if current_msg.strip():
        send_telegram(current_msg.strip())


def create_warm_page(browser):
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="Africa/Cairo",
        viewport={"width": 1366, "height": 768},
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9,ar-EG;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        },
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    page = context.new_page()
    if stealth_sync:
        try:
            stealth_sync(page)
        except Exception:
            pass

    try:
        page.goto("https://www.amazon.eg/-/en/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(random.randint(3500, 5500))
    except Exception as e:
        print(f"Warmup note: {e}")

    return context, page


def main():
    setup_bot_menu()

    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        products = json.load(f)

    history_data = {"products": {}}
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if "products" in loaded:
                history_data = loaded

    products_changed = False
    prod_records = history_data.setdefault("products", {})
    blocked_count = 0
    now_dt = datetime.now(CAIRO_TZ)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context, page = create_warm_page(browser) if products else (None, None)

        for idx, item in enumerate(products):
            raw_url = item["url"]
            asin, clean_url = resolve_amazon_url(raw_url)
            key = asin or clean_url
            name = item.get("name", f"منتج ({key})")
            target_price = item.get("target_price")
            seller_filter = item.get("seller", "all")
            seller_filter_label = SELLER_LABELS.get(seller_filter, "الكل (أمازون وتجار)")

            price = None
            status = "unknown"
            seller_name = "غير محدد"
            seller_type = "unknown"
            page_title = None
            resale_note = "شيّك على المخزون"

            for attempt in range(2):
                try:
                    page.goto(
                        clean_url,
                        wait_until="domcontentloaded",
                        referer="https://www.amazon.eg/-/en/",
                        timeout=45000,
                    )
                    page.wait_for_timeout(random.randint(3000, 5000))
                    price, status, seller_name, seller_type, page_title, resale_note = extract_page_data(page)
                    if status in ("ok", "oos"):
                        break
                    if status == "blocked" and attempt == 0:
                        context.close()
                        time.sleep(5)
                        context, page = create_warm_page(browser)
                except Exception as e:
                    print(f"[{name}] Error on attempt {attempt + 1}: {e}")
                    time.sleep(4)

            if page_title and name.startswith("منتج أمازون ("):
                item["name"] = page_title
                name = page_title
                products_changed = True

            print(f"[{name}] Status: {status} | Price: {price} | Seller: {seller_name} ({seller_type})")

            if status == "blocked":
                blocked_count += 1
            elif status in ("ok", "oos"):
                rec = prod_records.get(key, {
                    "name": name,
                    "url": clean_url,
                    "status": status,
                    "last_price": None,
                    "last_seller": None,
                    "last_matching_price": None,
                    "min_price": None,
                    "max_price": None,
                    "records": [],
                })

                prev_status = rec.get("status")
                prev_matching_price = rec.get("last_matching_price")
                records_list = rec.setdefault("records", [])

                seller_matches = (seller_filter == "all") or (seller_type == seller_filter)

                if prev_status == "oos" and status == "ok" and price is not None:
                    if seller_matches:
                        subline = f"🎯 <b>التارجت:</b> {float(target_price):,.2f} ج.م" if target_price is not None else ""
                        msg, markup = build_alert_card(
                            header="🔔 <b>رجع اتوفر من تاني على أمازون!</b>",
                            name=name,
                            asin=asin,
                            clean_url=clean_url,
                            price=price,
                            price_subline=subline,
                            badge_line="",
                            seller_name=seller_name,
                            seller_filter_label=seller_filter_label,
                            resale_note=resale_note,
                            now_dt=now_dt,
                        )
                        send_telegram(msg, markup)

                if status == "ok" and price is not None and seller_matches:
                    should_notify = False
                    header = "📉 <b>نزل سعره على أمازون!</b>"
                    price_subline = ""

                    if target_price is not None:
                        t_val = float(target_price)
                        if price <= t_val and (prev_matching_price is None or price < prev_matching_price):
                            should_notify = True
                            header = "🎯 <b>جبت السعر اللي عايزه!</b>"
                            if prev_matching_price is not None and price < prev_matching_price:
                                diff = prev_matching_price - price
                                price_subline = f"🎯 <b>التارجت:</b> {t_val:,.2f} ج.م (📉 نزل: {diff:,.2f} ج.م)"
                            else:
                                price_subline = f"🎯 <b>التارجت:</b> {t_val:,.2f} ج.م"
                    else:
                        if prev_matching_price is not None and price < prev_matching_price:
                            should_notify = True
                            diff = prev_matching_price - price
                            pct = (diff / prev_matching_price) * 100
                            price_subline = f"📉 <b>نزل:</b> {diff:,.2f} ج.م ({pct:.0f}%) — كان {prev_matching_price:,.2f} ج.م"

                    if should_notify:
                        timeframe_note = get_drop_timeframe_label(
                            records_list, price, rec.get("min_price") or price, now_dt
                        )
                        msg, markup = build_alert_card(
                            header=header,
                            name=name,
                            asin=asin,
                            clean_url=clean_url,
                            price=price,
                            price_subline=price_subline,
                            badge_line=timeframe_note,
                            seller_name=seller_name,
                            seller_filter_label=seller_filter_label,
                            resale_note=resale_note,
                            now_dt=now_dt,
                        )
                        send_telegram(msg, markup)

                    rec["last_matching_price"] = price

                rec["name"] = name
                rec["url"] = clean_url
                rec["status"] = status
                if status == "ok" and price is not None:
                    rec["last_price"] = price
                    rec["last_seller"] = seller_name
                    rec["min_price"] = min(price, rec["min_price"]) if rec.get("min_price") else price
                    rec["max_price"] = max(price, rec["max_price"]) if rec.get("max_price") else price
                elif status == "oos":
                    rec["last_seller"] = "غير متوفر"

                records_list.append({
                    "ts": now_dt.isoformat(timespec="minutes"),
                    "price": price,
                    "seller": seller_name,
                    "status": status,
                })
                if len(records_list) > 4500:
                    rec["records"] = records_list[-4500:]

                prod_records[key] = rec

            if idx < len(products) - 1:
                time.sleep(random.uniform(4, 8))

        if context:
            context.close()
        browser.close()

    if products_changed:
        with open(PRODUCTS_FILE, "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False, indent=2)

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history_data, f, ensure_ascii=False, indent=2)

    if len(products) > 0 and blocked_count == len(products):
        send_telegram("⚠️ <b>تنبيه:</b> أمازون حظر محاولة الفحص الحالية بصفحة Captcha، سيعاود النظام المحاولة في الدورة القادمة.")

    if EVENT_NAME == "workflow_dispatch":
        send_products_summary(products, prod_records)


if __name__ == "__main__":
    main()
