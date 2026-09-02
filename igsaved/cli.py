"""Komut satırı.

Ana akış (tarayıcısız, config source="instagrapi"):
  ig-login | collections | sync [--limit] [--full] [--collection AD ...] | process [--limit] [--redo] [--no-summary]
  | report | status | run [...]
Alternatif kaynak (source="browser", Playwright + Chrome): login | probe URL | demo URL
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests

from . import media as media_mod
from . import video as video_mod
from .browser import HardStop, IGBrowser, LoginRequired
from .comments import collect_comments, collect_comments_api, refresh_details_from_page, refresh_post
from .config import Config
from .ig_source import CollectionInfo, IGSource
from .parsers import PostRecord, extract_shortcode, parse_feed_item
from .report import write_reports
from .saved_feed import enumerate_saved, enumerate_saved_api
from .store import Store, loads_list
from .summarize import ProviderUnavailable, analyze, make_provider
from .transcribe import Transcriber

log = logging.getLogger("igsaved")
EXIT_OK, EXIT_ERROR, EXIT_HARDSTOP = 0, 1, 2
MAX_ATTEMPTS = 3  # bir post bu kadar kez başarısız olursa artık koşu bütçesini yemez


def setup_logging(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            RotatingFileHandler(cfg.data_dir / "igsaved.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"),
        ],
    )
    for noisy in ("httpx", "urllib3", "instagrapi", "private_request", "public_request"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def default_config_path() -> Path | None:
    candidate = Path(__file__).resolve().parent.parent / "config.json"
    return candidate if candidate.exists() else None


def _use_browser(cfg: Config) -> bool:
    return (cfg.get("source") or "instagrapi").lower() == "browser"


# --------------------------------------------------------------------------- giriş komutları
def cmd_ig_login(cfg: Config) -> int:
    from . import ig_private

    name = ig_private.interactive_login(cfg)
    print(f"Oturum kaydedildi (@{name}). Artık `run.cmd` → 3 (deneme) ya da 4 (tam koşu).")
    return EXIT_OK


def cmd_login(cfg: Config) -> int:
    """Alternatif kaynak: Chrome profiline bir kez giriş (config source='browser' ile kullanılır)."""
    with IGBrowser(cfg, headless=False) as b:
        b.page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        b.page.wait_for_timeout(2000)
        if b.is_logged_in():
            print(f"Oturum zaten açık: @{b.username()}")
            return EXIT_OK
        print("Açılan Chrome penceresinde Instagram'a giriş yap. Giriş algılanınca bu komut kendiliğinden biter (en fazla 15 dk).")
        deadline = time.time() + 900
        while time.time() < deadline:
            b.page.wait_for_timeout(2000)
            if b.is_logged_in():
                b.page.wait_for_timeout(3000)
                try:
                    name = b.username()
                except LoginRequired:
                    name = "?"
                print(f"Giriş başarılı: @{name}. Oturum data/chrome-profile içinde saklandı; şifre kaydedilmedi.")
                return EXIT_OK
        print("Zaman aşımı: giriş algılanmadı.")
        return EXIT_ERROR


def _require_browser_login(b: IGBrowser) -> None:
    b.page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    b.page.wait_for_timeout(1500)
    if not b.is_logged_in():
        raise LoginRequired("Chrome oturumu yok: önce `run.cmd login` çalıştır.")


# --------------------------------------------------------------------------- yardımcılar
def _store_comments_outcome(store: Store, shortcode: str, outcome) -> None:
    """Yalnızca sabitli yorumlar saklanır (diğer yorumlar gerekli değil)."""
    fields = dict(
        comments_status=outcome.status,
        pinned_status=outcome.pinned_status,
        pinned_json=json.dumps(outcome.pinned_dicts(), ensure_ascii=False),
        comments_json=None,
        comments_source=outcome.source,
        error=outcome.note if outcome.status == "failed" else None,
    )
    if outcome.result is not None and outcome.result.comment_count is not None:
        fields["comment_count"] = outcome.result.comment_count
    store.update(shortcode, **fields)


def _apply_media(store: Store, shortcode: str, media: dict, kind: str = "post_media_api") -> dict | None:
    rec = parse_feed_item(media)
    if rec is None:
        return None
    store.save_raw(shortcode, kind, media)
    fields: dict = dict(
        pk=rec.pk, author=rec.author, taken_at=rec.taken_at, caption=rec.caption, media_type=rec.media_type,
        product_type=rec.product_type, comment_count=rec.comment_count, details_status="ok",
    )
    if rec.video_urls:
        fields["has_video"] = 1
        fields["video_urls"] = json.dumps(rec.video_urls)
    elif rec.has_video:
        fields["has_video"] = 1
    if rec.image_urls:
        fields["image_urls"] = json.dumps(rec.image_urls)
    store.update(shortcode, **{k: v for k, v in fields.items() if v is not None})
    return store.get(shortcode)


def refresh_details_api(source: IGSource, store: Store, shortcode: str) -> dict | None:
    post = store.get(shortcode)
    if not post or not post.get("pk"):
        return None
    media = source.media_info_raw(str(post["pk"]))
    return _apply_media(store, shortcode, media) if media else None


def _collections_for(cfg: Config, source: IGSource, names: list[str] | None) -> list[CollectionInfo]:
    wanted = [n for n in (names or []) if n and n.strip()] or list(cfg.get("scope", "collections") or [])
    return source.resolve_collections(wanted) if wanted else []


def requeue_unknown_pins(store: Store, cfg: Config) -> int:
    """Web yolundan kalan 'unknown' sabitli durumları, mobil API oturumu varsa yeniden kuyruğa alır."""
    from . import ig_private
    if not cfg.get("instagrapi", "enabled") or not ig_private.session_available(cfg):
        return 0
    cur = store.conn.execute(
        "UPDATE posts SET comments_status='pending', comments_attempts=0, updated_at=? "
        "WHERE pinned_status='unknown' AND (comments_source IS NULL OR comments_source != 'instagrapi')",
        (int(time.time()),),
    )
    store.conn.commit()
    return cur.rowcount


def _note_refresh_failure(store: Store, shortcode: str) -> int:
    """Medya URL'si alınamadı: deneme sayacını artırır; MAX'a ulaşınca postu kalıcı 'failed' yapar (sessizce
    'Henüz işlenmedi' kalmasın, kuyrukları da meşgul etmesin)."""
    attempts = store.bump(shortcode, "video_attempts")
    if attempts >= MAX_ATTEMPTS:
        store.update(shortcode, details_status="ok", summary_status="failed", transcript_status="failed",
                     error=f"medya URL'si {attempts} denemede alınamadı; bu post atlanıyor")
    else:
        store.update(shortcode, details_status="pending", error="medya URL'si yok; sonraki koşuda tazelenecek")
    return attempts


def _visit_queue(store: Store, max_posts: int) -> list[tuple[str, dict]]:
    """Bu koşuda işlenecek postlar: yorumları bekleyenler + yalnızca detayı (video/görsel URL) tazelenecekler."""
    comment_posts = store.pending("comments_status", limit=max_posts, where_extra=f"AND comments_attempts < {MAX_ATTEMPTS}")
    seen = {p["shortcode"] for p in comment_posts}
    refresh_rows = store.conn.execute(
        f"SELECT * FROM posts WHERE details_status='pending' "
        f"AND (comments_status NOT IN ('pending','partial','failed') OR COALESCE(comments_attempts,0) >= {MAX_ATTEMPTS}) "
        f"AND COALESCE(video_attempts,0) < {MAX_ATTEMPTS} ORDER BY first_seen_run DESC, feed_position ASC LIMIT {int(max_posts)}"
    ).fetchall()
    refresh_posts = [dict(r) for r in refresh_rows if r["shortcode"] not in seen]
    reserve = min(len(refresh_posts), max(1, max_posts // 4))
    queue: list[tuple[str, dict]] = [("comments", p) for p in comment_posts[: max_posts - reserve]]
    queue += [("refresh", p) for p in refresh_posts[: max_posts - len(queue)]]
    return queue


# --------------------------------------------------------------------------- collections / sync
def cmd_collections(cfg: Config) -> int:
    source = IGSource(cfg)
    cols = source.collections()
    if not cols:
        print("Koleksiyon bulunamadı.")
        return EXIT_OK
    print("Koleksiyonlar (config.json → scope.collections ya da `run --collection AD`):")
    for c in cols:
        tag = "  (tümü)" if c.is_all else ""
        print(f"  - {c.name}  [{c.count if c.count is not None else '?'} post]{tag}")
    return EXIT_OK


def cmd_sync(cfg: Config, store: Store, limit: int | None, full: bool, collection_names: list[str] | None = None) -> int:
    run_id = store.start_run("sync")
    exit_code = EXIT_OK
    stats: dict = {}
    try:
        if _use_browser(cfg):
            stats = _sync_browser(cfg, store, run_id, limit, full)
        else:
            source = IGSource(cfg)
            cols = _collections_for(cfg, source, collection_names)
            stats = enumerate_saved_api(source, store, cfg, run_id, limit_new=limit, incremental=not full, collections=cols)
            log.info("Kaydedilenler tarandı: %s", stats)
            requeued = requeue_unknown_pins(store, cfg)
            if requeued:
                log.info("Sabitli durumu belirsiz %d post mobil API için yeniden kuyruğa alındı", requeued)
            max_posts = limit or int(cfg.get("pacing", "max_posts_per_run") or 40)
            queue = _visit_queue(store, max_posts)
            log.info("Yorum/detay aşaması: %d post (koşu sınırı %d)", len(queue), max_posts)
            for kind, post in queue:
                code = post["shortcode"]
                try:
                    if kind == "refresh":
                        refreshed = refresh_details_api(source, store, code)
                        got = bool(refreshed and (loads_list(refreshed.get("video_urls")) or loads_list(refreshed.get("image_urls"))))
                        if not got:
                            _note_refresh_failure(store, code)
                        log.info("%s: detay tazelendi (medya URL %s)", code, "var" if got else "yok")
                    else:
                        outcome = collect_comments_api(source, store, post)
                        _store_comments_outcome(store, code, outcome)
                        if outcome.status != "ok":
                            store.bump(code, "comments_attempts")
                        log.info("%s: yorumlar=%s sabitli=%d", code, outcome.status, len(outcome.pinned_dicts()))
                except HardStop:
                    raise
                except Exception as exc:
                    log.exception("%s: aşama hatası", code)
                    if kind == "refresh":
                        store.bump(code, "video_attempts")
                    else:
                        store.update(code, comments_status="failed", pinned_status="failed", error=str(exc)[:500])
                        store.bump(code, "comments_attempts")
    except LoginRequired as exc:
        log.error("OTURUM YOK: %s", exc)
        exit_code = EXIT_HARDSTOP
    except HardStop as exc:
        log.error("SERT DURMA: %s — state kaydedildi, birkaç saat sonra tekrar dene.", exc)
        exit_code = EXIT_HARDSTOP
    except ValueError as exc:  # koleksiyon adı bulunamadı
        log.error("%s", exc)
        exit_code = EXIT_ERROR
    finally:
        store.finish_run(run_id, json.dumps(stats, ensure_ascii=False))
    return exit_code


def _sync_browser(cfg: Config, store: Store, run_id: int, limit: int | None, full: bool) -> dict:
    """Alternatif kaynak: Playwright + Chrome (config source='browser')."""
    with IGBrowser(cfg) as b:
        _require_browser_login(b)
        stats = enumerate_saved(b, store, cfg, run_id, limit_new=limit, incremental=not full)
        log.info("Kaydedilenler tarandı: %s", stats)
        max_posts = limit or int(cfg.get("pacing", "max_posts_per_run") or 40)
        queue = _visit_queue(store, max_posts)
        for i, (kind, post) in enumerate(queue):
            code = post["shortcode"]
            visited = True
            try:
                if kind == "refresh":
                    refreshed = refresh_post(b, store, code)
                    if not (refreshed and loads_list(refreshed.get("video_urls"))):
                        store.bump(code, "video_attempts")
                else:
                    outcome = collect_comments(b, store, cfg, post)
                    visited = outcome.visited
                    _store_comments_outcome(store, code, outcome)
                    if outcome.status != "ok":
                        store.bump(code, "comments_attempts")
            except HardStop:
                raise
            except Exception as exc:
                log.exception("%s: ziyaret hatası", code)
                store.update(code, comments_status="failed", pinned_status="failed", error=str(exc)[:500])
                store.bump(code, "comments_attempts")
                continue
            if i < len(queue) - 1:
                b.sleep("between_posts" if visited else "between_fetches")
        return stats


# --------------------------------------------------------------------------- process (analiz)
def _kind_label(post: dict) -> str:
    mt = post.get("media_type")
    if mt == 8:
        return "karusel (birden çok görsel/video)"
    if mt == 1:
        return "fotoğraf"
    return "video"


def _download_with_refresh(url: str, dest: Path, source: IGSource | None, store: Store, post: dict, list_key: str, idx: int) -> Path:
    try:
        return video_mod.download(url, dest)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if source is None or status not in (403, 404, 410):
            raise
        log.info("%s: medya URL'si süresi dolmuş (HTTP %s), mobil API'den tazeleniyor", post["shortcode"], status)
        refreshed = refresh_details_api(source, store, post["shortcode"])
        urls = loads_list((refreshed or {}).get(list_key))
        if len(urls) <= idx:
            raise
        return video_mod.download(urls[idx], dest)


def _analyze_one(cfg: Config, store: Store, provider, transcriber: Transcriber, source: IGSource | None,
                 post: dict, tmp_dir: Path, video_dir: Path) -> None:
    code = post["shortcode"]
    keep = bool(cfg.get("video", "keep_files"))
    frames_speech = int(cfg.get("analysis", "frames_speech") or 2)
    frames_no_speech = int(cfg.get("analysis", "frames_no_speech") or 4)
    max_images = int(cfg.get("analysis", "max_images") or 6)
    max_edge = int(cfg.get("analysis", "max_edge") or 800)

    video_urls = loads_list(post.get("video_urls"))
    image_urls = loads_list(post.get("image_urls"))
    if not video_urls and not image_urls:
        refreshed = refresh_details_api(source, store, code) if source else None
        if refreshed:
            post = refreshed
            video_urls, image_urls = loads_list(post.get("video_urls")), loads_list(post.get("image_urls"))
        if not video_urls and not image_urls:
            _note_refresh_failure(store, code)
            return

    temp_files: list[Path] = []
    texts, langs, lang_probs = [], [], []
    no_speech_all = True
    frames: list[Path] = []
    images: list[Path] = []
    try:
        for idx, url in enumerate(video_urls):
            dest = video_dir / f"{code}_{idx}.mp4"
            if not dest.exists():
                _download_with_refresh(url, dest, source, store, post, "video_urls", idx)
            store.update(code, video_status="ok")
            tr = transcriber.transcribe(dest)
            log.info("%s[%d]: dil=%s p=%.2f no_speech=%s (%s/%s) %d karakter", code, idx, tr.language,
                     tr.language_probability or 0, tr.no_speech, tr.device, tr.compute_type, len(tr.text))
            if not tr.no_speech:
                texts.append(tr.text)
                no_speech_all = False
            if tr.language:
                langs.append(tr.language)
                lang_probs.append(float(tr.language_probability or 0))
            if provider is not None:
                n = frames_no_speech if tr.no_speech else frames_speech
                got = media_mod.extract_frames(dest, n, tmp_dir, max_edge)
                frames += got
                temp_files += got
            if not keep:
                video_mod.remove(dest)
        for idx, url in enumerate(image_urls[:max_images]):
            raw = tmp_dir / f"{code}_i{idx}.bin"
            _download_with_refresh(url, raw, source, store, post, "image_urls", idx)
            img = media_mod.prepare_image(raw, tmp_dir / f"{code}_i{idx}.jpg", max_edge)
            images.append(img)
            temp_files.append(img)
        transcript = "\n".join(texts).strip()
        if video_urls:
            store.update(code, transcript=transcript, language=langs[0] if langs else None,
                         language_prob=max(lang_probs) if lang_probs else None,
                         no_speech=int(no_speech_all), transcript_status="ok", error=None)
        else:
            store.update(code, transcript_status="no_video", video_status="no_video", no_speech=None)
        if provider is None:
            return
        kind = _kind_label(post)
        note = ", ".join(x for x in [f"{len(frames)} video karesi" if frames else "", f"{len(images)} fotoğraf" if images else ""] if x)
        summary = analyze(provider, kind, post.get("author"), post.get("caption"), transcript,
                          langs[0] if langs else None, images=frames + images, note=note)
        if not summary:
            raise RuntimeError("LLM boş yanıt döndü")
        store.update(code, summary=summary, summary_status="ok", error=None,
                     analysis_meta=json.dumps({"kind": kind, "frames": len(frames), "images": len(images),
                                               "provider": getattr(provider, "name", "?")}, ensure_ascii=False))
        log.info("%s: analiz tamam (%s, %d kare, %d fotoğraf)", code, kind, len(frames), len(images))
    except HardStop:
        raise
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        log.warning("%s: medya indirilemedi (HTTP %s)", code, status)
        store.update(code, video_status="failed", details_status="pending", error=f"medya HTTP {status}")
        store.bump(code, "video_attempts")
    except Exception as exc:
        log.exception("%s: analiz hatası", code)
        store.update(code, summary_status="failed", error=str(exc)[:500])
    finally:
        media_mod.cleanup(temp_files)


def cmd_process(cfg: Config, store: Store, limit: int | None, no_summary: bool, redo: bool = False) -> int:
    if redo:
        n = store.conn.execute("UPDATE posts SET summary_status='pending' WHERE details_status='ok'").rowcount
        store.conn.commit()
        log.info("Yeniden analiz: %d post kuyruğa alındı", n)
    provider = None
    if not no_summary:
        try:
            provider = make_provider(cfg)
            log.info("Analiz sağlayıcısı: %s / %s", provider.name, getattr(provider, "model", "") or "varsayılan model")
        except ProviderUnavailable as exc:
            log.warning("Analiz atlanacak (yalnızca transkript): %s", exc)
    source: IGSource | None = None
    if not _use_browser(cfg):
        try:
            source = IGSource(cfg)
        except LoginRequired as exc:
            log.warning("Mobil API oturumu yok; süresi dolan medya URL'leri tazelenemez (%s)", exc)
    transcriber = Transcriber(cfg)
    tmp_dir = cfg.data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    video_dir: Path = cfg.path("video", "dir")
    rows = store.pending("summary_status", limit=limit,
                         where_extra=f"AND details_status='ok' AND COALESCE(video_attempts,0) < {MAX_ATTEMPTS}")
    log.info("Analiz aşaması: %d post", len(rows))
    for post in rows:
        try:
            _analyze_one(cfg, store, provider, transcriber, source, post, tmp_dir, video_dir)
        except HardStop as exc:
            log.error("SERT DURMA (analiz sırasında): %s", exc)
            return EXIT_HARDSTOP
    return EXIT_OK


# --------------------------------------------------------------------------- rapor / durum / probe / demo
def cmd_report(cfg: Config, store: Store) -> int:
    md, js = write_reports(store, cfg.output_dir)
    print(f"Rapor yazıldı:\n  {md}\n  {js}")
    return EXIT_OK


def cmd_status(store: Store) -> int:
    for key, value in sorted(store.counts().items()):
        print(f"{key}: {value}")
    return EXIT_OK


def cmd_probe(cfg: Config, store: Store, url: str) -> int:
    """Tek postta yorum/sabitli probu: mobil API oturumu varsa onunla, yoksa Chrome ile."""
    code = extract_shortcode(url)
    if not code:
        print(f"Shortcode çözülemedi: {url}")
        return EXIT_ERROR
    run_id = store.start_run("probe")
    try:
        if store.get(code) is None:
            store.upsert_from_feed(PostRecord(shortcode=code), run_id, 0)
        from . import ig_private
        if ig_private.session_available(cfg) and not _use_browser(cfg):
            source = IGSource(cfg)
            post = store.get(code)
            if not post.get("pk"):
                with IGBrowser(cfg) as b:
                    t0 = time.time()
                    b.goto(f"https://www.instagram.com/p/{code}/")
                    b.page.wait_for_timeout(2500)
                    refresh_details_from_page(b, store, code, [c.json for c in b.captured_since(t0) if c.json is not None])
                post = store.get(code)
            outcome = collect_comments_api(source, store, post)
        else:
            with IGBrowser(cfg) as b:
                _require_browser_login(b)
                outcome = collect_comments(b, store, cfg, store.get(code))
        _store_comments_outcome(store, code, outcome)
        post = store.get(code)
        print("\n=== PROBE SONUCU ===")
        print(f"Post: https://www.instagram.com/p/{code}/  pk={post.get('pk')}  @{post.get('author')}")
        print(f"Kaynak: {outcome.source}  durum: {outcome.status}  sabitli durumu: {outcome.pinned_status}  {outcome.note}")
        if outcome.result:
            print(f"Yorum (ilk sayfa): {len(outcome.result.comments)}  pinned_comment_count: {outcome.result.pinned_count}  bayrak var mı: {outcome.result.flag_present}")
            for c in outcome.result.pinned:
                print(f"  SABİTLİ  @{c.username}: {c.text[:200]}")
        probe_dir = cfg.data_dir / "probe"
        probe_dir.mkdir(parents=True, exist_ok=True)
        rows = store.conn.execute("SELECT kind, payload FROM raw_payloads WHERE shortcode=? ORDER BY id", (code,)).fetchall()
        out = probe_dir / f"{code}.json"
        out.write_text(json.dumps([{"kind": r[0], "payload": json.loads(r[1])} for r in rows], ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"Ham JSON dökümü: {out}")
        return EXIT_OK
    finally:
        store.finish_run(run_id, f"probe {code}")


def cmd_demo(cfg: Config, url: str, no_summary: bool) -> int:
    """Oturumsuz tek post denemesi (Chrome): caption + video → transkript → analiz. Ana state'e yazmaz."""
    code = extract_shortcode(url)
    if not code:
        print(f"Shortcode çözülemedi: {url}")
        return EXIT_ERROR
    store = Store(cfg.data_dir / "demo.db")
    run_id = store.start_run("demo")
    try:
        store.upsert_from_feed(PostRecord(shortcode=code), run_id, 0)
        with IGBrowser(cfg) as b:
            t0 = time.time()
            b.goto(f"https://www.instagram.com/p/{code}/")
            b.page.wait_for_timeout(2500)
            post = refresh_details_from_page(b, store, code, [c.json for c in b.captured_since(t0) if c.json is not None])
        if not post:
            print("Sayfadan media bilgisi çıkarılamadı (post gizli/silinmiş olabilir).")
            return EXIT_ERROR
        provider = None
        if not no_summary:
            try:
                provider = make_provider(cfg)
            except ProviderUnavailable as exc:
                print(f"Analiz atlandı: {exc}")
        tmp_dir = cfg.data_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        _analyze_one(cfg, store, provider, Transcriber(cfg), None, post, tmp_dir, cfg.path("video", "dir"))
        row = store.get(code)
        print(f"@{row.get('author')}  tür={_kind_label(row)}\nTranskript: {(row.get('transcript') or '')[:800]}\nAnaliz: {row.get('summary')}\nHata: {row.get('error')}")
        return EXIT_OK
    finally:
        store.finish_run(run_id, "demo")
        store.close()


