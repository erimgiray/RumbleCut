"""✂ Edit a clip by its script: delete words in the text and they're cut from the video.

    transcribe_words()  -> every word with its own start/end (file time)
    cut_words()         -> removes words; cut points sit at the quietest moment between words
    quick_cleanup()     -> suggestions: "um"/"uh", stutters and repeats, long pauses, and spots where
                           someone clearly said something the speech-to-text left out (often a filler)
    ai_tighten()        -> suggestions from your AI: asides, false starts, restated sentences
    apply_cuts()        -> sets the clip's cuts and keeps pictures on the words they belong to

Cuts are stored on the clip as file-time ranges; the export stitches the kept pieces together
(editor.build_clip_command), zooming in on every other piece if the clip uses punch-in jump cuts.
"""
from __future__ import annotations

import re
import subprocess
from typing import Callable

import numpy as np

from . import ai
from . import ffmpeg_tools as ft
from .editor import Clip, kept_ranges

FILLERS = {"um", "umm", "ummm", "uh", "uhh", "uhm", "er", "erm", "ah", "ahh", "hmm", "hm", "mm", "mmm", "eh"}
PAUSE_MIN = 0.6           # pauses longer than this are offered for shortening
PAUSE_KEEP = 0.25         # ...down to this much silence


def norm(w: str) -> str:
    return re.sub(r"[^\w']", "", w.lower())


