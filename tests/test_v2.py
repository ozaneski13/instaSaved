"""v2: tarayıcısız kaynak, görsel analiz, koleksiyonlar."""
import json
from pathlib import Path

import pytest

from igsaved import cli
from igsaved.comments import collect_comments_api
from igsaved.config import Config
from igsaved.ig_source import CollectionInfo
from igsaved.parsers import PostRecord, collect_image_urls, parse_comments_v1, parse_feed_item
from igsaved.report import render_json, render_markdown
from igsaved.saved_feed import enumerate_saved_api
from igsaved.store import Store
from igsaved.summarize import analyze, build_user_prompt


def _photo(code="PH1", pk=1):
    return {"pk": pk, "code": code, "media_type": 1, "caption": {"text": "foto"}, "user": {"username": "u"},
            "image_versions2": {"candidates": [{"width": 640, "height": 800, "url": "https://cdn/s.jpg"},
                                               {"width": 1080, "height": 1350, "url": "https://cdn/l.jpg"}]}}


def test_image_urls_photo_and_carousel():
    rec = parse_feed_item({"media": _photo()})
    assert rec.image_urls == ["https://cdn/l.jpg"] and rec.has_video is False
    album = {"pk": 2, "code": "AL", "media_type": 8, "carousel_media": [
        _photo("c1", 11), {"media_type": 2, "video_versions": [{"width": 1, "height": 1, "url": "https://cdn/v.mp4"}]}, _photo("c2", 12)]}
    rec = parse_feed_item(album)
    assert rec.image_urls == ["https://cdn/l.jpg", "https://cdn/l.jpg"] and rec.video_urls == ["https://cdn/v.mp4"] and rec.has_video
    assert collect_image_urls({"media_type": 2, "video_versions": [], "image_versions2": {"candidates": [{"url": "x", "width": 1, "height": 1}]}}) == []


def test_comment_count_parsed():
    res = parse_comments_v1({"comments": [], "pinned_comment_count": 0, "comment_count": 42})
    assert res.comment_count == 42 and res.flag_present


class FakeSource:
    def __init__(self, pages, comments=None, info=None):
        self.pages = pages
        self.comments = comments or {}
        self.info = info or {}
        self.calls = []

    def collections(self):
        return [CollectionInfo("ALL_MEDIA_AUTO_COLLECTION", "All posts", "ALL_MEDIA_AUTO_COLLECTION", 5),
                CollectionInfo("123", "Japonya", "MEDIA", 2)]

    def resolve_collections(self, names):
        out = []
        for n in names:
            m = next((c for c in self.collections() if c.name.lower() == n.lower()), None)
            if m is None:
                raise ValueError(n)
            out.append(m)
        return out

    def iter_saved_pages(self, collection_id=None):
        self.calls.append(("pages", collection_id))
        yield from self.pages.get(collection_id, [])

    def comments_raw(self, pk):
        self.calls.append(("comments", pk))
        return self.comments.get(pk, {"comments": [], "pinned_comment_count": 0})

    def media_info_raw(self, pk):
        return self.info.get(pk)


def _page(codes, more=False):
    return {"items": [{"media": {"pk": i + 1, "code": c, "media_type": 1, "caption": {"text": c}, "user": {"username": "u"},
                                 "image_versions2": {"candidates": [{"width": 1, "height": 1, "url": f"https://cdn/{c}.jpg"}]}}}
                      for i, c in enumerate(codes)],
            "more_available": more, "next_max_id": "n" if more else None}


def test_enumerate_saved_api_all_and_collection(tmp_path):
    cfg = Config.load(None)
    s = Store(tmp_path / "s.db")
    run = s.start_run("t")
    src = FakeSource(pages={None: [_page(["A", "B"], True), _page(["C"])], "123": [_page(["B"])]})
    stats = enumerate_saved_api(src, s, cfg, run)
    assert stats["new"] == 3 and stats["pages"] == 2 and stats["stopped_by"] == "end_of_feed"
    assert s.known_shortcodes() == {"A", "B", "C"}
    stats = enumerate_saved_api(src, s, cfg, run, collections=src.resolve_collections(["japonya"]))
    assert stats["new"] == 0 and stats["collections"] == ["Japonya"]
    assert json.loads(s.get("B")["collections"]) == ["Japonya"]
    assert s.get("A")["collections"] is None


def test_enumerate_saved_api_incremental_stop(tmp_path):
    cfg = Config.load(None)
    cfg.raw["pacing"]["known_pages_to_stop"] = 1
    s = Store(tmp_path / "s.db")
    run = s.start_run("t")
    src = FakeSource(pages={None: [_page(["A"], True), _page(["B"], True), _page(["C"])]})
    enumerate_saved_api(src, s, cfg, run)
    src2 = FakeSource(pages={None: [_page(["A"], True), _page(["B"], True), _page(["C"])]})
    stats = enumerate_saved_api(src2, s, cfg, run + 1)
    assert stats["stopped_by"] == "all_known" and stats["pages"] == 1
    stats = enumerate_saved_api(src2, s, cfg, run + 1, limit_new=1)
    assert stats["new"] == 0


