"""Kaydedilenler listesini sayfalayarak state'e yazar (artımlı)."""
from __future__ import annotations

import logging
import time

from .browser import IGBrowser
from .config import Config
from .parsers import PostRecord, extract_shortcode, parse_saved_feed
from .store import Store

log = logging.getLogger(__name__)
FEED_URL_PART = "/api/v1/feed/saved/posts/"


def _is_feed(c) -> bool:
    return FEED_URL_PART in c.url and isinstance(c.json, dict) and "items" in c.json


def enumerate_saved(browser: IGBrowser, store: Store, cfg: Config, run_id: int,
                    limit_new: int | None = None, incremental: bool = True) -> dict:
    username = browser.username()
    known_before = store.known_shortcodes()
    stats = {"pages": 0, "seen": 0, "new": 0, "stopped_by": None}
    stop_after = int(cfg.get("pacing", "known_pages_to_stop") or 3)
    start_ts = time.time()

    browser.goto(f"https://www.instagram.com/{username}/saved/all-posts/")
    processed: set[int] = set()
    position = 0
    known_pages = 0
    stalls = 0
    more_available = True
    first = browser.wait_for_capture(lambda c: c.ts >= start_ts and _is_feed(c), timeout=25)
    if first is None:
        log.warning("Kaydedilenler XHR'i yakalanamadı; DOM bağlantılarına düşülüyor")
        for href in browser.post_links_in_dom():
            code = extract_shortcode("https://www.instagram.com" + href if href.startswith("/") else href)
            if code:
                position += 1
                stats["seen"] += 1
                if store.upsert_from_feed(PostRecord(shortcode=code), run_id, position):
                    stats["new"] += 1
        stats["stopped_by"] = "dom_fallback"
        return stats

    while True:
        fresh = [c for c in browser.captured_since(start_ts) if _is_feed(c) and c.seq not in processed]
        for c in fresh:
            processed.add(c.seq)
            stats["pages"] += 1
            if stats["pages"] == 1:
                store.save_raw(None, "saved_feed_sample", c.json)
            records, more_available, _next = parse_saved_feed(c.json)
            page_new = 0
            for rec in records:
                position += 1
                stats["seen"] += 1
                if store.upsert_from_feed(rec, run_id, position):
                    stats["new"] += 1
                    page_new += 1
            known_pages = known_pages + 1 if (page_new == 0 and records) else 0
            log.info("Kaydedilenler sayfa %d: %d öğe, %d yeni (toplam yeni %d)",
                     stats["pages"], len(records), page_new, stats["new"])
        if limit_new and stats["new"] >= limit_new:
            stats["stopped_by"] = "limit"
            break
        if incremental and known_before and known_pages >= stop_after:
            stats["stopped_by"] = "all_known"
            break
        if not more_available:
            stats["stopped_by"] = "end_of_feed"
            break
        before = len([c for c in browser.captured_since(start_ts) if _is_feed(c)])
        browser.scroll()
        nxt = browser.wait_for_capture(
            lambda c: c.ts >= start_ts and _is_feed(c) and c.seq not in processed, timeout=15
        )
        browser.check_hard_stop()
        if nxt is None:
            stalls += 1
            if stalls >= 3:
                stats["stopped_by"] = "stalled"
                break
            continue
        stalls = 0
        _ = before
    return stats


def enumerate_saved_api(source, store: Store, cfg: Config, run_id: int, limit_new: int | None = None,
                        incremental: bool = True, collections=None) -> dict:
    """instagrapi kaynağıyla kaydedilenleri sayfalar. collections: CollectionInfo listesi (boş → tümü)."""
    known_before = store.known_shortcodes()
    stop_after = int(cfg.get("pacing", "known_pages_to_stop") or 3)
    stats = {"pages": 0, "seen": 0, "new": 0, "stopped_by": None, "collections": [c.name for c in (collections or [])]}
    position = 0
    for col in (collections or [None]):
        known_pages = 0
        col_id = None if (col is None or col.is_all) else col.id
        for data in source.iter_saved_pages(col_id):
            stats["pages"] += 1
            if stats["pages"] == 1:
                store.save_raw(None, "saved_feed_sample", data)
            records, _more, _next = parse_saved_feed(data)
            page_new = 0
            for rec in records:
                position += 1
                stats["seen"] += 1
                if store.upsert_from_feed(rec, run_id, position):
                    stats["new"] += 1
                    page_new += 1
                if col is not None and not col.is_all:
                    store.add_collection(rec.shortcode, col.name)
            known_pages = known_pages + 1 if (page_new == 0 and records) else 0
            log.info("Kaydedilenler%s sayfa %d: %d öğe, %d yeni (toplam yeni %d)",
                     f" [{col.name}]" if col is not None else "", stats["pages"], len(records), page_new, stats["new"])
            if limit_new and stats["new"] >= limit_new:
                stats["stopped_by"] = "limit"
                return stats
            if incremental and known_before and known_pages >= stop_after:
                stats["stopped_by"] = "all_known"
                break
        else:
            stats["stopped_by"] = "end_of_feed"
    return stats
