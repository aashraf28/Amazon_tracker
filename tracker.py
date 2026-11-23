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
SCAN_MODE = os.environ.get("SCAN_MODE", EVENT_NAME)
TARGET_ASIN = os.environ.get("TARGET_ASIN", "").strip().upper()
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

BANK_WALLET_EXCLUSIONS = (
    # Cards & Payment Networks
    "visa", "mastercard", "master card", "meeza", "credit card", "debit card",
    "prepaid", "cards", "card", "بطاقة", "بطاقات", "كارت", "كروت", "فيزا",
    "ماستركارد", "ميزة", "ائتمان", "خصم مباشر",
    # Banks
    "nbe", "national bank", "cib", "qnb", "banque misr", "hsbc", "alexbank",
    "bank of alexandria", "fab", "first abu dhabi", "adcb", "adib",
    "emirates nbd", "enbd", "mashreq", "arab bank", "credit agricole",
    "saib", "suez canal", "united bank", "al ahly", "bank",
    "الأهلي", "بنك", "التجاري الدولي", "المشرق", "أبوظبي", "كريدي أجريكول", "قطر الوطني",
    # Wallets & BNPL
    "vodafone", "vf cash", "etisalat", "e&", "orange cash", "we pay",
    "instapay", "fawry", "valu", "souhoola", "sympl", "shahry", "contact",
    "aman", "forsah", "halan", "swype", "installment", "monthly",
    "فودافون", "اتصالات", "أورنج", "وي باي", "إنستاباي", "فوري", "فاليو",
    "سهولة", "أمان", "حالا", "تقسيط",
)

PROMO_POSITIVE_KEYWORDS = (
    "promo code", "enter code", "coupon", "redeem", "save ", "off",
    "buy ", "get 1", "free", "item(s)", "items", "bogo",
    "كود", "كوبون", "برومو", "خصم", "وفر", "وفّر", "اشتري", "مجانا", "مجاناً", "هدية",
)

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


def classify_seller_type(seller_name: str) -> str:
    if not seller_name or seller_name == "غير محدد":
        return "unknown"
    low = seller_name.lower()
    if "warehouse" in low or "مستودع" in low or "resale" in low:
        return "warehouse"
    if "amazon" in low or "أمازون" in low:
        return "amazon"
    return "third_party"


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

    return seller_name, classify_seller_type(seller_name)


def extract_product_title(page) -> str | None:
    el = page.query_selector("#productTitle")
    if el:
        title = re.sub(r"\s+", " ", el.inner_text()).strip()
        if title:
            return title[:70] + ("..." if len(title) > 70 else "")
    return None


def extract_list_price(page, current_price: float | None) -> float | None:
    """Extracts struck-through List Price / RRP if shown on the product page."""
    selectors = [
        "#corePriceDisplay_desktop_feature_div .basisPrice .a-price.a-text-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price.a-text-price[data-a-strike='true'] .a-offscreen",
        "#apex_offerDisplay_desktop .basisPrice .a-price.a-text-price .a-offscreen",
        ".basisPrice .a-price.a-text-price .a-offscreen",
        "#corePrice_feature_div .a-price.a-text-price .a-offscreen",
        "#listPrice",
        "#priceblock_listprice",
    ]
    for sel in selectors:
        el = page.query_selector(sel)
        if el:
            val = parse_price(el.inner_text())
            if val and (current_price is None or val > current_price):
                return val
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


def extract_aod_offer(page) -> tuple[float | None, str, str]:
    """Clicks 'See All Buying Options' if present and extracts the lowest offer price & seller."""
    btn = page.query_selector("#buybox-see-all-buying-choices a, #buybox-see-all-buying-choices, #aod-ingress-link")
    if not btn:
        return None, "غير محدد", "unknown"

    try:
        btn.click(timeout=3000)
        page.wait_for_selector("#aod-offer .a-price .a-offscreen, #aod-pinned-offer .a-price .a-offscreen", timeout=4500)
        page.wait_for_timeout(800)
    except Exception:
        return None, "غير محدد", "unknown"

    best_price = None
    best_seller = "غير محدد"
    best_type = "unknown"

    offer_blocks = page.query_selector_all("#aod-pinned-offer, #aod-offer")
    for block in offer_blocks:
        p_el = block.query_selector(".a-price .a-offscreen")
        if not p_el:
            continue
        p_val = parse_price(p_el.inner_text())
        if not p_val:
            continue

        s_name = "غير محدد"
        s_el = block.query_selector("#aod-offer-soldBy a, #aod-offer-soldBy .a-size-small, #aod-offer-soldBy")
        if s_el:
            raw_s = re.sub(r"\s+", " ", s_el.inner_text()).strip()
            raw_s = re.sub(r"^Sold by\s*:?\s*", "", raw_s, flags=re.IGNORECASE).strip()
            if raw_s:
                s_name = raw_s

        if best_price is None or p_val < best_price:
            best_price = p_val
            best_seller = s_name
            best_type = classify_seller_type(s_name)

    return best_price, best_seller, best_type


def extract_sns_price(page) -> float | None:
    """Extracts Subscribe & Save price if available on the product page."""
    sns_containers = [
        "#snsAccordionRowMiddle",
        "#newAccordionRow_1",
        "#snsPrice",
        "#snsDetailPagePrice",
        "#rcx-subscribe-submit-button-announce",
    ]
    for sel in sns_containers:
        container = page.query_selector(sel)
        if not container:
            continue
        txt = container.inner_text().strip()
        low = txt.lower()
        if "subscribe" in low or "اشترك" in low or sel in ("#snsAccordionRowMiddle", "#snsPrice", "#snsDetailPagePrice"):
            offscreen = container.query_selector(".a-price .a-offscreen")
            if offscreen:
                val = parse_price(offscreen.inner_text())
                if val:
                    return val
            whole_el = container.query_selector(".a-price-whole")
            if whole_el:
                frac_el = container.query_selector(".a-price-fraction")
                whole = whole_el.inner_text().strip().replace(".", "")
                frac = frac_el.inner_text().strip() if frac_el else "00"
                val = parse_price(f"{whole}.{frac}")
                if val:
                    return val
            m = re.search(r"(?:EGP|ج\.م)\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:EGP|ج\.م)", txt, re.IGNORECASE)
            if m:
                val = parse_price(m.group(1) or m.group(2))
                if val:
                    return val

    el = page.query_selector("#sns-base-price")
    if el:
        val = parse_price(el.inner_text())
        if val:
            return val

    return None


def calculate_promo_impact(promo_text: str, base_price: float | None) -> str:
    """Calculates effective price after applying general promo code / multi-buy / BOGO."""
    if not base_price or base_price <= 0:
        return ""

    bogo_match = re.search(
        r"(?:buy|اشتري)\s*(\d+)\s*(?:item|items|unit|units|قطعة|قطع)?\s*(?:and\s*)?(?:get|احصل\s*على)\s*(\d+)\s*(?:free|مجان)",
        promo_text,
        re.IGNORECASE,
    )
    if bogo_match:
        buy_qty = int(bogo_match.group(1))
        free_qty = int(bogo_match.group(2))
        if buy_qty > 0 and free_qty > 0:
            total_items = buy_qty + free_qty
            total_paid = base_price * buy_qty
            per_item = total_paid / total_items
            return f"القطعة الفعلي: <b>{per_item:,.2f} ج.م</b> (إجمالي {total_items} قطع: {total_paid:,.2f} ج.م)"

    qty_match = re.search(
        r"(?:on|for|buy|اشتري)\s*(\d+)\s*(?:item|items|unit|units|pc|pcs|قطعة|قطع)?",
        promo_text,
        re.IGNORECASE,
    )
    qty = int(qty_match.group(1)) if qty_match and int(qty_match.group(1)) > 1 else 1

    cap_match = re.search(
        r"(?:up to|بحد أقصى)\s*(?:EGP|ج\.م)?\s*([\d,]+(?:\.\d+)?)",
        promo_text,
        re.IGNORECASE,
    )
    cap = parse_price(cap_match.group(1)) if cap_match else None

    pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", promo_text)
    if pct_match:
        pct = float(pct_match.group(1))
        if 0 < pct <= 95:
            if qty > 1:
                bundle_orig = base_price * qty
                disc_total = bundle_orig * (pct / 100.0)
                if cap:
                    disc_total = min(disc_total, cap)
                bundle_final = bundle_orig - disc_total
                per_item_final = bundle_final / qty
                return f"القطعة بعد الخصم: <b>{per_item_final:,.2f} ج.م</b> (إجمالي {qty} قطع: {bundle_final:,.2f} ج.م بدل {bundle_orig:,.2f} ج.م)"
            else:
                disc = base_price * (pct / 100.0)
                if cap:
                    disc = min(disc, cap)
                final_p = base_price - disc
                return f"السعر بعد الكود: <b>{final_p:,.2f} ج.م</b> (توفير {disc:,.2f} ج.م)"

    fixed_match = re.search(
        r"(?:save|off|خصم|وفر|وفّر)\s*(?:EGP|ج\.م)?\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:EGP|ج\.م)\s*(?:off|discount|خصم)",
        promo_text,
        re.IGNORECASE,
    )
    if fixed_match:
        fixed_disc = parse_price(fixed_match.group(1) or fixed_match.group(2))
        if fixed_disc and fixed_disc < (base_price * qty):
            if qty > 1:
                bundle_orig = base_price * qty
                bundle_final = bundle_orig - fixed_disc
                per_item_final = bundle_final / qty
                return f"القطعة بعد الخصم: <b>{per_item_final:,.2f} ج.م</b> (إجمالي {qty} قطع: {bundle_final:,.2f} ج.م)"
            else:
                final_p = base_price - fixed_disc
                return f"السعر بعد الكود: <b>{final_p:,.2f} ج.م</b> (توفير {fixed_disc:,.2f} ج.م)"

    return ""


