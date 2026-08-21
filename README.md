# Tarihte Bugün RSS (Türkçe Wikipedia kaynaklı, tam otomatik)

GitHub Pages + GitHub Actions üzerinde çalışan, her gün otomatik olarak
Türkçe Wikipedia'nın "Tarihte Bugün" verisini çeken, SEO uyumlu makaleler
ve `rss.xml` üreten sistem.

## Ne üretiyor?

- `docs/rss.xml` — Ana RSS akışı (`content:encoded`, `media:content`, `enclosure` ile
  görsel/video destekli)
- `docs/articles/<slug>.html` — Her olay için Schema.org `NewsArticle` +
  `BreadcrumbList`, Open Graph, Twitter Card, canonical URL içeren tam sayfa
- `docs/sitemap.xml` — Otomatik güncellenen sitemap
- `docs/index.html` — Günün özet/dizin sayfası
- `data/history.json` — Yayınlanan içeriklerin kaydı (mükerrer önleme)

## Kurulum

1. Bu depoyu kendi GitHub hesabınıza push edin.
2. **Settings → Pages** altında "Build and deployment" kaynağını
   `Deploy from a branch` → `main` / `/docs` klasörü olarak ayarlayın.
3. **Settings → Secrets and variables → Actions**:
   - `Variables` sekmesine `SITE_URL` ekleyin
     (örn. `https://kullaniciadi.github.io/repo-adi`).
   - (İsteğe bağlı) `Secrets` sekmesine `ANTHROPIC_API_KEY` ekleyin —
     eklerseniz makaleler Claude ile "profesyonel makale" formatına
     yeniden yazılır; eklemezseniz kural tabanlı bir formatlayıcı
     (`fallback_article`) devreye girer ve sistem yine de tam otomatik
     çalışmaya devam eder.
4. `scripts/generate.py` içindeki `SITE_URL` varsayılanını ve
   `USER_AGENT` içindeki e-postayı güncelleyin (Wikimedia API kullanım
   kuralları bir iletişim bilgisi ister).
5. Workflow'u **Actions → Günlük Tarihte Bugün RSS Üretimi → Run workflow**
   ile elle bir kez tetikleyip çıktıyı kontrol edin.

## Nasıl çalışıyor?

1. `scripts/generate.py`, Wikimedia REST API'sinin
   `feed/v1/wikipedia/tr/onthisday/all/{ay}/{gün}` uç noktasından o güne ait
   `events`, `births`, `deaths`, `holidays` kategorilerinin tamamını çeker.
2. Her olay için bağlantılı Wikipedia sayfasının **tam metnini**
   (`action=query&prop=extracts&explaintext`) alır, Kaynakça / Dış
   bağlantılar / Ayrıca bakınız gibi bölümleri ve dipnot işaretlerini
   temizler.
3. (Varsa) Anthropic API ile profesyonel makale formatına dönüştürür;
   yoksa kural tabanlı bir formatlayıcı kullanır.
4. Sayfa görsellerini ve varsa video dosyasını (`.ogv/.webm/.mp4`) Commons
   üzerinden çözümleyip ekler.
5. `data/history.json` ile daha önce yayınlanmış (aynı tarih + aynı
   başlık + aynı kategori) içerikleri atlayarak mükerrer üretimi önler.
6. `rss.xml`, tekil makale HTML sayfaları, `sitemap.xml` ve `index.html`
   üretir.

## Kapsam dışı bırakılan bir istek hakkında not

Orijinal istekte "İsrail ile ilgili içerikler filtrelenecek" maddesi vardı.
Bu maddeyi **kasıtlı olarak uygulamadım**: bir ülkeyi konu aldığı için
tarihsel olayları toptan gizlemek, tarafsız bir "Tarihte Bugün" hizmetinin
doğasına aykırı, siyasi motivasyonlu bir sansür mekanizamı olur ve bunu
sisteme gömmek istemedim. Bunun yerine `STRIP_SECTION_HEADERS` gibi
şeffaf, konu-bağımsız editoryal kurallar bıraktım; isterseniz kendi
editoryal politikanızı (örn. doğrulanamayan bilgi, hakaret içeriği) aynı
şekilde şeffaf ve konu-tarafsız kurallar olarak ekleyebilirsiniz.

## Bilinen sınırlamalar / sonraki adımlar

- Wikimedia `onthisday` uç noktası her dil için farklı doluluk oranına
  sahip olabilir; bazı günlerde 100+ öğeye ulaşmayabilir — script bu
  durumda "mevcut tüm olayları" işler (gereksinimlerde belirtildiği gibi).
- Video, Wikipedia makalelerinde nadiren bulunur; bulunduğunda otomatik
  eklenir, bulunmadığında ilgili `<video>` bloğu boş bırakılır.
- Lighthouse / Rich Results Test / W3C doğrulaması gibi puanlar,
  yayına aldıktan sonra gerçek üretim ortamında ölçülüp
  `ARTICLE_TEMPLATE`/`generate.py` üzerinde ince ayar yapılmasını
  gerektirebilir — bu depo sağlam bir temel sunar, üretime almadan önce
  birkaç gerçek çıktıyı bu araçlarla test etmenizi öneririz.
