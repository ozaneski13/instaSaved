import json

from igsaved.parsers import (
    Comment,
    extract_shortcode,
    find_comments_payloads,
    find_media_in_json,
    fix_mojibake,
    parse_comments_graphql,
    parse_comments_v1,
    parse_export_saved_posts,
    parse_feed_item,
    parse_saved_feed,
    pick_video_url,
)


def _video_media(code="ABC123xyz", pk=123456789012345678):
    return {
        "pk": pk,
        "id": f"{pk}_555",
        "code": code,
        "media_type": 2,
        "product_type": "clips",
        "taken_at": 1756700000,
        "comment_count": 42,
        "like_count": 1000,
        "user": {"username": "creator"},
        "caption": {"text": "Merhaba dünya #test"},
        "video_versions": [
            {"width": 640, "height": 1136, "url": "https://cdn/low.mp4?oe=1"},
            {"width": 720, "height": 1280, "url": "https://cdn/high.mp4?oe=1"},
        ],
    }


def test_parse_feed_item_unwraps_media_and_picks_largest_video():
    rec = parse_feed_item({"media": _video_media()})
    assert rec.shortcode == "ABC123xyz"
    assert rec.pk == "123456789012345678"
    assert rec.author == "creator"
    assert rec.caption == "Merhaba dünya #test"
    assert rec.has_video is True
    assert rec.video_urls == ["https://cdn/high.mp4?oe=1"]
    assert rec.comment_count == 42


def test_parse_feed_item_photo_has_no_video():
    media = _video_media()
    media.pop("video_versions")
    media["media_type"] = 1
    rec = parse_feed_item(media)
    assert rec.has_video is False and rec.video_urls == []


def test_parse_feed_item_album_collects_child_videos():
    media = {
        "pk": 1, "code": "ALBUM1", "media_type": 8, "caption": None,
        "carousel_media": [
            {"media_type": 1, "image_versions2": {}},
            {"media_type": 2, "video_versions": [{"width": 1, "height": 1, "url": "https://cdn/a.mp4"}]},
            {"media_type": 2, "video_versions": [{"width": 1, "height": 1, "url": "https://cdn/b.mp4"}]},
        ],
    }
    rec = parse_feed_item(media)
    assert rec.video_urls == ["https://cdn/a.mp4", "https://cdn/b.mp4"]
    assert rec.caption is None


def test_parse_saved_feed_returns_pagination():
    data = {"items": [{"media": _video_media("A")}, {"media": _video_media("B")}], "more_available": True, "next_max_id": "xyz"}
    records, more, nxt = parse_saved_feed(data)
    assert [r.shortcode for r in records] == ["A", "B"]
    assert more is True and nxt == "xyz"


def test_pick_video_url_ignores_entries_without_url():
    assert pick_video_url({"video_versions": [{"width": 1, "height": 1}, {"width": 2, "height": 2, "url": "u"}]}) == "u"
    assert pick_video_url({}) is None


# --- yorumlar ------------------------------------------------------------------
def _comment(pk, text, pinned=None, user="u"):
    c = {"pk": pk, "text": text, "user": {"username": user}, "created_at": 1, "comment_like_count": 3,
         "preview_child_comments": [{"pk": pk * 10, "text": "child", "visual_comment_reply_sticker_info": {"is_pinned": 0}}]}
    if pinned is not None:
        c["is_pinned"] = pinned
    return c


def test_parse_comments_v1_reads_top_level_is_pinned_only():
    data = {
        "comments": [_comment(1, "birinci", True, "owner"), _comment(2, "ikinci", True), _comment(3, "sıradan"), _comment(4, "başka")],
        "pinned_comment_count": 2,
        "has_more_headload_comments": True,
        "next_min_id": "{\"cached\":1}",
    }
    res = parse_comments_v1(data)
    assert res.flag_present is True
    assert [c.text for c in res.pinned] == ["birinci", "ikinci"]
    assert res.pinned_count == 2 and res.count_mismatch is False
    assert len(res.comments) == 4
    assert res.has_more is True and res.next_min_id == "{\"cached\":1}"


