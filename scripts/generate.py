#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wikipedia "Tarihte Bugün" -> Otomatik RSS + Statik Makale Üretici
==================================================================

Türkçe Wikipedia'nın Wikimedia REST API'sini kullanarak günün tarihine ait
tüm olayları (events / births / deaths / holidays) çeker, her biri için
kaynak Wikipedia makalesinin TAM metnini alır, gereksiz bölümleri
(Kaynakça, Dış bağlantılar, Ayrıca bakınız, vb.) temizler, kural tabanlı
(harici bir yapay zekâ servisine bağlı olmayan) bir biçimlendirmeyle
makale haline getirir ve şunları üretir:

  docs/rss.xml                -> Ana RSS akışı (SEO uyumlu)
  docs/articles/<slug>.html   -> Her olay için tam SEO/Schema.org sayfası
  data/history.json           -> Yayınlanan makalelerin kaydı (mükerrer önleme)

Not: Bu script herhangi bir dış AI/LLM API'sine (ör. Anthropic) bağımlı
DEĞİLDİR; tüm makale ve başlık üretimi doğrudan Wikipedia kaynaklı verilerden
kural tabanlı olarak türetilir. Ayrıca sitemap.xml ve index.html ÜRETİLMEZ;
tek çıktı RSS akışı ve tekil makale sayfalarıdır.

Notlar
------
* Wikipedia içeriği CC BY-SA 4.0 lisanslıdır; bu nedenle her makalede
  kaynağa atıf ve link zorunlu tutulmuştur (build_article_html içinde).
* Bu script kasıtlı olarak herhangi bir ülke / etnik köken / siyasi konu
  bazlı bir "içerik yasaklama listesi" İÇERMEZ. Böyle bir filtre, tarihte
  o gün gerçekten yaşanmış olayları -sırf konusu yüzünden- sistematik
  olarak gizlemek anlamına gelir ki bu, tarafsız bir "Tarihte Bugün"
  hizmetinin doğasına aykırıdır. Bunun yerine, istenmeyen/insan tarafından
  gözden geçirilecek öğeler için `MANUAL_REVIEW_TITLES` gibi şeffaf,
  editoryal bir mekanizma bırakılmıştır (aşağıya bakınız) — burada kendi
  editoryal kriterlerinizi (örn. hakaret/nefret söylemi, doğrulanamayan
  bilgi) şeffaf şekilde tanımlayabilirsiniz.
