"""Instagram mobil API oturumu (instagrapi): `ig-login` ile giriş, oturum dosyası, yorum isteği.

Şifre getpass ile alınır, saklanmaz; diske yalnızca oturum dosyası (dump_settings) yazılır.
Giriş akışı instagrapi 2.18.18'e göre yazıldı (requirements'ta sabit); sürüm değişirse 2FA kısmını yeniden doğrula.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from uuid import uuid4

from .config import Config

log = logging.getLogger(__name__)
BACKUP_CODE_LENGTH = 8


def _client(cfg: Config):
    from instagrapi import Client

    cl = Client()
    cl.delay_range = [3, 7]
    session_file: Path = cfg.path("instagrapi", "session_file")
    if session_file.exists():
        cl.load_settings(session_file)
    return cl, session_file


def _read_code(prompt: str, lengths: tuple[int, ...]) -> str:
    while True:
        code = input(prompt).strip().replace(" ", "")
        if code.isdigit() and len(code) in lengths:
            return code
        print(f"Kod {' ya da '.join(str(n) for n in lengths)} haneli olmalı, tekrar gir.")


def _challenge_code_prompt(username: str, choice=None) -> str:
    """instagrapi'nin doğrulama kodu istemi (varsayılanı İngilizce ve enum gösteriyor)."""
    channel = "e-posta" if "EMAIL" in str(choice) else ("SMS" if "SMS" in str(choice) else "e-posta ya da SMS")
    return _read_code(f"Instagram doğrulama kodu ({channel} ile gönderildi): ", (4, 5, 6, 7, 8))


def _change_password_prompt(username: str) -> str:
    """instagrapi'nin varsayılanı yeni şifreyi ekranda açık yazdırıyor; getpass ile gizli al."""
    import getpass

    return getpass.getpass("Instagram yeni şifre belirlemeni istiyor. Yeni şifre (ekranda görünmez, kaydedilmez): ")


def _session_id(cl) -> str | None:
    return (getattr(cl, "authorization_data", None) or {}).get("sessionid")


def _authorized(cl) -> bool:
    return bool(_session_id(cl)) and bool(getattr(cl, "user_id", None))


def _submit_two_factor(cl, identifier: str, code: str) -> None:
    """instagrapi 2.18.18 auth.py login() içindeki accounts/two_factor_login isteğinin aynısı. Tek fark: accounts/login
    yeniden gönderilmez; SMS kodu ilk denemenin two_factor_identifier'ına bağlı olduğu için ikinci giriş onu bozabilir."""
    data = {
        "verification_code": code,
        "phone_id": cl.phone_id,
        "_csrftoken": cl.token,
        "two_factor_identifier": identifier,
        "username": cl.username,
        "trust_this_device": "0",
        "guid": cl.uuid,
        "device_id": cl.android_device_id,
        "waterfall_id": str(uuid4()),
        "verification_method": "3",
    }
    cl.private_request("accounts/two_factor_login/", data, login=True)
    cl.authorization_data = cl.parse_authorization(cl.last_response.headers.get("ig-set-authorization"))
    cl.login_flow()
    cl.last_login = time.time()
    cl.relogin_attempt = 0


def _complete_two_factor(cl, username: str, password: str, first_response: dict) -> None:
    from instagrapi.exceptions import TwoFactorRequired, UnknownError

    info = first_response.get("two_factor_info") or {}
    if info.get("totp_two_factor_on"):
        where = "kimlik doğrulama uygulamasındaki"
    elif info.get("sms_two_factor_on"):
        where = "SMS ile gelen"
    else:
        where = "kimlik doğrulama uygulamasındaki ya da SMS ile gelen"
    code = _read_code(f"İki adımlı doğrulama kodu ({where}; yedek kod da olur): ", (6, BACKUP_CODE_LENGTH))
    identifier = info.get("two_factor_identifier")
    if not identifier or len(code) == BACKUP_CODE_LENGTH:
        # Bloks bağlamı ya da yedek kod: kütüphanenin kendi yolu (accounts/login'i yeniden gönderir)
        cl.login(username, password, verification_code=code)
        return
    try:
        _submit_two_factor(cl, identifier, code)
    except UnknownError as exc:
        if (getattr(exc, "message", "") or "").strip().lower() == "invalid parameters":
            cl.login(username, password, verification_code=code)  # kütüphanenin Bloks yedeğine devret
            return
        raise TwoFactorRequired(str(exc)) from exc


