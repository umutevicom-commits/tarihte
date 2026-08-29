#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GSMArena -> Otomatik Türkçe RSS + Makale Üretici
==================================================================

GSMArena.com'un KENDİ RESMİ RSS akışını (https://www.gsmarena.com/rss-news-reviews.php3)
kaynak olarak kullanır; her haberin başlığını ve GSMArena'nın kendi yayınladığı
özet metnini ücretsiz Google Translate motoru (deep-translator) ile Türkçeye
çevirir, SEO/Schema.org uyumlu tekil makale sayfaları ve bir RSS akışı üretir.

ÖNEMLİ - NEDEN TAM MAKALE METNİ ÇEKİLMİYOR
--------------------------------------------------------------------------
Bu script BİLİNÇLİ olarak GSMArena'nın makale sayfalarını kazımaz (scrape
etmez) ve tam makale metnini kopyalamaz. Bunun yerine yalnızca GSMArena'nın
KENDİ YAYINLADIĞI ve RSS ile herkese açık şekilde dağıttığı başlık + özet
metnini kullanır. Bu, telif hakkına saygılı, sürdürülebilir bir haber
agregatörü modelidir (Google News, Feedly vb. aynı modeli kullanır).

Her makalede KAYNAK LİNKİ ZORUNLU olarak eklenir (footer + meta alanı).
Bu link kasıtlı olarak kaldırılamaz; kaldırılırsa içerik GSMArena'nın
telif hakkını ihlal eden, izinsiz bir kopya haline gelir.

Üretilenler:
  docs/rss.xml                -> Ana RSS akışı (Türkçe, SEO uyumlu)
  docs/articles/<slug>.html   -> Her haber için tam SEO/Schema.org sayfası
  data/history.json           -> Yayınlanan/çevrilmiş haberlerin kaydı
                                  (mükerrer önleme + yeniden çeviri israfını önler)
