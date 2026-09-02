"""Markdown + JSON rapor üretimi."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .store import Store, loads_list
from .summarize import NO_SPEECH_TEXT, NO_VIDEO_TEXT  # noqa: F401

PINNED_LABELS = {
    "ok": None,
    "dom": None,
    "unknown": "Belirlenemedi (web yanıtı sabitleme bilgisi vermiyor; instagrapi yedeği için SABAH-NOTU adım 2)",
    "failed": "Belirlenemedi (yorumlar alınamadı)",
    "pending": "Henüz alınmadı",
}


def _fmt_date(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone().strftime("%Y-%m-%d")


def _quote_block(text: str, indent: str = "     ") -> str:
    lines = [ln.rstrip() for ln in text.strip().splitlines()] or [""]
    return "\n".join(f"{indent}> {ln}" if ln else f"{indent}>" for ln in lines)


def _kind_label(post: dict) -> str:
    mt = post.get("media_type")
    if mt == 8:
        return "karusel"
    if mt == 1:
        return "fotoğraf"
    if mt == 2 or post.get("has_video"):
        return "video"
    return "gönderi"


def content_suffix(post: dict) -> str:
    meta = {}
    try:
        meta = json.loads(post.get("analysis_meta") or "{}")
    except json.JSONDecodeError:
        meta = {}
    parts = [_kind_label(post)]
    if post.get("has_video"):
        if post.get("no_speech"):
            parts.append("konuşma yok")
        else:
            if post.get("language"):
                parts.append(f"dil: {post['language']}")
            if post.get("transcript"):
                parts.append(f"{len(post['transcript'].split())} kelime transkript")
    n_img = int(meta.get("images", 0) or 0) + int(meta.get("frames", 0) or 0)
    if n_img:
        parts.append(f"{n_img} görsel incelendi")
    return f" _({', '.join(parts)})_"


def video_line(post: dict) -> str:
    """'İçerik' satırı: fotoğraf/karusel/video fark etmeksizin analiz özeti."""
    if post.get("summary_status") == "ok" and post.get("summary"):
        return f"{post['summary'].strip()}{content_suffix(post)}"
    if post.get("has_video") and post.get("transcript_status") == "ok" and post.get("transcript"):
        return f"(analiz bekliyor) Transkript: {post['transcript'][:600]}"
    if post.get("summary_status") == "failed" or post.get("video_status") == "failed" or post.get("transcript_status") == "failed":
        return f"İşlenemedi: {post.get('error') or 'bilinmeyen hata'}"
    return "Henüz işlenmedi"


def pinned_lines(post: dict) -> list[str]:
    status = post.get("pinned_status") or "pending"
    pinned = loads_list(post.get("pinned_json"))
    if status in ("ok", "dom"):
        if not pinned:
            return ["Sabitli yorum yok"]
        out = []
        for c in pinned:
            user = f"@{c.get('username')}" if c.get("username") else "(kullanıcı yok)"
            text = (c.get("text") or "").replace("\n", " ").strip()
            out.append(f"{user}: {text}")
        if status == "dom":
            out.append("_(sayfa etiketinden okundu)_")
        return out
    return [PINNED_LABELS.get(status, status)]


def render_markdown(posts: list[dict], generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now()
    lines = [
        "# Instagram Kaydedilenler — İçerik Listesi",
        "",
        f"Güncelleme: {generated_at.strftime('%Y-%m-%d %H:%M')} · Toplam {len(posts)} post",
        "",
    ]
    for i, post in enumerate(posts, 1):
        author = f"@{post['author']}" if post.get("author") else "(hesap bilinmiyor)"
        date = _fmt_date(post.get("taken_at"))
        head = f"{i}. **{author}** · [Postu aç](https://www.instagram.com/p/{post['shortcode']}/)"
        if date:
            head += f" · {date}"
        lines.append(head)
        cols = loads_list(post.get("collections"))
        if cols:
            lines.append(f"   - **Koleksiyon:** {', '.join(cols)}")
        lines.append(f"   - **İçerik:** {video_line(post)}")
        caption = (post.get("caption") or "").strip()
        if caption:
            lines.append("   - **Açıklama:**")
            lines.append(_quote_block(caption))
        else:
            lines.append("   - **Açıklama:** —")
        pl = pinned_lines(post)
        if len(pl) == 1 and not pl[0].startswith("@"):
            lines.append(f"   - **Sabitli yorumlar:** {pl[0]}")
        else:
            lines.append("   - **Sabitli yorumlar:**")
            for item in pl:
                lines.append(f"     - {item}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_json(posts: list[dict]) -> list[dict]:
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
            "content_summary": post.get("summary"),
            "content_line": video_line(post),
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


def write_reports(store: Store, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    posts = store.all_posts()
    md_path = output_dir / "saved_posts.md"
    json_path = output_dir / "saved_posts.json"
    md_path.write_text(render_markdown(posts), encoding="utf-8")
    json_path.write_text(json.dumps(render_json(posts), ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path
