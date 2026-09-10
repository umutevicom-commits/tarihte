# Google, daha hızlı ve daha güvenilir bağlantılar için ADB Wi - Fi 2.0 'ı tanıttı

Bir mobil uygulama geliştiricisiyseniz, ADB'yi (Android Hata Ayıklama Köprüsü) kesinlikle biliyorsunuzdur. Bununla birlikte, normal kullanıcılar da bunun için kullanımlar bulmuşlardır – bazıları normal kullanıcı arayüzünün izin vermediği şeyler için kullanır (örneğin, belirli uygulamaları devre dışı bırakma, scrcpy çalıştırma vb.), diğerleri büyük veri aktarımları için kullanmıştır (MTP üzerinden yapmak yerine).

ADB için kullanım durumunuz ne olursa olsun, Google'ın Android Debug Bridge Wi - Fi 2.0 'ı kullanıma sunduğunu duymaktan memnun olacaksınız. Bu yeni sürüm, daha hızlı ve daha güvenilir hale getirmek için sistemin üç temel bileşenini yeniden çalıştırıyor.

Birincisi ADB sunucusudur. Eski sürümde, ağ yapılandırması değiştiğinde veya cihaz kapatıldığında ADB bağlantısı kesilirdi. Google, bağlantıları daha güvenilir hale getirmek için eski mDNS yığınını değiştirdi.

İkincisi ADB Daemon'dur. Eski mDNS zaman zaman bilgisayarınız ve Android cihaz arasındaki bağlantıları keserdi. Yeni olan, bilgisayarınız ve cihazınız güvenilmeyen bir ağ üzerinden bağlandığında bağlantıları otomatik olarak devre dışı bırakır ve ardından tekrar güvenilir bir ağa bağlandığınızda bunları yeniden etkinleştirir.

Son olarak, Android Studio daha iyi keşfedilebilirlik sunar. Cihazda kablosuz hata ayıklamayı etkinleştirdikten sonra Android Studio'nun Cihaz Yöneticisi'nde görünmelidir.

Tüm bu değişikliklerle Google, ADB Wi - Fi 2.0 'ın % 66 daha yüksek bağlantı hızları (bağlantıların % 90' ı için) sunduğunu ve otomatik bağlantı oranlarının % 32 daha yüksek olduğunu bildiriyor.

ADB Wi - Fi 2.0 telefonlar, tabletler, wearOS saatler ve Android TV'lerle kullanılabilir. Nasıl başlayacağınızla ilgili ayrıntılar için Kaynak bağlantısını takip edin.

---

## Görseller

![Google introduces ADB Wi-Fi 2.0 for faster and more reliable connections](https://umutevicom-commits.github.io/tarihte/data/images/google_introduces_adb_wifi_20_for_faster_and_more_reliable_connections-news-74554/immaculate_001.jpg)

---

## Videolar

🎬 https://fdn.gsmarena.com/imgroot/news/26/09/google-adb-wi-fi-2/gsmarena_001.mp4