def extract_valid_promos(page, base_price: float | None) -> list[dict]:
    """Extracts non-bank promo codes, coupons, quantity discounts, and BOGO offers."""
    promo_containers = [
        "#promoPriceBlockMessage_feature_div",
        "#applicablePromotionList_feature_div",
        "#couponFeature_feature_div",
        "#vpcButton",
        "#bundleV2_feature_div",
    ]
    raw_candidates = []

    for sel in promo_containers:
        container = page.query_selector(sel)
        if not container:
            continue
        sub_items = container.query_selector_all("li, tr, .apl_list_item, .promoPriceBlockMessage, div.a-row, label")
        if sub_items:
            for item in sub_items:
                t = re.sub(r"\s+", " ", item.inner_text()).strip()
                if t:
                    raw_candidates.append(t)
        else:
            full_t = container.inner_text().strip()
            for part in re.split(r"(?:Terms\.?|Details\.?|\n)", full_t):
                part = re.sub(r"\s+", " ", part).strip()
                if part:
                    raw_candidates.append(part)

    valid_promos = []
    seen = set()

    for raw in raw_candidates:
        if len(raw) < 8 or len(raw) > 220:
            continue

        cleaned = re.sub(r"^(?:Redeemed|Redeem|Apply|Applied)\s+", "", raw, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"\s*(?:Shop items\s*>?|Terms\.?|Details\.?)$", "", cleaned, flags=re.IGNORECASE).strip()

        if len(cleaned) < 8:
            continue

        low = cleaned.lower()

        if any(bad in low for bad in BANK_WALLET_EXCLUSIONS):
            continue

        if not any(good in low for good in PROMO_POSITIVE_KEYWORDS):
            continue

        norm_key = re.sub(r"[^a-z0-9\u0600-\u06FF]", "", low)
        if not norm_key or any(norm_key in s or s in norm_key for s in seen):
            continue
        seen.add(norm_key)

        code_match = re.search(r"(?:promo\s*code|enter\s*code|كود)\s*:?\s*([A-Z0-9]{4,20})", cleaned, re.IGNORECASE)
        promo_code = code_match.group(1).upper() if code_match else None
        impact_line = calculate_promo_impact(cleaned, base_price)

        valid_promos.append({
            "clean_text": cleaned,
            "code": promo_code,
            "impact": impact_line,
        })

    return valid_promos[:3]


def format_sns_and_promos_lines(
    base_price: float | None,
    sns_price: float | None,
    promos: list[dict],
) -> str:
    """Formats Subscribe & Save and Promo Code sections for Telegram cards."""
    sections = []

    if sns_price is not None and (base_price is None or sns_price < base_price):
        if base_price and base_price > sns_price:
            extra_save = base_price - sns_price
            sections.append(
                f"🔁 <b>اشتراك وتوفير (Subscribe & Save):</b> {sns_price:,.2f} ج.م (توفير {extra_save:,.2f} ج.م إضافية)"
            )
        else:
            sections.append(f"🔁 <b>اشتراك وتوفير (Subscribe & Save):</b> {sns_price:,.2f} ج.م")

    if promos:
        promo_lines = ["🎟️ <b>برومو كود / عرض متاح:</b>"]
        for p in promos:
            safe_txt = html.escape(p["clean_text"])
            if p.get("code"):
                safe_code = html.escape(p["code"])
                safe_txt = re.sub(
                    re.escape(safe_code),
                    f"<code>{safe_code}</code>",
                    safe_txt,
                    flags=re.IGNORECASE,
                )
            promo_lines.append(f"└ {safe_txt}")
            if p.get("impact"):
                promo_lines.append(f"   💵 {p['impact']}")
        sections.append("\n".join(promo_lines))

    return "\n\n".join(sections)


