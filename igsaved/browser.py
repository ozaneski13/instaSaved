"""Playwright üzerinden gerçek Chrome profiliyle Instagram okuma katmanı.

- Şifre programa hiç girmez: kullanıcı `login` komutunun açtığı pencerede kendisi giriş yapar,
  oturum kalıcı profilde (data/chrome-profile/<kanal>) saklanır.
- Sayfanın kendi XHR/GraphQL yanıtları yakalanır (DOM ayrıştırma yerine).
- Hız sınırı / doğrulama sinyalinde HardStop: yeniden deneme yok, koşu durur.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import unquote

from playwright.sync_api import BrowserContext, Page, Request, Response, sync_playwright

from .config import Config

log = logging.getLogger(__name__)


class HardStop(Exception):
    """Instagram sınırlama/doğrulama sinyali: koşu durur, yeniden denenmez."""


class LoginRequired(HardStop):
    """Oturum yok ya da düştü."""


HARD_STOP_MARKERS = (
    "please wait a few minutes before you try again",
    "challenge_required",
    "checkpoint_required",
    "feedback_required",
    "birkaç dakika bekleyip",
    "lütfen birkaç dakika",
)
HARD_STOP_MESSAGES = ("challenge_required", "checkpoint_required", "feedback_required")
# Web istemcisinin yıllardır sabit x-ig-app-id değeri; yalnızca sayfadan hiç v1 isteği yakalanamadıysa kullanılır.
V1_APP_ID_FALLBACK = "936619743392459"
V1_HEADER_KEYS = ("x-ig-app-id", "x-asbd-id", "x-ig-www-claim", "x-csrftoken", "x-requested-with", "x-web-session-id")
INTERESTING_URL_PARTS = ("/api/v1/feed/saved/", "/api/v1/media/", "/api/graphql", "/graphql/query", "/api/v1/accounts/current_user")

# Yalnızca Instagram'ın kendi hata yüzeyleri: modal/uyarı kutuları ve içerik barındırmayan kısa ara sayfalar.
# Post sayfasının caption/yorum metni buraya girmez (yanlış alarm engeli).
ERROR_SURFACES_JS = """() => {
  const out = [];
  for (const el of document.querySelectorAll('[role="dialog"], [role="alert"]')) {
    const t = el.innerText || '';
    if (t.length < 600) out.push(t);
  }
  const body = document.body ? (document.body.innerText || '') : '';
  if (body.length < 600 && !document.querySelector('article')) out.push(body);
  return out;
}"""


@dataclass
class Captured:
    url: str
    status: int
    method: str
    friendly_name: str | None
    json: Any
    ts: float
    seq: int = 0
    _resp: Response | None = field(default=None, repr=False)

    def ensure_json(self) -> Any:
        if self.json is None and self._resp is not None:
            try:
                self.json = self._resp.json()
            except Exception:
                try:
                    self.json = json.loads(self._resp.text())
                except Exception:
                    self.json = None
            self._resp = None
        return self.json


def _message_hard_stop(data: Any, where: str) -> None:
    """XHR/fetch JSON gövdesindeki Instagram mesajını sınırlama sinyali olarak değerlendirir."""
    if not isinstance(data, dict):
        return
    msg = str(data.get("message") or "")
    if not msg:
        return
    if msg == "login_required":
        raise LoginRequired(f"{where}: login_required")
    low = msg.lower()
    if msg in HARD_STOP_MESSAGES or any(marker in low for marker in HARD_STOP_MARKERS):
        raise HardStop(f"{where}: '{msg[:80]}'")


class IGBrowser:
    def __init__(self, cfg: Config, headless: bool | None = None):
        self.cfg = cfg
        self.headless = bool(cfg.get("browser", "headless")) if headless is None else headless
        self._pw = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.captured: list[Captured] = []
        self._seq = 0
        self.v1_headers: dict[str, str] = {}
        self._username: str | None = cfg.get("username") or None

    # --- yaşam döngüsü ------------------------------------------------------
    def __enter__(self) -> "IGBrowser":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        self._pw = sync_playwright().start()
        channel = self.cfg.get("browser", "channel") or ""
        # Kanal başına ayrı profil: Chrome ile Playwright Chromium aynı profil dizinini asla paylaşmaz.
        profile = self.cfg.path("browser", "profile_dir") / (channel or "chromium")
        profile.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {
            "user_data_dir": str(profile),
            "headless": self.headless,
            "viewport": {"width": 1280, "height": 900},
        }
        if channel:
            kwargs["channel"] = channel
        try:
            self.context = self._pw.chromium.launch_persistent_context(**kwargs)
        except Exception as exc:
            self._pw.stop()
            self._pw = None
            raise RuntimeError(
                f"Tarayıcı açılamadı (kanal='{channel or 'chromium'}'): {exc}. "
                "Google Chrome kurulu mu? Değilse config'te browser.channel='' yapıp "
                "`.venv\\Scripts\\python -m playwright install chromium` çalıştır."
            ) from exc
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(30_000)
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)

    def close(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
            self.context = None
            self.page = None
            self._pw = None

    # --- yakalama -------------------------------------------------------------
    def _on_request(self, req: Request) -> None:
        try:
            url = req.url
            if "instagram.com" in url and "/api/v1/" in url:
                headers = req.headers
                picked = {k: v for k, v in headers.items() if k in V1_HEADER_KEYS}
                if picked:
                    self.v1_headers.update(picked)
        except Exception:
            pass

    def _on_response(self, resp: Response) -> None:
        url = resp.url
        if "instagram.com" not in url or not any(part in url for part in INTERESTING_URL_PARTS):
            return
        friendly = None
        data = None
        try:
            post_data = resp.request.post_data or ""
            m = re.search(r"fb_api_req_friendly_name=([^&]+)", post_data)
            if m:
                friendly = unquote(m.group(1))
        except Exception:
            pass
        try:
            data = resp.json()
        except Exception:
            data = None
        self._seq += 1
        self.captured.append(
            Captured(url=url, status=resp.status, method=resp.request.method, friendly_name=friendly,
                     json=data, ts=time.time(), seq=self._seq, _resp=None if data is not None else resp)
        )
        if len(self.captured) > 600:
            del self.captured[:200]

    def drain(self) -> None:
        for c in list(self.captured):
            if c.json is None and c._resp is not None:
                c.ensure_json()

    def captured_since(self, ts: float) -> list[Captured]:
        self.drain()
        return [c for c in self.captured if c.ts >= ts]

    def wait_for_capture(self, predicate: Callable[[Captured], bool], timeout: float = 20.0) -> Captured | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.drain()
            for c in list(self.captured):
                if predicate(c):
                    return c
            self.page.wait_for_timeout(500)
        return None

    # --- gezinme ------------------------------------------------------------------
    def sleep(self, key: str) -> None:
        lo, hi = self.cfg.get("pacing", key)
        time.sleep(random.uniform(float(lo), float(hi)))

    def goto(self, url: str, wait_until: str = "domcontentloaded") -> Response | None:
        self.sleep("before_nav")
        resp = self.page.goto(url, wait_until=wait_until)
        self.page.wait_for_timeout(1500)
        self.check_hard_stop(resp)
        return resp

    def ensure_on_instagram(self) -> None:
        """Sayfa içi fetch için aynı kökende olmak gerekir; gerekirse ana sayfaya gider (tek sefer)."""
        if "instagram.com" not in (self.page.url or ""):
            self.goto("https://www.instagram.com/")

    def scroll(self) -> None:
        try:
            self.page.mouse.move(640, 450)
        except Exception:
            pass
        self.page.mouse.wheel(0, random.randint(2500, 4000))
        self.sleep("between_scrolls")

    def body_text(self) -> str:
        try:
            return self.page.inner_text("body", timeout=3000)
        except Exception:
            return ""

    def error_surfaces(self) -> list[str]:
        try:
            return self.page.evaluate(ERROR_SURFACES_JS) or []
        except Exception:
            return []

    def check_hard_stop(self, resp: Response | None = None) -> None:
        current = self.page.url
        if any(p in current for p in ("/accounts/login", "/accounts/suspended", "/challenge/")):
            raise LoginRequired(f"Instagram girişe/doğrulamaya yönlendirdi: {current}")
        if resp is not None and resp.status == 429:
            raise HardStop("HTTP 429 (sayfa)")
        for chunk in self.error_surfaces():
            low = chunk.lower()
            for marker in HARD_STOP_MARKERS:
                if marker in low:
                    raise HardStop(f"Instagram uyarısı: '{marker}'")
        for c in list(self.captured[-30:]):
            if c.status == 429:
                raise HardStop(f"HTTP 429 (XHR) {c.url[:80]}")
            _message_hard_stop(c.ensure_json(), f"XHR {c.url[:60]}")

    # --- oturum ---------------------------------------------------------------------
    def cookies(self) -> dict[str, str]:
        return {c["name"]: c["value"] for c in self.context.cookies("https://www.instagram.com")}

    def is_logged_in(self) -> bool:
        cookies = self.cookies()
        return bool(cookies.get("sessionid")) and bool(cookies.get("ds_user_id"))

    def username(self) -> str:
        """Kullanıcı adı: config → users/{ds_user_id}/info → accounts/current_user → ana sayfadaki profil bağlantısı.
        (current_user ucu sayfa içi fetch'te 'useragent mismatch' verebiliyor; info ucu ve DOM güvenilir.)"""
        if self._username:
            return self._username
        if "instagram.com" not in self.page.url:
            self.goto("https://www.instagram.com/")
        user_id = self.cookies().get("ds_user_id")
        candidates = []
        if user_id:
            candidates.append(("users_info", f"https://www.instagram.com/api/v1/users/{user_id}/info/"))
        candidates.append(("current_user", "https://www.instagram.com/api/v1/accounts/current_user/?edit=true"))
        for source, url in candidates:
            try:
                data = self.fetch_json(url)
            except LoginRequired:
                raise
            except Exception as exc:
                log.debug("username %s hatası: %s", source, exc)
                continue
            user = (data or {}).get("user") if isinstance(data, dict) else None
            if isinstance(user, dict) and user.get("username"):
                self._username = str(user["username"])
                log.info("Kullanıcı adı %s kaynağından alındı: @%s", source, self._username)
                return self._username
            log.debug("username %s beklenmeyen yanıt: %s", source, str(data)[:160])
        try:
            name = self.page.evaluate(
                """() => {
                    for (const a of document.querySelectorAll('a[href]')) {
                        const href = a.getAttribute('href') || '';
                        const img = a.querySelector('img');
                        const alt = img ? (img.getAttribute('alt') || '') : '';
                        if (img && /^\/[A-Za-z0-9._]+\/$/.test(href) && /profile picture|profil/i.test(alt)) return href.replace(/\//g, '');
                    }
                    return null;
                }"""
            )
        except Exception:
            name = None
        if name:
            self._username = str(name)
            log.info("Kullanıcı adı DOM profil bağlantısından alındı: @%s", self._username)
            return self._username
        raise LoginRequired("Kullanıcı adı alınamadı; oturum açık değil olabilir (`login` komutunu çalıştır) "
                            "ya da config.json içine \"username\" yaz.")

    def fetch_json(self, url: str, extra_headers: dict[str, str] | None = None) -> Any:
        """Sayfa bağlamında (aynı köken, çerezlerle) GET; başlıklar sayfanın kendi isteklerinden kopyalanır."""
        headers = {
            "x-ig-app-id": self.v1_headers.get("x-ig-app-id", V1_APP_ID_FALLBACK),
            "x-csrftoken": self.cookies().get("csrftoken", ""),
            "x-requested-with": "XMLHttpRequest",
            "accept": "*/*",
        }
        for key in ("x-asbd-id", "x-ig-www-claim", "x-web-session-id"):
            if key in self.v1_headers:
                headers[key] = self.v1_headers[key]
        if extra_headers:
            headers.update(extra_headers)
        result = self.page.evaluate(
            """async ([url, headers]) => {
                const r = await fetch(url, {headers, credentials: 'include'});
                const text = await r.text();
                return {status: r.status, text};
            }""",
            [url, headers],
        )
        status, text = int(result["status"]), result["text"] or ""
        if status == 429:
            raise HardStop("HTTP 429 (fetch)")
        if status in (401, 403) and "login_required" in text:
            raise LoginRequired("fetch login_required")
        try:
            data = json.loads(text) if text else None
        except json.JSONDecodeError:
            return {"_status": status, "_raw": text[:500]}
        _message_hard_stop(data, "fetch")
        return data

    # --- sayfa verisi ------------------------------------------------------------------
    def embedded_json(self) -> list[Any]:
        texts = self.page.eval_on_selector_all(
            'script[type="application/json"]', "els => els.map(e => e.textContent)"
        )
        out = []
        for t in texts:
            if not t:
                continue
            try:
                out.append(json.loads(t))
            except json.JSONDecodeError:
                continue
        return out

    def post_links_in_dom(self) -> list[str]:
        hrefs = self.page.eval_on_selector_all(
            'a[href*="/p/"], a[href*="/reel/"]', 'els => els.map(e => e.getAttribute("href"))'
        )
        return [h for h in hrefs if h]
