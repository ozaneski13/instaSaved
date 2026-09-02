import json
from datetime import datetime

from igsaved.parsers import PostRecord
from igsaved.report import render_json, render_markdown
from igsaved.store import Store, loads_list


def _store(tmp_path):
    return Store(tmp_path / "state.db")


def test_upsert_new_then_update_keeps_progress(tmp_path):
    s = _store(tmp_path)
    run = s.start_run("test")
    rec = PostRecord(shortcode="A", pk="1", author="x", caption="c", has_video=True, video_urls=["u"], media_type=2)
    assert s.upsert_from_feed(rec, run, 1) is True
    s.update("A", comments_status="ok", pinned_status="ok", pinned_json="[]")
    assert s.upsert_from_feed(PostRecord(shortcode="A", pk="1", caption="c2"), run, 1) is False
    row = s.get("A")
    assert row["caption"] == "c2" and row["comments_status"] == "ok" and row["video_urls"] == json.dumps(["u"])
    assert s.known_shortcodes() == {"A"}


def test_pending_and_counts(tmp_path):
    s = _store(tmp_path)
    run = s.start_run("test")
    s.upsert_from_feed(PostRecord(shortcode="A", pk="1", has_video=True, video_urls=["u"]), run, 1)
    s.upsert_from_feed(PostRecord(shortcode="B", pk="2"), run, 2)
    assert [p["shortcode"] for p in s.pending("comments_status")] == ["A", "B"]
    s.update("A", comments_status="ok")
    assert [p["shortcode"] for p in s.pending("comments_status")] == ["B"]
    assert [p["shortcode"] for p in s.pending("transcript_status", where_extra="AND has_video=1")] == ["A"]
    counts = s.counts()
    assert counts["total"] == 2 and counts["comments_status:ok"] == 1


def test_details_pending_without_pk(tmp_path):
    s = _store(tmp_path)
    run = s.start_run("test")
    s.upsert_from_feed(PostRecord(shortcode="NOPK"), run, 1)
    assert s.get("NOPK")["details_status"] == "pending"
    s.upsert_from_feed(PostRecord(shortcode="NOPK", pk="9"), run, 1)
    assert s.get("NOPK")["details_status"] == "ok"


def test_raw_payloads_saved(tmp_path):
    s = _store(tmp_path)
    s.save_raw("A", "comments_rest", {"comments": [], "pinned_comment_count": 0})
    row = s.conn.execute("SELECT kind, payload FROM raw_payloads").fetchone()
    assert row[0] == "comments_rest" and json.loads(row[1])["pinned_comment_count"] == 0


def _post(**over):
    base = {
        "shortcode": "CODE1", "author": "creator", "taken_at": 1756700000, "caption": "Satır 1\nSatır 2",
        "has_video": 1, "summary": "Videoda kahve demleme anlatılıyor.", "summary_status": "ok", "language": "tr",
        "transcript": "bir iki üç dört", "transcript_status": "ok", "no_speech": 0,
        "pinned_status": "ok", "pinned_json": json.dumps([{"username": "creator", "text": "Link yorumda"}]),
        "details_status": "ok", "comments_status": "ok", "video_status": "ok", "comment_count": 5, "comments_source": "web_rest",
    }
    base.update(over)
    return base


def test_render_markdown_full_post():
    md = render_markdown([_post()], generated_at=datetime(2026, 9, 3, 8, 0))
    assert "1. **@creator** · [Postu aç](https://www.instagram.com/p/CODE1/)" in md
    assert "- **İçerik:** Videoda kahve demleme anlatılıyor. _(video, dil: tr, 4 kelime transkript)_" in md
    assert "     > Satır 1\n     > Satır 2" in md
    assert "     - @creator: Link yorumda" in md
    assert "Toplam 1 post" in md


def test_render_markdown_states():
    md = render_markdown([
        _post(shortcode="PHOTO", has_video=0, media_type=1, transcript=None, language=None, pinned_status="unknown", pinned_json="[]"),
        _post(shortcode="MUSIC", no_speech=1, summary="Görüntüde kahve var.", summary_status="ok", pinned_status="ok", pinned_json="[]"),
        _post(shortcode="WAIT", summary=None, summary_status="pending", transcript="a b", transcript_status="ok"),
        _post(shortcode="FAIL", pinned_status="failed", transcript_status="failed", summary_status="failed", summary=None, error="boom"),
        _post(shortcode="DOM", pinned_status="dom"),
    ])
    assert "_(fotoğraf)_" in md
    assert "**Sabitli yorumlar:** Belirlenemedi (web yanıtı sabitleme bilgisi vermiyor; instagrapi yedeği için SABAH-NOTU adım 2)" in md
    assert "Görüntüde kahve var. _(video, konuşma yok)_" in md
    assert "(analiz bekliyor) Transkript: a b" in md
    assert "**Sabitli yorumlar:** Sabitli yorum yok" in md
    assert "İşlenemedi: boom" in md
    assert "_(sayfa etiketinden okundu)_" in md


def test_render_json_shape():
    data = render_json([_post()])
    assert data[0]["url"].endswith("/p/CODE1/")
    assert data[0]["pinned_comments"][0]["username"] == "creator"
    assert data[0]["statuses"]["comments_status"] == "ok"


def test_loads_list_tolerates_garbage():
    assert loads_list(None) == [] and loads_list("nope") == [] and loads_list("[1]") == [1]


def test_bump_attempts_and_exclusion(tmp_path):
    s = _store(tmp_path)
    run = s.start_run("test")
    s.upsert_from_feed(PostRecord(shortcode="A", pk="1"), run, 1)
    assert s.bump("A", "comments_attempts") == 1
    assert s.bump("A", "comments_attempts") == 2
    assert s.bump("A", "comments_attempts") == 3
    assert s.pending("comments_status", where_extra="AND comments_attempts < 3") == []
    assert len(s.pending("comments_status")) == 1


def test_migration_adds_missing_columns(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE posts (shortcode TEXT PRIMARY KEY, pk TEXT, first_seen_run INTEGER, feed_position INTEGER, "
                 "comments_status TEXT DEFAULT 'pending', transcript_status TEXT DEFAULT 'pending', has_video INTEGER DEFAULT 0, "
                 "details_status TEXT DEFAULT 'pending', updated_at INTEGER)")
    conn.execute("INSERT INTO posts(shortcode) VALUES('OLD')")
    conn.commit(); conn.close()
    s = Store(db)
    cols = {row[1] for row in s.conn.execute("PRAGMA table_info(posts)")}
    assert {"comments_attempts", "video_attempts"} <= cols
    assert s.bump("OLD", "video_attempts") == 1