def test_parse_comments_v1_without_flags_is_not_pinned_evidence():
    res = parse_comments_v1({"comments": [_comment(1, "a"), _comment(2, "b")]})
    assert res.flag_present is False
    assert res.pinned == []
    assert res.pinned_count is None


def test_parse_comments_v1_count_zero_means_no_pinned_but_flag_present():
    res = parse_comments_v1({"comments": [_comment(1, "a")], "pinned_comment_count": 0})
    assert res.flag_present is True and res.pinned == [] and res.count_mismatch is False


def test_parse_comments_v1_detects_count_mismatch():
    res = parse_comments_v1({"comments": [_comment(1, "a", True)], "pinned_comment_count": 3})
    assert res.count_mismatch is True


def test_parse_comments_graphql_connection():
    data = {"data": {"xdt_api__v1__media__media_id__comments__connection": {
        "edges": [{"node": _comment(1, "pinli", True)}, {"node": _comment(2, "normal")}],
        "page_info": {"has_next_page": False, "end_cursor": None},
    }}}
    res = parse_comments_graphql(data)
    assert res is not None and res.flag_present is True
    assert [c.text for c in res.pinned] == ["pinli"]
    assert parse_comments_graphql({"data": {"other": {}}}) is None


def test_find_comments_payloads_finds_nested_lists():
    blob = {"require": [[{"__bbox": {"result": {"comments": [_comment(1, "x")], "pinned_comment_count": 0}}}]]}
    found = find_comments_payloads([blob])
    assert len(found) == 1 and found[0]["pinned_comment_count"] == 0


# --- yardımcılar ------------------------------------------------------------------
def test_extract_shortcode_variants():
    assert extract_shortcode("https://www.instagram.com/p/DC7Y9vyyg_b/") == "DC7Y9vyyg_b"
    assert extract_shortcode("https://www.instagram.com/reel/DcwCFJXvkDw/?igsh=abc") == "DcwCFJXvkDw"
    assert extract_shortcode("https://www.instagram.com/mrbeast/p/DC7Y9vyyg_b/") == "DC7Y9vyyg_b"
    assert extract_shortcode("https://www.instagram.com/tv/ABCDEFG/") == "ABCDEFG"
    assert extract_shortcode("DcwCFJXvkDw") == "DcwCFJXvkDw"
    assert extract_shortcode("https://www.instagram.com/someuser/") is None


def test_fix_mojibake_only_when_markers_present():
    assert fix_mojibake("Ã§ok gÃ¼zel bir gÃ¼n") == "çok güzel bir gün"
    assert fix_mojibake("zaten düzgün ş ğ ı") == "zaten düzgün ş ğ ı"
    assert fix_mojibake(None) is None


def test_find_media_in_json_by_code():
    blob = {"require": [[{"data": {"items": [_video_media("TARGET"), _video_media("OTHER")]}}]]}
    media = find_media_in_json([blob, {"foo": 1}], "TARGET")
    assert media is not None and media["code"] == "TARGET"
    assert find_media_in_json([blob], "MISSING") is None


def test_parse_export_saved_posts_with_localized_key():
    data = {"saved_saved_media": [
        {"title": "creator1", "string_map_data": {"Kaydedildi": {"href": "https://www.instagram.com/reel/AAA111/", "timestamp": 1700000000}}},
        {"title": "creator2", "string_map_data": {"Saved on": {"href": "https://www.instagram.com/p/BBB222/", "timestamp": 1700000001}}},
        {"title": "broken", "string_map_data": {"Saved on": {"timestamp": 1}}},
    ]}
    rows = parse_export_saved_posts(data)
    assert [r["shortcode"] for r in rows] == ["AAA111", "BBB222"]
    assert rows[0]["author"] == "creator1"


def test_comment_to_dict_roundtrip():
    c = Comment(id="1", username="u", text="t", is_pinned=True)
    assert json.loads(json.dumps(c.to_dict()))["is_pinned"] is True
