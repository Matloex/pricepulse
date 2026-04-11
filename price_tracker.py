#!/usr/bin/env python3
"""
PricePulse Bot v6.1 — Upstash Redis ile Kalıcı Veri

Düzeltmeler & İyileştirmeler:
  1. Paralel tarama (asyncio.gather) — tüm ürünler aynı anda taranır
  2. parse_price tamamen yeniden yazıldı — regex tabanlı, hatasız
  3. Toplu kayıt (save sıklığı azaltıldı, hız arttı)
  4. make_uid UUID4 tabanlı — çakışma yok
  5. check_seller mantığı düzeltildi — yeni ürün her zaman bildirilir
  6. load_products log hatası düzeltildi
  7. _try_jsonld null-safe yapıldı
  8. Session reuse (requests.Session) — her istek yeni bağlantı açmıyor
  9. Bağlantı havuzu ve retry mekanizması
  10. Conversation state çakışmaları giderildi
  11. Cooldown: ürün bazında, satıcı için ayrı limit
  12. Amazon fiyat parse iyileştirildi

Kurulum:
    pip install requests beautifulsoup4 python-telegram-bot==20.7 schedule
"""

import asyncio
import copy
import json
import logging
import os
import re
import schedule
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler, ContextTypes,
)

# ════════════════════════════════════════════════════════
#  ⚙️  AYARLAR
# ════════════════════════════════════════════════════════

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN",  "8597080702:AAFY-udbqWDoTmuAJIr52GGmDl5DuSCTFoE")
CHAT_ID         = int(os.environ.get("CHAT_ID", "1003976351142"))
UPSTASH_URL     = os.environ.get("UPSTASH_URL",      "")
UPSTASH_TOKEN   = os.environ.get("UPSTASH_TOKEN",    "")
SCRAPER_KEY     = os.environ.get("SCRAPER_API_KEY",  "")  # scraperapi.com
CHECK_EVERY     = 1
ALERT_COOLDOWN  = 30
DAILY_REPORT    = "09:00"
MAX_PARALLEL    = 5

# Redis key isimleri
_KEY_PRODUCTS = "pricepulse:products"
_KEY_SETTINGS = "pricepulse:settings"

# ── Conversation states ─────────────────────────────────
(
    ASK_TYPE,
    ASK_LINK_NAME, ASK_LINK_URL, ASK_LINK_PRICE,
    ASK_KW_PLATFORM, ASK_KW_QUERY, ASK_KW_PRICE,
    ASK_SELLER_PLATFORM, ASK_SELLER_URL, ASK_SELLER_KEYWORD, ASK_SELLER_PRICE,
    ASK_HEDEF_SEC, ASK_HEDEF_FIYAT,
    ASK_NOT_SEC, ASK_NOT_METIN,
    ASK_KOPYALA_SEC, ASK_KOPYALA_FIYAT,
    ASK_AYAR_KEY, ASK_AYAR_VAL,
) = range(19)

# ════════════════════════════════════════════════════════
#  📝  LOGGING (en başta kurulmalı)
# ════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s · %(levelname)s · %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("pricepulse.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════
#  🗄️  VERİ YÖNETİMİ — Upstash Redis
# ════════════════════════════════════════════════════════

def _redis_get(key: str):
    """Redis'ten string değer oku. Upstash REST API."""
    try:
        r = requests.get(
            f"{UPSTASH_URL}/get/{key}",
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
            timeout=10
        )
        data = r.json()
        # Upstash cevabı: {"result": "değer"} veya {"result": null}
        result = data.get("result")
        return result  # str veya None
    except Exception as e:
        log.error(f"Redis GET hatası [{key}]: {e}")
        return None

def _redis_set(key: str, value: str):
    """Redis'e string değer yaz. Upstash REST API."""
    try:
        # Upstash SET komutu: POST /set/KEY body=VALUE
        r = requests.post(
            f"{UPSTASH_URL}/set/{key}",
            headers={
                "Authorization": f"Bearer {UPSTASH_TOKEN}",
                "Content-Type": "text/plain",
            },
            data=value.encode("utf-8"),
            timeout=10
        )
        data = r.json()
        if data.get("result") != "OK":
            log.error(f"Redis SET başarısız [{key}]: {data}")
    except Exception as e:
        log.error(f"Redis SET hatası [{key}]: {e}")

def load_products() -> list:
    try:
        raw = _redis_get(_KEY_PRODUCTS)
        if raw:
            parsed = json.loads(raw)
            # Güvenlik: gerçekten liste mi?
            if isinstance(parsed, list):
                log.info(f"Redis'ten {len(parsed)} ürün yüklendi.")
                return parsed
            log.error(f"Redis products beklenen liste değil: {type(parsed)}")
    except Exception as e:
        log.error(f"Ürün yükleme hatası: {e}")
    return []

def save_products(products: list):
    try:
        _redis_set(_KEY_PRODUCTS, json.dumps(products, ensure_ascii=False))
    except Exception as e:
        log.error(f"Ürün kaydetme hatası: {e}")

def load_settings() -> dict:
    defaults = {
        "check_every":          CHECK_EVERY,
        "alert_cooldown":       ALERT_COOLDOWN,
        "daily_report":         DAILY_REPORT,
        "silent_start":         "23:00",
        "silent_end":           "08:00",
        "silent_mode":          False,
        "max_keyword_results":  5,
    }
    try:
        raw = _redis_get(_KEY_SETTINGS)
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                defaults.update(parsed)
    except Exception as e:
        log.error(f"Ayar yükleme hatası: {e}")
    return defaults

def save_settings(s: dict):
    try:
        _redis_set(_KEY_SETTINGS, json.dumps(s, ensure_ascii=False))
    except Exception as e:
        log.error(f"Ayar kaydetme hatası: {e}")

PRODUCTS: list = load_products()
SETTINGS: dict = load_settings()
_scan_lock = threading.Lock()
_dirty     = False

def mark_dirty():
    global _dirty
    _dirty = True

def flush_if_dirty():
    global _dirty
    if _dirty:
        save_products(PRODUCTS)
        _dirty = False

# ════════════════════════════════════════════════════════
#  🌐  HTTP SESSION (bağlantı havuzu + retry)
# ════════════════════════════════════════════════════════

def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=20,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language":  "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept":           "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Encoding":  "gzip, deflate, br",
        "Cache-Control":    "no-cache",
        "Pragma":           "no-cache",
        "Sec-Ch-Ua":        '"Chromium";v="123", "Not:A-Brand";v="8"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest":   "document",
        "Sec-Fetch-Mode":   "navigate",
        "Sec-Fetch-Site":   "none",
        "Sec-Fetch-User":   "?1",
        "Upgrade-Insecure-Requests": "1",
    })
    return s

SESSION = _make_session()

# ════════════════════════════════════════════════════════
#  🔧  YARDIMCI FONKSİYONLAR
# ════════════════════════════════════════════════════════

def fmt(p: float) -> str:
    return f"₺{p:,.0f}"

def make_uid() -> str:
    return str(uuid.uuid4())[:12]

def now_str() -> str:
    return datetime.now().strftime("%H:%M")

def date_str() -> str:
    return datetime.now().strftime("%d.%m.%Y")

def find_product(pid: str):
    return next((p for p in PRODUCTS if p["id"] == pid), None)

def label_of(p: dict) -> str:
    return (p.get("keyword") or p.get("seller_name") or p.get("name", "?"))[:40]

def platform_from_url(url: str) -> str:
    u = url.lower()
    if "trendyol.com" in u or "ty.gl" in u:          return "trendyol"
    if "hepsiburada.com" in u or "app.hb.biz" in u:  return "hepsiburada"
    if "n11.com"         in u:                         return "n11"
    if any(x in u for x in [
        "amazon.com.tr", "amazon.de", "amazon.com", "amazon.co.uk",
        "amazon.fr", "amazon.it", "amazon.es", "amazon.nl",
        "amzn.to", "amzn.eu", "amzn.com", "a.co",
    ]): return "amazon"
    return "diger"

def normalize_amazon(url: str) -> str:
    """Her Amazon linkini amazon.com.tr/dp/ASIN formatına çevir."""
    for pat in [
        r'/dp/([A-Z0-9]{10})',
        r'/gp/product/([A-Z0-9]{10})',
        r'/d/([A-Z0-9]{10})',
        r'asin=([A-Z0-9]{10})',
    ]:
        m = re.search(pat, url, re.IGNORECASE)
        if m:
            return f"https://www.amazon.com.tr/dp/{m.group(1).upper()}"
    return url

def _resolve_short_url(url: str) -> str:
    """Kısa linki redirect takip ederek gerçek URL'ye çevir."""
    try:
        r = SESSION.get(url, allow_redirects=True, timeout=10,
                        headers={"Accept-Encoding": "gzip, deflate"})
        log.info(f"Kısa link: {url[:40]} → {r.url[:60]}")
        return r.url
    except Exception as e:
        log.warning(f"Kısa link çözülemedi [{url}]: {e}")
        # amzn.eu/d/ASIN formatından ASIN çekmeyi dene
        m = re.search(r'/d/([A-Z0-9]{10})', url, re.IGNORECASE)
        if m:
            return f"https://www.amazon.com.tr/dp/{m.group(1).upper()}"
        return url

def normalize_url(raw: str) -> str:
    url = raw.strip()
    if not url.startswith("http"):
        url = "https://" + url
    u = url.lower()
    # Kısa linkler — redirect takip et
    if any(x in u for x in ["ty.gl", "app.hb.biz", "amzn.to", "amzn.eu", "a.co/"]):
        url = _resolve_short_url(url)
    # Amazon URL'ini normalize et
    if platform_from_url(url) == "amazon":
        url = normalize_amazon(url)
    return url

