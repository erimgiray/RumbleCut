"""What is said: turn a transcript into 'clip-worthy' spans with reasons.

Two scorers:
  * ai_spans       - your AI model (local Ollama / LM Studio, or Claude) reads each passage
                     and rates it: funny, hot take, shocking, heated, emotional, quotable…
  * lexicon_spans  - offline fallback: laughter, exclamations, intensifiers, strong words
"""
from __future__ import annotations

import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
    context_start: float | None = None   # where a viewer has to start watching to follow it
    setup: str = ""       # what that context is, e.g. "the viewer's question about tariffs"


@dataclass
class Section:
    """A stretch of the stream as the AI summarised it, e.g. 'argues with a viewer about tariffs'."""
    start: float
    end: float
    summary: str
    interest: float       # 0-1: how much viewers would care (filler, transitions, coughing = low)

    def as_dict(self) -> dict:
        return {"start": round(self.start, 2), "end": round(self.end, 2), "summary": self.summary,
                "interest": round(self.interest, 2)}


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
    "You are a sharp video editor going through a livestream transcript. First you note what each "
    "section is about, then you pick the clip-worthy moments. Clip-worthy means people would share it: "
    "genuinely funny lines or banter, bold or controversial takes, shocking claims or reveals, heated "
    "arguments, emotional or passionate rants, memorable quotable one-liners, or the payoff of a story. "
    "Ignore greetings, sponsor reads, housekeeping, reading out chat names, technical problems, "
    "transitions between topics, coughing, pauses and dull filler. Be strict: most passages contain "
    "nothing worth clipping. Every clip must make sense to someone who hasn't seen the stream: it "
    "starts where the thought or exchange starts (the question being answered, the claim being "
    "responded to, the start of the story or argument), never mid-sentence or mid-argument, and it "
    "ends once the point has landed (the punchline, the conclusion, the reaction). Answer only with JSON."
)

PROMPT = """{guidance}Transcript excerpt (times are positions in the stream):
{lines}

1. Split the excerpt into its sections - a new topic, story, segment or conversation starts a new section (usually 1-4 per excerpt). For each, say in one short sentence what happens, like "argues with a viewer about tariffs" or "reacts to the Vivek ad and mocks it". Rate how interesting each section is for viewers from 1-10{fit}; transitions, technical issues, reading chat names, ads and filler get 1-3.
2. Pick the clip-worthy moments, if any. Only include moments scoring 6 or more{fit}. For each, think about its rhetorical structure:
   - "context_start": the line where a viewer must start watching to follow it - where the question, claim, topic or story it depends on is first raised. It can be well before the moment itself; keep it as late as possible while still making sense on its own.
   - "start": where the moment itself (the line that makes it good) begins.
   - "end": the line where the point has fully landed - the punchline, the conclusion, or the reaction to it. Don't stop mid-argument.
   - "setup": in a few words, what the viewer hears first, e.g. "a viewer asks about tariffs".

Answer as:
{{"sections": [{{"start": "m:ss", "end": "m:ss", "summary": "what happens, one sentence", "interest": 1-10}}], "moments": [{{"context_start": "m:ss", "start": "m:ss", "end": "m:ss", "setup": "what the viewer hears first", "score": 1-10, "type": "funny | hot take | shocking | heated | emotional | quotable | story payoff", "why": "one short sentence saying what makes it good", "quote": "the key line, max 15 words"}}]}}
Use start/end times from the transcript lines. Use "moments": [] if nothing stands out."""

MERGE_PROMPT = """Below are consecutive sections of one livestream, each summarised separately. Some neighbouring sections are really the same conversation or topic continuing. Group neighbours that belong together and write one summary for each group.

{lines}

Answer as {{"groups": [{{"first": 1, "last": 3, "summary": "one sentence, what happens", "interest": 1-10}}]}} covering every section once, in order. Only group sections that are next to each other."""


def _window_lines(segs: list[Segment]) -> str:
    return "\n".join(f"[{_fmt(s)}] {t}" for s, e, t in segs if t.strip())


