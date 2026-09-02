"""Saf ayrıştırma fonksiyonları. Instagram'a hiç dokunmaz; test edilebilir.

Şema notları (araştırma 2026-09-02):
- Kaydedilenler akışı: GET /api/v1/feed/saved/posts/ → {"items": [{"media": {...}}], "more_available", "next_max_id"}
- Yorumlar: GET /api/v1/media/{pk}/comments/ → {"comments": [...], "pinned_comment_count": N, ...}
  Sabitli yorumda üst seviyede "is_pinned": true; sabitli DEĞİLSE anahtar hiç yok → bool(c.get("is_pinned")).
- Tuzak alanlar (sinyal değil): hoisted_comments, is_ranked_comment, comment_index, pinned_for_users,
  visual_comment_reply_sticker_info.is_pinned (int), Post.is_pinned (profil sabitlemesi).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

SHORTCODE_RE = re.compile(r"instagram\.com/(?:[^/?#]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")
MEDIA_TYPE_IMAGE = 1
MEDIA_TYPE_VIDEO = 2
MEDIA_TYPE_ALBUM = 8
MOJIBAKE_MARKERS = ("Ã", "Å", "Ä", "Ä°", "Ã¶", "Ã¼", "ÅŸ", "Ä±", "â€")


def extract_shortcode(url_or_code: str) -> str | None:
    if not url_or_code:
        return None
    m = SHORTCODE_RE.search(url_or_code)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{5,}", url_or_code.strip("/")):
        return url_or_code.strip("/")
    return None


def fix_mojibake(text: str | None) -> str | None:
    """latin-1 → utf-8 onarımı; yalnızca bozukluk işaretleri varsa uygulanır."""
    if not text or not any(marker in text for marker in MOJIBAKE_MARKERS):
        return text
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    return repaired


@dataclass
class PostRecord:
    shortcode: str
    pk: str | None = None
    author: str | None = None
    taken_at: int | None = None
    caption: str | None = None
    media_type: int | None = None
    product_type: str | None = None
    has_video: bool = False
    video_urls: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    saved_collection_ids: list[str] = field(default_factory=list)
    comment_count: int | None = None
    like_count: int | None = None

    @property
    def url(self) -> str:
        return f"https://www.instagram.com/p/{self.shortcode}/"


def _unwrap_media(item: dict) -> dict:
    if isinstance(item, dict) and isinstance(item.get("media"), dict):
        return item["media"]
    return item


def pick_video_url(media: dict) -> str | None:
    versions = media.get("video_versions") or []
    best = None
    best_area = -1
    for v in versions:
        if not isinstance(v, dict) or not v.get("url"):
            continue
        area = int(v.get("width") or 0) * int(v.get("height") or 0)
        if area > best_area:
            best, best_area = v["url"], area
    return best


def collect_video_urls(media: dict) -> list[str]:
    if media.get("media_type") == MEDIA_TYPE_ALBUM:
        urls = []
        for child in media.get("carousel_media") or []:
            u = pick_video_url(child)
            if u:
                urls.append(u)
        return urls
    u = pick_video_url(media)
    return [u] if u else []


def pick_image_url(media: dict) -> str | None:
    cands = ((media.get("image_versions2") or {}).get("candidates")) or []
    best, best_area = None, -1
    for c in cands:
        if not isinstance(c, dict) or not c.get("url"):
            continue
        area = int(c.get("width") or 0) * int(c.get("height") or 0)
        if area > best_area:
            best, best_area = c["url"], area
    return best


def collect_image_urls(media: dict) -> list[str]:
    """Fotoğraf → 1 görsel; karusel → çocuk fotoğraflar (video çocuklar video_urls'e gider); video → yok."""
    if media.get("media_type") == MEDIA_TYPE_ALBUM:
        out = []
        for child in media.get("carousel_media") or []:
            if child.get("media_type") == MEDIA_TYPE_VIDEO or child.get("video_versions"):
                continue
            u = pick_image_url(child)
            if u:
                out.append(u)
        return out
    if media.get("media_type") == MEDIA_TYPE_VIDEO or media.get("video_versions"):
        return []
    u = pick_image_url(media)
    return [u] if u else []


def parse_feed_item(item: dict) -> PostRecord | None:
    media = _unwrap_media(item)
    code = media.get("code")
    if not code:
        return None
    caption_obj = media.get("caption")
    caption = None
    if isinstance(caption_obj, dict):
        caption = caption_obj.get("text")
    elif isinstance(caption_obj, str):
        caption = caption_obj
    user = media.get("user") or media.get("owner") or {}
    video_urls = collect_video_urls(media)
    return PostRecord(
        shortcode=code,
        pk=str(media["pk"]) if media.get("pk") is not None else (str(media["id"]).split("_")[0] if media.get("id") else None),
        author=user.get("username") if isinstance(user, dict) else None,
        taken_at=media.get("taken_at"),
        caption=fix_mojibake(caption),
        media_type=media.get("media_type"),
        product_type=media.get("product_type"),
        # video_versions liste görünümünde bazen gelmez; media_type VIDEO diyorsa video vardır (URL sonra tazelenir)
        has_video=bool(video_urls) or media.get("media_type") == MEDIA_TYPE_VIDEO,
        video_urls=video_urls,
        image_urls=collect_image_urls(media),
        saved_collection_ids=[str(x) for x in (media.get("saved_collection_ids") or [])],
        comment_count=media.get("comment_count"),
        like_count=media.get("like_count"),
    )


def parse_saved_feed(data: dict) -> tuple[list[PostRecord], bool, str | None]:
    records = []
    for item in data.get("items") or []:
        rec = parse_feed_item(item)
        if rec:
            records.append(rec)
    return records, bool(data.get("more_available")), data.get("next_max_id")


@dataclass
class Comment:
    id: str | None
    username: str | None
    text: str
    created_at: int | None = None
    like_count: int | None = None
    is_pinned: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "text": self.text,
            "created_at": self.created_at,
            "like_count": self.like_count,
            "is_pinned": self.is_pinned,
        }


