"""Context Images - COMING FEATURE.

Goal: while the streamer talks about something ("the new Tesla recall", "Zelensky's
speech", "this chart"), automatically pop a relevant picture onto the clip.

Pipeline (the render side is already live - see editor.ImageOverlay):

    clip audio ──► transcribe.py (faster-whisper, word timings, GPU)
               ──► TopicExtractor        : what is being talked about, and when
               ──► ImageProvider(s)      : find candidate pictures for each topic
               ──► ContextImagePlanner   : choose, space out and time the overlays
               ──► editor.ImageOverlay   : rendered by the normal export path

Everything below is the stable interface the feature will plug into. The first
provider (LocalFolderProvider) and a simple offline topic extractor are included so
the plumbing can be tested end-to-end; online providers (Wikimedia Commons, Pexels,
an LLM topic extractor) slot in behind the same interfaces.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .editor import Clip, ImageOverlay

FEATURE_STATUS = "coming_soon"


@dataclass
class Topic:
    text: str                 # e.g. "Tesla recall"
    start: float              # seconds, relative to clip start
    end: float
    weight: float = 1.0


@dataclass
class ImageCandidate:
    path_or_url: str
    title: str = ""
    source: str = ""          # attribution / licence note
    score: float = 0.0
    extra: dict = field(default_factory=dict)


class TopicExtractor(ABC):
    @abstractmethod
    def extract(self, segments: list[tuple[float, float, str]]) -> list[Topic]: ...


class ImageProvider(ABC):
    name = "base"

    @abstractmethod
    def search(self, query: str, limit: int = 5) -> list[ImageCandidate]: ...


# --------------------------------------------------------------------------- offline defaults
_STOP = set("""a an the and or but if then so of to in on at by for with from up down out over under
again further once here there when where why how all any both each few more most other some such no
nor not only own same than too very can will just don should now i me my we our you your he him his
she her it its they them their what which who whom this that these those am is are was were be been
being have has had having do does did doing gonna wanna yeah okay ok like know think really right
going get got one thing things people guys""".split())


class SimpleTopicExtractor(TopicExtractor):
    """Offline heuristic: capitalised phrases and repeated content words per segment."""

    def extract(self, segments):
        topics: list[Topic] = []
        for s, e, text in segments:
            caps = re.findall(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+)*)", text)
            words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", text)]
            common = [w for w, _ in Counter(w for w in words if w not in _STOP).most_common(2)]
            for c in caps[:2]:
                topics.append(Topic(c, s, e, 2.0))
            if not caps and common:
                topics.append(Topic(" ".join(common), s, e, 1.0))
        return topics


class LocalFolderProvider(ImageProvider):
    """Matches topics against image file names in a folder you choose."""
    name = "Local folder"
    EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)

    def search(self, query, limit=5):
        q = set(re.findall(r"[a-z0-9]+", query.lower()))
        hits = []
        for p in self.folder.rglob("*"):
            if p.suffix.lower() in self.EXTS:
                name = set(re.findall(r"[a-z0-9]+", p.stem.lower()))
                overlap = len(q & name)
                if overlap:
                    hits.append(ImageCandidate(str(p), p.stem, "local", overlap / max(1, len(q))))
        return sorted(hits, key=lambda h: -h.score)[:limit]


class ContextImagePlanner:
    """Turns topics + candidates into timed overlays, avoiding clutter."""

    def __init__(self, providers: list[ImageProvider], extractor: TopicExtractor | None = None,
                 min_spacing: float = 6.0, show_for: float = 3.5, max_per_clip: int = 4):
        self.providers = providers
        self.extractor = extractor or SimpleTopicExtractor()
        self.min_spacing, self.show_for, self.max_per_clip = min_spacing, show_for, max_per_clip

    def plan(self, clip: Clip, transcript: list[tuple[float, float, str]]) -> list[ImageOverlay]:
        # transcript is in stream time; keep segments inside the clip, convert to clip time
        segs = [(max(0.0, s - clip.start) / clip.speed, (min(e, clip.end) - clip.start) / clip.speed, t)
                for s, e, t in transcript if e > clip.start and s < clip.end]
        topics = sorted(self.extractor.extract(segs), key=lambda t: -t.weight)
        chosen: list[ImageOverlay] = []
        for tp in topics:
            if len(chosen) >= self.max_per_clip:
                break
            if any(abs(tp.start - o.start) < self.min_spacing for o in chosen):
                continue
            for prov in self.providers:
                cands = prov.search(tp.text, 1)
                if cands:
                    c = cands[0]
                    chosen.append(ImageOverlay(image=c.path_or_url, start=tp.start,
                                               duration=self.show_for, label=tp.text, source=c.source))
                    break
        return sorted(chosen, key=lambda o: o.start)
