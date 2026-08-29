#!/usr/bin/env python3
"""
GSMArena makale scraper'ı.

Tüm makale sayfalarını sırayla gezer, her makalenin TAM içeriğini
(başlık, tarih, yazar, gövde metni, görsel URL'leri, etiketler) çeker
ve hem JSON hem de Markdown formatında kaydeder.
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

BASE_URL = "https://www.gsmarena.com"
NEWS_INDEX_URL = f"{BASE_URL}/news.php3"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "articles"
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

ARTICLE_LINK_RE = re.compile(r"^[a-z0-9_]+-news-\d+\.php$")


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
    fetched_at: str = ""


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
    title = h1.get_text(strip=True) if h1 else slug.replace("_", " ").title()

    # Tarih — .dtreviewed veya .float-left içinde
    date_str = ""
    date_el = soup.find("span", class_="dtreviewed") or soup.find(
        "div", class_="dtreviewed"
    )
    if date_el:
        date_str = date_el.get_text(strip=True)
    else:
        # Fallback: article-tags içindeki tarih
        tags_div = soup.find("div", class_="article-tags")
        if tags_div:
            text = tags_div.get_text(" ", strip=True)
            date_match = re.search(r"(\d{1,2}\s+\w+\s+\d{4})", text)
            if date_match:
                date_str = date_match.group(1)

    # Yazar — meta tag veya article-tags içindeki ilk link
    author = ""
    author_meta = soup.find("meta", attrs={"name": "author"})
    if author_meta:
        author = author_meta.get("content", "")
    if not author:
        tags_div = soup.find("div", class_="article-tags")
        if tags_div:
            first_link = tags_div.find("a")
            if first_link:
                author = first_link.get_text(strip=True)

    # Gövde — .review-body içindeki tüm paragraflar
    body_paragraphs: list[str] = []
    content = soup.find("div", class_="review-body")
    if content:
        for p in content.find_all("p"):
            text = p.get_text(strip=True)
            if text:
                body_paragraphs.append(text)
        # Liste öğeleri
        for li in content.find_all("li"):
            text = li.get_text(strip=True)
            if text and text not in body_paragraphs:
                body_paragraphs.append(text)

    # Görseller — .review-body içindeki img etiketleri
    images: list[dict] = []
    if content:
        for img in content.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if src:
                images.append(
                    {
                        "url": fix_url(src, url),
                        "alt": img.get("alt", ""),
                    }
                )

    # Etiketler — .article-tags içindeki linkler (ilk link yazar, atla)
    tags: list[str] = []
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
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def article_to_markdown(article: Article) -> str:
    lines: list[str] = [f"# {article.title}", ""]
    meta: list[str] = []
    if article.author:
        meta.append(f"**Yazar:** {article.author}")
    if article.date:
        meta.append(f"**Tarih:** {article.date}")
    if meta:
        lines.append(" | ".join(meta))
        lines.append("")
    lines.append(f"**Kaynak:** {article.url}")
    lines.append("")
    if article.tags:
        lines.append("**Etiketler:** " + ", ".join(article.tags))
        lines.append("")
    lines.append("---")
    lines.append("")
    for para in article.body_paragraphs:
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
            save_article(article)
            all_articles.append(article)
            print(
                f"  [tamam] {article.title} "
                f"({len(article.body_paragraphs)} paragraf, "
                f"{len(article.images)} görsel)"
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
        }
        for a in all_articles
    ]
    index_path.write_text(
        json.dumps(index_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[toplam] {len(all_articles)} makale çekildi.")
    print(f"[indeks] {index_path}")
    return all_articles


def main() -> int:
    print("GSMArena makale scraper'ı başlatılıyor...")
    print(f"Çıktı dizini: {OUTPUT_DIR}")
    articles = crawl()
    if not articles:
        print("[uyarı] Hiç makale çekilemedi.")
        return 1
    print(f"[başarılı] {len(articles)} makale tam içerikle kaydedildi.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
