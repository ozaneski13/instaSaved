"""faster-whisper ile yerel transkript. CUDA float16 → int8_float16 → CPU int8 sırasıyla denenir."""
from __future__ import annotations

import logging
import os
import site
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)
NO_SPEECH_MEAN_THRESHOLD = 0.6
MIN_SPEECH_CHARS = 25
CUDA_ERROR_MARKERS = ("cublas", "cudnn", "cuda")


def prepare_cuda_dlls() -> list[str]:
    """Windows: pip'in kurduğu nvidia-cublas-cu12 / nvidia-cudnn-cu12 DLL'leri PATH'te değildir;
    CTranslate2 cublas64_12.dll'i LoadLibrary ile arar. Dizinleri PATH'e ve DLL arama yoluna ekler."""
    if os.name != "nt":
        return []
    roots = [Path(p) for p in site.getsitepackages()] + [Path(sys.prefix) / "Lib" / "site-packages"]
    added: list[str] = []
    for root in roots:
        nvidia = root / "nvidia"
        if not nvidia.is_dir():
            continue
        for bin_dir in sorted(nvidia.glob("*/bin")):
            s = str(bin_dir)
            if s in added:
                continue
            try:
                os.add_dll_directory(s)
            except (AttributeError, OSError):
                pass
            os.environ["PATH"] = s + os.pathsep + os.environ.get("PATH", "")
            added.append(s)
    if added:
        log.debug("CUDA DLL dizinleri eklendi: %s", added)
    return added


@dataclass
class TranscriptResult:
    text: str
    language: str | None
    language_probability: float | None
    no_speech: bool
    mean_no_speech_prob: float
    duration: float | None
    device: str
    compute_type: str


class Transcriber:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._model = None
        self.device = None
        self.compute_type = None
        self._attempts = self._build_attempts()

    def _build_attempts(self) -> list[tuple[str, str]]:
        device = self.cfg.get("whisper", "device") or "cuda"
        ct = self.cfg.get("whisper", "compute_type") or "float16"
        attempts = [(device, ct)]
        if device == "cuda":
            attempts += [("cuda", "int8_float16"), ("cuda", "float32")]
        attempts.append(("cpu", "int8"))
        seen, out = set(), []
        for a in attempts:
            if a not in seen:
                seen.add(a)
                out.append(a)
        return out

    def _load(self) -> None:
        if self._model is not None:
            return
        prepare_cuda_dlls()
        from faster_whisper import WhisperModel

        name = self.cfg.get("whisper", "model") or "large-v3"
        last_exc: Exception | None = None
        while self._attempts:
            device, ct = self._attempts[0]
            try:
                log.info("Whisper yükleniyor: %s (%s/%s)", name, device, ct)
                self._model = WhisperModel(name, device=device, compute_type=ct)
                self.device, self.compute_type = device, ct
                return
            except Exception as exc:
                last_exc = exc
                log.warning("Whisper %s/%s yüklenemedi: %s", device, ct, exc)
                self._attempts.pop(0)
        raise RuntimeError(f"Whisper modeli yüklenemedi: {last_exc}")

    def transcribe(self, path: Path | str) -> TranscriptResult:
        self._load()
        try:
            return self._run(path)
        except Exception as exc:
            message = str(exc)
            if any(k in message.lower() for k in CUDA_ERROR_MARKERS) and len(self._attempts) > 1:
                log.warning("CUDA çalışma hatası (%s); bir sonraki compute tipine geçiliyor", message[:120])
                self._attempts.pop(0)
                self._model = None
                self._load()
                return self._run(path)
            raise

    def _run(self, path: Path | str) -> TranscriptResult:
        beam = int(self.cfg.get("whisper", "beam_size") or 5)
        segments, info = self._model.transcribe(
            str(path), language=None, vad_filter=True, beam_size=beam, condition_on_previous_text=False,
        )
        segs = list(segments)
        text = " ".join(s.text.strip() for s in segs if s.text).strip()
        probs = [float(s.no_speech_prob) for s in segs]
        mean_prob = sum(probs) / len(probs) if probs else 1.0
        no_speech = (not segs) or mean_prob > NO_SPEECH_MEAN_THRESHOLD or len(text) < MIN_SPEECH_CHARS
        return TranscriptResult(
            text=text,
            language=getattr(info, "language", None),
            language_probability=getattr(info, "language_probability", None),
            no_speech=no_speech,
            mean_no_speech_prob=mean_prob,
            duration=getattr(info, "duration", None),
            device=self.device or "?",
            compute_type=self.compute_type or "?",
        )