"""

import os
import re
import json
import html
import time
import hashlib
import datetime as dt
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

import requests

# --------------------------------------------------------------------------
# AYARLAR
# --------------------------------------------------------------------------

SITE_URL = os.environ.get("SITE_URL", "https://example.github.io/gsmarena-tr")
SITE_NAME = os.environ.get("SITE_NAME", "Mobil Teknoloji Haberleri")
SITE_DESCRIPTION = (
    "GSMArena.com kaynaklı, İngilizceden Türkçeye otomatik çevrilen; "
    "her habere kaynak atfı ve orijinal bağlantı içeren mobil teknoloji "
    "haber akışı."
)
LANG = "tr"
OUTPUT_DIR = "docs"
ARTICLES_DIR = os.path.join(OUTPUT_DIR, "articles")
HISTORY_FILE = os.path.join("data", "history.json")

# GSMArena'nın KENDİ RESMİ RSS akışı. Bu URL GSMArena tarafından herkese
# açık dağıtım için özel olarak yayınlanmıştır (bkz. gsmarena.com/rss).
GSMARENA_FEED_URL = "https://www.gsmarena.com/rss-news-reviews.php3"
GSMARENA_BASE = "https://www.gsmarena.com/"

USER_AGENT = (
    "MobilTeknolojiHaberleriTRBot/1.0 "
    "(+https://github.com/; Turkce ceviri/atif ile haber agregatoru; "
    "contact: info@example.com)"
)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"})

REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "0.3"))
MAX_RETRIES = 5

# RSS akışında tutulacak maksimum haber sayısı (döngüsel/kayan pencere).
# GSMArena akışı sürekli güncellendiği için, "bugüne özel" değil,
# "en güncel N haber" mantığıyla çalışır.
MAX_RSS_ITEMS = int(os.environ.get("MAX_RSS_ITEMS", "80"))


def get_with_retry(url: str, params: dict = None, timeout: int = 30) -> requests.Response:
    """429/5xx durumlarında üstel bekleme ile yeniden dener."""
    last_exc = None
    resp = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = SESSION.get(url, params=params, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            time.sleep(min(2 ** attempt, 30))
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 30)
            print(f"  [uyarı] {resp.status_code} alındı ({url}), {wait:.1f}s bekleniyor "
                  f"(deneme {attempt + 1}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        return resp

    if last_exc:
        raise last_exc
    return resp


# --------------------------------------------------------------------------
# ÇEVİRİ (ücretsiz Google Translate motoru üzerinden, deep-translator)
# --------------------------------------------------------------------------
# Not: Resmi ücretli "Google Cloud Translation API" DEĞİL; deep-translator
# kütüphanesinin kullandığı, herkese açık/ücretsiz Google Translate web
# arayüzü uç noktasıdır. Bu yüzden: (a) API anahtarı gerektirmez,
# (b) makul istek hızında tutulmalıdır (agresif paralel istek YOK),
# (c) Google'ın kısa süreli erişimi kısıtlaması ihtimaline karşı
# yeniden deneme + son çare olarak "çevrilemedi -> orijinali kullan"
# yedeği içerir (script tamamen çökmesin diye).

try:
    from deep_translator import GoogleTranslator
    _TRANSLATOR_AVAILABLE = True
except ImportError:
    _TRANSLATOR_AVAILABLE = False

TRANSLATE_CHUNK_LIMIT = 4500  # Google'ın pratik karakter sınırının altında güvenli pay
TRANSLATE_MAX_RETRIES = 4


def _translate_chunk(text: str) -> str:
    last_exc = None
    for attempt in range(TRANSLATE_MAX_RETRIES):
        try:
            result = GoogleTranslator(source="en", target="tr").translate(text)
            if result:
                return result
        except Exception as exc:  # noqa: BLE001 - çeviri servisi çok çeşitli hata fırlatabilir
            last_exc = exc
            time.sleep(min(2 ** attempt, 15))
    print(f"  [uyarı] Çeviri başarısız oldu, orijinal metin korunuyor: {last_exc}")
    return text


def translate_to_tr(text: str) -> str:
    """Metni İngilizceden Türkçeye çevirir. Google'ın pratik karakter
    sınırını aşan uzun metinleri cümle sınırlarını bozmadan parçalar."""
    text = (text or "").strip()
    if not text:
        return ""
    if not _TRANSLATOR_AVAILABLE:
        print("  [uyarı] deep-translator kurulu değil; çeviri atlanıyor (orijinal İngilizce metin kullanılacak).")
        return text

    if len(text) <= TRANSLATE_CHUNK_LIMIT:
        return _translate_chunk(text)

    # Uzun metni paragraf/cümle sınırlarında parçala
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, current = [], ""
    for s in sentences:
        if len(current) + len(s) + 1 > TRANSLATE_CHUNK_LIMIT:
            if current:
                chunks.append(current.strip())
            current = s
        else:
            current = f"{current} {s}".strip()
    if current:
        chunks.append(current.strip())

    translated_parts = []
    for chunk in chunks:
        translated_parts.append(_translate_chunk(chunk))
        time.sleep(0.2)  # ücretsiz servisi yormamak için küçük gecikme
    return " ".join(translated_parts)


# --------------------------------------------------------------------------
# YARDIMCI FONKSİYONLAR
# --------------------------------------------------------------------------

def slugify(text: str, original_for_hash: str = None) -> str:
    tr_map = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    base = (original_for_hash or text)
    out = text.translate(tr_map)
    out = re.sub(r"[^a-zA-Z0-9]+", "-", out).strip("-").lower()
    out = out[:80]
    digest = hashlib.md5(base.encode("utf-8")).hexdigest()[:10]
    return f"{out}-{digest}" if out else digest


def load_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}


def save_history(history: dict) -> None:
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def content_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def xml_escape(text: str) -> str:
    return html.escape(text or "", quote=True)


# --------------------------------------------------------------------------
# GSMARENA RSS ÇEKME VE AYRIŞTIRMA
# --------------------------------------------------------------------------

IMG_SRC_RE = re.compile(r'<img[^>]*\bsrc="([^"]+)"', re.IGNORECASE)
TAG_STRIP_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


def fetch_gsmarena_feed() -> list:
    """GSMArena'nın resmi RSS akışını çeker ve item listesini döndürür."""
    resp = get_with_retry(GSMARENA_FEED_URL, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    channel = root.find("channel")
    if channel is None:
        return []

    items = []
    for item_el in channel.findall("item"):
        title = (item_el.findtext("title") or "").strip()
        link = (item_el.findtext("link") or "").strip()
        guid = (item_el.findtext("guid") or link).strip()
        pub_date_raw = (item_el.findtext("pubDate") or "").strip()
        description_raw = item_el.findtext("description") or ""
        category = (item_el.findtext("category") or "news").strip()

        if not title or not link:
            continue

        image_url = None
        m = IMG_SRC_RE.search(description_raw)
        if m:
            image_url = m.group(1)

        text_only = TAG_STRIP_RE.sub(" ", description_raw)
        text_only = html.unescape(text_only)
        text_only = WHITESPACE_RE.sub(" ", text_only).strip()

        try:
            pub_dt = parsedate_to_datetime(pub_date_raw) if pub_date_raw else None
        except (TypeError, ValueError):
            pub_dt = None
        if pub_dt is None:
            pub_dt = dt.datetime.now(dt.timezone.utc)
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=dt.timezone.utc)

        items.append({
            "title_en": title,
            "link": link,
            "guid": guid,
            "category": category,
            "image_url": image_url,
            "excerpt_en": text_only,
            "pub_dt": pub_dt,
        })
    return items


