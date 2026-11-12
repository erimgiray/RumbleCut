"""Rumble downloading via yt-dlp (works for regular videos and past livestreams).

Built-in and independent: yt-dlp is bundled into the .exe, so no browser or
external downloader is needed. Large streams resume automatically if interrupted.

Partial downloads: pass `ranges` (e.g. from parse_ranges("1:05:00-1:20:00, 2:10:00-2:15:00"))
and only those parts of the video are fetched - each range is saved as its own file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config, ffmpeg_tools
from .ffmpeg_tools import Cancelled

QUALITY_FORMATS = {
    "Best available": "bv*+ba/b",
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "720p": "bv*[height<=720]+ba/b[height<=720]/b",
    "480p": "bv*[height<=480]+ba/b[height<=480]/b",
    "Audio only": "ba/b",
}

# Formats tried, in order, when only parts of a stream are downloaded. ffmpeg cuts those parts
# straight from Rumble's servers, and it reads a plain single-file MP4 most reliably (the same
# kind the sound scan uses). Rumble's "timeline" previews are skipped - ffmpeg can't cut them
# ("Invalid data found", exit code 3199971767). If one kind fails, the next is tried.
def _section_formats(quality: str) -> list[str]:
    h = {"1080p": 1080, "720p": 720, "480p": 480}.get(quality)
    cap = f"[height<={h}]" if h else ""
    nt = "[format_id!*=timeline]"
    return [
        f"b{cap}[protocol^=http][ext=mp4]{nt}/b{cap}[protocol^=http]{nt}",
        f"b{cap}[protocol*=m3u8]{nt}/bv*{cap}[protocol*=m3u8]{nt}+ba",
        f"b{cap}{nt}/b{nt}",
        f"bv*{cap}{nt}+ba/b{cap}/b",
    ]


RUMBLE_RE = re.compile(r"^https?://(www\.)?rumble\.com/", re.I)


@dataclass
class Section:
    path: str
    start: float            # where this file starts in the original video (seconds)
    end: float | None


@dataclass
class DownloadResult:
    path: str
    title: str
    uploader: str
    duration: float
    url: str
    sections: list[Section] = field(default_factory=list)   # filled for partial downloads
    segments: list = field(default_factory=list)            # audio scans of parts: [file_start, stream_start, length]


def is_rumble_url(url: str) -> bool:
    return bool(RUMBLE_RE.match(url.strip()))


# ----------------------------------------------------------------------------- time ranges
def parse_time(text: str) -> float:
    """'1:05:30' / '65:30' / '3930' / '1h5m30s' / '90s' -> seconds."""
    t = text.strip().lower()
    if not t:
        raise ValueError("empty time")
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+(?:\.\d+)?)m)?\s*(?:(\d+(?:\.\d+)?)s)?", t)
    if m and any(m.groups()):
        h, mi, s = (float(x) if x else 0.0 for x in m.groups())
        return h * 3600 + mi * 60 + s
    parts = t.split(":")
    if not 1 <= len(parts) <= 3 or not all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
        raise ValueError(f"“{text.strip()}” isn't a time - use h:mm:ss, mm:ss or seconds")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def parse_ranges(text: str, duration: float | None = None) -> list[tuple[float, float | None]]:
    """Parse '1:05:00-1:20:00, 2:10:30 - 2:15' into [(3900, 4800), (7830, 7830+...)].
    An empty end ('1:30:00-' or '1:30:00-end') means 'to the end of the video'.
    Ranges are sorted and overlapping ones merged."""
    out: list[tuple[float, float | None]] = []
    chunks = [c for c in re.split(r"[,;\n]+", text) if c.strip()]
    if not chunks:
        raise ValueError("Enter at least one time range, e.g. 1:05:00-1:20:00")
    for c in chunks:
        m = re.fullmatch(r"\s*([^-–—]+?)\s*(?:-|–|—|to)\s*([^-–—]*?)\s*", c, re.I)
        if not m:
            raise ValueError(f"“{c.strip()}” should look like start-end, e.g. 1:05:00-1:20:00")
        start = parse_time(m.group(1))
        end_txt = m.group(2).strip().lower()
        end = None if end_txt in ("", "end") else parse_time(end_txt)
        if end is not None and end <= start:
            hint = ""
            if end_txt.count(":") < m.group(1).count(":"):
                hint = f" Did you mean {m.group(1).strip().split(':')[0]}:{end_txt}?"
            raise ValueError(f"In “{c.strip()}” the end is before the start.{hint}")
        if duration:
            if start >= duration:
                raise ValueError(f"“{c.strip()}” starts after the video ends ({fmt(duration)}).")
            if end is not None and end > duration:
                end = None
        out.append((start, end))
    out.sort(key=lambda r: r[0])
    merged: list[tuple[float, float | None]] = []
    for s, e in out:
        if merged:
            ps, pe = merged[-1]
            if pe is None or s <= pe:
                merged[-1] = (ps, None if (pe is None or e is None) else max(pe, e))
                continue
        merged.append((s, e))
    return merged


def fmt(t: float) -> str:
    t = int(round(t))
    return f"{t // 3600:d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def ranges_total(ranges: list[tuple[float, float | None]], duration: float | None) -> float | None:
    total = 0.0
    for s, e in ranges:
        if e is None:
            if not duration:
                return None
            e = duration
        total += e - s
    return total


# ----------------------------------------------------------------------------- downloading
def _impersonation() -> dict:
    """Rumble answers 403 to anything that doesn't look like a real browser, so requests
    are sent with Chrome's TLS fingerprint (yt-dlp + curl_cffi). Empty if unavailable."""
    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget
        return {"impersonate": ImpersonateTarget.from_str("chrome")}
    except Exception:
        return {}


