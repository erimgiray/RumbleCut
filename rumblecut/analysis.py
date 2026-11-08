"""Highlight detection for long streams.

Signals (all computed locally, no internet needed):
  * Excitement  - loudness relative to the *local* baseline (a 2-minute rolling median),
                  so a shouting streamer in a quiet segment stands out even if the
                  whole stream is loud.
  * Rise        - sudden jumps in loudness (reactions, laughter, crowd noise).
  * Scene cuts  - bursts of visual change (gameplay action, cuts between cameras).
  * Keywords    - optional; hype phrases found in the transcript ("no way", "let's go"…).

The combined score is peak-picked with a minimum gap between clips, and every clip's
start/end is nudged to the nearest pause so it doesn't cut mid-sentence.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from . import ffmpeg_tools
from .ffmpeg_tools import Cancelled, MediaInfo

HOP = 0.5           # seconds per analysis step
SR = 16000          # analysis sample rate

DEFAULT_KEYWORDS = [
    "no way", "oh my god", "let's go", "lets go", "what the", "holy", "insane", "crazy",
    "unbelievable", "are you kidding", "wow", "breaking", "huge", "incredible", "shocking",
    "listen to this", "watch this", "look at this", "clip that", "clip it", "hahaha",
]


@dataclass
class Highlight:
    start: float
    end: float
    peak: float
    score: float                     # 0-100
    reasons: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class AnalysisResult:
    duration: float
    times: np.ndarray                # seconds, one per HOP
    score: np.ndarray                # 0-1 normalised combined score
    loudness: np.ndarray             # dB
    scene_times: list[float]
    highlights: list[Highlight]
    transcript: list[tuple[float, float, str]] = field(default_factory=list)


ProgressFn = Callable[[float, str], None]


# --------------------------------------------------------------------------- signals
def audio_loudness(src: str, duration: float, on_progress: ProgressFn | None = None,
                   cancel: Callable[[], bool] | None = None) -> np.ndarray:
    """RMS loudness in dBFS per HOP, streamed so multi-hour files use little memory."""
    hop_samples = int(SR * HOP)
    proc = ffmpeg_tools.popen(
        [ffmpeg_tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", src, "-vn",
         "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )
    assert proc.stdout
    out: list[float] = []
    chunk_bytes = hop_samples * 2 * 240            # 2 minutes of audio per read
    leftover = b""
    total_hops = max(1, int(duration / HOP))
    while True:
        if cancel and cancel():
            proc.kill()
            raise Cancelled()
        buf = proc.stdout.read(chunk_bytes)
        if not buf:
            break
        buf = leftover + buf
        usable = len(buf) - (len(buf) % (hop_samples * 2))
        leftover = buf[usable:]
        a = np.frombuffer(buf[:usable], dtype=np.int16).astype(np.float32) / 32768.0
        frames = a.reshape(-1, hop_samples)
        rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
        out.extend((20 * np.log10(rms)).tolist())
        if on_progress:
            on_progress(min(1.0, len(out) / total_hops), "Listening to audio…")
    proc.wait()
    return np.asarray(out, dtype=np.float32)


def scene_changes(src: str, duration: float, threshold: float = 0.3,
                  on_progress: ProgressFn | None = None,
                  cancel: Callable[[], bool] | None = None) -> list[float]:
    """Timestamps of visual cuts, from a downscaled 4 fps pass (fast even on long streams)."""
    args = [ffmpeg_tools.ffmpeg(), "-hide_banner", "-nostats", "-hwaccel", "auto", "-i", src, "-an",
            "-vf", f"fps=4,scale=192:-2,select='gt(scene\\,{threshold})',showinfo",
            "-f", "null", "-progress", "pipe:1", "-"]
    stderr = ffmpeg_tools.run(
        args, cancel=cancel, duration=duration,
        on_progress=(lambda f: on_progress(f, "Scanning for scene changes…")) if on_progress else None,
    )
    return [float(m) for m in re.findall(r"pts_time:([0-9.]+)", stderr)]


# --------------------------------------------------------------------------- helpers
def _smooth(x: np.ndarray, seconds: float) -> np.ndarray:
    n = max(1, int(round(seconds / HOP)))
    if n <= 1 or len(x) < n:
        return x.copy()
    k = np.ones(n, dtype=np.float32) / n
    return np.convolve(x, k, mode="same")


def _rolling_median(x: np.ndarray, seconds: float) -> np.ndarray:
    n = max(3, int(round(seconds / HOP)) | 1)
    if len(x) < n:
        return np.full_like(x, np.median(x) if len(x) else 0.0)
    pad = n // 2
    xp = np.pad(x, pad, mode="edge")
    # subsample the window to keep this quick on 4h+ streams
    step = max(1, n // 60)
    win = np.lib.stride_tricks.sliding_window_view(xp, n)[:, ::step]
    return np.median(win, axis=1).astype(np.float32)[: len(x)]


def _norm01(x: np.ndarray) -> np.ndarray:
    if len(x) == 0:
        return x
    lo, hi = np.percentile(x, 5), np.percentile(x, 99.5)
    if hi - lo < 1e-6:
        return np.zeros_like(x)
    return np.clip((x - lo) / (hi - lo), 0, 1)


def _snap_to_pause(t: float, loud: np.ndarray, window: float = 2.5) -> float:
    i = int(round(t / HOP))
    w = int(window / HOP)
    lo, hi = max(0, i - w), min(len(loud), i + w + 1)
    if hi - lo < 2:
        return t
    j = lo + int(np.argmin(loud[lo:hi]))
    return j * HOP


def keyword_hits(transcript: list[tuple[float, float, str]], n_hops: int,
                 keywords: list[str]) -> tuple[np.ndarray, dict[int, str]]:
    arr = np.zeros(n_hops, dtype=np.float32)
    label: dict[int, str] = {}
    pats = [(k, re.compile(r"\b" + re.escape(k) + r"\b", re.I)) for k in keywords if k.strip()]
    for s, e, text in transcript:
        for k, p in pats:
            if p.search(text):
                a, b = int(s / HOP), min(n_hops, int(e / HOP) + 1)
                arr[a:b] += 1.0
                for i in range(a, b):
                    label.setdefault(i, k)
    return arr, label


# --------------------------------------------------------------------------- main entry
def find_highlights(src: str, num_clips: int = 10, clip_length: float = 30.0,
                    min_gap: float = 45.0, use_scenes: bool = True,
                    transcript: list[tuple[float, float, str]] | None = None,
                    keywords: list[str] | None = None,
                    on_progress: ProgressFn | None = None,
                    cancel: Callable[[], bool] | None = None) -> AnalysisResult:
    info = MediaInfo(src)
    dur = info.duration
    if dur <= 0:
        raise ValueError("Couldn't read the video's length.")

    # Stage weights for an overall progress bar
    def stage(lo, hi):
        if not on_progress:
            return None
        return lambda f, msg: on_progress(lo + (hi - lo) * f, msg)

    scenes_on = use_scenes and info.has_video
    audio_hi = 0.55 if scenes_on else 0.95

    if info.has_audio:
        loud = audio_loudness(src, dur, stage(0.0, audio_hi), cancel)
    else:
        loud = np.full(int(dur / HOP) + 1, -60.0, dtype=np.float32)
    n = max(len(loud), 1)
    times = np.arange(n, dtype=np.float32) * HOP

    scene_t: list[float] = []
    if scenes_on:
        scene_t = scene_changes(src, dur, on_progress=stage(audio_hi, 0.95), cancel=cancel)

    if on_progress:
        on_progress(0.96, "Scoring moments…")

    loud = np.maximum(loud, -70.0)
    voiced = loud > -55
    # Excitement: loudness above local baseline, scaled by robust spread
    baseline = _rolling_median(loud, 120.0)
    dev = loud - baseline
    mad = float(np.median(np.abs(dev[voiced] - np.median(dev[voiced])))) if voiced.any() else 1.0
    excite = np.clip(dev / (1.4826 * max(mad, 0.5)), -2, 8)
    excite_s = _smooth(excite, 4.0)
    sustain = _smooth(np.clip(excite, 0, None), 12.0)
    # Rise: sudden jump in short-term loudness
    short = _smooth(loud, 1.0)
    rise = np.clip(short - np.roll(_smooth(loud, 6.0), int(4 / HOP)), 0, None)
    rise[: int(6 / HOP)] = 0

    scene_density = np.zeros(n, dtype=np.float32)
    if scene_t:
        idx = np.clip((np.asarray(scene_t) / HOP).astype(int), 0, n - 1)
        np.add.at(scene_density, idx, 1.0)
        scene_density = _smooth(scene_density, 10.0)

    kw = np.zeros(n, dtype=np.float32)
    kw_label: dict[int, str] = {}
    if transcript:
        kw, kw_label = keyword_hits(transcript, n, keywords or DEFAULT_KEYWORDS)
        kw = _smooth(kw, 6.0)

    score = (1.0 * _norm01(excite_s) + 0.45 * _norm01(sustain) + 0.45 * _norm01(rise)
             + (0.35 * _norm01(scene_density) if scene_t else 0)
             + (0.8 * _norm01(kw) if transcript else 0))
    score = _smooth(score, 2.0)
    score = _norm01(score)

    # Peak picking with non-maximum suppression
    L = max(5.0, float(clip_length))
    gap_hops = int(max(min_gap, L * 0.75) / HOP)
    order = np.argsort(-score)
    taken = np.zeros(n, dtype=bool)
    picks: list[int] = []
    edge = int(min(L / 2, dur / 4) / HOP)
    for i in order:
        if len(picks) >= num_clips:
            break
        if taken[i]:
            continue
        if dur > 2 * L and (i < edge // 2):
            continue  # skip the stream's opening seconds (intros / "can you hear me")
        picks.append(int(i))
        taken[max(0, i - gap_hops): i + gap_hops + 1] = True

    scene_arr = np.asarray(scene_t) if scene_t else None
    highlights: list[Highlight] = []
    for i in picks:
        peak = i * HOP
        start = peak - 0.6 * L          # most of the clip is the build-up to the moment
        start = _snap_to_pause(start, short)
        if scene_arr is not None and len(scene_arr):
            near = scene_arr[np.abs(scene_arr - start) < 2.0]
            if len(near):
                start = float(near[np.argmin(np.abs(near - start))])
        start = max(0.0, min(start, max(0.0, dur - L)))
        end = _snap_to_pause(start + L, short, window=3.0)
        end = min(dur, max(end, start + 0.6 * L, peak + 1.5))   # never cut off the moment itself

        reasons: list[str] = []
        if excite[i] > 1.5 or dev[i] > 4:
            reasons.append(f"Volume spike (+{max(dev[i], 0):.0f} dB over normal)")
        if sustain[i] > 1.0:
            reasons.append("Sustained high energy")
        if rise[i] > 6:
            reasons.append("Sudden reaction")
        if scene_t and scene_density[i] > np.percentile(scene_density, 90):
            reasons.append("Lots of on-screen action")
        if i in kw_label or (transcript and kw[i] > 0):
            lab = kw_label.get(i) or next((kw_label[j] for j in range(i - 12, i + 12) if j in kw_label), None)
            if lab:
                reasons.append(f'Said "{lab}"')
        if not reasons:
            reasons.append("Above-average energy")
        highlights.append(Highlight(start=round(start, 2), end=round(end, 2), peak=round(peak, 2),
                                    score=round(float(score[i]) * 100, 1), reasons=reasons))

    highlights.sort(key=lambda h: -h.score)
    if on_progress:
        on_progress(1.0, f"Found {len(highlights)} highlights")
    return AnalysisResult(duration=dur, times=times, score=score, loudness=loud,
                          scene_times=scene_t, highlights=highlights, transcript=transcript or [])
