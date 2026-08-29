#!/usr/bin/env python3
"""
GSMArena makale scraper'ı — SADECE O GÜN YAYINLANAN yeni haberler için.

Tüm siteyi baştan taramaz: haber listeleme sayfasını (news.php3) en yeni
kayıttan başlayarak okur, listeleme sayfasındaki her öğenin yanında zaten
görünen tarihi kullanarak bugüne ait OLMAYAN veya daha önce işlenmiş
(kayıt defterinde "ok" durumunda) bir öğeye ulaşır ulaşmaz taramayı durdurur
(liste kronolojik olduğundan ondan sonrası zaten eski/işlenmiş demektir).

Sadece bugüne ait yeni bulunan haberler için makale/haber sayfasının TAM
içeriği çekilir (site'nin kendi RSS özet akışı asla kullanılmaz), çevrilir
ve hem JSON hem de Markdown formatında kaydedilir.

Kalıcı durum: data/registry.json — haber ID'sine (URL'deki "-news-<id>.php"
numarası) göre anahtarlanan, her haberin durumunu (ok/failed), URL'sini,
tarihini ve bir SHA-256 içerik özetini (hash) tutan kalıcı kayıt defteri.
Bu defter sayesinde: (1) daha önce işlenen hiçbir haber tekrar çekilmez,
(2) başarısız olan haberler bir sonraki çalıştırmada otomatik tekrar denenir,
(3) RSS feed'i, her haber için BİR KEZ üretilip diskte önbelleklenen
<item> XML parçacıkları birleştirilerek oluşturulur; eski haberlerin
HTML/XML içeriği her çalıştırmada yeniden üretilmez, sadece yeni haberler
için üretilir ve mevcut feed'e eklenir.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from deep_translator import MyMemoryTranslator

BASE_URL = "https://www.gsmarena.com"
NEWS_INDEX_URL = f"{BASE_URL}/news.php3"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT_DIR = DATA_DIR / "articles"
REGISTRY_PATH = DATA_DIR / "registry.json"
DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
RSS_PATH = DOCS_DIR / "rss.xml"
SITE_URL = os.environ.get("SITE_URL", BASE_URL).rstrip("/")
SITE_NAME = os.environ.get("SITE_NAME", "GSMArena Türkçe")
RSS_ITEM_LIMIT = 60

# Görseller GSMArena'nın kendi CDN'inden (fdn.gsmarena.com) doğrudan
# hotlink'lenmek yerine indirilip data/images/<slug>/ altında SAKLANIR ve
# RSS'e bu yayınlanan (GitHub Pages) adresle konur. Bu iki sorunu birden
# çözer: (1) GSMArena'nın olası hotlink/referrer engeli görselin RSS
# okuyucuda kırık çıkmasına yol açmaz, (2) görsel silinir/taşınırsa bile
# kendi arşivimizde kalıcı olarak durur. Repodaki mevcut yayın adresi
# (docs/rss.xml şu an bu domainde canlı) varsayılan değerdir; farklı bir
# GitHub Pages adresine taşınırsa PAGES_BASE_URL ortam değişkeniyle geçilebilir.
PAGES_BASE_URL = (
    os.environ.get("PAGES_BASE_URL") or "https://umutevicom-commits.github.io/tarihte"
).rstrip("/")
IMAGES_DIR = DATA_DIR / "images"
# İndirilecek görseller için bilinen/izin verilen uzantılar; URL'de bunlardan
# biri yoksa (ör. sorgu parametreli/uzantısız adresler) Content-Type
# başlığından tahmin edilir, o da başarısız olursa .jpg varsayılır.
IMAGE_EXT_FALLBACK = ".jpg"
KNOWN_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,tr;q=0.8",
}
REQUEST_TIMEOUT = 30
# Artık tüm site değil, sadece bugüne ait yeni haberler işlendiğinden bir
# çalıştırmada tipik olarak tek haneli/düşük onlu sayıda istek yapılır; nezaket
# gecikmesi bu yüzden güvenle kısaltıldı (performans isteği).
DELAY_BETWEEN_REQUESTS = 1.0  # saniye
# Listeleme sayfası, taşma durumuna (bugüne ait haberlerin ilk sayfaya sığmadığı
# yoğun günlere) karşı bir güvenlik sınırı; normal koşulda taramayı bugünün
# haberleri bitince kendisi durdurur, bu sadece bir üst sınırdır.
MAX_PAGES = int(os.environ.get("GSMARENA_MAX_PAGES", "5"))
DELAY_BETWEEN_TRANSLATIONS = 0.5  # saniye
# Çeviri yapılıp yapılmayacağı (1=evet, 0=hayır)
ENABLE_TRANSLATION = os.environ.get("GSMARENA_TRANSLATE", "1") != "0"

# Aynı sunucuya yapılan ardışık istekler için tek bir TCP/TLS bağlantısı
# yeniden kullanılır (keep-alive); her istekte yeniden el sıkışma yapılmaz.
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Haber ID'si her zaman "...-news-<id>.php" kalıbındadır (URL'nin sonu).
ARTICLE_ID_RE = re.compile(r"-news-(\d+)\.php$")
# Listeleme sayfasında her haberin yanında yorum sayısı linki
# "newscomm-<id>.php" kalıbındadır ve haberin ID'siyle birebir eşleşir; bu,
# class adından bağımsız, o haberin YAYIN TARİHİNİ bulmak için kullanılan
# sabit bir çapa noktasıdır (tarih metni bu linkin hemen yanında durur).
LISTING_COMMENT_ID_RE = re.compile(r"newscomm-(\d+)\.php")

MAX_TRANSLATION_CHUNK = 450  # MyMemoryTranslator'ın ~500 karakter limitinin altında

# MyMemory ücretsiz API, anonim (email'siz) istemcilerde günlük ~5.000 karakterlik
# çok düşük bir kotayla sınırlıdır ve bu kota dolduğunda İSTİSNA FIRLATMAZ; bunun
# yerine "MYMEMORY WARNING: ..." gibi bir metni normal çeviri sonucu gibi döndürür.
# Bu, fark edilmeden makale içine İngilizce/hatalı uyarı metninin karışmasına yol
# açar. Bir e-posta adresi (de= parametresi) gönderildiğinde günlük kota ~50.000
# karaktere çıkar. GSMARENA_TRANSLATE_EMAIL ortam değişkeni ile ayarlanabilir.
TRANSLATE_EMAIL = os.environ.get("GSMARENA_TRANSLATE_EMAIL") or None

# NOT: https://cdn.gtranslate.net/widgets/latest/float.js bir TARAYICI widget'ıdır;
# bir ziyaretçinin AÇTIĞI bir web sayfasında JavaScript çalıştırarak sayfayı
# anlık çevirir. rss.xml statik bir dosyadır ve besleme okuyucuları (feed
# reader) <script> içeriğini ne çalıştırır ne de yükler; bu widget'ı
# <content:encoded> içine gömmek gerçek bir çeviriye yol açmaz, sadece etkisiz/
# temizlenen bir <script> etiketi bırakır. Bu yüzden RSS'e gömülecek GERÇEK
# Türkçe metin, mevcut haliyle (MyMemory tabanlı) sunucu taraflı çeviri
# üzerinden üretilmeye devam eder — bu, mevcut mimariyi bozmamak ve feed'in
# gerçekten Türkçe içerik taşımasını sağlamak için bilinçli bir tercihtir.

_translator = (
    MyMemoryTranslator(source="english", target="turkish", email=TRANSLATE_EMAIL)
    if ENABLE_TRANSLATION
    else None
)


TRANSLATION_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0

# MyMemory'nin normal çeviri yerine döndürdüğü bilinen hata/uyarı metinleri.
# Bunlardan biri sonuçta geçiyorsa, bu GEÇERLİ bir çeviri DEĞİLDİR; tekrar
# denenmeli, olmuyorsa orijinal metne düşülmelidir (yoksa uyarı metni sessizce
# makalenin içine "çeviri" gibi karışır).
MYMEMORY_ERROR_MARKERS = (
    "MYMEMORY WARNING",
    "YOU USED ALL AVAILABLE FREE TRANSLATIONS",
    "INVALID SOURCE LANGUAGE",
    "INVALID TARGET LANGUAGE",
    "IS AN INVALID",
    "AMOUNT OF WORDS DAILY LIMIT",
    "QUERY LENGTH LIMIT",
    "PLEASE SELECT TWO DISTINCT LANGUAGES",
)

# Aynı metin (başlık, tekrarlayan paragraf, etiket vb.) birden fazla makalede /
# yerde geçebilir. Aynı içerik ASLA iki kez çevrilmez: bir kez çevrilen her metin
# bu önbellekte tutulur ve tekrar karşılaşıldığında doğrudan oradan kullanılır.
_translation_cache: dict[str, str] = {}


def _looks_like_translation_error(result: str) -> bool:
    upper = result.upper()
    return any(marker in upper for marker in MYMEMORY_ERROR_MARKERS)


def _translate_chunk(text: str) -> str:
    """Tek bir metin parçasını çevirir (450 karakterden kısa olmalı).
    Geçici hatalarda ve MyMemory'nin kota/hata uyarısı döndürdüğü durumlarda
    birkaç kez tekrar dener; eksiksiz çeviri için son çare olarak orijinal
    metne düşer (asla bir hata uyarı metnini "çeviri" olarak kabul etmez)."""
    last_exc: Exception | None = None
    for attempt in range(1, TRANSLATION_RETRIES + 1):
        try:
            result = _translator.translate(text)
            time.sleep(DELAY_BETWEEN_TRANSLATIONS)
            if result and result.strip() and not _looks_like_translation_error(result):
                return result
            if result and _looks_like_translation_error(result):
                print(f"    [çeviri-tekrar {attempt}/{TRANSLATION_RETRIES}] MyMemory kota/hata uyarısı döndürdü")
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            print(f"    [çeviri-tekrar {attempt}/{TRANSLATION_RETRIES}] {exc}")
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    if last_exc:
        print(f"    [çeviri-hata] {TRANSLATION_RETRIES} denemeden sonra vazgeçildi: {last_exc}")
    else:
        print(f"    [çeviri-hata] {TRANSLATION_RETRIES} denemeden sonra geçerli çeviri alınamadı, orijinal metin korunuyor")
    return text


def translate_text(text: str) -> str:
    """Tek bir metni Türkçeye çevirir. Uzun metinleri cümlelere bölerek çevirir.
    Çeviri başarısız olursa orijinal metni döndürür. Daha önce çevrilmiş aynı
    metin varsa API'ye tekrar istek atmadan önbellekten döndürülür."""
    if not _translator or not text.strip():
        return text
    cached = _translation_cache.get(text)
    if cached is not None:
        return cached
    try:
        if len(text) <= MAX_TRANSLATION_CHUNK:
            translated = _translate_chunk(text)
            _translation_cache[text] = translated
            return translated
        # Uzun metinleri nokta ile biten cümlelere böl
        sentences = re.split(r"(?<=[.!?])\s+", text)
        chunks: list[str] = []
        current = ""
        for sentence in sentences:
            if len(current) + len(sentence) + 1 <= MAX_TRANSLATION_CHUNK:
                current = f"{current} {sentence}".strip()
            else:
                if current:
                    chunks.append(current)
                # Tek bir cümle bile chunk limitinden uzunsa, karakter olarak böl
                if len(sentence) > MAX_TRANSLATION_CHUNK:
                    for i in range(0, len(sentence), MAX_TRANSLATION_CHUNK):
                        chunks.append(sentence[i:i + MAX_TRANSLATION_CHUNK])
                else:
                    current = sentence
        if current:
            chunks.append(current)
        translated = [_translate_chunk(chunk) for chunk in chunks]
        full_translation = " ".join(translated)
        _translation_cache[text] = full_translation
        return full_translation
    except Exception as exc:
        print(f"    [çeviri-hata] {exc}")
        return text

ARTICLE_LINK_RE = re.compile(r"^[a-z0-9_]+-news-\d+\.php$")
# Yazar linki her zaman author.php3?idAuthor=... kalıbındadır (class adı değişse bile sabit kalır)
AUTHOR_LINK_RE = re.compile(r"author\.php3\?idAuthor=")
# Etiket linkleri her zaman news.php3?sTag=... kalıbındadır
TAG_LINK_RE = re.compile(r"news\.php3\?sTag=")
# "29 August 2026" gibi tarih metinlerini serbest metin içinde yakalar
DATE_TEXT_RE = re.compile(r"\d{1,2}\s+[A-Za-zÇŞĞÜÖİçşğüöı]+\s+\d{4}")

# Gövde metnini ararken önce denenecek bilinen kapsayıcılar (class adı zamanla
# değişmiş olabileceğinden hepsi denenir, ilk eşleşen ve içinde <p> bulunan kullanılır)
BODY_CONTAINER_CANDIDATES: list[tuple[str, dict]] = [
    ("div", {"class": "review-body"}),
    ("div", {"id": "article-body"}),
    ("div", {"class": "article-body"}),
    ("div", {"itemprop": "articleBody"}),
    ("article", {}),
]

# Gövde taramasını durduran başlık metinleri (bunlardan sonrası yorum/ilgili
# haber/tavsiye edilen içerik bölümüdür, makalenin bir parçası değildir)
STOP_HEADING_MARKERS = ("related articles", "reader comments", "recommended", "ilgili haberler", "yorumlar")

# Ata elemanlardan biri bu ipuçlarını içeriyorsa (fiyat kutusu, reklam, yorum,
# ilgili içerik, sosyal paylaşım vb.) o eleman gövdeye dahil edilmez
SKIP_ANCESTOR_HINTS = (
    "comment", "related", "recommended", "sidebar", "popular", "similar",
    "price", "deal", "banner", "advert", "footer", "share", "social",
    "newsletter", "subscribe", "toplist", "top10",
)

# Fiyat/afiliate kutularında sıkça geçen, makale metni olmayan ibareler
DISCLOSURE_SNIPPETS = ("affiliate partners", "qualifying sales", "preferred source on google")

# "İlgili haberler" (Related articles) kutusu her zaman bu URL kalıbındaki
# linkleri içerir (class adı değişse bile sabit kalır). Bu kalıp gövde
# taramasında bir SINIR işaretidir: karşılaşıldığı an makale gövdesi bitmiş
# demektir, o elemandan itibaren hiçbir şey gövdeye eklenmez.
REL_ARTICLE_LINK_RE = re.compile(r"newsdetail\.php3\?idNews=")

# Yorum yazma/yorumları görüntüleme linkleri (class adından bağımsız, URL
# kalıbı sabittir). Bunlar makale gövdesinin bir parçası değildir ama gövde
# taramasını durdurmaz (yorum linki genelde başlığın hemen altında, gerçek
# içerikten ÖNCE de görünebilir); yalnızca o tekil eleman atlanır.
COMMENT_LINK_RE = re.compile(r"postcomment\.php3|newscomm-\d+\.php|user\.php3\?idUser=")
# Sosyal paylaşım linkleri (Facebook/Twitter paylaş ikonları)
SHARE_LINK_RE = re.compile(r"facebook\.com/sharer|twitter\.com/intent")

# Gövde taramasını tamamen durduran (makalenin bittiğini işaret eden) tekil
# başlık/etiket metinleri — h2/h3/h4 olmasalar bile (site düzeni bunları düz
# metin/div olarak da render edebilir) eşleştiğinde tarama sonlandırılır.
BOUNDARY_EXACT_TEXTS = {
    "related articles", "recommended", "reader comments",
    "ilgili haberler", "yorumlar", "önerilenler",
}