def impersonation_available() -> bool:
    return bool(_impersonation())


def fetch_info(url: str) -> dict:
    """Metadata only (title, duration, thumbnail) - no download."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noprogress": True,
            **_impersonation()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return ydl.sanitize_info(info)
    except yt_dlp.utils.DownloadError as e:
        raise RuntimeError(_friendly_error(str(e))) from e


def download(url: str, quality: str = "1080p", out_dir: str | Path | None = None,
             ranges: list[tuple[float, float | None]] | None = None,
             on_progress: Callable[[float, str], None] | None = None,
             cancel: Callable[[], bool] | None = None,
             audio_only: bool = False) -> DownloadResult:
    """Download a video. With `ranges`, only those parts are fetched (one file per range).
    With `audio_only`, just the sound is saved (its original stream copied, not re-encoded) - a fraction of the size, used to
    find the best moments before downloading any video. Rumble often has no separate
    audio track; then the smallest video version is fetched and only its audio kept.
    Progress fraction is -1 when the size isn't known yet (show a busy bar)."""
    import yt_dlp

    out_dir = Path(out_dir or config.download_dir())
    out_dir.mkdir(parents=True, exist_ok=True)
    n_sections = len(ranges or [])
    state = {"section": 0}

    def hook(d: dict):
        if cancel and cancel():
            raise Cancelled()
        if not on_progress:
            return
        prefix = (f"Part {min(state['section'] + 1, n_sections)}/{n_sections}  •  "
                  if n_sections > 1 else "")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            frac = done / total if total else -1.0
            if n_sections > 1 and frac >= 0:
                frac = (state["section"] + frac) / n_sections
            speed = d.get("speed") or 0
            eta = d.get("eta")
            msg = prefix + (f"{done / 1e6:,.0f} / {total / 1e6:,.0f} MB" if total
                            else f"{done / 1e6:,.1f} MB downloaded")
            if speed:
                msg += f"  •  {speed / 1e6:.1f} MB/s"
            if eta:
                msg += f"  •  {int(eta) // 60}m {int(eta) % 60:02d}s left"
            on_progress(frac, msg)
        elif d.get("status") == "finished":
            state["section"] += 1
            done_all = state["section"] >= max(1, n_sections)
            if done_all:
                on_progress(-1.0 if audio_only else 1.0,
                            "Saving the sound…" if audio_only else "Finishing up…")
            else:   # next part starts; its size isn't known in advance -> busy bar
                on_progress(-1.0, f"Part {state['section']} of {n_sections} saved  •  "
                                  f"downloading part {state['section'] + 1}…")

    if audio_only and ranges:
        tmpl = "%(title).70B [%(id)s] audio %(section_start)d-%(section_end)ds.%(ext)s"
    elif audio_only:
        tmpl = "%(title).85B [%(id)s] audio.%(ext)s"
    elif ranges:
        tmpl = "%(title).80B [%(id)s] %(section_start)d-%(section_end)ds.%(ext)s"
    else:
        tmpl = "%(title).90B [%(id)s].%(ext)s"
    opts = {
        "format": ("ba/b" if audio_only
                   else QUALITY_FORMATS.get(quality, QUALITY_FORMATS["1080p"])),
        "outtmpl": str(out_dir / tmpl),
        "merge_output_format": "mp4",
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "continuedl": True,          # resume partial downloads of long streams
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "windowsfilenames": True,
        "noplaylist": True,
        **_impersonation(),
    }
    if audio_only:
        opts.pop("merge_output_format", None)
        # Rumble usually has no audio-only track: take the version with the best sound (and the
        # smallest picture among those), then keep its audio stream exactly as it is - copied, not
        # re-encoded, so the scan sounds like the stream. Only non-AAC sound is converted (to AAC).
        opts["format_sort"] = ["abr", "+res", "+size"]
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a",
                                   "preferredquality": "192"}]
    if ranges:
        from yt_dlp.utils import download_range_func
        opts["download_ranges"] = download_range_func(
            None, [(s, e if e is not None else float("inf")) for s, e in ranges])
    loc = ffmpeg_tools.ffmpeg_dir()
    if loc:
        opts["ffmpeg_location"] = loc

    if ranges and on_progress:
        on_progress(-1.0, f"Downloading part 1 of {n_sections}…" if n_sections > 1
                    else "Downloading the selected part…")
    attempts = ([opts["format"]] if audio_only or not ranges
                else _section_formats(quality))
    last_err = None
    for n, fmt_str in enumerate(attempts):
        opts["format"] = fmt_str
        state["section"] = 0
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info.get("_type") == "playlist" and info.get("entries"):
                    info = next(e for e in info["entries"] if e)
                paths: list[str] = []
                for rd in info.get("requested_downloads") or []:
                    fp = rd.get("filepath") or rd.get("_filename")
                    if fp and fp not in paths:
                        paths.append(fp)
                if not paths:
                    paths = [ydl.prepare_filename(info)]
            break
        except yt_dlp.utils.DownloadError as e:
            if cancel and cancel():
                raise Cancelled() from e
            last_err = e
            msg = str(e).lower()
            retry = ("ffmpeg exited" in msg or "invalid data" in msg
                     or "requested format is not available" in msg)
            if not retry or n == len(attempts) - 1:
                raise RuntimeError(_friendly_error(str(e))) from e
            if on_progress:
                on_progress(-1.0, "That version of the video couldn't be cut - trying another…")
    else:   # pragma: no cover - loop always breaks or raises
        raise RuntimeError(_friendly_error(str(last_err)))

    files = []
    for p in paths:
        pp = Path(p)
        for ext in ((".m4a", ".mp3") if audio_only else ()) + (".mp4",):
            if (not pp.exists() or pp.suffix != ext and audio_only) and pp.with_suffix(ext).exists():
                pp = pp.with_suffix(ext)
                break
        files.append(str(pp))

    duration = float(info.get("duration") or 0)
    sections: list[Section] = []
    if ranges:
        # match files back to ranges by the start second written into the filename
        by_start = {}
        for f in files:
            m = re.search(r" (\d+)-(\d+|inf|NA)s\.\w+$", f)
            if m:
                by_start[int(m.group(1))] = f
        for i, (s, e) in enumerate(ranges):
            f = by_start.get(int(s)) or (files[i] if i < len(files) else None)
            if f and Path(f).exists():
                sections.append(Section(path=f, start=s, end=e if e is not None else duration or None))
        if not sections:
            raise RuntimeError("The selected parts couldn't be saved. Check the times and try again.")

    if audio_only and sections:
        joined, segments = _join_audio_parts(sections, out_dir, info)
        return DownloadResult(path=joined, title=info.get("title") or Path(joined).stem,
                              uploader=info.get("uploader") or "", duration=duration, url=url,
                              segments=segments)

    return DownloadResult(
        path=sections[0].path if sections else files[0],
        title=info.get("title") or Path(files[0]).stem, uploader=info.get("uploader") or "",
        duration=duration, url=url, sections=sections,
    )


