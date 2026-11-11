"""Context Images: pictures that match what's being said.

While the streamer talks about something ("Vivek's attack ad", "the Strait of Hormuz", "oil
prices"), RumbleCut suggests a fitting picture for that moment. You approve, swap or remove each
suggestion; only approved pictures are rendered into the export.

    clip audio ──► transcribe.py (faster-whisper)           what is said, and when
               ──► find_topics()  (your AI, or offline)     what is worth showing, and when
               ──► providers      (Wikimedia, Pexels, web)  candidate pictures + credits
               ──► suggest_for_clip()                       timed, pending ImageOverlays
               ──► editor.ImageOverlay                      rendered by the normal export

Sources
  * Wikimedia - Wikipedia article lead images + Wikimedia Commons. Free, reuse-licensed, no key.
  * Pexels    - free stock photos (Pexels licence). Needs a free API key.
  * Web       - Brave image search. Widest coverage, but most results are copyrighted: check
                before you publish. Needs a Brave Search API key (monthly free credit).
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__, ai, config
from .editor import Clip, ImageOverlay
from .ffmpeg_tools import Cancelled

FEATURE_STATUS = "live"
USER_AGENT = f"RumbleCut/{__version__} (https://github.com/erimgiray/RumbleCut; context images)"

SOURCES = {
    "wikimedia": "Wikipedia / Wikimedia",
    "pexels": "Pexels stock photos",
    "web": "Web image search (Brave)",
}


class ImageSearchError(Exception):
    pass


# ============================================================================ data
@dataclass
class Topic:
    text: str                 # what to search for, e.g. "Vivek Ramaswamy"
    start: float              # seconds from the start of the exported clip
    end: float
    weight: float = 1.0
    label: str = ""           # what to call it on screen / in the list
    kind: str = "thing"       # person | place | organisation | event | thing | concept
    why: str = ""


@dataclass
class ImageCandidate:
    path_or_url: str          # full-size image URL (or a local path)
    title: str = ""
    source: str = ""          # provider key: wikimedia | pexels | web | local
    score: float = 0.0
    thumb_url: str = ""
    page_url: str = ""        # where it came from (for credits)
    credit: str = ""          # author / photographer
    licence: str = ""         # e.g. "CC BY-SA 4.0", "Pexels licence", "Unknown - check before reuse"
    width: int = 0
    height: int = 0
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ImageCandidate":
        known = {k: d[k] for k in ImageCandidate.__dataclass_fields__ if k in d}
        return ImageCandidate(**known)

    @property
    def credit_line(self) -> str:
        who = self.credit or "unknown author"
        where = SOURCES.get(self.source, self.source or "")
        bits = [f"“{self.title}”" if self.title else "", f"by {who}", self.licence, where, self.page_url]
        return " · ".join(b for b in bits if b)


# ============================================================================ HTTP
def _http_json(url: str, headers: dict | None = None, timeout: float = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                               **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ImageSearchError("the API key was refused - check it in Settings → Context images") from e
        if e.code == 429:
            raise ImageSearchError("too many searches right now (rate limit) - try again in a minute") from e
        raise ImageSearchError(f"the server answered {e.code}") from e
    except urllib.error.URLError as e:
        raise ImageSearchError(f"couldn't connect ({e.reason})") from e
    except (ValueError, TimeoutError) as e:
        raise ImageSearchError(f"bad answer ({e})") from e


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


# ============================================================================ keys
def _key_name(provider: str) -> str:
    return {"pexels": "pexels_api_key", "web": "brave_api_key"}[provider]


def set_key(provider: str, key: str) -> None:
    config.save_settings({_key_name(provider): ai._protect(key.strip())})


def get_key(provider: str) -> str:
    try:
        return ai._unprotect(config.load_settings().get(_key_name(provider), ""))
    except Exception:
        return ""


def enabled_sources() -> list[str]:
    s = config.load_settings().get("ctx_sources") or ["wikimedia"]
    return [x for x in s if x in SOURCES]


# ============================================================================ providers
class ImageProvider:
    name = "base"
    key = "base"

    def search(self, query: str, limit: int = 6) -> list[ImageCandidate]:
        raise NotImplementedError

    def ready(self) -> tuple[bool, str]:
        return True, ""


class WikimediaProvider(ImageProvider):
    """Wikipedia article lead images (best for people, places, organisations, events), then
    Wikimedia Commons file search. Only freely licensed images; author + licence included."""
    name = SOURCES["wikimedia"]
    key = "wikimedia"
    WIKI = "https://en.wikipedia.org/w/api.php"
    COMMONS = "https://commons.wikimedia.org/w/api.php"
    META = "Artist|LicenseShortName|ObjectName|Credit|UsageTerms"

    def search(self, query, limit=6):
        out: list[ImageCandidate] = []
        seen: set[str] = set()
        # 1) lead images of the best-matching Wikipedia articles
        q = urllib.parse.urlencode({
            "action": "query", "format": "json", "formatversion": 2, "generator": "search",
            "gsrsearch": query, "gsrlimit": 5, "prop": "pageimages|info", "piprop": "thumbnail|name",
            "pithumbsize": 1280, "pilicense": "free", "inprop": "url"})
        d = _http_json(f"{self.WIKI}?{q}")
        pages = sorted(d.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
        files = []
        for p in pages:
            th, name = p.get("thumbnail"), p.get("pageimage")
            if not th or not name or name in seen:
                continue
            seen.add(name)
            files.append(name)
            out.append(ImageCandidate(th["source"], p.get("title", ""), self.key,
                                      score=1.0 - 0.1 * (p.get("index", 1) - 1), thumb_url=th["source"],
                                      page_url=p.get("fullurl", ""), width=th.get("width", 0),
                                      height=th.get("height", 0), extra={"file": "File:" + name}))
        if files:                                    # author + licence for those files
            q = urllib.parse.urlencode({
                "action": "query", "format": "json", "formatversion": 2, "prop": "imageinfo",
                "titles": "|".join("File:" + f for f in files), "iiprop": "extmetadata|url",
                "iiextmetadatafilter": self.META})
            try:
                meta = _http_json(f"{self.WIKI}?{q}")
                info = {}
                for p in meta.get("query", {}).get("pages", []):
                    ii = (p.get("imageinfo") or [{}])[0]
                    info[p.get("title", "").replace("_", " ")] = ii
                norm = {n.get("from"): n.get("to") for n in meta.get("query", {}).get("normalized", [])}
                for c in out:
                    t = c.extra["file"]
                    ii = info.get(norm.get(t, t).replace("_", " ")) or info.get(t.replace("_", " ")) or {}
                    em = ii.get("extmetadata", {})
                    c.credit = _strip_html(em.get("Artist", {}).get("value", ""))[:120]
                    c.licence = _strip_html(em.get("LicenseShortName", {}).get("value", "")) or "free licence"
                    if ii.get("descriptionurl"):
                        c.extra["file_page"] = ii["descriptionurl"]
            except ImageSearchError:
                pass
        # 2) Wikimedia Commons file search (photos of the thing itself)
        if len(out) < limit:
            q = urllib.parse.urlencode({
                "action": "query", "format": "json", "formatversion": 2, "generator": "search",
                "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": limit,
                "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime", "iiurlwidth": 1280,
                "iiextmetadatafilter": self.META})
            try:
                d = _http_json(f"{self.COMMONS}?{q}")
            except ImageSearchError:
                d = {}
            for p in sorted(d.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99)):
                ii = (p.get("imageinfo") or [{}])[0]
                name = p.get("title", "").removeprefix("File:")
                if not ii.get("thumburl") or name in seen or not str(ii.get("mime", "")).startswith("image/"):
                    continue
                seen.add(name)
                em = ii.get("extmetadata", {})
                title = _strip_html(em.get("ObjectName", {}).get("value", "")) or Path(name).stem
                out.append(ImageCandidate(
                    ii["thumburl"], title[:100], self.key, score=0.7 - 0.05 * (p.get("index", 1) - 1),
                    thumb_url=ii.get("thumburl", ""), page_url=ii.get("descriptionurl", ""),
                    credit=_strip_html(em.get("Artist", {}).get("value", ""))[:120],
                    licence=_strip_html(em.get("LicenseShortName", {}).get("value", "")) or "free licence",
                    width=ii.get("thumbwidth", 0), height=ii.get("thumbheight", 0)))
        return out[:limit]


class PexelsProvider(ImageProvider):
    """Free stock photos - good for generic things (money, a crowd, oil rigs). Free API key."""
    name = SOURCES["pexels"]
    key = "pexels"

    def ready(self):
        return (True, "") if get_key("pexels") else (False, "add your free Pexels API key in Settings")

    def search(self, query, limit=6):
        k = get_key("pexels")
        if not k:
            raise ImageSearchError("add your free Pexels API key in Settings → Context images")
        q = urllib.parse.urlencode({"query": query, "per_page": limit, "orientation": "landscape"})
        d = _http_json(f"https://api.pexels.com/v1/search?{q}", {"Authorization": k})
        out = []
        for i, ph in enumerate(d.get("photos", [])[:limit]):
            src = ph.get("src", {})
            full = src.get("large2x") or src.get("large") or src.get("original")
            if not full:
                continue
            out.append(ImageCandidate(full, (ph.get("alt") or query)[:100], self.key, score=0.8 - 0.05 * i,
                                      thumb_url=src.get("medium") or src.get("small") or full,
                                      page_url=ph.get("url", ""), credit=ph.get("photographer", ""),
                                      licence="Pexels licence (free to use)", width=ph.get("width", 0),
                                      height=ph.get("height", 0)))
        return out


class BraveImageProvider(ImageProvider):
    """Web image search through the Brave Search API. Widest coverage (news photos, memes) but
    the images usually belong to someone - the licence is unknown."""
    name = SOURCES["web"]
    key = "web"

    def ready(self):
        return (True, "") if get_key("web") else (False, "add your Brave Search API key in Settings")

    def search(self, query, limit=6):
        k = get_key("web")
        if not k:
            raise ImageSearchError("add your Brave Search API key in Settings → Context images")
        q = urllib.parse.urlencode({"q": query, "count": max(limit, 10), "safesearch": "strict"})
        d = _http_json(f"https://api.search.brave.com/res/v1/images/search?{q}",
                       {"X-Subscription-Token": k, "Accept-Encoding": "identity"})
        out = []
        for i, r in enumerate(d.get("results", [])):
            props = r.get("properties", {}) or {}
            full = props.get("url") or (r.get("thumbnail") or {}).get("src")
            if not full:
                continue
            out.append(ImageCandidate(full, (r.get("title") or query)[:100], self.key, score=0.6 - 0.03 * i,
                                      thumb_url=(r.get("thumbnail") or {}).get("src", "") or full,
                                      page_url=r.get("url", ""), credit=r.get("source", ""),
                                      licence="Unknown - check you may reuse it",
                                      width=int(props.get("width") or 0), height=int(props.get("height") or 0)))
            if len(out) >= limit:
                break
        return out


class LocalFolderProvider(ImageProvider):
    """Matches topics against image file names in a folder you choose."""
    name = "Local folder"
    key = "local"
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
                    hits.append(ImageCandidate(str(p), p.stem, "local", overlap / max(1, len(q)),
                                               thumb_url=str(p), licence="your own file"))
        return sorted(hits, key=lambda h: -h.score)[:limit]


PROVIDERS = {"wikimedia": WikimediaProvider, "pexels": PexelsProvider, "web": BraveImageProvider}

# which sources to try first for each kind of topic
_ORDER = {
    "person": ["wikimedia", "web", "pexels"],
    "place": ["wikimedia", "pexels", "web"],
    "organisation": ["wikimedia", "web", "pexels"],
    "event": ["wikimedia", "web", "pexels"],
    "thing": ["wikimedia", "pexels", "web"],
    "concept": ["pexels", "wikimedia", "web"],
}


# ============================================================================ downloads
def cache_dir() -> Path:
    d = config.app_data_dir() / "image-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


_MAGIC = [(b"\xff\xd8\xff", ".jpg"), (b"\x89PNG", ".png"), (b"GIF8", ".gif"), (b"BM", ".bmp")]


def download(url: str, max_bytes: int = 15_000_000, timeout: float = 20) -> str:
    """Fetch an image into the cache (once) and return the local path."""
    if not url.startswith(("http://", "https://")):
        if Path(url).exists():
            return url
        raise ImageSearchError("image file not found")
    h = hashlib.sha1(url.encode()).hexdigest()[:20]
    for f in cache_dir().glob(h + ".*"):
        if f.stat().st_size:
            return str(f)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        raise ImageSearchError(f"the image server answered {e.code}") from e
    except urllib.error.URLError as e:
        raise ImageSearchError(f"couldn't download the image ({e.reason})") from e
    if len(data) > max_bytes:
        raise ImageSearchError("the image is too large")
    ext = next((e for m, e in _MAGIC if data.startswith(m)), None)
    if ext is None and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = ".webp"
    if ext is None:
        raise ImageSearchError("that link isn't a picture")
    out = cache_dir() / (h + ext)
    out.write_bytes(data)
    return str(out)


# ============================================================================ what to show
TOPIC_SYSTEM = (
    "You are a video editor adding pictures to a livestream clip, like a news edit: when the "
    "speaker mentions a specific person, place, organisation, event, product or striking thing, "
    "the viewer briefly sees it. Answer only with JSON."
)

TOPIC_PROMPT = """{guidance}Transcript of the clip (times from the start of the clip):
{lines}