def is_valid_url(url: str) -> bool:
    try:
        r = urlparse(url)
        return r.scheme in ("http", "https") and bool(r.netloc)
    except:
        return False

def is_silent_now() -> bool:
    if not SETTINGS.get("silent_mode"):
        return False
    try:
        now   = datetime.now()
        start = datetime.strptime(SETTINGS["silent_start"], "%H:%M").replace(
            year=now.year, month=now.month, day=now.day)
        end   = datetime.strptime(SETTINGS["silent_end"], "%H:%M").replace(
            year=now.year, month=now.month, day=now.day)
        return (now >= start or now <= end) if start > end else (start <= now <= end)
    except:
        return False

def current_price(p: dict):
    h = p.get("price_history", [])
    return h[-1]["price"] if h else None

def lowest_price(p: dict):
    h = p.get("price_history", [])
    return min((x["price"] for x in h), default=None)

def highest_price(p: dict):
    h = p.get("price_history", [])
    return max((x["price"] for x in h), default=None)

def record_price(product: dict, price: float):
    """Fiyatı geçmişe ekle. Kayıt dirty flag ile ertelenmiş."""
    entry = {"price": price, "date": datetime.now().strftime("%Y-%m-%d %H:%M")}
    h = product.setdefault("price_history", [])
    if not h or abs(h[-1]["price"] - price) > 0.01:
        h.append(entry)
    product["price_history"] = h[-50:]
    mark_dirty()

def price_trend(product: dict) -> str:
    h = product.get("price_history", [])
    if len(h) < 2:
        return "📊 Yeni takipte"
    prices = [x["price"] for x in h[-5:]]
    if prices[-1] < prices[0]:
        return f"📉 {fmt(prices[0] - prices[-1])} düştü"
    elif prices[-1] > prices[0]:
        return f"📈 {fmt(prices[-1] - prices[0])} yükseldi"
    return "➡️ Stabil"

def parse_price(text) -> float | None:
    """
    Fiyat metnini float'a çevir.
    Desteklenen formatlar: 1.234,56 · 1,234.56 · 1234.56 · 1234,56 · 1234
    """
    if text is None:
        return None
    t = str(text).strip()
    # Para birimi sembollerini kaldır
    t = re.sub(r'[₺$€£\xA0\s]', '', t)
    t = re.sub(r'(?i)(TL|TRY|USD|EUR)', '', t).strip()
    if not t:
        return None

    # Türkçe format: 1.234,56 veya 1.234
    if re.fullmatch(r'\d{1,3}(\.\d{3})+(,\d{1,2})?', t):
        t = t.replace('.', '').replace(',', '.')
    # İngilizce format: 1,234.56 veya 1,234
    elif re.fullmatch(r'\d{1,3}(,\d{3})+(\.\d{1,2})?', t):
        t = t.replace(',', '')
    # Virgüllü ondalık (Türkçe kısa): 1234,56
    elif re.fullmatch(r'\d+(,\d{1,2})', t):
        t = t.replace(',', '.')
    # Noktalı ondalık (İngilizce kısa): 1234.56
    # else: zaten doğru format

    try:
        v = float(re.sub(r'[^\d.]', '', t))
        return v if v > 0 else None
    except:
        return None

def get_last_log(n: int = 25) -> str:
    try:
        with open("pricepulse.log", "r", encoding="utf-8") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except:
        return "Log dosyası bulunamadı."

# ════════════════════════════════════════════════════════
#  💰  FİYAT ÇEKİCİLER
# ════════════════════════════════════════════════════════

def _try_jsonld(soup) -> float | None:
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            raw = sc.string
            if not raw:
                continue
            data = json.loads(raw)
            if isinstance(data, list):
                data = data[0] if data else {}
            offers = data.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = offers.get("price")
            if price is not None:
                p = parse_price(price)
                if p:
                    return p
        except:
            continue
    return None

def _decode_response(r) -> str:
    """Response içeriğini güvenli şekilde string'e çevir."""
    try:
        return r.content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return r.content.decode("latin-1")
        except:
            return r.text

def scraper_get(url: str, timeout: int = 20) -> requests.Response:
    """
    ScraperAPI üzerinden istek at.
    SCRAPER_API_KEY varsa ScraperAPI kullan (bot korumasını aşar),
    yoksa direkt istek at.
    """
    if SCRAPER_KEY:
        api_url = "https://api.scraperapi.com/"
        params = {
            "api_key": SCRAPER_KEY,
            "url": url,
            "render": "false",   # JS render istemiyoruz, sadece HTML lazım
            "country_code": "tr",
        }
        return SESSION.get(api_url, params=params, timeout=timeout)
    else:
        headers = dict(SESSION.headers)
        headers["Accept-Encoding"] = "gzip, deflate"
        return SESSION.get(url, headers=headers, timeout=timeout)

def get_price_trendyol(url: str):
    try:
        r    = scraper_get(url)
        html = _decode_response(r)
        log.info(f"  Trendyol HTTP {r.status_code} [{url[:55]}]")
        if r.status_code != 200:
            return None, True
        soup     = BeautifulSoup(html, "html.parser")
        in_stock = not bool(soup.select_one(".sold-out-badge, .out-of-stock"))
        for pat in [
            r'"discountedPrice"\s*:\s*([\d]+(?:\.\d+)?)',
            r'"sellingPrice"\s*:\s*([\d]+(?:\.\d+)?)',
            r'"originalPrice"\s*:\s*([\d]+(?:\.\d+)?)',
            r'"price"\s*:\s*([\d]+(?:\.\d+)?)',
        ]:
            m = re.search(pat, html)
            if m:
                p = parse_price(m.group(1))
                if p and 10 < p < 1_000_000:
                    log.info(f"  Trendyol fiyat: {p}")
                    return p, in_stock
        p = _try_jsonld(soup)
        if p: return p, in_stock
        for sel in [".prc-dsc", ".pr-bx-pr-dsc span", ".prc-slg"]:
            el = soup.select_one(sel)
            if el:
                p = parse_price(el.get_text())
                if p and 10 < p < 1_000_000:
                    return p, in_stock
        for raw in re.findall(r'([\d]{2,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)\s*(?:TL|₺)', html):
            p = parse_price(raw)
            if p and 10 < p < 1_000_000:
                return p, in_stock
        log.warning(f"  Trendyol fiyat yok | {soup.title.string[:50] if soup.title else 'yok'}")
    except Exception as e:
        log.warning(f"  Trendyol hata: {e}")
    return None, True


def get_price_hepsiburada(url: str):
    try:
        r    = scraper_get(url)
        html = _decode_response(r)
        log.info(f"  Hepsiburada HTTP {r.status_code} [{url[:55]}]")
        if r.status_code != 200:
            return None, True
        soup     = BeautifulSoup(html, "html.parser")
        in_stock = not bool(soup.select_one(".out-of-stock, .tezgah-out-of-stock"))
        el = soup.select_one('[itemprop="price"]')
        if el:
            p = parse_price(el.get("content") or el.get_text())
            if p and 10 < p < 1_000_000:
                log.info(f"  Hepsiburada fiyat: {p}")
                return p, in_stock
        p = _try_jsonld(soup)
        if p and 10 < p < 1_000_000:
            return p, in_stock
        for pat in [
            r'"salePrice"\s*:\s*([\d]+(?:[.,]\d+)?)',
            r'"currentPrice"\s*:\s*([\d]+(?:[.,]\d+)?)',
            r'"price"\s*:\s*([\d]+(?:[.,]\d+)?)',
            r'data-price="([\d]+(?:[.,]\d+)?)"',
        ]:
            m = re.search(pat, html)
            if m:
                p = parse_price(m.group(1))
                if p and 10 < p < 1_000_000:
                    log.info(f"  Hepsiburada fiyat: {p}")
                    return p, in_stock
        for sel in [".price-value", "[class*='currentPrice']", ".product-price"]:
            el = soup.select_one(sel)
            if el:
                p = parse_price(el.get_text())
                if p and 10 < p < 1_000_000:
                    return p, in_stock
        log.warning(f"  Hepsiburada fiyat yok | {soup.title.string[:50] if soup.title else 'yok'}")
    except Exception as e:
        log.warning(f"  Hepsiburada hata: {e}")
    return None, True


def get_price_amazon(url: str):
    try:
        url  = normalize_amazon(url)
        r    = scraper_get(url)
        html = _decode_response(r)
        log.info(f"  Amazon HTTP {r.status_code} [{url[:55]}]")
        if r.status_code != 200:
            return None, True
        soup     = BeautifulSoup(html, "html.parser")
        in_stock = ("Stokta yok" not in html and "currently unavailable" not in html.lower())
        for sel in [
            "#corePriceDisplay_desktop_feature_div .a-price-whole",
            "#corePriceDisplay_desktop_feature_div .a-offscreen",
            ".a-price.aok-align-center .a-offscreen",
            "#priceblock_ourprice", "#priceblock_dealprice", ".a-price-whole",
        ]:
            el = soup.select_one(sel)
            if el:
                p = parse_price(el.get_text())
                if p and 10 < p < 1_000_000:
                    frac = soup.select_one(".a-price-fraction")
                    if frac:
                        try:
                            fv = parse_price(frac.get_text())
                            if fv and fv < 100: p += fv / 100
                        except: pass
                    log.info(f"  Amazon fiyat: {p}")
                    return p, in_stock
        p = _try_jsonld(soup)
        if p: return p, in_stock
        log.warning(f"  Amazon fiyat yok | {soup.title.string[:50] if soup.title else 'yok'}")
    except Exception as e:
        log.warning(f"  Amazon hata: {e}")
    return None, True


