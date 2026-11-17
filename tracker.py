from datetime import datetime, timedelta, timezone
import html
import json
import os
import random
import re
import time
from zoneinfo import ZoneInfo
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
SESSION_FILE = "session_state.json"

try:
    CAIRO_TZ = ZoneInfo("Africa/Cairo")
except Exception:
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
        [{"text": "🎯 تعديل التارجت"}, {"text": "🏪 فلتر البائع"}],
        [{"text": "🗑️ حذف منتج"}, {"text": "❓ مساعدة"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def format_arabic_datetime(dt: datetime) -> str:
    month_name = ARABIC_MONTHS[dt.month]
    hour_12 = dt.hour % 12 or 12
    period = "ص" if dt.hour < 12 else "م"
    return f"{dt.day} {month_name} {dt.year}، {hour_12:02d}:{dt.minute:02d} {period}"


def format_arabic_date(dt: datetime) -> str:
    return f"{dt.day} {ARABIC_MONTHS[dt.month]} {dt.year}"


def tg_api(method: str, payload: dict):
    if not BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=15)
        data = resp.json()
        if not data.get("ok"):
            print(f"Telegram API warning ({method}): {data}")
        return data
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
        {"command": "target", "description": "🎯 تعديل السعر المستهدف لمنتج"},
        {"command": "seller", "description": "🏪 اختيار البائع (أمازون / ويرهاوس / الكل)"},
        {"command": "remove", "description": "🗑️ حذف منتج من المتابعة بالأزرار"},
        {"command": "help", "description": "❓ طريقة إضافة منتج واستخدام البوت"},
    ]
    tg_api("setMyCommands", {"commands": commands})


def resolve_amazon_url(raw_url: str) -> tuple[str | None, str]:
    """Extracts ASIN and forces clean English amazon.eg URL."""
    url = raw_url.strip()
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url, re.IGNORECASE)
    if match:
        asin = match.group(1).upper()
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
        if title:
            return title[:70] + ("..." if len(title) > 70 else "")
    return None


def extract_other_offers_info(page) -> tuple[str, float | None]:
    """Checks 'New & Used / Other Sellers' starting price. Returns (note, parsed_price)."""
    for sel in ("#olp_feature_div", "#aod-ingress-link", "#buybox-see-all-buying-choices"):
        el = page.query_selector(sel)
        if el:
            txt = re.sub(r"\s+", " ", el.inner_text()).strip()
            price_match = re.search(r"(?:EGP|ج\.م)\s*([\d,]+(?:\.\d+)?)", txt, re.IGNORECASE)
            if price_match:
                p_val = parse_price(price_match.group(1))
                return f"تبدأ من {price_match.group(1)} ج.م", p_val
    return "شيّك على المخزون", None


def extract_page_data(page) -> tuple[float | None, str, str, str, str | None, str]:
    """Returns (price, status, seller_name, seller_type, page_title, resale_note)."""
    content = page.content().lower()
    block_markers = (
        "validatecaptcha",
        "type the characters you see in this image",
        "enter the characters you see below",
        "sorry! something went wrong",
        "dogs of amazon",
        "automated access to amazon data",
    )
    if any(marker in content for marker in block_markers):
        return None, "blocked", "غير محدد", "unknown", None, "شيّك على المخزون"

    title = extract_product_title(page)
    if not title:
        return None, "blocked", "غير محدد", "unknown", None, "شيّك على المخزون"

    resale_note, other_offers_price = extract_other_offers_info(page)

    for oos_selector in ("#availability", "#outOfStock"):
        oos_el = page.query_selector(oos_selector)
        if oos_el:
            oos_text = oos_el.inner_text().lower()
            oos_phrases = (
                "currently unavailable",
                "temporarily out of stock",
                "we don't know when",
                "غير متوفر",
                "نفدت الكمية",
            )
            if any(phrase in oos_text for phrase in oos_phrases):
                return None, "oos", "غير متوفر", "unknown", title, resale_note

    seller_name, seller_type = extract_seller(page)

    selectors = [
        "#corePriceDisplay_desktop_feature_div .a-price.priceToPay .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .apexPriceToPay .a-offscreen",
        ".reinventPricePriceToPayMargin .a-offscreen",
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePrice_desktop .a-price .a-offscreen",
        "#apex_desktop .a-price .a-offscreen",
        "#tp_price_block_total_price_ww .a-offscreen",
        "#price_inside_buybox",
        "#newBuyBoxPrice",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#sns-base-price",
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

    if other_offers_price is not None:
        fallback_seller = seller_name if seller_name != "غير محدد" else "عروض متعددة (Buying Options)"
        return other_offers_price, "ok", fallback_seller, seller_type, title, resale_note

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
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=CAIRO_TZ)
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


