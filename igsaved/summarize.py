"""Takılıp çıkarılabilir LLM analiz sağlayıcısı (metin + görsel).

config.json → "llm": {"provider": ...}
  "claude_code"        → Claude Code CLI `claude -p` — Claude Max/Pro aboneliği, API anahtarı gerekmez.
                         Görseller Read aracıyla okunur. Ayarlar: model ("opus"/"sonnet"), effort, exe.
  "anthropic"          → resmî anthropic SDK; ANTHROPIC_API_KEY (ya da llm.api_key). model örn. "claude-opus-5".
  "openai_compatible"  → {base_url}/chat/completions; OpenAI, Groq, DeepSeek, Gemini-compat, Ollama (vision model gerekir).
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Protocol, Sequence

import requests

from .config import Config

log = logging.getLogger(__name__)

SYSTEM_TR = (
    "Sen Instagram gönderilerini analiz eden bir asistansın. Sana gönderinin türü (fotoğraf, karusel ya da video), "
    "açıklaması (caption), varsa konuşma transkripti ve gönderiden alınan görseller/kareler verilir. Görevin: "
    "gönderide ne anlatıldığını ve gösterildiğini 2-4 cümleyle, Türkçe, sade ve bilgi odaklı özetlemek: konu, "
    "gösterilen yer/ürün/kişi/olay, verilen bilgi ya da tavsiye, görsellerdeki önemli yazılar (varsa kısaca). "
    "Yalnızca özeti yaz; başlık, madde işareti, giriş cümlesi, emoji ekleme. Transkript ya da yazılar başka dildeyse "
    "yine Türkçe özetle. Görselleri göremiyorsan bunu tek kelimeyle belirt ve açıklamaya dayan."
)
NO_SPEECH_TEXT = "Konuşma yok (müzik/sessiz video)."
NO_VIDEO_TEXT = "Video yok (fotoğraf/karusel)."


class ProviderUnavailable(RuntimeError):
    pass


class LLMProvider(Protocol):
    name: str

    def complete(self, system: str, user: str, images: Sequence[Path] = ()) -> str: ...


def _b64(path: Path) -> str:
    return base64.standard_b64encode(Path(path).read_bytes()).decode("ascii")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, api_key: str, max_tokens: int = 2048, timeout: float = 120.0):
        import anthropic

        self.model = model
        self.max_tokens = max_tokens
        self.client = anthropic.Anthropic(api_key=api_key or None, timeout=timeout)

    def complete(self, system: str, user: str, images: Sequence[Path] = ()) -> str:
        content: list[dict] = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(p)}} for p in images
        ]
        content.append({"type": "text", "text": user})
        response = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system,
            messages=[{"role": "user", "content": content}],
        )
        if response.stop_reason == "refusal":
            log.warning("Anthropic yanıtı reddetti (stop_reason=refusal)")
            return ""
        return "".join(block.text for block in response.content if block.type == "text").strip()


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(self, model: str, base_url: str, api_key: str = "", max_tokens: int = 2048, timeout: float = 120.0):
        if not base_url:
            raise ProviderUnavailable("openai_compatible için llm.base_url gerekli (örn. http://localhost:11434/v1)")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.timeout = timeout

    def complete(self, system: str, user: str, images: Sequence[Path] = ()) -> str:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key or 'none'}"}
        if images:
            user_content: list[dict] | str = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64(p)}"}} for p in images
            ]
        else:
            user_content = user
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_content}],
            "temperature": 0.3,
            "max_tokens": self.max_tokens,
        }
        r = requests.post(f"{self.base_url}/chat/completions", headers=headers, json=payload, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        return (data["choices"][0]["message"].get("content") or "").strip()


class ClaudeCodeProvider:
    """Claude Code CLI (`claude -p`): kullanıcının Claude Max/Pro girişiyle çalışır, API anahtarı gerekmez.
    Görseller varsa yalnızca Read aracı açılır ve model dosyaları okuyup inceler."""

    name = "claude_code"

    def __init__(self, model: str = "", exe: str = "", timeout: float = 240.0, effort: str = ""):
        import shutil

        self.exe = exe or shutil.which("claude") or shutil.which("claude.cmd") or ""
        if not self.exe:
            raise ProviderUnavailable("`claude` komutu bulunamadı (npm i -g @anthropic-ai/claude-code) ya da config'te llm.exe ver.")
        self.model = model
        self.timeout = timeout
        self.effort = effort

    def complete(self, system: str, user: str, images: Sequence[Path] = ()) -> str:
        import os
        import subprocess

        tools = "Read" if images else ""
        # --bare KULLANILMAZ: o modda CLI kayıtlı claude.ai girişini okumuyor ("Not logged in").
        cmd = [self.exe, "-p", "--output-format", "text", "--no-session-persistence",
               "--tools", tools, "--disable-slash-commands", "--system-prompt", system]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort:
            cmd += ["--effort", self.effort]
        if images:
            listing = "\n".join(f"{i + 1}) {Path(p).resolve()}" for i, p in enumerate(images))
            user = f"{user}\n\nGörsel dosyaları (her birini Read aracıyla aç ve incele, sonra yanıtı yaz):\n{listing}"
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}  # iç içe oturum korumasını tetiklemesin
        r = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=self.timeout)
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            err = (r.stderr or out or "").strip()[:300]
            if "Not logged in" in (out + err):
                raise ProviderUnavailable("Claude Code CLI oturumu yok: bir terminalde `claude` yazıp `/login` ile "
                                          "Claude hesabına bir kez giriş yap; sonra tekrar çalıştır.")
            raise RuntimeError(f"claude -p başarısız (rc={r.returncode}): {err}")
        return out


def make_provider(cfg: Config) -> LLMProvider:
    provider = (cfg.get("llm", "provider") or "claude_code").lower()
    model = cfg.get("llm", "model") or ""
    max_tokens = int(cfg.get("llm", "max_tokens") or 2048)
    timeout = float(cfg.get("llm", "timeout") or 120)
    if provider == "claude_code":
        return ClaudeCodeProvider(model=model, exe=cfg.get("llm", "exe") or "",
                                  timeout=max(timeout, 240.0), effort=cfg.get("llm", "effort") or "")
    if provider == "anthropic":
        key = cfg.llm_api_key()
        if not key:
            raise ProviderUnavailable(
                f"Anthropic anahtarı yok: {cfg.get('llm', 'api_key_env')} ortam değişkenini ayarla ya da llm.api_key yaz."
            )
        return AnthropicProvider(model=model or "claude-opus-5", api_key=key, max_tokens=max_tokens, timeout=timeout)
    if provider == "openai_compatible":
        return OpenAICompatibleProvider(
            model=model, base_url=cfg.get("llm", "base_url") or "", api_key=cfg.llm_api_key(),
            max_tokens=max_tokens, timeout=timeout,
        )
    raise ProviderUnavailable(f"Bilinmeyen llm.provider: {provider} (claude_code | anthropic | openai_compatible)")


def build_user_prompt(kind: str, author: str | None, caption: str | None, transcript: str, language: str | None,
                      n_images: int = 0, note: str = "") -> str:
    parts = [f"Gönderi türü: {kind}"]
    if author:
        parts.append(f"Hesap: @{author}")
    parts.append(f"Açıklama (caption):\n{caption.strip() if caption else '(yok)'}")
    if kind.startswith("video") or (transcript and transcript.strip()):
        parts.append(f"Konuşma transkripti (dil: {language or 'bilinmiyor'}):\n{transcript.strip() if transcript else '(konuşma yok)'}")
    if n_images:
        parts.append(f"Ekli görsel sayısı: {n_images}" + (f" ({note})" if note else ""))
    parts.append("Gönderide ne anlatılıyor / gösteriliyor? 2-4 cümle, Türkçe.")
    return "\n\n".join(parts)


def analyze(provider: LLMProvider, kind: str, author: str | None, caption: str | None, transcript: str,
            language: str | None, images: Sequence[Path] = (), note: str = "") -> str:
    return provider.complete(SYSTEM_TR, build_user_prompt(kind, author, caption, transcript, language, len(images), note), images)


def summarize(provider: LLMProvider, author: str | None, caption: str | None, transcript: str, language: str | None) -> str:
    """Geriye dönük uyumluluk: yalnızca metinle video özeti."""
    return analyze(provider, "video", author, caption, transcript, language)