@dataclass
class CommentsResult:
    comments: list[Comment]
    pinned: list[Comment]
    pinned_count: int | None
    flag_present: bool
    has_more: bool = False
    next_min_id: str | None = None
    comment_count: int | None = None

    @property
    def count_mismatch(self) -> bool:
        return self.pinned_count is not None and self.pinned_count != len(self.pinned)


def _comment_from_v1(c: dict) -> Comment:
    user = c.get("user") or {}
    return Comment(
        id=str(c.get("pk")) if c.get("pk") is not None else None,
        username=user.get("username") if isinstance(user, dict) else None,
        text=fix_mojibake(c.get("text") or "") or "",
        created_at=c.get("created_at"),
        like_count=c.get("comment_like_count"),
        is_pinned=bool(c.get("is_pinned")),
    )


def parse_comments_v1(data: dict) -> CommentsResult:
    """GET /api/v1/media/{pk}/comments/ yanıtı. Yalnızca üst seviye (parent) yorumlar."""
    raw_comments = data.get("comments") or []
    comments = [_comment_from_v1(c) for c in raw_comments if isinstance(c, dict)]
    flag_present = "pinned_comment_count" in data or any(
        isinstance(c, dict) and "is_pinned" in c for c in raw_comments
    )
    pinned_count = data.get("pinned_comment_count")
    pinned = [c for c in comments if c.is_pinned]
    return CommentsResult(
        comments=comments,
        pinned=pinned,
        pinned_count=pinned_count if isinstance(pinned_count, int) else None,
        flag_present=flag_present,
        has_more=bool(data.get("has_more_headload_comments") or data.get("has_more_comments")),
        next_min_id=data.get("next_min_id"),
        comment_count=data.get("comment_count") if isinstance(data.get("comment_count"), int) else None,
    )


def parse_comments_graphql(data: dict) -> CommentsResult | None:
    """xdt_api__v1__media__media_id__comments__connection şekli; pin alanı varsa okunur."""
    conn = None
    node_data = data.get("data") if isinstance(data.get("data"), dict) else data
    for key, value in (node_data or {}).items():
        if "comments__connection" in key and isinstance(value, dict):
            conn = value
            break
    if conn is None:
        return None
    nodes = [e.get("node") for e in conn.get("edges") or [] if isinstance(e, dict)]
    nodes = [n for n in nodes if isinstance(n, dict)]
    comments = [_comment_from_v1(n) for n in nodes]
    flag_present = any("is_pinned" in n for n in nodes) or "pinned_comment_count" in conn
    pinned = [c for c in comments if c.is_pinned]
    page = conn.get("page_info") or {}
    return CommentsResult(
        comments=comments,
        pinned=pinned,
        pinned_count=conn.get("pinned_comment_count") if isinstance(conn.get("pinned_comment_count"), int) else None,
        flag_present=flag_present,
        has_more=bool(page.get("has_next_page")),
        next_min_id=page.get("end_cursor"),
    )


def iter_dicts(obj: Any) -> Iterator[dict]:
    """JSON ağacındaki tüm dict düğümlerini derinlik-öncelikli gezer."""
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def find_media_in_json(objects: Iterable[Any], shortcode: str) -> dict | None:
    """Gömülü JSON bloklarında shortcode'a ait media nesnesini bulur (anahtar adına bağlı değil)."""
    fallback = None
    for obj in objects:
        for d in iter_dicts(obj):
            if d.get("code") != shortcode:
                continue
            if any(k in d for k in ("video_versions", "image_versions2", "caption", "carousel_media")):
                if d.get("pk") is not None or d.get("id"):
                    return d
                fallback = fallback or d
    return fallback


COMMENTS_RESPONSE_KEYS = ("pinned_comment_count", "has_more_headload_comments", "has_more_comments",
                          "next_min_id", "comment_likes_enabled", "caption_is_edited")


def find_comments_payloads(objects: Iterable[Any]) -> list[dict]:
    """Gömülü/yakalanan JSON'larda yorum UÇ NOKTASI yanıtı taşıyan nesneleri bulur (v1 veya GraphQL şekli).
    Media nesnelerinin içindeki önizleme yorum listeleri (başka postlara ait olabilir) sayılmaz."""
    found = []
    for obj in objects:
        for d in iter_dicts(obj):
            comments = d.get("comments")
            if (isinstance(comments, list) and comments and isinstance(comments[0], dict) and "text" in comments[0]
                    and any(k in d for k in COMMENTS_RESPONSE_KEYS)):
                found.append(d)
            elif any("comments__connection" in k for k in d.keys() if isinstance(k, str)):
                found.append(d)
    return found


def parse_export_saved_posts(data: dict) -> list[dict]:
    """Resmî 'Bilgilerini indir' → your_instagram_activity/saved/saved_posts.json.
    'Saved on' anahtarı yerelleştirilmiş olabilir → href taşıyan ilk string_map_data girdisi alınır."""
    out = []
    for entry in data.get("saved_saved_media") or []:
        smd = entry.get("string_map_data") or {}
        href, ts = None, None
        for value in smd.values():
            if isinstance(value, dict) and value.get("href"):
                href, ts = value.get("href"), value.get("timestamp")
                break
        code = extract_shortcode(href or "")
        if code:
            out.append({"shortcode": code, "href": href, "timestamp": ts, "author": entry.get("title")})
    return out
