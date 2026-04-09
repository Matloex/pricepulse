#!/usr/bin/env python3
"""
PricePulse Bot v4 — Link + Anahtar Kelime + Satıcı Takip Sistemi

Kurulum:
    pip install requests beautifulsoup4 python-telegram-bot==20.7 schedule

Takip türleri:
    - link     : Direkt ürün linki (önceki gibi)
    - keyword  : Anahtar kelime ile arama sonuçlarını tara
    - seller   : Satıcı mağaza sayfasını tara
"""

import asyncio
import logging
import schedule
import threading
import time
import json
import os
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler
)

# ════════════════════════════════════════════════════════
#  ⚙️  AYARLAR
# ════════════════════════════════════════════════════════

TELEGRAM_TOKEN = "8597080702:AAFY-udbqWDoTmuAJIr52GGmDl5DuSCTFoE"
CHAT_ID = "1003976351142"
CHECK_EVERY = 1
ALERT_COOLDOWN = 30
DAILY_REPORT = "09:00"
DATA_FILE = "products.json"
SETTINGS_FILE = "settings.json"

# Conversation state'leri
(ASK_TYPE,
 ASK_NAME, ASK_URL, ASK_PRICE,
 ASK_KW_PLATFORM, ASK_KW_QUERY, ASK_KW_PRICE,
 ASK_SELLER_PLATFORM, ASK_SELLER_URL, ASK_SELLER_KEYWORD, ASK_SELLER_PRICE,
 ASK_HEDEF_URUN, ASK_HEDEF_FIYAT,
 ASK_NOT_URUN, ASK_NOT_METIN,
 ASK_KOPYALA_URUN, ASK_KOPYALA_FIYAT,
 ASK_AYAR_KEY, ASK_AYAR_VAL) = range(19)

# ════════════════════════════════════════════════════════
#  🗄️  VERİ YÖNETİMİ
# ════════════════════════════════════════════════════════


def load_products():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_products(products):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)


def load_settings():
    defaults = {
        "check_every": CHECK_EVERY,
        "alert_cooldown": ALERT_COOLDOWN,
        "daily_report": DAILY_REPORT,
        "silent_start": "23:00",
        "silent_end": "08:00",
        "silent_mode": False,
        "max_keyword_results": 5,   # anahtar kelimede kaç ürün kontrol edilsin
    }
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            defaults.update(json.load(f))
    return defaults


def save_settings(s):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


PRODUCTS = load_products()
SETTINGS = load_settings()

# ════════════════════════════════════════════════════════
#  🔧  YARDIMCI
# ════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s · %(levelname)s · %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("pricepulse.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9",
}


def fmt(p): return f"₺{p:,.0f}"
def uid(): return f"u{int(time.time())}"
def now_str(): return datetime.now().strftime("%H:%M")
def date_str(): return datetime.now().strftime("%d.%m.%Y")


def platform_from_url(url):
    u = url.lower()
    for p in ["trendyol", "hepsiburada", "amazon", "n11"]:
        if p in u:
            return p
    return "diger"


def is_silent():
    if not SETTINGS.get("silent_mode"):
        return False
    now = datetime.now()
    start = datetime.strptime(SETTINGS["silent_start"], "%H:%M").replace(year=now.year, month=now.month, day=now.day)
    end = datetime.strptime(SETTINGS["silent_end"], "%H:%M").replace(year=now.year, month=now.month, day=now.day)
    return (now >= start or now <= end) if start > end else (start <= now <= end)


def current_price(p):
    h = p.get("price_history", [])
    return h[-1]["price"] if h else None


def record_price(product, price):
    entry = {"price": price, "date": datetime.now().strftime("%Y-%m-%d %H:%M")}
    h = product.setdefault("price_history", [])
    if not h or h[-1]["price"] != price:
        h.append(entry)
    product["price_history"] = h[-50:]
    save_products(PRODUCTS)


def price_trend(product):
    h = product.get("price_history", [])
    if len(h) < 2:
        return "📊 Yeni takipte"
    prices = [x["price"] for x in h[-5:]]
    if prices[-1] < prices[0]:
        return f"📉 {fmt(prices[0] - prices[-1])} düştü"
    elif prices[-1] > prices[0]:
        return f"📈 {fmt(prices[-1] - prices[0])} yükseldi"
    return "➡️ Stabil"


def lowest_price(p):
    h = p.get("price_history", [])
    return min((x["price"] for x in h), default=None)


def highest_price(p):
    h = p.get("price_history", [])
    return max((x["price"] for x in h), default=None)


def get_last_log(n=20):
    try:
        with open("pricepulse.log", "r", encoding="utf-8") as f:
            return "".join(f.readlines()[-n:])
    except BaseException:
        return "Log bulunamadı."

# ════════════════════════════════════════════════════════
#  🔗  ARAMA URL ÜRETİCİLERİ
# ════════════════════════════════════════════════════════


def search_url(platform: str, query: str) -> str:
    q = requests.utils.quote(query)
    urls = {
        "trendyol": f"https://www.trendyol.com/sr?q={q}&qt={q}&st={q}",
        "hepsiburada": f"https://www.hepsiburada.com/ara?q={q}",
        "amazon": f"https://www.amazon.com.tr/s?k={q}",
        "n11": f"https://www.n11.com/arama?q={q}",
    }
    return urls.get(platform.lower(), f"https://www.trendyol.com/sr?q={q}")

# ════════════════════════════════════════════════════════
#  💰  FİYAT ÇEKİCİLER — ÜRÜN LİNKİ
# ════════════════════════════════════════════════════════