def build_price_reference_block(
    current_price: float,
    min_price: float,
    max_price: float,
    avg_price: float | None,
    list_price: float | None,
    amazon_min_price: float | None,
    prev_price_before_oos: float | None = None,
    is_back_in_stock: bool = False,
) -> str:
    """Builds the historical price range, min/max, List Price, and quick verdict block."""
    verdict = ""
    if max_price > min_price and avg_price is not None:
        if current_price <= min_price * 1.03:
            verdict = "🟢 ممتاز (عند أو قريب من أقل سعر مسجل)"
        elif current_price <= avg_price * 0.93:
            verdict = "🟢 جيد جداً (أقل من المتوسط التاريخي)"
        elif current_price >= avg_price * 1.08 or current_price >= max_price * 0.97:
            verdict = "🔴 مرتفع (قريب من أعلى سعر مسجل)"
        else:
            verdict = "🟡 متوسط (في النطاق السعري المعتاد)"
    elif list_price is not None and list_price > current_price:
        disc_pct = ((list_price - current_price) / list_price) * 100
        if disc_pct >= 15:
            verdict = f"🟢 جيد (خصم {disc_pct:.0f}% عن السعر الأصلي)"
        else:
            verdict = f"🟡 عادي (خصم {disc_pct:.0f}% عن السعر الأصلي)"

    lines = []
    if verdict:
        lines.append(f"🏷️ <b>تقييم السعر:</b> {verdict}")

    if is_back_in_stock and prev_price_before_oos is not None:
        diff = current_price - prev_price_before_oos
        if diff <= -0.01:
            cmp_txt = f"رجع أرخص بـ {abs(diff):,.2f} ج.م"
        elif diff >= 0.01:
            cmp_txt = f"رجع أغلى بـ {diff:,.2f} ج.م"
        else:
            cmp_txt = "رجع بنفس السعر السابق"
        lines.append(f"⏮️ <b>قبل النفاد:</b> {prev_price_before_oos:,.2f} ج.م ({cmp_txt})")

    lines.append(f"📉 <b>أقل سعر:</b> {min_price:,.2f} ج.م")
    lines.append(f"📈 <b>أعلى سعر:</b> {max_price:,.2f} ج.م")

    if avg_price is not None and max_price > min_price:
        lines.append(f"📊 <b>المتوسط:</b> {avg_price:,.2f} ج.م")

    if list_price is not None and list_price > 0:
        lines.append(f"🔖 <b>السعر الأصلي (List Price):</b> {list_price:,.2f} ج.م")

    if amazon_min_price is not None:
        lines.append(f"🏪 <b>أقل سعر لـ Amazon.eg:</b> {amazon_min_price:,.2f} ج.م")

    formatted = []
    for i, line in enumerate(lines):
        prefix = "└" if i == len(lines) - 1 else "├"
        formatted.append(f"{prefix} {line}")

    return "📊 <b>مرجع وتقييم السعر:</b>\n" + "\n".join(formatted)