# --------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="igsaved", description="Instagram kaydedilenler → içerik listesi")
    p.add_argument("--config", help="config.json yolu (varsayılan: proje kökündeki config.json)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("ig-login", help="Instagram girişi (mobil API oturumu; şifre saklanmaz)")
    sub.add_parser("collections", help="Kaydedilen koleksiyonlarını listele")
    sy = sub.add_parser("sync", help="Kaydedilenleri tara, caption/sabitli yorum topla")
    sy.add_argument("--limit", type=int)
    sy.add_argument("--full", action="store_true", help="Artımlı durmayı kapat, listeyi sonuna kadar tara")
    sy.add_argument("--collection", action="append", help="Yalnızca bu koleksiyon(lar) (tekrarlanabilir)")
    pc = sub.add_parser("process", help="Medya indir → transkript/kareler → LLM analizi")
    pc.add_argument("--limit", type=int)
    pc.add_argument("--no-summary", action="store_true", help="LLM analizi yapma, yalnızca transkript")
    pc.add_argument("--redo", action="store_true", help="Tüm postları yeniden analiz et")
    sub.add_parser("report", help="output/saved_posts.md + .json üret")
    sub.add_parser("status", help="Durum sayaçları")
    rn = sub.add_parser("run", help="sync + process + report")
    rn.add_argument("--limit", type=int)
    rn.add_argument("--full", action="store_true")
    rn.add_argument("--no-summary", action="store_true")
    rn.add_argument("--redo", action="store_true")
    rn.add_argument("--collection", action="append")
    sub.add_parser("login", help="Alternatif kaynak: Chrome ile giriş (source=browser)")
    pr = sub.add_parser("probe", help="Tek postta sabitli yorum probu")
    pr.add_argument("url")
    dm = sub.add_parser("demo", help="Oturumsuz tek post denemesi (Chrome)")
    dm.add_argument("url")
    dm.add_argument("--no-summary", action="store_true")
    return p


def run_all(cfg: Config, store: Store, limit: int | None, full: bool, no_summary: bool,
            collections: list[str] | None = None, redo: bool = False) -> int:
    code = cmd_sync(cfg, store, limit, full, collections)
    if code == EXIT_HARDSTOP:
        log.error("Sert durma: analiz aşaması atlandı; yalnızca rapor üretiliyor.")
    elif code == EXIT_OK:
        code = cmd_process(cfg, store, limit, no_summary, redo)
    cmd_report(cfg, store)
    return code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load(args.config or default_config_path())
    setup_logging(cfg)
    limit = getattr(args, "limit", None) or None
    try:
        if args.command == "ig-login":
            return cmd_ig_login(cfg)
        if args.command == "login":
            return cmd_login(cfg)
        if args.command == "collections":
            return cmd_collections(cfg)
        if args.command == "demo":
            return cmd_demo(cfg, args.url, args.no_summary)
    except HardStop as exc:  # LoginRequired dahil
        log.error("%s", exc)
        return EXIT_HARDSTOP
    store = Store(cfg.data_dir / "probe.db") if args.command == "probe" else Store(cfg.db_path)
    try:
        if args.command == "probe":
            return cmd_probe(cfg, store, args.url)
        if args.command == "sync":
            return cmd_sync(cfg, store, limit, args.full, args.collection)
        if args.command == "process":
            return cmd_process(cfg, store, limit, args.no_summary, args.redo)
        if args.command == "report":
            return cmd_report(cfg, store)
        if args.command == "status":
            return cmd_status(store)
        if args.command == "run":
            return run_all(cfg, store, limit, args.full, args.no_summary, args.collection, args.redo)
    except HardStop as exc:  # LoginRequired dahil
        log.error("%s", exc)
        return EXIT_HARDSTOP
    finally:
        store.close()
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