# ============================================================================ audio energy
def energy(src: str, start: float, end: float, hop: float = 0.01) -> tuple[np.ndarray, np.ndarray]:
    """(times, loudness in dB) every 10 ms for start..end of the file."""
    sr = 16000
    p = subprocess.run([ft.ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, start):.3f}",
                        "-t", f"{max(0.1, end - start):.3f}", "-i", src, "-vn", "-ac", "1", "-ar", str(sr),
                        "-f", "s16le", "-"], capture_output=True, creationflags=getattr(ft, "NO_WINDOW", 0))
    x = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    n = int(sr * hop)
    if len(x) < n:
        return np.array([start]), np.array([-90.0])
    frames = len(x) // n
    rms = np.sqrt(np.mean(x[: frames * n].reshape(frames, n) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-9)
    t = start + (np.arange(frames) + 0.5) * hop
    return t, db


class Quiet:
    """Finds the quietest moment in a window, so cuts don't clip a syllable."""

    def __init__(self, times: np.ndarray | None = None, db: np.ndarray | None = None):
        self.t, self.db = times, db

    def at(self, a: float, b: float, default: float) -> float:
        if self.t is None or b - a < 0.02:
            return default
        m = (self.t >= a) & (self.t <= b)
        if not m.any():
            return default
        ts, ds = self.t[m], self.db[m]
        lo = ds.min()
        # among the near-quietest frames, take the one closest to the middle of the window
        cand = ts[ds <= lo + 1.5]
        mid = (a + b) / 2
        return float(cand[np.argmin(np.abs(cand - mid))])

    def voiced_runs(self, a: float, b: float, floor: float, bridge: float = 0.08) -> list[tuple[float, float]]:
        """Stretches between a and b louder than `floor` dB (tiny dips bridged)."""
        if self.t is None or b <= a:
            return []
        m = (self.t >= a) & (self.t <= b)
        ts, loud = self.t[m], self.db[m] > floor
        runs, start = [], None
        for t, on in zip(ts, loud):
            if on and start is None:
                start = t
            elif not on and start is not None:
                runs.append([start, t])
                start = None
        if start is not None:
            runs.append([start, ts[-1]])
        merged = []
        for x, y in runs:
            if merged and x - merged[-1][1] <= bridge:
                merged[-1][1] = y
            else:
                merged.append([x, y])
        return [(float(x), float(y)) for x, y in merged]

    def voiced(self, a: float, b: float, floor: float) -> float:
        """Share of a window that's louder than `floor` dB."""
        if self.t is None:
            return 0.0
        m = (self.t >= a) & (self.t <= b)
        return float((self.db[m] > floor).mean()) if m.any() else 0.0


# ============================================================================ word <-> cut ranges
def _words(clip: Clip) -> list[tuple[float, float, str]]:
    return [(float(a), float(b), str(w)) for a, b, w in clip.words if b > clip.start and a < clip.end]


def word_cut_range(words, i: int, j: int, clip: Clip, quiet: Quiet) -> tuple[float, float]:
    """File-time range that removes words i..j (inclusive), cut at the quiet points around them."""
    a, b = words[i][0], words[j][1]
    if i == 0:
        left = clip.start if a - clip.start < 1.5 else quiet.at(max(clip.start, a - 0.4), a, a - 0.05)
    else:
        pe = words[i - 1][1]
        left = quiet.at(pe, a, (pe + a) / 2) if a > pe else a
    if j == len(words) - 1:
        right = clip.end if clip.end - b < 1.5 else quiet.at(b, min(clip.end, b + 0.4), b + 0.05)
    else:
        ns = words[j + 1][0]
        right = quiet.at(b, ns, (b + ns) / 2) if ns > b else b
    return max(clip.start, left), min(clip.end, max(right, left + 0.02))


def merge(ranges) -> list[list[float]]:
    out: list[list[float]] = []
    for a, b in sorted((float(a), float(b)) for a, b in ranges if b > a):
        if out and a <= out[-1][1] + 0.01:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def subtract(ranges, a: float, b: float) -> list[list[float]]:
    out = []
    for x, y in ranges:
        if y <= a or x >= b:
            out.append([x, y])
            continue
        if x < a:
            out.append([x, a])
        if y > b:
            out.append([b, y])
    return out


def is_cut(clip_or_cuts, a: float, b: float) -> bool:
    """Is (most of) a..b inside a cut?"""
    cuts = clip_or_cuts.cuts if isinstance(clip_or_cuts, Clip) else clip_or_cuts
    mid = (a + b) / 2
    return any(x <= mid <= y for x, y in cuts)


def apply_cuts(clip: Clip, new_cuts) -> None:
    """Replace the clip's cuts, keeping pictures (and their timing) on the same words."""
    anchors = [clip.to_file(ov.start) for ov in clip.overlays]
    clip.cuts = merge(new_cuts)
    for ov, src in zip(clip.overlays, anchors):
        ov.start = round(clip.to_out(src), 2)


# ============================================================================ tokens for the script view
def _speech_floor(quiet: "Quiet | None") -> float | None:
    if quiet is None or quiet.t is None or not len(quiet.db):
        return None
    return float(np.percentile(quiet.db, 70)) - 12


def tokens(clip: Clip, quiet: "Quiet | None" = None) -> list[dict]:
    """Words and gaps in order: {kind: word|pause|unheard, i (word index), start, end, text}.
    A gap is a "pause" when it's quiet, "unheard" when someone's talking but it isn't in the script."""
    ws = _words(clip)
    floor = _speech_floor(quiet)
    out = []
    prev_end = clip.start

    def pause(a, b):
        if b - a >= PAUSE_MIN:
            out.append({"kind": "pause", "start": a, "end": b, "text": f"⏸ {b - a:.1f}s"})

    def gap(a, b):
        runs = quiet.voiced_runs(a + 0.05, b - 0.05, floor) if floor is not None else []
        runs = [(x, y) for x, y in runs if y - x >= 0.25]
        pos = a
        for x, y in runs:                      # speech in the gap that isn't in the script
            pause(pos, x)
            out.append({"kind": "unheard", "start": x, "end": y, "text": f"[… {y - x:.1f}s]"})
            pos = y
        pause(pos, b)

    for i, (a, b, w) in enumerate(ws):
        if a - prev_end >= 0.35:
            gap(prev_end, a)
        out.append({"kind": "word", "i": i, "start": a, "end": b, "text": w})
        prev_end = b
    if ws and clip.end - prev_end >= 0.35:
        gap(prev_end, clip.end)
    return out


def pause_cut_range(a: float, b: float) -> tuple[float, float]:
    """Shorten a pause to PAUSE_KEEP seconds of silence (half each side)."""
    k = PAUSE_KEEP / 2
    return a + k, b - k


# ============================================================================ suggestions
def quick_cleanup(clip: Clip, quiet: Quiet) -> list[dict]:
    """No-AI suggestions: fillers, stutters/repeats, long pauses, unheard bits."""
    ws = _words(clip)
    sug: list[dict] = []
    i = 0
    while i < len(ws):
        w = norm(ws[i][2])
        if w in FILLERS or (w in ("like",) and False):
            j = i
            while j + 1 < len(ws) and norm(ws[j + 1][2]) in FILLERS:
                j += 1
            a, b = word_cut_range(ws, i, j, clip, quiet)
            sug.append({"start": a, "end": b, "kind": "filler",
                        "text": " ".join(x[2] for x in ws[i:j + 1])})
            i = j + 1
            continue
        # stutters / repeats: "I I think", "the the", "we need to, we need to"
        for n in (4, 3, 2, 1):
            if i + 2 * n <= len(ws):
                first = [norm(x[2]) for x in ws[i:i + n]]
                second = [norm(x[2]) for x in ws[i + n:i + 2 * n]]
                if first == second and all(first) and (n > 1 or len(first[0]) <= 12):
                    a, b = word_cut_range(ws, i, i + n - 1, clip, quiet)
                    sug.append({"start": a, "end": b, "kind": "repeat",
                                "text": " ".join(x[2] for x in ws[i:i + n])})
                    i += n
                    break
        else:
            i += 1
            continue
    # pauses, and speech the transcript doesn't show (Whisper tends to leave out "um"/"uh")
    for tk in tokens(clip, quiet):
        if tk["kind"] == "pause":
            a, b = pause_cut_range(tk["start"], tk["end"])
            if b - a >= 0.2:
                sug.append({"start": a, "end": b, "kind": "pause", "text": tk["text"]})
        elif tk["kind"] == "unheard" and tk["end"] - tk["start"] < 2.5:
            sug.append({"start": tk["start"] + 0.03, "end": tk["end"] - 0.03, "kind": "unheard",
                        "text": f"(something not in the script, {tk['end'] - tk['start']:.1f}s - probably a filler)"})
    # word-based suggestions win over pauses when they overlap; skip what's already cut
    rank = {"filler": 0, "repeat": 0, "unheard": 1, "pause": 2}
    out, seen = [], []
    for s_ in sorted(sug, key=lambda x: (rank.get(x["kind"], 3), x["start"])):
        if is_cut(clip, s_["start"], s_["end"]) or any(not (s_["end"] <= x["start"] or s_["start"] >= x["end"]) for x in seen):
            continue
        seen.append(s_)
        out.append({**s_, "start": round(s_["start"], 3), "end": round(s_["end"], 3)})
    out.sort(key=lambda x: x["start"])
    return out


TIGHTEN_SYSTEM = ("You are a sharp short-form video editor tightening a clip cut from a livestream. You remove "
                  "what doesn't serve the point - asides, false starts, restarted sentences, repeated "
                  "points, filler phrases ('you know what I mean', 'basically', 'kind of like') - while "
                  "keeping the setup, the meaning and the punchline intact and the speech natural. Answer "
                  "only with JSON.")

TIGHTEN_PROMPT = """{guidance}The clip's words, numbered:
{words}

Pick the stretches to cut. Rules:
- Cut whole phrases, never single words from the middle of a phrase that would make it ungrammatical.
- Keep what's needed to understand the point and keep the ending.
- Cut at most about {pct}% of the words.

Answer as {{"cuts": [{{"from": first word number, "to": last word number, "why": "a few words"}}]}}. Use "cuts": [] if it's already tight."""


def ai_tighten(clip: Clip, quiet: Quiet, guidance: str = "", max_pct: int = 25) -> list[dict]:
    ws = _words(clip)
    if len(ws) < 8:
        return []
    numbered = " ".join(f"{i}:{w}" for i, (_, _, w) in enumerate(ws)
                        if not is_cut(clip, ws[i][0], ws[i][1]))
    g = f"The editor's note: {guidance.strip()}\n\n" if guidance.strip() else ""
    d = ai.complete_json(TIGHTEN_PROMPT.format(guidance=g, words=numbered[:14000], pct=max_pct),
                         TIGHTEN_SYSTEM, max_tokens=900)
    out = []
    budget = int(len(ws) * max_pct / 100) + 2
    used = 0
    for c in d.get("cuts", []) or []:
        try:
            i, j = int(c.get("from")), int(c.get("to"))
        except (TypeError, ValueError, AttributeError):
            continue
        if not (0 <= i <= j < len(ws)) or j - i + 1 > len(ws) * 0.5:
            continue
        if used + (j - i + 1) > budget:
            break
        used += j - i + 1
        a, b = word_cut_range(ws, i, j, clip, quiet)
        out.append({"start": round(a, 3), "end": round(b, 3), "kind": "ai",
                    "text": " ".join(x[2] for x in ws[i:j + 1]), "why": str(c.get("why", ""))[:80]})
    return [s for s in out if not is_cut(clip, s["start"], s["end"])]


KIND_NAMES = {"filler": "filler word", "repeat": "repeat / stutter", "pause": "long pause",
              "unheard": "probably a filler", "ai": "your AI suggests cutting"}
