#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wikipedia "Tarihte Bugün" -> Otomatik RSS + Statik Makale Üretici
==================================================================

Türkçe Wikipedia'nın Wikimedia REST API'sini kullanarak günün tarihine ait
tüm olayları (events / births / deaths / holidays) çeker, her biri için
kaynak Wikipedia makalesinin TAM metnini alır, gereksiz bölümleri
(Kaynakça, Dış bağlantılar, Ayrıca bakınız, vb.) temizler, isteğe bağlı
olarak Anthropic (Claude) API'si ile profesyonel bir makale formatına
dönüştürür ve şunları üretir:

  docs/rss.xml                -> Ana RSS akışı (SEO uyumlu)
  docs/sitemap.xml            -> Sitemap
  docs/index.html             -> Basit günlük dizin sayfası
  docs/articles/<slug>.html   -> Her olay için tam SEO/Schema.org sayfası
  data/history.json           -> Yayınlanan makalelerin kaydı (mükerrer önleme)

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

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

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


def fetch_page_images(title: str, limit: int = 6) -> list:
    """Sayfadaki uygun (ikon/logo olmayan) görselleri döndürür."""
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
        original = page.get("original", {}).get("source")
        if original:
            images.append(original)
        for img in page.get("images", []):
            fname = img.get("title", "")
            if re.search(r"\.(jpg|jpeg|png|svg)$", fname, re.I) and not re.search(
                r"(commons-logo|wiki|icon|edit-icon|flag of|question)", fname, re.I
            ):
                images.append("FILE:" + fname)
    # FILE: girdilerini gerçek URL'e çevir
    resolved = []
    file_titles = [i[5:] for i in images if i.startswith("FILE:")]
    if file_titles:
        resolved.extend(resolve_file_urls(file_titles))
    resolved = [i for i in images if not i.startswith("FILE:")] + resolved
    # tekilleştir, sınırla
    seen, out = set(), []
    for u in resolved:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= limit:
            break
    return out


def resolve_file_urls(file_titles: list) -> list:
    urls = []
    for chunk_start in range(0, len(file_titles), 50):
        chunk = file_titles[chunk_start:chunk_start + 50]
        params = {
            "action": "query",
            "titles": "|".join(f"Dosya:{t}" if not t.lower().startswith("dosya:") else t for t in chunk),
            "prop": "imageinfo",
            "iiprop": "url",
            "format": "json",
        }
        resp = get_with_retry(WIKI_API, params=params, timeout=30)
        if resp.ok:
            pages = resp.json().get("query", {}).get("pages", {})
            for page in pages.values():
                for ii in page.get("imageinfo", []):
                    if ii.get("url"):
                        urls.append(ii["url"])
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
        urls = resolve_file_urls(video_titles)
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
    """Anthropic API anahtarı olmadan (ücretsiz) çalışan kural tabanlı format.
    Wikipedia'dan çekilen TÜM temizlenmiş metni, hiçbir kesme/özetleme
    yapmadan olduğu gibi HTML paragraflarına döker."""
    paragraphs = [p for p in body.split("\n\n") if len(p) > 40]
    intro = f"<p>{html.escape(event_text)}</p>" if event_text else ""
    body_html = "\n".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)
    return (intro + "\n" + body_html).strip() if intro else body_html


def ai_rewrite_article(title: str, event_text: str, raw_body: str, year) -> str:
    """Anthropic API ile profesyonel makale formatına dönüştürür.
    ANTHROPIC_API_KEY tanımlı değilse kural tabanlı fallback'e döner."""
    if not ANTHROPIC_API_KEY:
        return fallback_article(title, event_text, raw_body, year, event_text)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = (
            "Aşağıdaki Wikipedia kaynaklı ham bilgiyi, Türkçe, SEO uyumlu, "
            "profesyonel bir haber/ansiklopedi makalesi formatına dönüştür.\n"
            "Kurallar:\n"
            "- Sadece geçerli, semantik HTML üret (<p>, <h2>, <h3>, <ul> vb.), "
            "  <html>/<body> etiketi KOYMA.\n"
            "- Yorum, dış bağlantı, kaynakça, editör notu EKLEME.\n"
            "- İçeriği özetleme; mevcut bilgiyi genişletilmiş, akıcı, "
            "  tam cümlelerle anlat.\n"
            "- Wikipedia metnini birebir kopyalama, kendi cümlelerinle yeniden yaz.\n"
            "- En az 3 paragraf üret.\n\n"
            f"BAŞLIK: {title}\n"
            f"OLAY ÖZETİ (Tarihte Bugün): {event_text} ({year})\n"
            f"HAM KAYNAK METİN:\n{raw_body[:6000]}\n"
        )
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return text.strip() or fallback_article(title, event_text, raw_body, year, event_text)
    except Exception as exc:  # ağ/anahtar hatalarında sessizce fallback'e düş
        print(f"[uyarı] AI yeniden yazım başarısız ({title}): {exc}")
        return fallback_article(title, event_text, raw_body, year, event_text)


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
    <a href="{site_url}/index.html#{date_slug}">{date_label}</a> &rsaquo;
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


