# instaSaved

Turn your Instagram **Saved** posts into one readable digest. For every saved post the tool produces what the post
is about (an LLM analysis of videos, photos and carousels), the caption, and the author's **pinned comments**.
No browser is opened, runs are incremental, and everything is designed for a single user (developed on Windows 11
with an NVIDIA GPU; CPU-only also works).

*Türkçe açıklama için aşağıdaki [Türkçe](#türkçe) bölümüne bakın.*

---

## Contents

1. [What it produces](#what-it-produces)
2. [How it works](#how-it-works)
3. [How pinned comments are detected](#how-pinned-comments-are-detected)
4. [Installation](#installation)
5. [Quick start](#quick-start)
6. [Commands](#commands)
7. [Configuration](#configuration)
8. [LLM providers](#llm-providers)
9. [Output format](#output-format)
10. [Incremental runs and state](#incremental-runs-and-state)
11. [Security, privacy and risks](#security-privacy-and-risks)
12. [Troubleshooting](#troubleshooting)
13. [Development](#development)
14. [How it was built](#how-it-was-built)
15. [License](#license)
16. [Türkçe](#türkçe)

---

## What it produces

One entry per post in `output/saved_posts.md`. Report labels and analysis language are configurable
(`report.language`: `tr` | `en`, `analysis.language`: e.g. `"English"`); the example below uses `en` / `English`:

```markdown
6. **@jeffrey_in_nyc** · [Open post](https://www.instagram.com/p/Da_S-LVze42/) · 2026-07-20
   - **Content:** A visitor's first trip to an otter café in Tokyo. The frames show the visitor wearing the
     café's pink protective apron with an otter climbing into their lap, while other otters roam the play area.
     The visitor talks about how cute the animals are and how they come up to cuddle.
     _(video, language: en, 51-word transcript, 2 images inspected)_
   - **Caption:**
     > First Time Visiting an Otter Cafe in Tokyo Japan #japan #travel #tokyo #experience
   - **Pinned comments:**
     - @jeffrey_in_nyc: 📍Otters Family in Harajuku Tokyo
     - @jeffrey_in_nyc: Sorry guys I said cute like a million times but I couldn't help it😆
```

With the defaults (`tr` / `Türkçe`) the same entry reads `[Postu aç]`, `**İçerik:**`, `**Açıklama:**`,
`**Sabitli yorumlar:**` and the analysis is in Turkish. A `**Collection:**` line appears when a post was found through
a named collection.

The same data is written field by field to `output/saved_posts.json` (url, author, caption, content_summary,
transcript, language, pinned_comments, collections, statuses…) for feeding other tools.

Analysis by post type:

| Type | Material | Analysis |
|---|---|---|
| Video with speech | Whisper transcript + 2 frames + caption | What is said, places/products/people shown, advice given |
| Video without speech (music) | 4 frames + caption | What is shown, on-screen text |
| Photo | The image itself + caption | What is shown, text in the image |
| Carousel | Up to 6 images (+ any video children) + caption | Summary of the whole carousel (e.g. "list of 30 Japanese phrases") |

---

## How it works

```
 ig-login (once)                          run.cmd → 4  (every run)
 ┌──────────────────┐   ┌──────────────────────────────────────────────────────────────────────┐
 │ Instagram mobile │   │ 1. sync    feed/saved/posts  ──► new posts ──► SQLite (state.db)        │
 │ API session      │──►│            media/{pk}/comments ──► pinned comments (is_pinned)         │
 │ (instagrapi)     │   │ 2. process download media from CDN ──► faster-whisper (CUDA) transcript │
 └──────────────────┘   │            frames via PyAV / images via Pillow ──► LLM analysis         │
                        │ 3. report  output/saved_posts.md + .json                              │
                        └──────────────────────────────────────────────────────────────────────┘
```

1. **Source (`ig_source.py`)** — Instagram's mobile API through `instagrapi`, used as raw JSON: saved posts
   (`feed/saved/posts/`), collections (`collections/list/`, `feed/collection/{id}/`), comments
   (`media/{pk}/comments/`) and `media/{pk}/info/` to refresh expired media URLs. Instagram's throttling and
   verification signals (`PleaseWaitFewMinutes`, `ChallengeRequired`, `LoginRequired`…) are mapped to a single
   **HardStop** exception: the program does not retry, persists its state and exits.
2. **Parsing (`parsers.py`)** — pure functions covered by unit tests; they never touch Instagram. From a feed item:
   caption, highest-resolution video/image URLs, carousel children. From a comments response: the pinned ones.
3. **Media (`video.py`, `media.py`, `transcribe.py`)** — signed CDN URLs are downloaded immediately (they expire);
   video is transcribed with `faster-whisper large-v3` (tries CUDA float16 → int8_float16 → CPU); PyAV samples frames
   evenly across the timeline; Pillow shrinks images to an 800 px long edge. Files are deleted after analysis.
4. **Analysis (`summarize.py`)** — post type, caption, transcript and images go to the LLM in one prompt; 2–4 sentences.
   The provider is pluggable (see below).
5. **State (`store.py`)** — SQLite, committed after every step. An interrupted run resumes; finished posts are never
   re-fetched. Raw API responses are archived in `raw_payloads` (latest per post and response kind) so data can be re-parsed if the
   schema drifts.
6. **Report (`report.py`)** — Markdown + JSON.

---

## How pinned comments are detected

This was the hardest part and was settled with live probes (2026-09-02):

- Instagram's **web** surface exposes no pin information even in a logged-in session: not the web
  `api/v1/media/{pk}/comments/` response, not the embedded GraphQL connection
  (`xdt_api__v1__media__media_id__comments__connection`), not the page DOM. Posts known to have pinned comments
  show nothing.
- The **mobile API** response carries `pinned_comment_count` at the root and `is_pinned: true` on pinned comment
  objects. Unpinned comments have **no key at all**, so the code reads `bool(comment.get("is_pinned"))`, never
  `comment["is_pinned"]`. `pinned_comment_count` is used as a cross-check: if fewer pinned comments were found on the
  first page than the count says, the post is marked `partial` and the report shows the count next to the list.
- Look-alike fields are deliberately ignored: `hoisted_comments` (always empty), `is_ranked_comment`,
  `comment_index`, `pinned_for_users`, `visual_comment_reply_sticker_info.is_pinned` (an unrelated integer) and the
  profile-grid `Post.is_pinned`.
- Pinned comments arrive on the first page, so only the first page is read; other comments are not stored.

The official Instagram Graph API exposes neither saved posts nor pin state, which is why the tool works with the
user's own session (see Risks).

---

## Installation

Requirements: Python 3.10+, [uv](https://docs.astral.sh/uv/) (or pip), an Instagram account. All Python dependencies
(including PyAV and Pillow) come from `requirements.txt`. GPU optional
(NVIDIA with a CUDA 12 driver; verified on an RTX 5080). ffmpeg is **not** required (PyAV bundles FFmpeg).

```bat
git clone https://github.com/ozaneski13/instaSaved.git
cd instaSaved
uv venv --python 3.10 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
copy config.example.json config.json
```

Edit at least `instagrapi.username` (or top-level `username`) and the `llm` section in `config.json` (see below).
The Whisper model (~3 GB) is downloaded on first use.

No GPU: `"whisper": {"device": "cpu", "compute_type": "int8", "model": "medium"}` (slow but works).

**Windows + pip CUDA note:** the DLLs shipped by `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` are not on PATH;
`transcribe.py` adds them to the DLL search path automatically. If you still see `cublas64_12.dll not found`, update
the driver or switch to CPU mode.

The batch launcher `run.cmd` is Windows-only; on macOS/Linux run `python -m igsaved <command>` from the project root.

---

## Quick start

Double-click `run.cmd`; a numbered menu appears (menu text is Turkish; the window stays open when a job finishes,
`0` or an empty input exits):

```
 1 - Instagram girisi (mobil API, bir kez)      Instagram login (mobile API, once)  <- first step
 2 - Koleksiyonlari listele                     List collections
 3 - Deneme kosusu, 3 post                      Trial run, 3 posts (run --limit 3)
 4 - Tam kosu                                   Full run (run)
 5 - Belirli koleksiyonda kosu                  Run on one collection (asks for its name)
 6 - Tum postlari yeniden analiz et             Re-analyze all posts (process --redo + report)
 7 - Durum                                      Status
 8 - Sadece rapor                               Report only
 9 - Chrome ile giris                           Chrome login (alternative source: source=browser)
 0 - Cikis                                      Exit
```

1. **1** → you are asked for username and password (the password is not echoed and **never stored**; enter the 2FA
   code if prompted). The session is kept in `data/instagrapi_session.json`; you will not be asked again.
2. **3** → trial with 3 posts. `output/saved_posts.md` appears.
3. **4** → full run. On later days run **4** again: only newly saved posts are processed.

From a terminal: `run.cmd ig-login`, `run.cmd run --limit 3`, `run.cmd run`.

---

## Commands

| Command | What it does |
|---|---|
| `run.cmd ig-login` | Creates the mobile API session (password via `getpass`, not stored) |
| `run.cmd collections` | Lists your saved collections |
| `run.cmd sync [--limit N] [--full] [--collection NAME ...]` | Scans the saved list, fetches new posts and their pinned comments |
| `run.cmd process [--limit N] [--redo] [--no-summary]` | Downloads media → transcript/frames → LLM analysis. `--redo` re-analyzes everything, `--no-summary` transcribes only |
| `run.cmd report` | Regenerates the `output/` files |
| `run.cmd status` | Status counters |
| `run.cmd run [same options]` | `sync` + `process` + `report` |
| `run.cmd probe URL` | Pinned-comment probe for one post (mobile API when a session exists, otherwise Chrome); writes to `data/probe.db` |
| `run.cmd login` / `demo URL` | Alternative browser source (Playwright + Chrome); see the `source` setting |

`--collection` can be repeated; names are case-insensitive.

---

## Configuration

Every field is documented with `_help` notes in `config.example.json`. The most important ones:

| Field | Default | Meaning |
|---|---|---|
| `instagrapi.username` / `username` | `""` | Your Instagram username (asked interactively if both are empty) |
| `source` | `instagrapi` | `instagrapi` (mobile API, no browser) or `browser` (Playwright + Chrome) |
| `scope.collections` | `[]` | Empty → all saved posts; otherwise only these collections |
| `llm.provider` | `claude_code` | `claude_code` \| `anthropic` \| `openai_compatible` |
| `llm.model` | `""` | claude_code: `opus`/`sonnet`/`haiku`; anthropic: e.g. `claude-opus-5`; openai_compatible: the endpoint's model name |
| `llm.effort` | `""` | `low`/`medium`/`high` for claude_code |
| `llm.api_key_env` / `llm.api_key` | `ANTHROPIC_API_KEY` / `""` | Key for anthropic and openai_compatible |
| `llm.base_url` | `""` | openai_compatible endpoint |
| `analysis.frames_speech` / `frames_no_speech` | `2` / `4` | Frames per video sent to the LLM |
| `analysis.max_images` | `6` | Max images taken from a carousel |
| `analysis.max_edge` | `800` | Image long edge in px |
| `analysis.language` | `Türkçe` | Language of the analysis text (e.g. `English`) |
| `report.language` | `tr` | Report labels: `tr` or `en` |
| `whisper.*` | `large-v3`, `cuda`, `float16` | Transcription model and device |
| `pacing.max_posts_per_run` | `40` | Posts whose comments are fetched per run |
| `instagrapi.delay_range` | `[3, 7]` | Random delay between API requests (seconds) |
| `llm.timeout` | `120` | Request timeout in seconds (`claude_code` uses at least 240) |
| `video.keep_files` | `false` | `true` keeps downloaded videos |

---

## LLM providers

The analysis layer uses one image-capable interface: `complete(system, user, images)`.

**`claude_code` (default)** — via the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) `claude -p`;
works with a Claude Max/Pro subscription, no API key needed. Setup: `npm i -g @anthropic-ai/claude-code`, then run
`claude` and `/login` once. Images are handed to the CLI's `Read` tool as files. Note: the CLI's `--bare` mode does not
pick up the stored login, so it is not used.

**`anthropic`** — the official Python SDK; `ANTHROPIC_API_KEY` environment variable (or `llm.api_key`). Images are sent
as base64. Example: `"llm": {"provider": "anthropic", "model": "claude-opus-5"}`.

**`openai_compatible`** — any endpoint speaking `{base_url}/chat/completions`; images go as `image_url` data URIs.
Examples: OpenAI (`https://api.openai.com/v1`, `gpt-4o`), local Ollama (`http://localhost:11434/v1` with a **vision**
model such as `qwen3-vl:8b`; text-only models cannot see the images).

After switching providers, refresh old analyses with `run.cmd process --redo`.

**Output language.** `analysis.language` sets the language of the analysis text (default `Türkçe`; any other value
switches to an English prompt template asking for that language) and `report.language` (`tr`/`en`) sets the report
labels. After changing either, run `process --redo` / `report`.

---

## Output format

`saved_posts.json`, one object per post:

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

`media_type`: 1 photo, 2 video, 8 carousel. Posts are listed newest-saved first.

---

## Incremental runs and state

- Every post is a row in `data/state.db`; stages are tracked in separate status columns
  (`details_status`, `comments_status`, `pinned_status`, `video_status`, `transcript_status`, `summary_status`).
- `sync` walks the list newest-first and stops after 3 consecutive pages of already-known posts (`--full` disables this).
- Failed steps are retried on the next run; each stage (comments, media/URL refresh, analysis) is attempted
  **at most 3 times** per post, then marked `failed` so it stops consuming the run budget (`process --redo` resets
  the analysis counter).
- An expired or missing CDN URL (HTTP 403/404/410) is refreshed from the mobile API immediately and the download
  retried once (mobile API session required).
- `process --redo` queues every post whose details are complete for re-analysis (transcripts are regenerated; videos are
  re-downloaded unless `video.keep_files` kept a copy).
- Older `state.db` files are completed automatically by a schema migration.

---

## Security, privacy and risks

- **Passwords are never stored.** `ig-login` reads the password with `getpass` and passes it only to the `instagrapi`
  login call; only session cookies/authorization data are written to disk (`data/instagrapi_session.json`). That file,
  the `data/` and `output/` folders are in `.gitignore` — **never commit them**; the session file grants access to the account.
- **API keys** are read only from environment variables (or your local `config.json`), which is also ignored by git.
  The repository ships `config.example.json` plus two provider examples (`config.anthropic-api.json`,
  `config.ollama.json`), none containing secrets.
- **Terms of use:** because Instagram's official API does not expose saved posts, the tool uses the mobile API with your
  own session. This violates Instagram's Terms of Use and may trigger a temporary verification (challenge) or
  restriction on your account. Mitigations: 3–7 s between requests, 40 posts per run, immediate stop on throttling
  signals (no retries), a single session. The decision and responsibility are the user's.
- Only **your own** saved posts are read; nothing is written (no likes, comments or follows).
- For the analysis, post text and downscaled images are sent to the provider you choose (Claude Code / Anthropic /
  your own endpoint). To stay local, use `openai_compatible` with an Ollama vision model.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `OTURUM YOK` / `login_required` | `run.cmd` → 1 to log in again |
| `SERT DURMA: ... PleaseWaitFewMinutes / ChallengeRequired` | Instagram throttling. Wait a few hours; for a challenge, approve it in the app, then run 4 again |
| `Claude Code CLI oturumu yok` | `claude auth status` in a terminal; if needed `claude` → `/login` |
| `claude` command not found | `npm i -g @anthropic-ai/claude-code` or set the full path in `config.json → llm.exe` |
| `cublas64_12.dll ... cannot be loaded` | `uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`; otherwise `whisper.device=cpu` |
| Analysis says it cannot see the images | A non-vision model is configured for openai_compatible; use a vision model |
| Empty report | Check counters with `run.cmd status`; read the tail of `data/igsaved.log` |

Log: `data/igsaved.log` (rotates at 5 MB). Raw API responses: `data/state.db` → `raw_payloads`. Log messages are in Turkish.

---

## Development

```bat
.venv\Scripts\python -m pytest          # unit tests (fully offline)
```

Project layout:

```
igsaved/
  cli.py          commands; sync / process / report flow, attempt caps, HardStop handling
  config.py       config.json + defaults (deep merge)
  store.py        SQLite state store, schema migration, raw response archive
  ig_source.py    instagrapi source (saved posts, collections, comments, media info) + exception mapping
  ig_private.py   ig-login (getpass), session file
  parsers.py      pure parsers (feed item, video/image URLs, comments/is_pinned, embedded JSON)
  saved_feed.py   saved-list pagination (API and browser), incremental stop
  comments.py     comment collection (API; strategy chain for the browser source)
  media.py        PyAV frame extraction, Pillow downscaling
  video.py        CDN download (.part → rename)
  transcribe.py   faster-whisper wrapper, CUDA→CPU fallback, Windows DLL path fix
  summarize.py    LLM providers (claude_code / anthropic / openai_compatible), image-capable prompt
  report.py       Markdown + JSON
  browser.py      Playwright layer (alternative source: source=browser)
tests/            57 tests: parsers, state machine, report, providers (fakes), collections, migration
```

Design principles: parsing is pure and testable; every Instagram call goes through one place and is mapped to
HardStop; no hard-coded `doc_id`/`query_hash`; commit after every step; raw responses are kept so they can be
re-parsed if the schema drifts.

---

## How it was built

The project was built in one night together with Claude Code:

1. **Research (14 parallel agents):** instaloader, instagrapi, Instagram web/GraphQL/mobile API response shapes,
   the official "Download your information" export, a Playwright approach, and the video pipeline on Windows/RTX 5080
   (faster-whisper/CTranslate2 Blackwell support, model choice for Turkish). Every finding went through adversarial
   verification.
2. **First version (browser-based):** Playwright with a logged-in Chrome profile, capturing Instagram's own JSON
   responses, and an offline end-to-end demo (public reel → CUDA transcript → Turkish summary).
3. **Independent code review (two rounds, 30 agents):** 17 confirmed defects fixed — continuing downloads after a
   HardStop, attributing comments to the wrong post, endless retry loops, `id()`-based deduplication, missing the
   throttling message inside XHR JSON, and more. Each got a regression test.
4. **Live verification and pivot:** probes with a real session showed the web surface carries no pin information at
   all; the source moved to the mobile API, the browser dependency was dropped, image analysis was added for photos,
   carousels and speechless videos, a `claude_code` provider running on a Claude Max subscription was written, and
   collection selection was added.

---

## License

MIT — see `LICENSE`.

---

## Türkçe

Instagram **Kaydedilenler** listendeki her gönderiyi tek bir okunabilir listeye döker: gönderide ne anlatılıyor / gösteriliyor (video, fotoğraf ve karuseller için LLM analizi), açıklama metni ve gönderi sahibinin **sabitlediği yorumlar**. Tarayıcı açmaz, artımlı çalışır, tek kullanıcı için tasarlanmıştır. Kurulum ve komutlar İngilizce bölümdekiyle aynıdır; aşağıda tam Türkçe açıklama.

### Ne üretir

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

### Nasıl çalışır

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
   gönderi ve yanıt türü başına yalnızca en yenisi tutulur).
6. **Rapor (`report.py`)** — Markdown + JSON.

---

### Sabitli yorumlar nasıl bulunuyor

Bu projenin en zor kısmıydı ve kesin sonuca canlı probelarla ulaşıldı (2026-09-02):

- Instagram'ın **web** tarafı (giriş yapılmış oturumda bile) sabitleme bilgisi vermiyor: ne `api/v1/media/{pk}/comments/`
  web yanıtı, ne gömülü GraphQL (`xdt_api__v1__media__media_id__comments__connection`), ne de sayfa DOM'u.
  Sabitli yorum olduğu bilinen gönderilerde bile alan yok.
- **Mobil API** yanıtı ise kökte `pinned_comment_count` ve sabitli yorum nesnesinde `is_pinned: true` taşıyor.
  Sabitli olmayan yorumda anahtar **hiç yok** — bu yüzden kod `bool(comment.get("is_pinned"))` okur, `comment["is_pinned"]`
  asla. `pinned_comment_count` çapraz kontrol içindir: ilk sayfada sayaçtan az sabitli bulunursa post `partial` olarak
  işaretlenir ve raporda listenin yanında sayaç gösterilir.
- Tuzak alanlar bilinçli olarak yok sayılır: `hoisted_comments` (her zaman boş), `is_ranked_comment`, `comment_index`,
  `pinned_for_users`, `visual_comment_reply_sticker_info.is_pinned` (alakasız bir tamsayı), profil sabitlemesi `Post.is_pinned`.
- Sabitli yorumlar ilk sayfada geldiği için yalnızca ilk sayfa okunur; diğer yorumlar saklanmaz.

Resmî Instagram Graph API ne kaydedilenleri ne de sabitleme bilgisini verir; bu yüzden araç kullanıcının kendi
oturumuyla çalışır (bkz. Riskler).

---

### Kurulum

Gereksinimler: Python 3.10+, [uv](https://docs.astral.sh/uv/) (ya da pip), Instagram hesabı. GPU isteğe bağlı
(NVIDIA + CUDA 12 sürücüsü; RTX 5080'de doğrulandı). ffmpeg **gerekmez** (PyAV bundled FFmpeg kullanır).

```bat
git clone https://github.com/ozaneski13/instaSaved.git
cd instaSaved
uv venv --python 3.10 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
copy config.example.json config.json
```

`config.json` içinde en azından `instagrapi.username` (ya da üst düzey `username`) ve `llm` bölümünü düzenle
(aşağıda). Whisper modeli (~3 GB) ilk kullanımda indirilir.

GPU yoksa: `"whisper": {"device": "cpu", "compute_type": "int8", "model": "medium"}` (yavaş ama çalışır).

**Windows + pip CUDA notu:** `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` paketlerinin DLL'leri PATH'te olmaz;
`transcribe.py` bunları otomatik olarak DLL arama yoluna ekler. Yine de `cublas64_12.dll not found` görürsen
sürücünü güncelle ya da CPU moduna geç.

---

### Hızlı başlangıç

`run.cmd` dosyasına çift tıkla; numaralı menü açılır (pencere iş bitince kapanmaz):

```
 1 - Instagram girisi (mobil API, bir kez)   <- ilk adim
 2 - Koleksiyonlari listele
 3 - Deneme kosusu, 3 post (run --limit 3)
 4 - Tam kosu (run)
 5 - Belirli koleksiyonda kosu (adi sorar)
 6 - Tum postlari yeniden analiz et (process --redo + report)
 7 - Durum (status)
 8 - Sadece rapor (report)
 9 - Chrome ile giris (alternatif kaynak: source=browser)
 0 - Cikis (bos Enter de cikar)
```

1. **1** → kullanıcı adı ve şifre sorulur (şifre ekranda görünmez, **kaydedilmez**; 2FA kodu istenirse gir).
   Oturum `data/instagrapi_session.json` dosyasında saklanır; bir daha sormaz.
2. **3** → 3 gönderiyle deneme. `output/saved_posts.md` oluşur.
3. **4** → tam koşu. Sonraki günlerde yine **4**: yalnızca yeni kaydedilenler işlenir.

Terminalden aynı işler: `run.cmd ig-login`, `run.cmd run --limit 3`, `run.cmd run`.

---

### Komutlar

| Komut | Ne yapar |
|---|---|
| `run.cmd ig-login` | Mobil API oturumu oluşturur (şifre `getpass` ile, saklanmaz) |
| `run.cmd collections` | Kaydedilen koleksiyonlarını listeler |
| `run.cmd sync [--limit N] [--full] [--collection AD ...]` | Listeyi tarar, yeni gönderileri ve sabitli yorumları alır |
| `run.cmd process [--limit N] [--redo] [--no-summary]` | Medya indir → transkript/kareler → LLM analizi. `--redo` tümünü yeniden analiz eder, `--no-summary` yalnızca transkript |
| `run.cmd report` | `output/` dosyalarını yeniden üretir |
| `run.cmd status` | Durum sayaçları |
| `run.cmd run [aynı seçenekler]` | `sync` + `process` + `report` |
| `run.cmd probe URL` | Tek postta sabitli yorum probu (oturum varsa mobil API, yoksa Chrome); `data/probe.db`'ye yazar |
| `run.cmd login` / `demo URL` | Alternatif tarayıcı kaynağı (Playwright + Chrome); bkz. `source` ayarı |

`--collection` birden çok kez verilebilir; koleksiyon adı büyük/küçük harf duyarsızdır.

---

### Yapılandırma

Tüm alanlar `config.example.json` içinde `_help` notlarıyla açıklanmıştır. En önemlileri:

| Alan | Varsayılan | Açıklama |
|---|---|---|
| `instagrapi.username` / `username` | `""` | Instagram kullanıcı adın (ikisi de boşsa girişte sorulur) |
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
| `analysis.language` | `Türkçe` | Analiz metninin dili (örn. `English`) |
| `report.language` | `tr` | Rapor etiketleri: `tr` ya da `en` |
| `whisper.*` | `large-v3`, `cuda`, `float16` | Transkript modeli ve cihazı |
| `pacing.max_posts_per_run` | `40` | Koşu başına yorumları çekilecek gönderi sayısı |
| `instagrapi.delay_range` | `[3, 7]` | API istekleri arası rastgele bekleme (sn) |
| `video.keep_files` | `false` | `true` ise indirilen videolar silinmez |

---

### LLM sağlayıcıları

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

### Çıktı formatı

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

### Artımlılık ve durum takibi

- Her gönderi `data/state.db` içinde bir satırdır; aşamalar ayrı durum sütunlarıyla izlenir
  (`details_status`, `comments_status`, `pinned_status`, `video_status`, `transcript_status`, `summary_status`).
- `sync`, listeyi en yeniden eskiye tarar ve ardışık 3 sayfa tamamen bilinen gönderi görünce durur (`--full` ile kapatılır).
- Başarısız adımlar sonraki koşuda yeniden denenir; her aşama (yorumlar, medya/URL tazeleme, analiz) gönderi başına
  **en fazla 3 kez** denenir, sonra `failed` olarak işaretlenir ve koşu bütçesini meşgul etmez (`process --redo` analiz
  sayacını sıfırlar).
- Süresi dolan ya da kaybolan CDN URL'si (HTTP 403/404/410) anında mobil API'den tazelenir ve indirme bir kez daha
  denenir (mobil API oturumu gerekir).
- `process --redo` detayları tamam olan tüm gönderileri yeniden analiz kuyruğuna alır (transkriptler yeniden üretilir;
  `video.keep_files` ile saklanmış video varsa yeniden indirilmez).
- Şema değişikliklerinde eski `state.db` otomatik migrasyonla tamamlanır.

---

### Güvenlik, gizlilik ve riskler

- **Şifre asla saklanmaz.** `ig-login` şifreyi `getpass` ile alır, yalnızca `instagrapi` giriş çağrısına verir;
  diske yalnızca oturum çerezleri/yetki verileri yazılır (`data/instagrapi_session.json`). Bu dosya, `data/` ve `output/`
  klasörleri `.gitignore` içindedir — **repoya asla eklemeyin**; oturum dosyası hesaba erişim demektir.
- **API anahtarları** yalnızca ortam değişkeninden (ya da yerel `config.json`'dan) okunur; `config.json` de
  `.gitignore`'dadır. Repo `config.example.json` ile iki sağlayıcı örneği (`config.anthropic-api.json`,
  `config.ollama.json`) içerir; hiçbirinde anahtar yok.
- **Kullanım şartları:** Instagram'ın resmî API'si kaydedilenleri vermediği için araç, hesabınızın kendi oturumuyla
  mobil API'yi kullanır. Bu, Instagram Kullanım Şartları'na aykırıdır ve hesabınızda geçici doğrulama (challenge)
  ya da kısıtlamaya yol açabilir. Riski azaltmak için: istekler arası 3-7 sn bekleme, koşu başına 40 gönderi,
  sınırlama sinyalinde anında durma (yeniden deneme yok), tek oturum. Kararı ve sorumluluğu kullanıcı alır.
- Yalnızca **kendi** kaydettiğiniz gönderiler okunur; hiçbir yazma işlemi (beğeni, yorum, takip) yapılmaz.
- LLM analizi için gönderi metni ve küçültülmüş görseller seçtiğiniz sağlayıcıya gönderilir (Claude Code / Anthropic /
  kendi uç noktanız). Yerel kalmak isterseniz `openai_compatible` + Ollama vision modeli kullanın.

---

### Sorun giderme

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

### Geliştirme

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

### Nasıl geliştirildi

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

### Lisans

MIT — bkz. `LICENSE`.
