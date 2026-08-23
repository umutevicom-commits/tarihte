#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wikipedia "Tarihte Bugün" -> Otomatik RSS + Statik Makale Üretici
==================================================================

Türkçe Wikipedia'nın Wikimedia REST API'sini kullanarak günün tarihine ait
tüm olayları (events / births / deaths / holidays) çeker; bunlardan YALNIZCA
"Mucitler, İcatlar, Keşifler" konu havuzuna giren olayları seçer (bkz.
TECHNOLOGY_TOPIC_RE ve is_technology_topic() — dinî, siyasi, askerî,
kültürel, spor, sanat, ekonomi/piyasa, genel gündelik teknoloji kullanımı
vb. HER TÜRLÜ diğer konu otomatik olarak elenir; bir olay yalnızca somut
bir İCAT, KEŞİF/BULUŞ ya da tanınmış bir MUCİT/bilim insanının doğum-ölüm
kaydıysa kabul edilir), seçilen her olay için kaynak Wikipedia makalesinin
TAM metnini alır (hiçbir özetleme yapılmaz), gereksiz bölümleri (Kaynakça,
Dış bağlantılar, Ayrıca bakınız, vb.) temizler, kural tabanlı (harici bir
yapay zekâ servisine bağlı olmayan) bir biçimlendirmeyle makale haline
getirir ve şunları üretir:

  docs/rss.xml                -> Ana RSS akışı (SEO uyumlu, SADECE Mucitler/İcatlar/Keşifler)
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
SITE_DESCRIPTION = (
    "Türkçe Wikipedia kaynaklı, her gün otomatik güncellenen; SADECE Mucitler, "
    "İcatlar ve Keşifler konulu 'Tarihte Bugün' arşivi."
)
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
    """Latin alfabesi dışında (Çince, Arapça, Kiril vb.) veya tamamen
    sembol/emoji'den oluşan başlıklarda translit + regex temizliği,
    girdinin Latin/rakam DIŞINDAKİ her karakterini siler. Bu, iki ayrı
    riskli senaryoya yol açar:

      1) Girdinin TAMAMI Latin olmayan alfabedeyse/sembol-emoji'yse,
         temizlik sonucu tamamen BOŞ string'e düşer.
      2) Girdinin yalnızca BİR KISMI (ör. başlığın kendisi) Latin
         olmayan ama geri kalanı (ör. "-08-23-births" gibi tarih/
         kategori eki) Latin/rakamsa, temizlik sonucu boş DEĞİLDİR ama
         yalnızca o ortak Latin/rakam parçasından ibarettir — yani
         FARKLI (yalnızca Latin olmayan kısımda ayrışan) girdiler AYNI
         slug'ı üretir. Önceki sürümde bu ikinci senaryo hiç ele
         alınmıyordu (yalnızca 1. senaryo için, üstelik zaten boşalmış
         `text` üzerinden hesaplanan hatalı bir md5 fallback vardı) ve
         her iki senaryo da farklı olayların aynı slug'ı paylaşıp
         birbirinin makale dosyasının üzerine yazmasına (çakışma/veri
         kaybı) yol açabiliyordu.

    Kalıcı çözüm: slug'ın sonuna HER ZAMAN, dönüştürülmemiş ORİJİNAL
    girdinin kısa bir hash'i eklenir. Böylece Latin kısım okunabilir bir
    önek olarak korunur, ama her farklı orijinal girdi — Latin olmayan
    bölümü ne olursa olsun — kendine özgü, çakışmasız bir slug üretir."""
    original = text
    tr_map = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    text = text.translate(tr_map)
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    text = text[:80]
    digest = hashlib.md5(original.encode("utf-8")).hexdigest()[:10]
    return f"{text}-{digest}" if text else digest


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
# İÇERİK KAPSAMI: YALNIZCA "MUCİTLER, İCATLAR, KEŞİFLER"
# --------------------------------------------------------------------------
# Bu site YALNIZCA aşağıdaki üç temaya giren "tarihte bugün" olaylarını
# yayınlar:
#
#   1) MUCİTLER   -> bir icadın/keşfin sahibi olan kişiler (doğum/ölüm
#                    kayıtları dahil — bkz. KNOWN_TECH_FIGURES_RE)
#   2) İCATLAR    -> somut bir alet/makine/sistemin ilk kez üretilmesi,
#                    tasarlanması, patentlenmesi
#   3) KEŞİFLER   -> bilimsel bir gerçeğin, yerin, türün, ilkenin,
#                    gök cisminin vb. ilk kez ortaya çıkarılması
#
# Dinî, siyasi, askerî, kültürel, sanatsal, sportif, ekonomik/piyasa
# (ör. "Bitcoin çöktü") ve GÜNLÜK/GENEL teknoloji KULLANIMI haberleri
# (ör. "İnternet erişimi kesildi", "sosyal medya yasaklandı") — kategorisi
# ne olursa olsun (events, births, deaths) — otomatik olarak elenir.
# "holidays" kategorisi (dinî/millî/kültürel özel günler) hiçbir istisna
# olmadan tamamen dışlanır.
#
# Kural tabanlı bir anahtar kelime filtresi kullanılır (dış bir AI/LLM
# servisi olmadığından %100 anlam çözümlemesi garanti edilemez). Filtre
# kasıtlı olarak İKİ katmana ayrılmıştır:
#
#   * TECHNOLOGY_TOPIC_RE  -> yalnızca KENDİNE ÖZGÜ, başka bağlamda
#     (kaza/piyasa/siyaset/gündelik kullanım haberi vb.) neredeyse hiç
#     geçmeyen, doğrudan bir İCAT/KEŞİF olayına işaret eden terimler.
#     Bu terimler tek başına (bağlam aranmadan) KOŞULSUZ kabul edilir.
#
#   * AMBIGUOUS_TECH_TERMS_RE -> "internet", "bilgisayar", "telefon",
#     "yapay zeka", "bitcoin" gibi GÜNÜMÜZDE son derece yaygın, İCAT/
#     KEŞİF dışı bağlamlarda da (kesinti, yasak, piyasa haberi, gündelik
#     kullanım vb.) sürekli geçen genel terimler. Bunlar TEK BAŞINA hiçbir
#     zaman yeterli sayılmaz; yalnızca aynı metinde AYRICA açık bir icat/
#     keşif/buluş SİNYALİ (bkz. INVENTION_SIGNAL_RE) de varsa kabul edilir
#     (ör. "Telefonu icat eden Alexander Graham Bell..." kabul edilir, ama
#     yalnızca "Akıllı telefon satışları arttı" reddedilir).
#
# Bir olay şüpheli/belirsizse (hiçbir katmanda eşleşme yoksa) YAYINLANMAZ
# — yani filtre "emin değilsen atla" mantığıyla, permissive değil,
# RESTRICTIVE çalışır. Bu, kelime havuzuna "Mucitler, İcatlar, Keşifler"
# dışında hiçbir konunun (ör. genel "teknoloji şirketi haberi",
# "teknoloji fuarı", "yönetmelik", "yatırım turu" gibi icat/keşif İÇERMEYEN
# teknoloji haberleri) sızmamasını garanti eder.

