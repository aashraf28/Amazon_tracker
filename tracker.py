from datetime import datetime, timedelta, timezone
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
CAIRO_TZ = timezone(timedelta(hours=2))

SELLER_LABELS = {
    "all": "الكل (أمازون وتجار)",
    "amazon": "أمازون فقط (Amazon.eg)",
    "warehouse": "مستودع أمازون (Warehouse)",
    "third_party": "تجار عاديين فقط",
}

REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "📋 قائمة المنتجات"}, {"text": "🗑️ حذف منتج"}],
        [{"text": "🏪 فلتر البائع"}, {"text": "❓ مساعدة"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


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
        {"command": "list", "description": "📋 عرض قائمة المنتجات والأسعار المسجلة"},
        {"command": "remove", "description": "🗑️ حذف منتج من المتابعة بالأزرار"},
        {"command": "seller", "description": "🏪 اختيار البائع (أمازون / ويرهاوس / الكل)"},
        {"command": "help", "description": "❓ طريقة إضافة منتج واستخدام البوت"},
    ]
    tg_api("setMyCommands", {"commands": commands})


def resolve_amazon_url(raw_url: str) -> tuple[str | None, str]:
    """Resolves short links (amzn.eu/amzn.to) and extracts ASIN."""
    url = raw_url.strip()
    if not re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url):
        try:
            resp = requests.get(
                url,
                allow_redirects=True,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            url = resp.url
        except Exception as e:
            print(f"Redirect resolve failed for {url}: {e}")

    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    if match:
        asin = match.group(1)
        return asin, f"https://www.amazon.eg/-/en/dp/{asin}"
    return None, url


def process_telegram_commands(products: list[dict], history_data: dict) -> bool:
    """Reads user commands & button taps from Telegram. Returns True if products.json changed."""
    if not BOT_TOKEN or not CHAT_ID:
        return False

    last_update_id = history_data.get("last_update_id", 0)
    res = tg_api(
        "getUpdates",
        {
            "offset": last_update_id + 1,
            "timeout": 5,
            "allowed_updates": ["message", "callback_query"],
        },
    )
    if not res or not res.get("ok"):
        return False

    updates = res.get("result", [])
    if not updates:
        return False

    products_changed = False
    prod_records = history_data.get("products", {})

    for upd in updates:
        history_data["last_update_id"] = max(history_data.get("last_update_id", 0), upd["update_id"])

        if "callback_query" in upd:
            cb = upd["callback_query"]
            from_id = str(cb.get("from", {}).get("id", ""))
            if from_id != str(CHAT_ID):
                continue

            data = cb.get("data", "")
            tg_api("answerCallbackQuery", {"callback_query_id": cb["id"]})

            if data.startswith("del:"):
                target_asin = data.split(":", 1)[1]
                new_list = []
                removed_name = None
                for item in products:
                    asin, _ = resolve_amazon_url(item["url"])
                    if asin == target_asin or item["url"] == target_asin:
                        removed_name = item.get("name", target_asin)
                    else:
                        new_list.append(item)
                if removed_name:
                    products[:] = new_list
                    products_changed = True
                    send_telegram(f"🗑️ تم حذف <b>{removed_name}</b> من قائمة المتابعة بنجاح.")

            elif data.startswith("sel_item:"):
                target_asin = data.split(":", 1)[1]
                buttons = [
                    [{"text": label, "callback_data": f"set_sel:{target_asin}:{code}"}]
                    for code, label in SELLER_LABELS.items()
                ]
                send_telegram(
                    "🏪 <b>اختر نوع البائع الذي تريد متابعته لهذا المنتج:</b>",
                    reply_markup={"inline_keyboard": buttons},
                )

            elif data.startswith("set_sel:"):
                _, target_asin, code = data.split(":", 2)
                for item in products:
                    asin, _ = resolve_amazon_url(item["url"])
                    if asin == target_asin or item["url"] == target_asin:
                        item["seller"] = code
                        products_changed = True
                        send_telegram(
                            f"✅ تم ضبط فلتر البائع للمنتج <b>{item.get('name')}</b> إلى:\n"
                            f"<b>{SELLER_LABELS.get(code, code)}</b>"
                        )
                        break
            continue

        msg = upd.get("message")
        if not msg or str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID):
            continue

        text = (msg.get("text") or "").strip()
        if not text:
            continue

        url_match = re.search(r"(https?://(?:www\.)?(?:amazon\.eg|amzn\.eu|amzn\.to)\S+)", text)
        if url_match:
            raw_link = url_match.group(1)
            asin, clean_url = resolve_amazon_url(raw_link)
            if not asin:
                send_telegram("⚠️ لم أتمكن من قراءة كود المنتج من الرابط، تأكد أنه رابط منتج من أمازون مصر.")
                continue

            remaining = text.replace(raw_link, "").replace("/add", "").strip()
            price_match = re.search(r"\b(\d+(?:\.\d+)?)\b", remaining)
            target_price = float(price_match.group(1)) if price_match else None
            custom_name = re.sub(r"\b\d+(?:\.\d+)?\b", "", remaining).strip()
            name = custom_name if len(custom_name) >= 2 else f"منتج أمازون ({asin})"

            updated = False
            for item in products:
                existing_asin, _ = resolve_amazon_url(item["url"])
                if existing_asin == asin:
                    item["url"] = clean_url
                    if target_price is not None:
                        item["target_price"] = target_price
                    if len(custom_name) >= 2:
                        item["name"] = name
                    updated = True
                    break

            if not updated:
                products.append({
                    "name": name,
                    "url": clean_url,
                    "target_price": target_price,
                    "seller": "all",
                })

            products_changed = True
            target_txt = f"{target_price:,.0f} ج.م" if target_price else "أي انخفاض في السعر"
            seller_buttons = [
                [{"text": label, "callback_data": f"set_sel:{asin}:{code}"}]
                for code, label in SELLER_LABELS.items()
            ]
            send_telegram(
                f"✅ <b>تمت إضافة المنتج للمتابعة!</b>\n"
                f"📦 <b>الاسم:</b> {name}\n"
                f"🎯 <b>التنبيه عند:</b> {target_txt}\n\n"
                f"👇 <i>يمكنك اختيار فلتر البائع من الأزرار بالأسفل (الافتراضي: الكل):</i>",
                reply_markup={"inline_keyboard": seller_buttons},
            )

        elif text in ("/list", "📋 قائمة المنتجات"):
            if not products:
                send_telegram("📭 قائمتك فارغة حالياً. أرسل رابط أي منتج من أمازون مصر لإضافته فوراً.")
            else:
                lines = ["📋 <b>قائمة المنتجات المتابعة حالياً:</b>\n"]
                for i, item in enumerate(products, 1):
                    asin, _ = resolve_amazon_url(item["url"])
                    rec = prod_records.get(asin, {})
                    curr = f"{rec['last_price']:,.0f} ج.م" if rec.get("last_price") else "قيد الفحص"
                    low = f"{rec['min_price']:,.0f} ج.م" if rec.get("min_price") else "-"
                    seller_mode = SELLER_LABELS.get(item.get("seller", "all"), "الكل")
                    lines.append(
                        f"{i}. <b>{item.get('name')}</b>\n"
                        f"   💰 الحالي: {curr} | 📉 الأقل: {low}\n"
                        f"   🏪 البائع: {seller_mode}"
                    )
                send_telegram("\n".join(lines))

        elif text in ("/remove", "🗑️ حذف منتج"):
            if not products:
                send_telegram("📭 لا توجد منتجات لحذفها.")
            else:
                buttons = []
                for item in products:
                    asin, _ = resolve_amazon_url(item["url"])
                    key = asin or item["url"]
                    buttons.append([{"text": f"❌ {item.get('name', key)}", "callback_data": f"del:{key}"}])
                send_telegram(
                    "🗑️ <b>اضغط على اسم المنتج الذي تريد حذفه:</b>",
                    reply_markup={"inline_keyboard": buttons},
                )

        elif text in ("/seller", "🏪 فلتر البائع"):
            if not products:
                send_telegram("📭 أضف منتجاً أولاً لتحديد فلتر البائع.")
            else:
                buttons = []
                for item in products:
                    asin, _ = resolve_amazon_url(item["url"])
                    key = asin or item["url"]
                    curr_sel = item.get("seller", "all")
                    buttons.append([{"text": f"🏪 {item.get('name', key)} ({curr_sel})", "callback_data": f"sel_item:{key}"}])
                send_telegram(
                    "🏪 <b>اختر المنتج لتعديل فلتر البائع الخاص به:</b>",
                    reply_markup={"inline_keyboard": buttons},
                )

        elif text in ("/help", "/start", "❓ مساعدة"):
            help_msg = (
                "🤖 <b>دليل استخدام متابع أسعار أمازون مصر:</b>\n\n"
                "1️⃣ <b>لإضافة منتج جديد:</b>\n"
                "فقط انسخ رابط المنتج من أمازون (أو اضغط Share من تطبيق أمازون للبوت مباشرة)، وسيتم جلب اسمه ومتابعته تلقائياً.\n"
                "💡 <i>لو عايز سعر مستهدف اكتب الرقم جنب الرابط، مثال:</i>\n"
                "<code>https://www.amazon.eg/dp/B0B1LVC5VZ 2500</code>\n\n"
                "2️⃣ <b>باقي التحكم:</b>\n"
                "استخدم الأزرار الظاهرة بالأسفل لعرض قائمتك أو حذف منتج أو تحديد البائع (أمازون فقط / مستودع أمازون / الكل)."
            )
            send_telegram(help_msg)

    return products_changed


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
    if "warehouse" in low or "مستودع" in low:
        return seller_name, "warehouse"
    if "amazon" in low or "أمازون" in low:
        return seller_name, "amazon"
    return seller_name, "third_party"


