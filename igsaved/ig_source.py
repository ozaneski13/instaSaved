"""instagrapi tabanlı veri kaynağı: kaydedilenler, koleksiyonlar, yorumlar, media bilgisi. Tarayıcı gerekmez.

Oturum `ig-login` ile bir kez oluşturulur (data/instagrapi_session.json). Tüm çağrılar ham JSON döner
(parsers.py aynı şekli web yanıtlarıyla paylaşır). Instagram sınırlama/doğrulama sinyalleri HardStop'a çevrilir.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator

from . import ig_private
from .browser import HardStop, LoginRequired
from .config import Config

log = logging.getLogger(__name__)
ALL_COLLECTION = "ALL_MEDIA_AUTO_COLLECTION"
COLLECTION_TYPES = '["ALL_MEDIA_AUTO_COLLECTION","MEDIA"]'


@dataclass
class CollectionInfo:
    id: str
    name: str
    type: str
    count: int | None

    @property
    def is_all(self) -> bool:
        return self.id == ALL_COLLECTION or self.type == ALL_COLLECTION


class MediaGone(RuntimeError):
    """Post silinmiş/erişilemez (MediaNotFound, MediaUnavailable, 404)."""


class IGSource:
    def __init__(self, cfg: Config):
        if not ig_private.session_available(cfg):
            raise LoginRequired("instagrapi oturumu yok: `run.cmd` → 1 (ig-login) ile bir kez giriş yap.")
        self.cfg = cfg
        self.cl, _ = ig_private._client(cfg)
        lo, hi = cfg.get("instagrapi", "delay_range") or [3, 7]
        self.cl.delay_range = [float(lo), float(hi)]

    # --- ortak çağrı: instagrapi istisnalarını HardStop'a çevir -----------------------------
    def _call(self, endpoint: str, params: dict | None = None) -> Any:
        from instagrapi import exceptions as ex

        try:
            return self.cl.private_request(endpoint, params=params or {})
        except (ex.MediaNotFound, ex.MediaUnavailable, ex.ClientNotFoundError) as exc:
            raise MediaGone(str(exc)[:120]) from exc
        except (ex.LoginRequired,) as exc:
            raise LoginRequired(f"instagrapi oturumu düştü ({exc}); `run.cmd` → 1 ile yeniden giriş yap.") from exc
        except (ex.PleaseWaitFewMinutes, ex.ChallengeRequired, ex.FeedbackRequired, ex.RateLimitError, ex.SentryBlock) as exc:
            raise HardStop(f"instagrapi: {type(exc).__name__}: {str(exc)[:120]}") from exc
        except ex.ClientError as exc:
            msg = str(exc).lower()
            if "please wait" in msg or "challenge" in msg or "checkpoint" in msg or "feedback_required" in msg:
                raise HardStop(f"instagrapi: {str(exc)[:120]}") from exc
            raise

    # --- koleksiyonlar ------------------------------------------------------------------------
    def collections(self) -> list[CollectionInfo]:
        data = self._call("collections/list/", {"collection_types": COLLECTION_TYPES})
        out = []
        for it in data.get("items", []) if isinstance(data, dict) else []:
            out.append(CollectionInfo(
                id=str(it.get("collection_id")), name=it.get("collection_name") or "",
                type=it.get("collection_type") or "", count=it.get("collection_media_count"),
            ))
        return out

    def resolve_collections(self, names: list[str]) -> list[CollectionInfo]:
        """Ad ya da id ile eşleştirir (büyük/küçük harf duyarsız). Bulunamayanı mevcut listeyle birlikte hata verir."""
        available = self.collections()
        out = []
        for name in names:
            key = name.strip().lower()
            match = next((c for c in available if c.name.lower() == key or c.id.lower() == key), None)
            if match is None:
                choices = ", ".join(f"'{c.name}'" for c in available if c.name)
                raise ValueError(f"Koleksiyon bulunamadı: '{name}'. Mevcut koleksiyonlar: {choices}")
            out.append(match)
        return out

    # --- kaydedilenler ----------------------------------------------------------------------------
    def iter_saved_pages(self, collection_id: str | None = None) -> Iterator[dict]:
        endpoint = "feed/saved/posts/" if not collection_id or collection_id == ALL_COLLECTION else f"feed/collection/{collection_id}/"
        max_id: str | None = None
        while True:
            data = self._call(endpoint, {"max_id": max_id} if max_id else {})
            if not isinstance(data, dict):
                return
            yield data
            if not data.get("more_available") or not data.get("next_max_id"):
                return
            max_id = str(data["next_max_id"])

    # --- post verisi -------------------------------------------------------------------------------
    def comments_raw(self, pk: str) -> dict:
        return self._call(f"media/{pk}/comments/", {"can_support_threading": "true", "permalink_enabled": "false"})

    def media_info_raw(self, pk: str) -> dict | None:
        try:
            data = self._call(f"media/{pk}/info/")
        except MediaGone:
            return None
        items = data.get("items") if isinstance(data, dict) else None
        return items[0] if items else None