# Makale gövdesine ait olmayan ama tek başına bir <p>/<li> olarak görünebilen,
# yalnızca bir bağlantıdan/aksiyon metninden ibaret tekil ifadeler. Bunlar
# gövde taramasını DURDURMAZ, sadece o tekil eleman atlanır ("Source" gibi
# tek kelimelik bağlantı paragrafları makale metnine karışmasın diye).
NOISE_EXACT_TEXTS = {
    "source", "sources", "via", "read more", "continue reading",
    "add as a preferred source on google", "post your comment",
    "reply", "read all comments", "share", "tweet", "advertisement",
    "sponsored", "sponsored content", "ads by playwire",
}
NOISE_TEXT_PATTERNS = (
    re.compile(r"^comments?\s*\(\d+\)$", re.I),
    re.compile(r"^total reader comments:.*$", re.I),
)
# "Source (in German)", "Image credit: xyz", "Photo: xyz", "H/T: xyz" gibi
# KISA (en fazla ~6 kelime) atıf/kredi satırları — makale metni değil, dış
# kaynağa/linke işaret eden meta bilgidir. Yalnızca kısa metinlerde eşleşir ki
# "Sources say the phone will launch in January" gibi gerçek cümleler
# yanlışlıkla süzülmesin.
NOISE_PREFIX_RE = re.compile(
    r"^(source|sources|via|credit|image credit|photo credit|image source|"
    r"photo source|h/t|hat tip)\b", re.I
)
MAX_NOISE_PREFIX_WORDS = 6

# Bilinen reklam/izleyici ağı domain veya yol parçaları — bu ipuçlarını taşıyan
# <img>/<iframe> src'leri, ana metin dışında kalsalar bile ASLA görsel/video
# olarak eklenmez (ör. banner reklamlar, izleme pikselleri).
AD_SRC_HINTS = (
    "doubleclick", "googlesyndication", "googleadservices", "adservice",
    "playwire", "taboola", "outbrain", "criteo", "amazon-adsystem",
    "assets12/i/logo", "assets12/i/playwire",
)

# Video gömme kaynağı olarak kabul edilen, bilinen video barındırma servisleri.
# Bunların DIŞINDAKİ hiçbir <iframe> video olarak kabul edilmez (reklam/anket/
# widget iframe'leri video sanılıp içeriğe sızmasın diye).
VIDEO_HOST_HINTS = (
    "youtube.com/embed", "youtube-nocookie.com/embed", "player.vimeo.com",
    "gsmarena.com/videoplayer", "fdn.gsmarena.com",
)


def _is_ad_src(src: str) -> bool:
    low = src.lower()
    return any(hint in low for hint in AD_SRC_HINTS)


def _is_known_video_host(src: str) -> bool:
    low = src.lower()
    return any(hint in low for hint in VIDEO_HOST_HINTS)





def _element_text_norm(el) -> str:
    return el.get_text(" ", strip=True).strip().lower()


def _clean_paragraph_text(el) -> str:
    """Bir p/li elemanının TAM ve temiz metnini döndürür. get_text(strip=True)
    kullanmak, link/etiket sınırlarında boşluğu tamamen kaybederek
    "anearlier reportinline" gibi kelimelerin birbirine yapışmasına yol açar;
    bunun yerine elemanlar arasına boşluk konur, sonra fazla boşluklar ve
    noktalama işaretinden önceki gereksiz boşluk temizlenir."""
    text = el.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([.,!?;:%])", r"\1", text)
    return text


def _is_boundary_marker(el) -> bool:
    """Eleman, 'İlgili haberler'/'Önerilenler'/'Yorumlar' bölümünün başlangıcını
    mı işaret ediyor? (h2/h3/h4 olsun ya da olmasın, ayrıca ilgili-haber
    linkleri her zaman bu kalıptadır)."""
    text = _element_text_norm(el)
    if text in BOUNDARY_EXACT_TEXTS:
        return True
    for a in el.find_all("a", href=True) if hasattr(el, "find_all") else []:
        if REL_ARTICLE_LINK_RE.search(a["href"]):
            return True
    if el.name == "a" and el.get("href") and REL_ARTICLE_LINK_RE.search(el["href"]):
        return True
    return False


def _is_inline_noise(el) -> bool:
    """Eleman, gövdeye ait olmayan tekil bir gürültü parçası mı (yorum/paylaşım
    linki, "Source" / "Source (in German)" gibi kısa bağlantı-atıf metni)?
    Tarama durdurulmaz, sadece bu eleman atlanır."""
    text = _element_text_norm(el)
    if not text:
        return True
    if text in NOISE_EXACT_TEXTS:
        return True
    if any(pattern.match(text) for pattern in NOISE_TEXT_PATTERNS):
        return True
    word_count = len(text.split())
    if word_count <= MAX_NOISE_PREFIX_WORDS and NOISE_PREFIX_RE.match(text):
        return True
    links = el.find_all("a", href=True) if hasattr(el, "find_all") else []
    if el.name == "a" and el.get("href"):
        links = list(links) + [el]
    for a in links:
        href = a["href"]
        if COMMENT_LINK_RE.search(href) or SHARE_LINK_RE.search(href):
            return True
    # Eleman, içinde başka hiçbir gerçek cümle metni olmadan TAMAMEN tek bir
    # linkten ibaretse (metni linkin kendi metniyle aynıysa), bu her zaman bir
    # "Source" / "Read more" / resim altyazısı-linki tarzı atıftır — gerçek bir
    # makale cümlesi asla sadece kendi linkinin metninden ibaret olmaz. Kısa
    # gerçek cümleler içindeki tekil linklere (ör. "...was already spotted
    # [online].") burada DOKUNULMAZ, çünkü o durumda eleman metni linkin
    # metninden daha uzundur.
    if len(links) == 1:
        link_text = links[0].get_text(" ", strip=True).strip().lower()
        if link_text and link_text == text:
            return True
    return False


def _ancestor_has_hint(el) -> bool:
    """Eleman, istenmeyen bir bölüm (yorum/fiyat/ilgili haber vb.) içinde mi kontrol eder."""
    for parent in el.parents:
        if not hasattr(parent, "get"):
            continue
        classes = parent.get("class") or []
        combined = " ".join(classes).lower() + " " + (parent.get("id") or "").lower()
        if any(hint in combined for hint in SKIP_ANCESTOR_HINTS):
            return True
    return False


def _extract_from_container(container) -> tuple[list[str], list[dict], list[dict]]:
    """Bilinen bir kapsayıcı elemandan paragraf, görsel ve VİDEOLARI, belge
    sırasına göre, 'İlgili haberler/Yorumlar' sınırında durarak ve yorum/
    paylaşım/reklam gibi tekil gürültü elemanlarını atlayarak çıkarır. Görsel
    ve videolar URL'ye göre tekilleştirilir; bilinen video barındırma
    servisleri (YouTube/Vimeo) dışındaki hiçbir iframe video sayılmaz (reklam/
    anket widget'ları sızmasın diye)."""
    paragraphs: list[str] = []
    images: list[dict] = []
    videos: list[dict] = []
    seen_image_urls: set[str] = set()
    seen_video_urls: set[str] = set()
    for el in container.find_all(["p", "li", "img", "iframe", "video"]):
        if el.find_parent("table") or _ancestor_has_hint(el):
            continue
        if _is_boundary_marker(el):
            break
        if el.name == "img":
            src = el.get("src") or el.get("data-src") or ""
            if src and not _is_ad_src(src) and src not in seen_image_urls:
                seen_image_urls.add(src)
                images.append({"url": src, "alt": el.get("alt", "")})
            continue
        if el.name == "iframe":
            src = el.get("src") or el.get("data-src") or ""
            if src and _is_known_video_host(src) and src not in seen_video_urls:
                seen_video_urls.add(src)
                videos.append({"url": src, "title": el.get("title", "")})
            continue
        if el.name == "video":
            src = el.get("src") or ""
            if not src:
                source_tag = el.find("source", src=True)
                src = source_tag.get("src", "") if source_tag else ""
            if src and not _is_ad_src(src) and src not in seen_video_urls:
                seen_video_urls.add(src)
                videos.append({"url": src, "title": el.get("title", "")})
            continue
        if _is_inline_noise(el):
            continue
        text = _clean_paragraph_text(el)
        if text and not any(s in text.lower() for s in DISCLOSURE_SNIPPETS) and text not in paragraphs:
            paragraphs.append(text)
    return paragraphs, images, videos