def get_price_trendyol(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        in_stock = not bool(soup.select_one(".sold-out-badge"))
        for sc in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(sc.string)
                if isinstance(data, list):
                    data = data[0]
                offers = data.get("offers", {})
                if "OutOfStock" in offers.get("availability", ""):
                    in_stock = False
                price = offers.get("price")
                if price:
                    return float(price), in_stock
            except BaseException:
                pass
        el = soup.select_one(".prc-dsc, .pr-bx-pr-dsc span")
        if el:
            raw = el.text.strip().replace(".", "").replace(",", ".").replace("TL", "").strip()
            return float(raw), in_stock
    except Exception as e:
        log.warning(f"Trendyol link hata: {e}")
    return None, True


def get_price_hepsiburada(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        in_stock = not bool(soup.select_one(".out-of-stock"))
        el = soup.select_one('[itemprop="price"]')
        if el:
            raw = el.get("content") or el.text
            return float(str(raw).replace(".", "").replace(",", ".").strip()), in_stock
    except Exception as e:
        log.warning(f"Hepsiburada link hata: {e}")
    return None, True


def get_price_amazon(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        in_stock = "Stokta yok" not in r.text
        whole = soup.select_one(".a-price-whole")
        frac = soup.select_one(".a-price-fraction")
        if whole:
            price = float(whole.text.strip().replace(".", "").replace(",", ""))
            if frac:
                price += float(frac.text.strip()) / 100
            return price, in_stock
    except Exception as e:
        log.warning(f"Amazon link hata: {e}")
    return None, True


def get_price_n11(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        el = soup.select_one(".newPrice ins, .price ins")
        if el:
            raw = el.text.strip().replace(".", "").replace(",", ".").replace("TL", "").strip()
            return float(raw), True
    except Exception as e:
        log.warning(f"N11 link hata: {e}")
    return None, True


def fetch_price_link(product):
    p = product["platform"].lower()
    u = product["url"]
    if "trendyol" in p:
        return get_price_trendyol(u)
    if "hepsiburada" in p:
        return get_price_hepsiburada(u)
    if "amazon" in p:
        return get_price_amazon(u)
    if "n11" in p:
        return get_price_n11(u)
    return None, True

# ════════════════════════════════════════════════════════
#  🔍  ANAHTAR KELİME TARAYICILARI
#      Her biri [(isim, fiyat, url, satıcı)] döndürür
# ════════════════════════════════════════════════════════


def search_trendyol(query: str, limit: int = 5):
    results = []
    try:
        url = search_url("trendyol", query)
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select(".p-card-wrppr")[:limit]
        for card in cards:
            try:
                name_el = card.select_one(".prdct-desc-cntnr-name, .product-title")
                price_el = card.select_one(".prc-box-dscntd, .prc-box-sllng")
                link_el = card.select_one("a")
                seller_el = card.select_one(".merchant-text, .seller-name-text")
                if not name_el or not price_el:
                    continue
                name = name_el.get_text(strip=True)
                price_raw = price_el.get_text(strip=True).replace(".", "").replace(",", ".").replace("TL", "").strip()
                price = float(price_raw)
                link = "https://www.trendyol.com" + link_el["href"] if link_el else url
                seller = seller_el.get_text(strip=True) if seller_el else "Bilinmiyor"
                results.append({"name": name, "price": price, "url": link, "seller": seller})
            except BaseException:
                continue
    except Exception as e:
        log.warning(f"Trendyol arama hata: {e}")
    return results


def search_hepsiburada(query: str, limit: int = 5):
    results = []
    try:
        url = search_url("hepsiburada", query)
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("[data-test-id='product-card-wrapper']")[:limit]
        for card in cards:
            try:
                name_el = card.select_one("[data-test-id='product-card-name']")
                price_el = card.select_one("[data-test-id='price-current-price']")
                link_el = card.select_one("a")
                seller_el = card.select_one("[data-test-id='product-card-merchant']")
                if not name_el or not price_el:
                    continue
                name = name_el.get_text(strip=True)
                price_raw = price_el.get_text(strip=True).replace(".", "").replace(",", ".").replace("TL", "").strip()
                price = float(price_raw)
                link = "https://www.hepsiburada.com" + link_el["href"] if link_el and link_el.get("href", "").startswith("/") else (link_el["href"] if link_el else url)
                seller = seller_el.get_text(strip=True) if seller_el else "Bilinmiyor"
                results.append({"name": name, "price": price, "url": link, "seller": seller})
            except BaseException:
                continue
    except Exception as e:
        log.warning(f"Hepsiburada arama hata: {e}")
    return results


def search_amazon(query: str, limit: int = 5):
    results = []
    try:
        url = search_url("amazon", query)
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("[data-component-type='s-search-result']")[:limit]
        for card in cards:
            try:
                name_el = card.select_one("h2 span")
                whole_el = card.select_one(".a-price-whole")
                link_el = card.select_one("h2 a")
                seller_el = card.select_one(".a-row .a-size-base+ .a-size-base")
                if not name_el or not whole_el:
                    continue
                name = name_el.get_text(strip=True)
                price = float(whole_el.get_text(strip=True).replace(".", "").replace(",", ""))
                link = "https://www.amazon.com.tr" + link_el["href"] if link_el else url
                seller = seller_el.get_text(strip=True) if seller_el else "Bilinmiyor"
                results.append({"name": name, "price": price, "url": link, "seller": seller})
            except BaseException:
                continue
    except Exception as e:
        log.warning(f"Amazon arama hata: {e}")
    return results


def search_n11(query: str, limit: int = 5):
    results = []
    try:
        url = search_url("n11", query)
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select(".columnContent .column")[:limit]
        for card in cards:
            try:
                name_el = card.select_one(".productName")
                price_el = card.select_one(".newPrice ins, .price ins")
                link_el = card.select_one("a")
                seller_el = card.select_one(".sellerStoreName")
                if not name_el or not price_el:
                    continue
                name = name_el.get_text(strip=True)
                price_raw = price_el.get_text(strip=True).replace(".", "").replace(",", ".").replace("TL", "").strip()
                price = float(price_raw)
                link = link_el["href"] if link_el else url
                seller = seller_el.get_text(strip=True) if seller_el else "Bilinmiyor"
                results.append({"name": name, "price": price, "url": link, "seller": seller})
            except BaseException:
                continue
    except Exception as e:
        log.warning(f"N11 arama hata: {e}")
    return results


def do_keyword_search(platform: str, query: str, limit: int = 5):
    p = platform.lower()
    if "trendyol" in p:
        return search_trendyol(query, limit)
    if "hepsiburada" in p:
        return search_hepsiburada(query, limit)
    if "amazon" in p:
        return search_amazon(query, limit)
    if "n11" in p:
        return search_n11(query, limit)
    return []

# ════════════════════════════════════════════════════════
#  🏪  SATICI TARAYICILARI
# ════════════════════════════════════════════════════════


def scrape_seller_trendyol(seller_url: str, limit: int = 10):
    results = []
    try:
        r = requests.get(seller_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select(".p-card-wrppr")[:limit]
        for card in cards:
            try:
                name_el = card.select_one(".prdct-desc-cntnr-name")
                price_el = card.select_one(".prc-box-dscntd, .prc-box-sllng")
                link_el = card.select_one("a")
                if not name_el or not price_el:
                    continue
                name = name_el.get_text(strip=True)
                price_raw = price_el.get_text(strip=True).replace(".", "").replace(",", ".").replace("TL", "").strip()
                price = float(price_raw)
                link = "https://www.trendyol.com" + link_el["href"] if link_el else seller_url
                results.append({"name": name, "price": price, "url": link})
            except BaseException:
                continue
    except Exception as e:
        log.warning(f"Trendyol satıcı hata: {e}")
    return results


def scrape_seller_hepsiburada(seller_url: str, limit: int = 10):
    results = []
    try:
        r = requests.get(seller_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("[data-test-id='product-card-wrapper']")[:limit]
        for card in cards:
            try:
                name_el = card.select_one("[data-test-id='product-card-name']")
                price_el = card.select_one("[data-test-id='price-current-price']")
                link_el = card.select_one("a")
                if not name_el or not price_el:
                    continue
                name = name_el.get_text(strip=True)
                price_raw = price_el.get_text(strip=True).replace(".", "").replace(",", ".").replace("TL", "").strip()
                price = float(price_raw)
                link = "https://www.hepsiburada.com" + link_el["href"] if link_el and link_el.get("href", "").startswith("/") else (link_el["href"] if link_el else seller_url)
                results.append({"name": name, "price": price, "url": link})
            except BaseException:
                continue
    except Exception as e:
        log.warning(f"Hepsiburada satıcı hata: {e}")
    return results


def scrape_seller_amazon(seller_url: str, limit: int = 10):
    """
    Amazon satıcı mağaza sayfasını tara.
    Satıcı mağaza URL formatı:
      https://www.amazon.com.tr/s?me=SELLER_ID   (satıcı ürün listesi)
      https://www.amazon.com.tr/sp?seller=XXXX   (satıcı profili)
    Profil sayfası verilirse otomatik ürün listesine yönlendir.
    """
    results = []
    try:
        # Profil sayfasıysa ürün listesi URL'ine çevir
        if "/sp?" in seller_url or "/seller/" in seller_url:
            # seller ID'yi URL'den çek
            import re
            m = re.search(r"seller=([A-Z0-9]+)", seller_url)
            if m:
                seller_id = m.group(1)
                seller_url = f"https://www.amazon.com.tr/s?me={seller_id}&marketplaceID=A33AVmaiden2RD"

        r = requests.get(seller_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("[data-component-type='s-search-result']")[:limit]
        for card in cards:
            try:
                name_el = card.select_one("h2 span")
                whole_el = card.select_one(".a-price-whole")
                frac_el = card.select_one(".a-price-fraction")
                link_el = card.select_one("h2 a")
                if not name_el or not whole_el:
                    continue
                name = name_el.get_text(strip=True)
                price = float(whole_el.get_text(strip=True).replace(".", "").replace(",", ""))
                if frac_el:
                    price += float(frac_el.get_text(strip=True)) / 100
                link = "https://www.amazon.com.tr" + link_el["href"] if link_el else seller_url
                results.append({"name": name, "price": price, "url": link})
            except BaseException:
                continue
    except Exception as e:
        log.warning(f"Amazon satıcı hata: {e}")
    return results


def do_seller_scrape(platform: str, seller_url: str, limit: int = 10):
    p = platform.lower()
    if "trendyol" in p:
        return scrape_seller_trendyol(seller_url, limit)
    if "hepsiburada" in p:
        return scrape_seller_hepsiburada(seller_url, limit)
    if "amazon" in p:
        return scrape_seller_amazon(seller_url, limit)
    return []

# ════════════════════════════════════════════════════════
#  📲  MESAJ OLUŞTURUCULARI
# ════════════════════════════════════════════════════════


def build_alert_link(product, price, in_stock):
    """Direkt link alarmı"""
    diff = product["target_price"] - price
    pct = round((diff / product["target_price"]) * 100, 1)
    low = lowest_price(product)
    stock = "✅ Stokta" if in_stock else "⚠️ Stok belirsiz"
    note = f"\n📝 <i>{product['note']}</i>" if product.get("note") else ""
    low_flag = "🏆 <b>Tüm zamanların en ucuzu!</b>\n" if (low and price <= low) else ""

    text = (
        f"⚡️ <b>FİYAT ALARMI — PricePulse</b>\n\n"
        f"📦 <b>{product['name']}</b>{note}\n"
        f"🏪 {product['platform'].capitalize()}\n\n"
        f"💰 Şu an: <b>{fmt(price)}</b>\n"
        f"🎯 Hedef: {fmt(product['target_price'])}\n"
        f"📉 <b>{fmt(diff)} ({pct}%) daha ucuz!</b>\n"
        f"{low_flag}{price_trend(product)} | {stock}\n\n"
        f"⏰ {now_str()}'de tespit edildi\n"
        f"━━━━━━━━━━━━━━━\nNe yapmak istersin?"
    )
    uid_val = product["id"]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Satın Al →", url=product["url"])],
        [
            InlineKeyboardButton("⏸ 1 Saat", callback_data=f"pause_60_{uid_val}"),
            InlineKeyboardButton("⏸ 3 Saat", callback_data=f"pause_180_{uid_val}"),
            InlineKeyboardButton("⏸ Yarın", callback_data=f"pause_1440_{uid_val}"),
        ],
        [
            InlineKeyboardButton("❌ Durdur", callback_data=f"stop_{uid_val}"),
            InlineKeyboardButton("📊 Geçmiş", callback_data=f"history_{uid_val}"),
            InlineKeyboardButton("✅ Devam", callback_data=f"resume_{uid_val}"),
        ],
    ])
    return text, kb


def build_alert_keyword(product, matched_item):
    """Anahtar kelime eşleşme alarmı"""
    diff = product["target_price"] - matched_item["price"]
    pct = round((diff / product["target_price"]) * 100, 1)

    text = (
        f"🔍 <b>ANAHTAR KELİME EŞLEŞTİ — PricePulse</b>\n\n"
        f"🔎 Arama: <b>\"{product['keyword']}\"</b>\n"
        f"🏪 {product['platform'].capitalize()}\n\n"
        f"📦 <b>{matched_item['name']}</b>\n"
        f"👤 Satıcı: {matched_item.get('seller', '—')}\n\n"
        f"💰 Fiyat: <b>{fmt(matched_item['price'])}</b>\n"
        f"🎯 Hedefin: {fmt(product['target_price'])}\n"
        f"📉 <b>{fmt(diff)} ({pct}%) daha ucuz!</b>\n\n"
        f"⏰ {now_str()}'de bulundu\n"
        f"━━━━━━━━━━━━━━━\nNe yapmak istersin?"
    )
    uid_val = product["id"]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Ürüne Git →", url=matched_item["url"])],
        [
            InlineKeyboardButton("⏸ 1 Saat", callback_data=f"pause_60_{uid_val}"),
            InlineKeyboardButton("⏸ 3 Saat", callback_data=f"pause_180_{uid_val}"),
            InlineKeyboardButton("⏸ Yarın", callback_data=f"pause_1440_{uid_val}"),
        ],
        [
            InlineKeyboardButton("❌ Durdur", callback_data=f"stop_{uid_val}"),
            InlineKeyboardButton("✅ Devam", callback_data=f"resume_{uid_val}"),
        ],
    ])
    return text, kb


def build_alert_seller(product, new_item):
    """Satıcıda yeni ürün / fiyat alarmı"""
    diff = product["target_price"] - new_item["price"]
    pct = round((diff / product["target_price"]) * 100, 1)
    is_cheap = new_item["price"] <= product["target_price"]

    header = "💰 SATICI FİYAT ALARMI" if is_cheap else "🆕 SATICIDE YENİ ÜRÜN"

    text = (
        f"🏪 <b>{header} — PricePulse</b>\n\n"
        f"🏬 Satıcı: <b>{product['seller_name']}</b>\n"
        f"🏪 {product['platform'].capitalize()}\n\n"
        f"📦 <b>{new_item['name']}</b>\n\n"
        f"💰 Fiyat: <b>{fmt(new_item['price'])}</b>\n"
        f"🎯 Hedefin: {fmt(product['target_price'])}\n"
        + (f"📉 <b>{fmt(diff)} ({pct}%) daha ucuz!</b>\n" if is_cheap else "") +
        f"\n⏰ {now_str()}'de tespit edildi\n"
        f"━━━━━━━━━━━━━━━\nNe yapmak istersin?"
    )
    uid_val = product["id"]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Ürüne Git →", url=new_item["url"])],
        [
            InlineKeyboardButton("⏸ 1 Saat", callback_data=f"pause_60_{uid_val}"),
            InlineKeyboardButton("⏸ 3 Saat", callback_data=f"pause_180_{uid_val}"),
        ],
        [
            InlineKeyboardButton("❌ Durdur", callback_data=f"stop_{uid_val}"),
            InlineKeyboardButton("✅ Devam", callback_data=f"resume_{uid_val}"),
        ],
    ])
    return text, kb


def build_daily_report():
    if not PRODUCTS:
        return "📭 Takip listesi boş."
    aktif = [p for p in PRODUCTS if p.get("active")]
    type_icons = {"link": "🔗", "keyword": "🔍", "seller": "🏪"}
    lines = [
        "📊 <b>PricePulse — Günlük Rapor</b>",
        f"🗓 {datetime.now().strftime('%d.%m.%Y')}",
        f"📦 {len(aktif)} takip aktif",
        "━━━━━━━━━━━━━━━",
    ]
    for p in aktif:
        icon = type_icons.get(p.get("type", "link"), "🔗")
        cur = current_price(p)
        pa = p.get("paused_until")
        durum = "✅" if not (pa and time.time() < pa) else "⏸"
        label = p.get("keyword") or p.get("seller_name") or p.get("name", "?")
        cur_str = f" | {fmt(cur)}" if cur else ""
        lines.append(f"\n{icon}{durum} <b>{label}</b>\n   Hedef: {fmt(p['target_price'])}{cur_str} | {price_trend(p)}")
    lines += ["", "━━━━━━━━━━━━━━━"]
    return "\n".join(lines)

# ════════════════════════════════════════════════════════
#  ➕  ÜRÜN EKLEME CONVERSATION  (/ekle)
# ════════════════════════════════════════════════════════


async def cmd_ekle(update, context):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Direkt Link", callback_data="ekle_link")],
        [InlineKeyboardButton("🔍 Anahtar Kelime", callback_data="ekle_keyword")],
        [InlineKeyboardButton("🏪 Satıcı Takibi", callback_data="ekle_seller")],
    ])
    await update.message.reply_text(
        "➕ <b>Ne tür takip eklemek istiyorsun?</b>\n\n"
        "🔗 <b>Direkt Link</b> — Ürün sayfasının linkini gir\n"
        "🔍 <b>Anahtar Kelime</b> — \"sony kulaklık\" gibi yaz, uygun ürünler bulununca bildir\n"
        "🏪 <b>Satıcı Takibi</b> — Satıcı mağaza sayfasını izle, fiyat düşünce/yeni ürün gelince bildir\n\n"
        "/iptal",
        parse_mode="HTML", reply_markup=kb
    )
    return ASK_TYPE


