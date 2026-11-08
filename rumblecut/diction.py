"""What is said: turn a transcript into 'clip-worthy' spans with reasons.

Two scorers:
  * ai_spans       - your AI model (local Ollama / LM Studio, or Claude) reads each passage
                     and rates it: funny, hot take, shocking, heated, emotional, quotable…
  * lexicon_spans  - offline fallback: laughter, exclamations, intensifiers, strong words
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

from . import ai
from .ffmpeg_tools import Cancelled

Segment = tuple[float, float, str]


@dataclass
class Span:
    start: float
    end: float
    score: float          # 0-1
    label: str            # e.g. "Funny", "Hot take"
    why: str              # one sentence for the hover text
    quote: str = ""
    source: str = "ai"    # "ai" | "words"


def _fmt(t: float) -> str:
    t = int(round(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def _parse_t(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    parts = v.strip().split(":")
    try:
        secs = 0.0
        for p in parts:
            secs = secs * 60 + float(p)
        return secs
    except ValueError:
        return None


# --------------------------------------------------------------------------- offline lexicon
LEXICON = [
    ("Laughter", 1.0, r"\b(ha(ha)+|ha ha( ha)*|lol|lmao)\b|\(laugh|\[laugh|laughter"),
    ("Exclamation", 0.7, r"\b(no way|oh my god|oh my gosh|what the|holy (cow|crap|shit)|are you kidding|"
                          r"you('re| are) kidding|wow|let'?s go|unbelievable|insane|crazy|ridiculous)\b"),
    ("Strong words", 0.6, r"\b(liar|lies|lying|fraud|disgrace|disgusting|pathetic|destroyed|exposed|"
                           r"backfire[sd]?|shameful|outrageous|scandal|corrupt|evil|idiot|clown)\b"),
    ("Hype", 0.5, r"\b(best|worst|biggest|greatest|ever|never seen|incredible|amazing|huge|massive|"
                   r"breaking|shocking)\b"),
    ("Call-out", 0.5, r"\b(listen to this|watch this|look at this|clip (this|that|it)|mark my words|"
                       r"i('m| am) telling you|here'?s the thing)\b"),
    ("Swearing", 0.4, r"\b(f+u+c+k\w*|shit\w*|damn|hell|ass(hole)?|bullshit)\b"),
]
_LEX = [(lab, w, re.compile(p, re.I)) for lab, w, p in LEXICON]


def lexicon_spans(segments: list[Segment]) -> list[Span]:
    out: list[Span] = []
    for s, e, text in segments:
        hits, total = [], 0.0
        for lab, w, rx in _LEX:
            found = rx.findall(text)
            if found:
                m = rx.search(text)
                hits.append((lab, m.group(0)))
                total += w * min(2, len(found))
        total += 0.3 * min(3, text.count("!")) + 0.15 * min(3, text.count("?"))
        if total >= 0.5:
            words = ", ".join(f"“{h[1]}”" for h in hits[:3]) or "exclamations"
            out.append(Span(s, e, min(1.0, total / 2.5), hits[0][0] if hits else "Emphatic",
                            f"Emphatic wording: {words}.", text[:120], "words"))
    return out


# --------------------------------------------------------------------------- AI reader
SYSTEM = (
    "You are a sharp video editor finding the most clip-worthy moments in a livestream transcript. "
    "Clip-worthy means people would share it: genuinely funny lines or banter, bold or controversial "
    "takes, shocking claims or reveals, heated arguments, emotional or passionate rants, memorable "
    "quotable one-liners, or the payoff of a story. Ignore greetings, sponsor reads, housekeeping, "
    "reading out chat names and dull filler. Be strict: most passages contain nothing worth clipping. "
    "Answer only with JSON."
)

PROMPT = """Transcript excerpt (times are positions in the stream):
{lines}

Return the clip-worthy moments in this excerpt, if any, as:
{{"moments": [{{"start": "m:ss", "end": "m:ss", "score": 1-10, "type": "funny | hot take | shocking | heated | emotional | quotable | story payoff", "why": "one short sentence saying what makes it good", "quote": "the key line, max 15 words"}}]}}
Only include moments scoring 6 or more. Use start/end times from the transcript lines. Return {{"moments": []}} if nothing stands out."""


def _window_lines(segs: list[Segment]) -> str:
    return "\n".join(f"[{_fmt(s)}] {t}" for s, e, t in segs if t.strip())


def ai_spans(windows: list[tuple[float, float, list[Segment]]],
             on_progress: Callable[[float, str], None] | None = None,
             cancel: Callable[[], bool] | None = None, workers: int | None = None) -> list[Span]:
    """windows: (start, end, segments) blocks of transcript. One AI call per block."""
    if workers is None:
        workers = 4 if ai.settings()["mode"] == "cloud" else 1
    todo = [(a, b, segs) for a, b, segs in windows if any(t.strip() for _, _, t in segs)]
    spans: list[Span] = []
    errors: list[str] = []

    def one(a, b, segs):
        d = ai.complete_json(PROMPT.format(lines=_window_lines(segs)[:6000]), SYSTEM, max_tokens=600)
        found = []
        for m in d.get("moments", []) or []:
            try:
                score = float(m.get("score", 0))
            except (TypeError, ValueError):
                continue
            s, e = _parse_t(m.get("start")), _parse_t(m.get("end"))
            if s is None or score < 6:
                continue
            e = e if e is not None and e > s else s + 8
            s, e = max(a, min(s, b)), max(a, min(e, b + 5))
            label = str(m.get("type", "")).strip().split("|")[0].strip().capitalize() or "Interesting"
            found.append(Span(s, e, min(1.0, score / 10), label, str(m.get("why", "")).strip(),
                              str(m.get("quote", "")).strip()[:140], "ai"))
        return found

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(one, *w): w for w in todo}
        for f in as_completed(futs):
            if cancel and cancel():
                for x in futs:
                    x.cancel()
                raise Cancelled()
            done += 1
            try:
                spans.extend(f.result())
            except ai.AIError as e:
                errors.append(str(e))
            except Exception as e:      # a bad reply for one passage shouldn't stop the scan
                errors.append(str(e))
            if on_progress:
                on_progress(done / max(1, len(todo)), f"AI is reading what's said… ({done}/{len(todo)} passages)")
    if errors and not spans and len(errors) == len(todo):
        raise ai.AIError(errors[0])
    return spans