def _extract_body_fallback(soup, h1) -> tuple[list[str], list[dict], list[dict]]:
    """Bilinen kapsayıcı sınıfları eşleşmezse (site düzeni değiştiyse), h1'den
    itibaren belgeyi tarayarak yorumlar/ilgili haberler/fiyat kutularına/
    reklamlara girmeden makalenin TÜM gövde paragraflarını, görsellerini ve
    VİDEOLARINI toplar."""
    paragraphs: list[str] = []
    images: list[dict] = []
    videos: list[dict] = []
    seen_image_urls: set[str] = set()
    seen_video_urls: set[str] = set()
    if not h1:
        return paragraphs, images, videos
    for el in h1.find_all_next(["p", "li", "h2", "h3", "h4", "img", "iframe", "video"]):
        if el.find_parent("table") or _ancestor_has_hint(el):
            continue
        # Sınır kontrolü YALNIZCA h2/h3/h4 metniyle değil, "İlgili haberler" /
        # "Yorumlar" bölümünün gerçek URL imzasıyla da yapılır — bu bölümler
        # sitede her zaman başlık etiketiyle (h2-h4) render edilmeyebilir.
        if _is_boundary_marker(el):
            break
        if el.name in ("h2", "h3", "h4"):
            heading_text = el.get_text(strip=True).lower()
            if any(marker in heading_text for marker in STOP_HEADING_MARKERS):
                break
            # Diğer alt başlıklar (fiyat kutusu ürün adları gibi) gövdeye eklenmez, atlanır
            continue
        if el.name == "img":
            src = el.get("src") or el.get("data-src") or ""
            if src and not _is_ad_src(src) and src not in seen_image_urls:
                seen_image_urls.add(src)
                images.append({"url": src, "alt": el.get("alt", "")})
            continue
        if el.name == "iframe":
            src = el.get("src") or el.get("data-src") or ""
            if src and _is_known_video_host(src) and src not in seen_video_urls:
                seen_video_urls.add(src)
                videos.append({"url": src, "title": el.get("title", "")})
            continue
        if el.name == "video":
            src = el.get("src") or ""
            if not src:
                source_tag = el.find("source", src=True)
                src = source_tag.get("src", "") if source_tag else ""
            if src and not _is_ad_src(src) and src not in seen_video_urls:
                seen_video_urls.add(src)
                videos.append({"url": src, "title": el.get("title", "")})
            continue
        if _is_inline_noise(el):
            # Yorum yazma/yorumları görüntüleme linki, "Source" gibi tek başına
            # bağlantı paragrafı vb. — makale gövdesine ait değil, atla.
            continue
        text = _clean_paragraph_text(el)
        if not text or any(s in text.lower() for s in DISCLOSURE_SNIPPETS):
            continue
        if text not in paragraphs:
            paragraphs.append(text)
    return paragraphs, images, videos


def extract_body(soup, h1) -> tuple[list[str], list[dict], list[dict]]:
    """Makalenin TAM gövde metnini, görsellerini ve VİDEOLARINI eksiksiz
    şekilde çıkarır. Önce bilinen kapsayıcı class'ları dener; yeterli içerik
    bulunamazsa (ör. site class adlarını değiştirmişse) h1 tabanlı sağlam
    fallback'e düşer."""
    for tag, attrs in BODY_CONTAINER_CANDIDATES:
        container = soup.find(tag, attrs=attrs) if attrs else soup.find(tag)
        if container:
            paragraphs, images, videos = _extract_from_container(container)
            if len(paragraphs) >= 2:
                return paragraphs, images, videos
    # Fallback: h1'den itibaren tüm belgeyi tara
    return _extract_body_fallback(soup, h1)


@dataclass
class Article:
    url: str
    slug: str
    title: str
    news_id: str = ""
    date: str = ""
    author: str = ""
    tags: list[str] = field(default_factory=list)
    body_paragraphs: list[str] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    videos: list[dict] = field(default_factory=list)
    fetched_at: str = ""
    # Türkçeye çevrilmiş alanlar
    title_tr: str = ""
    body_paragraphs_tr: list[str] = field(default_factory=list)
    tags_tr: list[str] = field(default_factory=list)


def fetch(url: str) -> str:
    resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _filename_from_url(url: str, content_type: str = "") -> str:
    """URL yolundan bir dosya adı çıkarır (ör. '.../inline/-1200/gsmarena_001.jpg'
    -> 'gsmarena_001.jpg'). Uzantı bilinen bir görsel uzantısı değilse (sorgu
    parametreli/uzantısız adres), Content-Type başlığından tahmin edilir;
    o da yoksa .jpg varsayılır."""
    raw_name = Path(urlparse(url).path).name or "gorsel"
    stem = Path(raw_name).stem or "gorsel"
    ext = Path(raw_name).suffix.lower()
    # Sadece harf/rakam/tire/alt çizgi bırak (URL parçası güvenilmez olabilir)
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", stem)[:80] or "gorsel"
    if ext not in KNOWN_IMAGE_EXTS:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip()) if content_type else None
        ext = guessed if guessed in KNOWN_IMAGE_EXTS else IMAGE_EXT_FALLBACK
    return f"{stem}{ext}"


def download_image(url: str, dest_dir: Path, used_names: set[str]) -> str | None:
    """Bir görseli indirip dest_dir altına kaydeder. Aynı makale içinde
    farklı görseller aynı dosya adına düşerse (ör. iki farklı boyut
    varyantının aynı temel adı taşıması), adın sonuna kısa bir içerik
    özeti eklenerek çakışma önlenir. Başarısız olursa None döner (çağıran
    taraf bu durumda orijinal uzak URL'e düşer, akış hiçbir zaman durmaz)."""
    try:
        resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        content = resp.content
        if not content:
            return None
        filename = _filename_from_url(url, resp.headers.get("Content-Type", ""))
        if filename in used_names:
            digest = hashlib.sha256(content).hexdigest()[:8]
            stem = Path(filename).stem
            ext = Path(filename).suffix
            filename = f"{stem}-{digest}{ext}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / filename).write_bytes(content)
        used_names.add(filename)
        return filename
    except Exception as exc:  # noqa: BLE001 — tek görsel indirme hatası akışı durdurmasın
        print(f"    [görsel-hata] {url} indirilemedi: {exc}")
        return None