async def ekle_type_sec(update, context):
    query = update.callback_query
    await query.answer()
    t = query.data.replace("ekle_", "")
    context.user_data["type"] = t

    if t == "link":
        await query.edit_message_text("🔗 Ürünün adını yaz:\n/iptal", parse_mode="HTML")
        return ASK_NAME
    elif t == "keyword":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(p, callback_data=f"kw_platform_{p}")]
            for p in ["Trendyol", "Amazon TR", "Hepsiburada", "N11"]
        ])
        await query.edit_message_text("🔍 <b>Anahtar Kelime Takibi</b>\n\nHangi platformda arayalım?", parse_mode="HTML", reply_markup=kb)
        return ASK_KW_PLATFORM
    elif t == "seller":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(p, callback_data=f"seller_platform_{p}")]
            for p in ["Trendyol", "Amazon TR", "Hepsiburada"]
        ])
        await query.edit_message_text(
            "🏪 <b>Satıcı Takibi</b>\n\nHangi platformdaki satıcıyı takip etmek istiyorsun?\n\n"
            "<i>Amazon için satıcı mağaza linki veya https://www.amazon.com.tr/s?me=SATICI_ID formatı</i>",
            parse_mode="HTML", reply_markup=kb
        )
        return ASK_SELLER_PLATFORM

