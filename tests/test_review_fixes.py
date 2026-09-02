"""İnceleme (2026-09-02) sonrası düzeltmelerin regresyon testleri."""
import json

from igsaved import cli
from igsaved.parsers import PostRecord, find_comments_payloads, parse_feed_item
from igsaved.store import Store


def test_has_video_from_media_type_when_video_versions_missing():
    rec = parse_feed_item({"pk": 1, "code": "REEL", "media_type": 2, "product_type": "clips", "caption": None})
    assert rec.has_video is True and rec.video_urls == []
    photo = parse_feed_item({"pk": 2, "code": "PHOTO", "media_type": 1})
    assert photo.has_video is False


def test_find_comments_payloads_ignores_media_preview_comments():
    media_with_preview = {"code": "X", "pk": 1, "comments": [{"pk": 5, "text": "önizleme", "user": {}}]}
    endpoint_response = {"comments": [{"pk": 6, "text": "gerçek", "user": {}}], "pinned_comment_count": 1}
    found = find_comments_payloads([{"a": media_with_preview, "b": endpoint_response}])
    assert found == [endpoint_response]


def test_save_raw_keeps_only_latest_per_kind(tmp_path):
    s = Store(tmp_path / "s.db")
    s.save_raw("A", "comments_rest_replay", {"n": 1})
    s.save_raw("A", "comments_rest_replay", {"n": 2})
    s.save_raw("A", "post_media", {"n": 3})
    rows = s.conn.execute("SELECT kind, payload FROM raw_payloads WHERE shortcode='A' ORDER BY id").fetchall()
    assert [(r[0], json.loads(r[1])["n"]) for r in rows] == [("comments_rest_replay", 2), ("post_media", 3)]


def test_run_all_skips_process_after_hardstop(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli, "cmd_sync", lambda *a, **k: cli.EXIT_HARDSTOP)
    monkeypatch.setattr(cli, "cmd_process", lambda *a, **k: calls.append("process"))
    monkeypatch.setattr(cli, "cmd_report", lambda cfg, store: calls.append("report"))
    code = cli.run_all(cfg=None, store=None, limit=None, full=False, no_summary=False)
    assert code == cli.EXIT_HARDSTOP and calls == ["report"]