def build_new_product_discovery_card(
    name: str,
    asin: str | None,
    clean_url: str,
    price: float,
    target_price: float | None,
    seller_name: str,
    seller_filter_label: str,
    now_dt: datetime,
) -> tuple[str, dict]:
    """Builds the initial price report card with quick discount % and custom target buttons."""
    safe_name = html.escape(name)
    safe_seller = html.escape(seller_name)
    safe_filter = html.escape(seller_filter_label)
    asin_line = f"\n└ 🆔 <code>{asin}</code>" if asin else ""
    target_line = (
        f"{float(target_price):,.2f} ج.م"
        if target_price is not None
        else "غير محدد (أي انخفاض)"
    )

    msg = (
        f"🆕 <b>تم رصد السعر الحالي للمنتج الجديد!</b>\n\n"
        f"📦 <b>{safe_name}</b>{asin_line}\n\n"
        f"💰 <b>السعر الحالي:</b> {price:,.2f} ج.م\n"
        f"🏢 <b>البائع:</b> {safe_seller}\n"
        f"🎛️ <b>فلتر البائع:</b> {safe_filter}\n"
        f"🎯 <b>التارجت الحالي:</b> {target_line}\n\n"
        f"📅 السعر بتاريخ {format_arabic_datetime(now_dt)}\n\n"
        f"👇 <i>اختر نسبة الخصم المطلوبة أو اكتب سعراً مخصصاً:</i>"
    )

    keyboard_rows = []
    if asin:
        def calc_disc(pct: int) -> int | float:
            raw = price * (1 - pct / 100.0)
            return round(raw) if price >= 100 else round(raw, 2)

        p5, p10, p15, p20 = calc_disc(5), calc_disc(10), calc_disc(15), calc_disc(20)
        keyboard_rows.extend([
            [
                {"text": f"📉 خصم 5% ({p5:,} ج.م)", "callback_data": f"set_tgt:{asin}:{p5}"},
                {"text": f"📉 خصم 10% ({p10:,} ج.م)", "callback_data": f"set_tgt:{asin}:{p10}"},
            ],
            [
                {"text": f"📉 خصم 15% ({p15:,} ج.م)", "callback_data": f"set_tgt:{asin}:{p15}"},
                {"text": f"📉 خصم 20% ({p20:,} ج.م)", "callback_data": f"set_tgt:{asin}:{p20}"},
            ],
            [
                {"text": "✍️ اكتب سعر تارجت مخصص", "callback_data": f"tgt_item:{asin}"},
            ],
        ])

    keyboard_rows.append([{"text": "🛒 شوفه على أمازون", "url": clean_url}])
    return msg, {"inline_keyboard": keyboard_rows}


def format_product_blocks(products: list[dict], prod_records: dict) -> list[str]:
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
    return blocks


def send_chunked_blocks(header: str, blocks: list[str]):
    current_msg = header
    for block in blocks:
        if len(current_msg) + len(block) + 2 > 3800:
            send_telegram(current_msg.strip())
            current_msg = block + "\n\n"
        else:
            current_msg += block + "\n\n"

    if current_msg.strip():
        send_telegram(current_msg.strip())


def send_products_summary(products: list[dict], prod_records: dict):
    """Sends the full updated product list automatically after a manual scan."""
    if not products:
        send_telegram("✅ <b>انتهى الفحص اليدوي!</b>\n📭 قائمتك فارغة حالياً. أرسل رابط أي منتج من أمازون مصر لإضافته فوراً.")
        return

    header = "✅ <b>انتهى الفحص اليدوي — قائمة المنتجات المحدّثة:</b>\n\n"
    blocks = format_product_blocks(products, prod_records)
    send_chunked_blocks(header, blocks)