# ── Link akışı ───────────────────────────────────────────


async def ekle_name(update, context):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("🔗 Ürün linkini gönder:", parse_mode="HTML")
    return ASK_URL


async def ekle_url(update, context):
    url = update.message.text.strip()
    if not url.startswith("http"):
        await update.message.reply_text("❌ Geçersiz link, tekrar dene:")
        return ASK_URL
    context.user_data["url"] = url
    context.user_data["platform"] = platform_from_url(url)
    await update.message.reply_text(
        f"✅ Platform: <b>{context.user_data['platform'].capitalize()}</b>\n\nHedef fiyatı yaz (₺):",
        parse_mode="HTML"
    )
    return ASK_PRICE


async def ekle_price(update, context):
    try:
        price = float(update.message.text.strip().replace(",", ".").replace("₺", ""))
    except BaseException:
        await update.message.reply_text("❌ Geçersiz fiyat:")
        return ASK_PRICE
    product = {
        "id": uid(), "type": "link",
        "name": context.user_data["name"], "url": context.user_data["url"],
        "platform": context.user_data["platform"], "target_price": price,
        "active": True, "paused_until": None, "last_alerted": None,
        "price_history": [], "note": "", "added_at": date_str(),
    }
    PRODUCTS.append(product)
    save_products(PRODUCTS)
    context.user_data.clear()
    await update.message.reply_text(
        f"✅ <b>Link takibi eklendi!</b>\n\n📦 {product['name']}\n🎯 Hedef: {fmt(price)}",
        parse_mode="HTML"
    )
    return ConversationHandler.END

# ── Anahtar kelime akışı ─────────────────────────────────