def localize_images(article: Article) -> Article:
    """Makalenin article.images listesindeki her görseli data/images/<slug>/
    altına indirir ve URL'sini yayınlanan (GitHub Pages) adresle değiştirir,
    ör: https://umutevicom-commits.github.io/tarihte/data/images/<slug>/<dosya>.
    İndirme başarısız olan tekil bir görsel varsa (ağ hatası vb.) o görsel
    için sadece orijinal GSMArena URL'i korunur; diğer görseller ve makalenin
    geri kalanı etkilenmez.

    İDEMPOTENT: bir görsel zaten doğru yayın adresiyle işaretlenmiş VE
    karşılık gelen dosya diskte gerçekten mevcutsa tekrar indirilmez (script
    tekrar tekrar güvenle çalıştırılabilir). Adres "yerel" görünüyor ama
    dosya diskte yoksa (ör. önceki hatalı bir çalıştırmadan kalan bozuk
    kayıt) bu görsel eksik/onarılamaz kabul edilir ve olduğu gibi bırakılır
    — çağıran taraf (localize_existing_images.py) böyle durumları makale
    sayfasını yeniden çekip orijinal adresi kurtararak onarabilir."""
    if not article.images:
        return article
    dest_dir = IMAGES_DIR / article.slug
    prefix = f"{PAGES_BASE_URL}/data/images/{article.slug}/"
    used_names: set[str] = set()
    localized: list[dict] = []
    for img in article.images:
        remote_url = img.get("url", "")
        if not remote_url:
            continue
        if remote_url.startswith(prefix):
            existing_name = remote_url[len(prefix):]
            used_names.add(existing_name)
            localized.append(img)  # zaten doğru adres; dosya kontrolü çağıran tarafta
            continue
        filename = download_image(remote_url, dest_dir, used_names)
        if filename:
            public_url = f"{prefix}{filename}"
            localized.append({"url": public_url, "alt": img.get("alt", "")})
        else:
            # İndirilemedi: orijinal uzak URL'e düş, görsel yine de RSS'te yer alır
            localized.append(img)
        time.sleep(0.2)
    article.images = localized
    return article


def image_is_fully_localized(article: Article) -> bool:
    """Makalenin TÜM görselleri doğru yayın adresiyle işaretli VE
    karşılık gelen dosyalar diskte gerçekten var mı? Değilse (adres eksik/
    bozuk ya da dosya hiç indirilmemiş) False döner ve makalenin
    yeniden onarılması gerekir."""
    if not article.images:
        return True
    prefix = f"{PAGES_BASE_URL}/data/images/{article.slug}/"
    for img in article.images:
        url = img.get("url", "")
        if not url.startswith(prefix):
            return False
        filename = url[len(prefix):]
        if not (IMAGES_DIR / article.slug / filename).exists():
            return False
    return True


def parse_article_links(html: str) -> list[str]:
    """Haber listeleme sayfasından makale URL'lerini çıkarır."""
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ARTICLE_LINK_RE.match(href):
            full = f"{BASE_URL}/{href}"
            if full not in seen:
                seen.add(full)
                links.append(full)
    return links


def parse_listing_dates(html: str) -> dict[str, str]:
    """Listeleme sayfasındaki her haberin yanında zaten görünen yayın tarihini
    ('29 August 2026' gibi) haber ID'sine göre çıkarır. Class adına değil,
    sabit "newscomm-<id>.php" yorum linkine dayanır: tarih metni bu linkin
    bulunduğu üst elemanın metni içindedir. Bu sayede, haberin YENİ olup
    olmadığını anlamak için makale sayfasını hiç çekmeye gerek kalmaz."""
    soup = BeautifulSoup(html, "html.parser")
    id_to_date: dict[str, str] = {}
    for a in soup.find_all("a", href=LISTING_COMMENT_ID_RE):
        match = LISTING_COMMENT_ID_RE.search(a["href"])
        if not match or not a.parent:
            continue
        news_id = match.group(1)
        if news_id in id_to_date:
            continue
        container_text = a.parent.get_text(" ", strip=True)
        date_match = DATE_TEXT_RE.search(container_text)
        if date_match:
            id_to_date[news_id] = date_match.group(0)
    return id_to_date


def parse_listing_items(html: str) -> list[tuple[str, str, str]]:
    """Listeleme sayfasındaki haberleri, sayfadaki SIRAYA (en yeniden en
    eskiye) göre (news_id, url, date_str) üçlüleri olarak döndürür. date_str
    bulunamazsa boş string olur (o öğe için ihtiyatlı davranılıp tarih
    kontrolü atlanmaz; işlenmesi gerekiyorsa makale sayfası zaten tam tarihi
    de verecektir)."""
    id_to_date = parse_listing_dates(html)
    items: list[tuple[str, str, str]] = []
    seen_ids: set[str] = set()
    for link in parse_article_links(html):
        match = ARTICLE_ID_RE.search(link)
        if not match:
            continue
        news_id = match.group(1)
        if news_id in seen_ids:
            continue
        seen_ids.add(news_id)
        items.append((news_id, link, id_to_date.get(news_id, "")))
    return items


def is_same_day_as_today(date_str: str) -> bool | None:
    """date_str ('29 August 2026') bugünün tarihiyle (UTC) aynı gün mü?
    Ayrıştırılamazsa None döner (bilinmiyor demektir; çağıran taraf bu
    durumda temkinli davranıp o haberi yine de işlemeyi seçebilir)."""
    if not date_str:
        return None
    try:
        parsed = datetime.strptime(date_str, "%d %B %Y").date()
    except ValueError:
        return None
    return parsed == datetime.now(timezone.utc).date()


def parse_pagination(html: str) -> str | None:
    """Sonraki sayfa linkini döndürür, yoksa None."""
    soup = BeautifulSoup(html, "html.parser")
    nav = soup.find("div", class_="nav-pages")
    if nav:
        for a in nav.find_all("a", href=True):
            text = a.get_text(strip=True)
            if text in ("►", "Next", "»"):
                return f"{BASE_URL}/{a['href']}"
    # Fallback: iPage parametresini içeren link
    for a in soup.find_all("a", href=True):
        if "iPage" in a["href"] and a.get_text(strip=True) == "►":
            return f"{BASE_URL}/{a['href']}"
    return None


def extract_domain_from_url(url: str) -> str:
    """URL'den domain kısmını çıkarır (gsmarena.com veya arenaev.com)."""
    match = re.match(r"https?://([^/]+)", url)
    return match.group(1) if match else BASE_URL.removeprefix("https://")


def fix_url(src: str, page_url: str) -> str:
    """Göreceli URL'yi mutlak URL'ye çevirir."""
    if src.startswith("http"):
        return src
    domain = extract_domain_from_url(page_url)
    if src.startswith("/"):
        return f"https://{domain}{src}"
    return f"https://{domain}/{src}"