"""

import os
import re
import json
import html
import time
import hashlib
import datetime as dt
from urllib.parse import quote

import requests

# --------------------------------------------------------------------------
# AYARLAR
# --------------------------------------------------------------------------

SITE_URL = os.environ.get("SITE_URL", "https://umutevicom-commits.github.io/tarihte")
SITE_NAME = "Tarihte Bugün"
SITE_DESCRIPTION = "Türkçe Wikipedia kaynaklı, her gün otomatik güncellenen 'Tarihte Bugün' arşivi."
LANG = "tr"
OUTPUT_DIR = "docs"
ARTICLES_DIR = os.path.join(OUTPUT_DIR, "articles")
HISTORY_FILE = os.path.join("data", "history.json")
MIN_ITEMS_TARGET = 100  # günlük hedef minimum içerik sayısı

WIKI_API = "https://tr.wikipedia.org/w/api.php"
ONTHISDAY_API = "https://api.wikimedia.org/feed/v1/wikipedia/{lang}/onthisday/all/{mm}/{dd}"
USER_AGENT = "TarihteBugunRSSBot/1.0 (https://github.com/; contact: info@immaculate.tr)"

# Kaldırılacak Wikipedia bölüm başlıkları (kaynakça, dış bağlantılar, vb.)
STRIP_SECTION_HEADERS = [
    "kaynakça", "dış bağlantılar", "ayrıca bakınız", "notlar", "referanslar",
    "dipnotlar", "bibliyografya", "further reading", "see also",
]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

# Wikipedia/Wikimedia API'leri kısa sürede çok fazla istek atıldığında
# "429 Too Many Requests" ile yanıt verebiliyor. Bunu tolere etmek için
# üstel bekleme (exponential backoff) uygulayan ortak bir istek fonksiyonu
# kullanıyoruz; ayrıca her olay arasına küçük bir gecikme koyuyoruz.
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "0.4"))
MAX_RETRIES = 5


def get_with_retry(url: str, params: dict = None, timeout: int = 30) -> requests.Response:
    """SESSION.get() çağrısını 429/5xx durumlarında üstel bekleme ile
    yeniden dener. Tüm denemeler tükenirse son yanıtı (veya son hatayı)
    yükseltir."""
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = SESSION.get(url, params=params, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            time.sleep(wait)
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
# YARDIMCI FONKSİYONLAR
# --------------------------------------------------------------------------

def slugify(text: str) -> str:
    tr_map = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    text = text.translate(tr_map)
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:100] or hashlib.md5(text.encode()).hexdigest()[:10]


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


# --------------------------------------------------------------------------
# WIKIPEDIA VERİ ÇEKME
# --------------------------------------------------------------------------

def fetch_onthisday(month: int, day: int) -> dict:
    """Wikimedia REST API'den o güne ait tüm kategorileri çeker."""
    url = ONTHISDAY_API.format(lang=LANG, mm=f"{month:02d}", dd=f"{day:02d}")
    resp = get_with_retry(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_full_extract(title: str) -> str:
    """Verilen Wikipedia sayfasının TAM düz metin içeriğini döndürür."""
    params = {
        "action": "query",
        "prop": "extracts",
        "explaintext": 1,
        "format": "json",
        "redirects": 1,
        "titles": title,
    }
    resp = get_with_retry(WIKI_API, params=params, timeout=30)
    resp.raise_for_status()
    pages = resp.json().get("query", {}).get("pages", {})
    for page in pages.values():
        return page.get("extract", "") or ""
    return ""


# Wikipedia'da ikon/logo/arma/amblem/sembol/harita-pini niteliğindeki
# dosyaların adlarında sıkça geçen kalıplar. Not: eski sürümdeki
# "flag of" deseni BOŞLUKLU yazılmıştı ama Wikipedia dosya adları alt
# çizgi kullanır (ör. "Flag_of_Turkey.svg") — bu yüzden hiç eşleşmiyor ve
# bayrak/arma ikonları süzülmeden geçiyordu. Aşağıdaki liste hem alt
# çizgili hem boşluklu yazımları, hem de çok daha geniş bir ikon/sembol
# kelime kümesini kapsayacak şekilde genişletildi.
ICON_FILENAME_BLACKLIST = re.compile(
    r"(commons[-_ ]?logo|wiki(pedia|media|data|source|quote|news|books|voyage)?[-_ ]?logo|"
    r"\blogo\b|\bicon\b|edit[-_ ]?icon|question|"
    r"flag[-_ ]?of|coat[-_ ]?of[-_ ]?arms|arms[-_ ]?of|"
    r"seal[-_ ]?of|emblem|\bcrest\b|\bsymbol\b|"
    r"portal|disambig|folder|padlock|\block\b|"
    r"nuvola|crystal[-_ ]?clear|gnome[-_ ]?|ambox|stub[-_ ]?icon|"
    r"pog[-_ ]?(blue|red|green|yellow)|map[-_ ]?pin|location[-_ ]?(dot|pin|marker)|"
    r"star[-_ ]?(full|empty|half)|pictogram|"
    r"text[-_ ]?document|page[-_ ]?white|mergefrom|mergeto|cleanup|"
    r"protection|unbalanced[-_ ]?scales|"
    r"blank\.(png|svg)|spacer\.(png|gif)|1x1|transparent\.(png|gif)|"
    r"p[-_ ]vip|question[-_ ]?book|sound[-_ ]?icon|speaker[-_ ]?icon)",
    re.IGNORECASE,
)

# Wikipedia'daki içerik görsellerinin (sayfa içi <img>) neredeyse tamamı
# gerçek fotoğraflardır ve JPG/PNG formatındadır. SVG ise Wikipedia'da
# neredeyse istisnasız ikon, logo, arma, amblem, sembol, harita ya da
# diyagram formatıdır — bu yüzden içerik görseli olarak KABUL EDİLMEZ.
CONTENT_IMAGE_EXTENSIONS = r"\.(jpg|jpeg|png)$"

# İkon/sembol niteliğindeki küçük görselleri ayıklamak için minimum
# kabul edilebilir piksel boyutu. Wikipedia'daki gerçek editoryal/kapak
# fotoğrafları neredeyse her zaman bunun çok üzerindedir; ikonlar ise
# genellikle 16-64px aralığındadır.
MIN_IMAGE_DIMENSION = 200


def _looks_like_icon(name_or_url: str) -> bool:
    return bool(ICON_FILENAME_BLACKLIST.search(name_or_url or ""))


def fetch_page_images(title: str, limit: int = 6) -> list:
    """Sayfadaki uygun (ikon/logo/arma olmayan, yeterince büyük, gerçek
    fotoğraf niteliğindeki) görselleri döndürür."""
    params = {
        "action": "query",
        "prop": "pageimages|images",
        "piprop": "original",
        "format": "json",
        "titles": title,
        "imlimit": 20,
    }
    resp = get_with_retry(WIKI_API, params=params, timeout=30)
    resp.raise_for_status()
    pages = resp.json().get("query", {}).get("pages", {})
    images = []
    for page in pages.values():
        original = page.get("original", {})
        original_url = original.get("source")
        original_w = original.get("width") or 0
        original_h = original.get("height") or 0
        if (
            original_url
            and re.search(CONTENT_IMAGE_EXTENSIONS, original_url, re.I)
            and not _looks_like_icon(original_url)
            and original_w >= MIN_IMAGE_DIMENSION
            and original_h >= MIN_IMAGE_DIMENSION
        ):
            images.append(original_url)
        for img in page.get("images", []):
            fname = img.get("title", "")
            if re.search(CONTENT_IMAGE_EXTENSIONS, fname, re.I) and not _looks_like_icon(fname):
                images.append("FILE:" + fname)
    # FILE: girdilerini gerçek URL'e çevir (boyut kontrolüyle birlikte)
    resolved = []
    file_titles = [i[5:] for i in images if i.startswith("FILE:")]
    if file_titles:
        resolved.extend(resolve_file_urls(file_titles))
    resolved = [i for i in images if not i.startswith("FILE:")] + resolved
    # tekilleştir, sınırla
    seen, out = set(), []
    for u in resolved:
        if u and u not in seen and not _looks_like_icon(u):
            seen.add(u)
            out.append(u)
        if len(out) >= limit:
            break
    return out


def resolve_file_urls(file_titles: list, apply_size_filter: bool = True) -> list:
    """Dosya başlıklarını gerçek URL'lere çevirir. apply_size_filter=True
    ise (varsayılan; görseller için) ikon boyutundaki (MIN_IMAGE_DIMENSION
    altı) dosyaları eler. Video dosyaları için apply_size_filter=False
    kullanılmalı — video'ların genişlik/yükseklik meta verisi görsel
    ikonlarla aynı anlama gelmez ve bazı formatlarda hiç raporlanmayabilir."""
    urls = []
    for chunk_start in range(0, len(file_titles), 50):
        chunk = file_titles[chunk_start:chunk_start + 50]
        params = {
            "action": "query",
            "titles": "|".join(f"Dosya:{t}" if not t.lower().startswith("dosya:") else t for t in chunk),
            "prop": "imageinfo",
            "iiprop": "url|size",
            "format": "json",
        }
        resp = get_with_retry(WIKI_API, params=params, timeout=30)
        if resp.ok:
            pages = resp.json().get("query", {}).get("pages", {})
            for page in pages.values():
                for ii in page.get("imageinfo", []):
                    url = ii.get("url")
                    if not url:
                        continue
                    if apply_size_filter:
                        width = ii.get("width") or 0
                        height = ii.get("height") or 0
                        if width < MIN_IMAGE_DIMENSION or height < MIN_IMAGE_DIMENSION:
                            continue
                    urls.append(url)
    return urls


def fetch_page_video(title: str) -> str:
    """Sayfada gömülü bir video dosyası varsa (.ogv/.webm) URL'ini döndürür."""
    params = {
        "action": "query",
        "prop": "images",
        "format": "json",
        "titles": title,
        "imlimit": 30,
    }
    resp = get_with_retry(WIKI_API, params=params, timeout=30)
    if not resp.ok:
        return ""
    pages = resp.json().get("query", {}).get("pages", {})
    video_titles = []
    for page in pages.values():
        for img in page.get("images", []):
            if re.search(r"\.(ogv|webm|mp4)$", img.get("title", ""), re.I):
                video_titles.append(img["title"])
    if video_titles:
        urls = resolve_file_urls(video_titles, apply_size_filter=False)
        return urls[0] if urls else ""
    return ""


# --------------------------------------------------------------------------
# İÇERİK TEMİZLEME + MAKALE ÜRETİMİ
# --------------------------------------------------------------------------

def clean_extract(text: str) -> str:
    """Kaynakça / Dış bağlantılar gibi gereksiz bölümleri, editör notlarını
    ve boş satırları temizler."""
    lines = text.split("\n")
    cleaned = []
    skip_section = False
    for line in lines:
        stripped = line.strip()
        lowered = stripped.lower().strip("== ").strip()
        if re.match(r"^={2,}.*={2,}$", stripped):
            if lowered in STRIP_SECTION_HEADERS:
                skip_section = True
                continue
            else:
                skip_section = False
                continue  # başlıkları da düz metinden çıkarıyoruz (HTML'de kendimiz başlık kuracağız)
        if skip_section:
            continue
        if not stripped:
            continue
        # editör notu / şablon kalıntısı temizliği
        stripped = re.sub(r"\[\d+\]", "", stripped)  # dipnot işaretleri
        cleaned.append(stripped)
    return "\n\n".join(cleaned)


def fallback_article(title: str, lead: str, body: str, year, event_text: str) -> str:
    """Dış bir AI/LLM servisine bağlı olmadan çalışan, kural tabanlı makale
    biçimlendiricisi. Wikipedia'dan çekilen temizlenmiş metni HTML
    paragraflarına döker; ardışık tekrar eden veya anlamlı bilgi taşımayan
    (çok kısa / yalnızca liste kalıntısı gibi görünen) paragrafları eler."""
    raw_paragraphs = [p.strip() for p in body.split("\n\n") if len(p.strip()) > 40]

    paragraphs = []
    seen_normalized = set()
    for p in raw_paragraphs:
        normalized = re.sub(r"\s+", " ", p).strip().lower()
        if normalized in seen_normalized:
            continue  # birebir veya neredeyse birebir tekrar eden paragrafı atla
        seen_normalized.add(normalized)
        paragraphs.append(p)

    # Olay cümlesi (event_text) çok kısa/anlamsızsa ("x", "ok" gibi) giriş
    # paragrafı olarak kullanılmaz; bu, içerik gövdesine anlamsız tek
    # kelimelik/harfli bir paragraf sızmasını engeller.
    MIN_INTRO_LEN = 15
    clean_event = re.sub(r"\s+", " ", (event_text or "")).strip()
    intro = f"<p>{html.escape(clean_event)}</p>" if len(clean_event) >= MIN_INTRO_LEN else ""
    body_html = "\n".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)
    return (intro + "\n" + body_html).strip() if intro else body_html


