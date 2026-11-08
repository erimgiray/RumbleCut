"""Speech-to-text with faster-whisper - always runs locally on this PC.

Used for hype-word detection, AI clip titles/captions and the upcoming Context Images
feature. The speech model (tiny…large-v3) downloads once on first use into
%APPDATA%/RumbleCut/models. Uses the NVIDIA GPU when its CUDA libraries are present,
otherwise the CPU (int8), which is plenty fast for clip-length audio.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Download speech models over plain HTTPS (the newer Xet transfer needs a native helper
# that's fragile inside a packaged .exe and is blocked by some networks).
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
from typing import Callable

from . import config
from . import ffmpeg_tools as ft
from .ffmpeg_tools import Cancelled

Segment = tuple[float, float, str]
_model_cache: dict = {}


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _load_model(size: str, on_progress=None):
    from faster_whisper import WhisperModel
    if size in _model_cache:
        return _model_cache[size]
    root = str(config.app_data_dir() / "models")
    attempts = []
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            attempts.append(("cuda", "float16"))
    except Exception:
        pass
    attempts.append(("cpu", "int8"))
    last = None
    for device, compute in attempts:
        try:
            if on_progress:
                on_progress(-1.0, f"Loading speech model '{size}' ({device.upper()}) - first use downloads it…")
            m = WhisperModel(size, device=device, compute_type=compute, download_root=root)
            _model_cache[size] = m
            return m
        except Exception as e:      # e.g. CUDA libraries missing -> fall back to CPU
            last = e
    raise RuntimeError(f"Couldn't load the speech model: {last}")


def transcribe(src: str, model_size: str | None = None, duration: float = 0.0,
               on_progress: Callable[[float, str], None] | None = None,
               cancel: Callable[[], bool] | None = None,
               start: float = 0.0, end: float | None = None) -> list[Segment]:
    """Transcribe a file (or just start..end of it). Times are in the source file's timeline."""
    if not available():
        raise RuntimeError("Speech recognition isn't included in this copy of RumbleCut. "
                           "Install the latest version from Settings → Updates.")
    model_size = model_size or config.load_settings().get("whisper_model", "small")
    model = _load_model(model_size, on_progress)
    with tempfile.TemporaryDirectory(prefix="rumblecut_asr_") as tmp:
        wav = Path(tmp) / "audio.wav"
        args = [ft.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
        if start:
            args += ["-ss", f"{start:.3f}"]
        if end:
            args += ["-t", f"{max(0.1, end - start):.3f}"]
        args += ["-i", src, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
        if on_progress:
            on_progress(-1.0, "Extracting audio…")
        ft.run(args, cancel=cancel)
        segments, info = model.transcribe(str(wav), vad_filter=True, beam_size=1)
        total = (end - start) if end else (duration or getattr(info, "duration", 0) or 1.0)
        out: list[Segment] = []
        for seg in segments:
            if cancel and cancel():
                raise Cancelled()
            out.append((float(seg.start) + start, float(seg.end) + start, seg.text.strip()))
            if on_progress:
                on_progress(min(1.0, seg.end / total), "Transcribing speech…")
    return out
