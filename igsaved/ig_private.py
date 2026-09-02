"""instagrapi yedeği (opt-in). Yalnızca yorumlar için; tarayıcı oturumuyla karıştırılmaz.

Giriş `ig-login` komutunda getpass ile alınır, saklanmaz; oturum dosyası (dump_settings) kullanılır.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)


def _client(cfg: Config):
    from instagrapi import Client

    cl = Client()
    cl.delay_range = [3, 7]
    session_file: Path = cfg.path("instagrapi", "session_file")
    if session_file.exists():
        cl.load_settings(session_file)
    return cl, session_file


def interactive_login(cfg: Config) -> str:
    import getpass

    username = cfg.get("instagrapi", "username") or input("Instagram kullanıcı adı: ").strip()
    password = getpass.getpass("Instagram şifresi (ekranda görünmez, saklanmaz): ")
    cl, session_file = _client(cfg)
    code = ""
    try:
        cl.login(username, password)
    except Exception as exc:
        if "two" in str(exc).lower() or "2fa" in str(exc).lower() or "verification" in str(exc).lower():
            code = input("2FA kodu: ").strip()
            cl.login(username, password, verification_code=code)
        else:
            raise
    session_file.parent.mkdir(parents=True, exist_ok=True)
    cl.dump_settings(session_file)
    return username


class SessionMissing(RuntimeError):
    """`ig-login` henüz yapılmamış."""


def session_available(cfg: Config) -> bool:
    return cfg.path("instagrapi", "session_file").exists()


def fetch_comments_raw(cfg: Config, pk: str) -> dict:
    cl, session_file = _client(cfg)
    if not session_file.exists():
        raise SessionMissing("instagrapi oturum dosyası yok; önce `run.cmd ig-login` (menü 8) çalıştır.")
    return cl.private_request(
        f"media/{pk}/comments/",
        params={"can_support_threading": "true", "permalink_enabled": "false"},
    )
