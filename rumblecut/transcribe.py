"""Optional speech-to-text with faster-whisper.

Used for (a) the keyword boost in highlight detection and (b) the upcoming
Context Images feature, which needs to know what is being said and when.
If faster-whisper isn't installed the rest of the app works normally.
"""
from __future__ import annotations

from typing import Callable

from .ffmpeg_tools import Cancelled

Segment = tuple[float, float, str]


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _device() -> tuple[str, str]:
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def transcribe(src: str, model_size: str = "small", duration: float = 0.0,
               on_progress: Callable[[float, str], None] | None = None,
               cancel: Callable[[], bool] | None = None,
               start: float = 0.0, end: float | None = None) -> list[Segment]:
    if not available():
        raise RuntimeError("Transcription needs the optional 'faster-whisper' package "
                           "(pip install faster-whisper).")
    from faster_whisper import WhisperModel

    device, compute = _device()
    if on_progress:
        on_progress(0.0, f"Loading speech model ({model_size}, {device.upper()})…")
    model = WhisperModel(model_size, device=device, compute_type=compute)
    clip = [start, end] if (start or end) else None
    kwargs = {"vad_filter": True, "beam_size": 1}
    if clip:
        kwargs["clip_timestamps"] = [x for x in clip if x is not None]
    segments, info = model.transcribe(src, **kwargs)
    total = duration or getattr(info, "duration", 0) or 1.0
    out: list[Segment] = []
    for seg in segments:
        if cancel and cancel():
            raise Cancelled()
        out.append((float(seg.start), float(seg.end), seg.text.strip()))
        if on_progress:
            on_progress(min(1.0, seg.end / total), "Transcribing speech…")
    return out