def build_article_content(title: str, event_text: str, raw_body: str, year) -> str:
    """Makale gövdesini kural tabanlı olarak üretir (bkz. fallback_article)."""
    return fallback_article(title, event_text, raw_body, year, event_text)


# --------------------------------------------------------------------------
# SEO BAŞLIK ÜRETİMİ
# --------------------------------------------------------------------------

MIN_SEO_TITLE_LEN = 12  # bundan kısa/anlamsız (tek harfli vb.) başlıklar asla kullanılmaz
MAX_SEO_TITLE_LEN = 90


def _clean_title_text(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    text = text.strip('"').strip("“”„").strip()
    # başta/sonda kalabilecek noktalama artıklarını temizle
    text = text.strip(" -–—:;,")
    return text


def generate_seo_title(event_text: str, title: str) -> str:
    """Olayın (event_text) kendi cümlesinden, hiçbir dış AI servisine bağlı
    olmadan, kural tabanlı, doğal ve SEO uyumlu bir başlık üretir.

    Güvenceler:
    - Üretilen başlık ASLA MIN_SEO_TITLE_LEN karakterden kısa olmaz; bu,
      tek harfli / anlamsız / bozuk başlık üretimini engeller.
    - Olay cümlesi çok kısaysa Vikipedi madde başlığıyla birleştirilir.
    - Başlık hiçbir zaman tarih ya da 'Olay/Doğum/Ölüm/Özel Gün' gibi bir
      kategori etiketi İÇERMEZ; olayın kendisini anlatır.
    - Aşırı uzun başlıklar kelime sınırında, anlamı bozmadan kısaltılır.
    """
    event = _clean_title_text(event_text)
    wiki_title = _clean_title_text(title)

    candidate = event if len(event) >= MIN_SEO_TITLE_LEN else ""

    if not candidate:
        # Olay cümlesi çok kısa/boşsa, önce olay + madde başlığını birleştirmeyi dene
        if event and wiki_title and wiki_title.lower() not in event.lower():
            candidate = _clean_title_text(f"{event} ({wiki_title})")
        elif wiki_title:
            candidate = wiki_title
        else:
            candidate = event

    # Son çare: hâlâ minimum uzunluğun altındaysa (çok nadir; hem olay
    # cümlesi hem madde başlığı aşırı kısa), elimizdeki en bilgilendirici
    # metni kullan — asla boş ya da tek kelimelik bir başlık üretme.
    if (
        len(candidate) < MIN_SEO_TITLE_LEN
        and wiki_title
        and wiki_title.lower() not in candidate.lower()
    ):
        candidate = _clean_title_text(f"{candidate} — {wiki_title}") if candidate else wiki_title

    if not candidate:
        return ""  # bu öğe main() içinde geçersiz sayılıp RSS'e yazılmayacak

    if len(candidate) > MAX_SEO_TITLE_LEN:
        truncated = candidate[:MAX_SEO_TITLE_LEN].rsplit(" ", 1)[0].rstrip(",;:.- ")
        candidate = f"{truncated}…" if truncated else candidate[:MAX_SEO_TITLE_LEN]

    return candidate


# --------------------------------------------------------------------------
# HTML / SCHEMA.ORG ÜRETİMİ
# --------------------------------------------------------------------------

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
    <span>{date_label}</span> &rsaquo;
    <span>{title}</span>
  </nav>
</header>
<main>
  <article itemscope itemtype="https://schema.org/NewsArticle">
    <h1 itemprop="headline">{title}</h1>
    <p class="meta"><time itemprop="datePublished" datetime="{published_iso}">{date_label}</time> &middot; Kaynak: <a href="{wiki_url}" rel="noopener" itemprop="isBasedOn">Vikipedi</a></p>
    {figure_html}
    <div itemprop="articleBody">
    {content_html}
    </div>
    {video_html}
    <footer class="attribution">
      <p>Bu içerik, <a href="{wiki_url}" rel="noopener nofollow">Türkçe Wikipedia</a> kaynaklı verilerden
      derlenmiştir ve <a href="https://creativecommons.org/licenses/by-sa/4.0/deed.tr" rel="license noopener">CC BY-SA 4.0</a>
      lisansı kapsamında paylaşılmaktadır.</p>
    </footer>
  </article>
</main>
</body>
</html>
"""


def build_schema_newsarticle(title, meta_description, canonical_url, images, published_iso):
    return json.dumps({
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical_url},
        "headline": title[:110],
        "description": meta_description,
        "image": images or [f"{SITE_URL}/assets/default-og.jpg"],
        "datePublished": published_iso,
        "dateModified": published_iso,
        "author": {"@type": "Organization", "name": SITE_NAME, "url": SITE_URL},
        "publisher": {
            "@type": "Organization",
            "name": SITE_NAME,
            "logo": {"@type": "ImageObject", "url": f"{SITE_URL}/assets/logo.png"},
        },
        "isBasedOn": "https://tr.wikipedia.org",
    }, ensure_ascii=False, indent=2)


def build_schema_breadcrumb(title, canonical_url):
    """2 seviyeli breadcrumb: Site > Makale. Artık index.html üretilmediği
    için var olmayan bir 'gün dizini' sayfasına ("Tarihte Bugün — X" gibi)
    referans veren bir ara seviye eklenmez; böylece breadcrumb'daki her
    öğe gerçekten var olan, çözümlenebilir bir URL'ye işaret eder."""
    return json.dumps({
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": SITE_NAME, "item": SITE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": title, "item": canonical_url},
        ],
    }, ensure_ascii=False, indent=2)