def _join_audio_parts(sections: list[Section], out_dir: Path, info: dict):
    """Join the downloaded audio parts into one scan file; remember where each part sits."""
    parts = sorted(sections, key=lambda x: x.start)
    segments, pos = [], 0.0
    for sec in parts:
        length = ffmpeg_tools.MediaInfo(sec.path).duration
        segments.append([round(pos, 3), float(sec.start), round(length, 3)])
        pos += length
    ext = Path(parts[0].path).suffix or ".m4a"
    name = "%s [%s] audio %s" % (
        ffmpeg_tools.safe_filename(info.get("title") or "stream", 70), info.get("id") or "x",
        "+".join(f"{fmt(s.start).replace(':', '.')}" for s in parts)) + ext
    joined = out_dir / name
    if len(parts) == 1:
        Path(parts[0].path).replace(joined)
    else:
        lst = out_dir / ".rumblecut_join.txt"
        lst.write_text("".join("file '%s'\n" % str(Path(p.path).resolve()).replace("'", "'\\''")
                               for p in parts), encoding="utf-8")
        ffmpeg_tools.run([ffmpeg_tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                          "-safe", "0", "-i", str(lst)]
                         + (["-c", "copy"] if ext == ".mp3" else
                            # .m4a parts carry trim info that a plain copy-join ignores (each part
                            # would gain ~0.5 s and the stream times would drift), so they're decoded
                            # and saved once at a high bitrate - transparent next to Rumble's audio
                            ["-c:a", "aac", "-b:a", "192k"])
                         + [str(joined)])
        lst.unlink(missing_ok=True)
        for p in parts:
            Path(p.path).unlink(missing_ok=True)
    return str(joined), segments


