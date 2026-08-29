#!/usr/bin/env python3
"""
Daha önce çekilmiş makalelerin (article.json) görsellerini GSMArena'dan
İNDİRİP data/images/<slug>/ altına kaydeder, article.json'daki görsel
URL'lerini yayınlanan (GitHub Pages) adresle değiştirir ve ardından
item.xml + docs/rss.xml'i bu yeni adreslerle yeniden üretir.

ONARIM MANTIĞI: Bir makalenin görselleri daha önce (ör. bozuk bir
PAGES_BASE_URL ile) yanlış/eksik bir yerel adresle işaretlenmiş olabilir —
bu durumda orijinal GSMArena kaynak adresi article.json'da artık mevcut
DEĞİLDİR ve tekrar indirilemez. Bu script, image_is_fully_localized() ile
her makaleyi kontrol eder; eksik/bozuk bulduğu makalelerin haber sayfasını
(article.url) AĞDAN YENİDEN ÇEKİP yalnızca görsel URL'lerini tazeler (gövde
metni, çeviri, başlık gibi diğer hiçbir alana dokunmaz) ve ardından bu taze
adresleri indirir. Zaten tam ve doğru şekilde yerelleştirilmiş makaleler
atlanır, bu yüzden script'i tekrar tekrar çalıştırmak güvenlidir.

Bu script ağ isteği atar (hem onarım için haber sayfasını hem de görselleri
indirir). İnternet erişimi olan bir ortamda (GitHub Actions) çalıştırılmalıdır.

Kullanım:
    python scripts/localize_existing_images.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate import (  # noqa: E402
    Article,
    OUTPUT_DIR,
    build_item_xml,
    fetch,
    image_is_fully_localized,
    load_registry,
    localize_images,
    parse_article,
    write_rss,
)

import requests  # noqa: E402


def main() -> int:
    if not OUTPUT_DIR.exists():
        print(f"[hata] {OUTPUT_DIR} bulunamadı.")
        return 1

    updated = 0
    skipped = 0
    failed = 0
    for json_path in sorted(OUTPUT_DIR.glob("*/article.json")):
        slug = json_path.parent.name
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            article = Article(**payload)
            if not article.images:
                skipped += 1
                continue
            if image_is_fully_localized(article):
                skipped += 1
                continue

            print(f"  [işleniyor] {slug} ({len(article.images)} görsel)")

            # Görsellerden en az biri eksik/bozuk: article.json'daki kayıtlı
            # URL artık orijinal GSMArena kaynağı OLMAYABİLİR (önceki hatalı
            # bir çalıştırmada üzerine yazılmış olabilir). Güvenli tek yol:
            # haber sayfasını yeniden çekip TAZE orijinal görsel URL'lerini
            # almak. Gövde metni/çeviri/başlık gibi diğer alanlara dokunulmaz.
            try:
                html = fetch(article.url)
                fresh = parse_article(article.url, html)
                article.images = fresh.images
            except requests.RequestException as exc:
                print(f"    [onarım-hata] haber sayfası tekrar çekilemedi: {exc}")
                failed += 1
                continue

            article = localize_images(article)
            json_path.write_text(
                json.dumps(asdict(article), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (json_path.parent / "item.xml").write_text(
                build_item_xml(article), encoding="utf-8"
            )
            updated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [uyarı] {slug} güncellenemedi: {exc}")

    print(
        f"[tamam] {updated} makale onarıldı/yerelleştirildi, "
        f"{skipped} zaten tamamdı, {failed} başarısız."
    )

    registry = load_registry()
    write_rss(registry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