# --------------------------------------------------------------------------
# MAKALE (HTML) ÜRETİMİ
# --------------------------------------------------------------------------

MAX_SEO_TITLE_LEN = 90
MIN_SEO_TITLE_LEN = 8


def build_seo_title(translated_title: str) -> str:
    t = WHITESPACE_RE.sub(" ", translated_title or "").strip().strip(" -–—:;,")
    if len(t) > MAX_SEO_TITLE_LEN:
        truncated = t[:MAX_SEO_TITLE_LEN].rsplit(" ", 1)[0].rstrip(",;:.- ")
        t = f"{truncated}…" if truncated else t[:MAX_SEO_TITLE_LEN]
    return t


def truncate_meta_description(text: str, limit: int = 155) -> str:
    text = WHITESPACE_RE.sub(" ", text or "").strip()
    if len(text) <= limit:
        return text
    truncated = text[:limit].rsplit(" ", 1)[0].rstrip(",;:.-")
    return f"{truncated}…" if truncated else text[:limit]


def build_article_content_html(translated_excerpt: str, category: str) -> str:
    """Çevrilmiş özeti okunabilir paragraflara böler. GSMArena'nın kendi
    özeti genelde "..." ile kesilir (tam makalenin BİLİNÇLİ olarak
    kazınmadığının bir sonucu); bu, gizlenmez, editoryal bir notla
    okuyucuya açıkça belirtilir (bkz. footer atıf metni)."""
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", translated_excerpt) if p.strip()]
    if not paragraphs:
        paragraphs = [translated_excerpt]
    return "\n".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)


ARTICLE_TEMPLATE = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{seo_title}</title>
<meta name="description" content="{meta_description}">
<link rel="canonical" href="{canonical_url}">
<meta name="robots" content="index, follow, max-image-preview:large">

<!-- Open Graph -->
<meta property="og:type" content="article">
<meta property="og:title" content="{seo_title}">
<meta property="og:description" content="{meta_description}">
<meta property="og:url" content="{canonical_url}">
<meta property="og:image" content="{og_image}">
<meta property="og:locale" content="tr_TR">
<meta property="og:site_name" content="{site_name}">
<meta property="article:published_time" content="{published_iso}">

<!-- Twitter Card -->
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{seo_title}">
<meta name="twitter:description" content="{meta_description}">
<meta name="twitter:image" content="{og_image}">

<!-- Schema.org: NewsArticle -->
<script type="application/ld+json">
{schema_newsarticle}
</script>
<!-- Schema.org: BreadcrumbList -->
<script type="application/ld+json">
{schema_breadcrumb}
</script>
</head>
<body>
<header>
  <nav aria-label="breadcrumb">
    <a href="{site_url}/">{site_name}</a> &rsaquo;
    <span>{title}</span>
  </nav>
</header>
<main>
  <article itemscope itemtype="https://schema.org/NewsArticle">
    <h1 itemprop="headline">{title}</h1>
    <p class="meta"><time itemprop="datePublished" datetime="{published_iso}">{date_label}</time> &middot; Kaynak: <a href="{source_url}" rel="noopener nofollow" itemprop="isBasedOn">GSMArena.com</a></p>
    {figure_html}
    <div itemprop="articleBody">
    {content_html}
    </div>
    <footer class="attribution">
      <p>Bu haberin başlığı ve özeti, <a href="{source_url}" rel="noopener nofollow">GSMArena.com</a>'un
      kendi resmi RSS akışında yayınladığı içerikten alınıp Türkçeye otomatik çevrilmiştir.
      Haberin tam metni ve güncel görselleri için lütfen orijinal kaynağı ziyaret edin:
      <a href="{source_url}" rel="noopener nofollow">{source_url}</a></p>
    </footer>
  </article>