def extract_page_data(page) -> tuple[float | None, str, str, str, str | None, str, float | None, list[dict], float | None]:
    """Returns (price, status, seller_name, seller_type, page_title, resale_note, sns_price, promos, list_price)."""
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
        return None, "blocked", "غير محدد", "unknown", None, "شيّك على المخزون", None, [], None

    title = extract_product_title(page)
    if not title:
        return None, "blocked", "غير محدد", "unknown", None, "شيّك على المخزون", None, [], None

    resale_note, other_offers_price = extract_other_offers_info(page)

    oos_selectors = (
        "#availability",
        "#outOfStock",
        "#outOfStockBuyBox_feature_div",
        "#fod-cx-message",
        "#availabilityInsideBuyBox_feature_div",
    )
    oos_phrases = (
        "currently unavailable",
        "temporarily out of stock",
        "we don't know when",
        "no featured offers available",
        "غير متوفر",
        "نفدت الكمية",
        "لا تتوفر عروض مميزة",
    )
    for oos_selector in oos_selectors:
        oos_el = page.query_selector(oos_selector)
        if oos_el:
            oos_text = oos_el.inner_text().lower()
            if any(phrase in oos_text for phrase in oos_phrases):
                return None, "oos", "غير متوفر", "unknown", title, resale_note, None, [], None

    seller_name, seller_type = extract_seller(page)
    sns_price = extract_sns_price(page)

    selectors = [
        "#corePriceDisplay_desktop_feature_div .a-price.priceToPay .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .apexPriceToPay .a-offscreen",
        "#apex_offerDisplay_desktop .a-price.priceToPay .a-offscreen",
        "#apex_offerDisplay_desktop .a-price .a-offscreen",
        ".reinventPricePriceToPayMargin .a-offscreen",
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePrice_desktop .a-price .a-offscreen",
        "#apex_desktop .a-price .a-offscreen",
        "#exports_desktop_qualifiedBuybox .a-price .a-offscreen",
        "#qualifiedBuybox .a-price .a-offscreen",
        "#centerCol .a-price.priceToPay .a-offscreen",
        "#centerCol .apexPriceToPay .a-offscreen",
        "#tp_price_block_total_price_ww .a-offscreen",
        "#price_inside_buybox",
        "#newBuyBoxPrice",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
    ]

    found_price = None
    for selector in selectors:
        el = page.query_selector(selector)
        if el:
            p_val = parse_price(el.inner_text())
            if p_val:
                found_price = p_val
                break

    if found_price is None:
        for box_sel in (
            "#corePriceDisplay_desktop_feature_div .a-price.priceToPay",
            "#apex_desktop .a-price.priceToPay",
            "#centerCol .a-price.priceToPay",
        ):
            price_box = page.query_selector(box_sel)
            if price_box:
                whole_el = price_box.query_selector(".a-price-whole")
                if whole_el:
                    frac_el = price_box.query_selector(".a-price-fraction")
                    whole = whole_el.inner_text().strip().replace(".", "")
                    frac = frac_el.inner_text().strip() if frac_el else "00"
                    found_price = parse_price(f"{whole}.{frac}")
                    if found_price:
                        break

    if found_price is None and other_offers_price is not None:
        found_price = other_offers_price
        if seller_name == "غير محدد":
            seller_name = "عروض متعددة (Buying Options)"

    if found_price is None:
        aod_price, aod_seller, aod_type = extract_aod_offer(page)
        if aod_price is not None:
            found_price = aod_price
            seller_name = aod_seller
            seller_type = aod_type
            resale_note = f"تبدأ من {aod_price:,.2f} ج.م"

    if found_price is not None:
        if sns_price is not None and sns_price >= found_price:
            sns_price = None
        promos = extract_valid_promos(page, found_price)
        list_price = extract_list_price(page, found_price)
        return found_price, "ok", seller_name, seller_type, title, resale_note, sns_price, promos, list_price

    if "available from these sellers" in content or "no featured offers" in content or page.query_selector("#buybox-see-all-buying-choices"):
        return None, "oos", "غير متوفر", "unknown", title, resale_note, None, [], None

    avail_debug = page.query_selector("#availability")
    if avail_debug:
        clean_avail = re.sub(r"\s+", " ", avail_debug.inner_text()).strip()[:120]
        print(f"Debug availability text: {clean_avail}")

    return None, "unknown", seller_name, seller_type, title, resale_note, sns_price, [], None


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
    sns_price: float | None = None,
    promos: list[dict] | None = None,
    ref_block: str = "",
) -> tuple[str, dict]:
    safe_name = html.escape(name)
    safe_seller = html.escape(seller_name)
    safe_filter = html.escape(seller_filter_label)
    offers_url = f"https://www.amazon.eg/-/en/dp/{asin}?aod=1" if asin else clean_url
    asin_line = f"\n└ 🆔 <code>{asin}</code>" if asin else ""
    sub_block = f"\n{price_subline}" if price_subline else ""
    badge_block = f"\n{badge_line}" if badge_line else ""

    extras_text = format_sns_and_promos_lines(price, sns_price, promos or [])
    extras_block = f"\n\n{extras_text}" if extras_text else ""
    ref_section = f"\n\n{ref_block}" if ref_block else ""

    msg = (
        f"{header}\n\n"
        f"📦 <b>{safe_name}</b>{asin_line}\n\n"
        f"💰 <b>السعر الحالي:</b> {price:,.2f} ج.م{sub_block}{badge_block}{ref_section}{extras_block}\n\n"
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
    min_price: float,
    max_price: float,
    list_price: float | None,
    target_price: float | None,
    seller_name: str,
    seller_filter_label: str,
    resale_note: str,
    now_dt: datetime,
    sns_price: float | None = None,
    promos: list[dict] | None = None,
) -> tuple[str, dict]:
    """Builds the initial price report card with min/max, List Price, S&S, promos, and quick discount buttons."""
    safe_name = html.escape(name)
    safe_seller = html.escape(seller_name)
    safe_filter = html.escape(seller_filter_label)
    offers_url = f"https://www.amazon.eg/-/en/dp/{asin}?aod=1" if asin else clean_url
    asin_line = f"\n└ 🆔 <code>{asin}</code>" if asin else ""
    target_line = (
        f"{float(target_price):,.2f} ج.م"
        if target_price is not None
        else "غير محدد (أي انخفاض)"
    )

    range_line = f"\n📉 <b>أقل سعر:</b> {min_price:,.2f} ج.م | 📈 <b>أعلى سعر:</b> {max_price:,.2f} ج.م"
    list_line = f"\n🔖 <b>السعر الأصلي (List Price):</b> {list_price:,.2f} ج.م" if (list_price and list_price > price) else ""

    extras_text = format_sns_and_promos_lines(price, sns_price, promos or [])
    extras_block = f"\n\n{extras_text}" if extras_text else ""

    msg = (
        f"🆕 <b>تم رصد السعر الحالي للمنتج الجديد!</b>\n\n"
        f"📦 <b>{safe_name}</b>{asin_line}\n\n"
        f"💰 <b>السعر الحالي:</b> {price:,.2f} ج.م{range_line}{list_line}{extras_block}\n\n"
        f"🏢 <b>البائع:</b> {safe_seller}\n"
        f"🎛️ <b>فلتر البائع:</b> {safe_filter}\n"
        f"🎯 <b>التارجت الحالي:</b> {target_line}\n\n"
        f"💡 <b>خيارات تانية:</b>\n"
        f"└ 📦 <a href='{offers_url}'>أمازون ريسيل ({resale_note})</a>\n\n"
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
        last_sns = rec.get("last_sns_price")
        last_promo = rec.get("last_promo_sig")
        list_p = rec.get("list_price")
        amz_min = rec.get("amazon_min_price")
        safe_name = html.escape(item.get("name", f"منتج ({key})"))

        if status == "oos":
            curr = f"❌ غير متوفر حالياً (آخر سعر: {float(last_price):,.2f} ج.م)" if last_price else "❌ غير متوفر حالياً"
        elif last_price is not None:
            curr = f"{float(last_price):,.2f} ج.م"
        else:
            curr = "⏳ تعذّر جلب السعر في هذه الدورة"

        low = f"{float(rec['min_price']):,.2f} ج.م" if rec.get("min_price") is not None else "-"
        high = f"{float(rec['max_price']):,.2f} ج.م" if rec.get("max_price") is not None else "-"
        actual_seller = html.escape(rec.get("last_seller") or ("غير متوفر" if status == "oos" else "غير محدد"))
        seller_filter_label = html.escape(SELLER_LABELS.get(item.get("seller", "all"), "الكل (أمازون وتجار)"))
        target_note = f" | 🎯 التارجت: {float(item['target_price']):,.2f} ج.م" if item.get("target_price") is not None else ""
        list_note = f"\n   🔖 السعر الأصلي: {float(list_p):,.2f} ج.م" if list_p else ""
        amz_note = f"\n   🏪 أقل سعر لـ Amazon.eg: {float(amz_min):,.2f} ج.م" if amz_min else ""
        sns_note = f"\n   🔁 Subscribe & Save: {float(last_sns):,.2f} ج.م" if last_sns else ""
        promo_note = f"\n   🎟️ عرض نشط: {html.escape(last_promo)}" if last_promo else ""

        blocks.append(
            f"{idx + 1}. <a href='{clean_url}'><b>{safe_name}</b></a>\n"
            f"   💰 السعر الحالي: {curr}{target_note}\n"
            f"   📉 أقل سعر: {low} | 📈 أعلى سعر: {high}{list_note}{amz_note}{sns_note}{promo_note}\n"
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

    items_to_scan = products
    if TARGET_ASIN and SCAN_MODE != "manual":
        items_to_scan = [
            item for item in products
            if (resolve_amazon_url(item["url"])[0] or "").upper() == TARGET_ASIN
        ]
        print(f"Fast single-item scan for ASIN: {TARGET_ASIN} (matched {len(items_to_scan)} item)")

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
        context, page = create_warm_page(browser, use_cached_session=True) if items_to_scan else (None, None)

        for idx, item in enumerate(items_to_scan):
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
            sns_price = None
            promos = []
            list_price = None

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
                    price, status, seller_name, seller_type, page_title, resale_note, sns_price, promos, list_price = extract_page_data(page)
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

            print(
                f"[{name}] Status: {status} | Price: {price} | List: {list_price} | S&S: {sns_price} | "
                f"Promos: {len(promos)} | Seller: {seller_name} ({seller_type})"
            )

            if status == "blocked":
                blocked_count += 1
            elif status in ("ok", "oos"):
                rec = prod_records.get(key, {
                    "name": name,
                    "url": clean_url,
                    "status": status,
                    "last_price": None,
                    "last_sns_price": None,
                    "last_promo_sig": None,
                    "last_seller": None,
                    "last_matching_price": None,
                    "min_price": None,
                    "max_price": None,
                    "amazon_min_price": None,
                    "list_price": None,
                    "records": [],
                })

                prev_status = rec.get("status")
                prev_last_price = rec.get("last_price")
                prev_sns_price = rec.get("last_sns_price")
                prev_promo_sig = rec.get("last_promo_sig")
                prev_matching_price = rec.get("last_matching_price")
                records_list = rec.setdefault("records", [])

                curr_promo_sig = " | ".join(p["clean_text"] for p in promos) if promos else None
                seller_matches = (seller_filter == "all") or (seller_type == seller_filter)
                needs_initial_card = (
                    (not item.get("initial_notified", True))
                    or (prev_last_price is None and "initial_notified" not in item)
                )

                effective_min = min(price, rec["min_price"]) if (price is not None and rec.get("min_price") is not None) else (price if price is not None else rec.get("min_price"))
                effective_max = max(price, rec["max_price"]) if (price is not None and rec.get("max_price") is not None) else (price if price is not None else rec.get("max_price"))
                effective_list_price = list_price if list_price is not None else rec.get("list_price")

                prev_amz_min = rec.get("amazon_min_price")
                if status == "ok" and price is not None and seller_type == "amazon":
                    effective_amz_min = min(price, prev_amz_min) if prev_amz_min is not None else price
                else:
                    effective_amz_min = prev_amz_min

                hist_prices = [float(r["price"]) for r in records_list if r.get("price") is not None]
                if status == "ok" and price is not None:
                    hist_prices.append(float(price))
                avg_price = (sum(hist_prices) / len(hist_prices)) if hist_prices else None

                # 1. First-time discovery card for a newly added item
                if needs_initial_card and status == "ok" and price is not None:
                    hit_target_immediately = (
                        target_price is not None
                        and seller_matches
                        and price <= float(target_price)
                    )
                    if hit_target_immediately:
                        ref_block = build_price_reference_block(
                            current_price=price,
                            min_price=effective_min,
                            max_price=effective_max,
                            avg_price=avg_price,
                            list_price=effective_list_price,
                            amazon_min_price=effective_amz_min,
                            prev_price_before_oos=prev_last_price,
                            is_back_in_stock=False,
                        )
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
                            sns_price=sns_price,
                            promos=promos,
                            ref_block=ref_block,
                        )
                        send_telegram(msg, markup)
                    else:
                        msg, markup = build_new_product_discovery_card(
                            name=name,
                            asin=asin,
                            clean_url=clean_url,
                            price=price,
                            min_price=effective_min,
                            max_price=effective_max,
                            list_price=effective_list_price,
                            target_price=target_price,
                            seller_name=seller_name,
                            seller_filter_label=seller_filter_label,
                            resale_note=resale_note,
                            now_dt=now_dt,
                            sns_price=sns_price,
                            promos=promos,
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

                # 2. Existing tracked item: Back in stock, price drop, S&S drop & new promo alerts
                else:
                    if prev_status == "oos" and status == "ok" and price is not None:
                        if seller_matches:
                            subline = f"🎯 <b>التارجت:</b> {float(target_price):,.2f} ج.م" if target_price is not None else ""
                            ref_block = build_price_reference_block(
                                current_price=price,
                                min_price=effective_min,
                                max_price=effective_max,
                                avg_price=avg_price,
                                list_price=effective_list_price,
                                amazon_min_price=effective_amz_min,
                                prev_price_before_oos=prev_last_price,
                                is_back_in_stock=True,
                            )
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
                                sns_price=sns_price,
                                promos=promos,
                                ref_block=ref_block,
                            )
                            send_telegram(msg, markup)

                    elif status == "ok" and price is not None and seller_matches:
                        regular_triggered = False
                        sns_triggered = False
                        promo_triggered = False
                        header = "📉 <b>نزل سعره على أمازون!</b>"
                        price_subline = ""

                        if target_price is not None:
                            t_val = float(target_price)
                            if price <= t_val and (prev_matching_price is None or price < prev_matching_price):
                                regular_triggered = True
                                header = "🎯 <b>جبت السعر اللي عايزه!</b>"
                                if prev_matching_price is not None and price < prev_matching_price:
                                    diff = prev_matching_price - price
                                    price_subline = f"🎯 <b>التارجت:</b> {t_val:,.2f} ج.م (📉 نزل: {diff:,.2f} ج.م)"
                                else:
                                    price_subline = f"🎯 <b>التارجت:</b> {t_val:,.2f} ج.م"
                        else:
                            if prev_matching_price is not None and price < prev_matching_price:
                                regular_triggered = True
                                diff = prev_matching_price - price
                                pct = (diff / prev_matching_price) * 100
                                price_subline = f"📉 <b>نزل:</b> {diff:,.2f} ج.م ({pct:.0f}%) — كان {prev_matching_price:,.2f} ج.م"

                        if sns_price is not None:
                            if target_price is not None:
                                t_val = float(target_price)
                                if sns_price <= t_val and (prev_sns_price is None or sns_price < prev_sns_price):
                                    sns_triggered = True
                            else:
                                if prev_sns_price is not None and sns_price < prev_sns_price:
                                    sns_triggered = True

                        if curr_promo_sig and curr_promo_sig != prev_promo_sig:
                            promo_triggered = True

                        if regular_triggered or sns_triggered or promo_triggered:
                            if not regular_triggered:
                                if sns_triggered:
                                    header = "🔁 <b>نزل سعر الاشتراك والتوفير (Subscribe & Save)!</b>"
                                elif promo_triggered:
                                    header = "🎟️ <b>ظهر برومو كود / عرض توفير جديد!</b>"

                            timeframe_note = (
                                get_drop_timeframe_label(records_list, price, effective_min, now_dt)
                                if regular_triggered
                                else ""
                            )
                            ref_block = build_price_reference_block(
                                current_price=price,
                                min_price=effective_min,
                                max_price=effective_max,
                                avg_price=avg_price,
                                list_price=effective_list_price,
                                amazon_min_price=effective_amz_min,
                                prev_price_before_oos=prev_last_price,
                                is_back_in_stock=False,
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
                                sns_price=sns_price,
                                promos=promos,
                                ref_block=ref_block,
                            )
                            send_telegram(msg, markup)

                        if rec.get("last_matching_price") != price:
                            rec["last_matching_price"] = price
                            history_changed = True

                effective_seller = seller_name if status == "ok" else "غير متوفر"
                if should_record_snapshot(rec, status, price, effective_seller, now_dt):
                    records_list.append({
                        "ts": now_dt.isoformat(timespec="minutes"),
                        "price": price,
                        "sns_price": sns_price,
                        "seller": effective_seller,
                        "status": status,
                    })
                    if len(records_list) > 4500:
                        rec["records"] = records_list[-4500:]
                    history_changed = True

                if (
                    rec.get("name") != name
                    or rec.get("status") != status
                    or rec.get("last_sns_price") != sns_price
                    or rec.get("last_promo_sig") != curr_promo_sig
                    or rec.get("list_price") != effective_list_price
                    or rec.get("amazon_min_price") != effective_amz_min
                ):
                    history_changed = True

                rec["name"] = name
                rec["url"] = clean_url
                rec["status"] = status
                rec["last_sns_price"] = sns_price
                rec["last_promo_sig"] = curr_promo_sig
                rec["list_price"] = effective_list_price
                rec["amazon_min_price"] = effective_amz_min

                if status == "ok" and price is not None:
                    if rec.get("last_price") != price or rec.get("last_seller") != seller_name:
                        history_changed = True
                    rec["last_price"] = price
                    rec["last_seller"] = seller_name
                    rec["min_price"] = effective_min
                    rec["max_price"] = effective_max
                elif status == "oos":
                    rec["last_seller"] = "غير متوفر"

                prod_records[key] = rec

            if idx < len(items_to_scan) - 1:
                time.sleep(random.uniform(3, 7))

        if context:
            try:
                context.storage_state(path=SESSION_FILE)
            except Exception as e:
                print(f"Session save note: {e}")
            context.close()
        browser.close()

    if len(items_to_scan) > 0 and blocked_count == len(items_to_scan):
        send_telegram("⚠️ <b>تنبيه:</b> أمازون حظر محاولة الفحص الحالية بصفحة Captcha، سيعاود النظام المحاولة في الدورة القادمة.")

    if SCAN_MODE == "manual":
        send_products_summary(products, prod_records)
    elif not TARGET_ASIN:
        today_str = now_dt.strftime("%Y-%m-%d")
        if now_dt.hour >= 21 and history_data.get("last_daily_summary_date") != today_str:
            send_daily_summary(products, prod_records, now_dt)
            history_data["last_daily_summary_date"] = today_str
            history_changed = True

    if products_changed:
        with open(PRODUCTS_FILE, "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False, indent=2)

    if history_changed:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