def send_daily_summary(products: list[dict], prod_records: dict, now_dt: datetime):
    """Sends the 9:00 PM Cairo time daily summary with today's changes and full price list."""
    if not products:
        return

    today_date = now_dt.date()
    changes_lines = []

    for item in products:
        asin, _ = resolve_amazon_url(item["url"])
        key = asin or item["url"]
        rec = prod_records.get(key, {})
        records_list = rec.get("records", [])
        safe_name = html.escape(item.get("name", f"منتج ({key})"))
        curr_status = rec.get("status")
        curr_price = float(rec["last_price"]) if rec.get("last_price") is not None else None

        before_today_price = None
        before_today_status = None
        today_records = []

        for r in records_list:
            try:
                r_dt = datetime.fromisoformat(r["ts"])
                if r_dt.tzinfo is None:
                    r_dt = r_dt.replace(tzinfo=CAIRO_TZ)
                else:
                    r_dt = r_dt.astimezone(CAIRO_TZ)

                if r_dt.date() < today_date:
                    if r.get("price") is not None:
                        before_today_price = float(r["price"])
                    before_today_status = r.get("status")
                elif r_dt.date() == today_date:
                    today_records.append(r)
            except Exception:
                continue

        today_prices = [float(r["price"]) for r in today_records if r.get("price") is not None]
        baseline_price = before_today_price if before_today_price is not None else (today_prices[0] if today_prices else None)

        if before_today_status == "oos" and curr_status == "ok" and curr_price is not None:
            changes_lines.append(f"• 🔔 <b>{safe_name}</b>: رجع اتوفر اليوم بسعر <b>{curr_price:,.2f} ج.م</b>")
        elif before_today_status == "ok" and curr_status == "oos":
            changes_lines.append(f"• ❌ <b>{safe_name}</b>: نفدت الكمية اليوم وأصبح غير متوفر")
        elif curr_status == "ok" and curr_price is not None and baseline_price is not None:
            diff = curr_price - baseline_price
            if diff <= -0.01:
                changes_lines.append(
                    f"• 📉 <b>{safe_name}</b>: نزل من {baseline_price:,.2f} إلى <b>{curr_price:,.2f} ج.م</b> (-{abs(diff):,.2f} ج.م)"
                )
            elif diff >= 0.01:
                changes_lines.append(
                    f"• 📈 <b>{safe_name}</b>: زاد من {baseline_price:,.2f} إلى <b>{curr_price:,.2f} ج.م</b> (+{diff:,.2f} ج.م)"
                )
            elif len(today_prices) >= 2 and min(today_prices) != max(today_prices):
                changes_lines.append(
                    f"• ↔️ <b>{safe_name}</b>: تذبذب اليوم بين {min(today_prices):,.2f} و {max(today_prices):,.2f} ج.م"
                )

    header = f"🌙 <b>الملخص اليومي لأسعار أمازون — {format_arabic_date(now_dt)}</b>\n\n"
    if changes_lines:
        header += "📊 <b>التغيرات اللي حصلت النهاردة:</b>\n" + "\n".join(changes_lines) + "\n\n"
    else:
        header += "✨ <b>حركة الأسعار اليوم:</b> الأسعار مستقرة النهاردة بدون أي تغيير.\n\n"

    header += "📋 <b>قائمة الأسعار الحالية:</b>\n\n"
    blocks = format_product_blocks(products, prod_records)
    send_chunked_blocks(header, blocks)


def should_record_snapshot(
    rec: dict,
    new_status: str,
    new_price: float | None,
    new_seller: str,
    now_dt: datetime,
) -> bool:
    """Returns True only if price/status/seller changed or >= 12h elapsed since last snapshot."""
    records = rec.get("records", [])
    if not records:
        return True
    if rec.get("status") != new_status:
        return True
    if new_status == "ok" and new_price is not None and rec.get("last_price") != new_price:
        return True
    if new_status == "ok" and rec.get("last_seller") != new_seller:
        return True

    try:
        last_ts = datetime.fromisoformat(records[-1]["ts"])
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=CAIRO_TZ)
        if (now_dt - last_ts).total_seconds() >= 43200:
            return True
    except Exception:
        return True

    return False


def block_heavy_resources(route):
    if route.request.resource_type in ("image", "media", "font"):
        route.abort()
    else:
        route.continue_()