def parse_article(url: str, html: str) -> Article:
    """Tek bir makale sayfasının TAM içeriğini ayrıştırır."""
    soup = BeautifulSoup(html, "html.parser")
    slug = url.rsplit("/", 1)[-1].removesuffix(".php").removesuffix("-news")

    # Başlık
    h1 = soup.find("h1")
    title = _clean_paragraph_text(h1) if h1 else slug.replace("_", " ").title()

    # Yazar — önce href="author.php3?idAuthor=..." linkini ara (class adından bağımsız,
    # sayfa düzeni değişse de kırılmaz). Bulamazsa meta tag'e, o da yoksa eski
    # article-tags fallback'ine düş.
    author = ""
    author_link = soup.find("a", href=AUTHOR_LINK_RE)
    if author_link:
        author = author_link.get_text(strip=True)
    if not author:
        author_meta = soup.find("meta", attrs={"name": "author"})
        if author_meta:
            author = author_meta.get("content", "")
    if not author:
        tags_div = soup.find("div", class_="article-tags")
        if tags_div:
            first_link = tags_div.find("a")
            if first_link:
                author = first_link.get_text(strip=True)

    # Tarih — yazar linkinin bulunduğu üst kapsayıcının metninden tarih deseni
    # ("29 August 2026" gibi) çıkar. Bu, tarihin span/time/düz metin olmasından
    # ve class adından bağımsız çalışır.
    date_str = ""
    if author_link and author_link.parent:
        container_text = author_link.parent.get_text(" ", strip=True)
        date_match = DATE_TEXT_RE.search(container_text)
        if date_match:
            date_str = date_match.group(0)
    if not date_str:
        # Fallback: eski .dtreviewed / .float-left tabanlı arama
        date_el = soup.find("span", class_="dtreviewed") or soup.find(
            "div", class_="dtreviewed"
        )
        if date_el:
            date_str = date_el.get_text(strip=True)
        else:
            tags_div = soup.find("div", class_="article-tags")
            if tags_div:
                text = tags_div.get_text(" ", strip=True)
                date_match = re.search(r"(\d{1,2}\s+\w+\s+\d{4})", text)
                if date_match:
                    date_str = date_match.group(1)

    # Gövde, görseller ve videolar — eksiksiz olacak şekilde extract_body() ile
    # çıkarılır (bilinen kapsayıcı bulunamazsa h1 tabanlı fallback devreye
    # girer, hiçbir paragraf/görsel/video atlanmaz)
    body_paragraphs, raw_images, raw_videos = extract_body(soup, h1)
    images: list[dict] = [
        {"url": fix_url(img["url"], url), "alt": img.get("alt", "")}
        for img in raw_images
        if img.get("url")
    ]
    videos: list[dict] = [
        {"url": fix_url(vid["url"], url), "title": vid.get("title", "")}
        for vid in raw_videos
        if vid.get("url")
    ]

    # Etiketler — önce href="news.php3?sTag=..." kalıbındaki linkleri topla (class
    # adından bağımsız). Bulamazsa eski .article-tags tabanlı mantığa düş.
    tags: list[str] = []
    seen_tags: set[str] = set()
    for a in soup.find_all("a", href=TAG_LINK_RE):
        tag = a.get_text(strip=True)
        if tag and tag != author and tag != date_str and tag not in seen_tags:
            seen_tags.add(tag)
            tags.append(tag)
    if not tags:
        tags_div = soup.find("div", class_="article-tags")
        if tags_div:
            all_links = tags_div.find_all("a")
            # İlk link genelde yazar, geri kalanı etiket
            start = 1 if author and all_links and all_links[0].get_text(strip=True) == author else 0
            for a in all_links[start:]:
                tag = a.get_text(strip=True)
                if tag and tag != date_str:
                    tags.append(tag)

    id_match = ARTICLE_ID_RE.search(url)
    news_id = id_match.group(1) if id_match else slug

    return Article(
        url=url,
        slug=slug,
        title=title,
        news_id=news_id,
        date=date_str,
        author=author,
        tags=tags,
        body_paragraphs=body_paragraphs,
        images=images,
        videos=videos,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def translate_article(article: Article) -> Article:
    """Makalenin başlık, gövde ve etiketlerini Türkçeye çevirir.
    Orijinal alanlara dokunmaz, _tr sonekli alanları doldurur."""
    if not ENABLE_TRANSLATION:
        return article
    print(f"  [çevriliyor] {article.slug}")
    article.title_tr = translate_text(article.title)
    article.body_paragraphs_tr = [
        translate_text(p) for p in article.body_paragraphs
    ]
    article.tags_tr = [translate_text(t) for t in article.tags]
    print(f"  [çeviri-tamam] {article.slug}")
    return article


def article_to_markdown(article: Article) -> str:
    # Türkçe başlık varsa onu kullan, yoksa orijinal İngilizce
    display_title = article.title_tr or article.title
    lines: list[str] = [f"# {display_title}", ""]
    # NOT: Yazar/Tarih/Etiketler/yorumlar/ilgili haberler/reklamlar/dış linkler
    # bilinçli olarak makale içeriğinde GÖSTERİLMEZ. Sadece başlık, gövde metni,
    # görseller ve videolar yer alır. Yazar/tarih/etiket bilgileri yalnızca
    # article.json içinde ve RSS feed'inin kendi yapısal alanlarında
    # (pubDate/author/category) bulunur.
    # Türkçe metin varsa onu kullan, yoksa orijinal
    paragraphs = article.body_paragraphs_tr or article.body_paragraphs
    for para in paragraphs:
        lines.append(para)
        lines.append("")
    if article.images:
        lines.append("---")
        lines.append("")
        lines.append("## Görseller")
        lines.append("")
        for img in article.images:
            alt = img["alt"] or "Görsel"
            lines.append(f"![{alt}]({img['url']})")
            lines.append("")
    if article.videos:
        lines.append("---")
        lines.append("")
        lines.append("## Videolar")
        lines.append("")
        for vid in article.videos:
            lines.append(f"🎬 {vid['url']}")
            lines.append("")
    return "\n".join(lines)


def save_article(article: Article) -> None:
    out_dir = OUTPUT_DIR / article.slug
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "article.json").write_text(
        json.dumps(asdict(article), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "article.md").write_text(
        article_to_markdown(article), encoding="utf-8"
    )
    # RSS <item> parçacığı BİR KEZ üretilip önbelleklenir; sonraki her
    # çalıştırmada write_rss() bunu yeniden hesaplamadan doğrudan okur.
    (out_dir / "item.xml").write_text(
        build_item_xml(article), encoding="utf-8"
    )


def _rfc822_date(date_str: str, fallback_iso: str) -> str:
    """'29 August 2026' gibi bir tarihi RSS'in beklediği RFC 822 biçimine çevirir.
    Ayrıştırılamazsa fetched_at (ISO) değerinden üretir, o da yoksa şu anki zamanı kullanır."""
    if date_str:
        try:
            dt = datetime.strptime(date_str, "%d %B %Y").replace(tzinfo=timezone.utc)
            return dt.strftime("%a, %d %b %Y %H:%M:%S %z")
        except ValueError:
            pass
    if fallback_iso:
        try:
            dt = datetime.fromisoformat(fallback_iso)
            return dt.strftime("%a, %d %b %Y %H:%M:%S %z")
        except ValueError:
            pass
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _cdata(html: str) -> str:
    """HTML'i RSS için CDATA bloğuna sarar; içeride kaçınılmaz olarak geçen
    ']]>' dizisini güvenli hale getirir."""
    safe = html.replace("]]>", "]]&gt;")
    return f"<![CDATA[{safe}]]>"


def article_to_html(article: Article) -> str:
    """Makaleyi eksiksiz, temiz ve premium seviye <p> tabanlı tam bir blog
    makalesi HTML'i olarak üretir. Türkçe içerik varsa onu, yoksa orijinali
    kullanır; hiçbir paragraf atlanmaz. Yazar/tarih/etiket, yorum, ilgili
    haberler/reklam/dış link gibi hiçbir şey gövdeye DAHIL EDİLMEZ — yalnızca
    başlık (RSS <title>), gövde metni, görseller ve videolar bulunur. Gövde
    paragrafları düz metin olarak yazıldığından (etiketler değil) çıktıda
    zaten hiçbir <a> linki oluşmaz.

    ÖNEMLİ (bozulma/metin-kayması düzeltmesi): <img>/<video>/<iframe>
    etiketlerinin TARAYICI VARSAYILAN CSS'i "inline"dır. Kendi sitemizde
    sorun çıkarmasa da RSS okuyucu uygulamaları (RSS.app vb.) genelde kendi
    stil sayfalarını uygular ve <p> sarmalayıcıyı yok sayıp bu elemanları
    paragraf metniyle AYNI SATIR AKIŞINA sokabilir; bu da ekran görüntüsünde
    görülen "metin videonun soluna dar bir sütun halinde kayıyor" bozulmasına
    yol açar. Bunu okuyucu uygulamasından bağımsız, kalıcı olarak önlemek
    için her görsel/video/iframe'e display:block + width:100% + clear:both
    satır-içi (inline) stil EKLENİR — böylece hangi istemcide açılırsa
    açılsın eleman kendi satırında, tam genişlikte durur ve yanına metin
    kayamaz. iframe video gömmeleri ayrıca sabit en-boy oranlı (16:9) bir
    sarmalayıcı içine alınır; boyutsuz bir iframe bazı okuyucularda 0
    yükseklikte/varsayılan küçük kutuda render olup video alanının "kopuk"
    görünmesine neden olur."""
    paragraphs = article.body_paragraphs_tr or article.body_paragraphs
    parts: list[str] = []
    for para in paragraphs:
        parts.append(f"<p>{_xml_escape(para)}</p>")
    for img in article.images:
        src = img.get("url", "")
        if not src:
            continue
        alt = _xml_escape(img.get("alt") or "Görsel")
        parts.append(
            f'<p style="clear:both;margin:16px 0;">'
            f'<img src="{_xml_escape(src)}" alt="{alt}" loading="lazy" '
            f'style="display:block;width:100%;height:auto;max-width:100%;'
            f'margin:0;float:none;clear:both;" /></p>'
        )
    for vid in article.videos:
        src = vid.get("url", "")
        if not src:
            continue
        title = _xml_escape(vid.get("title") or "Video")
        low = src.lower()
        if low.endswith((".mp4", ".webm", ".ogg", ".ogv")):
            mime = {
                ".mp4": "video/mp4",
                ".webm": "video/webm",
                ".ogg": "video/ogg",
                ".ogv": "video/ogg",
            }[next(ext for ext in (".mp4", ".webm", ".ogg", ".ogv") if low.endswith(ext))]
            parts.append(
                f'<p style="clear:both;margin:16px 0;">'
                f'<video controls preload="metadata" title="{title}" '
                f'style="display:block;width:100%;height:auto;max-width:100%;'
                f'aspect-ratio:16/9;margin:0;float:none;clear:both;background:#000;">'
                f'<source src="{_xml_escape(src)}" type="{mime}" /></video></p>'
            )
        else:
            # Sabit en-boy oranlı (16:9) sarmalayıcı: iframe'e genişlik/
            # yükseklik atanmamış olsa bile video alanı her zaman doğru
            # oranda, tam genişlikte ve kendi satırında render olur.
            parts.append(
                '<div style="clear:both;margin:16px 0;position:relative;'
                'width:100%;padding-top:56.25%;overflow:hidden;background:#000;">'
                f'<iframe src="{_xml_escape(src)}" title="{title}" '
                'loading="lazy" allowfullscreen frameborder="0" '
                'style="position:absolute;top:0;left:0;width:100%;height:100%;'
                'display:block;border:0;float:none;"></iframe></div>'
            )
    return "\n".join(parts)


def load_registry() -> dict:
    """Kalıcı kayıt defterini (data/registry.json) okur. Dosya yoksa (ilk
    çalıştırma veya eski bir sürümden geçiş), data/articles altında zaten
    var olan makalelerden BİR KEZE mahsus bir defter oluşturur (migrate_
    registry_from_existing_articles) — böylece daha önce çekilmiş hiçbir
    makale "yeni" sanılıp tekrar işlenmez ve mevcut rss.xml içeriği kaybolmaz."""
    if REGISTRY_PATH.exists():
        try:
            return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"  [kayıt-uyarı] registry.json bozuk, sıfırdan oluşturuluyor: {exc}")
    return migrate_registry_from_existing_articles()