async def kw_platform_sec(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data["platform"] = query.data.replace("kw_platform_", "")
    await query.edit_message_text(
        f"🔍 Platform: <b>{context.user_data['platform']}</b>\n\n"
        "Ne aramak istiyorsun? Yaz:\n"
        "<i>örn: sony kulaklık, iphone 15, gaming mouse</i>\n\n/iptal",
        parse_mode="HTML"
    )
    return ASK_KW_QUERY


async def kw_query_gir(update, context):
    context.user_data["keyword"] = update.message.text.strip()
    await update.message.reply_text(
        f"🔍 <b>\"{context.user_data['keyword']}\"</b> aranacak\n\n"
        "Bu arama sonuçlarında hangi fiyat altını bildir?\n"
        "<i>Sadece rakam gir (örn: 3500)</i>\n/iptal",
        parse_mode="HTML"
    )
    return ASK_KW_PRICE


async def kw_price_gir(update, context):
    try:
        price = float(update.message.text.strip().replace(",", ".").replace("₺", ""))
    except BaseException:
        await update.message.reply_text("❌ Geçersiz fiyat:")
        return ASK_KW_PRICE
    product = {
        "id": uid(), "type": "keyword",
        "name": f"🔍 {context.user_data['keyword']}",
        "keyword": context.user_data["keyword"],
        "platform": context.user_data["platform"],
        "target_price": price, "active": True,
        "paused_until": None, "last_alerted": None,
        "price_history": [], "note": "",
        "seen_urls": [],   # daha önce alarmı verilen ürün linkleri
        "added_at": date_str(),
    }
    PRODUCTS.append(product)
    save_products(PRODUCTS)
    context.user_data.clear()
    await update.message.reply_text(
        f"✅ <b>Anahtar kelime takibi eklendi!</b>\n\n"
        f"🔍 Arama: \"{product['keyword']}\"\n"
        f"🏪 Platform: {product['platform']}\n"
        f"🎯 Hedef fiyat: {fmt(price)} altı\n\n"
        f"Her taramada ilk {SETTINGS.get('max_keyword_results', 5)} sonuç kontrol edilecek.",
        parse_mode="HTML"
    )
    return ConversationHandler.END

# ── Satıcı akışı ─────────────────────────────────────────
SELLER_HINTS = {
    "Trendyol": ("https://www.trendyol.com/magaza/techstore-m-123456",
                 "Trendyol → mağaza profil sayfasının linki"),
    "Amazon TR": ("https://www.amazon.com.tr/s?me=SATICI_ID",
                  "Amazon → satıcı ürün listesi linki veya profil sayfası"),
    "Hepsiburada": ("https://www.hepsiburada.com/magaza/techstore",
                    "Hepsiburada → satıcı mağaza sayfasının linki"),
}


async def seller_platform_sec(update, context):
    query = update.callback_query
    await query.answer()
    platform = query.data.replace("seller_platform_", "")
    context.user_data["platform"] = platform
    placeholder, hint = SELLER_HINTS.get(platform, ("https://...", "Mağaza linki"))
    await query.edit_message_text(
        f"🏪 Platform: <b>{platform}</b>\n\n"
        f"Satıcının mağaza sayfasının linkini gönder:\n"
        f"<i>{hint}</i>\n"
        f"<code>{placeholder}</code>\n\n/iptal",
        parse_mode="HTML"
    )
    return ASK_SELLER_URL


async def seller_url_gir(update, context):
    url = update.message.text.strip()
    if not url.startswith("http"):
        await update.message.reply_text("❌ Geçersiz link, http ile başlamalı:")
        return ASK_SELLER_URL
    context.user_data["seller_url"] = url
    # Satıcı adını linkten tahmin et
    parts = [p for p in url.split("/") if p and "?" not in p]
    context.user_data["seller_name"] = parts[-1] if parts else "Satıcı"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Anahtar Kelime Filtrele", callback_data="seller_kw_yes")],
        [InlineKeyboardButton("📦 Tüm Ürünleri Takip Et", callback_data="seller_kw_no")],
    ])
    await update.message.reply_text(
        f"✅ Mağaza linki alındı: <b>{context.user_data['seller_name']}</b>\n\n"
        "Bu mağazada nasıl takip yapmak istiyorsun?\n\n"
        "🔍 <b>Anahtar Kelime</b> — sadece belirli ürünleri izle\n"
        "   <i>örn: sadece 'kulaklık' veya 'laptop' ürünleri</i>\n\n"
        "📦 <b>Tüm Ürünler</b> — mağazadaki her şeyi izle",
        parse_mode="HTML", reply_markup=kb
    )
    return ASK_SELLER_KEYWORD


async def seller_keyword_sec(update, context):
    query = update.callback_query
    await query.answer()
    choice = query.data  # "seller_kw_yes" veya "seller_kw_no"

    if choice == "seller_kw_yes":
        context.user_data["seller_keyword"] = None  # sonra doldurulacak
        await query.edit_message_text(
            "🔍 Mağazada hangi kelimeyi aramak istiyorsun?\n\n"
            "<i>örn: kulaklık, laptop, samsung</i>\n\n"
            "Boş bırakmak için sadece - yaz\n/iptal",
            parse_mode="HTML"
        )
        return ASK_SELLER_KEYWORD  # keyword girişini bekle

    else:  # seller_kw_no — tüm ürünler
        context.user_data["seller_keyword"] = ""
        await query.edit_message_text(
            "💰 Fiyat eşiği nedir?\n\n"
            "Bu fiyat altındaki ürün bulununca bildirim gelir.\n"
            "<i>Tüm ürünler için bildirim istiyorsan 0 gir</i>\n/iptal",
            parse_mode="HTML"
        )
        return ASK_SELLER_PRICE


async def seller_keyword_gir(update, context):
    """Kullanıcı anahtar kelime yazdı"""
    text = update.message.text.strip()
    context.user_data["seller_keyword"] = "" if text == "-" else text
    await update.message.reply_text(
        f"✅ Filtre: <b>{'Tüm ürünler' if not context.user_data['seller_keyword'] else context.user_data['seller_keyword']}</b>\n\n"
        "💰 Fiyat eşiği nedir?\n"
        "<i>Bu fiyat altındaki ürün bulununca bildirim gelir. Tümü için 0 gir</i>\n/iptal",
        parse_mode="HTML"
    )
    return ASK_SELLER_PRICE


async def seller_price_gir(update, context):
    try:
        price = float(update.message.text.strip().replace(",", ".").replace("₺", ""))
    except BaseException:
        await update.message.reply_text("❌ Geçersiz fiyat, sadece rakam gir:")
        return ASK_SELLER_PRICE

    keyword = context.user_data.get("seller_keyword", "")
    keyword_label = f"🔍 Filtre: {keyword}" if keyword else "📦 Tüm ürünler"

    product = {
        "id": uid(), "type": "seller",
        "name": f"🏪 {context.user_data['seller_name']}",
        "seller_name": context.user_data["seller_name"],
        "seller_url": context.user_data["seller_url"],
        "seller_keyword": keyword,           # "" = tüm ürünler, dolu = filtreli
        "platform": context.user_data["platform"],
        "target_price": price, "active": True,
        "paused_until": None, "last_alerted": None,
        "price_history": [], "note": "",
        "seen_urls": [],
        "added_at": date_str(),
    }
    PRODUCTS.append(product)
    save_products(PRODUCTS)
    context.user_data.clear()

    await update.message.reply_text(
        f"✅ <b>Satıcı takibi eklendi!</b>\n\n"
        f"🏪 {product['seller_name']} ({product['platform']})\n"
        f"{keyword_label}\n"
        f"🎯 Fiyat eşiği: {fmt(price) if price > 0 else 'Hepsi'}\n\n"
        f"Ürün bulununca veya fiyat düşünce bildirim alacaksın.",
        parse_mode="HTML"
    )
    return ConversationHandler.END