def get_price_n11(url: str):
    try:
        r    = scraper_get(url)
        html = _decode_response(r)
        log.info(f"  N11 HTTP {r.status_code} [{url[:55]}]")
        if r.status_code != 200:
            return None, True
        soup = BeautifulSoup(html, "html.parser")
        el   = soup.select_one('[itemprop="price"]')
        if el:
            p = parse_price(el.get("content") or el.get_text())
            if p and 10 < p < 1_000_000:
                return p, True
        for sel in [".newPrice ins", ".price ins", ".prodPrice", ".fiyat"]:
            el = soup.select_one(sel)
            if el:
                p = parse_price(el.get_text())
                if p and 10 < p < 1_000_000:
                    return p, True
        log.warning(f"  N11 fiyat yok")
    except Exception as e:
        log.warning(f"  N11 hata: {e}")
    return None, True
def fetch_price(product: dict):
    url  = product.get("url", "")
    plat = platform_from_url(url) if url else product.get("platform", "").lower()
    if "trendyol"    in plat: return get_price_trendyol(url)
    if "hepsiburada" in plat: return get_price_hepsiburada(url)
    if "amazon"      in plat: return get_price_amazon(url)
    if "n11"         in plat: return get_price_n11(url)
    return None, True

# ════════════════════════════════════════════════════════
#  🔍  ARAMA & SATICI TARAMA
# ════════════════════════════════════════════════════════

def _search_url(platform: str, query: str) -> str:
    q = requests.utils.quote(query)
    return {
        "trendyol":    f"https://www.trendyol.com/sr?q={q}",
        "hepsiburada": f"https://www.hepsiburada.com/ara?q={q}",
        "amazon":      f"https://www.amazon.com.tr/s?k={q}",
        "n11":         f"https://www.n11.com/arama?q={q}",
    }.get(platform.lower(), f"https://www.trendyol.com/sr?q={q}")

def _parse_cards_trendyol(soup, base_url: str) -> list:
    out = []
    for card in soup.select(".p-card-wrppr, .product-card"):
        try:
            name_el  = card.select_one(".prdct-desc-cntnr-name, h3")
            price_el = card.select_one(".prc-box-dscntd, .prc-box-sllng, .prc-dsc")
            link_el  = card.select_one("a[href]")
            if not (name_el and price_el): continue
            p = parse_price(price_el.get_text())
            if not p: continue
            href = link_el["href"] if link_el else ""
            link = ("https://www.trendyol.com" + href) if href.startswith("/") else (href or base_url)
            out.append({"name": name_el.get_text(strip=True)[:80], "price": p, "url": link})
        except: continue
    return out

def _parse_cards_hb(soup, base_url: str) -> list:
    out = []
    for card in soup.select("[data-test-id='product-card-wrapper'], li[class*='product']"):
        try:
            name_el  = card.select_one("[data-test-id='product-card-name'], .product-name, h3")
            price_el = card.select_one("[data-test-id='price-current-price'], .price, [class*='currentPrice']")
            link_el  = card.select_one("a[href]")
            if not (name_el and price_el): continue
            p = parse_price(price_el.get_text())
            if not p: continue
            href = link_el["href"] if link_el else ""
            link = ("https://www.hepsiburada.com" + href) if href.startswith("/") else (href or base_url)
            out.append({"name": name_el.get_text(strip=True)[:80], "price": p, "url": link})
        except: continue
    return out

def _parse_cards_amazon(soup, base_url: str) -> list:
    out = []
    for card in soup.select("[data-component-type='s-search-result']"):
        try:
            name_el  = card.select_one("h2 span")
            whole_el = card.select_one(".a-price-whole")
            link_el  = card.select_one("h2 a[href]")
            if not (name_el and whole_el): continue
            p = parse_price(whole_el.get_text())
            if not p: continue
            href = link_el["href"] if link_el else ""
            link = ("https://www.amazon.com.tr" + href) if href.startswith("/") else (href or base_url)
            out.append({"name": name_el.get_text(strip=True)[:80], "price": p, "url": link})
        except: continue
    return out

def _parse_cards_n11(soup, base_url: str) -> list:
    out = []
    for card in soup.select(".column.product, .columnContent .column, li[class*='product']"):
        try:
            name_el  = card.select_one(".productName, h3")
            price_el = card.select_one(".newPrice ins, .price ins, .prodPrice")
            link_el  = card.select_one("a[href]")
            if not (name_el and price_el): continue
            p = parse_price(price_el.get_text())
            if not p: continue
            link = link_el["href"] if link_el else base_url
            out.append({"name": name_el.get_text(strip=True)[:80], "price": p, "url": link})
        except: continue
    return out

def _fetch_and_parse(url: str, platform: str) -> list:
    try:
        r    = SESSION.get(url, timeout=15)
        soup = BeautifulSoup(r.content, "html.parser")
        p    = platform.lower()
        if "trendyol"    in p: return _parse_cards_trendyol(soup, url)
        if "hepsiburada" in p: return _parse_cards_hb(soup, url)
        if "amazon"      in p: return _parse_cards_amazon(soup, url)
        if "n11"         in p: return _parse_cards_n11(soup, url)
    except Exception as e:
        log.warning(f"Arama/satıcı hatası [{platform}|{url[:50]}]: {e}")
    return []

def do_keyword_search(platform: str, query: str, limit: int = 5) -> list:
    url = _search_url(platform.lower(), query)
    return _fetch_and_parse(url, platform)[:limit]

def do_seller_scrape(platform: str, seller_url: str, limit: int = 10) -> list:
    # Amazon satıcı profil → ürün listesi
    if "amazon" in platform.lower():
        m = re.search(r'seller=([A-Z0-9]+)', seller_url)
        if m:
            seller_url = f"https://www.amazon.com.tr/s?me={m.group(1)}"
    return _fetch_and_parse(seller_url, platform)[:limit]

# ════════════════════════════════════════════════════════
#  📲  MESAJ OLUŞTURUCULARI
# ════════════════════════════════════════════════════════

def _std_kb(uid_val: str, buy_url: str = "") -> InlineKeyboardMarkup:
    rows = []
    if buy_url:
        rows.append([InlineKeyboardButton("🛒 Satın Al →", url=buy_url)])
    rows.append([
        InlineKeyboardButton("⏸ 1 Saat",  callback_data=f"pause_60_{uid_val}"),
        InlineKeyboardButton("⏸ 3 Saat",  callback_data=f"pause_180_{uid_val}"),
        InlineKeyboardButton("⏸ Yarın",   callback_data=f"pause_1440_{uid_val}"),
    ])
    rows.append([
        InlineKeyboardButton("❌ Durdur",  callback_data=f"stop_{uid_val}"),
        InlineKeyboardButton("📊 Geçmiş", callback_data=f"history_{uid_val}"),
        InlineKeyboardButton("✅ Devam",  callback_data=f"resume_{uid_val}"),
    ])
    return InlineKeyboardMarkup(rows)

def build_alert_link(product: dict, price: float, in_stock: bool):
    diff     = product["target_price"] - price
    pct      = round(diff / product["target_price"] * 100, 1)
    low      = lowest_price(product)
    low_flag = "🏆 <b>Tüm zamanların en ucuzu!</b>\n" if (low and price <= low + 0.01) else ""
    note     = f"\n📝 <i>{product['note']}</i>" if product.get("note") else ""
    text = (
        f"⚡️ <b>FİYAT ALARMI — PricePulse</b>\n\n"
        f"📦 <b>{product['name']}</b>{note}\n"
        f"🏪 {product.get('platform','?').capitalize()}\n\n"
        f"💰 Şu an: <b>{fmt(price)}</b>\n"
        f"🎯 Hedef: {fmt(product['target_price'])}\n"
        f"📉 <b>{fmt(diff)} ({pct}%) daha ucuz!</b>\n"
        f"{low_flag}"
        f"{price_trend(product)}\n"
        f"{'✅ Stokta' if in_stock else '⚠️ Stok belirsiz'}\n\n"
        f"⏰ {now_str()}'de tespit edildi"
    )
    return text, _std_kb(product["id"], product.get("url", ""))

def build_alert_keyword(product: dict, item: dict):
    diff = product["target_price"] - item["price"]
    pct  = round(diff / product["target_price"] * 100, 1)
    text = (
        f"🔍 <b>ANAHTAR KELİME EŞLEŞTİ — PricePulse</b>\n\n"
        f"🔎 Arama: <b>\"{product['keyword']}\"</b>\n"
        f"🏪 {product.get('platform','?').capitalize()}\n\n"
        f"📦 <b>{item['name'][:60]}</b>\n"
        f"💰 Fiyat: <b>{fmt(item['price'])}</b>\n"
        f"🎯 Hedefin: {fmt(product['target_price'])}\n"
        f"📉 <b>{fmt(diff)} ({pct}%) ucuz!</b>\n\n"
        f"⏰ {now_str()}'de bulundu"
    )
    return text, _std_kb(product["id"], item["url"])