def save_registry(registry: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def migrate_registry_from_existing_articles() -> dict:
    """Eski sürümden (registry.json henüz yokken) geçiş: data/articles
    altındaki her mevcut article.json için bir "ok" kaydı ve gerekiyorsa bir
    item.xml önbelleği oluşturur. Bu, script'in genel yapısını ve daha önce
    biriktirilmiş tüm makaleleri korumak için sadece BİR KEZ çalışır."""
    registry: dict = {}
    if not OUTPUT_DIR.exists():
        return registry
    for json_path in OUTPUT_DIR.glob("*/article.json"):
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            article = Article(**payload)
        except (json.JSONDecodeError, TypeError) as exc:
            print(f"  [geçiş-uyarı] {json_path} okunamadı: {exc}")
            continue
        news_id = article.news_id or (
            ARTICLE_ID_RE.search(article.url).group(1)
            if ARTICLE_ID_RE.search(article.url)
            else article.slug
        )
        item_path = json_path.parent / "item.xml"
        if not item_path.exists():
            item_path.write_text(build_item_xml(article), encoding="utf-8")
        registry[news_id] = {
            "slug": article.slug,
            "url": article.url,
            "date": article.date,
            "fetched_at": article.fetched_at,
            "status": "ok",
            "retries": 0,
            "last_error": "",
            "content_hash": "",
        }
    if registry:
        print(f"[geçiş] {len(registry)} mevcut makaleden registry.json oluşturuldu.")
    return registry


def process_new_article(link: str) -> tuple[Article | None, str, str]:
    """Tek bir haberi baştan sona işler: tam sayfayı çeker, ayrıştırır,
    çevirir, kaydeder. Başarısızlık DURDURMAZ: (None, "", hata_mesajı) döner
    ki çağıran taraf registry'yi "failed" olarak işaretleyip bir sonraki
    çalıştırmada tekrar denesin. Başarılıysa (Article, içerik_hash, "") döner."""
    try:
        article_html = fetch(link)
    except requests.RequestException as exc:
        return None, "", f"sayfa alınamadı: {exc}"
    try:
        content_hash = hashlib.sha256(article_html.encode("utf-8")).hexdigest()
        article = parse_article(link, article_html)
        article = localize_images(article)
        article = translate_article(article)
        save_article(article)
    except Exception as exc:  # noqa: BLE001 — tek haberdeki beklenmeyen hata tüm koşuyu durdurmasın
        return None, "", f"ayrıştırma/çeviri hatası: {exc}"
    return article, content_hash, ""


def build_item_xml(article: Article) -> str:
    """Tek bir makale için RSS <item> XML parçacığını üretir. Bu, her makale
    için SADECE BİR KEZ (ilk işlendiğinde) çağrılır ve sonucu diske
    (item.xml) önbelleklenir; RSS feed'i her çalıştırmada bu parçacığı
    yeniden üretmek yerine olduğu gibi diskten okuyup birleştirir."""
    title = article.title_tr or article.title
    paragraphs = article.body_paragraphs_tr or article.body_paragraphs
    description = paragraphs[0] if paragraphs else ""
    tags = article.tags_tr or article.tags
    categories = "".join(f"<category>{_xml_escape(t)}</category>" for t in tags)
    author_xml = f"<author>{_xml_escape(article.author)}</author>" if article.author else ""
    pub_date = _rfc822_date(article.date, article.fetched_at)
    content_html = article_to_html(article)
    return (
        "    <item>\n"
        f"      <title>{_xml_escape(title)}</title>\n"
        f"      <link>{_xml_escape(article.url)}</link>\n"
        f"      <guid isPermaLink=\"true\">{_xml_escape(article.url)}</guid>\n"
        f"      <pubDate>{pub_date}</pubDate>\n"
        f"      {author_xml}\n"
        f"      {categories}\n"
        f"      <description>{_xml_escape(description)}</description>\n"
        f"      <content:encoded>{_cdata(content_html)}</content:encoded>\n"
        "    </item>"
    )


def build_rss_envelope(items_xml: list[str]) -> str:
    """Önbellekten okunan/az önce üretilen <item> parçacıklarını RSS 2.0
    kanal zarfına (channel envelope) sarar. Sadece zarf (lastBuildDate vb.)
    her çalıştırmada tazelenir; item içerikleri buraya olduğu gibi girer."""
    build_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    channel_link = SITE_URL or BASE_URL
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/">\n'
        "  <channel>\n"
        f"    <title>{_xml_escape(SITE_NAME)}</title>\n"
        f"    <link>{_xml_escape(channel_link)}</link>\n"
        f"    <atom:link href=\"{_xml_escape(channel_link)}/rss.xml\" rel=\"self\" type=\"application/rss+xml\" />\n"
        "    <description>GSMArena haberlerinin eksiksiz Türkçe çevirisi</description>\n"
        "    <language>tr</language>\n"
        f"    <lastBuildDate>{build_date}</lastBuildDate>\n"
        + "\n".join(items_xml)
        + "\n  </channel>\n</rss>\n"
    )