def extract_product_title(page) -> str | None:
    el = page.query_selector("#productTitle")
    if el:
        title = re.sub(r"\s+", " ", el.inner_text()).strip()
        return title[:65] + ("..." if len(title) > 65 else "")
    return None


def extract_page_data(page) -> tuple[float | None, str, str, str, str | None]:
    """Returns (price, status, seller_name, seller_type, page_title)."""
    content = page.content().lower()
    if "validatecaptcha" in content or "type the characters you see in this image" in content:
        return None, "blocked", "غير محدد", "unknown", None

    title = extract_product_title(page)

    avail_el = page.query_selector("#availability")
    if avail_el:
        avail_text = avail_el.inner_text().lower()
        if "currently unavailable" in avail_text or "غير متوفر" in avail_text:
            return None, "oos", "غير متوفر", "unknown", title

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
                return price, "ok", seller_name, seller_type, title

    whole_el = page.query_selector("#corePriceDisplay_desktop_feature_div .a-price-whole")
    if whole_el:
        frac_el = page.query_selector("#corePriceDisplay_desktop_feature_div .a-price-fraction")
        whole = whole_el.inner_text().strip().replace(".", "")
        frac = frac_el.inner_text().strip() if frac_el else "00"
        price = parse_price(f"{whole}.{frac}")
        if price:
            return price, "ok", seller_name, seller_type, title

    return None, "unknown", seller_name, seller_type, title


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

    # Warm up on Amazon Egypt homepage to collect valid session cookies
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

    history_data = {"last_update_id": 0, "products": {}}
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if "products" in loaded:
                history_data = loaded

    products_changed = process_telegram_commands(products, history_data)
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

            price = None
            status = "unknown"
            seller_name = "غير محدد"
            seller_type = "unknown"
            page_title = None

            for attempt in range(2):
                try:
                    page.goto(
                        clean_url,
                        wait_until="domcontentloaded",
                        referer="https://www.amazon.eg/-/en/",
                        timeout=45000,
                    )
                    page.wait_for_timeout(random.randint(3000, 5000))
                    price, status, seller_name, seller_type, page_title = extract_page_data(page)
                    if status in ("ok", "oos"):
                        break
                    if status == "blocked" and attempt == 0:
                        # Refresh session cookies with a clean context before retrying
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
                        msg = (
                            f"🔔 <b>المنتج رجع اتوفر من تاني على أمازون مصر!</b>\n\n"
                            f"📦 <b>المنتج:</b> {name}\n"
                            f"💰 <b>السعر الحالي:</b> {price:,.2f} ج.م\n"
                            f"🏪 <b>البائع:</b> {seller_name}\n\n"
                            f"🔗 <a href='{clean_url}'>فتح صفحة المنتج</a>"
                        )
                        send_telegram(msg)

                if status == "ok" and price is not None and seller_matches:
                    should_notify = False
                    drop_header = ""

                    if target_price is not None:
                        if price <= float(target_price) and (prev_matching_price is None or price < prev_matching_price):
                            should_notify = True
                            drop_header = f"🎯 وصل للسعر المستهدف ({float(target_price):,.0f} ج.م)"
                    else:
                        if prev_matching_price is not None and price < prev_matching_price:
                            should_notify = True
                            diff = prev_matching_price - price
                            pct = (diff / prev_matching_price) * 100
                            drop_header = f"انخفض بمقدار {diff:,.0f} ج.م ({pct:.0f}%) — كان {prev_matching_price:,.0f} ج.م"

                    if should_notify:
                        all_min = min(price, rec["min_price"]) if rec.get("min_price") else price
                        all_max = max(price, rec["max_price"]) if rec.get("max_price") else price
                        timeframe_note = get_drop_timeframe_label(records_list, price, rec.get("min_price") or price, now_dt)

                        msg = (
                            f"📉 <b>تخفيض جديد على أمازون مصر!</b>\n\n"
                            f"📦 <b>المنتج:</b> {name}\n"
                            f"💰 <b>السعر الحالي:</b> {price:,.2f} ج.م\n"
                            f"📌 <b>التفاصيل:</b> {drop_header}\n"
                            f"{timeframe_note + chr(10) if timeframe_note else ''}"
                            f"📊 <b>سجل الأسعار:</b> الأقل {all_min:,.0f} ج.م | الأعلى {all_max:,.0f} ج.م\n"
                            f"🏪 <b>البائع:</b> {seller_name}\n\n"
                            f"🔗 <a href='{clean_url}'>فتح صفحة المنتج</a>"
                        )
                        send_telegram(msg)

                    rec["last_matching_price"] = price

                rec["name"] = name
                rec["url"] = clean_url
                rec["status"] = status
                if status == "ok" and price is not None:
                    rec["last_price"] = price
                    rec["last_seller"] = seller_name
                    rec["min_price"] = min(price, rec["min_price"]) if rec.get("min_price") else price
                    rec["max_price"] = max(price, rec["max_price"]) if rec.get("max_price") else price

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


if __name__ == "__main__":
    main()
