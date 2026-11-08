"""How things are said: voice features measured from the audio, every 0.5 s.

Everything is relative to the speaker's own normal (a rolling 5-minute baseline), so a
naturally high-pitched or fast-talking streamer isn't flagged all the time.

  loud_db    loudness (dBFS)
  pitch      median voice pitch (Hz) of the voiced frames, NaN when nobody is talking
  spread     how much the pitch swings inside the half-second (semitones) - "animated"
  voiced     share of the half-second that is voiced speech
  onsets     syllable-like energy bursts per second - talking speed
  laughish   rapid, regular 'ha-ha-ha' bursts with raised pitch - possible laughter

Pitch uses a fast autocorrelation tracker on 40 ms frames, computed in 2-minute chunks
while the audio streams in, so a 4-hour stream needs little memory.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable

import numpy as np

from . import ffmpeg_tools
from .ffmpeg_tools import Cancelled

SR = 16000
HOP = 0.5                      # seconds per analysis step (matches analysis.HOP)
FRAME = 640                    # 40 ms
STEP = 320                     # 20 ms -> 25 frames per hop
NFFT = 2048
LAG_MIN, LAG_MAX = SR // 420, SR // 70     # 70-420 Hz covers men, women, shouting


@dataclass
class VoiceFeatures:
    loud_db: np.ndarray
    pitch: np.ndarray
    spread: np.ndarray
    voiced: np.ndarray
    onsets: np.ndarray
    laughish: np.ndarray


def _frames(x: np.ndarray) -> np.ndarray:
    n = 1 + max(0, (len(x) - FRAME) // STEP)
    idx = np.arange(FRAME)[None, :] + STEP * np.arange(n)[:, None]
    return x[idx]


def _chunk_features(x: np.ndarray, hops: int) -> tuple:
    """x: float32 audio for `hops` half-seconds (plus a little overlap)."""
    fr = _frames(x)                                                # (F, 640)
    if len(fr) == 0:
        z = np.zeros(hops, np.float32)
        return z - 70, np.full(hops, np.nan, np.float32), z, z, z, z
    win = np.hanning(FRAME).astype(np.float32)
    frw = (fr - fr.mean(axis=1, keepdims=True)) * win
    energy = np.sqrt(np.mean(fr * fr, axis=1) + 1e-12)
    edb = 20 * np.log10(energy)
    spec = np.fft.rfft(frw, NFFT, axis=1)
    ac = np.fft.irfft(np.abs(spec) ** 2, NFFT, axis=1)[:, :LAG_MAX + 2]
    ac0 = ac[:, :1] + 1e-9
    r = ac / ac0
    # correct for the window's own taper so long lags aren't penalised
    wac = np.correlate(win, win, "full")[FRAME - 1:FRAME - 1 + LAG_MAX + 2]
    r = r / np.maximum(wac / wac[0], 0.05)
    seg = r[:, LAG_MIN:LAG_MAX]
    best = np.argmax(seg, axis=1)
    rmax = seg[np.arange(len(seg)), best]
    # prefer the shortest lag that is nearly as strong (avoids octave-down errors)
    thr = (0.88 * rmax)[:, None]
    first = np.argmax(seg >= thr, axis=1)
    lag = (first + LAG_MIN).astype(np.float32)
    # parabolic refinement
    li = np.clip(first + LAG_MIN, 1, r.shape[1] - 2)
    a, b, c = r[np.arange(len(r)), li - 1], r[np.arange(len(r)), li], r[np.arange(len(r)), li + 1]
    denom = (a - 2 * b + c)
    with np.errstate(invalid="ignore", divide="ignore"):
        lag = lag + np.where(np.abs(denom) > 1e-6, 0.5 * (a - c) / denom, 0.0)
    f0 = SR / np.maximum(lag, 1)
    loud_floor = max(-50.0, float(np.percentile(edb, 30)) + 6)
    voiced_fr = (rmax > 0.45) & (edb > loud_floor)

    per = int(round(HOP * SR / STEP))                              # 25 frames per hop
    out = [np.zeros(hops, np.float32) for _ in range(6)]
    loud_h, pitch_h, spread_h, voiced_h, onset_h, laugh_h = out
    pitch_h[:] = np.nan
    # syllable onsets: rises in a smoothed energy envelope
    env = np.convolve(edb, np.ones(3) / 3, mode="same")
    rise = np.diff(env, prepend=env[0])
    onset = (rise > 2.5) & (np.roll(rise, 1) <= 2.5) & voiced_fr
    for h in range(hops):
        a0, a1 = h * per, min(len(fr), (h + 1) * per)
        if a1 <= a0:
            loud_h[h] = -70
            continue
        e = energy[a0:a1]
        loud_h[h] = 20 * np.log10(np.sqrt(np.mean(e * e)) + 1e-9)
        v = voiced_fr[a0:a1]
        voiced_h[h] = v.mean()
        onset_h[h] = onset[a0:a1].sum() / HOP
        if v.sum() >= 4:
            semis = 12 * np.log2(f0[a0:a1][v] / 100.0)
            med = float(np.median(semis))
            semis = semis[np.abs(semis - med) < 7]          # drop octave jumps / tracker glitches
            if len(semis) >= 4:
                pitch_h[h] = float(100.0 * 2 ** (med / 12))
                q75, q25 = np.percentile(semis, [75, 25])
                spread_h[h] = float(min(6.0, (q75 - q25) / 1.35))   # robust pitch swing
        # laughter-ish: regular 4-7 Hz bursts over ~1.5 s, mostly voiced
        b0, b1 = max(0, a0 - per), min(len(fr), a1 + per)
        envw = env[b0:b1] - env[b0:b1].mean()
        if len(envw) > 40 and voiced_fr[b0:b1].mean() > 0.3:
            acw = np.correlate(envw, envw, "full")[len(envw) - 1:]
            acw = acw / (acw[0] + 1e-9)
            lags = slice(7, 13)                                   # 140-260 ms = 4-7 Hz
            laugh_h[h] = float(max(0.0, acw[lags].max()))
    return loud_h, pitch_h, spread_h, voiced_h, onset_h, laugh_h


def analyse(src: str, duration: float, on_progress: Callable[[float, str], None] | None = None,
            cancel: Callable[[], bool] | None = None) -> VoiceFeatures:
    proc = ffmpeg_tools.popen(
        [ffmpeg_tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", src, "-vn",
         "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )
    assert proc.stdout
    hop_samples = int(SR * HOP)
    chunk_hops = 240                                   # 2 minutes
    pad = FRAME                                        # overlap so frames don't fall off the edge
    parts = [[] for _ in range(6)]
    buf = np.zeros(0, np.float32)
    total_hops = max(1, int(duration / HOP))
    done = 0
    eof = False
    while not eof:
        if cancel and cancel():
            proc.kill()
            raise Cancelled()
        need = chunk_hops * hop_samples + pad - len(buf)
        raw = proc.stdout.read(max(0, need) * 2)
        if not raw:
            eof = True
        if raw:
            buf = np.concatenate([buf, np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0])
        hops = min(chunk_hops, len(buf) // hop_samples) if not eof else int(np.ceil(len(buf) / hop_samples))
        if hops <= 0:
            continue
        seg = buf[: hops * hop_samples + pad]
        if len(seg) < hops * hop_samples + pad:
            seg = np.pad(seg, (0, hops * hop_samples + pad - len(seg)))
        for lst, arr in zip(parts, _chunk_features(seg, hops)):
            lst.append(arr)
        buf = buf[hops * hop_samples:]
        done += hops
        if on_progress:
            on_progress(min(1.0, done / total_hops), "Listening to the voice: pitch, pace and energy…")
    proc.wait()
    cat = [np.concatenate(p) if p else np.zeros(0, np.float32) for p in parts]
    return VoiceFeatures(*cat)


# --------------------------------------------------------------------------- relative-to-normal helpers
def nan_rolling_median(x: np.ndarray, seconds: float) -> np.ndarray:
    """Rolling median that ignores NaN (unvoiced) values; falls back to the global median."""
    n = len(x)
    if n == 0:
        return x
    glob = np.nanmedian(x) if np.isfinite(x).any() else 0.0
    w = max(3, int(seconds / HOP))
    step = max(1, w // 40)
    out = np.empty(n, np.float32)
    # evaluate on a coarse grid and interpolate - fast on multi-hour streams
    grid = np.arange(0, n, max(1, int(30 / HOP)))
    vals = []
    for g in grid:
        a, b = max(0, g - w // 2), min(n, g + w // 2)
        seg = x[a:b:step]
        seg = seg[np.isfinite(seg)]
        vals.append(np.median(seg) if len(seg) > 10 else glob)
    out[:] = np.interp(np.arange(n), grid, vals)
    return out