</main>
</body>
</html>
"""


def build_schema_newsarticle(title, meta_description, canonical_url, image_url, published_iso, source_url):
    return json.dumps({
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical_url},
        "headline": title[:110],
        "description": meta_description,
        "image": [image_url] if image_url else [f"{SITE_URL}/assets/default-og.jpg"],
        "datePublished": published_iso,
        "dateModified": published_iso,
        "author": {"@type": "Organization", "name": SITE_NAME, "url": SITE_URL},
        "publisher": {
            "@type": "Organization",
            "name": SITE_NAME,
            "logo": {"@type": "ImageObject", "url": f"{SITE_URL}/assets/logo.png"},
        },
        "isBasedOn": source_url,
    }, ensure_ascii=False, indent=2)


def build_schema_breadcrumb(title, canonical_url):
    return json.dumps({
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": SITE_NAME, "item": SITE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": title, "item": canonical_url},
        ],
    }, ensure_ascii=False, indent=2)


def build_article_html(item: dict, date_label: str) -> str:
    canonical_url = f"{SITE_URL}/articles/{item['slug']}.html"
    image_url = item.get("image_url") or ""
    figure_html = ""
    if image_url:
        figure_html = (
            f'<figure><img src="{html.escape(image_url)}" alt="{html.escape(item["title_tr"])}" '
            f'loading="lazy" width="1200" height="675"></figure>'
        )

    return ARTICLE_TEMPLATE.format(
        seo_title=html.escape(item["seo_title"]),
        meta_description=html.escape(item["meta_description"]),
        canonical_url=canonical_url,
        og_image=html.escape(image_url or f"{SITE_URL}/assets/default-og.jpg"),
        site_name=SITE_NAME,
        published_iso=item["published_iso"],
        site_url=SITE_URL,
        date_label=date_label,
        title=html.escape(item["title_tr"]),
        source_url=item["source_url"],
        figure_html=figure_html,
        content_html=item["content_html"],
        schema_newsarticle=build_schema_newsarticle(
            item["title_tr"], item["meta_description"], canonical_url,
            image_url, item["published_iso"], item["source_url"],
        ),
        schema_breadcrumb=build_schema_breadcrumb(item["title_tr"], canonical_url),
    )


# --------------------------------------------------------------------------
# RSS ÜRETİMİ
# --------------------------------------------------------------------------

def build_rss(items: list, last_build_iso: str) -> str:
    rss_items = []
    for item in items:
        canonical_url = f"{SITE_URL}/articles/{item['slug']}.html"

        image_html = ""
        if item.get("image_url"):
            image_html = (
                f'<p><img src="{xml_escape(item["image_url"])}" '
                f'alt="{xml_escape(item["title_tr"])}" loading="lazy"></p>'
            )

        raw_content = f"{image_html}{item['content_html']}"
        safe_content = raw_content.replace("]]>", "]]]]><![CDATA[>")
        content_encoded = f"<![CDATA[{safe_content}]]>"

        rss_items.append(f"""  <item>
    <title>{xml_escape(item['seo_title'])}</title>
    <link>{canonical_url}</link>
    <guid isPermaLink="true">{canonical_url}</guid>
    <pubDate>{item['pub_date_rfc822']}</pubDate>
    <description>{xml_escape(item['meta_description'])}</description>
    <content:encoded>{content_encoded}</content:encoded>
    <source url="{xml_escape(item['source_url'])}">GSMArena.com</source>
  </item>""")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>{xml_escape(SITE_NAME)}</title>
  <link>{SITE_URL}/</link>
  <atom:link href="{SITE_URL}/rss.xml" rel="self" type="application/rss+xml" />
  <description>{xml_escape(SITE_DESCRIPTION)}</description>
  <language>tr</language>
  <lastBuildDate>{last_build_iso}</lastBuildDate>
{chr(10).join(rss_items)}
</channel>
</rss>
"""


# --------------------------------------------------------------------------
# ANA AKIŞ
# --------------------------------------------------------------------------

TR_MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
             "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


