from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "username": "",
    "source": "instagrapi",
    "scope": {"collections": []},
    "analysis": {"frames_speech": 2, "frames_no_speech": 4, "max_images": 6, "max_edge": 800},
    "data_dir": "data",
    "output_dir": "output",
    "browser": {"channel": "chrome", "headless": False, "profile_dir": "data/chrome-profile"},
    "pacing": {
        "before_nav": [1.5, 3.0],
        "between_scrolls": [3.0, 6.5],
        "between_posts": [5.0, 10.0],
        "between_fetches": [2.0, 4.0],
        "max_posts_per_run": 40,
        "known_pages_to_stop": 3,
    },
    "comments": {"pages": 1, "visit_post_page": False},
    "video": {"keep_files": False, "dir": "data/videos"},
    "whisper": {"model": "large-v3", "device": "cuda", "compute_type": "float16", "beam_size": 5},
    "llm": {
        "provider": "claude_code",
        "model": "",
        "effort": "",
        "exe": "",
        "api_key_env": "ANTHROPIC_API_KEY",
        "api_key": "",
        "base_url": "",
        "max_tokens": 2048,
        "timeout": 120,
    },
    "instagrapi": {"enabled": True, "username": "", "session_file": "data/instagrapi_session.json", "delay_range": [3, 7]},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Config:
    raw: dict[str, Any]
    root: Path

    @classmethod
    def load(cls, path: str | os.PathLike | None) -> "Config":
        if path is None:
            root = Path.cwd()
            data = copy.deepcopy(DEFAULTS)
        else:
            p = Path(path).resolve()
            root = p.parent
            with open(p, encoding="utf-8") as fh:
                data = _deep_merge(DEFAULTS, json.load(fh))
        return cls(raw=data, root=root)

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.raw
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def path(self, *keys: str) -> Path:
        value = self.get(*keys)
        p = Path(value)
        return p if p.is_absolute() else (self.root / p)

    @property
    def data_dir(self) -> Path:
        return self.path("data_dir")

    @property
    def output_dir(self) -> Path:
        return self.path("output_dir")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    def llm_api_key(self) -> str:
        explicit = self.get("llm", "api_key") or ""
        if explicit:
            return explicit
        env_name = self.get("llm", "api_key_env") or ""
        return os.environ.get(env_name, "") if env_name else ""
