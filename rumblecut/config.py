"""Settings and app folders. Settings live in %APPDATA%/RumbleCut/settings.json on Windows."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    "download_dir": "",          # empty -> ~/Videos/RumbleCut/Downloads
    "export_dir": "",            # empty -> ~/Videos/RumbleCut/Exports
    "download_quality": "1080p",
    "ffmpeg_dir": "",
    "use_gpu": True,             # NVIDIA NVENC when available
    "clip_length": 30,
    "num_clips": 10,
    "min_gap": 45,
    "scene_detection": True,
    "transcript_boost": False,   # needs optional faster-whisper
    "whisper_model": "small",
    "export_quality": "high",
    "auto_update_check": True,
    "update_repo": "erimgiray/RumbleCut",
}


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.join(Path.home(), ".config")
    d = Path(base) / "RumbleCut"
    d.mkdir(parents=True, exist_ok=True)
    return d


def videos_root() -> Path:
    return Path.home() / "Videos" / "RumbleCut"


def _settings_file() -> Path:
    return app_data_dir() / "settings.json"


_cache: dict | None = None


def load_settings() -> dict:
    global _cache
    if _cache is None:
        data = dict(DEFAULTS)
        try:
            data.update(json.loads(_settings_file().read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        _cache = data
    return _cache


def save_settings(updates: dict) -> None:
    s = load_settings()
    s.update(updates)
    try:
        _settings_file().write_text(json.dumps(s, indent=2), encoding="utf-8")
    except OSError:
        pass


def download_dir() -> Path:
    d = Path(load_settings().get("download_dir") or videos_root() / "Downloads")
    d.mkdir(parents=True, exist_ok=True)
    return d


def export_dir() -> Path:
    d = Path(load_settings().get("export_dir") or videos_root() / "Exports")
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    d = app_data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d