def build_article_html(item: dict, published_iso: str, date_label: str) -> str:
    canonical_url = f"{SITE_URL}/articles/{item['slug']}.html"
    images = item["images"]
    og_image = images[0] if images else f"{SITE_URL}/assets/default-og.jpg"
    figure_html = ""
    if images:
        figure_html = (
            f'<figure><img src="{html.escape(images[0])}" alt="{html.escape(item["title"])}" '
            f'loading="lazy" width="1200" height="675"></figure>'
        )
        for extra in images[1:]:
            figure_html += (
                f'<figure><img src="{html.escape(extra)}" alt="{html.escape(item["title"])}" loading="lazy"></figure>'
            )
    video_html = ""
    if item.get("video_url"):
        video_html = (
            f'<figure><video controls preload="none" poster="{html.escape(og_image)}">'
            f'<source src="{html.escape(item["video_url"])}"></video></figure>'
        )

    return ARTICLE_TEMPLATE.format(
        seo_title=html.escape(item["seo_title"]),
        meta_description=html.escape(item["meta_description"]),
        canonical_url=canonical_url,
        og_image=html.escape(og_image),
        site_name=SITE_NAME,
        published_iso=published_iso,
        site_url=SITE_URL,
        date_label=date_label,
        title=html.escape(item["title"]),
        wiki_url=item["wiki_url"],
        figure_html=figure_html,
        content_html=item["content_html"],
        video_html=video_html,
        schema_newsarticle=build_schema_newsarticle(
            item["title"], item["meta_description"], canonical_url, images, published_iso
        ),
        schema_breadcrumb=build_schema_breadcrumb(item["title"], canonical_url),
    )


