"""Post sayfasından caption/video bilgisi ve yorumlar + sabitli yorumlar (strateji zinciri).

Zincir: sayfanın kendi REST yorum yanıtı (pk eşleşmeli) → sayfa içi fetch tekrarı → GraphQL/gömülü yorum
düğümleri → DOM 'Pinned/Sabitlendi' etiketi → instagrapi (opt-in) → 'unknown'.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from .browser import HardStop, IGBrowser
from .config import Config
from .parsers import (Comment, CommentsResult, find_comments_payloads, find_media_in_json,
                      parse_comments_graphql, parse_comments_v1, parse_feed_item)
from .store import Store

log = logging.getLogger(__name__)
_IG_SESSION_WARNED = False

DOM_PINNED_JS = """
() => {
  const labels = new Set(['Pinned', 'Sabitlendi', 'Sabitlenmiş', 'Sabitlenmis', 'Sabitlenen yorum']);
  const out = [];
  const leaves = [...document.querySelectorAll('span, div')].filter(
    e => e.children.length === 0 && labels.has((e.textContent || '').trim()));
  for (const el of leaves) {
    let node = el, depth = 0, container = null;
    while (node && depth < 10) {
      if (node.tagName === 'LI') { container = node; break; }
      if (node.tagName === 'DIV' && node.querySelector('a[href^="/"]') && (node.innerText || '').length > 8) container = node;
      node = node.parentElement; depth++;
      if (container && node && node.tagName === 'UL') break;
    }
    if (!container) continue;
    const a = container.querySelector('a[href^="/"]');
    const username = a ? a.getAttribute('href').replace(/\\//g, '') : null;
    out.push({username, text: (container.innerText || '').replace(/\\s+/g, ' ').slice(0, 600)});
  }
  return out;
}
"""


@dataclass
class CommentsOutcome:
    status: str            # ok | partial | failed
    pinned_status: str     # ok | dom | unknown | failed
    source: str | None
    result: CommentsResult | None
    note: str = ""
    visited: bool = False

    def pinned_dicts(self) -> list[dict]:
        return [c.to_dict() for c in (self.result.pinned if self.result else [])]

    def comments_dicts(self) -> list[dict]:
        return [c.to_dict() for c in (self.result.comments if self.result else [])]


def _rest_url(pk: str) -> str:
    return f"https://www.instagram.com/api/v1/media/{pk}/comments/?can_support_threading=true&permalink_enabled=false"


def _is_own_comments_capture(c, pk: str | None) -> bool:
    """Yalnızca bu postun (pk) yorum uç noktasına ait yakalanan yanıtlar; başka postun yanıtı asla atfedilmez."""
    if not pk:
        return False
    return f"/api/v1/media/{pk}/comments" in c.url and isinstance(c.json, dict) and "comments" in c.json


def _better(a: CommentsResult | None, b: CommentsResult) -> CommentsResult:
    return b if a is None or len(b.comments) > len(a.comments) else a


def refresh_details_from_page(browser: IGBrowser, store: Store, shortcode: str, captured_json: list) -> dict | None:
    """Gömülü/yakalanan JSON'dan media nesnesini bulup feed'den gelmemiş alanları tamamlar.
    Video bilgisini asla geriye düşürmez: sayfada video_versions yoksa eldeki URL'ler korunur."""
    objects = browser.embedded_json() + captured_json
    media = find_media_in_json(objects, shortcode)
    if not media:
        return None
    rec = parse_feed_item(media)
    if rec is None:
        return None
    store.save_raw(shortcode, "post_media", media)
    existing = store.get(shortcode) or {}
    fields: dict = dict(
        pk=rec.pk, author=rec.author, taken_at=rec.taken_at, caption=rec.caption,
        media_type=rec.media_type, product_type=rec.product_type, comment_count=rec.comment_count,
        details_status="ok",
    )
    if rec.video_urls:
        fields["has_video"] = 1
        fields["video_urls"] = json.dumps(rec.video_urls)
        # video_attempts burada sıfırlanmaz: kalıcı 403/410 veren post MAX_ATTEMPTS sonra emekli olabilsin.
    elif rec.has_video:
        fields["has_video"] = 1
        if existing.get("has_video") and existing.get("video_urls") not in (None, "", "[]"):
            pass
        else:
            fields["details_status"] = "pending"  # video var ama URL bu sayfada gelmedi; sonraki ziyarette tekrar dene
    elif not existing.get("has_video"):
        fields["has_video"] = 0
        fields["video_urls"] = "[]"
    # Sayfadaki kısmi media nesnesi (author/caption/taken_at eksik) eldeki veriyi NULL ile ezmesin.
    store.update(shortcode, **{k: v for k, v in fields.items() if v is not None})
    return store.get(shortcode)


def refresh_post(browser: IGBrowser, store: Store, shortcode: str) -> dict | None:
    """Yalnızca caption/video URL'sini tazeler (yorum zinciri çalıştırmaz)."""
    t0 = time.time()
    browser.goto(f"https://www.instagram.com/p/{shortcode}/")
    browser.page.wait_for_timeout(2000)
    captured_json = [c.json for c in browser.captured_since(t0) if c.json is not None]
    return refresh_details_from_page(browser, store, shortcode, captured_json)


def collect_comments_api(source, store: Store, post: dict) -> CommentsOutcome:
    """Tarayıcısız: yorumlar mobil API'den (is_pinned + pinned_comment_count taşır). Yalnızca sabitliler saklanır."""
    shortcode = post["shortcode"]
    pk = str(post["pk"]) if post.get("pk") else None
    if not pk:
        return CommentsOutcome("failed", "failed", None, None, note="pk yok")
    try:
        data = source.comments_raw(pk)
    except Exception as exc:  # MediaGone vb.: post silinmiş/erişilemez
        if type(exc).__name__ == "MediaGone":
            return CommentsOutcome("failed", "failed", "instagrapi", None, note=f"post erişilemez: {exc}")
        raise
    if not isinstance(data, dict) or "comments" not in data:
        return CommentsOutcome("failed", "failed", "instagrapi", None, note=f"beklenmeyen yanıt: {str(data)[:80]}")
    store.save_raw(shortcode, "comments_instagrapi", data)
    res = parse_comments_v1(data)
    if res.count_mismatch:
        log.warning("%s: pinned_comment_count=%s ama ilk sayfada %d sabitli", shortcode, res.pinned_count, len(res.pinned))
        return CommentsOutcome("ok", "partial", "instagrapi", res,
                               note=f"pinned_comment_count={res.pinned_count}, ilk sayfada {len(res.pinned)} bulundu")
    return CommentsOutcome("ok", "ok", "instagrapi", res)


def collect_comments(browser: IGBrowser, store: Store, cfg: Config, post: dict) -> CommentsOutcome:
    """Yorumlar + sabitli yorumlar. Varsayılan: post sayfasına GİTMEZ; yorumları sayfa bağlamında tek bir
    istekle çeker (post başına ~1 XHR). Post sayfası yalnızca feed'de pk/detay eksikse ya da
    comments.visit_post_page=true ise açılır. instagrapi açıksa (tek kanıtlı pin kaynağı) önce o denenir."""
    shortcode = post["shortcode"]
    visit = bool(cfg.get("comments", "visit_post_page")) or not post.get("pk") or post.get("details_status") != "ok"
    captured = []
    captured_json: list = []
    if visit:
        t0 = time.time()
        browser.goto(f"https://www.instagram.com/p/{shortcode}/")
        browser.page.wait_for_timeout(2500)
        captured = browser.captured_since(t0)
        captured_json = [c.json for c in captured if c.json is not None]
        refreshed = refresh_details_from_page(browser, store, shortcode, captured_json)
        if refreshed:
            post = refreshed
    else:
        browser.ensure_on_instagram()
    pk = str(post["pk"]) if post.get("pk") else None
    best: CommentsResult | None = None
    best_source: str | None = None

    # 0) instagrapi (opt-in) — mobil API, is_pinned bayrağını taşıyan tek kanıtlı kaynak
    if cfg.get("instagrapi", "enabled") and pk:
        from . import ig_private
        if not ig_private.session_available(cfg):
            global _IG_SESSION_WARNED
            if not _IG_SESSION_WARNED:
                log.warning("instagrapi açık ama oturum dosyası yok; sabitli yorumlar için `run.cmd ig-login` (menü 1). Web yoluna devam.")
                _IG_SESSION_WARNED = True
        try:
            if not ig_private.session_available(cfg):
                raise ig_private.SessionMissing()
            data = ig_private.fetch_comments_raw(cfg, pk)
            if isinstance(data, dict) and "comments" in data:
                store.save_raw(shortcode, "comments_instagrapi", data)
                res = parse_comments_v1(data)
                return CommentsOutcome("ok", "ok", "instagrapi", res, visited=visit)
            log.warning("%s: instagrapi beklenmeyen yanıt: %s", shortcode, str(data)[:120])
        except ig_private.SessionMissing:
            pass
        except Exception as exc:
            log.warning("%s: instagrapi yedeği başarısız: %s", shortcode, exc)

    # 1) Sayfanın kendi REST yorum yanıtı (yalnızca ziyaret edildiyse; bu postun pk'sine ait)
    for c in captured:
        if not _is_own_comments_capture(c, pk):
            continue
        res = parse_comments_v1(c.json)
        store.save_raw(shortcode, "comments_rest_captured", c.json)
        if res.flag_present:
            return CommentsOutcome("ok", "ok", "web_rest", res, visited=visit)
        if best is None or len(res.comments) > len(best.comments):
            best, best_source = res, "web_rest"

    # 2) Yorum uç noktasını sayfa bağlamında çağır (gezinme gerekmez)
    if pk:
        try:
            data = browser.fetch_json(_rest_url(pk))
        except HardStop:
            raise
        except Exception as exc:
            log.warning("%s: yorum fetch hatası: %s", shortcode, exc)
            data = None
        if isinstance(data, dict):
            store.save_raw(shortcode, "comments_rest_replay", data)
            if "comments" in data:
                res = parse_comments_v1(data)
                if res.flag_present:
                    return CommentsOutcome("ok", "ok", "web_rest_replay", res, visited=visit)
                if best is None or len(res.comments) > len(best.comments):
                    best, best_source = res, "web_rest_replay"

    if visit:
        # 3) GraphQL / gömülü JSON yorum düğümleri
        own_json = [c.json for c in captured if c.json is not None and (
            _is_own_comments_capture(c, pk) or "/api/graphql" in c.url or "/graphql/query" in c.url)]
        for payload in find_comments_payloads(browser.embedded_json() + own_json):
            if any("comments__connection" in str(k) for k in payload.keys()):
                res = parse_comments_graphql(payload)
            else:
                res = parse_comments_v1(payload)
            if res is None or not res.comments:
                continue
            store.save_raw(shortcode, "comments_graphql", payload)
            if res.flag_present:
                return CommentsOutcome("ok", "ok", "graphql", res, visited=visit)
            if best is None or len(res.comments) > len(best.comments):
                best, best_source = res, "graphql"

        # 4) DOM etiketi
        try:
            dom_pinned = browser.page.evaluate(DOM_PINNED_JS) or []
        except Exception as exc:
            log.debug("DOM pinned taraması başarısız: %s", exc)
            dom_pinned = []
        if dom_pinned:
            pinned = [Comment(id=None, username=d.get("username"), text=d.get("text") or "", is_pinned=True) for d in dom_pinned]
            comments = best.comments if best else []
            res = CommentsResult(comments=comments, pinned=pinned, pinned_count=None, flag_present=False)
            return CommentsOutcome("ok" if comments else "partial", "dom", "dom", res, note="DOM etiketinden", visited=visit)

    if best is not None and best.comments:
        return CommentsOutcome("ok", "unknown", best_source, best,
                               note="Yorumlar alındı; web yanıtı sabitleme bilgisi taşımıyor (instagrapi yedeği gerekir)", visited=visit)
    return CommentsOutcome("failed", "failed", None, None, note="Yorum verisi alınamadı", visited=visit)