Pick up to {n} moments where a picture would help the viewer, at least {gap:.0f} seconds apart. Prefer concrete, recognisable subjects that are named: people, places, organisations, events, products, documents. Skip vague words, the speaker themself, and anything you can't picture. Each picture appears when the subject is first mentioned.

Answer as:
{{"images": [{{"at": "m:ss", "show_for": 3-6, "query": "best image-search words, e.g. 'Vivek Ramaswamy' or 'Strait of Hormuz map'", "label": "short on-screen name", "kind": "person | place | organisation | event | thing | concept", "why": "what's being said there, briefly"}}]}}
Use "images": [] if nothing would help."""


def _fmt(t: float) -> str:
    t = max(0, int(round(t)))
    return f"{t // 60}:{t % 60:02d}"


def _parse_t(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.match(r"^\s*(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)\s*$", str(v or ""))
    if not m:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


_STOP = set("""a an the and or but if then so of to in on at by for with from up down out over under
again further once here there when where why how all any both each few more most other some such no
nor not only own same than too very can will just don should now i me my we our you your he him his
she her it its they them their what which who whom this that these those am is are was were be been
being have has had having do does did doing gonna wanna yeah okay ok like know think really right
going get got one thing things people guys""".split())


_NAME = re.compile(r"\b([A-Z][a-zA-Z'-]+(?:\s+(?:of\s+(?:the\s+)?|de\s+|the\s+)?[A-Z][a-zA-Z'-]+){0,3})\b")


class SimpleTopicExtractor:
    """Offline fallback: names (capitalised phrases like "Strait of Hormuz") mentioned in the clip."""

    @staticmethod
    def _names(text: str) -> list[str]:
        out = []
        for m in _NAME.finditer(text):
            words = m.group(1).split()
            while words and words[0].lower() in _STOP:          # "So Vivek Ramaswamy" -> "Vivek Ramaswamy"
                words = words[1:]
            name = " ".join(words)
            if len(name) >= 4 and name.lower() not in _STOP:
                out.append(name)
        return out

    def extract(self, segments, n: int = 4, gap: float = 6.0) -> list[Topic]:
        counts = Counter(nm for _, _, text in segments for nm in self._names(text))
        topics: list[Topic] = []
        seen: set[str] = set()
        for s, e, text in segments:
            for nm in self._names(text):
                if nm in seen:
                    continue
                if " " not in nm and counts[nm] < 2 and text.strip().startswith(nm):
                    continue                              # just a capital at the start of a sentence
                seen.add(nm)
                topics.append(Topic(nm, s, e, float(counts[nm]) + 0.5 * nm.count(" "), nm, "thing"))
        topics.sort(key=lambda t: -t.weight)
        chosen: list[Topic] = []
        for t in topics:
            if len(chosen) >= n:
                break
            if all(abs(t.start - o.start) >= gap for o in chosen):
                chosen.append(t)
        return sorted(chosen, key=lambda t: t.start)


def find_topics(segments: list[tuple[float, float, str]], clip_len: float, n: int = 4, gap: float = 6.0,
                guidance: str = "", use_ai: bool = True,
                on_log: Callable[[str], None] | None = None) -> list[Topic]:
    """segments are in clip time. Returns topics in clip time."""
    if not segments:
        return []
    if use_ai and ai.is_configured():
        lines = "\n".join(f"[{_fmt(s)}] {t}" for s, e, t in segments if t.strip())
        g = f"The editor's note: {guidance.strip()}\n\n" if guidance.strip() else ""
        try:
            d = ai.complete_json(TOPIC_PROMPT.format(guidance=g, lines=lines[:12000], n=n, gap=gap),
                                 TOPIC_SYSTEM, max_tokens=900)
            out = []
            for x in d.get("images", []) or []:
                if not isinstance(x, dict):
                    continue
                at = _parse_t(x.get("at"))
                q = str(x.get("query", "")).strip()
                if at is None or not q:
                    continue
                at = max(0.0, min(clip_len - 1.0, at))
                try:
                    dur = float(x.get("show_for", 4))
                except (TypeError, ValueError):
                    dur = 4.0
                dur = max(2.0, min(8.0, dur))
                kind = str(x.get("kind", "thing")).strip().lower()
                kind = {"organization": "organisation", "org": "organisation", "product": "thing",
                        "object": "thing"}.get(kind, kind)
                out.append(Topic(q[:80], at, min(clip_len, at + dur), 1.0,
                                 (str(x.get("label", "")).strip() or q)[:60],
                                 kind if kind in _ORDER else "thing", str(x.get("why", "")).strip()[:160]))
            out.sort(key=lambda t: t.start)
            spaced: list[Topic] = []
            for t in out:
                if len(spaced) < n and all(abs(t.start - o.start) >= gap * 0.8 for o in spaced):
                    spaced.append(t)
            if on_log:
                on_log(f"🧠 Your AI picked {len(spaced)} thing{'s' if len(spaced) != 1 else ''} to show")
            return spaced
        except ai.AIError as e:
            if on_log:
                on_log(f"⚠ AI couldn't pick topics ({e}) - using names from the transcript instead")
    return SimpleTopicExtractor().extract(segments, n, gap)


# ============================================================================ suggestions
def search_topic(topic: Topic, sources: list[str], limit: int = 6,
                 on_log: Callable[[str], None] | None = None) -> list[ImageCandidate]:
    """Candidates for one topic, best source for the kind of topic first."""
    order = [s for s in _ORDER.get(topic.kind, _ORDER["thing"]) if s in sources]
    found: list[ImageCandidate] = []
    for src in order:
        prov = PROVIDERS[src]()
        ok, why = prov.ready()
        if not ok:
            continue
        try:
            found += prov.search(topic.text, limit)
        except ImageSearchError as e:
            if on_log:
                on_log(f"⚠ {prov.name}: {e}")
        if len(found) >= limit:
            break
    return found


def suggest_for_clip(clip: Clip, segments_file_time: list[tuple[float, float, str]], sources: list[str],
                     n: int = 4, gap: float = 6.0, guidance: str = "", use_ai: bool = True,
                     on_progress: Callable[[float, str], None] | None = None,
                     cancel: Callable[[], bool] | None = None,
                     on_log: Callable[[str], None] | None = None) -> list[ImageOverlay]:
    """Pending (not yet approved) image suggestions for a clip. `segments_file_time` are
    transcript segments in the clip source's own timeline."""
    sp = max(0.25, clip.speed)
    segs = [((max(s, clip.start) - clip.start) / sp, (min(e, clip.end) - clip.start) / sp, t)
            for s, e, t in segments_file_time if e > clip.start and s < clip.end and t.strip()]
    if not segs:
        if on_log:
            on_log("· Nothing is said in this moment - no pictures to suggest")
        return []
    if on_progress:
        on_progress(0.1, "Working out what to show…")
    topics = find_topics(segs, clip.out_length, n, gap, guidance, use_ai, on_log)
    if not topics:
        if on_log:
            on_log("· Nothing concrete to picture in this moment")
        return []
    out: list[ImageOverlay] = []
    for i, tp in enumerate(topics):
        if cancel and cancel():
            raise Cancelled()
        if on_progress:
            on_progress(0.2 + 0.75 * i / len(topics), f"Finding pictures of “{tp.label}”…")
        cands = search_topic(tp, sources, on_log=on_log)
        # thumbnails for the review strip (in parallel); the chosen one in full
        with ThreadPoolExecutor(max_workers=4) as pool:
            thumbs = list(pool.map(lambda c: _try(download, c.thumb_url or c.path_or_url), cands))
        good = []
        for c, th in zip(cands, thumbs):
            if th:
                c.extra["thumb_path"] = th
                good.append(c)
        chosen_path = None
        while good and chosen_path is None:
            chosen_path = _try(download, good[0].path_or_url)
            if chosen_path is None:
                good.pop(0)
        if not good or not chosen_path:
            if on_log:
                on_log(f"· No usable picture found for “{tp.label}”")
            continue
        best = good[0]
        if on_log:
            on_log(f"🖼 {_fmt(tp.start)} “{tp.label}” → {best.title or best.source} ({SOURCES.get(best.source, best.source)})")
        out.append(ImageOverlay(
            image=chosen_path, start=round(tp.start, 2), duration=round(max(2.0, tp.end - tp.start), 2),
            position="top-right", scale=0.38, border=True, label=tp.label, show_label=True,
            source=best.credit_line, style="corner", status="pending", query=tp.text, kind=tp.kind,
            why=tp.why, chosen=0, candidates=[c.as_dict() for c in good]))
    if on_progress:
        on_progress(1.0, "Done")
    return out