async def conv_iptal(update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ İptal edildi.")
    return ConversationHandler.END

# ════════════════════════════════════════════════════════
#  📋  KOMUTLAR
# ════════════════════════════════════════════════════════


async def cmd_start(update, context):
    await update.message.reply_text(
        "⚡️ <b>PricePulse v4</b>\n\n"
        "━━━━━━━━━━━━━━━\n"
        "<b>📦 Ürün Yönetimi</b>\n"
        "/ekle        — Link / Anahtar Kelime / Satıcı ekle\n"
        "/sil         — Takip sil\n"
        "/liste       — Tüm takipler\n"
        "/kopyala     — Takibi kopyala\n"
        "/not         — Not ekle\n\n"
        "<b>💰 Fiyat & Takip</b>\n"
        "/fiyat       — Anlık fiyat sorgula\n"
        "/hedef       — Hedef fiyatı değiştir\n"
        "/ara         — Şimdi anahtar kelime tara\n"
        "/kontrol     — Tüm takipleri şimdi tara\n"
        "/en_ucuz     — Hedefe en yakın ürünler\n\n"
        "<b>📊 Raporlar</b>\n"
        "/rapor       — Günlük rapor\n"
        "/istatistik  — Bot istatistikleri\n"
        "/platform    — Platforma göre listele\n"
        "/log         — Son log kayıtları\n\n"
        "<b>⚙️ Sistem</b>\n"
        "/durdurAll   — Tüm takibi duraklat\n"
        "/baslatAll   — Tümünü başlat\n"
        "/ayarlar     — Bot ayarları\n"
        "/yardim      — Yardım",
        parse_mode="HTML"
    )


async def cmd_liste(update, context):
    if not PRODUCTS:
        await update.message.reply_text("📭 Liste boş. /ekle ile takip ekle.")
        return
    type_icons = {"link": "🔗", "keyword": "🔍", "seller": "🏪"}
    lines = [f"📋 <b>Takip Listesi ({len(PRODUCTS)} kayıt)</b>\n"]
    for p in PRODUCTS:
        icon = type_icons.get(p.get("type", "link"), "🔗")
        durum = "✅" if p.get("active") else "❌"
        pa = p.get("paused_until")
        if pa and time.time() < pa:
            durum = f"⏸{int((pa - time.time()) / 60)}dk"
        label = p.get("keyword") or p.get("seller_name") or p.get("name", "?")
        cur = current_price(p)
        cur_str = f" | {fmt(cur)}" if cur else ""
        lines.append(f"{icon}{durum} <b>{label}</b>\n   🎯 {fmt(p['target_price'])}{cur_str}\n")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_ara(update, context):
    """Anahtar kelime takiplerini şimdi tara"""
    kws = [p for p in PRODUCTS if p.get("type") == "keyword" and p.get("active")]
    if not kws:
        await update.message.reply_text("🔍 Aktif anahtar kelime takibi yok. /ekle ile ekle.")
        return
    await update.message.reply_text(f"🔍 {len(kws)} anahtar kelime taranıyor...")
    # Mevcut uygulama üzerinde manuel tetikle
    for p in kws:
        results = do_keyword_search(p["platform"], p["keyword"], SETTINGS.get("max_keyword_results", 5))
        lines = [f"🔍 <b>\"{p['keyword']}\"</b> — {p['platform']}\n"]
        for r in results[:5]:
            ok = "🎯" if r["price"] <= p["target_price"] else "  "
            lines.append(f"{ok} {fmt(r['price'])} — {r['name'][:40]}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_kontrol(update, context):
    await update.message.reply_text("🔍 Taranıyor...")
    await run_price_check(context.application)
    await update.message.reply_text(f"✅ Tamamlandı. ({now_str()})")


async def cmd_rapor(update, context):
    await update.message.reply_text(build_daily_report(), parse_mode="HTML")


async def cmd_sil(update, context):
    if not PRODUCTS:
        await update.message.reply_text("📭 Liste boş.")
        return
    type_icons = {"link": "🔗", "keyword": "🔍", "seller": "🏪"}
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{type_icons.get(p.get('type', 'link'), '🔗')} {p.get('keyword') or p.get('seller_name') or p.get('name', '?')}",
            callback_data=f"delete_{p['id']}"
        )] for p in PRODUCTS
    ])
    await update.message.reply_text("Hangi takibi silmek istiyorsun?", reply_markup=kb)


async def cmd_fiyat(update, context):
    links = [p for p in PRODUCTS if p.get("type", "link") == "link" and p.get("active")]
    if not links:
        await update.message.reply_text("📭 Aktif link takibi yok.")
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(p["name"], callback_data=f"fiyatsor_{p['id']}")]
        for p in links
    ])
    await update.message.reply_text("Hangi ürünün fiyatını sorgulayalım?", reply_markup=kb)


async def cmd_en_ucuz(update, context):
    aktif = [p for p in PRODUCTS if p.get("active") and current_price(p)]
    if not aktif:
        await update.message.reply_text("📭 Veri yok.")
        return
    sirali = sorted(aktif, key=lambda p: (current_price(p) - p["target_price"]) / p["target_price"])
    lines = ["🏆 <b>Hedefe En Yakın Takipler</b>\n"]
    for i, p in enumerate(sirali[:5], 1):
        cur = current_price(p)
        diff = cur - p["target_price"]
        ok = "🎯" if diff <= 0 else f"+{round(diff / p['target_price'] * 100, 1)}%"
        label = p.get("keyword") or p.get("seller_name") or p.get("name", "?")
        lines.append(f"{i}. <b>{label}</b>\n   {fmt(cur)} → hedef {fmt(p['target_price'])} {ok}\n")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_platform(update, context):
    if not PRODUCTS:
        await update.message.reply_text("📭 Liste boş.")
        return
    gruplar = {}
    for p in PRODUCTS:
        pl = p["platform"].capitalize()
        gruplar.setdefault(pl, []).append(p)
    lines = ["🏪 <b>Platforma Göre Takipler</b>\n"]
    type_icons = {"link": "🔗", "keyword": "🔍", "seller": "🏪"}
    for pl, urunler in sorted(gruplar.items()):
        lines.append(f"<b>— {pl} ({len(urunler)}) —</b>")
        for p in urunler:
            icon = type_icons.get(p.get("type", "link"), "🔗")
            label = p.get("keyword") or p.get("seller_name") or p.get("name", "?")
            cur = current_price(p)
            lines.append(f"  {icon} {label} → hedef {fmt(p['target_price'])}" + (f" | {fmt(cur)}" if cur else ""))
        lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_durdurAll(update, context):
    count = sum(1 for p in PRODUCTS if p.get("active"))
    for p in PRODUCTS:
        p["active"] = False
    save_products(PRODUCTS)
    await update.message.reply_text(f"⏸ <b>{count} takip</b> duraklatıldı.", parse_mode="HTML")


async def cmd_baslatAll(update, context):
    count = sum(1 for p in PRODUCTS if not p.get("active"))
    for p in PRODUCTS:
        p["active"] = True
        p["paused_until"] = None
    save_products(PRODUCTS)
    await update.message.reply_text(f"✅ <b>{count} takip</b> başlatıldı.", parse_mode="HTML")


async def cmd_istatistik(update, context):
    aktif = len([p for p in PRODUCTS if p.get("active")])
    links = len([p for p in PRODUCTS if p.get("type", "link") == "link"])
    kws = len([p for p in PRODUCTS if p.get("type") == "keyword"])
    sellers = len([p for p in PRODUCTS if p.get("type") == "seller"])
    olcum = sum(len(p.get("price_history", [])) for p in PRODUCTS)
    await update.message.reply_text(
        f"📊 <b>PricePulse v4 — İstatistikler</b>\n\n"
        f"📦 Toplam takip: {len(PRODUCTS)}\n"
        f"✅ Aktif: {aktif}\n"
        f"🔗 Link takibi: {links}\n"
        f"🔍 Anahtar kelime: {kws}\n"
        f"🏪 Satıcı takibi: {sellers}\n"
        f"📈 Toplam fiyat ölçümü: {olcum}\n\n"
        f"⏱ Tarama: her {SETTINGS['check_every']} dk\n"
        f"🔕 Cooldown: {SETTINGS['alert_cooldown']} dk",
        parse_mode="HTML"
    )