def test_collect_comments_api_pinned_only(tmp_path):
    s = Store(tmp_path / "s.db")
    run = s.start_run("t")
    s.upsert_from_feed(PostRecord(shortcode="X", pk="7"), run, 1)
    src = FakeSource(pages={}, comments={"7": {"comments": [
        {"pk": 1, "text": "sabit", "user": {"username": "owner"}, "is_pinned": True},
        {"pk": 2, "text": "normal", "user": {"username": "v"}}], "pinned_comment_count": 1, "comment_count": 9}})
    out = collect_comments_api(src, s, s.get("X"))
    assert out.status == "ok" and out.pinned_status == "ok" and [c.text for c in out.result.pinned] == ["sabit"]
    cli._store_comments_outcome(s, "X", out)
    row = s.get("X")
    assert json.loads(row["pinned_json"])[0]["text"] == "sabit" and row["comments_json"] is None and row["comment_count"] == 9
    assert collect_comments_api(src, s, {"shortcode": "Y"}).status == "failed"


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.calls = []

    def complete(self, system, user, images=()):
        self.calls.append((system, user, list(images)))
        return "Analiz."


def test_analyze_passes_images_and_kind(tmp_path):
    p = FakeProvider()
    imgs = [tmp_path / "a.jpg", tmp_path / "b.jpg"]
    out = analyze(p, "karusel (birden çok görsel/video)", "u", "cap", "", None, images=imgs, note="2 fotoğraf")
    assert out == "Analiz."
    system, user, images = p.calls[0]
    assert images == imgs and "Gönderi türü: karusel" in user and "Ekli görsel sayısı: 2 (2 fotoğraf)" in user
    assert "transkript" not in user.lower()
    assert "Konuşma transkripti" in build_user_prompt("video", None, None, "abc", "tr")


def test_prepare_image_and_frames_roundtrip(tmp_path):
    from PIL import Image
    from igsaved import media
    src = tmp_path / "big.png"
    Image.new("RGB", (2000, 1000), (200, 30, 30)).save(src)
    out = media.prepare_image(src, tmp_path / "small.jpg", max_edge=400)
    with Image.open(out) as im:
        assert max(im.size) == 400 and im.format == "JPEG"
    assert not src.exists()


def test_report_content_line_and_collections():
    post = {"shortcode": "C1", "author": "u", "media_type": 8, "has_video": 1, "no_speech": 0, "language": "en",
            "transcript": "one two three", "summary": "Özet metni.", "summary_status": "ok",
            "analysis_meta": json.dumps({"frames": 2, "images": 3}), "collections": json.dumps(["Japonya"]),
            "pinned_status": "ok", "pinned_json": "[]", "details_status": "ok", "comments_status": "ok",
            "video_status": "ok", "transcript_status": "ok", "comment_count": 1}
    md = render_markdown([post])
    assert "- **Koleksiyon:** Japonya" in md
    assert "- **İçerik:** Özet metni. _(karusel, dil: en, 3 kelime transkript, 5 görsel incelendi)_" in md
    photo = dict(post, media_type=1, has_video=0, transcript=None, language=None, analysis_meta=json.dumps({"images": 1}), collections=None)
    md2 = render_markdown([photo])
    assert "_(fotoğraf, 1 görsel incelendi)_" in md2 and "Koleksiyon" not in md2
    js = render_json([post])[0]
    assert js["collections"] == ["Japonya"] and js["content_summary"] == "Özet metni." and js["media_type"] == 8


def test_run_all_redo_and_collections_flow(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_sync", lambda cfg, store, limit, full, cols=None: calls.append(("sync", cols)) or cli.EXIT_OK)
    monkeypatch.setattr(cli, "cmd_process", lambda cfg, store, limit, ns, redo=False: calls.append(("process", redo)) or cli.EXIT_OK)
    monkeypatch.setattr(cli, "cmd_report", lambda cfg, store: calls.append(("report", None)))
    assert cli.run_all(None, None, None, False, False, ["Japonya"], True) == cli.EXIT_OK
    assert calls == [("sync", ["Japonya"]), ("process", True), ("report", None)]


def test_store_migration_adds_v2_columns(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE posts (shortcode TEXT PRIMARY KEY, pk TEXT, first_seen_run INTEGER, feed_position INTEGER, "
                 "comments_status TEXT DEFAULT 'pending', transcript_status TEXT DEFAULT 'pending', has_video INTEGER DEFAULT 0, "
                 "details_status TEXT DEFAULT 'pending', updated_at INTEGER)")
    conn.commit(); conn.close()
    s = Store(db)
    cols = {row[1] for row in s.conn.execute("PRAGMA table_info(posts)")}
    assert {"image_urls", "collections", "analysis_meta", "video_attempts"} <= cols