def test_run_all_processes_when_sync_ok(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_sync", lambda *a, **k: cli.EXIT_OK)
    monkeypatch.setattr(cli, "cmd_process", lambda *a, **k: calls.append("process") or cli.EXIT_OK)
    monkeypatch.setattr(cli, "cmd_report", lambda cfg, store: calls.append("report"))
    assert cli.run_all(None, None, None, False, False) == cli.EXIT_OK and calls == ["process", "report"]


def test_visit_queue_includes_details_refresh_rows(tmp_path):
    s = Store(tmp_path / "s.db")
    run = s.start_run("t")
    s.upsert_from_feed(PostRecord(shortcode="C", pk="1"), run, 1)                   # yorumları bekliyor
    s.upsert_from_feed(PostRecord(shortcode="R", pk="2", has_video=True), run, 2)   # video URL'si yok
    s.update("R", comments_status="ok", pinned_status="ok", details_status="pending")
    queue = cli._visit_queue(s, 10)
    assert [(k, p["shortcode"]) for k, p in queue] == [("comments", "C"), ("refresh", "R")]
    s.bump("R", "video_attempts"); s.bump("R", "video_attempts"); s.bump("R", "video_attempts")
    assert [(k, p["shortcode"]) for k, p in cli._visit_queue(s, 10)] == [("comments", "C")]



def test_save_raw_dedupes_null_shortcode(tmp_path):
    s = Store(tmp_path / "s.db")
    s.save_raw(None, "saved_feed_sample", {"n": 1})
    s.save_raw(None, "saved_feed_sample", {"n": 2})
    rows = s.conn.execute("SELECT payload FROM raw_payloads WHERE shortcode IS NULL").fetchall()
    assert [json.loads(r[0])["n"] for r in rows] == [2]


def test_process_retires_video_post_without_url_after_max_attempts(tmp_path, monkeypatch):
    from igsaved.config import Config
    cfg = Config.load(None)
    cfg.raw["data_dir"] = str(tmp_path / "data")
    cfg.raw["video"]["dir"] = str(tmp_path / "videos")
    cfg.raw["instagrapi"]["session_file"] = str(tmp_path / "no-session.json")  # testler ağa çıkmaz
    s = Store(tmp_path / "s.db")
    run = s.start_run("t")
    s.upsert_from_feed(PostRecord(shortcode="REEL", pk="1", media_type=2, has_video=True), run, 1)
    cli.cmd_process(cfg, s, None, True)                     # URL yok → 1. deneme, detay tazelemeye devredilir
    row = s.get("REEL")
    assert row["video_attempts"] == 1 and row["details_status"] == "pending"
    cli.cmd_process(cfg, s, None, True)                     # detay pending → process tekrar seçmez (sonsuz döngü yok)
    assert s.get("REEL")["video_attempts"] == 1
    s.update("REEL", comments_status="ok", pinned_status="ok")
    assert [(k, p["shortcode"]) for k, p in cli._visit_queue(s, 10)] == [("refresh", "REEL")]
    cli._note_refresh_failure(s, "REEL"); cli._note_refresh_failure(s, "REEL")   # sync'teki tazeleme 2 kez daha başarısız
    row = s.get("REEL")
    assert row["video_attempts"] == 3 and row["details_status"] == "ok"
    assert row["summary_status"] == "failed" and row["transcript_status"] == "failed"
    assert cli._visit_queue(s, 10) == []
    cli.cmd_process(cfg, s, None, True)                     # video_attempts >= MAX → seçilmez
    assert s.get("REEL")["video_attempts"] == 3


def test_visit_queue_reserves_slot_for_refresh(tmp_path):
    s = Store(tmp_path / "s.db")
    run = s.start_run("t")
    for i in range(5):
        s.upsert_from_feed(PostRecord(shortcode=f"C{i}", pk=str(i)), run, i)
    s.upsert_from_feed(PostRecord(shortcode="R", pk="9", has_video=True), run, 9)
    s.update("R", comments_status="ok", pinned_status="ok", details_status="pending")
    queue = cli._visit_queue(s, 3)
    assert [k for k, _ in queue] == ["comments", "comments", "refresh"]
    assert queue[-1][1]["shortcode"] == "R"


def test_visit_queue_refreshes_comment_capped_posts(tmp_path):
    s = Store(tmp_path / "s.db")
    run = s.start_run("t")
    s.upsert_from_feed(PostRecord(shortcode="X"), run, 1)   # pk yok → details pending, comments pending
    for _ in range(3):
        s.bump("X", "comments_attempts")
    assert [(k, p["shortcode"]) for k, p in cli._visit_queue(s, 10)] == [("refresh", "X")]


def test_requeue_unknown_pins_only_with_session(tmp_path):
    from igsaved.config import Config
    cfg = Config.load(None)
    cfg.raw["instagrapi"] = {"enabled": True, "username": "u", "session_file": str(tmp_path / "sess.json")}
    s = Store(tmp_path / "s.db")
    run = s.start_run("t")
    s.upsert_from_feed(PostRecord(shortcode="U", pk="1"), run, 1)
    s.update("U", comments_status="ok", pinned_status="unknown", comments_source="web_rest_replay")
    s.upsert_from_feed(PostRecord(shortcode="K", pk="2"), run, 2)
    s.update("K", comments_status="ok", pinned_status="ok", comments_source="instagrapi")
    assert cli.requeue_unknown_pins(s, cfg) == 0          # oturum dosyası yok → dokunma
    (tmp_path / "sess.json").write_text("{}", encoding="utf-8")
    assert cli.requeue_unknown_pins(s, cfg) == 1
    assert s.get("U")["comments_status"] == "pending" and s.get("K")["comments_status"] == "ok"
    cfg.raw["instagrapi"]["enabled"] = False
    assert cli.requeue_unknown_pins(s, cfg) == 0
