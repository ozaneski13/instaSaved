from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .parsers import PostRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    shortcode TEXT PRIMARY KEY,
    pk TEXT,
    author TEXT,
    taken_at INTEGER,
    first_seen_run INTEGER,
    feed_position INTEGER,
    caption TEXT,
    media_type INTEGER,
    product_type TEXT,
    has_video INTEGER DEFAULT 0,
    video_urls TEXT,
    video_path TEXT,
    comment_count INTEGER,
    details_status TEXT DEFAULT 'pending',
    comments_status TEXT DEFAULT 'pending',
    pinned_status TEXT DEFAULT 'pending',
    pinned_json TEXT,
    comments_json TEXT,
    comments_source TEXT,
    video_status TEXT DEFAULT 'pending',
    transcript TEXT,
    language TEXT,
    language_prob REAL,
    no_speech INTEGER,
    transcript_status TEXT DEFAULT 'pending',
    summary TEXT,
    summary_status TEXT DEFAULT 'pending',
    comments_attempts INTEGER DEFAULT 0,
    video_attempts INTEGER DEFAULT 0,
    image_urls TEXT,
    collections TEXT,
    analysis_meta TEXT,
    error TEXT,
    updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS raw_payloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shortcode TEXT,
    kind TEXT,
    payload TEXT,
    fetched_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_raw_shortcode ON raw_payloads(shortcode);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER,
    finished_at INTEGER,
    command TEXT,
    note TEXT
);
"""


# Şema tarihçesi: sonradan eklenen sütunlar buraya da yazılır ki eski state.db dosyaları açılırken tamamlansın.
POST_COLUMNS_ADDED_LATER = {
    "comments_attempts": "INTEGER DEFAULT 0",
    "video_attempts": "INTEGER DEFAULT 0",
    "image_urls": "TEXT",
    "collections": "TEXT",
    "analysis_meta": "TEXT",
}


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Eski veritabanlarına sonradan eklenen sütunları ekler (CREATE IF NOT EXISTS sütun eklemez)."""
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(posts)")}
        for column, ddl in POST_COLUMNS_ADDED_LATER.items():
            if column not in existing:
                self.conn.execute(f"ALTER TABLE posts ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        self.conn.close()

    # --- runs -------------------------------------------------------------
    def start_run(self, command: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(started_at, command) VALUES(?, ?)", (int(time.time()), command)
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, note: str = "") -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, note=? WHERE id=?", (int(time.time()), note, run_id)
        )
        self.conn.commit()

    # --- posts ------------------------------------------------------------
    def known_shortcodes(self) -> set[str]:
        return {row[0] for row in self.conn.execute("SELECT shortcode FROM posts")}

    def get(self, shortcode: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM posts WHERE shortcode=?", (shortcode,)).fetchone()
        return dict(row) if row else None

    def upsert_from_feed(self, rec: PostRecord, run_id: int, position: int) -> bool:
        """Yeni post ise True döner. Var olan postun feed alanları güncellenir, ilerleme durumları korunur."""
        existing = self.get(rec.shortcode)
        now = int(time.time())
        if existing is None:
            self.conn.execute(
                """INSERT INTO posts(shortcode, pk, author, taken_at, first_seen_run, feed_position, caption,
                   media_type, product_type, has_video, video_urls, image_urls, comment_count, details_status, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec.shortcode, rec.pk, rec.author, rec.taken_at, run_id, position, rec.caption,
                    rec.media_type, rec.product_type, int(rec.has_video), json.dumps(rec.video_urls),
                    json.dumps(rec.image_urls), rec.comment_count, "ok" if rec.pk else "pending", now,
                ),
            )
            self.conn.commit()
            return True
        self.conn.execute(
            """UPDATE posts SET pk=COALESCE(?, pk), author=COALESCE(?, author), taken_at=COALESCE(?, taken_at),
               caption=COALESCE(?, caption), media_type=COALESCE(?, media_type), product_type=COALESCE(?, product_type),
               has_video=MAX(has_video, ?), video_urls=CASE WHEN ? != '[]' THEN ? ELSE video_urls END,
               image_urls=CASE WHEN ? != '[]' THEN ? ELSE image_urls END,
               comment_count=COALESCE(?, comment_count), details_status=CASE WHEN ? IS NOT NULL THEN 'ok' ELSE details_status END,
               updated_at=? WHERE shortcode=?""",
            (
                rec.pk, rec.author, rec.taken_at, rec.caption, rec.media_type, rec.product_type,
                int(rec.has_video), json.dumps(rec.video_urls), json.dumps(rec.video_urls),
                json.dumps(rec.image_urls), json.dumps(rec.image_urls), rec.comment_count,
                rec.pk, now, rec.shortcode,
            ),
        )
        self.conn.commit()
        return False

    def update(self, shortcode: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE posts SET {cols} WHERE shortcode=?", (*fields.values(), shortcode))
        self.conn.commit()

    def add_collection(self, shortcode: str, name: str) -> None:
        row = self.get(shortcode)
        if row is None or not name:
            return
        current = loads_list(row.get("collections"))
        if name not in current:
            current.append(name)
            self.update(shortcode, collections=json.dumps(current, ensure_ascii=False))

    def bump(self, shortcode: str, column: str) -> int:
        """Deneme sayacını artırır (comments_attempts / video_attempts); yeni değeri döner."""
        assert column in ("comments_attempts", "video_attempts")
        self.conn.execute(
            f"UPDATE posts SET {column}=COALESCE({column},0)+1, updated_at=? WHERE shortcode=?",
            (int(time.time()), shortcode),
        )
        self.conn.commit()
        return int(self.conn.execute(f"SELECT {column} FROM posts WHERE shortcode=?", (shortcode,)).fetchone()[0])

    def save_raw(self, shortcode: str | None, kind: str, payload: Any) -> None:
        """Ham yanıtı saklar; aynı (shortcode, kind) için yalnızca en yenisi tutulur (DB sınırsız büyümez)."""
        # `IS` NULL-güvenli eşitlik: shortcode=None olan örnek kayıtlar (saved_feed_sample) da tekilleşir.
        self.conn.execute("DELETE FROM raw_payloads WHERE shortcode IS ? AND kind=?", (shortcode, kind))
        self.conn.execute(
            "INSERT INTO raw_payloads(shortcode, kind, payload, fetched_at) VALUES(?,?,?,?)",
            (shortcode, kind, json.dumps(payload, ensure_ascii=False), int(time.time())),
        )
        self.conn.commit()

    def pending(self, column: str, limit: int | None = None, where_extra: str = "") -> list[dict]:
        sql = f"SELECT * FROM posts WHERE {column} IN ('pending','partial','failed') {where_extra} ORDER BY first_seen_run DESC, feed_position ASC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(sql)]

    def all_posts(self) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM posts ORDER BY first_seen_run DESC, feed_position ASC, shortcode ASC"
            )
        ]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        out["total"] = self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        for col in ("comments_status", "pinned_status", "video_status", "transcript_status", "summary_status"):
            for row in self.conn.execute(f"SELECT {col}, COUNT(*) FROM posts GROUP BY {col}"):
                out[f"{col}:{row[0]}"] = row[1]
        return out


def loads_list(value: str | None) -> list:
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []
