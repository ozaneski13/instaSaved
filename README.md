# instaSaved

Instagram **Kaydedilenler** (Saved) listendeki her gönderiyi tek bir okunabilir listeye döker: gönderide ne
anlatılıyor / gösteriliyor (video, fotoğraf ve karuseller için LLM analizi), açıklama metni ve gönderi sahibinin
**sabitlediği yorumlar**. Tarayıcı açmaz, artımlı çalışır, tek kullanıcı için tasarlanmıştır (Windows 11 + NVIDIA GPU
üzerinde geliştirildi; CPU ile de çalışır).

> **English summary.** instaSaved turns your Instagram *Saved* posts into a Markdown/JSON digest: for every post it
> produces a Turkish content analysis (video transcript via local Whisper + sampled frames, or the photos themselves,
> sent to a vision-capable LLM), the caption, and the author's **pinned comments** (read from Instagram's mobile API,
> the only surface that exposes `is_pinned`). Incremental, resumable, no browser. LLM backend is pluggable
> (Claude Code CLI with a Claude subscription, Anthropic API, or any OpenAI-compatible endpoint incl. Ollama).

---

## İçindekiler

1. [Ne üretir](#ne-üretir)
2. [Nasıl çalışır](#nasıl-çalışır)
3. [Sabitli yorumlar nasıl bulunuyor](#sabitli-yorumlar-nasıl-bulunuyor)
4. [Kurulum](#kurulum)
5. [Hızlı başlangıç](#hızlı-başlangıç)
6. [Komutlar](#komutlar)
7. [Yapılandırma](#yapılandırma)
8. [LLM sağlayıcıları](#llm-sağlayıcıları)
9. [Çıktı formatı](#çıktı-formatı)
10. [Artımlılık ve durum takibi](#artımlılık-ve-durum-takibi)
11. [Güvenlik, gizlilik ve riskler](#güvenlik-gizlilik-ve-riskler)
12. [Sorun giderme](#sorun-giderme)
13. [Geliştirme](#geliştirme)
14. [Nasıl geliştirildi](#nasıl-geliştirildi)
15. [Lisans](#lisans)

---

## Ne üretir

`output/saved_posts.md` içinde her gönderi için bir madde:

```markdown
6. **@jeffrey_in_nyc** · [Postu aç](https://www.instagram.com/p/Da_S-LVze42/) · 2026-07-20
   - **İçerik:** Tokyo'daki bir su samuru kafesine ilk kez giden bir ziyaretçinin deneyimi gösteriliyor.
     Karelerde kafenin verdiği pembe koruyucu önlüğü giymiş ziyaretçinin kucağına çıkan bir su samuru ve
     oyun alanında dolaşan diğer samurlar görünüyor. Ziyaretçi hayvanların ne kadar sevimli olduğunu anlatıyor.
     _(video, dil: en, 51 kelime transkript, 2 görsel incelendi)_
   - **Açıklama:**
     > First Time Visiting an Otter Cafe in Tokyo Japan #japan #travel #tokyo #experience
   - **Sabitli yorumlar:**
     - @jeffrey_in_nyc: 📍Otters Family in Harajuku Tokyo
     - @jeffrey_in_nyc: Sorry guys I said cute like a million times but I couldn't help it😆
```

Aynı veri `output/saved_posts.json` içinde alan alan (url, author, caption, content_summary, transcript, language,
pinned_comments, collections, statuses…) durur; başka araçlara beslemek için.

Gönderi türüne göre analiz:

| Tür | Kaynak malzeme | Analiz |
|---|---|---|
| Konuşmalı video | Whisper transkripti + 2 kare + caption | Ne anlatıldığı, gösterilen yer/ürün/kişi, verilen tavsiye |
| Konuşmasız video (müzik) | 4 kare + caption | Görüntüden ne yapıldığı, ekrandaki yazılar |
| Fotoğraf | Görselin kendisi + caption | Ne gösterildiği, görseldeki yazılar |
| Karusel | En fazla 6 görsel (+ varsa video çocukları) + caption | Bütün karuselin özeti (ör. "30 Japonca kalıp listesi") |

---

## Nasıl çalışır

```
 ig-login (bir kez)                       run.cmd → 4  (her koşu)
 ┌──────────────────┐   ┌──────────────────────────────────────────────────────────────────────┐
 │ Instagram mobil  │   │ 1. sync    feed/saved/posts  ──► yeni gönderiler ──► SQLite (state.db)  │
 │ API oturumu      │──►│            media/{pk}/comments ──► sabitli yorumlar (is_pinned)        │
 │ (instagrapi)     │   │ 2. process CDN'den medya indir ──► faster-whisper (CUDA) transkript    │
 └──────────────────┘   │            PyAV ile kareler / Pillow ile görseller ──► LLM analizi     │
                        │ 3. report  output/saved_posts.md + .json                              │
                        └──────────────────────────────────────────────────────────────────────┘
```

1. **Kaynak (`ig_source.py`)** — Instagram'ın mobil API'si, `instagrapi` üzerinden ham JSON olarak:
   kaydedilenler (`feed/saved/posts/`), koleksiyonlar (`collections/list/`, `feed/collection/{id}/`),
   yorumlar (`media/{pk}/comments/`) ve süresi dolan medya URL'lerini tazelemek için `media/{pk}/info/`.
   Instagram'ın sınırlama/doğrulama sinyalleri (`PleaseWaitFewMinutes`, `ChallengeRequired`, `LoginRequired`…)
   tek bir **HardStop** istisnasına çevrilir: program yeniden denemez, durumu kaydeder ve çıkar.
2. **Ayrıştırma (`parsers.py`)** — saf fonksiyonlar; Instagram'a dokunmaz, birim testleriyle korunur. Feed öğesinden
   caption, en yüksek çözünürlüklü video/görsel URL'leri, karusel çocukları; yorum yanıtından sabitliler.
3. **Medya (`video.py`, `media.py`, `transcribe.py`)** — imzalı CDN URL'leri hemen indirilir (süreli); video
   `faster-whisper large-v3` ile (CUDA float16 → int8_float16 → CPU sırasıyla denenir) yazıya çevrilir; PyAV ile
   zaman eksenine eşit dağılmış kareler alınır, Pillow ile uzun kenar 800 px'e küçültülür. Dosyalar analizden sonra silinir.
4. **Analiz (`summarize.py`)** — tür, caption, transkript ve görseller tek bir istemle LLM'e gider; 2-4 cümle Türkçe.
   Sağlayıcı takılıp çıkarılabilir (aşağıda).
5. **Durum (`store.py`)** — SQLite; her adımdan sonra commit. Kesinti olursa kaldığı yerden devam eder, bitmiş gönderiyi
   yeniden çekmez. Ham API yanıtları `raw_payloads` tablosunda saklanır (şema değişirse yeniden ayrıştırmak için;
   gönderi başına yalnızca en yenisi tutulur).
6. **Rapor (`report.py`)** — Markdown + JSON.

---

## Sabitli yorumlar nasıl bulunuyor

Bu projenin en zor kısmıydı ve kesin sonuca canlı probelarla ulaşıldı (2026-09-02):

- Instagram'ın **web** tarafı (giriş yapılmış oturumda bile) sabitleme bilgisi vermiyor: ne `api/v1/media/{pk}/comments/`
  web yanıtı, ne gömülü GraphQL (`xdt_api__v1__media__media_id__comments__connection`), ne de sayfa DOM'u.
  Sabitli yorum olduğu bilinen gönderilerde bile alan yok.
- **Mobil API** yanıtı ise kökte `pinned_comment_count` ve sabitli yorum nesnesinde `is_pinned: true` taşıyor.
  Sabitli olmayan yorumda anahtar **hiç yok** — bu yüzden kod `bool(comment.get("is_pinned"))` okur, `comment["is_pinned"]`
  asla; ve `pinned_comment_count` ile çapraz kontrol eder.
- Tuzak alanlar bilinçli olarak yok sayılır: `hoisted_comments` (her zaman boş), `is_ranked_comment`, `comment_index`,
  `pinned_for_users`, `visual_comment_reply_sticker_info.is_pinned` (alakasız bir tamsayı), profil sabitlemesi `Post.is_pinned`.
- Sabitli yorumlar ilk sayfada geldiği için yalnızca ilk sayfa okunur; diğer yorumlar saklanmaz.

Resmî Instagram Graph API ne kaydedilenleri ne de sabitleme bilgisini verir; bu yüzden araç kullanıcının kendi
oturumuyla çalışır (bkz. Riskler).

---

## Kurulum

Gereksinimler: Python 3.10+, [uv](https://docs.astral.sh/uv/) (ya da pip), Instagram hesabı. GPU isteğe bağlı
(NVIDIA + CUDA 12 sürücüsü; RTX 5080'de doğrulandı). ffmpeg **gerekmez** (PyAV bundled FFmpeg kullanır).

```bat
git clone https://github.com/ozaneski13/instaSaved.git
cd instaSaved
uv venv --python 3.10 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
copy config.example.json config.json
```

`config.json` içinde en azından `username` ve `llm` bölümünü düzenle (aşağıda). Whisper modeli (~3 GB) ilk
kullanımda indirilir.

GPU yoksa: `"whisper": {"device": "cpu", "compute_type": "int8", "model": "medium"}` (yavaş ama çalışır).

**Windows + pip CUDA notu:** `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` paketlerinin DLL'leri PATH'te olmaz;
`transcribe.py` bunları otomatik olarak DLL arama yoluna ekler. Yine de `cublas64_12.dll not found` görürsen
sürücünü güncelle ya da CPU moduna geç.

---

## Hızlı başlangıç

`run.cmd` dosyasına çift tıkla; numaralı menü açılır (pencere iş bitince kapanmaz):

```
 1 - Instagram girisi (mobil API, bir kez)   <- ilk adim
 2 - Koleksiyonlari listele
 3 - Deneme kosusu, 3 post (run --limit 3)
 4 - Tam kosu (run)
 5 - Belirli koleksiyonda kosu
 6 - Tum postlari yeniden analiz et (process --redo + report)
 7 - Durum (status)
 8 - Sadece rapor (report)
 9 - Chrome ile giris (alternatif kaynak: source=browser)
```

1. **1** → kullanıcı adı ve şifre sorulur (şifre ekranda görünmez, **kaydedilmez**; 2FA kodu istenirse gir).
   Oturum `data/instagrapi_session.json` dosyasında saklanır; bir daha sormaz.
2. **3** → 3 gönderiyle deneme. `output/saved_posts.md` oluşur.
3. **4** → tam koşu. Sonraki günlerde yine **4**: yalnızca yeni kaydedilenler işlenir.

Terminalden aynı işler: `run.cmd ig-login`, `run.cmd run --limit 3`, `run.cmd run`.

---

## Komutlar

| Komut | Ne yapar |
|---|---|
| `run.cmd ig-login` | Mobil API oturumu oluşturur (şifre `getpass` ile, saklanmaz) |
| `run.cmd collections` | Kaydedilen koleksiyonlarını listeler |
| `run.cmd sync [--limit N] [--full] [--collection AD ...]` | Listeyi tarar, yeni gönderileri ve sabitli yorumları alır |
| `run.cmd process [--limit N] [--redo] [--no-summary]` | Medya indir → transkript/kareler → LLM analizi. `--redo` tümünü yeniden analiz eder, `--no-summary` yalnızca transkript |
| `run.cmd report` | `output/` dosyalarını yeniden üretir |
| `run.cmd status` | Durum sayaçları |
| `run.cmd run [aynı seçenekler]` | `sync` + `process` + `report` |
| `run.cmd login` / `probe URL` / `demo URL` | Alternatif tarayıcı kaynağı (Playwright + Chrome); bkz. `source` ayarı |

`--collection` birden çok kez verilebilir; koleksiyon adı büyük/küçük harf duyarsızdır.

---

## Yapılandırma

Tüm alanlar `config.example.json` içinde `_help` notlarıyla açıklanmıştır. Özet:

| Alan | Varsayılan | Açıklama |
|---|---|---|
| `username` | `""` | Instagram kullanıcı adın |
| `source` | `instagrapi` | `instagrapi` (mobil API, tarayıcısız) ya da `browser` (Playwright + Chrome) |
| `scope.collections` | `[]` | Boş → tüm kaydedilenler; dolu → yalnızca bu koleksiyonlar |
| `llm.provider` | `claude_code` | `claude_code` \| `anthropic` \| `openai_compatible` |
| `llm.model` | `""` | claude_code: `opus`/`sonnet`/`haiku`; anthropic: örn. `claude-opus-5`; openai_compatible: uç noktanın model adı |
| `llm.effort` | `""` | claude_code için `low`/`medium`/`high` |
| `llm.api_key_env` / `llm.api_key` | `ANTHROPIC_API_KEY` / `""` | anthropic ve openai_compatible için anahtar |
| `llm.base_url` | `""` | openai_compatible uç noktası |
| `analysis.frames_speech` / `frames_no_speech` | `2` / `4` | Video başına LLM'e giden kare sayısı |
| `analysis.max_images` | `6` | Karuselden alınacak en fazla görsel |
| `analysis.max_edge` | `800` | Görsel uzun kenarı (px) |
| `whisper.*` | `large-v3`, `cuda`, `float16` | Transkript modeli ve cihazı |
| `pacing.max_posts_per_run` | `40` | Koşu başına yorumları çekilecek gönderi sayısı |
| `instagrapi.delay_range` | `[3, 7]` | API istekleri arası rastgele bekleme (sn) |
| `video.keep_files` | `false` | `true` ise indirilen videolar silinmez |

---

## LLM sağlayıcıları

Analiz katmanı görsel destekli tek bir arayüz kullanır: `complete(system, user, images)`.

**`claude_code` (varsayılan)** — [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) `claude -p`
üzerinden; Claude Max/Pro aboneliğiyle çalışır, API anahtarı gerekmez. Kurulum: `npm i -g @anthropic-ai/claude-code`,
sonra terminalde `claude` → `/login`. Görseller CLI'nin `Read` aracıyla dosyadan okutulur. Not: CLI'nin `--bare`
modu kayıtlı girişi okumadığı için kullanılmaz.

**`anthropic`** — resmî Python SDK; `ANTHROPIC_API_KEY` ortam değişkeni (ya da `llm.api_key`). Görseller base64
olarak gönderilir. Örnek: `"llm": {"provider": "anthropic", "model": "claude-opus-5"}`.

**`openai_compatible`** — `{base_url}/chat/completions` konuşan her uç nokta; görseller `image_url` data URI olarak
gider. Örnekler: OpenAI (`https://api.openai.com/v1`, `gpt-4o`), yerel Ollama (`http://localhost:11434/v1`,
`qwen3-vl:8b` gibi bir **vision** modeli; metin-only modeller görselleri göremez).

Sağlayıcı değiştirdikten sonra eski analizleri yenilemek için `run.cmd process --redo`.

---

## Çıktı formatı

`saved_posts.json` her gönderi için:

```json
{
  "shortcode": "Da_S-LVze42",
  "url": "https://www.instagram.com/p/Da_S-LVze42/",
  "author": "jeffrey_in_nyc",
  "taken_at": 1784500000,
  "caption": "First Time Visiting an Otter Cafe ...",
  "has_video": true,
  "media_type": 2,
  "collections": [],
  "content_summary": "Tokyo'daki bir su samuru kafesine ...",
  "content_line": "... _(video, dil: en, 51 kelime transkript, 2 görsel incelendi)_",
  "transcript": "...",
  "language": "en",
  "no_speech": false,
  "pinned_status": "ok",
  "pinned_comments": [{"username": "jeffrey_in_nyc", "text": "📍Otters Family in Harajuku Tokyo", "is_pinned": true}],
  "comments_source": "instagrapi",
  "comment_count": 57,
  "statuses": {"details_status": "ok", "comments_status": "ok", "video_status": "ok",
               "transcript_status": "ok", "summary_status": "ok"}
}
```

`media_type`: 1 fotoğraf, 2 video, 8 karusel. Gönderiler kaydedilme sırasına göre (en yeni önce) listelenir.

---

## Artımlılık ve durum takibi

- Her gönderi `data/state.db` içinde bir satırdır; aşamalar ayrı durum sütunlarıyla izlenir
  (`details_status`, `comments_status`, `pinned_status`, `video_status`, `transcript_status`, `summary_status`).
- `sync`, listeyi en yeniden eskiye tarar ve ardışık 3 sayfa tamamen bilinen gönderi görünce durur (`--full` ile kapatılır).
- Başarısız adımlar sonraki koşuda yeniden denenir; aynı gönderi bir aşamada **en fazla 3 kez** denenir, sonra
  `failed` olarak işaretlenir ve koşu bütçesini meşgul etmez.
- Süresi dolan CDN URL'si (HTTP 403) anında mobil API'den tazelenir ve indirme bir kez daha denenir.
- `process --redo` tüm gönderileri yeniden analiz kuyruğuna alır (transkriptler yeniden üretilir, videolar yeniden indirilir).
- Şema değişikliklerinde eski `state.db` otomatik migrasyonla tamamlanır.

---

## Güvenlik, gizlilik ve riskler

- **Şifre asla saklanmaz.** `ig-login` şifreyi `getpass` ile alır, yalnızca `instagrapi` giriş çağrısına verir;
  diske yalnızca oturum çerezleri/yetki verileri yazılır (`data/instagrapi_session.json`). Bu dosya, `data/` ve `output/`
  klasörleri `.gitignore` içindedir — **repoya asla eklemeyin**; oturum dosyası hesaba erişim demektir.
- **API anahtarları** yalnızca ortam değişkeninden (ya da yerel `config.json`'dan) okunur; `config.json` de
  `.gitignore`'dadır. Repo yalnızca `config.example.json` içerir.
- **Kullanım şartları:** Instagram'ın resmî API'si kaydedilenleri vermediği için araç, hesabınızın kendi oturumuyla
  mobil API'yi kullanır. Bu, Instagram Kullanım Şartları'na aykırıdır ve hesabınızda geçici doğrulama (challenge)
  ya da kısıtlamaya yol açabilir. Riski azaltmak için: istekler arası 3-7 sn bekleme, koşu başına 40 gönderi,
  sınırlama sinyalinde anında durma (yeniden deneme yok), tek oturum. Kararı ve sorumluluğu kullanıcı alır.
- Yalnızca **kendi** kaydettiğiniz gönderiler okunur; hiçbir yazma işlemi (beğeni, yorum, takip) yapılmaz.
- LLM analizi için gönderi metni ve küçültülmüş görseller seçtiğiniz sağlayıcıya gönderilir (Claude Code / Anthropic /
  kendi uç noktanız). Yerel kalmak isterseniz `openai_compatible` + Ollama vision modeli kullanın.

---

## Sorun giderme

| Belirti | Çözüm |
|---|---|
| `OTURUM YOK` / `login_required` | `run.cmd` → 1 ile yeniden giriş |
| `SERT DURMA: ... PleaseWaitFewMinutes / ChallengeRequired` | Instagram sınırlaması. Birkaç saat bekle; challenge ise uygulamadan onayla, sonra tekrar 4 |
| `Claude Code CLI oturumu yok` | Terminalde `claude auth status`; gerekirse `claude` → `/login` |
| `claude` komutu bulunamadı | `npm i -g @anthropic-ai/claude-code` ya da `config.json → llm.exe` ile tam yol |
| `cublas64_12.dll ... cannot be loaded` | `uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`; olmadı → `whisper.device=cpu` |
| Analiz "Görselleri göremiyorum" diyor | openai_compatible'da vision olmayan model seçilmiş; vision modeli kullan |
| Rapor boş | `run.cmd status` ile sayaçlara bak; `data/igsaved.log` son satırları |

Log: `data/igsaved.log` (5 MB'de döner). Ham API yanıtları: `data/state.db` → `raw_payloads`.

---

## Geliştirme

```bat
.venv\Scripts\python -m pytest          # birim testleri (ağa çıkmaz)
```

Proje yapısı:

```
igsaved/
  cli.py          komutlar; sync / process / report akışı, deneme sayaçları, HardStop ele alma
  config.py       config.json + varsayılanlar (derin birleştirme)
  store.py        SQLite durum deposu, şema migrasyonu, ham yanıt arşivi
  ig_source.py    instagrapi kaynağı (kaydedilenler, koleksiyonlar, yorumlar, media info) + istisna eşlemesi
  ig_private.py   ig-login (getpass), oturum dosyası
  parsers.py      saf ayrıştırıcılar (feed öğesi, video/görsel URL'leri, yorumlar/is_pinned, gömülü JSON)
  saved_feed.py   kaydedilenler sayfalama (API ve tarayıcı), artımlı durma
  comments.py     yorum toplama (API; tarayıcı için strateji zinciri)
  media.py        PyAV kare çıkarma, Pillow küçültme
  video.py        CDN indirme (.part → rename)
  transcribe.py   faster-whisper sarmalayıcı, CUDA→CPU düşüşü, Windows DLL yolu düzeltmesi
  summarize.py    LLM sağlayıcıları (claude_code / anthropic / openai_compatible), görsel destekli istem
  report.py       Markdown + JSON
  browser.py      Playwright katmanı (alternatif kaynak: source=browser)
tests/            57 test: ayrıştırıcılar, durum makinesi, rapor, sağlayıcılar (sahte), koleksiyonlar, migrasyon
```

Tasarım ilkeleri: ayrıştırma saf ve test edilebilir; Instagram'a giden her çağrı tek yerden ve HardStop eşlemeli;
hiçbir sabit `doc_id`/`query_hash` yok; her adımdan sonra commit; ham yanıtlar saklanır ki şema kayarsa
yeniden ayrıştırılabilsin.

---

## Nasıl geliştirildi

Proje bir gecede, Claude Code ile birlikte, şu adımlarla yapıldı:

1. **Araştırma (14 paralel ajan):** instaloader, instagrapi, Instagram web/GraphQL/mobil API yanıt şekilleri,
   resmî "Bilgilerini indir" dışa aktarımı, Playwright yaklaşımı ve Windows/RTX 5080 üzerinde video pipeline'ı
   (faster-whisper/CTranslate2 Blackwell desteği, Türkçe için model seçimi). Her bulgu düşmanca doğrulamadan geçti.
2. **İlk sürüm (tarayıcı tabanlı):** Playwright ile oturum açık Chrome profili, Instagram'ın kendi JSON yanıtlarının
   yakalanması, çevrimdışı uçtan uca demo (herkese açık reel → CUDA transkript → Türkçe özet).
3. **Bağımsız kod incelemesi (iki tur, 30 ajan):** 17 doğrulanmış kusur düzeltildi — HardStop sonrası indirmeye devam,
   yanlış gönderiye yorum atfı, sonsuz yeniden deneme döngüleri, `id()` ile tekilleştirme, XHR JSON'daki sınırlama
   mesajının kaçırılması vb. Hepsine regresyon testi eklendi.
4. **Canlı doğrulama ve dönüş:** gerçek oturumla yapılan problar web tarafında sabitleme bilgisinin hiç olmadığını
   gösterdi; kaynak mobil API'ye taşındı, tarayıcı bağımlılığı kaldırıldı, fotoğraf/karusel/konuşmasız videolar için
   görsel analiz eklendi, Claude Max aboneliğiyle çalışan `claude_code` sağlayıcısı yazıldı, koleksiyon seçimi eklendi.

---

## Lisans

MIT — bkz. `LICENSE`.
