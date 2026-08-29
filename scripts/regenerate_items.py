#!/usr/bin/env python3
"""
Var olan data/articles/*/article.json dosyalarını YENİDEN ÇEKMEDEN (ağ
isteği atmadan), generate.py içindeki GÜNCEL build_item_xml/article_to_html
şablonuyla item.xml önbelleklerini ve docs/rss.xml'i yeniden üretir.

Ne zaman kullanılır: article_to_html() içindeki HTML şablonu değiştiğinde
(ör. bu düzeltmede video/görsel etiketlerine eklenen display:block stilleri
gibi) — daha önce zaten başarıyla işlenmiş ve item.xml'i önbelleğe alınmış
makaleler normalde bir daha ASLA yeniden üretilmez (bkz. generate.py'deki
önbellekleme mantığı). Bu script, o önbelleği tek seferlik olarak, mevcut
article.json verisinden (yeniden indirme/çeviri yapmadan) tazeler.

Kullanım:
    python scripts/regenerate_items.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate import (  # noqa: E402
    Article,
    OUTPUT_DIR,
    build_item_xml,
    load_registry,
    write_rss,
)


def main() -> int:
    if not OUTPUT_DIR.exists():
        print(f"[hata] {OUTPUT_DIR} bulunamadı.")
        return 1

    regenerated = 0
    failed = 0
    for json_path in sorted(OUTPUT_DIR.glob("*/article.json")):
        slug = json_path.parent.name
        try:
            import json

            payload = json.loads(json_path.read_text(encoding="utf-8"))
            article = Article(**payload)
            xml = build_item_xml(article)
            (json_path.parent / "item.xml").write_text(xml, encoding="utf-8")
            regenerated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [uyarı] {slug} yeniden üretilemedi: {exc}")

    print(f"[tamam] {regenerated} item.xml yeniden üretildi, {failed} başarısız.")

    registry = load_registry()
    write_rss(registry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