async def cmd_log(update, context):
    lines = get_last_log(20)
    await update.message.reply_text(f"📋 <b>Son Loglar</b>\n\n<pre>{lines}</pre>", parse_mode="HTML")


async def cmd_ayarlar(update, context):
    s = SETTINGS
    await update.message.reply_text(
        f"⚙️ <b>Ayarlar</b>\n\n"
        f"<b>check_every</b>  → {s['check_every']} dk\n"
        f"<b>alert_cooldown</b> → {s['alert_cooldown']} dk\n"
        f"<b>daily_report</b>  → {s['daily_report']}\n"
        f"<b>silent_mode</b>   → {s['silent_mode']}\n"
        f"<b>max_keyword_results</b> → {s.get('max_keyword_results', 5)}\n\n"
        "Değiştirmek için ayar adını yaz:\n/iptal",
        parse_mode="HTML"
    )
    return ASK_AYAR_KEY


async def ayar_key_gir(update, context):
    key = update.message.text.strip().lower()
    valid = ["check_every", "alert_cooldown", "daily_report", "silent_mode", "silent_start", "silent_end", "max_keyword_results"]
    if key not in valid:
        await update.message.reply_text(f"❌ Geçersiz. Şunlardan biri: {', '.join(valid)}")
        return ASK_AYAR_KEY
    context.user_data["ayar_key"] = key
    await update.message.reply_text(f"Yeni değer? ({key})")
    return ASK_AYAR_VAL


async def ayar_val_gir(update, context):
    key = context.user_data.get("ayar_key")
    val = update.message.text.strip()
    try:
        if key in ["check_every", "alert_cooldown", "max_keyword_results"]:
            SETTINGS[key] = int(val)
        elif key == "silent_mode":
            SETTINGS[key] = val.lower() in ("true", "evet", "1", "açık")
        else:
            SETTINGS[key] = val
        save_settings(SETTINGS)
        context.user_data.clear()
        await update.message.reply_text(f"✅ <b>{key}</b> → <b>{SETTINGS[key]}</b>", parse_mode="HTML")
    except BaseException:
        await update.message.reply_text("❌ Geçersiz değer.")
    return ConversationHandler.END


async def cmd_yardim(update, context):
    await update.message.reply_text(
        "⚡️ <b>PricePulse v4 — Yardım</b>\n\n"
        "📦 <b>Takip Türleri</b>\n"
        "🔗 Link      — Ürün sayfasını direkt izle\n"
        "🔍 Kelime    — Arama sonuçlarında eşleşme ara\n"
        "🏪 Satıcı   — Mağazayı izle, yeni ürün/fiyat gelince bildir\n\n"
        "📋 <b>Komutlar</b>\n"
        "/ekle /sil /liste /kopyala /not\n"
        "/fiyat /hedef /ara /kontrol /en_ucuz\n"
        "/rapor /istatistik /platform /log\n"
        "/durdurAll /baslatAll /ayarlar",
        parse_mode="HTML"
    )

# ════════════════════════════════════════════════════════
#  🔁  CALLBACK HANDLER
# ════════════════════════════════════════════════════════