def create_warm_page(browser, use_cached_session: bool = True):
    storage_arg = SESSION_FILE if (use_cached_session and os.path.exists(SESSION_FILE)) else None
    context = browser.new_context(
        storage_state=storage_arg,
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
    context.route("**/*", block_heavy_resources)
    page = context.new_page()
    if stealth_sync:
        try:
            stealth_sync(page)
        except Exception:
            pass

    if not storage_arg or random.random() < 0.25:
        try:
            page.goto("https://www.amazon.eg/-/en/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(random.randint(2500, 4500))
        except Exception as e:
            print(f"Warmup note: {e}")

    return context, page


def main():
    if EVENT_NAME == "schedule":
        jitter = random.uniform(10, 45)
        print(f"Scheduled run: waiting {jitter:.1f}s random jitter...")
        time.sleep(jitter)

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
    history_changed = False
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
        context, page = create_warm_page(browser, use_cached_session=True) if products else (None, None)

        for idx, item in enumerate(products):
            if idx > 0 and idx % 6 == 0:
                try:
                    context.storage_state(path=SESSION_FILE)
                except Exception:
                    pass
                context.close()
                time.sleep(random.uniform(3, 6))
                context, page = create_warm_page(browser, use_cached_session=True)

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
                    try:
                        page.wait_for_selector("#productTitle, #captchacharacters, form[action*='validateCaptcha']", timeout=8000)
                    except Exception:
                        pass
                    page.wait_for_timeout(random.randint(2000, 3500))
                    price, status, seller_name, seller_type, page_title, resale_note = extract_page_data(page)
                    if status in ("ok", "oos"):
                        break
                    if attempt == 0:
                        if os.path.exists(SESSION_FILE):
                            os.remove(SESSION_FILE)
                        context.close()
                        time.sleep(random.uniform(5, 9))
                        context, page = create_warm_page(browser, use_cached_session=False)
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
                prev_last_price = rec.get("last_price")
                prev_matching_price = rec.get("last_matching_price")
                records_list = rec.setdefault("records", [])

                seller_matches = (seller_filter == "all") or (seller_type == seller_filter)
                needs_initial_card = (
                    (not item.get("initial_notified", True))
                    or (prev_last_price is None and "initial_notified" not in item)
                )

                # 1. First-time discovery card for a newly added item
                if needs_initial_card and status == "ok" and price is not None:
                    hit_target_immediately = (
                        target_price is not None
                        and seller_matches
                        and price <= float(target_price)
                    )
                    if hit_target_immediately:
                        msg, markup = build_alert_card(
                            header="🎯 <b>جبت السعر اللي عايزه!</b>",
                            name=name,
                            asin=asin,
                            clean_url=clean_url,
                            price=price,
                            price_subline=f"🎯 <b>التارجت:</b> {float(target_price):,.2f} ج.م",
                            badge_line="",
                            seller_name=seller_name,
                            seller_filter_label=seller_filter_label,
                            resale_note=resale_note,
                            now_dt=now_dt,
                        )
                        send_telegram(msg, markup)
                    else:
                        msg, markup = build_new_product_discovery_card(
                            name=name,
                            asin=asin,
                            clean_url=clean_url,
                            price=price,
                            target_price=target_price,
                            seller_name=seller_name,
                            seller_filter_label=seller_filter_label,
                            now_dt=now_dt,
                        )
                        send_telegram(msg, markup)

                    item["initial_notified"] = True
                    products_changed = True
                    if seller_matches:
                        rec["last_matching_price"] = price
                        history_changed = True

                elif needs_initial_card and status == "oos":
                    safe_name = html.escape(name)
                    asin_line = f"\n└ 🆔 <code>{asin}</code>" if asin else ""
                    send_telegram(
                        f"🆕 <b>تم فحص المنتج الجديد لأول مرة!</b>\n\n"
                        f"📦 <b>{safe_name}</b>{asin_line}\n\n"
                        f"❌ <b>الحالة الحالية:</b> غير متوفر حالياً على أمازون\n"
                        f"🔔 سأنبهك فوراً بمجرد توفره!",
                        {"inline_keyboard": [[{"text": "🛒 شوفه على أمازون", "url": clean_url}]]},
                    )
                    item["initial_notified"] = True
                    products_changed = True

                # 2. Existing tracked item: Back in stock & price drop alerts
                else:
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
                            if prev_matching_price is not None and price < prev_matching_