def write_rss(registry: dict) -> None:
    """RSS feed'ini docs/rss.xml olarak yazar. Her başarılı ("ok") kayıt için
    item.xml önbelleğini diskten okur; hiçbir eski makalenin HTML/XML içeriği
    burada yeniden HESAPLANMAZ, sadece dosyadan okunup birleştirilir. Yalnızca
    bu çalıştırmada yeni işlenen makaleler için item.xml az önce üretilmiş
    olur (process_new_article içinde)."""
    ok_entries = [e for e in registry.values() if e.get("status") == "ok"]
    ok_entries.sort(key=lambda e: e.get("fetched_at", ""), reverse=True)
    items_xml: list[str] = []
    for entry in ok_entries[:RSS_ITEM_LIMIT]:
        item_path = OUTPUT_DIR / entry["slug"] / "item.xml"
        try:
            items_xml.append(item_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # Önbellek eksikse (ör. elle silinmiş / eski format), bu tek
            # makale için article.json'dan yeniden üretip önbelleği onar;
            # diğer makaleler etkilenmez.
            json_path = OUTPUT_DIR / entry["slug"] / "article.json"
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
                article = Article(**payload)
                xml = build_item_xml(article)
                item_path.write_text(xml, encoding="utf-8")
                items_xml.append(xml)
            except (json.JSONDecodeError, TypeError, FileNotFoundError) as exc:
                print(f"  [rss-uyarı] {entry['slug']} için item.xml onarılamadı: {exc}")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    RSS_PATH.write_text(build_rss_envelope(items_xml), encoding="utf-8")
    print(f"[rss] {len(items_xml)} makale ile {RSS_PATH} güncellendi.")


def _record_success(registry: dict, news_id: str, article: Article, content_hash: str) -> None:
    registry[news_id] = {
        "slug": article.slug,
        "url": article.url,
        "date": article.date,
        "fetched_at": article.fetched_at,
        "status": "ok",
        "retries": 0,
        "last_error": "",
        "content_hash": content_hash,
    }


def _record_failure(registry: dict, news_id: str, url: str, date_hint: str, error: str) -> None:
    existing = registry.get(news_id, {})
    registry[news_id] = {
        "slug": existing.get("slug") or (url.rsplit("/", 1)[-1].removesuffix(".php")),
        "url": url,
        "date": existing.get("date") or date_hint,
        "fetched_at": existing.get("fetched_at", ""),
        "status": "failed",
        "retries": int(existing.get("retries", 0)) + 1,
        "last_error": error,
        "content_hash": existing.get("content_hash", ""),
    }


def retry_failed(registry: dict) -> int:
    """Önceki çalıştırmalarda başarısız olan haberleri tekrar dener. Hata
    durumunda işlem asla tümüyle durmaz; her haber bağımsız denenir ve
    başarısız kalanlar bir sonraki çalıştırmaya (bir sonraki 2 saatlik
    döngüye) devredilir."""
    failed_ids = [nid for nid, e in registry.items() if e.get("status") == "failed"]
    if not failed_ids:
        return 0
    print(f"[tekrar-deneme] {len(failed_ids)} daha önce başarısız haber tekrar deneniyor.")
    fixed = 0
    for news_id in failed_ids:
        entry = registry[news_id]
        url = entry.get("url", "")
        if not url:
            continue
        print(f"  [tekrar] {news_id} -> {url}")
        article, content_hash, error = process_new_article(url)
        if article:
            _record_success(registry, news_id, article, content_hash)
            fixed += 1
            print(f"    [tamam] artık başarılı: {article.title}")
        else:
            _record_failure(registry, news_id, url, entry.get("date", ""), error)
            print(f"    [hâlâ-başarısız] {error}")
        time.sleep(DELAY_BETWEEN_REQUESTS)
    return fixed


def crawl_new_today() -> tuple[dict, int, int]:
    """Sadece o gün (bugün, UTC) yayınlanan ve daha önce işlenmemiş haberleri
    bulup işler. Tüm siteyi taramaz: listeleme sayfasını en yeniden başlayarak
    okur ve kronolojik sırada ilk kez (a) zaten registry'de "ok" olan bir
    habere ya da (b) bugüne ait olmayan bir tarihe rastladığı an DURUR, çünkü
    listeleme kronolojik olduğundan ondan sonrası zaten eski/işlenmiş demektir."""
    registry = load_registry()
    fixed = retry_failed(registry)

    new_count = 0
    page_url: str | None = NEWS_INDEX_URL
    page_num = 0
    stop = False

    while page_url and not stop:
        page_num += 1
        if page_num > MAX_PAGES:
            print(f"  [limit] {MAX_PAGES} sayfa güvenlik limitine ulaşıldı, durduruluyor.")
            break

        print(f"[liste sayfa {page_num}] {page_url}")
        try:
            html = fetch(page_url)
        except requests.RequestException as exc:
            print(f"  [hata] Listeleme sayfası alınamadı: {exc}")
            break

        items = parse_listing_items(html)
        print(f"  {len(items)} haber bulundu (bu sayfada).")

        for news_id, link, date_str in items:
            existing = registry.get(news_id)
            if existing and existing.get("status") == "ok":
                print(f"  [dur] {news_id} zaten işlenmiş, liste kronolojik: tarama bitti.")
                stop = True
                break

            same_day = is_same_day_as_today(date_str)
            if same_day is False:
                print(f"  [dur] {news_id} bugüne ait değil ({date_str}): tarama bitti.")
                stop = True
                break
            # same_day is None (tarih ayrıştırılamadı) ise temkinli davranılır
            # ve haber yine de işlenir; site düzeni değişmiş olabilir, bir
            # haberi atlamak sessiz veri kaybına yol açar.

            print(f"  [yeni] {news_id} çekiliyor: {link}")
            article, content_hash, error = process_new_article(link)
            if article:
                _record_success(registry, news_id, article, content_hash)
                new_count += 1
                print(
                    f"    [tamam] {article.title} "
                    f"({len(article.body_paragraphs)} paragraf, "
                    f"{len(article.images)} görsel, {len(article.videos)} video)"
                )
            else:
                _record_failure(registry, news_id, link, date_str, error)
                print(f"    [hata] {error} — bir sonraki döngüde tekrar denenecek.")
            time.sleep(DELAY_BETWEEN_REQUESTS)

        if stop:
            break

        next_page = parse_pagination(html)
        if next_page:
            page_url = next_page
            time.sleep(DELAY_BETWEEN_REQUESTS)
        else:
            print("  [bilgi] Sonraki sayfa yok.")
            page_url = None

    save_registry(registry)
    return registry, new_count, fixed


def write_index(registry: dict) -> None:
    """Küçük bir insan-okunur özet indeks dosyası (data/articles/index.json)
    yazar; registry'den üretildiği için tek tek article.json dosyalarını
    tekrar okumaya gerek kalmaz (performans)."""
    ok_entries = [e for e in registry.values() if e.get("status") == "ok"]
    ok_entries.sort(key=lambda e: e.get("fetched_at", ""), reverse=True)
    index_path = OUTPUT_DIR / "index.json"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(ok_entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[indeks] {index_path}")


def main() -> int:
    print("GSMArena makale scraper'ı başlatılıyor (sadece bugünün yeni haberleri)...")
    print(f"Çıktı dizini: {OUTPUT_DIR}")
    print(f"Kayıt defteri: {REGISTRY_PATH}")
    print(f"Türkçe çeviri: {'açık' if ENABLE_TRANSLATION else 'kapalı'}")

    registry, new_count, fixed_count = crawl_new_today()
    write_index(registry)
    write_rss(registry)

    total_ok = sum(1 for e in registry.values() if e.get("status") == "ok")
    total_failed = sum(1 for e in registry.values() if e.get("status") == "failed")
    print(
        f"\n[özet] {new_count} yeni haber işlendi, {fixed_count} eski hata düzeldi, "
        f"toplam {total_ok} başarılı / {total_failed} hâlâ başarısız kayıt var."
    )
    if total_failed:
        print("[bilgi] Başarısız kayıtlar bir sonraki çalıştırmada otomatik tekrar denenecek.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
