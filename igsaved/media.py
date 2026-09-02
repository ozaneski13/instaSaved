"""Görsel malzeme hazırlığı: video karesi çıkarma (PyAV) ve görsel küçültme (Pillow). Dosyalar analizden sonra silinir."""
from __future__ import annotations

import logging
from pathlib import Path

from .video import download as download_file  # noqa: F401  (aynı indirici; imzalı CDN URL'leri hemen indirilir)
from .video import remove

log = logging.getLogger(__name__)
DEFAULT_MAX_EDGE = 800


def extract_frames(video_path: Path, n: int, out_dir: Path, max_edge: int = DEFAULT_MAX_EDGE) -> list[Path]:
    """Videodan zaman eksenine eşit dağılmış n kare alır (JPEG, uzun kenar max_edge)."""
    import av

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    n = max(1, int(n))
    with av.open(str(video_path)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        duration = (container.duration or 0) / 1_000_000.0
        targets = [duration * (i + 0.5) / n for i in range(n)] if duration > 0 else [0.0]
        ti = 0
        for frame in container.decode(stream):
            t = frame.time if frame.time is not None else 0.0
            if t + 1e-6 < targets[ti]:
                continue
            img = frame.to_image()
            img.thumbnail((max_edge, max_edge))
            p = out_dir / f"{video_path.stem}_f{ti}.jpg"
            img.convert("RGB").save(p, "JPEG", quality=82)
            paths.append(p)
            ti += 1
            if ti >= len(targets):
                break
    return paths


def prepare_image(src: Path, dest: Path, max_edge: int = DEFAULT_MAX_EDGE) -> Path:
    """İndirilen görseli JPEG'e çevirip küçültür (LLM'e gidecek boyut)."""
    from PIL import Image

    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((max_edge, max_edge))
        im.save(dest, "JPEG", quality=85)
    if src != dest:
        remove(src)
    return dest


def cleanup(paths: list[Path]) -> None:
    for p in paths:
        remove(p)
