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
    reasons: list[str] = field(default_factory=list)      # short labels for the clip list
    why: list[str] = field(default_factory=list)          # plain-English: what was heard / seen
    timing: list[str] = field(default_factory=list)       # plain-English: why this start and end
    kind: str = "Moment"                                  # one-word label used in the title

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


def _fmt(t: float) -> str:
    t = int(round(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


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
                    cancel: Callable[[], bool] | None = None,
                    min_len: float | None = None, max_len: float | None = None,
                    lead_in: float = 4.0, words: str = "off",
                    transcriber: Callable | None = None,
                    ai_reader: Callable | None = None,
                    guidance: str = "", on_log: Callable[[str], None] | None = None,
                    cached_blocks: list | None = None, cached_voice=None) -> AnalysisResult:
    """Find the most interesting moments.

    How it's said (always): voice pitch vs the speaker's normal, how animated the voice is,
    talking pace, laughter - plus loudness as a minor signal.
    What's said (words="candidates" or "full"): the speech is transcribed by `transcriber`
    and rated by `ai_reader` (your AI model) or, without one, an offline word list.
      candidates - the AI reads the ~20 liveliest stretches PLUS a sample every few minutes
                   across the whole stream, so calm-but-great moments get a chance too
      full       - the whole stream is transcribed and read (slowest)
    When the AI has rated passages, *content leads*: the AI's rating is the main score and
    voice/energy only break ties. `guidance` ("funny moments", "anything about X") is passed
    to the AI. `cached_blocks` / `cached_voice` reuse an earlier transcript and voice pass.

    Clips follow each moment: they start when it begins to build (plus `lead_in` seconds),
    end once it has settled, snap to pauses, and stay within min_len..max_len."""
    from . import diction, voice
    log = on_log or (lambda m: None)

    min_len = float(min_len if min_len is not None else max(5.0, clip_length * 0.6))
    max_len = float(max_len if max_len is not None else max(min_len, clip_length * 1.5))
    if max_len < min_len:
        min_len, max_len = max_len, min_len
    info = MediaInfo(src)
    dur = info.duration
    if dur <= 0:
        raise ValueError("Couldn't read the video's length.")

    def stage(lo, hi):
        if not on_progress:
            return None
        return lambda f, msg: on_progress(lo + (hi - lo) * max(0.0, f), msg)

    scenes_on = use_scenes and info.has_video
    words_on = words in ("candidates", "full") and (transcriber is not None or cached_blocks is not None)
    a_hi = 0.25 if words_on else (0.6 if scenes_on else 0.95)

    # ---- 1. how it's said
    if cached_voice is not None:
        vf = cached_voice
    elif info.has_audio:
        vf = voice.analyse(src, dur, stage(0.0, a_hi), cancel)
    else:
        z = np.zeros(int(dur / HOP) + 1, np.float32)
        vf = voice.VoiceFeatures(z - 60, np.full_like(z, np.nan), z, z, z, z)
    loud = vf.loud_db
    n = max(len(loud), 1)
    times = np.arange(n, dtype=np.float32) * HOP

    scene_t: list[float] = []
    if scenes_on:
        scene_t = scene_changes(src, dur, on_progress=stage(a_hi, a_hi + 0.1 if words_on else 0.95),
                                cancel=cancel)

    loud = np.maximum(loud, -70.0)
    talking = _smooth(vf.voiced, 2.0) > 0.15
    # loudness - kept as a minor signal
    baseline = _rolling_median(loud, 120.0)
    dev = loud - baseline
    voiced_l = loud > -55
    mad = float(np.median(np.abs(dev[voiced_l] - np.median(dev[voiced_l])))) if voiced_l.any() else 1.0
    excite = np.clip(dev / (1.4826 * max(mad, 0.5)), -2, 8)
    excite_s = _smooth(excite, 4.0)
    sustain = _smooth(np.clip(excite, 0, None), 12.0)
    short = _smooth(loud, 1.0)
    rise = np.clip(short - np.roll(_smooth(loud, 6.0), int(4 / HOP)), 0, None)
    rise[: int(6 / HOP)] = 0
    # pitch vs the speaker's normal (semitones); upbeat / excited speech goes up
    with np.errstate(invalid="ignore", divide="ignore"):
        base_pitch = voice.nan_rolling_median(vf.pitch, 300.0)
        semis = 12 * np.log2(vf.pitch / np.maximum(base_pitch, 40))
    semis = np.where(np.isfinite(semis), semis, 0.0)
    pitch_up = _smooth(np.clip(semis, 0, 12), 3.0)
    # averages over *talking time only*, so pauses between sentences don't distort them
    vw = np.clip(vf.voiced, 0, 1)
    vden = _smooth(vw, 4.0) + 1e-3
    steady = vden > 0.25                                    # at least ~1 s of speech in the 4 s window
    # animated: pitch swings vs normal
    spread_avg = _smooth(vf.spread * vw, 4.0) / vden
    spread_base = voice.nan_rolling_median(np.where(steady, spread_avg, np.nan), 300.0)
    animated = np.clip(spread_avg / np.maximum(spread_base, 0.3) - 1, 0, 3) * steady
    # pace: syllable-like bursts per second of speech vs normal
    pace_now = _smooth(vf.onsets, 4.0) / vden
    pace_base = voice.nan_rolling_median(np.where(steady, pace_now, np.nan), 300.0)
    pace = np.clip(pace_now / np.maximum(pace_base, 0.3) - 1, 0, 2) * steady
    # laughter-ish: regular rapid bursts with raised pitch
    laugh = _smooth(np.where((vf.laughish > 0.45) & (semis > 2) & (vf.voiced > 0.3), vf.laughish, 0.0), 2.0)

    scene_density = np.zeros(n, dtype=np.float32)
    if scene_t:
        idx = np.clip((np.asarray(scene_t) / HOP).astype(int), 0, n - 1)
        np.add.at(scene_density, idx, 1.0)
        scene_density = _smooth(scene_density, 10.0)

    voice_score = (1.0 * _norm01(pitch_up) + 0.45 * _norm01(animated) + 0.35 * _norm01(pace)
                   + 0.4 * _norm01(laugh))
    energy_score = 0.4 * _norm01(excite_s) + 0.2 * _norm01(sustain) + 0.25 * _norm01(rise)
    base = voice_score + energy_score + (0.3 * _norm01(scene_density) if scene_t else 0)
    base = _smooth(base, 2.0)

    # ---- 2. what's said
    spans: list = []
    word_note = ""
    blocks: list = []
    transcript = list(transcript or [])
    if words_on:
        if words == "full":
            windows = [(a, min(dur, a + 150.0)) for a in np.arange(0, dur, 150.0)]
        else:
            # the liveliest stretches...
            order = np.argsort(-base)
            taken = np.zeros(n, bool)
            peaks = []
            for i in order:
                if len(peaks) >= 20:
                    break
                if not taken[i]:
                    peaks.append(i)
                    taken[max(0, i - int(60 / HOP)): i + int(60 / HOP)] = True
            raw = [(max(0.0, i * HOP - 50), min(dur, i * HOP + 40)) for i in peaks]
            # ...plus an even sample across the whole stream, so calm parts get read too
            every = 300.0 if dur > 1800 else max(60.0, dur / 6)
            raw += [(t, min(dur, t + 90.0)) for t in np.arange(every / 2, dur, every)]
            windows = []
            for a, b in sorted(raw):
                if windows and a <= windows[-1][1]:
                    windows[-1] = (windows[-1][0], max(windows[-1][1], b))
                else:
                    windows.append((a, b))
        t_lo, t_hi = a_hi + (0.1 if scenes_on else 0), 0.75
        blocks = []
        asr_error = None
        if cached_blocks is not None:
            blocks = list(cached_blocks)
            for _, _, segs in blocks:
                transcript.extend(segs)
            log(f"Reusing the transcript from the last scan ({len(blocks)} passages).")
        else:
            total_secs = sum(b - a for a, b in windows) or 1
            log(f"🎙 Transcribing {len(windows)} passages ({total_secs / 60:.0f} min of {dur / 60:.0f} min) on this PC…")
            done_secs = 0.0
            for a, b in windows:
                if cancel and cancel():
                    raise Cancelled()
                try:
                    segs = transcriber(src, a, b, cancel=cancel,
                                   on_progress=(lambda f, m, d=done_secs, ln=b - a: on_progress(
                                       t_lo + (t_hi - t_lo) * (d + max(0, f) * ln) / total_secs,
                                       f"Transcribing what's said… ({_fmt(a)} – {_fmt(b)})")) if on_progress else None)
                except Cancelled:
                    raise
                except Exception as e:          # e.g. speech model can't download while offline
                    asr_error = str(e)
                    break
                done_secs += b - a
                transcript.extend(segs)
                blocks.append((a, b, segs))
        if asr_error:
            word_note = f"Couldn't transcribe the speech ({asr_error[:120]}) - judged by voice & energy only"
            blocks, transcript = [], []
        if asr_error:
            log(f"⚠ Couldn't transcribe: {asr_error[:160]}")
        if ai_reader is not None and blocks:
            try:
                spans = ai_reader(blocks, on_progress=stage(0.75, 0.96), cancel=cancel,
                                  guidance=guidance, on_log=log)
                word_note = "ai"
            except Cancelled:
                raise
            except Exception as e:
                word_note = f"AI unavailable ({e}) - used the built-in word list instead"
                log(f"⚠ AI unavailable: {e}. Using the built-in word list instead.")
                spans = []
        if not spans and ai_reader is None and not asr_error:
            word_note = "words"
            log("No AI model is set up (Settings → AI models), so the built-in word list judged what's said.")
    if transcript:
        spans = list(spans) + diction.lexicon_spans(transcript)

    words_curve = np.zeros(n, np.float32)
    span_at: dict[int, object] = {}
    for sp in spans:
        a, b = int(sp.start / HOP), min(n, int(sp.end / HOP) + 1)
        w = sp.score * (1.0 if sp.source == "ai" else 0.55)
        for j in range(a, b):
            if w > words_curve[j]:
                words_curve[j] = w
                span_at[j] = sp
    has_words = bool(spans)
    has_ai = any(sp.source == "ai" for sp in spans)
    if has_ai:
        # content leads: the AI's rating decides, voice/energy break ties (and still surface
        # passages the AI didn't read)
        score = 2.4 * words_curve + 0.3 * base
    elif has_words:
        score = base + 1.2 * words_curve
    else:
        score = base
    score = _smooth(score, 2.0)
    # scale to 0..1 against the very best moment (not a percentile), so the top picks keep
    # their order instead of all tying at 100
    lo_s = float(np.percentile(score, 5)) if len(score) else 0.0
    score = np.clip((score - lo_s) / max(1e-6, float(score.max()) - lo_s), 0, 1)
    if on_progress:
        on_progress(0.97, "Scoring moments…")

    # keep values for the explanations below
    kw_label: dict[int, str] = {}
    kw = words_curve

    # Peak picking with non-maximum suppression
    L = max(5.0, (min_len + max_len) / 2)
    gap_hops = int(max(min_gap, min_len * 0.75) / HOP)
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
    floor = float(np.percentile(score, 60)) if len(score) else 0.0
    highlights: list[Highlight] = []
    for i in picks:
        peak = i * HOP
        # 1) the moment itself: where the excitement is clearly above normal around the peak
        thr = max(0.45 * float(score[i]), floor)
        limit = int(max_len / HOP)
        a = i
        while a > 0 and i - a < limit and score[a - 1] > thr:
            a -= 1
        b = i
        while b < n - 1 and b - i < limit and score[b + 1] > thr:
            b += 1
        ev_start, ev_end = a * HOP, b * HOP
        ai_sp = span_at.get(i)
        if ai_sp is not None and getattr(ai_sp, "source", "") == "ai":
            ev_start, ev_end = float(ai_sp.start), max(float(ai_sp.end), float(ai_sp.start) + 3)
            timing = [f"Your AI marked {_fmt(ev_start)} – {_fmt(ev_end)} as the moment itself."]
        else:
            timing = [f"The excitement peaks at {_fmt(peak)}."]
            if ev_end - ev_start >= 2:
                timing.append(f"It starts building at {_fmt(ev_start)} and settles by {_fmt(ev_end)}.")

        # 2) add context: a lead-in so viewers see what led up to it, and a short tail
        start, end = ev_start - lead_in, ev_end + 2.0
        timing.append(f"The clip starts {lead_in:.0f} s before that so viewers get the build-up, "
                      "and runs 2 s past the end.")

        # 3) respect min / max length (extend mostly before the peak, trim mostly after it)
        length = end - start
        if length < min_len:
            extra = min_len - length
            start -= extra * 0.6
            end += extra * 0.4
            timing.append(f"Lengthened to your minimum of {min_len:.0f} s (extra time added mostly before the moment).")
        elif length > max_len:
            start = max(start, peak - 0.65 * max_len)
            end = start + max_len
            timing.append(f"Shortened to your maximum of {max_len:.0f} s, keeping the peak and what led up to it.")

        # 4) snap to natural cut points without breaking the limits
        s0, e0 = start, end
        start = _snap_to_pause(start, short)
        end = _snap_to_pause(end, short, window=3.0)
        snapped_scene = False
        if scene_arr is not None and len(scene_arr):
            near = scene_arr[np.abs(scene_arr - start) < 2.0]
            if len(near):
                start = float(near[np.argmin(np.abs(near - start))])
                snapped_scene = True
        if end - start < min_len or end - start > max_len:      # snapping broke a limit: undo it
            start, end = s0, e0
        else:
            moved = []
            if abs(start - s0) > 0.4:
                moved.append("start")
            if abs(end - e0) > 0.4:
                moved.append("end")
            if moved:
                what = " and ".join(moved)
                timing.append(f"The {what} {'were' if len(moved) > 1 else 'was'} moved to "
                              f"{'a scene cut' if snapped_scene and 'start' in moved else ('natural pauses' if len(moved) > 1 else 'a natural pause')} "
                              "so speech isn't cut mid-word.")
        # 5) keep inside the file and never cut off the moment itself
        if start < 0:
            end, start = min(dur, end - start), 0.0
        if end > dur:
            start, end = max(0.0, start - (end - dur)), dur
        end = max(end, min(dur, peak + 1.5))

        # what was heard / said / seen, in plain English
        reasons: list[str] = []
        why: list[str] = []
        kind = "Moment"
        win = slice(max(0, i - int(4 / HOP)), min(n, i + int(4 / HOP)))
        sp = next((span_at[j] for j in sorted(range(win.start, win.stop), key=lambda j: abs(j - i))
                   if j in span_at), None)
        if sp is not None:
            if sp.source == "ai":
                reasons.append(f"{sp.label}: {sp.why}"[:70])
                why.append(f"What's said - {sp.label.lower()} ({sp.score * 10:.0f}/10 from your AI): {sp.why}"
                           + (f" “{sp.quote}”" if sp.quote else ""))
                kind = sp.label
            else:
                reasons.append(sp.label)
                why.append(f"What's said - {sp.why}" + (f" “{sp.quote[:80]}”" if sp.quote else ""))
                kind = "Laughter" if sp.label == "Laughter" else kind
        pu = float(np.max(pitch_up[win]))
        if pu >= 2.0:
            reasons.append(f"Voice up {pu:.0f} semitones")
            why.append(f"Upbeat voice: pitch about {pu:.0f} semitones above their normal - sounds excited.")
            if kind == "Moment":
                kind = "Excited"
        if float(np.max(animated[win])) > 0.8:
            reasons.append("Animated delivery")
            why.append(f"Animated: the voice swings in pitch {1 + float(np.max(animated[win])):.1f}× more than usual.")
            if kind == "Moment":
                kind = "Animated"
        if float(np.max(pace[win])) > 0.4:
            why.append(f"Talking fast: about {100 * float(np.max(pace[win])):.0f}% quicker than their normal pace.")
            reasons.append("Fast talking")
        if float(np.max(laugh[win])) > 0.3:
            reasons.append("Laughter")
            why.append("Sounds like laughter: rapid, regular 'ha-ha' bursts with a raised voice.")
            if kind in ("Moment", "Excited", "Animated"):
                kind = "Laughter"
        if excite[i] > 1.5 or dev[i] > 4:
            reasons.append(f"Louder (+{max(dev[i], 0):.0f} dB)")
            why.append(f"Louder: {max(dev[i], 0):.0f} dB above the typical level of the 2 minutes around it.")
            if kind == "Moment":
                kind = "Loud moment"
        if rise[i] > 6:
            why.append(f"Sudden reaction: the volume jumped by about {rise[i]:.0f} dB within a few seconds.")
            if kind in ("Moment", "Loud moment"):
                kind = "Reaction"
        if scene_t and scene_density[i] > np.percentile(scene_density, 90):
            cuts = int(np.sum(np.abs(np.asarray(scene_t) - peak) < 6))
            reasons.append("Lots of on-screen action")
            why.append(f"Busy on screen: {cuts} visual cuts or big changes within 6 s of the peak.")
            if kind == "Moment":
                kind = "Action"
        if not reasons:
            reasons.append("Above-average energy")
            why.append("No single standout signal, but voice and energy here are livelier than most of the stream.")
        top = max(1, int(round(100.0 * float(np.mean(score > score[i])))) or 1)
        why.append(f"Overall score {score[i] * 100:.0f}/100: in the top {top}% of the whole stream.")
        # skip near-duplicates: mostly the same stretch as a better moment already found
        dup = False
        for h in highlights:
            ov = min(end, h.end) - max(start, h.start)
            if ov > 0.5 * min(end - start, h.end - h.start):
                dup = True
                break
        if dup:
            continue
        highlights.append(Highlight(start=round(start, 2), end=round(end, 2), peak=round(peak, 2),
                                    score=round(float(score[i]) * 100, 1), reasons=reasons,
                                    why=why, timing=timing, kind=kind))

    highlights.sort(key=lambda h: -h.score)
    if on_progress:
        on_progress(1.0, f"Found {len(highlights)} highlights")
    res = AnalysisResult(duration=dur, times=times, score=score, loudness=loud,
                         scene_times=scene_t, highlights=highlights, transcript=transcript or [])
    res.word_note = word_note
    res.blocks = blocks if words_on else None
    res.voice = vf
    res.guidance = guidance
    return res
