#!/usr/bin/env python3
"""
GSMArena makale scraper'ı.

Tüm makale sayfalarını sırayla gezer, her makalenin TAM içeriğini
(başlık, tarih, yazar, gövde metni, görsel URL'leri, etiketler) çeker,
içeriği Türkçeye çevirir ve hem JSON hem de Markdown formatında kaydeder.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from deep_translator import MyMemoryTranslator

BASE_URL = "https://www.gsmarena.com"
NEWS_INDEX_URL = f"{BASE_URL}/news.php3"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "articles"
DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
RSS_PATH = DOCS_DIR / "rss.xml"
SITE_URL = os.environ.get("SITE_URL", BASE_URL).rstrip("/")
SITE_NAME = os.environ.get("SITE_NAME", "GSMArena Türkçe")
RSS_ITEM_LIMIT = 60
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,tr;q=0.8",
}
REQUEST_TIMEOUT = 30
DELAY_BETWEEN_REQUESTS = 1.5  # saniye
MAX_PAGES = int(os.environ.get("GSMARENA_MAX_PAGES", "0")) or None
DELAY_BETWEEN_TRANSLATIONS = 0.5  # saniye
# Çeviri yapılıp yapılmayacağı (1=evet, 0=hayır)
ENABLE_TRANSLATION = os.environ.get("GSMARENA_TRANSLATE", "1") != "0"

MAX_TRANSLATION_CHUNK = 450  # MyMemoryTranslator'ın ~500 karakter limitinin altında

# MyMemory ücretsiz API, anonim (email'siz) istemcilerde günlük ~5.000 karakterlik
# çok düşük bir kotayla sınırlıdır ve bu kota dolduğunda İSTİSNA FIRLATMAZ; bunun
# yerine "MYMEMORY WARNING: ..." gibi bir metni normal çeviri sonucu gibi döndürür.
# Bu, fark edilmeden makale içine İngilizce/hatalı uyarı metninin karışmasına yol
# açar. Bir e-posta adresi (de= parametresi) gönderildiğinde günlük kota ~50.000
# karaktere çıkar. GSMARENA_TRANSLATE_EMAIL ortam değişkeni ile ayarlanabilir.
TRANSLATE_EMAIL = os.environ.get("GSMARENA_TRANSLATE_EMAIL") or None

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
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


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

    return Article(
        url=url,
        slug=slug,
        title=title,
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
    zaten hiçbir <a> linki oluşmaz."""
    paragraphs = article.body_paragraphs_tr or article.body_paragraphs
    parts: list[str] = []
    for para in paragraphs:
        parts.append(f"<p>{_xml_escape(para)}</p>")
    for img in article.images:
        src = img.get("url", "")
        if not src:
            continue
        alt = _xml_escape(img.get("alt") or "Görsel")
        parts.append(f'<p><img src="{_xml_escape(src)}" alt="{alt}" loading="lazy" /></p>')
    for vid in article.videos:
        src = vid.get("url", "")
        if not src:
            continue
        title = _xml_escape(vid.get("title") or "Video")
        low = src.lower()
        if low.endswith((".mp4", ".webm", ".ogg", ".ogv")):
            parts.append(
                f'<p><video controls preload="metadata" title="{title}">'
                f'<source src="{_xml_escape(src)}" /></video></p>'
            )
        else:
            parts.append(
                f'<p><iframe src="{_xml_escape(src)}" title="{title}" '
                f'loading="lazy" allowfullscreen frameborder="0"></iframe></p>'
            )
    return "\n".join(parts)


def load_all_articles() -> list[Article]:
    """data/articles altındaki tüm article.json dosyalarını okuyup Article listesine çevirir."""
    articles: list[Article] = []
    if not OUTPUT_DIR.exists():
        return articles
    for json_path in OUTPUT_DIR.glob("*/article.json"):
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            articles.append(Article(**payload))
        except (json.JSONDecodeError, TypeError) as exc:
            print(f"  [rss-uyarı] {json_path} okunamadı: {exc}")
    articles.sort(key=lambda a: a.fetched_at, reverse=True)
    return articles