def interactive_login(cfg: Config) -> str:
    """Kayıtlı oturum dosyası varsa cihaz kimliğiyle birlikte yüklenir; instagrapi oturumu account_info() ile doğrular,
    düşmüşse aynı cihazla sıfırdan giriş yapar (yeni cihaz uyarısı çıkmaz)."""
    import getpass

    from instagrapi.exceptions import ClientError, TwoFactorRequired

    username = cfg.get("instagrapi", "username") or cfg.get("username") or input("Instagram kullanıcı adı: ").strip()
    password = getpass.getpass("Instagram şifresi (ekranda görünmez, saklanmaz): ")
    cl, session_file = _client(cfg)
    old_session = _session_id(cl)  # dosyadaki (muhtemelen düşmüş) oturum; hata yolunda "yeni oturum" ayrımı için
    cl.challenge_code_handler = _challenge_code_prompt
    cl.change_password_handler = _change_password_prompt
    try:
        try:
            cl.login(username, password)
        except TwoFactorRequired:
            first = dict(cl.last_json) if isinstance(cl.last_json, dict) else {}
            _complete_two_factor(cl, username, password, first)
    except Exception:
        if _authorized(cl) and _session_id(cl) != old_session:
            # Instagram girişi onayladı, sonraki bir istek düştü: yeni oturumu kaybetme (tekrar şifreli giriş = yeni şüphe)
            _save(cl, session_file)
            log.warning("Giriş onaylandı ve oturum kaydedildi, ama giriş sonrası bir istek hata verdi.")
        raise
    if not _authorized(cl):
        raise ClientError("Giriş tamamlanmadı: Instagram oturum bilgisi dönmedi.")
    _save(cl, session_file)
    return username


def _save(cl, session_file: Path) -> None:
    session_file.parent.mkdir(parents=True, exist_ok=True)
    cl.dump_settings(session_file)


def login_error_message(exc: BaseException) -> str | None:
    """Giriş hatasını kullanıcıya ne yapacağını söyleyen Türkçe mesaja çevirir.
    Tanınmayan bir hata ise None döner (gerçek bir kusurdur, yutulmamalı)."""
    import requests
    from instagrapi import exceptions as ex

    detail = f" (Instagram: {str(exc)[:200]})" if str(exc) else ""
    if isinstance(exc, (ex.ClientConnectionError, ex.ClientIncompleteReadError, ex.ClientRequestTimeout,
                        requests.RequestException)):
        return "İnternet bağlantısı yok ya da Instagram'a ulaşılamıyor. Bağlantını kontrol edip menü 1'i tekrar çalıştır."
    if isinstance(exc, (AssertionError, KeyError)):
        return ("Instagram'ın doğrulama akışı beklenmedik bir yanıt verdi. Telefondaki Instagram uygulamasını aç, "
                f"güvenlik bildirimini onayla, birkaç dakika sonra menü 1'i tekrar çalıştır. ({type(exc).__name__}: {str(exc)[:120]})")
    if not isinstance(exc, ex.ClientError):
        return None
    if isinstance(exc, (ex.BadPassword, ex.BadCredentials)):
        return ("Kullanıcı adı ya da şifre kabul edilmedi. Şifreyi kontrol edip menü 1'i tekrar çalıştır. "
                "Şifre doğruysa Instagram bu girişi şüpheli bulmuş olabilir: telefondaki Instagram uygulamasını aç, "
                "güvenlik bildirimini onayla, birkaç dakika sonra tekrar dene.")
    if isinstance(exc, ex.ProxyAddressIsBlocked):
        return ("Instagram bu kullanıcı adını tanımadı ya da bağlantıyı engelledi. config.json'daki instagrapi.username'i "
                "kontrol et; doğruysa birkaç saat sonra menü 1'i tekrar çalıştır.")
    if isinstance(exc, ex.AccountSuspended):
        return "Hesap askıya alınmış ya da incelemede görünüyor. Telefondaki Instagram uygulamasından çöz, sonra tekrar dene."
    if isinstance(exc, ex.TwoFactorRequired):
        return ("İki adımlı doğrulama kodu kabul edilmedi. Menü 1'i tekrar çalıştır ve en son gelen kodu gir; "
                "SMS kodu reddedilmeye devam ederse kimlik doğrulama uygulaması ya da yedek kod kullan." + detail)
    if isinstance(exc, (ex.ChallengeSelfieCaptcha, ex.RecaptchaChallengeForm)):
        return ("Instagram görsel doğrulama (selfie/captcha) istiyor; bunu program çözemez. Telefondaki Instagram "
                "uygulamasını aç, istenen doğrulamayı tamamla, sonra menü 1'i tekrar çalıştır.")
    if isinstance(exc, ex.ChallengeError):
        return ("Instagram bu giriş için doğrulama istiyor. Telefondaki Instagram uygulamasını aç, "
                "\"Bu bendim\" diyerek onayla ya da istenen adımı tamamla, birkaç dakika sonra menü 1'i tekrar çalıştır.")
    if isinstance(exc, (ex.PleaseWaitFewMinutes, ex.RateLimitError, ex.ClientThrottledError,
                        ex.FeedbackRequired, ex.SentryBlock)):
        return "Instagram şu an girişleri sınırlıyor. Birkaç saat bekleyip menü 1'i tekrar çalıştır; arka arkaya deneme."
    return f"Instagram girişi başarısız ({type(exc).__name__}): {str(exc)[:200]}"


class SessionMissing(RuntimeError):
    """`ig-login` henüz yapılmamış."""


def session_available(cfg: Config) -> bool:
    return cfg.path("instagrapi", "session_file").exists()


def fetch_comments_raw(cfg: Config, pk: str) -> dict:
    cl, session_file = _client(cfg)
    if not session_file.exists():
        raise SessionMissing("instagrapi oturum dosyası yok; önce `run.cmd ig-login` (menü 1) çalıştır.")
    return cl.private_request(
        f"media/{pk}/comments/",
        params={"can_support_threading": "true", "permalink_enabled": "false"},
    )