def ai_spans(windows: list[tuple[float, float, list[Segment]]],
             on_progress: Callable[[float, str], None] | None = None,
             cancel: Callable[[], bool] | None = None, workers: int | None = None,
             guidance: str = "", on_log: Callable[[str], None] | None = None,
             show_time: Callable[[float], str] | None = None,
             sections_out: list | None = None, raw_sections_out: list | None = None,
             joins: list[float] | None = None) -> list[Span]:
    """windows: (start, end, segments) blocks of transcript. One AI call per block.
    If `sections_out` is a list, the AI's section summaries (merged across neighbouring passages)
    are appended to it."""
    if workers is None:
        workers = 4 if ai.settings()["mode"] == "cloud" else 1
    st = show_time or _fmt          # how times are shown in the log (stream time, not file time)
    todo = [(a, b, segs) for a, b, segs in windows if any(t.strip() for _, _, t in segs)]
    spans: list[Span] = []
    sections: list[Section] = []
    errors: list[str] = []

    def one(a, b, segs):
        g = guidance.strip()
        prompt = PROMPT.format(
            guidance=(f"THE EDITOR IS LOOKING FOR: {g}\nPrefer moments that match this; rate others lower.\n\n"
                      if g else ""),
            fit=" - and how well it matches what the editor is looking for" if g else "",
            lines=_window_lines(segs)[:6000])
        d = ai.complete_json(prompt, SYSTEM, max_tokens=1400)
        secs = []
        for x in d.get("sections", []) or []:
            if not isinstance(x, dict):
                continue
            s, e = _parse_t(x.get("start")), _parse_t(x.get("end"))
            summ = str(x.get("summary", "")).strip()
            try:
                inter = float(x.get("interest", 0))
            except (TypeError, ValueError):
                inter = 0.0
            if s is None or not summ:
                continue
            e = e if e is not None and e > s else min(b, s + 30)
            s, e = max(a, min(s, b)), max(a, min(e, b))
            if e - s >= 2:
                secs.append(Section(s, e, summ[:200], max(0.0, min(1.0, inter / 10))))
        secs.sort(key=lambda x: x.start)
        # stretch so the sections cover the whole passage (small gaps between them are just pauses)
        for i, sc in enumerate(secs):
            sc.start = a if i == 0 else secs[i - 1].end
            if i == len(secs) - 1:
                sc.end = b
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
            cs = _parse_t(m.get("context_start"))
            cs = max(a, min(cs, s)) if cs is not None and cs < s - 0.5 else None
            found.append(Span(s, e, min(1.0, score / 10), label, str(m.get("why", "")).strip(),
                              str(m.get("quote", "")).strip()[:140], "ai", cs,
                              str(m.get("setup", "")).strip()[:100]))
        return found, secs

    done = 0
    if on_log:
        on_log(f"📖 {ai.describe()} is reading {len(todo)} passages"
               + (f" - looking for: {guidance.strip()}" if guidance.strip() else "") + "…")
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    futs = {pool.submit(one, *w): w for w in todo}
    pending = set(futs)
    try:
        while pending:
            # wake up often so Cancel works within a moment, not after the AI's current answer
            finished, pending = wait(pending, timeout=0.3, return_when=FIRST_COMPLETED)
            if cancel and cancel():
                raise Cancelled()
            for f in finished:
                done += 1
                a, b, segs = futs[f]
                try:
                    found, secs = f.result()
                    spans.extend(found)
                    sections.extend(secs)
                    if on_log:
                        for sc in secs:
                            on_log(f"📝 {st(sc.start)} – {st(sc.end)}  {sc.summary}  ({sc.interest * 10:.0f}/10)")
                        if found:
                            for sp in sorted(found, key=lambda x: x.start):
                                on_log(f"✨ {st(sp.start)}  {sp.label} {sp.score * 10:.0f}/10 - {sp.why}"
                                       + (f"  “{sp.quote}”" if sp.quote else ""))
                        else:
                            on_log(f"·  {st(a)} – {st(b)}: nothing stood out")
                except ai.AIError as e:
                    errors.append(str(e))
                    if on_log:
                        on_log(f"⚠  {st(a)} – {st(b)}: {e}")
                except Exception as e:      # a bad reply for one passage shouldn't stop the scan
                    errors.append(str(e))
                    if on_log:
                        on_log(f"⚠  {st(a)} – {st(b)}: couldn't read the AI's answer ({e})")
                if on_progress:
                    on_progress(done / max(1, len(todo)), f"AI is reading what's said… ({done}/{len(todo)} passages)")
    finally:
        # don't wait for an answer that's still coming in after Cancel - just stop asking for more
        pool.shutdown(wait=False, cancel_futures=True)
    if errors and not spans and not sections and len(errors) == len(todo):
        raise ai.AIError(errors[0])
    sections.sort(key=lambda x: x.start)
    if raw_sections_out is not None:                # per passage - finer grained, used for ranking
        raw_sections_out.extend(Section(x.start, x.end, x.summary, x.interest) for x in sections)
    if sections_out is not None:
        try:
            sections = merge_sections(sections, cancel=cancel, on_log=on_log, show_time=st, joins=joins)
        except Cancelled:
            raise
        except Exception as e:      # merging is a nicety - keep the per-passage summaries
            if on_log:
                on_log(f"⚠  Couldn't join neighbouring summaries ({e}) - showing them passage by passage")
        sections_out.extend(sections)
    return spans


