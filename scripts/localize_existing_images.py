#!/usr/bin/env python3
"""
Daha önce çekilmiş makalelerin (article.json) görsellerini GSMArena'dan
İNDİRİP data/images/<slug>/ altına kaydeder, article.json'daki görsel
URL'lerini yayınlanan (GitHub Pages) adresle değiştirir ve ardından
item.xml + docs/rss.xml'i bu yeni adreslerle yeniden üretir.

regenerate_items.py'den FARKI: bu script ağ isteği atar (görselleri gerçekten
indirir), regenerate_items.py atmaz. Bu yüzden bunu internet erişimi olan bir
ortamda (kendi bilgisayarın veya bir GitHub Actions çalıştırması) çalıştırman
gerekir.

Ne zaman kullanılır: article_to_html() artık görsel URL'lerini olduğu gibi
GSMArena'dan hotlink'lemek yerine yerel/yayınlanan bir adresle değiştirdiğinde
(bu güncellemede eklendi) — daha önce zaten "ok" olarak işaretlenmiş ve
item.xml'i önbelleğe alınmış makaleler normalde bir daha yeniden işlenmez.
Bu script o eski kayıtları, YENİDEN ÇEKMEDEN (haber sayfasını tekrar
indirmeden, sadece görselleri indirerek) tek seferlik günceller.

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
    load_registry,
    localize_images,
    write_rss,
)


def main() -> int:
    if not OUTPUT_DIR.exists():
        print(f"[hata] {OUTPUT_DIR} bulunamadı.")
        return 1

    updated = 0
    failed = 0
    for json_path in sorted(OUTPUT_DIR.glob("*/article.json")):
        slug = json_path.parent.name
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            article = Article(**payload)
            # Görseli olmayan veya zaten yerelleştirilmiş (PAGES_BASE_URL ile
            # başlayan) makaleleri atla — gereksiz ağ isteği yapılmasın.
            already_local = article.images and all(
                img.get("url", "").startswith("/") or "github.io" in img.get("url", "")
                for img in article.images
            )
            if not article.images or already_local:
                continue
            print(f"  [işleniyor] {slug} ({len(article.images)} görsel)")
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

    print(f"[tamam] {updated} makalenin görselleri yerelleştirildi, {failed} başarısız.")

    registry = load_registry()
    write_rss(registry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