def build_alert_seller(product: dict, item: dict, is_new: bool):
    is_cheap = item["price"] <= product["target_price"] or product["target_price"] == 0
    header   = "🆕 SATICIDE YENİ ÜRÜN" if is_new and not is_cheap else "💰 SATICI FİYAT ALARMI"
    diff     = product["target_price"] - item["price"]
    pct      = round(diff / product["target_price"] * 100, 1) if product["target_price"] > 0 else 0
    text = (
        f"🏪 <b>{header} — PricePulse</b>\n\n"
        f"🏬 Satıcı: <b>{product.get('seller_name','?')}</b>\n"
        f"🏪 {product.get('platform','?').capitalize()}\n\n"
        f"📦 <b>{item['name'][:60]}</b>\n"
        f"💰 Fiyat: <b>{fmt(item['price'])}</b>\n"
        f"🎯 Hedefin: {fmt(product['target_price'])}\n"
        + (f"📉 <b>{fmt(diff)} ({pct}%) ucuz!</b>\n" if is_cheap else "") +
        f"\n⏰ {now_str()}'de tespit edildi"
    )
    return text, _std_kb(product["id"], item["url"])

def build_daily_report() -> str:
    if not PRODUCTS:
        return "📭 Takip listesi boş."
    aktif = [p for p in PRODUCTS if p.get("active")]
    icons = {"link": "🔗", "keyword": "🔍", "seller": "🏪"}
    lines = [
        "📊 <b>PricePulse — Günlük Rapor</b>",
        f"🗓 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        f"📦 {len(aktif)}/{len(PRODUCTS)} aktif",
        "━━━━━━━━━━━━━━━",
    ]
    for p in aktif:
        icon    = icons.get(p.get("type", "link"), "🔗")
        cur     = current_price(p)
        pa      = p.get("paused_until")
        durum   = "⏸" if pa and time.time() < pa else "✅"
        lbl     = label_of(p)
        cur_s   = f" | {fmt(cur)}" if cur else ""
        hit     = " 🎯" if cur and cur <= p["target_price"] else ""
        lines.append(
            f"\n{icon}{durum} <b>{lbl}</b>{hit}\n"
            f"   Hedef: {fmt(p['target_price'])}{cur_s} | {price_trend(p)}"
        )
    lines += ["", "━━━━━━━━━━━━━━━",
              f"⏱ Tarama: her {SETTINGS.get('check_every', 1)} dk"]
    return "\n".join(lines)

# ════════════════════════════════════════════════════════
#  ➕  /ekle CONVERSATION
# ════════════════════════════════════════════════════════

async def cmd_ekle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Direkt Ürün Linki",  callback_data="ekle_link")],
        [InlineKeyboardButton("🔍 Anahtar Kelime Ara", callback_data="ekle_keyword")],
        [InlineKeyboardButton("🏪 Satıcı Mağazası",   callback_data="ekle_seller")],
    ])
    await update.message.reply_text(
        "➕ <b>Takip türünü seç</b>\n\n"
        "🔗 <b>Link</b> — Ürün sayfasını izle, fiyat düşünce bildir\n"
        "🔍 <b>Kelime</b> — Platformda ara, hedef fiyat altında çıkınca bildir\n"
        "🏪 <b>Satıcı</b> — Mağazayı izle, yeni ürün/indirim gelince bildir\n\n"
        "/iptal",
        parse_mode="HTML", reply_markup=kb
    )
    return ASK_TYPE

async def ekle_type_sec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    t = q.data.replace("ekle_", "")

    if t == "link":
        await q.edit_message_text(
            "🔗 <b>Ürün Adı</b>\n\nÜrünün adını yaz:\n<i>örn: Sony WH-1000XM5</i>\n\n/iptal",
            parse_mode="HTML"
        )
        return ASK_LINK_NAME

    if t == "keyword":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(pl, callback_data=f"kw_pl_{pl}")]
            for pl in ["Trendyol", "Amazon TR", "Hepsiburada", "N11"]
        ])
        await q.edit_message_text("🔍 Platform seç:", reply_markup=kb)
        return ASK_KW_PLATFORM

    if t == "seller":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(pl, callback_data=f"seller_pl_{pl}")]
            for pl in ["Trendyol", "Amazon TR", "Hepsiburada"]
        ])
        await q.edit_message_text("🏪 Satıcının platformunu seç:", reply_markup=kb)
        return ASK_SELLER_PLATFORM

# ── Link ────────────────────────────────────────────────
async def link_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ <b>{context.user_data['name']}</b>\n\n"
        "Ürün linkini gönder:\n<i>Trendyol, Amazon, Hepsiburada, N11</i>\n\n/iptal",
        parse_mode="HTML"
    )
    return ASK_LINK_URL

async def link_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw  = update.message.text.strip()
    # normalize_url senkron HTTP yapıyor (kısa link resolve) — executor'da çalıştır
    url  = await asyncio.get_event_loop().run_in_executor(None, normalize_url, raw)
    plat = platform_from_url(url)

    if not is_valid_url(url) or plat == "diger":
        await update.message.reply_text(
            "❌ Link tanınamadı.\n"
            "Desteklenen: Trendyol (ty.gl), Amazon (amzn.to/eu, amazon.de/com/com.tr), "
            "Hepsiburada (app.hb.biz), N11\n\n"
            f"Gönderdiğin: <code>{raw[:80]}</code>\n\n/iptal",
            parse_mode="HTML"
        )
        return ASK_LINK_URL

    context.user_data["url"]  = url
    context.user_data["plat"] = plat
    disp = {"trendyol":"Trendyol","hepsiburada":"Hepsiburada","amazon":"Amazon TR","n11":"N11"}.get(plat, plat)
    changed = f"\n🔄 Çevrilen: <code>{url[:60]}</code>" if url != raw else ""
    await update.message.reply_text(
        f"✅ Platform: <b>{disp}</b>{changed}\n\nHedef fiyatı yaz:\n<i>örn: 3500</i>\n\n/iptal",
        parse_mode="HTML"
    )
    return ASK_LINK_PRICE

async def link_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = parse_price(update.message.text)
    if not price:
        await update.message.reply_text("❌ Geçersiz fiyat:\n<i>örn: 3500</i>", parse_mode="HTML")
        return ASK_LINK_PRICE
    plat = context.user_data["plat"]
    disp = {"trendyol":"Trendyol","hepsiburada":"Hepsiburada","amazon":"Amazon TR","n11":"N11"}.get(plat, plat)
    product = {
        "id": make_uid(), "type": "link",
        "name": context.user_data["name"],
        "url":  context.user_data["url"],
        "platform": disp,
        "target_price": price, "active": True,
        "paused_until": None, "last_alerted": None,
        "price_history": [], "note": "", "added_at": date_str(),
    }
    PRODUCTS.append(product)
    save_products(PRODUCTS)
    context.user_data.clear()
    await update.message.reply_text(
        f"✅ <b>Eklendi!</b>\n\n📦 {product['name']}\n🏪 {disp}\n🎯 Hedef: {fmt(price)}\n\n"
        f"Fiyat şimdi kontrol ediliyor...",
        parse_mode="HTML"
    )
    # Hemen fiyat kontrolü yap — hedef altındaysa anında alarm
    await check_link(context.application, product)
    return ConversationHandler.END

# ── Keyword ─────────────────────────────────────────────
async def kw_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    context.user_data["platform"] = q.data.replace("kw_pl_", "")
    await q.edit_message_text(
        f"🔍 Platform: <b>{context.user_data['platform']}</b>\n\nArama kelimesini yaz:\n<i>örn: gaming mouse</i>\n\n/iptal",
        parse_mode="HTML"
    )
    return ASK_KW_QUERY

async def kw_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["keyword"] = update.message.text.strip()
    await update.message.reply_text(
        f"🔍 <b>\"{context.user_data['keyword']}\"</b> — {context.user_data['platform']}\n\n"
        "Hangi fiyat altında bildirim gelsin?\n<i>örn: 3500</i>\n\n/iptal",
        parse_mode="HTML"
    )
    return ASK_KW_PRICE

async def kw_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = parse_price(update.message.text)
    if not price:
        await update.message.reply_text("❌ Geçersiz fiyat:")
        return ASK_KW_PRICE
    product = {
        "id": make_uid(), "type": "keyword",
        "name": f"🔍 {context.user_data['keyword']}",
        "keyword": context.user_data["keyword"],
        "platform": context.user_data["platform"],
        "target_price": price, "active": True,
        "paused_until": None, "last_alerted": None,
        "price_history": [], "note": "", "seen_urls": [],
        "added_at": date_str(),
    }
    PRODUCTS.append(product)
    save_products(PRODUCTS)
    context.user_data.clear()
    await update.message.reply_text(
        f"✅ <b>Kelime takibi eklendi!</b>\n\n🔍 \"{product['keyword']}\"\n"
        f"🏪 {product['platform']}\n🎯 Hedef: {fmt(price)} altı",
        parse_mode="HTML"
    )
    return ConversationHandler.END

# ── Seller ──────────────────────────────────────────────
SELLER_HINT = {
    "Trendyol":    "https://www.trendyol.com/magaza/magaza-adi-m-123456",
    "Amazon TR":   "https://www.amazon.com.tr/s?me=SATICI_ID",
    "Hepsiburada": "https://www.hepsiburada.com/magaza/magaza-adi",
}

async def seller_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    pl = q.data.replace("seller_pl_", "")
    context.user_data["platform"] = pl
    hint = SELLER_HINT.get(pl, "Mağaza sayfası linki")
    await q.edit_message_text(
        f"🏪 Platform: <b>{pl}</b>\n\nMağaza sayfasının linkini gönder:\n<code>{hint}</code>\n\n/iptal",
        parse_mode="HTML"
    )
    return ASK_SELLER_URL