TECHNOLOGY_TOPIC_RE = re.compile(
    # --- Ana kökler: İCAT / BULUŞ / KEŞİF / PATENT / MUCİT --------------
    # Türkçe'de bu kökler ünsüz yumuşamasına uğrar (icat->icadı,
    # keşif->keşfi, mucit->mucidi). KÖK TABANLI eşleştirme (\w*) her
    # çekim ekini (buluşları, buluşundan, keşiflerin, icatlarıyla,
    # mucitlerin vb.) otomatik kapsar. Bu kökler bu sitenin TEK gerçek
    # kapsamıdır: MUCİTLER, İCATLAR, KEŞİFLER.
    r"\bica[dt]\w*|\bbuluş\w*|\bkeşif\w*|\bkeşf\w*|\bpatent\w*|\bmucit\w*|\bmucid\w*|"
    # NOT: "ilk kez" / "ilk defa" ifadeleri BİLİNÇLİ OLARAK burada bare
    # (tek başına) YOKTUR — Türkçe'de bu ifadeler spor ("ilk kez şampiyon
    # oldu"), siyaset ("ilk kez başkan seçildi"), tarih vb. HER TÜRLÜ
    # "ilk oluş" haberinde geçer ve icat/keşifle hiçbir ilgisi olmayabilir.
    # Bu yüzden yalnızca aşağıdaki gibi açık bir icat/geliştirme FİİLİYLE
    # BİRLİKTE geçtiğinde (ör. "ilk kez üretildi/geliştirildi/keşfedildi")
    # kabul edilir (bkz. bu bloğun altındaki "ilk kez (üretil|geliştiril|
    # ...)" kalıbı).
    r"\byenilikçi\w*|\binovasyon\w*|\bprototip\w*|"
    # --- Yazı, bilgi ve matbaa -------------------------------------------
    # NOT: aşağıdaki terimler, Türkçe Wikipedia "tarihte bugün" verisinde
    # neredeyse İSTİSNASIZ tarihî bir İCAT/KEŞİF olayına işaret eden,
    # kendine özgü (başka bağlamda pratikte hiç geçmeyen) sözcüklerdir; bu
    # yüzden BAĞLAM ARANMADAN koşulsuz kabul edilir (tıpkı orijinal
    # tasarımda olduğu gibi). Yalnızca GÜNÜMÜZ GÜNDELİK KULLANIMINDA da sık
    # geçen (ör. "internet", "telefon", "bitcoin") terimler bilinçli olarak
    # BURADA DEĞİL, aşağıdaki AMBIGUOUS_TECH_TERMS_RE'de tutulur.
    r"\balfabe\w*|hiyeroglif|çivi yazısı|\bpapirüs\b|\bparşömen\b|"
    r"\bmatbaa\w*|gutenberg|\bdaktilo\b|"
    # --- Tarım ve gıda teknolojisi ----------------------------------------
    r"tarım devrimi|neolitik devrim|sulama sistemi|\bsaban\b|\bpulluk\b|"
    r"su değirmeni|yel değirmeni|"
    # --- Mekanik, mühendislik, metalurji ------------------------------------
    r"arşimet vidası|\bkatapult\w*|\botomata\b|antik otomasyon|"
    r"bronz çağı|demir çağı|çelik üretimi|metalurji\w*|"
    # --- Saat, ölçüm, navigasyon --------------------------------------------
    r"güneş saati|su saati|kum saati|mekanik saat|astronomik saat|"
    r"\bpusula\b|manyetik pusula|"
    r"ilk takvim|gregoryen takvim|maya takvimi|"
    # --- Astronomi ve uzay ---------------------------------------------------
    r"\buydu\b|\broket\b|uzay (aracı|mekiği|istasyonu|görevi|programı|kolonisi|madenciliği|asansörü)|"
    r"\bnasa\b|\besa\b|uzaya (fırlat|çıktı|gönderildi)|ay'a (iniş|inen|indi)|"
    r"\bgezegen\w*|\byıldız\w* keşf\w*|\bgalaksi\w*|kuyruklu yıldız|\basteroit\w*|\bkomet\w*|"
    r"kara delik|\bevren\w*|big bang|büyük patlama|\bteleskop\w*|gökada|heliosentrik|kepler yasaları|"
    r"kütleçekim dalgaları|ötegezegen|karanlık madde|karanlık enerji|kozmik mikrodalga|"
    # --- Denizcilik ve haritacılık --------------------------------------------
    r"\bdenizaltı\w*|\bsonar\b|\bradar\b|yelkenli gemi|"
    r"coğrafi keşif\w*|dünya haritası|\bharitacılık\b|"
    # --- Matematik -------------------------------------------------------------
    r"sıfırın keşfi|\bcebir\b|\bkalkülüs\b|trigonometri|"
    # --- Ulaşım ve havacılık ----------------------------------------------------
    r"\buçak\b|ilk uçuş|havacılık|\botomobil\b|buhar makinesi|dizel motor|"
    r"wright kardeşler|"
    r"lokomotif|montaj hattı|seri üretim|sanayi devrimi|"
    r"\bdrone\b|\bdron\b|insansız hava aracı|otonom araç|sürücüsüz araç|\bhyperloop\b|uçan araba|"
    # --- Elektrik ve enerji -------------------------------------------------------
    r"jeneratör|transistör|mikroçip|\bişlemci\b|entegre devre|yarı iletken|"
    r"süperiletken|nanoteknoloji|biyoteknoloji|\bkuantum\b|\bgrafen\b|kompozit malzeme|"
    r"güneş paneli|fotovoltaik|rüzgar türbini|yakıt hücresi|nükleer füzyon|"
    # --- İletişim ve görüntü -----------------------------------------------------
    r"mors alfabesi|"
    r"fotoğraf makinesi|\bsinema\b|gramofon|\bplak\b|"
    # --- Bilimsel araçlar ve fizik --------------------------------------------------
    r"\bmikroskop\w*|x[- ]ışın|\blazer\b|nükleer (reaktör|enerji|santral)|higgs bozonu|"
    r"atom altı parçacık|periyodik tablo|kimyasal element|\belement\w* keşf\w*|"
    # --- Tıp, biyoloji, genetik ---------------------------------------------------
    r"\başı\b|antibiyotik|penisilin|\bdna\b|genom|kök hücre|kalp nakli|genetik mühendisl|"
    r"crispr|gen (düzenleme|tedavisi)|"
    # --- Arkeoloji -------------------------------------------------------------------
    r"\bfosil\w*|arkeolojik (buluntu|keşif|kazı)|\bkazı\w* (sırasında|sonucu)|kalıntı(sı)? bulun|"
    # --- Genel icat/geliştirme fiilleri ve bağlamsal eşleşmeler ----------------------
    r"\bprototip\b|geliştirdi|geliştirilen|geliştirilmiş|tasarladı|icat etti|"
    r"ilk kez (üretil|geliştiril|kullanıl|çalıştırıl|test edil|keşfedil)|"
    # --- Bilim insanı/mühendis KİMLİĞİ — YALNIZCA icat/keşif bağlamıyla birlikte ------
    # (bare "mühendis"/"bilim insanı" kelimeleri tek başına ALAKASIZ bir
    # kaza/atama/röportaj haberinde de geçebileceğinden koşulsuz kabul
    # EDİLMEZ; yalnızca aynı cümlede AYRICA bir icat/keşif fiili varsa
    # kabul edilir.)
    r"\bmühendis\b.{0,60}(icat|geliştir|tasarla|buluş|patent|keşf)|"
    r"(icat|geliştir|tasarla|buluş|patent|keşf).{0,60}\bmühendis\b|"
    r"bilim insan.{0,40}(icat|keşif|keşf|buluş|geliştir|patent)|"
    r"(icat|keşif|keşf|buluş|geliştir|patent).{0,40}bilim insan",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# TANINMIŞ MUCİT / BİLİM İNSANI TESPİTİ (İKİNCİ KATMAN)
# --------------------------------------------------------------------------
# Wikimedia "On This Day" verisindeki doğum/ölüm (births/deaths) kayıtları
# çoğu zaman yalnızca "Isaac Newton doğdu." gibi ÇOK KISA, anahtar kelime
# içermeyen bir cümledir — TECHNOLOGY_TOPIC_RE'deki hiçbir kelime bu tür
# bir cümlede geçmez, dolayısıyla tarihin en önemli MUCİT/bilim insanlarının
# doğum/ölüm günleri kaçırılabilir. Bunu gidermek için, madde başlığı
# (Wikipedia sayfa adı — genelde kişinin tam adıdır) tanınmış mucit/bilim
# insanı isimleriyle karşılaştırılır. Yalnızca TAM AD (ör. "James Watt",
# "Alexander Graham Bell") eşleştirilir; "Watt" veya "Bell" gibi tek
# başına çok genel/çok anlamlı kısa soyadları KASITLI OLARAK kullanılmaz
# (aksi halde alakasız kişi/yer adlarıyla yanlış eşleşme riski doğar).
# Bu liste yalnızca gerçekten bir İCAT ya da bir KEŞFİN sahibi olan
# kişilerden oluşur (ör. genel bir "girişimci"/"CEO" değil, İCAT/KEŞİF
# yapmış bir MUCİT ya da bilim insanı).
KNOWN_TECH_FIGURES = [
    # Antik / İslam dünyası bilim insanları ve mucitler
    "Arşimet", "Öklid", "Pisagor", "Eratosthenes", "Heron", "Hipokrat", "Galen",
    "İbn Sina", "İbnü'l-Heysem", "İbn El-Heysem", "El-Harezmi", "El-Bîrûnî", "El-Biruni",
    "İbnü'n-Nefis", "El-Cezeri", "Cezeri", "Uluğ Bey", "Takiyüddin",
    "Piri Reis", "Mimar Sinan",
    # Rönesans - Bilimsel Devrim
    "Leonardo da Vinci", "Nicolaus Copernicus", "Kopernik", "Johannes Kepler",
    "Galileo Galilei", "Isaac Newton", "Robert Hooke", "Antonie van Leeuwenhoek",
    "Robert Boyle",
    # 19. yüzyıl ve elektrik/kimya/biyoloji öncüleri
    "Michael Faraday", "James Watt", "Alessandro Volta", "André-Marie Ampère",
    "Georg Ohm", "James Clerk Maxwell", "Charles Darwin", "Gregor Mendel",
    "Louis Pasteur", "Joseph Lister", "Alexander Fleming", "Marie Curie",
    "Nikola Tesla", "Thomas Edison", "Alexander Graham Bell", "Guglielmo Marconi",
    "Wilhelm Röntgen", "Henry Ford", "Wright kardeşler",
    # 20. yüzyıl fizik / bilgisayar bilimi öncüleri
    "Albert Einstein", "Max Planck", "Niels Bohr", "Ernest Rutherford",
    "James Chadwick", "Alan Turing", "John von Neumann", "Claude Shannon",
    "Tim Berners-Lee", "Steve Jobs", "Bill Gates", "Dennis Ritchie", "Linus Torvalds",
    # Kadın bilim insanları ve mucitler
    "Ada Lovelace", "Grace Hopper", "Hedy Lamarr", "Rosalind Franklin",
    "Katherine Johnson", "Dorothy Vaughan", "Mary Jackson", "Stephanie Kwolek",
    "Emmanuelle Charpentier", "Jennifer Doudna",
]

KNOWN_TECH_FIGURES_RE = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in sorted(KNOWN_TECH_FIGURES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# BELİRSİZ (TEK BAŞINA YETERSİZ) TERİMLER — BAĞLAM ZORUNLU (3. KATMAN)
# --------------------------------------------------------------------------
# "motor", "elektrik", "pil", "telefon", "radyo", "televizyon", "telgraf",
# "disk", "cd", "dvd", "usb", "internet", "bilgisayar", "yazılım", "yapay
# zeka", "robot", "sosyal medya", "bitcoin" vb. kelimeler İCAT/KEŞİF
# bağlamında da, TAMAMEN ALAKASIZ bağlamlarda da (ör. "motor kazası",
# "elektrik kesintisi", "radyo istasyonu kapatıldı", "internet erişimi
# kesildi", "bitcoin değer kaybetti", "sosyal medya hesabı hacklendi")
# geçebilir. Bu yüzden TECHNOLOGY_TOPIC_RE'nin KOŞULSUZ eşleşen ana
# listesinden BİLİNÇLİ OLARAK çıkarılmıştır. Bu terimler yalnızca, aynı
# metinde AYRICA açık bir icat/buluş/keşif SİNYALİ de varsa kabul edilir
# (bkz. is_technology_topic). Böylece "İlk telefon görüşmesi Alexander
# Graham Bell tarafından gerçekleştirildi, telefonu icat etti" gibi
# gerçek icat haberleri kaçırılmaz, ama "motor kazasında X kişi öldü" ya
# da "Bitcoin son bir haftada %10 değer kazandı" gibi Mucitler/İcatlar/
# Keşifler kapsamı DIŞINDAKİ haberler artık sızmaz.
AMBIGUOUS_TECH_TERMS_RE = re.compile(
    r"\bmotor\b|\belektrik\b|\bpil\b|\btelefon\b|cep telefonu|akıllı telefon|"
    r"\bradyo\b|\btelevizyon\b|\btelgraf\b|\bdisk\b|\bcd\b|\bdvd\b|\busb\b|"
    r"\binternet\b|world wide web|\bweb sitesi\b|\bbilgisayar\b|\byazılım\b|\bdonanım\b|"
    r"yapay zeka|\brobot\b|robotik|algoritma|programlama dili|işletim sistemi|"
    r"arama motoru|sosyal medya|e-posta|elektronik posta|wi[- ]?fi\b|bluetooth|\bgps\b|"
    r"nesnelerin interneti|\biot\b|giyilebilir teknoloji|akıllı ev|dijital ikiz|"
    r"biyometrik teknoloji|beyin bilgisayar arayüzü|\bmetaverse\b|kuantum internet|"
    r"sanal gerçeklik|artırılmış gerçeklik|karma gerçeklik|genişletilmiş gerçeklik|"
    r"büyük dil modeli\w*|3[- ]?boyutlu yazıcı|3d yazıcı|"
    r"şifreleme|kriptografi|\bblockchain\b|blok zinciri|\bbitcoin\b|\bethereum\b|kripto para|"
    r"\bnft\b|akıllı sözleşme|\bteknoloji\w*|\bmühendislik\w*|\bmühendis\b|"
    r"\bbilim insan\w*|\bbilim (adamı|kadını|insanı)\b|\bfizikçi\b|\bkimyager\b|"
    r"\bastronom\w*|\bbiyolog\b|\buydu\b|\broket\b|\bdenizaltı\w*|\bsonar\b|\bradar\b|"
    r"\buçak\b|havacılık|\botomobil\b|jeneratör|transistör|mikroçip|\bişlemci\b|"
    r"entegre devre|yarı iletken|süperiletken|nanoteknoloji|biyoteknoloji|\bkuantum\b|"
    r"\bgrafen\b|kompozit malzeme|güneş paneli|fotovoltaik|rüzgar türbini|"
    r"yakıt hücresi|nükleer füzyon|\bmikroskop\w*|x[- ]ışın|\blazer\b|"
    r"nükleer (reaktör|enerji|santral)|higgs bozonu|periyodik tablo|"
    r"\başı\b|antibiyotik|penisilin|\bdna\b|genom|kök hücre|kalp nakli|"
    r"genetik mühendisl|crispr|gen (düzenleme|tedavisi)|\bfosil\w*",
    re.IGNORECASE,
)

INVENTION_SIGNAL_RE = re.compile(
    r"\bica[dt]\w*|\bbuluş\w*|\bkeşif\w*|\bkeşf\w*|\bpatent\w*|\bmucit\w*|\bmucid\w*|"
    r"\bprototip\b|geliştir\w*|tasarla\w*|"
    r"\byenilik\w*çi\w*|\binovasyon\w*|ilk kez|ilk defa",
    re.IGNORECASE,
)


def is_technology_topic(event_text: str, title: str, category: str) -> bool:
    """Yalnızca "Mucitler, İcatlar, Keşifler" temasına giren olaylar için
    True döner. Bu fonksiyon site kapsamının SADECE bu temayla sınırlı
    kalmasını sağlayan ana kapıdır — dinî, siyasi, askerî, kültürel,
    ekonomik ve GÜNDELİK teknoloji kullanımı dahil diğer TÜM konular
    burada elenir.

    Üç katmanlı kontrol yapılır:
      1) TECHNOLOGY_TOPIC_RE — doğrudan bir İCAT/KEŞİF/BULUŞ olayına
         işaret eden, KENDİNE ÖZGÜ (başka bağlamda neredeyse hiç
         geçmeyen) terimlerden oluşan dar bir havuz. Bu terimler tek
         başına (bağlam aranmadan) koşulsuz kabul edilir.
      2) KNOWN_TECH_FIGURES_RE — tanınmış bir MUCİDİN/bilim insanının tam
         adı doğrudan başlıkta (Wikipedia madde adı) geçiyorsa, olay
         metninde hiçbir anahtar kelime olmasa bile (ör. sade "X doğdu."
         gibi doğum/ölüm kayıtlarında) True döner.
      3) AMBIGUOUS_TECH_TERMS_RE + INVENTION_SIGNAL_RE — "internet",
         "telefon", "yapay zeka", "bitcoin", "disk" gibi TEK BAŞINA
         Mucitler/İcatlar/Keşifler dışı bağlamlarda da (kaza, kesinti,
         piyasa haberi, gündelik kullanım vb.) geçebilen genel/belirsiz
         terimler, yalnızca metinde AYRICA açık bir icat/buluş/keşif
         sinyali de varsa kabul edilir; aksi halde reddedilir.

    Bu üç katmanın DIŞINDA kalan HİÇBİR olay yayınlanmaz — yani kelime
    havuzu, tanım gereği, "Mucitler, İcatlar, Keşifler" dışında hiçbir
    konuyu içermez.
    """
    if category == "holidays":
        return False  # dinî/millî/kültürel özel günler asla bu kapsama girmez
    combined = f"{event_text} {title}"
    if TECHNOLOGY_TOPIC_RE.search(combined):
        return True
    if category in ("births", "deaths") and KNOWN_TECH_FIGURES_RE.search(title):
        return True
    if AMBIGUOUS_TECH_TERMS_RE.search(combined) and INVENTION_SIGNAL_RE.search(combined):
        return True
    return False


# --------------------------------------------------------------------------
# DİNÎ HASSASİYET: PEYGAMBER İSİMLERİNE "HZ." UNVANI EKLEME
# --------------------------------------------------------------------------
# NOT: Site artık SADECE "Mucitler, İcatlar, Keşifler" konularını
# yayınladığından (bkz. yukarıdaki is_technology_topic), pratikte dinî
# içerikli bir olay
# zaten seçilmeyecektir. Aşağıdaki fonksiyon yine de EK bir güvenlik katmanı
# olarak korunmuştur — örn. bir icadın açıklaması içinde peygamberlerden biri
# yan bir bilgi olarak (parantez içi vb.) geçerse, çıplak isimle anılmasını
# engellemek için.
# İslam inanışında peygamber kabul edilen isimlerden bahsedilirken çıplak
# isim yerine "Hz." (Hazreti) unvanıyla hitap edilir. Kaynak Wikipedia
# metinleri genelde unvansız ("Muhammed", "İbrahim" vb.) yazıldığından, bu
# script çıplak geçen her peygamber ismine otomatik olarak "Hz." ekler.
#
# ÖNEMLİ TASARIM NOTU: "Muhammed" dışındaki peygamber isimlerinin çoğu
# (İbrahim, Musa, Yusuf, Davud, Süleyman, Yahya, Harun, Yunus, İsmail,
# İshak, Yakup, İsa, Zekeriya vb.) günümüzde de son derece yaygın kişi
# adlarıdır — bir sporcu, general, siyasetçi ya da bilim insanı bu
# isimlerden herhangi birini taşıyabilir. Bu isimlere HER geçtiği yerde
# körü körüne "Hz." eklemek, sıradan bir kişiyi yanlışlıkla peygamber gibi
# göstererek daha ciddi bir dinî hassasiyet hatasına yol açar. Bu yüzden:
#   - "Muhammed" / "Muhammet" için: bu site "Tarihte Bugün" / tarihî
#     içerik ürettiğinden, çıplak geçen "Muhammed" neredeyse istisnasız
#     İslam peygamberine işaret eder; bu yüzden doğrudan (bağlam
#     aranmadan) "Hz." eklenir.
#   - DİĞER 24 peygamber ismi için: yalnızca yakın bağlamda ("peygamber",
#     "nebî/nebi", "resûl/resul", "elçi" gibi) açık bir dinî bağlam
#     sinyali varsa "Hz." eklenir; aksi halde isim olduğu gibi bırakılır
#     (böylece sıradan bir kişi yanlışlıkla peygamber ilan edilmez).
# Ayrıca her iki durumda da: isim hemen ardından başka büyük harfli bir
# kelimeyle (ör. "Muhammed Ali", "Muhammed bin Selman", "Musa Çelik" gibi
# modern/tam bir özel ismin parçası olabilecek durumlar) devam ediyorsa
# dokunulmaz; zaten "Hz." veya "Hazreti" ile başlıyorsa tekrar eklenmez.

PROPHET_NAME_CONTEXT_REQUIRED = {
    "Muhammed": False,
    "Muhammet": False,
    "İbrahim": True,
    "Musa": True,
    "İsa": True,
    "Nuh": True,
    "Âdem": True,
    "Adem": True,
    "Yusuf": True,
    "Yakup": True,
    "Yakub": True,
    "İshak": True,
    "İsmail": True,
    "Davud": True,
    "Davut": True,
    "Süleyman": True,
    "Harun": True,
    "Yahya": True,
    "Zekeriya": True,
    "İlyas": True,
    "Elyesa": True,
    "Zülkifl": True,
    "Şuayb": True,
    "Hûd": True,
    "Hud": True,
    "Salih": True,
    "Lût": True,
    "Lut": True,
    "İdris": True,
    "Yunus": True,
}

_PROPHET_CONTEXT_SIGNAL_RE = re.compile(
    r"peygamber|nebî|nebi|resûl|resul|\belçi\b|vahiy|kur'an", re.IGNORECASE
)
_PROPHET_LINEAGE_CONNECTOR_RE = re.compile(r"^\s+(bin|ibn|b\.)\s+[A-ZÂÇĞİÖŞÜ]", re.IGNORECASE)
_PROPHET_NEXT_CAPITALIZED_RE = re.compile(r"^\s*[A-ZÂÇĞİÖŞÜ][\wâçğıöşü]*")

_PROPHET_NAME_PATTERNS = {
    name: re.compile(r"(?<!Hz\. )(?<!Hazreti )(?<!Hz )\b" + re.escape(name) + r"\b")
    for name in PROPHET_NAME_CONTEXT_REQUIRED
}


def _apply_honorifics_to_plain_text(segment: str) -> str:
    """Yalnızca DÜZ METİN üzerinde çalışır (HTML etiketi içermemelidir)."""
    for name, needs_context in PROPHET_NAME_CONTEXT_REQUIRED.items():
        pattern = _PROPHET_NAME_PATTERNS[name]

        def repl(m, _needs_context=needs_context, _segment=segment):
            start, end = m.span()
            after = _segment[end:end + 60]

            # "Muhammed Ali", "Musa Çelik" gibi hemen ardından büyük
            # harfli bir kelime gelen modern/tam özel isimlere dokunma.
            if _PROPHET_NEXT_CAPITALIZED_RE.match(after):
                return m.group(0)
            # "Muhammed bin Selman" / "Muhammed ibn Sina" gibi Arapça soy
            # bağlacıyla devam eden özel isimlere dokunma.
            if _PROPHET_LINEAGE_CONNECTOR_RE.match(after):
                return m.group(0)
            # Bağlam gerektiren isimlerde yakın çevrede dinî bir sinyal
            # yoksa (ör. sıradan bir kişi adıysa) dokunma.
            if _needs_context:
                window = _segment[max(0, start - 150):min(len(_segment), end + 150)]
                if not _PROPHET_CONTEXT_SIGNAL_RE.search(window):
                    return m.group(0)
            return f"Hz. {m.group(0)}"

        segment = pattern.sub(repl, segment)
    return segment


def apply_prophet_honorifics(text: str) -> str:
    """Metindeki (gerekirse HTML etiketleri arasındaki) çıplak peygamber
    isimlerine dinî hassasiyet gereği "Hz." unvanını ekler. HTML
    etiketlerinin kendisine (class/attribute vb.) dokunmaz."""
    if not text:
        return text
    if "<" not in text:
        return _apply_honorifics_to_plain_text(text)
    parts = re.split(r"(<[^>]+>)", text)
    for i, part in enumerate(parts):
        if part and not part.startswith("<"):
            parts[i] = _apply_honorifics_to_plain_text(part)
    return "".join(parts)


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
    # Yalnızca EŞLEŞMEYEN (tek sayıda geçen) tırnak işaretlerini kırp.
    # Önceki sürüm text.strip('"') ile SADECE metnin en başındaki/
    # sonundaki tırnağı siliyordu; bu, "Büyük patlama" teorisi... gibi
    # cümle içinde DENGELİ kullanılan bir tırnak çiftinde açılışı silip
    # kapanışı bırakarak sarkan/bozuk bir tırnak üretiyordu (ör.
    # `Büyük patlama" teorisi...`). Artık tırnak sayısı çiftse (dengeliyse)
    # dokunulmuyor; yalnızca kaynak metinden taşmış TEK (eşleşmesiz) bir
    # tırnak varsa baştan/sondan temizleniyor.
    quote_chars = '"“”„'
    if sum(text.count(c) for c in quote_chars) % 2 == 1:
        text = text.strip(quote_chars).strip()
    # başta/sonda kalabilecek noktalama artıklarını temizle
    text = text.strip(" -–—:;,")
    return text


def truncate_meta_description(text: str, limit: int = 155) -> str:
    """Meta açıklamayı, SEO başlığı üretimindeki (generate_seo_title)
    yaklaşımla TUTARLI şekilde, kelimenin ORTASINDAN kesmeden ve "…" ile
    biterek kısaltır. Önceki sürümde ham [:155] dilimleme kullanılıyordu
    ve bu, çoğu zaman bir kelimenin tam ortasında kesilmiş, tamamlanmamış
    görünen (ör. "...gereken devas") SEO açısından zayıf açıklamalar
    üretiyordu."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    truncated = text[:limit].rsplit(" ", 1)[0].rstrip(",;:.-")
    return f"{truncated}…" if truncated else text[:limit]


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

        # Wikipedia kaynaklı metinlerde NADİREN de olsa "]]>" dizisi
        # geçebilir (matematiksel/mantıksal ifade, alıntılanan kod
        # parçası, XML/HTML örneği vb. içinde). Bu dizi CDATA bloğunun
        # KAPANIŞ imzasıyla birebir aynı olduğundan, ham haliyle
        # gömülürse CDATA'yı olması gerekenden ERKEN kapatır ve ardından
        # gelen metin XML gövdesi olarak ayrıştırılmaya çalışılıp TÜM
        # RSS akışını (bu <item>'den sonraki her şeyi) bozar. Standart
        # çözüm: CDATA içinde geçen her "]]>" dizisini, mevcut bloğu
        # kapatıp ">" karakterini yeni bir CDATA bloğunda yeniden açarak
        # "]]]]><![CDATA[>" ile değiştirmek — bu, orijinal içeriği
        # (görsel olarak yeniden birleştiğinde) HİÇ değiştirmeden CDATA
        # yapısını geçerli tutar.
        raw_content = f"{images_html}{item['content_html']}"
        safe_content = raw_content.replace("]]>", "]]]]><![CDATA[>")
        content_encoded = f"<![CDATA[{safe_content}]]>"

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

    try:
        data = fetch_onthisday(month, day)
    except requests.exceptions.RequestException as exc:
        # Wikimedia "On This Day" API'sinden (tüm yeniden denemeler
        # tükendikten sonra) kalıcı bir hata alınırsa, script'i yarım/
        # bozuk bir durumda bırakmamak için burada güvenle DURUYORUZ.
        # docs/rss.xml ve data/history.json'a HİÇ dokunulmadı; yani site
        # bir önceki başarılı build ile yayında kalmaya devam eder ve
        # bir sonraki (zamanlanmış ya da manuel) çalıştırmada yeniden
        # denenir. İş akışının bunu fark edebilmesi için hatayla çıkıyoruz.
        print(f"[HATA] Wikimedia 'On This Day' API'sinden veri alınamadı: {exc}")
        print("Bu çalıştırma iptal edildi; docs/rss.xml ve data/history.json değiştirilmedi.")
        raise SystemExit(1)

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
        # Slug/URL/fetch/hash için kullanılan orijinal `title` değişkenine
        # DOKUNULMAZ (mevcut mükerrer-önleme ve Wikipedia sorguları
        # bozulmasın diye). Okuyucuya gösterilecek her yerde (başlık,
        # schema, alt metni vb.) bunun yerine dinî hassasiyet düzeltmesi
        # uygulanmış `display_title` kullanılır.
        display_title = apply_prophet_honorifics(title)
        wiki_url = (
            primary_page.get("content_urls", {}).get("desktop", {}).get("page")
            or f"https://tr.wikipedia.org/wiki/{quote(title)}"
        )
        event_text = entry.get("text", "").strip()
        event_text = apply_prophet_honorifics(event_text)
        entry_year = entry.get("year")

        if not title:
            continue

        # KAPSAM FİLTRESİ: yalnızca "Mucitler, İcatlar, Keşifler" konulu
        # olaylar işlenir. Ağ çağrısı yapılmadan (fetch_full_extract vb.)
        # ÖNCE uygulanır ki alakasız konular için gereksiz API isteği
        # atılmasın.
        if not is_technology_topic(event_text, title, entry.get("_category", "")):
            continue

        h = content_hash(date_slug, title, entry.get("_category", ""))
        if h in history:
            continue  # daha önce yayınlanmış / mükerrer

        try:
            raw_extract = fetch_full_extract(title)
            cleaned = clean_extract(raw_extract)
            cleaned = apply_prophet_honorifics(cleaned)
            if len(cleaned) < 80 and not event_text:
                continue

            content_html = build_article_content(title, event_text, cleaned, entry_year)
            images = fetch_page_images(title)
            video_url = fetch_page_video(title)
        except requests.exceptions.RequestException as exc:
            # Wikipedia/Wikimedia API'lerinden kalıcı bir hata (ör. tüm
            # yeniden denemeler tükendi) alınırsa, tüm build'i düşürmek
            # yerine sadece bu tek olayı atlıyoruz.
            print(f"  [atlandı] '{title}' işlenemedi (ağ/HTTP hatası): {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - kasıtlı geniş yakalama
            # Ağ hatası dışında beklenmeyen HERHANGİ bir hata (ör. bozuk/
            # beklenmedik API yanıt biçimi, ayrıştırma hatası) tüm günün
            # build'ini düşürmesin diye burada da yakalanır; sadece bu tek
            # olay atlanır, script'in geri kalanı sorunsuz devam eder.
            print(f"  [atlandı] '{title}' işlenirken beklenmeyen hata: {exc}")
            continue
        finally:
            # Wikipedia API'lerini yormamak için olaylar arasına küçük
            # bir gecikme koyuyoruz.
            time.sleep(REQUEST_DELAY_SECONDS)

        seo_title = generate_seo_title(event_text, display_title)

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
        meta_description = truncate_meta_description(meta_source)

        item = {
            "slug": slugify(f"{title}-{date_slug}-{entry.get('_category')}"),
            "title": display_title,
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

        history[h] = {
            "title": display_title,
            "date": date_slug,
            "published": published_iso,
            # Aşağıdaki alanlar, script AYNI GÜN İÇİNDE birden fazla kez
            # çalıştırıldığında (ör. hem zamanlanmış cron hem manuel
            # "workflow_dispatch" tetiklenirse) RSS'in önceki çalıştırmada
            # üretilmiş öğeleri KAYBETMEDEN yeniden derlenebilmesi için
            # tutulur (bkz. main() sonundaki RSS birleştirme adımı).
            "seo_title": seo_title,
            "meta_description": meta_description,
            "slug": item["slug"],
            "images": images,
            "video_url": video_url,
            "wiki_url": wiki_url,
            "pub_date_rfc822": pub_date_rfc822,
        }

    print(f"{len(items)} yeni içerik üretildi (hedef: {MIN_ITEMS_TARGET}+ ya da mevcut tüm olaylar).")

    # RSS'i sadece bu çalıştırmada YENİ işlenen öğelerden değil, bugüne
    # (date_slug) ait TÜM geçmiş kayıtlardan oluştur. Bu, script aynı gün
    # içinde birden fazla kez çalıştırıldığında (ör. hem zamanlanmış cron
    # hem manuel tetikleme) önceki çalıştırmada üretilmiş öğelerin RSS'ten
    # düşüp kanalın boş kalmasını engeller — çünkü o öğeler bu çalıştırmada
    # "zaten yayınlanmış" (history hit) sayılıp işlenmeden atlanmış olabilir.
    already_in_items = {i["hash"] for i in items}
    recovered = 0
    for h, record in history.items():
        if record.get("date") != date_slug or h in already_in_items:
            continue
        required_fields = ("seo_title", "meta_description", "slug", "wiki_url", "pub_date_rfc822")
        if not all(record.get(f) for f in required_fields):
            continue  # eski/eksik formatlı bir history kaydı; RSS'e ekleme

        article_path = os.path.join(ARTICLES_DIR, f"{record['slug']}.html")
        content_html = ""
        if os.path.exists(article_path):
            with open(article_path, "r", encoding="utf-8") as f:
                article_source = f.read()
            body_match = re.search(
                r'<div itemprop="articleBody">\s*(.*?)\s*</div>', article_source, re.S
            )
            if body_match:
                content_html = body_match.group(1)
        if not content_html:
            continue  # makale dosyası bulunamadı/okunamadı; RSS'e ekleme

        items.append({
            "slug": record["slug"],
            "title": record.get("title", ""),
            "seo_title": record["seo_title"],
            "meta_description": record["meta_description"],
            "content_html": content_html,
            "images": record.get("images", []),
            "video_url": record.get("video_url", ""),
            "wiki_url": record["wiki_url"],
            "pub_date_rfc822": record["pub_date_rfc822"],
            "hash": h,
        })
        recovered += 1

    if recovered:
        print(f"{recovered} öğe, bugüne ait önceki bir çalıştırmadan RSS'e geri kazanıldı.")

    rss_xml = build_rss(items, published_iso)
    with open(os.path.join(OUTPUT_DIR, "rss.xml"), "w", encoding="utf-8") as f:
        f.write(rss_xml)

    save_history(history)
    print("Tamamlandı.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        # main() içinde bilinçli olarak (ör. On This Day API'si kalıcı
        # hata verdiğinde) tetiklenen çıkışlara dokunma; olduğu gibi
        # yukarı geçsin (iş akışı bunu bir "başarısız" adım olarak
        # görebilsin).
        raise
    except Exception:
        # Buraya kadar sızabilen HERHANGİ bir beklenmeyen hata, çıplak
        # bir traceback ile karışık/yarım bir çıkış durumuna yol açmasın
        # diye burada da yakalanır; tam traceback yine de loglanır (CI
        # loglarında teşhis için) ve script açık bir hata koduyla sonlanır.
        import traceback
        traceback.print_exc()
        print("[HATA] Beklenmeyen bir hata nedeniyle script sonlandırıldı.")
        raise SystemExit(1)