def _try(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


def use_candidate(ov: ImageOverlay, index: int) -> None:
    """Swap a suggestion to another candidate (downloads the full-size picture)."""
    c = ImageCandidate.from_dict(ov.candidates[index])
    ov.image = download(c.path_or_url)
    ov.chosen = index
    ov.source = c.credit_line


def research(ov: ImageOverlay, query: str, sources: list[str],
             on_log: Callable[[str], None] | None = None) -> None:
    """Search again for an existing suggestion with your own words."""
    tp = Topic(query, ov.start, ov.start + ov.duration, 1.0, ov.label, ov.kind or "thing")
    cands = search_topic(tp, sources, on_log=on_log)
    good = []
    for c in cands:
        th = _try(download, c.thumb_url or c.path_or_url)
        if th:
            c.extra["thumb_path"] = th
            good.append(c)
    if not good:
        raise ImageSearchError(f"no pictures found for “{query}”")
    ov.candidates = [c.as_dict() for c in good]
    ov.query = query
    use_candidate(ov, 0)


def credits_text(clips: list[tuple[str, Clip]]) -> str:
    """Plain-text credits for approved pictures, one block per exported clip."""
    blocks = []
    for name, c in clips:
        rows = [o for o in c.overlays if getattr(o, "status", "approved") == "approved" and o.source]
        if not rows:
            continue
        lines = [name]
        for o in rows:
            lines.append(f"  {_fmt(o.start)}  {o.label or Path(o.image).stem}: {o.source}")
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return ("Picture credits (RumbleCut context images)\n"
            "Pictures marked 'Unknown - check you may reuse it' came from a web search: make sure "
            "you're allowed to use them before you publish.\n\n" + "\n\n".join(blocks) + "\n")