async def seller_url_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    url = normalize_url(raw)
    if not is_valid_url(url):
        await update.message.reply_text("❌ Geçersiz link.\n\n/iptal")
        return ASK_SELLER_URL
    context.user_data["seller_url"] = url
    parts = [p for p in url.split("/") if p and "?" not in p and "." not in p]
    context.user_data["seller_name"] = parts[-1][:30] if parts else "Satıcı"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Kelimeyle Filtrele",     callback_data="seller_kw_yes")],
        [InlineKeyboardButton("📦 Tüm Ürünleri Takip Et", callback_data="seller_kw_no")],
    ])
    await update.message.reply_text(
        f"✅ Mağaza: <b>{context.user_data['seller_name']}</b>\n\n"
        "Nasıl takip yapalım?\n\n"
        "🔍 <b>Filtreli</b> — sadece belirli kelimeyi içeren ürünler\n"
        "📦 <b>Tümü</b> — mağazadaki her ürün\n\n/iptal",
        parse_mode="HTML", reply_markup=kb
    )
    return ASK_SELLER_KEYWORD

async def seller_kw_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "seller_kw_yes":
        await q.edit_message_text(
            "🔍 Hangi kelimeyi içeren ürünler takip edilsin?\n"
            "<i>örn: kulaklık   |   Tümü için: -</i>\n\n/iptal",
            parse_mode="HTML"
        )
        return ASK_SELLER_KEYWORD
    context.user_data["seller_kw"] = ""
    await q.edit_message_text(
        "💰 Fiyat eşiği?\n"
        "<i>Bu fiyat altındaki ürün bulununca bildir. Tümü için 0 gir.</i>\n\n/iptal",
        parse_mode="HTML"
    )
    return ASK_SELLER_PRICE

async def seller_kw_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    context.user_data["seller_kw"] = "" if t == "-" else t
    label = context.user_data["seller_kw"] or "Tüm ürünler"
    await update.message.reply_text(
        f"✅ Filtre: <b>{label}</b>\n\n"
        "💰 Fiyat eşiği?\n<i>Bu fiyat altındaki ürün bulununca bildir. Tümü için 0 gir.</i>\n\n/iptal",
        parse_mode="HTML"
    )
    return ASK_SELLER_PRICE

async def seller_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = parse_price(update.message.text)
    if price is None:
        await update.message.reply_text("❌ Geçersiz fiyat. Rakam gir (tümü için 0):")
        return ASK_SELLER_PRICE
    kw = context.user_data.get("seller_kw", "")
    pl = context.user_data["platform"]
    product = {
        "id": make_uid(), "type": "seller",
        "name": f"🏪 {context.user_data['seller_name']}",
        "seller_name": context.user_data["seller_name"],
        "seller_url":  context.user_data["seller_url"],
        "seller_keyword": kw,
        "platform": pl,
        "target_price": price, "active": True,
        "paused_until": None, "last_alerted": None,
        "price_history": [], "note": "", "seen_urls": [],
        "added_at": date_str(),
    }
    PRODUCTS.append(product)
    save_products(PRODUCTS)
    context.user_data.clear()
    kw_lbl = f"🔍 {kw}" if kw else "📦 Tüm ürünler"
    await update.message.reply_text(
        f"✅ <b>Satıcı takibi eklendi!</b>\n\n"
        f"🏪 {product['seller_name']} ({pl})\n"
        f"{kw_lbl}\n🎯 Eşik: {fmt(price) if price > 0 else 'Tümü'}",
        parse_mode="HTML"
    )
    return ConversationHandler.END

async def conv_iptal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ İptal edildi.")
    return ConversationHandler.END

# ════════════════════════════════════════════════════════
#  🎯  /hedef CONVERSATION
# ════════════════════════════════════════════════════════

async def cmd_hedef(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not PRODUCTS:
        await update.message.reply_text("📭 Liste boş.")
        return ConversationHandler.END
    icons = {"link":"🔗","keyword":"🔍","seller":"🏪"}
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{icons.get(p.get('type','link'),'🔗')} {label_of(p)}",
                              callback_data=f"hedef_sec_{p['id']}")]
        for p in PRODUCTS
    ])
    await update.message.reply_text("Hedef fiyatını değiştirmek istediğin takibi seç:", reply_markup=kb)
    return ASK_HEDEF_SEC

async def hedef_sec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    pid = q.data.replace("hedef_sec_", "")
    p   = find_product(pid)
    if not p:
        await q.edit_message_text("❌ Bulunamadı.")
        return ConversationHandler.END
    context.user_data["hedef_pid"] = pid
    await q.edit_message_text(
        f"📦 <b>{label_of(p)}</b>\n🎯 Mevcut: {fmt(p['target_price'])}\n\nYeni hedef fiyatı yaz:",
        parse_mode="HTML"
    )
    return ASK_HEDEF_FIYAT

async def hedef_fiyat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_price = parse_price(update.message.text)
    if not new_price:
        await update.message.reply_text("❌ Geçersiz fiyat:")
        return ASK_HEDEF_FIYAT
    p = find_product(context.user_data.get("hedef_pid", ""))
    if not p:
        await update.message.reply_text("❌ Bulunamadı.")
        return ConversationHandler.END
    old = p["target_price"]
    p["target_price"] = new_price
    p["last_alerted"]  = None
    save_products(PRODUCTS)
    context.user_data.clear()
    arrow = "⬇️" if new_price < old else "⬆️"
    await update.message.reply_text(
        f"✅ <b>{label_of(p)}</b>\n{arrow} {fmt(old)} → <b>{fmt(new_price)}</b>",
        parse_mode="HTML"
    )
    return ConversationHandler.END

# ════════════════════════════════════════════════════════
#  📝  /not CONVERSATION
# ════════════════════════════════════════════════════════

async def cmd_not(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not PRODUCTS:
        await update.message.reply_text("📭 Liste boş.")
        return ConversationHandler.END
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(label_of(p), callback_data=f"not_sec_{p['id']}")]
        for p in PRODUCTS
    ])
    await update.message.reply_text("Hangi takibe not eklemek istiyorsun?", reply_markup=kb)
    return ASK_NOT_SEC

async def not_sec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    pid = q.data.replace("not_sec_", "")
    p   = find_product(pid)
    if not p:
        await q.edit_message_text("❌ Bulunamadı.")
        return ConversationHandler.END
    context.user_data["not_pid"] = pid
    mevcut = f"\nMevcut: <i>{p['note']}</i>" if p.get("note") else ""
    await q.edit_message_text(
        f"📦 <b>{label_of(p)}</b>{mevcut}\n\nYeni notu yaz (sil için - yaz):",
        parse_mode="HTML"
    )
    return ASK_NOT_METIN

async def not_metin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    p = find_product(context.user_data.get("not_pid", ""))
    if not p:
        await update.message.reply_text("❌ Bulunamadı.")
        return ConversationHandler.END
    p["note"] = "" if text == "-" else text
    save_products(PRODUCTS)
    context.user_data.clear()
    await update.message.reply_text(
        f"📝 <i>{p['note'] or '(not silindi)'}</i>", parse_mode="HTML"
    )
    return ConversationHandler.END

# ════════════════════════════════════════════════════════
#  📋  /kopyala CONVERSATION
# ════════════════════════════════════════════════════════

async def cmd_kopyala(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not PRODUCTS:
        await update.message.reply_text("📭 Liste boş.")
        return ConversationHandler.END
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(label_of(p), callback_data=f"kopyala_sec_{p['id']}")]
        for p in PRODUCTS
    ])
    await update.message.reply_text(
        "Hangi takibi kopyalamak istiyorsun?\n<i>Aynı ürün, farklı hedef fiyat</i>",
        parse_mode="HTML", reply_markup=kb
    )
    return ASK_KOPYALA_SEC

async def kopyala_sec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    pid = q.data.replace("kopyala_sec_", "")
    p   = find_product(pid)
    if not p:
        await q.edit_message_text("❌ Bulunamadı.")
        return ConversationHandler.END
    context.user_data["kopyala_pid"] = pid
    await q.edit_message_text(
        f"📋 <b>{label_of(p)}</b>\nMevcut hedef: {fmt(p['target_price'])}\n\nYeni kopya için hedef fiyatı yaz:",
        parse_mode="HTML"
    )
    return ASK_KOPYALA_FIYAT

async def kopyala_fiyat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_price = parse_price(update.message.text)
    if not new_price:
        await update.message.reply_text("❌ Geçersiz fiyat:")
        return ASK_KOPYALA_FIYAT
    orig = find_product(context.user_data.get("kopyala_pid", ""))
    if not orig:
        await update.message.reply_text("❌ Bulunamadı.")
        return ConversationHandler.END
    kopya = copy.deepcopy(orig)
    kopya.update({
        "id": make_uid(),
        "target_price": new_price,
        "last_alerted": None,
        "price_history": [],
        "seen_urls": [],
        "added_at": date_str(),
        "name": f"{label_of(orig)} (kopya)",
    })
    PRODUCTS.append(kopya)
    save_products(PRODUCTS)
    context.user_data.clear()
    await update.message.reply_text(
        f"📋 <b>Kopyalandı!</b>\n\n📦 {kopya['name']}\n🎯 Hedef: {fmt(new_price)}",
        parse_mode="HTML"
    )
    return ConversationHandler.END

# ════════════════════════════════════════════════════════
#  ⚙️  /ayarlar CONVERSATION
# ════════════════════════════════════════════════════════

async def cmd_ayarlar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = SETTINGS
    await update.message.reply_text(
        f"⚙️ <b>Ayarlar</b>\n\n"
        f"check_every         → {s.get('check_every',1)} dk\n"
        f"alert_cooldown      → {s.get('alert_cooldown',30)} dk\n"
        f"daily_report        → {s.get('daily_report','09:00')}\n"
        f"silent_mode         → {s.get('silent_mode',False)}\n"
        f"silent_start        → {s.get('silent_start','23:00')}\n"
        f"silent_end          → {s.get('silent_end','08:00')}\n"
        f"max_keyword_results → {s.get('max_keyword_results',5)}\n\n"
        "Değiştirmek için ayar adını yaz:\n/iptal",
        parse_mode="HTML"
    )
    return ASK_AYAR_KEY

