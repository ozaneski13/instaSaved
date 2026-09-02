from __future__ import annotations

import logging
from pathlib import Path

import requests

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"


def download(url: str, dest: Path, timeout: int = 60) -> Path:
    """İmzalı CDN URL'sini hemen indirir (URL süreli). .part → rename ile atomik."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=timeout, headers={"User-Agent": UA}) as r:
            r.raise_for_status()
            with open(part, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        fh.write(chunk)
        part.replace(dest)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    return dest


def remove(path: Path | str | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        log.debug("Video silinemedi %s: %s", path, exc)