def _friendly_error(msg: str) -> str:
    m = msg.lower()
    if "is live" in m or "live event will begin" in m:
        return "This stream is still live or hasn't started. Download it once it has finished and the replay is up."
    if "unsupported url" in m:
        return "That link isn't a Rumble video page. Copy the link from the video's page (rumble.com/v…)."
    if "impersonat" in m and ("not available" in m or "unsupported" in m):
        return ("Browser impersonation isn't available in this copy of RumbleCut, and Rumble blocks "
                "downloads without it. Install the latest RumbleCut from Settings → Updates.")
    if "http error 403" in m or "http error 410" in m:
        hint = ("" if impersonation_available() else
                " This copy of RumbleCut can't imitate a browser, which Rumble now requires - "
                "install the latest RumbleCut from Settings → Updates.")
        return ("Rumble refused the download (403). The video may be private, members-only or "
                "region-locked, or Rumble changed something - try Settings → Updates → "
                "update the Rumble downloader." + hint)
    if "ffmpeg exited" in m or "invalid data" in m:
        return ("Rumble's server sent something ffmpeg couldn't read while cutting out the parts, "
                "even after trying every version of the video. Try again in a minute, pick a "
                "different quality, or update the Rumble downloader (Settings → Updates).\n\n"
                "Details: " + msg.replace("ERROR: ", ""))
    return msg.replace("ERROR: ", "")
