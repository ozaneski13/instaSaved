"""Markdown + JSON rapor üretimi."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .store import Store, loads_dict, loads_list
from .summarize import NO_SPEECH_TEXT, NO_VIDEO_TEXT  # noqa: F401

LABELS = {
    "tr": {
        "title": "Instagram Kaydedilenler — İçerik Listesi", "updated": "Güncelleme", "total": "Toplam {n} post",
        "open": "Postu aç", "collection": "Koleksiyon", "content": "İçerik", "caption": "Açıklama", "pinned": "Sabitli yorumlar",
        "location": "Konum", "map": "haritada aç", "stale": "_(yeniden analiz bekliyor)_",
        "no_pinned": "Sabitli yorum yok", "no_user": "(kullanıcı yok)", "dom_note": "_(sayfa etiketinden okundu)_",
        "partial_note": "_(sabitli sayısı {count}, ilk sayfada {found} bulundu)_",
        "unknown": "Belirlenemedi (web yanıtı sabitleme bilgisi vermiyor; instagrapi yedeği için SABAH-NOTU adım 2)",
        "failed": "Belirlenemedi (yorumlar alınamadı)", "pending": "Henüz alınmadı",
        "awaiting": "(analiz bekliyor) Transkript: ", "not_processed": "Henüz işlenmedi", "error": "İşlenemedi: ",
        "unknown_error": "bilinmeyen hata", "video": "video", "photo": "fotoğraf", "carousel": "karusel", "post": "gönderi",
        "no_speech": "konuşma yok", "lang": "dil: {lang}", "words": "{n} kelime transkript", "images": "{n} görsel incelendi",
    },
    "en": {
        "title": "Instagram Saved Posts — Content Digest", "updated": "Updated", "total": "{n} posts",
        "open": "Open post", "collection": "Collection", "content": "Content", "caption": "Caption", "pinned": "Pinned comments",
        "location": "Location", "map": "open in maps", "stale": "_(awaiting re-analysis)_",
        "no_pinned": "No pinned comments", "no_user": "(no user)", "dom_note": "_(read from the page label)_",
        "partial_note": "_(pinned count {count}, {found} found on the first page)_",
        "unknown": "Undetermined (the web response carries no pin information; use the instagrapi source)",
        "failed": "Undetermined (comments could not be fetched)", "pending": "Not fetched yet",
        "awaiting": "(awaiting analysis) Transcript: ", "not_processed": "Not processed yet", "error": "Failed: ",
        "unknown_error": "unknown error", "video": "video", "photo": "photo", "carousel": "carousel", "post": "post",
        "no_speech": "no speech", "lang": "language: {lang}", "words": "{n}-word transcript", "images": "{n} images inspected",
    },
}


def _labels(lang: str | None) -> dict:
    return LABELS.get((lang or "tr").lower()[:2], LABELS["tr"])


def _fmt_date(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone().strftime("%Y-%m-%d")


def _quote_block(text: str, indent: str = "     ") -> str:
    lines = [ln.rstrip() for ln in text.strip().splitlines()] or [""]
    return "\n".join(f"{indent}> {ln}" if ln else f"{indent}>" for ln in lines)


def _kind_label(post: dict, L: dict) -> str:
    mt = post.get("media_type")
    if mt == 8:
        return L["carousel"]
    if mt == 1:
        return L["photo"]
    if mt == 2 or post.get("has_video"):
        return L["video"]
    return L["post"]


def content_suffix(post: dict, lang: str = "tr") -> str:
    L = _labels(lang)
    try:
        meta = json.loads(post.get("analysis_meta") or "{}")
    except json.JSONDecodeError:
        meta = {}
    parts = [_kind_label(post, L)]
    if post.get("has_video"):
        if post.get("no_speech"):
            parts.append(L["no_speech"])
        else:
            if post.get("language"):
                parts.append(L["lang"].format(lang=post["language"]))
            if post.get("transcript"):
                parts.append(L["words"].format(n=len(post["transcript"].split())))
    n_img = int(meta.get("images", 0) or 0) + int(meta.get("frames", 0) or 0)
    if n_img:
        parts.append(L["images"].format(n=n_img))
    return f" _({', '.join(parts)})_"


def video_line(post: dict, lang: str = "tr") -> str:
    """'İçerik' satırı: fotoğraf/karusel/video fark etmeksizin analiz özeti."""
    L = _labels(lang)
    summary = (post.get("summary") or "").strip()
    if summary:
        # Durum 'ok' olmasa bile eldeki özet gösterilir (yeniden analiz kuyruğa alındıysa eski metin kaybolmasın).
        stale = "" if post.get("summary_status") == "ok" else f" {L['stale']}"
        return f"{summary}{content_suffix(post, lang)}{stale}"
    if post.get("has_video") and post.get("transcript_status") == "ok" and post.get("transcript"):
        return f"{L['awaiting']}{post['transcript'][:600]}"
    if post.get("summary_status") == "failed" or post.get("video_status") == "failed" or post.get("transcript_status") == "failed":
        return f"{L['error']}{post.get('error') or L['unknown_error']}"
    return L["not_processed"]


def pinned_lines(post: dict, lang: str = "tr") -> list[str]:
    L = _labels(lang)
    status = post.get("pinned_status") or "pending"
    pinned = loads_list(post.get("pinned_json"))
    if status in ("ok", "dom", "partial"):
        if not pinned:
            return [L["no_pinned"]]
        out = []
        for c in pinned:
            user = f"@{c.get('username')}" if c.get("username") else L["no_user"]
            text = (c.get("text") or "").replace("\n", " ").strip()
            out.append(f"{user}: {text}")
        if status == "dom":
            out.append(L["dom_note"])
        if status == "partial":
            try:
                meta = json.loads(post.get("analysis_meta") or "{}")
            except json.JSONDecodeError:
                meta = {}
            out.append(L["partial_note"].format(count=post.get("comment_count_pinned") or meta.get("pinned_count") or "?", found=len(pinned)))
        return out
    return [L.get(status, status)]


def location_line(post: dict, L: dict) -> str | None:
    """Gönderiye eklenen konum etiketi; varsa haritaya bağlantı verilir."""
    loc = loads_dict(post.get("location"))
    if not loc or not loc.get("name"):
        return None
    text = loc["name"]
    extra = [v for v in (loc.get("address"), loc.get("city")) if v]
    if extra:
        text += " — " + ", ".join(extra)
    lat, lng = loc.get("lat"), loc.get("lng")
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        text += f" ([{L['map']}](https://www.google.com/maps/search/?api=1&query={lat},{lng}))"
    return text


def render_markdown(posts: list[dict], generated_at: datetime | None = None, lang: str = "tr") -> str:
    L = _labels(lang)
    generated_at = generated_at or datetime.now()
    lines = [
        f"# {L['title']}",
        "",
        f"{L['updated']}: {generated_at.strftime('%Y-%m-%d %H:%M')} · {L['total'].format(n=len(posts))}",
        "",
    ]
    for i, post in enumerate(posts, 1):
        author = f"@{post['author']}" if post.get("author") else "(?)"
        date = _fmt_date(post.get("taken_at"))
        head = f"{i}. **{author}** · [{L['open']}](https://www.instagram.com/p/{post['shortcode']}/)"
        if date:
            head += f" · {date}"
        lines.append(head)
        cols = loads_list(post.get("collections"))
        if cols:
            lines.append(f"   - **{L['collection']}:** {', '.join(cols)}")
        loc_line = location_line(post, L)
        if loc_line:
            lines.append(f"   - **{L['location']}:** {loc_line}")
        lines.append(f"   - **{L['content']}:** {video_line(post, lang)}")
        caption = (post.get("caption") or "").strip()
        if caption:
            lines.append(f"   - **{L['caption']}:**")
            lines.append(_quote_block(caption))
        else:
            lines.append(f"   - **{L['caption']}:** —")
        pl = pinned_lines(post, lang)
        if len(pl) == 1 and not pl[0].startswith("@"):
            lines.append(f"   - **{L['pinned']}:** {pl[0]}")
        else:
            lines.append(f"   - **{L['pinned']}:**")
            for item in pl:
                lines.append(f"     - {item}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_json(posts: list[dict], lang: str = "tr") -> list[dict]:
    out = []
    for post in posts:
        out.append({
            "shortcode": post["shortcode"],
            "url": f"https://www.instagram.com/p/{post['shortcode']}/",
            "author": post.get("author"),
            "taken_at": post.get("taken_at"),
            "caption": post.get("caption"),
            "has_video": bool(post.get("has_video")),
            "media_type": post.get("media_type"),
            "collections": loads_list(post.get("collections")),
            "location": loads_dict(post.get("location")),
            "content_summary": post.get("summary"),
            "content_line": video_line(post, lang),
            "transcript": post.get("transcript"),
            "language": post.get("language"),
            "no_speech": bool(post.get("no_speech")) if post.get("no_speech") is not None else None,
            "pinned_status": post.get("pinned_status"),
            "pinned_comments": loads_list(post.get("pinned_json")),
            "comments_source": post.get("comments_source"),
            "comment_count": post.get("comment_count"),
            "statuses": {k: post.get(k) for k in ("details_status", "comments_status", "video_status", "transcript_status", "summary_status")},
        })
    return out


def write_reports(store: Store, output_dir: Path, lang: str = "tr") -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    posts = store.all_posts()
    md_path = output_dir / "saved_posts.md"
    json_path = output_dir / "saved_posts.json"
    md_path.write_text(render_markdown(posts, lang=lang), encoding="utf-8")
    json_path.write_text(json.dumps(render_json(posts, lang), ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path