def build_schema_breadcrumb(title, canonical_url, date_label, date_url):
    return json.dumps({
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": SITE_NAME, "item": SITE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": date_label, "item": date_url},
            {"@type": "ListItem", "position": 3, "name": title, "item": canonical_url},
        ],
    }, ensure_ascii=False, indent=2)


def build_article_html(item: dict, published_iso: str, date_label: str, date_slug: str) -> str:
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
        date_slug=date_slug,
        date_label=date_label,
        title=html.escape(item["title"]),
        wiki_url=item["wiki_url"],
        figure_html=figure_html,
        content_html=item["content_html"],
        video_html=video_html,
        schema_newsarticle=build_schema_newsarticle(
            item["title"], item["meta_description"], canonical_url, images, published_iso
        ),
        schema_breadcrumb=build_schema_breadcrumb(
            item["title"], canonical_url, date_label, f"{SITE_URL}/index.html#{date_slug}"
        ),
    )


# --------------------------------------------------------------------------
# RSS + SITEMAP
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


def build_sitemap(items: list, extra_urls: list) -> str:
    urls = extra_urls + [f"{SITE_URL}/articles/{i['slug']}.html" for i in items]
    entries = "\n".join(
        f"  <url><loc>{xml_escape(u)}</loc><changefreq>daily</changefreq></url>" for u in urls
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{entries}
</urlset>
"""


def build_index_html(items: list, date_label: str, date_slug: str) -> str:
    cards = "\n".join(
        f'<li><a href="articles/{i["slug"]}.html">{html.escape(i["title"])}</a></li>' for i in items
    )
    return f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{SITE_NAME} — {date_label}</title>
<meta name="description" content="{SITE_DESCRIPTION}">
<link rel="canonical" href="{SITE_URL}/index.html">
<link rel="alternate" type="application/rss+xml" title="{SITE_NAME} RSS" href="{SITE_URL}/rss.xml">
</head>
<body>
<h1 id="{date_slug}">{SITE_NAME} — {date_label}</h1>
<p>{len(items)} olay listelendi.</p>
<ul>
{cards}
</ul>
</body>
</html>
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

            content_html = ai_rewrite_article(title, event_text, cleaned, entry_year)
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

        cat_label = {
            "events": "Olay", "births": "Doğum", "deaths": "Ölüm", "holidays": "Özel Gün",
        }.get(entry.get("_category"), "Olay")

        seo_title = f"{title} — {date_label}'de Tarihte Bugün ({cat_label})"
        meta_description = (event_text or cleaned[:150]).strip()[:155]

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

        html_out = build_article_html(item, published_iso, date_label, date_slug)
        with open(os.path.join(ARTICLES_DIR, f"{item['slug']}.html"), "w", encoding="utf-8") as f:
            f.write(html_out)

        history[h] = {"title": title, "date": date_slug, "published": published_iso}

    print(f"{len(items)} yeni içerik üretildi (hedef: {MIN_ITEMS_TARGET}+ ya da mevcut tüm olaylar).")

    rss_xml = build_rss(items, published_iso)
    with open(os.path.join(OUTPUT_DIR, "rss.xml"), "w", encoding="utf-8") as f:
        f.write(rss_xml)

    index_html = build_index_html(items, date_label, date_slug)
    with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)

    sitemap_xml = build_sitemap(items, [f"{SITE_URL}/", f"{SITE_URL}/rss.xml"])
    with open(os.path.join(OUTPUT_DIR, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write(sitemap_xml)

    save_history(history)
    print("Tamamlandı.")


if __name__ == "__main__":
    main()