async def callback_handler(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("_")
    action = parts[0]

    if action == "fiyatsor":
        p = next((x for x in PRODUCTS if x["id"] == parts[1]), None)
        if not p:
            await query.edit_message_text("❌ Bulunamadı.")
            return
        await query.edit_message_text(f"🔍 <b>{p['name']}</b> sorgulanıyor...", parse_mode="HTML")
        price, in_stock = fetch_price_link(p)
        if price is None:
            await query.edit_message_text("⚠️ Fiyat alınamadı.", parse_mode="HTML")
            return
        record_price(p, price)
        diff = price - p["target_price"]
        ok = f"✅ Hedefe ulaştı! {fmt(abs(diff))} ucuz" if diff <= 0 else f"📍 Hedefe {fmt(diff)} uzak"
        await query.edit_message_text(
            f"💰 <b>{p['name']}</b>\nŞu an: <b>{fmt(price)}</b>\nHedef: {fmt(p['target_price'])}\n{ok}\n{'✅ Stokta' if in_stock else '⚠️ Stok belirsiz'}",
            parse_mode="HTML")
        return

    if action == "delete":
        p = next((x for x in PRODUCTS if x["id"] == parts[1]), None)
        if p:
            PRODUCTS.remove(p)
            save_products(PRODUCTS)
            await query.edit_message_text(f"🗑 <b>{p.get('keyword') or p.get('seller_name') or p.get('name')}</b> silindi.", parse_mode="HTML")
        return

    if action == "history":
        p = next((x for x in PRODUCTS if x["id"] == parts[1]), None)
        if not p:
            await query.edit_message_text("❌ Bulunamadı.")
            return
        h = p.get("price_history", [])
        if not h:
            await query.edit_message_text("📊 Veri yok.")
            return
        lines = [f"📊 <b>{p.get('name')} — Geçmiş</b>\n"]
        for e in h[-10:][::-1]:
            lines.append(f"  {e['date']} → {fmt(e['price'])}")
        low, high = lowest_price(p), highest_price(p)
        if low:
            lines.append(f"\n🏆 En düşük: {fmt(low)}")
        if high:
            lines.append(f"📈 En yüksek: {fmt(high)}")
        await query.edit_message_text("\n".join(lines), parse_mode="HTML")
        return

    uid_val = parts[-1]
    p = next((x for x in PRODUCTS if x["id"] == uid_val), None)
    if not p:
        await query.edit_message_text("❌ Bulunamadı.")
        return

    if action == "pause":
        mins = int(parts[1])
        p["paused_until"] = time.time() + mins * 60
        label = "1 gün" if mins == 1440 else f"{mins // 60} saat" if mins >= 60 else f"{mins}dk"
        save_products(PRODUCTS)
        await query.edit_message_text(f"⏸ {label} duraklatıldı.", parse_mode="HTML")
    elif action == "stop":
        p["active"] = False
        save_products(PRODUCTS)
        await query.edit_message_text("❌ Takip durduruldu.", parse_mode="HTML")
    elif action == "resume":
        p["active"] = True
        p["paused_until"] = None
        save_products(PRODUCTS)
        await query.edit_message_text("✅ Takip devam ediyor!", parse_mode="HTML")

# ════════════════════════════════════════════════════════
#  🔍  KONTROL MOTORU — 3 tür için
# ════════════════════════════════════════════════════════


async def check_link(app, product):
    price, in_stock = fetch_price_link(product)
    if price is None:
        return
    record_price(product, price)
    log.info(f"  🔗 {product['name']}: {fmt(price)} (hedef: {fmt(product['target_price'])})")
    if price > product["target_price"]:
        return
    last = product.get("last_alerted")
    if last and (time.time() - last) / 60 < SETTINGS.get("alert_cooldown", ALERT_COOLDOWN):
        return
    if is_silent():
        return
    text, kb = build_alert_link(product, price, in_stock)
    await app.bot.send_message(chat_id=CHAT_ID, text=text, reply_markup=kb, parse_mode="HTML")
    product["last_alerted"] = time.time()
    save_products(PRODUCTS)
    log.info(f"  ✅ Link alarm: {product['name']}")


async def check_keyword(app, product):
    limit = SETTINGS.get("max_keyword_results", 5)
    results = do_keyword_search(product["platform"], product["keyword"], limit)
    seen = set(product.get("seen_urls", []))
    alerted = False

    for item in results:
        if item["price"] > product["target_price"]:
            continue
        if item["url"] in seen:
            continue  # zaten bildirildi

        # Cooldown
        last = product.get("last_alerted")
        if last and (time.time() - last) / 60 < SETTINGS.get("alert_cooldown", ALERT_COOLDOWN):
            continue
        if is_silent():
            continue

        log.info(f"  🔍 Eşleşme: {item['name'][:40]} — {fmt(item['price'])}")
        text, kb = build_alert_keyword(product, item)
        await app.bot.send_message(chat_id=CHAT_ID, text=text, reply_markup=kb, parse_mode="HTML")
        seen.add(item["url"])
        product["last_alerted"] = time.time()
        alerted = True

    if alerted:
        product["seen_urls"] = list(seen)[-100:]  # son 100 url tut
        save_products(PRODUCTS)


async def check_seller(app, product):
    items = do_seller_scrape(product["platform"], product["seller_url"])
    seen = set(product.get("seen_urls", []))
    alerted = False
    keyword = product.get("seller_keyword", "").strip().lower()

    for item in items:
        # Keyword filtresi — boşsa tüm ürünler geçer
        if keyword and keyword not in item["name"].lower():
            continue

        is_new = item["url"] not in seen
        is_cheap = item["price"] <= product["target_price"] or product["target_price"] == 0

        if not (is_new or is_cheap):
            continue
        if not is_new and not is_cheap:
            continue

        last = product.get("last_alerted")
        if last and (time.time() - last) / 60 < SETTINGS.get("alert_cooldown", ALERT_COOLDOWN):
            continue
        if is_silent():
            continue

        kw_log = f" [filtre: {keyword}]" if keyword else ""
        log.info(f"  🏪 Satıcı hit{kw_log}: {item['name'][:40]} — {fmt(item['price'])}")
        text, kb = build_alert_seller(product, item)
        await app.bot.send_message(chat_id=CHAT_ID, text=text, reply_markup=kb, parse_mode="HTML")
        seen.add(item["url"])
        product["last_alerted"] = time.time()
        alerted = True

    if alerted:
        product["seen_urls"] = list(seen)[-200:]
        save_products(PRODUCTS)


async def run_price_check(app):
    log.info(f"Tarama — {datetime.now().strftime('%H:%M:%S')}")
    for product in PRODUCTS:
        if not product.get("active", True):
            continue
        pa = product.get("paused_until")
        if pa and time.time() < pa:
            continue
        try:
            t = product.get("type", "link")
            if t == "link":
                await check_link(app, product)
            elif t == "keyword":
                await check_keyword(app, product)
            elif t == "seller":
                await check_seller(app, product)
        except Exception as e:
            log.error(f"  ❌ {product.get('name')}: {e}")

# ════════════════════════════════════════════════════════
#  📅  SCHEDULER
# ════════════════════════════════════════════════════════


def start_scheduler(app):
    def run(coro):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(coro)

    async def daily():
        try:
            await app.bot.send_message(chat_id=CHAT_ID, text=build_daily_report(), parse_mode="HTML")
        except Exception as e:
            log.error(f"Rapor hata: {e}")
    schedule.every(SETTINGS.get("check_every", CHECK_EVERY)).minutes.do(lambda: run(run_price_check(app)))
    schedule.every().day.at(SETTINGS.get("daily_report", DAILY_REPORT)).do(lambda: run(daily()))

    def loop():
        while True:
            schedule.run_pending()
            time.sleep(10)
    threading.Thread(target=loop, daemon=True).start()
    log.info("⏱ Scheduler aktif")

# ════════════════════════════════════════════════════════
#  🚀  MAIN
# ════════════════════════════════════════════════════════


def main():
    print("⚡️ PricePulse v4 başlatılıyor...")
    print(f"   • {len(PRODUCTS)} takip yüklendi")
    print(f"   • Tarama: her {SETTINGS['check_every']} dakika")
    print(f"   • Günlük rapor: {SETTINGS['daily_report']}\n")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_ekle = ConversationHandler(
        entry_points=[CommandHandler("ekle", cmd_ekle)],
        states={
            ASK_TYPE: [CallbackQueryHandler(ekle_type_sec, pattern=r"^ekle_")],
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ekle_name)],
            ASK_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ekle_url)],
            ASK_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ekle_price)],
            ASK_KW_PLATFORM: [CallbackQueryHandler(kw_platform_sec, pattern=r"^kw_platform_")],
            ASK_KW_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, kw_query_gir)],
            ASK_KW_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, kw_price_gir)],
            ASK_SELLER_PLATFORM: [CallbackQueryHandler(seller_platform_sec, pattern=r"^seller_platform_")],
            ASK_SELLER_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, seller_url_gir)],
            ASK_SELLER_KEYWORD: [
                CallbackQueryHandler(seller_keyword_sec, pattern=r"^seller_kw_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, seller_keyword_gir),
            ],
            ASK_SELLER_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, seller_price_gir)],
        },
        fallbacks=[CommandHandler("iptal", conv_iptal)],
    )

    conv_ayarlar = ConversationHandler(
        entry_points=[CommandHandler("ayarlar", cmd_ayarlar)],
        states={
            ASK_AYAR_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ayar_key_gir)],
            ASK_AYAR_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ayar_val_gir)],
        },
        fallbacks=[CommandHandler("iptal", conv_iptal)],
    )

    app.add_handler(conv_ekle)
    app.add_handler(conv_ayarlar)

    for cmd, fn in [
        ("start", "cmd_start"), ("liste", "cmd_liste"), ("fiyat", "cmd_fiyat"),
        ("ara", "cmd_ara"), ("en_ucuz", "cmd_en_ucuz"), ("platform", "cmd_platform"),
        ("durdurAll", "cmd_durdurAll"), ("baslatAll", "cmd_baslatAll"),
        ("kontrol", "cmd_kontrol"), ("rapor", "cmd_rapor"), ("sil", "cmd_sil"),
        ("istatistik", "cmd_istatistik"), ("log", "cmd_log"), ("yardim", "cmd_yardim"),
    ]:
        app.add_handler(CommandHandler(cmd, eval(fn)))

    app.add_handler(CallbackQueryHandler(callback_handler))

  import asyncio
from telegram.ext import Application
from telegram import Update

async def on_startup(app: Application):
    start_scheduler(app)
    await asyncio.sleep(3)
    await run_price_check(app)
    print("✅ Bot hazır. /start yaz.\n")

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.post_init = on_startup
    await app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    asyncio.run(main())