VALID_SETTINGS = [
    "check_every", "alert_cooldown", "daily_report",
    "silent_mode", "silent_start", "silent_end", "max_keyword_results"
]

async def ayar_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.message.text.strip().lower()
    if key not in VALID_SETTINGS:
        await update.message.reply_text(
            f"❌ Geçersiz. Şunlardan biri:\n" + "  ".join(VALID_SETTINGS)
        )
        return ASK_AYAR_KEY
    context.user_data["ayar_key"] = key
    await update.message.reply_text(f"Yeni değer? ({key})\n/iptal")
    return ASK_AYAR_VAL

async def ayar_val(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = context.user_data.get("ayar_key")
    val = update.message.text.strip()
    try:
        if key in ("check_every", "alert_cooldown", "max_keyword_results"):
            SETTINGS[key] = int(val)
        elif key == "silent_mode":
            SETTINGS[key] = val.lower() in ("true", "evet", "1", "açık", "on")
        else:
            SETTINGS[key] = val
        save_settings(SETTINGS)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ <b>{key}</b> → <b>{SETTINGS[key]}</b>", parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Geçersiz değer: {e}")
    return ConversationHandler.END

# ════════════════════════════════════════════════════════
#  📋  TEK KOMUTLAR
# ════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡️ <b>PricePulse v6.1</b>\n\n"
        "━━━━━━━━━━━━━━━\n"
        "<b>📦 Takip</b>\n"
        "/ekle · /sil · /liste · /kopyala · /not · /hedef\n\n"
        "<b>💰 Fiyat</b>\n"
        "/fiyat · /kontrol · /en_ucuz\n\n"
        "<b>📊 Rapor</b>\n"
        "/rapor · /istatistik · /platform · /log\n\n"
        "<b>⚙️ Sistem</b>\n"
        "/durdurAll · /baslatAll · /ayarlar · /yardim",
        parse_mode="HTML"
    )

async def cmd_liste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not PRODUCTS:
        await update.message.reply_text("📭 Liste boş. /ekle ile başla.")
        return
    icons = {"link": "🔗", "keyword": "🔍", "seller": "🏪"}
    # Her ürün için ayrı mesaj + butonlar gönder
    await update.message.reply_text(
        f"📋 <b>Takip Listesi ({len(PRODUCTS)} ürün)</b>",
        parse_mode="HTML"
    )
    for p in PRODUCTS:
        icon  = icons.get(p.get("type", "link"), "🔗")
        durum = "✅" if p.get("active") else "❌"
        pa    = p.get("paused_until")
        if pa and time.time() < pa:
            durum = f"⏸ {int((pa - time.time()) / 60)}dk"
        cur   = current_price(p)
        lo    = lowest_price(p)
        pid   = p["id"]

        lines = [f"{icon} <b>{label_of(p)}</b>  {durum}"]
        lines.append(f"🎯 Hedef: <b>{fmt(p['target_price'])}</b>")
        if cur:
            diff = cur - p["target_price"]
            hit  = "🎯 Hedefte!" if diff <= 0 else f"(%{round(diff/p['target_price']*100,1)} uzakta)"
            lines.append(f"💰 Şu an: {fmt(cur)}  {hit}")
        if lo:
            lines.append(f"🏆 En düşük: {fmt(lo)}")
        lines.append(f"🏪 {p.get('platform','?').capitalize()}  |  {price_trend(p)}")

        # Buton satırları
        row1 = []
        if p.get("type", "link") == "link" and p.get("url"):
            row1.append(InlineKeyboardButton("🛒 Ürüne Git", url=p["url"]))
        if p.get("type", "link") == "link":
            row1.append(InlineKeyboardButton("💰 Fiyat Sorgula", callback_data=f"fiyatsor_{pid}"))

        row2 = [
            InlineKeyboardButton("📊 Geçmiş",  callback_data=f"history_{pid}"),
            InlineKeyboardButton("⏸ Duraklat", callback_data=f"pause_60_{pid}"),
            InlineKeyboardButton("❌ Sil",      callback_data=f"delete_{pid}"),
        ]
        if not p.get("active") or (pa and time.time() < pa):
            row2[1] = InlineKeyboardButton("▶ Devam",   callback_data=f"resume_{pid}")

        rows = []
        if row1: rows.append(row1)
        rows.append(row2)

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows)
        )

async def cmd_sil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not PRODUCTS:
        await update.message.reply_text("📭 Liste boş.")
        return
    icons = {"link":"🔗","keyword":"🔍","seller":"🏪"}
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{icons.get(p.get('type','link'),'🔗')} {label_of(p)}",
            callback_data=f"delete_{p['id']}"
        )] for p in PRODUCTS
    ])
    await update.message.reply_text("Hangi takibi silmek istiyorsun?", reply_markup=kb)

async def cmd_fiyat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    links = [p for p in PRODUCTS if p.get("type","link") == "link" and p.get("active")]
    if not links:
        await update.message.reply_text("📭 Aktif link takibi yok.")
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(p["name"][:40], callback_data=f"fiyatsor_{p['id']}")]
        for p in links
    ])
    await update.message.reply_text("Hangi ürünün fiyatını sorgulayalım?", reply_markup=kb)

async def cmd_kontrol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    aktif = [p for p in PRODUCTS if p.get("active")]
    if not aktif:
        await update.message.reply_text("📭 Aktif takip yok. /ekle ile ürün ekle.")
        return

    msg = await update.message.reply_text(
        f"🔍 {len(aktif)} takip taranıyor, lütfen bekle..."
    )

    sonuclar = []
    hatalar  = []

    # Her ürünü tek tek tara ve sonucu topla
    for p in aktif:
        pa = p.get("paused_until")
        if pa and time.time() < pa:
            continue
        try:
            t = p.get("type", "link")
            if t == "link":
                price, in_stock = await asyncio.get_event_loop().run_in_executor(
                    None, fetch_price, p
                )
                if price is None:
                    hatalar.append(f"⚠️ {label_of(p)}: fiyat alınamadı")
                    continue
                record_price(p, price)
                diff = price - p["target_price"]
                if diff <= 0:
                    sonuclar.append(
                        f"🎯 <b>{label_of(p)}</b>\n"
                        f"   {fmt(price)} — hedefe ulaştı! ({fmt(abs(diff))} ucuz)"
                    )
                    # Cooldown yoksa alarm gönder
                    if _cooldown_ok(p) and not is_silent_now():
                        text, kb = build_alert_link(p, price, in_stock)
                        await context.application.bot.send_message(
                            chat_id=CHAT_ID, text=text, reply_markup=kb, parse_mode="HTML"
                        )
                        p["last_alerted"] = time.time()
                        mark_dirty()
                else:
                    sonuclar.append(
                        f"📦 <b>{label_of(p)}</b>\n"
                        f"   {fmt(price)} — hedefe %{round(diff/p['target_price']*100,1)} uzakta"
                    )
            elif t == "keyword":
                results = await asyncio.get_event_loop().run_in_executor(
                    None, do_keyword_search,
                    p["platform"], p["keyword"],
                    SETTINGS.get("max_keyword_results", 5)
                )
                hits = [r for r in results if r["price"] <= p["target_price"]]
                if hits:
                    sonuclar.append(
                        f"🔍 <b>{label_of(p)}</b>\n"
                        f"   {len(hits)} eşleşme bulundu! En ucuz: {fmt(hits[0]['price'])}"
                    )
                else:
                    sonuclar.append(
                        f"🔍 <b>{label_of(p)}</b>\n"
                        f"   Hedef fiyat altında ürün bulunamadı"
                        + (f" (en ucuz: {fmt(results[0]['price'])})" if results else "")
                    )
            elif t == "seller":
                items = await asyncio.get_event_loop().run_in_executor(
                    None, do_seller_scrape,
                    p["platform"], p["seller_url"], 10
                )
                kw = p.get("seller_keyword", "").strip().lower()
                filtered = [i for i in items if not kw or kw in i["name"].lower()]
                cheap = [i for i in filtered if i["price"] <= p["target_price"] or p["target_price"] == 0]
                sonuclar.append(
                    f"🏪 <b>{label_of(p)}</b>\n"
                    f"   {len(filtered)} ürün bulundu" +
                    (f", {len(cheap)} hedef fiyat altında" if cheap else ", hedef fiyat altında yok")
                )
        except Exception as e:
            hatalar.append(f"❌ {label_of(p)}: {str(e)[:60]}")

    flush_if_dirty()

    # Özet mesaj
    ozet = [f"✅ <b>Tarama Tamamlandı</b> — {now_str()}\n"]
    if sonuclar:
        ozet.append("\n".join(sonuclar))
    if hatalar:
        ozet.append("\n⚠️ <b>Hatalar:</b>")
        ozet.extend(hatalar)
    if not sonuclar and not hatalar:
        ozet.append("Taranacak aktif ürün bulunamadı.")

    await msg.edit_text("\n\n".join(ozet), parse_mode="HTML")