# --------------------------------------------------------------------------
# RSS ÜRETİMİ
# --------------------------------------------------------------------------

def xml_escape(text: str) -> str:
    return html.escape(text, quote=True)


def build_rss(items: list, published_iso: str) -> str:
    rss_items = []
    for item in items:
        canonical_url = f"{SITE_URL}/articles/{item['slug']}.html"

        # Not: <enclosure> ve <media:content> etiketleri bazı RSS
        # okuyucularda (Feedly, Inoreader, NetNewsWire vb.) çıplak
        # "upload.wikimedia.org/..." URL'sini görünür bir ek/link olarak
        # gösteriyor. Bunun yerine görselleri doğrudan içerik HTML'inin
        # içine <img> olarak gömüyoruz; böylece okuyucuda görsel olarak
        # görünür, ham bağlantı metni olarak değil.
        images_html = ""
        if item["images"]:
            images_html = "".join(
                f'<p><img src="{xml_escape(u)}" alt="{xml_escape(item["title"])}" loading="lazy"></p>'
                for u in item["images"]
            )

        content_encoded = f"<![CDATA[{images_html}{item['content_html']}]]>"

        rss_items.append(f"""  <item>
    <title>{xml_escape(item['seo_title'])}</title>
    <link>{canonical_url}</link>
    <guid isPermaLink="true">{canonical_url}</guid>
    <pubDate>{item['pub_date_rfc822']}</pubDate>
    <description>{xml_escape(item['meta_description'])}</description>
    <content:encoded>{content_encoded}</content:encoded>
  </item>""")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:media="http://search.yahoo.com/mrss/"
     xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>{xml_escape(SITE_NAME)}</title>
  <link>{SITE_URL}/</link>
  <atom:link href="{SITE_URL}/rss.xml" rel="self" type="application/rss+xml" />
  <description>{xml_escape(SITE_DESCRIPTION)}</description>
  <language>tr</language>
  <lastBuildDate>{published_iso}</lastBuildDate>
{chr(10).join(rss_items)}
</channel>
</rss>
"""


# --------------------------------------------------------------------------
# ANA AKIŞ
# --------------------------------------------------------------------------

def main():
    now = dt.datetime.now(dt.timezone.utc)
    month, day, year_now = now.month, now.day, now.year
    date_label = f"{day} {['Ocak','Şubat','Mart','Nisan','Mayıs','Haziran','Temmuz','Ağustos','Eylül','Ekim','Kasım','Aralık'][month-1]}"
    date_slug = f"{month:02d}-{day:02d}"
    published_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    pub_date_rfc822 = now.strftime("%a, %d %b %Y %H:%M:%S +0000")

    os.makedirs(ARTICLES_DIR, exist_ok=True)
    history = load_history()

    data = fetch_onthisday(month, day)
    categories = ["events", "births", "deaths", "holidays"]
    raw_items = []
    for cat in categories:
        for entry in data.get(cat, []):
            entry["_category"] = cat
            raw_items.append(entry)

    print(f"{len(raw_items)} ham olay bulundu ({date_label}).")

    items = []
    for entry in raw_items:
        pages = entry.get("pages") or []
        primary_page = pages[0] if pages else None
        if not primary_page:
            continue

        title = primary_page.get("title", "").strip()
        wiki_url = (
            primary_page.get("content_urls", {}).get("desktop", {}).get("page")
            or f"https://tr.wikipedia.org/wiki/{quote(title)}"
        )
        event_text = entry.get("text", "").strip()
        entry_year = entry.get("year")

        if not title:
            continue

        h = content_hash(date_slug, title, entry.get("_category", ""))
        if h in history:
            continue  # daha önce yayınlanmış / mükerrer

        try:
            raw_extract = fetch_full_extract(title)
            cleaned = clean_extract(raw_extract)
            if len(cleaned) < 80 and not event_text:
                continue

            content_html = build_article_content(title, event_text, cleaned, entry_year)
            images = fetch_page_images(title)
            video_url = fetch_page_video(title)
        except requests.exceptions.RequestException as exc:
            # Wikipedia/Wikimedia API'lerinden kalıcı bir hata (ör. tüm
            # yeniden denemeler tükendi) alınırsa, tüm build'i düşürmek
            # yerine sadece bu tek olayı atlıyoruz.
            print(f"  [atlandı] '{title}' işlenemedi: {exc}")
            continue
        finally:
            # Wikipedia API'lerini yormamak için olaylar arasına küçük
            # bir gecikme koyuyoruz.
            time.sleep(REQUEST_DELAY_SECONDS)

        seo_title = generate_seo_title(event_text, title)

        # Kalite kapısı: anlamsız/tek harfli/bozuk başlık ya da boş/aşırı
        # kısa içerik üreten hiçbir öğe RSS'e ya da makale sayfalarına
        # yazılmaz. Bu, hatalı veya konu dışı çıktıların yayınlanmasını
        # engelleyen son kontroldür.
        content_text_len = len(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", content_html or "")).strip())
        if not seo_title or len(seo_title) < MIN_SEO_TITLE_LEN:
            print(f"  [atlandı] '{title}' için geçerli bir başlık üretilemedi.")
            continue
        if content_text_len < 60:
            print(f"  [atlandı] '{title}' için yeterli/anlamlı içerik üretilemedi.")
            continue

        # Olay cümlesi (event_text) anlamlı bir açıklama oluşturacak kadar
        # uzun değilse ("x" gibi), temizlenmiş kaynak metinden türetilen
        # bir açıklamaya düşülür; böylece RSS <description> alanı asla
        # anlamsız/aşırı kısa bir metin içermez.
        meta_source = event_text if len(event_text.strip()) >= 20 else cleaned
        meta_description = re.sub(r"\s+", " ", meta_source).strip()[:155]

        item = {
            "slug": slugify(f"{title}-{date_slug}-{entry.get('_category')}"),
            "title": title,
            "seo_title": seo_title,
            "meta_description": meta_description,
            "content_html": content_html,
            "images": images,
            "video_url": video_url,
            "wiki_url": wiki_url,
            "pub_date_rfc822": pub_date_rfc822,
            "hash": h,
        }
        items.append(item)

        html_out = build_article_html(item, published_iso, date_label)
        with open(os.path.join(ARTICLES_DIR, f"{item['slug']}.html"), "w", encoding="utf-8") as f:
            f.write(html_out)

        history[h] = {"title": title, "date": date_slug, "published": published_iso}

    print(f"{len(items)} yeni içerik üretildi (hedef: {MIN_ITEMS_TARGET}+ ya da mevcut tüm olaylar).")

    rss_xml = build_rss(items, published_iso)
    with open(os.path.join(OUTPUT_DIR, "rss.xml"), "w", encoding="utf-8") as f:
        f.write(rss_xml)

    save_history(history)
    print("Tamamlandı.")


if __name__ == "__main__":
    main()