def merge_sections(sections: list[Section], cancel: Callable[[], bool] | None = None,
                   on_log: Callable[[str], None] | None = None,
                   show_time: Callable[[float], str] | None = None, gap: float = 20.0,
                   joins: list[float] | None = None) -> list[Section]:
    """Join neighbouring sections that continue the same conversation (one AI call per run of
    touching sections), so the summary reads like '1:00:01 - 1:14:00: argues with a viewer…'."""
    st = show_time or _fmt
    secs = sorted(sections, key=lambda x: x.start)
    runs: list[list[Section]] = []
    for sc in secs:
        prev = runs[-1][-1] if runs else None
        crosses = prev is not None and any(prev.end - 1 <= j <= sc.start + 1 for j in (joins or []))
        if prev is not None and sc.start - prev.end <= gap and not crosses:
            runs[-1].append(sc)
        else:
            runs.append([sc])
    out: list[Section] = []
    merged_any = 0
    for run in runs:
        if len(run) < 2:
            out.extend(run)
            continue
        for k in range(0, len(run), 30):        # keep each request small
            if cancel and cancel():
                raise Cancelled()
            chunk = run[k:k + 30]
            if len(chunk) < 2:
                out.extend(chunk)
                continue
            lines = "\n".join(f"{i + 1}. [{st(x.start)} - {st(x.end)}] {x.summary} (interest {x.interest * 10:.0f})"
                              for i, x in enumerate(chunk))
            try:
                d = ai.complete_json(MERGE_PROMPT.format(lines=lines), SYSTEM, max_tokens=900)
            except ai.AIError:
                out.extend(chunk)
                continue
            groups, nxt = [], 1
            for g in d.get("groups", []) or []:
                try:
                    f, l = int(g.get("first")), int(g.get("last"))
                    inter = float(g.get("interest", 0))
                except (TypeError, ValueError, AttributeError):
                    groups = None
                    break
                if f != nxt or l < f or l > len(chunk):
                    groups = None
                    break
                summ = str(g.get("summary", "")).strip() or chunk[f - 1].summary
                groups.append(Section(chunk[f - 1].start, chunk[l - 1].end, summ[:200],
                                      max(0.0, min(1.0, inter / 10 if inter else
                                                   max(x.interest for x in chunk[f - 1:l])))))
                nxt = l + 1
            if not groups or nxt != len(chunk) + 1:
                out.extend(chunk)               # the answer didn't add up - keep them as they were
                continue
            merged_any += len(chunk) - len(groups)
            out.extend(groups)
    if on_log and merged_any:
        on_log(f"🧩 Joined {merged_any} neighbouring summaries that were the same conversation")
    return sorted(out, key=lambda x: x.start)