async def cmd_en_ucuz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    aktif = [p for p in PRODUCTS if p.get("active")]
    if not aktif:
        await update.message.reply_text("📭 Aktif takip yok.")
        return
    icons = {"link":"🔗","keyword":"🔍","seller":"🏪"}
    def sort_key(p):
        cur = current_price(p)
        return float("inf") if cur is None else (cur-p["target_price"])/p["target_price"]
    lines = ["🏆 <b>Hedefe Göre Sıralama</b>\n"]
    for i, p in enumerate(sorted(aktif, key=sort_key)[:8], 1):
        icon = icons.get(p.get("type","link"),"🔗")
        cur  = current_price(p)
        if cur:
            diff = cur - p["target_price"]
            ok   = f"🎯 {fmt(abs(diff))} ucuz!" if diff<=0 else f"+{round(diff/p['target_price']*100,1)}% uzakta"
            lines.append(f"{i}. {icon} <b>{label_of(p)}</b>\n   {fmt(cur)} → {fmt(p['target_price'])} {ok}\n")
        else:
            lines.append(f"{i}. {icon} <b>{label_of(p)}</b>\n   Hedef: {fmt(p['target_price'])} (ölçülmedi)\n")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_rapor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_daily_report(), parse_mode="HTML")

async def cmd_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not PRODUCTS:
        await update.message.reply_text("📭 Liste boş.")
        return
    gruplar: dict = {}
    for p in PRODUCTS:
        gruplar.setdefault(p.get("platform","?").capitalize(), []).append(p)
    icons = {"link":"🔗","keyword":"🔍","seller":"🏪"}
    lines = ["🏪 <b>Platforma Göre</b>\n"]
    for pl, urunler in sorted(gruplar.items()):
        lines.append(f"<b>— {pl} ({len(urunler)}) —</b>")
        for p in urunler:
            icon = icons.get(p.get("type","link"),"🔗")
            cur  = current_price(p)
            lines.append(f"  {icon} {label_of(p)} → {fmt(p['target_price'])}" + (f" | {fmt(cur)}" if cur else ""))
        lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_durdurAll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = sum(1 for p in PRODUCTS if p.get("active"))
    for p in PRODUCTS: p["active"] = False
    save_products(PRODUCTS)
    await update.message.reply_text(f"⏸ <b>{count} takip</b> duraklatıldı.", parse_mode="HTML")

async def cmd_baslatAll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = 0
    for p in PRODUCTS:
        if not p.get("active") or (p.get("paused_until") and time.time() < p["paused_until"]):
            p["active"] = True; p["paused_until"] = None; count += 1
    save_products(PRODUCTS)
    await update.message.reply_text(f"✅ <b>{count} takip</b> başlatıldı.", parse_mode="HTML")

async def cmd_istatistik(update: Update, context: ContextTypes.DEFAULT_TYPE):
    aktif   = sum(1 for p in PRODUCTS if p.get("active"))
    hedefte = sum(1 for p in PRODUCTS if current_price(p) and current_price(p)<=p["target_price"])
    olcum   = sum(len(p.get("price_history",[])) for p in PRODUCTS)
    await update.message.reply_text(
        f"📊 <b>PricePulse v6.1 — İstatistikler</b>\n\n"
        f"📦 Toplam: {len(PRODUCTS)}\n✅ Aktif: {aktif}\n🎯 Hedefte: {hedefte}\n"
        f"🔗 Link: {sum(1 for p in PRODUCTS if p.get('type','link')=='link')}\n"
        f"🔍 Kelime: {sum(1 for p in PRODUCTS if p.get('type')=='keyword')}\n"
        f"🏪 Satıcı: {sum(1 for p in PRODUCTS if p.get('type')=='seller')}\n"
        f"📈 Ölçüm: {olcum}\n\n"
        f"⏱ Tarama: {SETTINGS.get('check_every',1)} dk\n"
        f"🔕 Cooldown: {SETTINGS.get('alert_cooldown',30)} dk\n"
        f"🌙 Sessiz: {'Açık' if SETTINGS.get('silent_mode') else 'Kapalı'}",
        parse_mode="HTML"
    )

