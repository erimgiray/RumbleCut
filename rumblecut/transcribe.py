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

# Speech models, smallest first, with their one-time download size.
WHISPER_MODELS = [("tiny", "75 MB"), ("base", "145 MB"), ("small", "484 MB"),
                  ("medium", "1.5 GB"), ("large-v3", "3.1 GB")]
_model_cache: dict = {}


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


device_note = ""           # what the last model load ended up using, for the AI log
_gpu_broken = ""           # why the GPU can't be used this session (then we stay on CPU)

_GPU_ERR = ("cublas", "cudnn", "cuda", "cudart", "nvrtc")


def _is_gpu_error(e: Exception) -> bool:
    m = str(e).lower()
    return any(k in m for k in _GPU_ERR)


def _load_model(size: str, on_progress=None, force_cpu: bool = False):
    """Load Whisper on the NVIDIA GPU if it really works here, otherwise on the CPU.
    The GPU is *tested* with a tiny transcription, because missing NVIDIA libraries only
    show up when the model first runs, not when it loads."""
    global device_note, _gpu_broken
    from faster_whisper import WhisperModel
    from . import gpu_pack
    gpu_pack.activate()
    key = (size, "cpu" if force_cpu or _gpu_broken else "auto")
    if key in _model_cache:
        return _model_cache[key]
    root = str(config.app_data_dir() / "models")
    try_gpu = not force_cpu and not _gpu_broken and gpu_pack.nvidia_gpu_present() \
        and config.load_settings().get("speech_gpu", True)
    if try_gpu:
        try:
            if on_progress:
                on_progress(-1.0, f"Loading speech model '{size}' on the GPU - first use downloads it…")
            m = WhisperModel(size, device="cuda", compute_type="float16", download_root=root)
            import numpy as np
            list(m.transcribe(np.zeros(16000, np.float32), beam_size=1)[0])     # actually run it once
            device_note = "NVIDIA GPU"
            _model_cache[key] = m
            return m
        except Exception as e:      # e.g. cublas64_12.dll missing -> use the CPU
            _gpu_broken = str(e)
            key = (size, "cpu")
            if key in _model_cache:
                return _model_cache[key]
    if on_progress:
        on_progress(-1.0, f"Loading speech model '{size}' on the CPU - first use downloads it…")
    try:
        m = WhisperModel(size, device="cpu", compute_type="int8", download_root=root)
    except Exception as e:
        raise RuntimeError(f"Couldn't load the speech model: {e}") from e
    device_note = "CPU" + (" (the GPU needs the free GPU speed-up: Settings → AI models)"
                           if _gpu_broken and _is_gpu_error(Exception(_gpu_broken)) else "")
    _model_cache[key] = m
    return m


def gpu_status() -> str:
    """'' if fine, else why the GPU isn't being used."""
    return _gpu_broken


def reset() -> None:
    """Forget loaded models and GPU failures (after installing / removing the GPU speed-up)."""
    global _gpu_broken
    _model_cache.clear()
    _gpu_broken = ""


def _read_wav(path):
    """16 kHz mono 16-bit WAV -> float32 numpy array, the format Whisper expects."""
    import wave
    import numpy as np
    with wave.open(str(path), "rb") as w:
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


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
        audio = _read_wav(wav)   # pass samples directly - avoids faster-whisper's PyAV decoder (breaks on new PyAV)
        for attempt in (1, 2):
            try:
                segments, info = model.transcribe(audio, vad_filter=True, beam_size=1)
                total = (end - start) if end else (duration or getattr(info, "duration", 0) or 1.0)
                out: list[Segment] = []
                for seg in segments:
                    if cancel and cancel():
                        raise Cancelled()
                    out.append((float(seg.start) + start, float(seg.end) + start, seg.text.strip()))
                    if on_progress:
                        on_progress(min(1.0, seg.end / total), "Transcribing speech…")
                return out
            except Cancelled:
                raise
            except Exception as e:
                if attempt == 1 and _is_gpu_error(e):        # GPU failed mid-way: redo this on the CPU
                    global _gpu_broken
                    _gpu_broken = str(e)
                    model = _load_model(model_size, on_progress, force_cpu=True)
                    continue
                raise
    return []


# words said in a stretch, with their own timings (for editing a clip by its script)
Word = tuple[float, float, str]

# a "verbatim" style hint: Whisper otherwise tends to leave out "um", "uh" and false starts
VERBATIM_PROMPT = "Umm, so, uh, I- I mean, like, you know, hmm... Okay, uh, let me, let me think."


def transcribe_words(src: str, start: float, end: float, model_size: str | None = None,
                     on_progress: Callable[[float, str], None] | None = None,
                     cancel: Callable[[], bool] | None = None) -> list[Word]:
    """Every word between start and end, with start/end times in the source file's timeline."""
    if not available():
        raise RuntimeError("Speech recognition isn't included in this copy of RumbleCut. "
                           "Install the latest version from Settings → Updates.")
    model_size = model_size or config.load_settings().get("whisper_model", "small")
    model = _load_model(model_size, on_progress)
    with tempfile.TemporaryDirectory(prefix="rumblecut_asr_") as tmp:
        wav = Path(tmp) / "audio.wav"
        ft.run([ft.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}",
                "-t", f"{max(0.1, end - start):.3f}", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(wav)], cancel=cancel)
        audio = _read_wav(wav)
        for attempt in (1, 2):
            try:
                segments, _ = model.transcribe(audio, word_timestamps=True, vad_filter=False, beam_size=1,
                                               initial_prompt=VERBATIM_PROMPT, condition_on_previous_text=False)
                out: list[Word] = []
                total = max(0.1, end - start)
                for seg in segments:
                    if cancel and cancel():
                        raise Cancelled()
                    for w in (seg.words or []):
                        txt = str(w.word).strip()
                        if txt:
                            out.append((float(w.start) + start, float(w.end) + start, txt))
                    if on_progress:
                        on_progress(min(1.0, seg.end / total), "Writing out every word…")
                return out
            except Cancelled:
                raise
            except Exception as e:
                if attempt == 1 and _is_gpu_error(e):
                    global _gpu_broken
                    _gpu_broken = str(e)
                    model = _load_model(model_size, on_progress, force_cpu=True)
                    continue
                raise
    return []