def main():
    now = dt.datetime.now(dt.timezone.utc)
    last_build_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    os.makedirs(ARTICLES_DIR, exist_ok=True)
    history = load_history()

    try:
        feed_items = fetch_gsmarena_feed()
    except (requests.exceptions.RequestException, ET.ParseError) as exc:
        print(f"[HATA] GSMArena RSS akışından veri alınamadı: {exc}")
        print("Bu çalıştırma iptal edildi; docs/rss.xml ve data/history.json değiştirilmedi.")
        raise SystemExit(1)

    print(f"GSMArena akışında {len(feed_items)} haber bulundu.")

    new_count = 0
    for entry in feed_items:
        h = content_hash(entry["guid"])
        if h in history:
            continue  # daha önce çevrilip yayınlanmış

        title_en = entry["title_en"]
        excerpt_en = entry["excerpt_en"]

        try:
            title_tr = translate_to_tr(title_en)
            excerpt_tr = translate_to_tr(excerpt_en) if excerpt_en else ""
        except Exception as exc:  # noqa: BLE001
            print(f"  [atlandı] '{title_en}' çevrilemedi, beklenmeyen hata: {exc}")
            continue
        finally:
            time.sleep(REQUEST_DELAY_SECONDS)

        if not title_tr:
            print(f"  [atlandı] '{title_en}' için başlık üretilemedi.")
            continue

        pub_dt = entry["pub_dt"]
        published_iso = pub_dt.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        pub_date_rfc822 = pub_dt.astimezone(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        date_label = f"{pub_dt.day} {TR_MONTHS[pub_dt.month - 1]} {pub_dt.year}"

        content_html = build_article_content_html(excerpt_tr, entry["category"])
        content_text_len = len(re.sub(r"<[^>]+>", " ", content_html))
        if content_text_len < 30:
            print(f"  [atlandı] '{title_en}' için yeterli içerik üretilemedi.")
            continue

        seo_title = build_seo_title(title_tr)
        meta_description = truncate_meta_description(excerpt_tr or title_tr)
        slug = slugify(title_tr, original_for_hash=entry["guid"])

        item = {
            "slug": slug,
            "title_tr": title_tr,
            "seo_title": seo_title,
            "meta_description": meta_description,
            "content_html": content_html,
            "image_url": entry["image_url"],
            "source_url": entry["link"],
            "published_iso": published_iso,
            "pub_date_rfc822": pub_date_rfc822,
            "hash": h,
        }

        html_out = build_article_html(item, date_label)
        with open(os.path.join(ARTICLES_DIR, f"{slug}.html"), "w", encoding="utf-8") as f:
            f.write(html_out)

        history[h] = {
            "title_tr": title_tr,
            "seo_title": seo_title,
            "meta_description": meta_description,
            "content_html": content_html,
            "image_url": entry["image_url"],
            "source_url": entry["link"],
            "slug": slug,
            "published_iso": published_iso,
            "pub_date_rfc822": pub_date_rfc822,
        }
        new_count += 1
        print(f"  [yeni] {seo_title}")

    print(f"{new_count} yeni haber çevrildi ve yayınlandı.")

    # RSS'i history'deki TÜM kayıtlardan, en yeniden en eskiye sıralayıp
    # ilk MAX_RSS_ITEMS kadarını alarak üret (kayan pencere).
    all_records = sorted(
        history.values(),
        key=lambda r: r.get("published_iso", ""),
        reverse=True,
    )[:MAX_RSS_ITEMS]

    rss_items = []
    for record in all_records:
        required = ("seo_title", "meta_description", "content_html", "slug", "source_url", "pub_date_rfc822")
        if not all(record.get(f) for f in required):
            continue
        rss_items.append({
            "slug": record["slug"],
            "title_tr": record.get("title_tr", record["seo_title"]),
            "seo_title": record["seo_title"],
            "meta_description": record["meta_description"],
            "content_html": record["content_html"],
            "image_url": record.get("image_url"),
            "source_url": record["source_url"],
            "pub_date_rfc822": record["pub_date_rfc822"],
        })

    rss_xml = build_rss(rss_items, last_build_iso)
    with open(os.path.join(OUTPUT_DIR, "rss.xml"), "w", encoding="utf-8") as f:
        f.write(rss_xml)

    save_history(history)
    print(f"RSS akışı {len(rss_items)} haber ile yazıldı. Tamamlandı.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        print("[HATA] Beklenmeyen bir hata nedeniyle script sonlandırıldı.")
        raise SystemExit(1)