async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with open("pricepulse.log", "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-30:]
        # Binary / non-printable karakterleri temizle
        clean = ""
        for line in lines:
            clean += "".join(c if c.isprintable() or c in "\n\t" else "?" for c in line)
        if len(clean) > 3800:
            clean = "...\n" + clean[-3800:]
        await update.message.reply_text(
            f"📋 <b>Son Loglar</b>\n\n<pre>{clean}</pre>",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Log okunamadı: {e}")

async def cmd_yardim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡️ <b>PricePulse v6.1</b>\n\n"
        "🔗 Link — ürün sayfasını izle\n"
        "🔍 Kelime — aramada ucuz ürün bul\n"
        "🏪 Satıcı — mağazayı izle\n\n"
        "/ekle /sil /liste /kopyala /not /hedef\n"
        "/fiyat /kontrol /en_ucuz\n"
        "/rapor /istatistik /platform /log\n"
        "/durdurAll /baslatAll /ayarlar",
        parse_mode="HTML"
    )

# ════════════════════════════════════════════════════════
#  🔁  CALLBACK HANDLER
# ════════════════════════════════════════════════════════

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data  = q.data
    # Güvenli split: action ilk kelime, geri kalan ID
    idx    = data.index("_") if "_" in data else len(data)
    action = data[:idx]
    rest   = data[idx+1:] if "_" in data else ""

    # pause özel: pause_60_uid
    if action == "pause":
        parts2 = rest.split("_", 1)
        mins   = int(parts2[0]) if parts2 else 60
        pid    = parts2[1] if len(parts2) > 1 else ""
        p = find_product(pid)
        if not p: await q.edit_message_text("❌ Bulunamadı."); return
        p["paused_until"] = time.time() + mins * 60
        save_products(PRODUCTS)
        label = "1 gün" if mins==1440 else f"{mins//60} saat" if mins>=60 else f"{mins} dk"
        await q.edit_message_text(f"⏸ <b>{label_of(p)}</b> {label} duraklatıldı.", parse_mode="HTML")
        return

    if action == "stop":
        p = find_product(rest)
        if not p: await q.edit_message_text("❌ Bulunamadı."); return
        p["active"] = False; save_products(PRODUCTS)
        await q.edit_message_text(f"❌ <b>{label_of(p)}</b> durduruldu.", parse_mode="HTML"); return

    if action == "resume":
        p = find_product(rest)
        if not p: await q.edit_message_text("❌ Bulunamadı."); return
        p["active"] = True; p["paused_until"] = None; save_products(PRODUCTS)
        await q.edit_message_text(f"✅ <b>{label_of(p)}</b> tekrar aktif!", parse_mode="HTML"); return

    if action == "delete":
        p = find_product(rest)
        if p:
            lbl = label_of(p); PRODUCTS.remove(p); save_products(PRODUCTS)
            await q.edit_message_text(f"🗑 <b>{lbl}</b> silindi.", parse_mode="HTML")
        else:
            await q.edit_message_text("❌ Bulunamadı.")
        return

    if action == "history":
        p = find_product(rest)
        if not p: await q.edit_message_text("❌ Bulunamadı."); return
        h = p.get("price_history", [])
        if not h: await q.edit_message_text("📊 Henüz veri yok."); return
        lines = [f"📊 <b>{label_of(p)}</b>\n"]
        for e in h[-10:][::-1]:
            lines.append(f"  {e['date']}  →  {fmt(e['price'])}")
        lo, hi = lowest_price(p), highest_price(p)
        if lo: lines.append(f"\n🏆 En düşük: {fmt(lo)}")
        if hi: lines.append(f"📈 En yüksek: {fmt(hi)}")
        lines.append(f"\n{price_trend(p)}")
        await q.edit_message_text("\n".join(lines), parse_mode="HTML"); return

    if action == "fiyatsor":
        p = find_product(rest)
        if not p: await q.edit_message_text("❌ Bulunamadı."); return
        await q.edit_message_text(f"🔍 <b>{p['name']}</b>\nFiyat alınıyor...", parse_mode="HTML")
        price, in_stock = fetch_price(p)
        if price is None:
            await q.edit_message_text(f"⚠️ <b>{p['name']}</b>\nFiyat alınamadı.", parse_mode="HTML"); return
        record_price(p, price)
        flush_if_dirty()
        diff = price - p["target_price"]
        ok   = (f"✅ Hedefe ulaştı! {fmt(abs(diff))} ucuz!" if diff<=0
                else f"📍 Hedefe {fmt(diff)} ({round(diff/p['target_price']*100,1)}%) uzakta")
        await q.edit_message_text(
            f"💰 <b>{p['name']}</b>\n\n"
            f"Şu an: <b>{fmt(price)}</b>\nHedef: {fmt(p['target_price'])}\n{ok}\n"
            f"{'✅ Stokta' if in_stock else '⚠️ Stok belirsiz'} · {now_str()}",
            parse_mode="HTML"
        ); return

# ════════════════════════════════════════════════════════
#  🔍  PARALEL KONTROL MOTORU
# ════════════════════════════════════════════════════════

_cooldown_ok = lambda p: (
    not p.get("last_alerted") or
    (time.time() - p["last_alerted"]) / 60 >= SETTINGS.get("alert_cooldown", ALERT_COOLDOWN)
)

async def check_link(app, product: dict):
    price, in_stock = await asyncio.get_event_loop().run_in_executor(None, fetch_price, product)
    if price is None:
        log.warning(f"  ⚠️  {product['name'][:40]}: fiyat alınamadı")
        return
    record_price(product, price)
    log.info(f"  🔗 {product['name'][:40]}: {fmt(price)} (hedef: {fmt(product['target_price'])})")

    if price > product["target_price"]:
        log.info(f"  ↳ Hedef aşılmamış ({fmt(price)} > {fmt(product['target_price'])})")
        return
    if not _cooldown_ok(product):
        kalan = int(SETTINGS.get("alert_cooldown", ALERT_COOLDOWN) - (time.time() - product["last_alerted"]) / 60)
        log.info(f"  ↳ Cooldown: {kalan} dk kaldı")
        return
    if is_silent_now():
        log.info(f"  ↳ Sessiz mod aktif")
        return

    text, kb = build_alert_link(product, price, in_stock)
    await app.bot.send_message(chat_id=CHAT_ID, text=text, reply_markup=kb, parse_mode="HTML")
    product["last_alerted"] = time.time()
    mark_dirty()
    log.info(f"  ✅ Alarm gönderildi: {product['name'][:40]}")

async def check_keyword(app, product: dict):
    limit   = SETTINGS.get("max_keyword_results", 5)
    results = await asyncio.get_event_loop().run_in_executor(
        None, do_keyword_search, product["platform"], product["keyword"], limit
    )
    seen    = set(product.get("seen_urls", []))
    alerted = False
    for item in results:
        if item["price"] > product["target_price"] or item["url"] in seen:
            continue
        if not _cooldown_ok(product) or is_silent_now():
            continue
        log.info(f"  🔍 Eşleşme: {item['name'][:40]} — {fmt(item['price'])}")
        text, kb = build_alert_keyword(product, item)
        await app.bot.send_message(chat_id=CHAT_ID, text=text, reply_markup=kb, parse_mode="HTML")
        seen.add(item["url"])
        product["last_alerted"] = time.time()
        alerted = True
    if alerted:
        product["seen_urls"] = list(seen)[-100:]
        mark_dirty()

async def check_seller(app, product: dict):
    items   = await asyncio.get_event_loop().run_in_executor(
        None, do_seller_scrape, product["platform"], product["seller_url"], 10
    )
    seen    = set(product.get("seen_urls", []))
    keyword = product.get("seller_keyword", "").strip().lower()
    alerted = False
    for item in items:
        if keyword and keyword not in item["name"].lower():
            continue
        is_new   = item["url"] not in seen
        is_cheap = item["price"] <= product["target_price"] or product["target_price"] == 0
        # Yeni ürün her zaman bildir, pahalı eski ürünü bildirme
        if not is_new and not is_cheap:
            continue
        if not _cooldown_ok(product) or is_silent_now():
            continue
        log.info(f"  🏪 {'Yeni' if is_new else 'İndirim'}: {item['name'][:40]} — {fmt(item['price'])}")
        text, kb = build_alert_seller(product, item, is_new)
        await app.bot.send_message(chat_id=CHAT_ID, text=text, reply_markup=kb, parse_mode="HTML")
        seen.add(item["url"])
        product["last_alerted"] = time.time()
        alerted = True
    if alerted:
        product["seen_urls"] = list(seen)[-200:]
        mark_dirty()

async def run_price_check(app):
    log.info(f"═══ Tarama — {datetime.now().strftime('%H:%M:%S')} ═══")
    active = [
        p for p in PRODUCTS
        if p.get("active", True)
        and not (p.get("paused_until") and time.time() < p["paused_until"])
    ]
    if not active:
        log.info("  (aktif takip yok)")
        return

    # Semaphore ile maksimum eş zamanlı tarama sınırla
    sem = asyncio.Semaphore(MAX_PARALLEL)

    async def bounded(product):
        async with sem:
            try:
                t = product.get("type", "link")
                if   t == "link":    await check_link(app, product)
                elif t == "keyword": await check_keyword(app, product)
                elif t == "seller":  await check_seller(app, product)
            except Exception as e:
                log.error(f"  ❌ {product.get('name','?')[:30]}: {e}")

    await asyncio.gather(*[bounded(p) for p in active])
    flush_if_dirty()   # tarama sonunda toplu kaydet
    log.info(f"═══ Tarama tamamlandı ({len(active)} ürün) ═══")

# ════════════════════════════════════════════════════════
#  📅  SCHEDULER
# ════════════════════════════════════════════════════════

def start_scheduler(app):
    def run_sync(coro_fn):
        if not _scan_lock.acquire(blocking=False):
            log.warning("Tarama atlandı (önceki devam ediyor).")
            return
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(coro_fn())
        except Exception as e:
            log.error(f"Scheduler hata: {e}")
        finally:
            _scan_lock.release()
            loop.close()

    async def scan():  await run_price_check(app)
    async def daily():
        try:
            await app.bot.send_message(
                chat_id=CHAT_ID, text=build_daily_report(), parse_mode="HTML"
            )
            log.info("📅 Günlük rapor gönderildi.")
        except Exception as e:
            log.error(f"Rapor hatası: {e}")

    interval    = SETTINGS.get("check_every", CHECK_EVERY)
    report_time = SETTINGS.get("daily_report", DAILY_REPORT)

    schedule.every(interval).minutes.do(lambda: run_sync(scan))
    schedule.every().day.at(report_time).do(lambda: run_sync(daily))

    def loop():
        while True:
            schedule.run_pending()
            time.sleep(15)

    threading.Thread(target=loop, daemon=True).start()
    log.info(f"⏱  Scheduler: {interval} dk | rapor: {report_time}")

# ════════════════════════════════════════════════════════
#  🚀  MAIN
# ════════════════════════════════════════════════════════

def main():
    print("⚡️ PricePulse v6 başlatılıyor...")
    print(f"   • {len(PRODUCTS)} takip yüklendi")
    print(f"   • Tarama: her {SETTINGS.get('check_every',1)} dakika")
    print(f"   • Paralel tarama: {MAX_PARALLEL} ürün eş zamanlı")
    print(f"   • Günlük rapor: {SETTINGS.get('daily_report','09:00')}")
    print(f"   • Veri: Upstash Redis ({UPSTASH_URL[:30]}...)\n")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_ekle = ConversationHandler(
        entry_points=[CommandHandler("ekle", cmd_ekle)],
        states={
            ASK_TYPE:            [CallbackQueryHandler(ekle_type_sec,   pattern=r"^ekle_")],
            ASK_LINK_NAME:       [MessageHandler(filters.TEXT & ~filters.COMMAND, link_name)],
            ASK_LINK_URL:        [MessageHandler(filters.TEXT & ~filters.COMMAND, link_url)],
            ASK_LINK_PRICE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, link_price)],
            ASK_KW_PLATFORM:     [CallbackQueryHandler(kw_platform,     pattern=r"^kw_pl_")],
            ASK_KW_QUERY:        [MessageHandler(filters.TEXT & ~filters.COMMAND, kw_query)],
            ASK_KW_PRICE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, kw_price)],
            ASK_SELLER_PLATFORM: [CallbackQueryHandler(seller_platform, pattern=r"^seller_pl_")],
            ASK_SELLER_URL:      [MessageHandler(filters.TEXT & ~filters.COMMAND, seller_url_input)],
            ASK_SELLER_KEYWORD:  [
                CallbackQueryHandler(seller_kw_choice, pattern=r"^seller_kw_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, seller_kw_text),
            ],
            ASK_SELLER_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, seller_price)],
        },
        fallbacks=[CommandHandler("iptal", conv_iptal)],
        allow_reentry=True,
    )

    conv_hedef = ConversationHandler(
        entry_points=[CommandHandler("hedef", cmd_hedef)],
        states={
            ASK_HEDEF_SEC:  [CallbackQueryHandler(hedef_sec,   pattern=r"^hedef_sec_")],
            ASK_HEDEF_FIYAT:[MessageHandler(filters.TEXT & ~filters.COMMAND, hedef_fiyat)],
        },
        fallbacks=[CommandHandler("iptal", conv_iptal)],
        allow_reentry=True,
    )

    conv_not = ConversationHandler(
        entry_points=[CommandHandler("not", cmd_not)],
        states={
            ASK_NOT_SEC:   [CallbackQueryHandler(not_sec,   pattern=r"^not_sec_")],
            ASK_NOT_METIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, not_metin)],
        },
        fallbacks=[CommandHandler("iptal", conv_iptal)],
        allow_reentry=True,
    )

    conv_kopyala = ConversationHandler(
        entry_points=[CommandHandler("kopyala", cmd_kopyala)],
        states={
            ASK_KOPYALA_SEC:  [CallbackQueryHandler(kopyala_sec,   pattern=r"^kopyala_sec_")],
            ASK_KOPYALA_FIYAT:[MessageHandler(filters.TEXT & ~filters.COMMAND, kopyala_fiyat)],
        },
        fallbacks=[CommandHandler("iptal", conv_iptal)],
        allow_reentry=True,
    )

    conv_ayarlar = ConversationHandler(
        entry_points=[CommandHandler("ayarlar", cmd_ayarlar)],
        states={
            ASK_AYAR_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ayar_key)],
            ASK_AYAR_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ayar_val)],
        },
        fallbacks=[CommandHandler("iptal", conv_iptal)],
        allow_reentry=True,
    )

    for conv in [conv_ekle, conv_hedef, conv_not, conv_kopyala, conv_ayarlar]:
        app.add_handler(conv)

    for cmd, fn in [
        ("start",      cmd_start),
        ("liste",      cmd_liste),
        ("sil",        cmd_sil),
        ("fiyat",      cmd_fiyat),
        ("kontrol",    cmd_kontrol),
        ("en_ucuz",    cmd_en_ucuz),
        ("rapor",      cmd_rapor),
        ("platform",   cmd_platform),
        ("durdurAll",  cmd_durdurAll),
        ("baslatAll",  cmd_baslatAll),
        ("istatistik", cmd_istatistik),
        ("log",        cmd_log),
        ("yardim",     cmd_yardim),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(CallbackQueryHandler(callback_handler))

    async def on_startup(a):
        start_scheduler(a)
        await asyncio.sleep(5)
        await run_price_check(a)

    app.post_init = on_startup
    print("✅ Bot hazır. /start yaz.\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
