# Başlarken

Bu rehber sizi sıfırdan, kendi makinenizde kaynak gösterilmiş bir cevaba
ulaştırır; ardından üretim yolunu gösterir.

- [1. Yerel deneyin (API anahtarı gerekmez)](#1-yerel-deneyin-api-anahtarı-gerekmez)
- [2. Gerçek cevaplar: gömülü Ollama ya da kendi LLM'iniz](#2-gerçek-cevaplar-gömülü-ollama-ya-da-kendi-llminiz)
- [3. Uygulamada ilk adımlar](#3-uygulamada-ilk-adımlar)
- [4. Widget'ı gömün](#4-widgetı-gömün)
- [5. Bir sunucuya kurulum](#5-bir-sunucuya-kurulum)
- [Sorun giderme](#sorun-giderme)

## Gereksinimler

- Compose eklentisiyle birlikte Docker Engine 24+ (`docker compose version`).
- Varsayılan yığın için boşta 8 GB RAM (yerel embedding modelleri tek başına
  yaklaşık 3–4 GB kullanır). Bkz. [donanım tablosu](../README.md#hardware).
- Görüntüler ve modeller için yaklaşık 15 GB disk, ayrıca belgeleriniz için alan.
- Kaynak iş akışı için `make` ve `git`.

## 1. Yerel deneyin (API anahtarı gerekmez)

`make up-demo`, `core` yığınını artı deterministik, OpenAI-uyumlu bir sahte
LLM başlatır. Sahte LLM, getirilen kaynaklardan `[n]` atıflarıyla cevap verir
ve tüm LLM tüketicilerini ona yönlendirir (`deploy/compose/docker-compose.demo.yml`).
Geri kalan her şey (arama, belge işleme, embedding, yönetim konsolu) gerçektir.

```bash
git clone https://github.com/alicangunduz/raggie && cd raggie
make env       # .env.example'den .env oluşturur (geliştirme varsayılanları)
make up-demo   # core yığını + sahte LLM, API anahtarı gerekmez
```

İlk derleme servisleri derler ve embedding modellerini `embeddings` imajına
gömer; bu yüzden biraz zaman alır. İlerlemeyi `make logs` ile izleyin; UI
açıldığında web konteyneri healthy olarak raporlar.

Her şey gömülü **Caddy** ön kapısı üzerinden 80 numaralı portta sunulur.
80 portu başka bir şey kullanıyorsa `.env` içinde `HTTP_PORT=8081` ayarlayın
(örnek) ve aşağıda `http://localhost:8081` kullanın. Web uygulamasının kendi
`3000` portu yalnızca `127.0.0.1` üzerinde yayımlanır (hata ayıklama için).

Ardından:

1. <http://localhost/onboarding> adresini açın.
2. İlk yöneticiyi oluşturun (şifreler en az 12 karakter olmalıdır).
   Geliştirme modunda kurulum token'ı gerekmez.
3. Sihirbaz ilk çalışma alanınızı oluşturur ve belge yüklemeyi önerir.

İsteğe bağlı: `make demo`, sentetik bir şirketi ("Nebula Teknoloji")
10 departman, 25 kullanıcı ve `examples/sample-corpus/` içindeki 39 örnek
belgeyle doldurur. Demo kullanıcılarının şifreleri bir kez yazdırılır.

> `make env`, `RAGGIE_ENV=development` ayarlayan `.env.example` dosyasını
> kopyalar. Bu, üretim secret kontrollerini devre dışı bırakır ve yalnızca
> yerel değerlendirme içindir. Geliştirme yığınını asla bir ağa açmayın.

## 2. Gerçek cevaplar: gömülü Ollama ya da kendi LLM'iniz

**Yine API anahtarı yok:** Ollama'yı yığının içinde çalıştırın.

```bash
make up-ollama       # core yığını + qwen3:4b-instruct ile Ollama (CPU)
make up-ollama-gpu   # aynısı, NVIDIA GPU geçişiyle
```

Varsayılan model `qwen3:4b-instruct` (Apache-2.0, yaklaşık 2,5 GB) Türkçe ve
İngilizce işler ve yalnızca CPU'lu bir makinede kullanılabilir hızda cevap
verir. İlk başlatma modeli indirir, bu yüzden birkaç dakika sürer; hiçbir veri
makineden çıkmaz. `OLLAMA_MODEL` ile başka bir model seçin (örneğin GPU'da
`qwen3:8b`).

**Kendi endpoint'iniz:** Raggie, herhangi bir OpenAI-uyumlu `/v1` sohbet
endpoint'iyle konuşur. `.env` dosyasını düzenleyin:

```dotenv
LLMAAS_BASE_URL=https://api.openai.com/v1      # veya kendi vLLM / Ollama / gateway URL'niz
LLMAAS_API_KEY=sk-...
LLMAAS_CHAT_MODEL=gpt-4o-mini                  # endpoint'inizin beklediği model kimliği
```

`make up-core` ile uygulayın: demo veya Ollama katmanı olmadan servisleri
yeniden oluşturur, böylece LLM ayarlarını `.env`'den yeniden okurlar. Ollama,
vLLM ve çok modelli yönlendirme için bkz. [llm-providers.md](llm-providers.md).

## 3. Uygulamada ilk adımlar

- **Belge yükleyin:** *Yönetim → Bilgi bankası*. PDF, DOCX, XLSX, PPTX, TXT ve
  MD ayrıştırılır; taranmış PDF'ler ve görseller OCR'dan geçer (Türkçe +
  İngilizce). Durum *Beklemede* → *İşleniyor* → *Hazır* şeklinde ilerler;
  başarısız belgeler nedeni gösterir ve otomatik olarak yeniden denenir.
- **Soru sorun:** *Sohbet*'i açın, çalışma alanını seçin, sorun. Cevaplar
  numaralı alıntılarla akar; kullanılan pasajları görmek için *Kaynaklar*'ı
  açın.
- **İnsanları davet edin:** *Yönetim → Ekip*, davet bağlantıları oluşturur
  (e-posta sunucusu gerekmez). Roller: *Personel* (sohbet), *Çalışma alanı
  yöneticisi* (üyesi oldukları çalışma alanlarını yönetir), *Süper yönetici*
  (organizasyonu yönetir).
- **Kaliteyi gözden geçirin:** *Yönetim → İşaretlenenler*, sadakat veya alıntı
  puanları eşiklerin altında kalan cevapları listeler.
- **Dili değiştirin:** arayüz Türkçe ve İngilizce olarak kullanılabilir (hesap
  menüsü). Cevaplar sorunun dilini takip eder.

## 4. Widget'ı gömün

1. **Harici** tipte bir çalışma alanı oluşturun (sihirbaz "Müşteri Destek
   Widget'ı" önerir veya *Yönetim → Çalışma alanları* kullanın).
2. Widget'ı gömecek web sitesi origin'lerine izin verin: *Yönetim → Çalışma
   alanları*'nda Harici çalışma alanının satırındaki **Widget siteleri**
   düğmesine tıklayın, her origin'i ekleyin ve **Kaydet**'e basın. Origin'ler
   tam `scheme://host[:port]` değerleridir (`https://www.example.com` ve
   `https://example.com` farklı origin'lerdir); yollar ve joker karakterler
   reddedilir. En az bir origin kaydedilene kadar widget hiçbir yerde
   yüklenmez.
3. Gömme kod parçasını `</body>` etiketinden önce yapıştırın (sihirbaz bunu
   Harici bir çalışma alanı için gösterir; şu şekildedir):

   ```html
   <script src="https://chat.example.com/widgets/embed.js?id=WORKSPACE_ID"></script>
   ```

   Script origin'i sizin Caddy ön kapınızdır: yerelde `http://localhost` veya
   sunucuda `https://<RAGGIE_DOMAIN>` (sihirbazın doğru kod parçasını basması
   için `WIDGET_EMBED_BASE_URL` değerini aynı değere ayarlayın).

Widget bir Shadow DOM içinde çizilir, yalnızca ön kapının widget yollarıyla
konuşur ve yalnızca kendisi için düzenlenmiş çalışma alanına erişebilir.
Ayrıntılar için bkz. [security-model.md](security-model.md#3-güven-sınırları-stride).

## 5. Bir sunucuya kurulum

Docker'lı bir Linux sunucusu için kurulum betiği, sabitlenmiş ve sha256 ile
doğrulanmış bir sürümü `/opt/raggie` altına indirir, güçlü secret'lar üretir,
`RAGGIE_ENV=production` ayarlar, yığını başlatır (yayımlanmış görüntüler,
tek genel giriş noktası olarak Caddy) ve tek kullanımlık kurulum token'ını
basar:

```bash
curl -fsSL https://github.com/alicangunduz/raggie/releases/latest/download/install.sh | sudo bash -s -- \
  --domain chat.example.com --acme-email ops@example.com \
  --llm ollama
```

Kurulum betiğini çalıştırmadan önce doğrulamak için, sürümün checksum
dosyasıyla birlikte indirin:

```bash
curl -fsSLO https://github.com/alicangunduz/raggie/releases/latest/download/install.sh
curl -fsSLO https://github.com/alicangunduz/raggie/releases/latest/download/SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS
sudo bash install.sh --domain chat.example.com
```

Ana seçenekler (hepsi: `bash install.sh --help`):

| Seçenek | Anlamı |
|---|---|
| `--llm mock\|ollama\|self-hosted\|openai` | `mock` = çevrimdışı demo; `ollama` = gömülü Ollama; `self-hosted` = sizin vLLM/Ollama URL'niz; `openai` = OpenAI-uyumlu bir sağlayıcı (`--llm-base-url`, `--llm-api-key`, `--llm-model`). Etkileşimli sorulduğunda, en az 12 GB RAM'li sunucularda öneri `ollama`, aksi halde `mock`; `--yes` ile varsayılan `mock`'tur. |
| `--ollama-model ID`, `--gpu auto\|yes\|no` | Gömülü Ollama için model (varsayılan `qwen3:4b-instruct`) ve NVIDIA GPU kullanımı (`auto` = `nvidia-smi` çalıştığında). |
| `--embeddings local\|cloud` | Belge metninin nerede embed edileceği; bulut modu `--yes` ile `--accept-data-transfer` de gerektirir. |
| `--domain HOST`, `--acme-email ADDR` | Otomatik Let's Encrypt sertifikasıyla HTTPS; DNS sunucuyu göstermeli ve 80/443 portları erişilebilir olmalı. |
| `--http-port N`, `--https-port N` | Ön kapı portları (varsayılan 80 ve 443). |
| `--public-url URL` | Genel origin; Caddy'nin önünde başka bir proxy veya CDN olduğunda. |
| `--yes` | Asla sorma: flag'ler, `RAGGIE_*` ortam değişkenleri, sonra varsayılanlar. |

Her flag bir ortam değişkeni olarak da verilebilir (`--llm-api-key` →
`RAGGIE_LLM_API_KEY`); bu, anahtarları kabuk geçmişinizin dışında tutar.
Kurulum betiğini yeniden çalıştırmak kurulumu yükseltir: `.env` ve
`.env.embeddings` yedeklenir ve yalnızca yeni anahtarlar kazanır, asla
üzerine yazılmaz. `--ref main`, bir daldan checksum ile **doğrulanmayan** bir
geliştirme anlık görüntüsü kurar; üretimde kullanmayın.

Bir depo kopyasında `make setup ARGS="--yes --llm mock"` aynı sihirbazı
yerinde çalıştırır.

Canlıya geçmeden önce [production.md](production.md) dosyasını okuyun ve
güvenlik duvarını kontrol edin.

## Sorun giderme

| Belirti | Kontrol |
|---|---|
| `onboarding` kurulum token'ı ister | Üretim modu. Kurulum betiğinin bastığı token'ı kullanın veya `docker compose logs control-plane` içinde bulun (ilk açılışta bir kez basılır). |
| Bir servis başladıktan hemen sonra "refusing to start with insecure secrets" ile çıkar | `RAGGIE_ENV` production ve bir secret varsayılan, yer tutucu veya 32 bayttan kısa. `openssl rand -hex 32` ile bir tane üretin. |
| Belgeler *Beklemede* kalır | `docker compose logs ingestion-worker embeddings`. Embeddings servisi, işleme başlamadan önce healthy olmalıdır. `STALE_PENDING_SECONDS` (15 dk) boyunca takılan belgeler otomatik olarak yeniden kuyruğa alınır. `ModelNotAvailable` ile çıkan bir embeddings konteyneri, yerel modda çalışan `-slim` imajıdır: `-local` kullanın. |
| Cevaplar ilgili kaynak bulunamadı der | Belge hâlâ işleniyor olabilir veya sohbette seçilenden farklı bir çalışma alanındadır. |
| Widget "unavailable" gösterir | Sayfanın origin'i çalışma alanı izin listesinde değil veya çalışma alanı Harici tipte değil. |
| Giriş geçici olarak engellenir | Beş başarısız deneme hesabı artan bir geri çekilme ile kilitler (30 saniyeden 15 dakikaya kadar). |