def build_rss(articles: list[Article]) -> str:
    """Makale listesinden RSS 2.0 feed'i (XML metni) üretir. Her item, kısa bir
    özet (<description>) yanında, eksiksiz çevrilmiş, temiz <p> tabanlı tam bir
    blog makalesi HTML'i (<content:encoded>, CDATA içinde) taşır."""
    items_xml: list[str] = []
    for article in articles[:RSS_ITEM_LIMIT]:
        title = article.title_tr or article.title
        paragraphs = article.body_paragraphs_tr or article.body_paragraphs
        description = paragraphs[0] if paragraphs else ""
        tags = article.tags_tr or article.tags
        categories = "".join(f"<category>{_xml_escape(t)}</category>" for t in tags)
        author_xml = f"<author>{_xml_escape(article.author)}</author>" if article.author else ""
        pub_date = _rfc822_date(article.date, article.fetched_at)
        content_html = article_to_html(article)
        items_xml.append(
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


def write_rss(articles: list[Article]) -> None:
    """RSS feed'ini docs/rss.xml olarak yazar."""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    RSS_PATH.write_text(build_rss(articles), encoding="utf-8")
    print(f"[rss] {len(articles[:RSS_ITEM_LIMIT])} makale ile {RSS_PATH} güncellendi.")


def crawl() -> list[Article]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    all_articles: list[Article] = []
    page_url: str | None = NEWS_INDEX_URL
    page_num = 0

    while page_url:
        page_num += 1
        if MAX_PAGES and page_num > MAX_PAGES:
            print(f"  [limit] {MAX_PAGES} sayfa limitine ulaşıldı.")
            break

        print(f"[sayfa {page_num}] {page_url}")
        try:
            html = fetch(page_url)
        except requests.RequestException as exc:
            print(f"  [hata] Sayfa alınamadı: {exc}")
            break

        links = parse_article_links(html)
        print(f"  {len(links)} makale linki bulundu.")

        for link in links:
            slug = link.rsplit("/", 1)[-1].removesuffix(".php")
            json_path = OUTPUT_DIR / slug / "article.json"
            if json_path.exists():
                print(f"  [atla] {slug} (zaten kayıtlı)")
                continue

            print(f"  [çekiliyor] {slug}")
            try:
                article_html = fetch(link)
            except requests.RequestException as exc:
                print(f"  [hata] Makale alınamadı: {exc}")
                continue

            article = parse_article(link, article_html)
            article = translate_article(article)
            save_article(article)
            all_articles.append(article)
            print(
                f"  [tamam] {article.title} "
                f"({len(article.body_paragraphs)} paragraf, "
                f"{len(article.images)} görsel, {len(article.videos)} video)"
            )
            time.sleep(DELAY_BETWEEN_REQUESTS)

        next_page = parse_pagination(html)
        if next_page:
            page_url = next_page
            time.sleep(DELAY_BETWEEN_REQUESTS)
        else:
            print("  [bilgi] Sonraki sayfa yok, tarama tamam.")
            page_url = None

    index_path = OUTPUT_DIR / "index.json"
    index_data = [
        {
            "slug": a.slug,
            "title": a.title,
            "date": a.date,
            "author": a.author,
            "url": a.url,
            "paragraph_count": len(a.body_paragraphs),
            "image_count": len(a.images),
            "video_count": len(a.videos),
        }
        for a in all_articles
    ]
    index_path.write_text(
        json.dumps(index_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[toplam] {len(all_articles)} makale çekildi.")
    print(f"[indeks] {index_path}")

    # RSS feed'i sadece bu çalıştırmada çekilenlerden değil, arşivdeki TÜM
    # makalelerden üretilir (böylece feed her çalıştırmada dolu kalır).
    write_rss(load_all_articles())

    return all_articles


def main() -> int:
    print("GSMArena makale scraper'ı başlatılıyor...")
    print(f"Çıktı dizini: {OUTPUT_DIR}")
    print(f"Türkçe çeviri: {'açık' if ENABLE_TRANSLATION else 'kapalı'}")
    articles = crawl()
    if not articles:
        print("[uyarı] Hiç makale çekilemedi.")
        return 1
    print(f"[başarılı] {len(articles)} makale tam içerikle kaydedildi.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
