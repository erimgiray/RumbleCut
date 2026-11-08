"""Clip model, effects, transitions and export (all rendered with FFmpeg).

Per-clip effects : trim, speed, punch-in zoom, colour looks, vignette, caption,
                   fade in/out, audio loudness normalisation, image overlays.
Output formats   : original, 16:9, 9:16 (crop or blurred background), 1:1.
Compilation      : joins the clips with xfade transitions + audio cross-fades.
"""
from __future__ import annotations

import csv
import json
import random
import shutil
import tempfile
import textwrap
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from . import ffmpeg_tools as ft
from .ffmpeg_tools import Cancelled, MediaInfo

# ----------------------------------------------------------------------------- options
TRANSITIONS = {            # label -> ffmpeg xfade name ("cut" = hard cut)
    "Cut": "cut",
    "Cross-fade": "fade",
    "Fade through black": "fadeblack",
    "Fade through white": "fadewhite",
    "Dissolve": "dissolve",
    "Wipe left": "wipeleft",
    "Wipe right": "wiperight",
    "Slide left": "slideleft",
    "Slide up": "slideup",
    "Smooth left": "smoothleft",
    "Circle open": "circleopen",
    "Radial": "radial",
    "Zoom in": "zoomin",
    "Pixelate": "pixelize",
    "Blur": "hblur",
    "Squeeze": "squeezev",
    "Random mix": "random",
}
RANDOM_POOL = ["fade", "fadeblack", "wipeleft", "slideleft", "slideup", "smoothleft",
               "circleopen", "radial", "zoomin", "dissolve"]

COLOR_LOOKS = {
    "None": "",
    "Vivid": "eq=saturation=1.35:contrast=1.08",
    "Black & white": "hue=s=0,eq=contrast=1.15",
    "Warm": "colorbalance=rs=0.08:gs=0.02:bs=-0.08,eq=saturation=1.1",
    "Cool": "colorbalance=rs=-0.06:bs=0.1",
    "Cinematic": "eq=contrast=1.12:saturation=0.85:gamma=0.95,colorbalance=rs=0.04:bs=0.06:rh=0.05:bh=-0.05",
    "Retro": "curves=preset=vintage,noise=alls=8:allf=t",
}

ASPECTS = {
    "Original": "original",
    "Landscape 16:9": "16:9",
    "Vertical 9:16 (crop)": "9:16",
    "Vertical 9:16 (blurred background)": "9:16blur",
    "Square 1:1": "1:1",
}

OVERLAY_POSITIONS = ["top-right", "top-left", "bottom-right", "bottom-left", "center", "full"]


@dataclass
class ImageOverlay:
    """A picture shown over the video. Used manually today and by the upcoming
    Context Images feature, which will create these automatically."""
    image: str
    start: float                    # seconds from the start of the (exported) clip
    duration: float = 4.0
    position: str = "top-right"
    scale: float = 0.38             # fraction of output width
    border: bool = True
    label: str = ""                 # what it illustrates (e.g. the spoken topic)
    source: str = ""                # attribution / URL


@dataclass
class Clip:
    source: str
    start: float
    end: float
    title: str = ""
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    why: list[str] = field(default_factory=list)      # what was heard / seen
    timing: list[str] = field(default_factory=list)   # why it starts and ends where it does
    enabled: bool = True
    # effects
    fade_in: float = 0.25
    fade_out: float = 0.4
    speed: float = 1.0
    zoom: float = 1.0
    color: str = "None"
    vignette: bool = False
    caption: str = ""
    caption_pos: str = "bottom"
    normalize_audio: bool = True
    transition: str = "Default"     # transition *into* this clip in a compilation
    offset: float = 0.0             # where `source` starts on the project's timeline (video fetched
                                    # just for this moment after an audio-first scan)
    overlays: list[ImageOverlay] = field(default_factory=list)

    @property
    def length(self) -> float:
        return max(0.1, self.end - self.start)

    @property
    def out_length(self) -> float:
        return self.length / max(0.25, self.speed)


@dataclass
class ExportSettings:
    aspect: str = "Original"
    height: int = 1080
    fps: int = 30
    quality: str = "high"
    transition: str = "Cross-fade"
    transition_duration: float = 0.6
    watermark: str = ""
    individual: bool = True
    compilation: bool = False
    thumbnails: bool = True


@dataclass
class Project:
    source: str = ""
    title: str = ""
    url: str = ""
    offset: float = 0.0        # where this file starts in the original stream (partial downloads)
    # For an audio scan of several parts joined together: [file_start, stream_start, length] per part
    segments: list = field(default_factory=list)
    clips: list[Clip] = field(default_factory=list)
    export: ExportSettings = field(default_factory=ExportSettings)

    def to_stream(self, t: float) -> float:
        """Position in this project's file -> position in the original stream."""
        if self.segments:
            seg = self.segments[0]
            for sg in self.segments:
                if t >= sg[0] - 1e-6:
                    seg = sg
            return seg[1] + (t - seg[0])
        return t + self.offset

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @staticmethod
    def load(path: str | Path) -> "Project":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        clips = []
        for c in d.get("clips", []):
            ovs = [ImageOverlay(**o) for o in c.pop("overlays", [])]
            clips.append(Clip(**c, overlays=ovs))
        exp = ExportSettings(**d.get("export", {}))
        return Project(source=d.get("source", ""), title=d.get("title", ""), url=d.get("url", ""),
                       offset=float(d.get("offset", 0.0)), segments=d.get("segments", []) or [],
                       clips=clips, export=exp)


# ----------------------------------------------------------------------------- helpers
def _even(x: float) -> int:
    v = int(round(x))
    return v - (v % 2)


def output_size(aspect: str, height: int, src_w: int, src_h: int) -> tuple[int, int]:
    mode = ASPECTS.get(aspect, aspect)
    src_w, src_h = src_w or 1920, src_h or 1080
    if mode == "16:9":
        return _even(height * 16 / 9), _even(height)
    if mode in ("9:16", "9:16blur"):
        w = _even(height)                      # "1080" -> 1080x1920, "720" -> 720x1280
        return w, _even(w * 16 / 9)
    if mode == "1:1":
        return _even(height), _even(height)
    h = min(height, src_h)
    return _even(src_w * h / src_h), _even(h)


@lru_cache(maxsize=None)
def _has_text_align() -> bool:
    try:
        import subprocess
        r = subprocess.run([ft.ffmpeg(), "-hide_banner", "-h", "filter=drawtext"],
                           capture_output=True, text=True, creationflags=ft.NO_WINDOW)
        return "text_align" in r.stdout
    except Exception:
        return False


def _atempo_chain(speed: float) -> list[str]:
    parts, s = [], speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    if abs(s - 1.0) > 1e-3:
        parts.append(f"atempo={s:.4f}")
    return parts


def _drawtext(textfile: Path, font: str | None, size: int, x: str, y: str,
              extra: str = "") -> str:
    opts = [f"textfile='{ft.escape_filter_path(textfile)}'", "expansion=none",
            f"fontsize={size}", "fontcolor=white", f"borderw={max(2, size // 12)}",
            "bordercolor=black@0.85", f"x={x}", f"y={y}", f"line_spacing={size // 6}"]
    if font:
        opts.insert(0, f"fontfile='{ft.escape_filter_path(font)}'")
    if _has_text_align():
        opts.append("text_align=C")
    if extra:
        opts.append(extra)
    return "drawtext=" + ":".join(opts)


def _overlay_xy(pos: str, margin: str = "W*0.04") -> tuple[str, str]:
    return {
        "top-left": (margin, margin),
        "top-right": (f"W-w-{margin}", margin),
        "bottom-left": (margin, f"H-h-{margin}"),
        "bottom-right": (f"W-w-{margin}", f"H-h-{margin}"),
        "center": ("(W-w)/2", "(H-h)/2"),
        "full": ("(W-w)/2", "(H-h)/2"),
    }.get(pos, (f"W-w-{margin}", margin))


# ----------------------------------------------------------------------------- render one clip
def build_clip_command(clip: Clip, settings: ExportSettings, out: str | Path, tmp: Path,
                       info: MediaInfo | None = None, fades: bool = True) -> tuple[list[str], float]:
    info = info or MediaInfo(clip.source)
    W, H = output_size(settings.aspect, settings.height, info.width, info.height)
    mode = ASPECTS.get(settings.aspect, settings.aspect)
    D = clip.out_length
    speed = max(0.25, min(4.0, clip.speed))

    args = [ft.ffmpeg(), "-hide_banner", "-y", "-nostats", "-loglevel", "error",
            "-ss", f"{clip.start:.3f}", "-t", f"{clip.length:.3f}", "-i", clip.source]
    for ov in clip.overlays:
        args += ["-loop", "1", "-t", f"{D:.3f}", "-i", ov.image]

    f: list[str] = []
    v: list[str] = []
    if not info.has_video:
        f.append(f"color=c=black:s={W}x{H}:r={settings.fps}:d={D:.3f}[base0]")
        cur = "[base0]"
    else:
        if abs(speed - 1) > 1e-3:
            v.append(f"setpts=PTS/{speed:.4f}")
        z = max(1.0, min(2.5, clip.zoom))
        if z > 1.001:
            v.append(f"crop=iw/{z:.3f}:ih/{z:.3f},scale=iw*{z:.3f}:ih*{z:.3f}")
        look = COLOR_LOOKS.get(clip.color, "")
        if look:
            v.append(look)
        if clip.vignette:
            v.append("vignette=PI/4.5")
        chain = ",".join(v) if v else "null"
        if mode == "9:16blur":
            f.append(f"[0:v]{chain},split=2[bgsrc][fgsrc]")
            f.append(f"[bgsrc]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                     f"boxblur=20:2,eq=brightness=-0.08[bg]")
            f.append(f"[fgsrc]scale={W}:-2[fg]")
            f.append(f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={settings.fps},setsar=1[base0]")
        elif mode in ("9:16", "1:1"):
            ar = "9/16" if mode == "9:16" else "1"
            f.append(f"[0:v]{chain},crop='min(iw,ih*{ar})':'min(ih,iw/({ar}))',"
                     f"scale={W}:{H},fps={settings.fps},setsar=1[base0]")
        else:
            f.append(f"[0:v]{chain},scale={W}:{H}:force_original_aspect_ratio=decrease,"
                     f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,fps={settings.fps},setsar=1[base0]")
        cur = "[base0]"

    # Image overlays (manual today; auto-generated by Context Images later)
    for k, ov in enumerate(clip.overlays, start=1):
        s = max(0.0, min(ov.start, D - 0.2))
        e = min(D, s + max(0.5, ov.duration))
        fd = min(0.3, (e - s) / 3)
        if ov.position == "full":
            sc = f"scale={W}:{H}:force_original_aspect_ratio=decrease"
        else:
            sc = f"scale={_even(W * ov.scale)}:-2"
        border = ",pad=iw+12:ih+12:6:6:white" if ov.border and ov.position != "full" else ""
        f.append(f"[{k}:v]{sc}{border},format=rgba,"
                 f"fade=t=in:st={s:.3f}:d={fd:.3f}:alpha=1,fade=t=out:st={e - fd:.3f}:d={fd:.3f}:alpha=1[ov{k}]")
        x, y = _overlay_xy(ov.position)
        f.append(f"{cur}[ov{k}]overlay={x}:{y}:enable='between(t,{s:.3f},{e:.3f})'[base{k}]")
        cur = f"[base{k}]"

    post: list[str] = []
    font = ft.default_font()
    if clip.caption.strip():
        size = max(18, int(H * (0.038 if H > W else 0.06)))
        per_line = max(10, int(W * 0.9 / (size * 0.58)))
        txt = tmp / f"cap_{id(clip)}.txt"
        txt.write_text(textwrap.fill(clip.caption.strip(), per_line), encoding="utf-8")
        if clip.caption_pos == "top":
            y = "h*0.07"
        else:
            y = "h*0.86-text_h" if H > W else "h-text_h-h*0.08"
        post.append(_drawtext(txt, font, size, "(w-text_w)/2", y))
    if settings.watermark.strip():
        size = max(14, int(H * 0.028))
        wm = tmp / "watermark.txt"
        wm.write_text(settings.watermark.strip(), encoding="utf-8")
        post.append(_drawtext(wm, font, size, "w-text_w-w*0.03", "h-text_h-h*0.03",
                              "alpha=0.7"))
    if fades and clip.fade_in > 0:
        post.append(f"fade=t=in:st=0:d={min(clip.fade_in, D / 3):.3f}")
    if fades and clip.fade_out > 0:
        fo = min(clip.fade_out, D / 3)
        post.append(f"fade=t=out:st={D - fo:.3f}:d={fo:.3f}")
    post.append("format=yuv420p")
    f.append(f"{cur}{','.join(post)}[vout]")

    # Audio
    a: list[str] = []
    if info.has_audio:
        a += _atempo_chain(speed)
        if clip.normalize_audio:
            a.append("loudnorm=I=-16:TP=-1.5:LRA=11")
        a.append("aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo")
        if fades and clip.fade_in > 0:
            a.append(f"afade=t=in:st=0:d={min(clip.fade_in, D / 3):.3f}")
        if fades and clip.fade_out > 0:
            fo = min(clip.fade_out, D / 3)
            a.append(f"afade=t=out:st={D - fo:.3f}:d={fo:.3f}")
        a.append(f"atrim=0:{D:.3f}")
        f.append(f"[0:a]{','.join(a)}[aout]")
    else:
        f.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{D:.3f}[aout]")

    args += ["-filter_complex", ";".join(f), "-map", "[vout]", "-map", "[aout]",
             *ft.video_encoder_args(settings.quality),
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{D:.3f}",
             "-movflags", "+faststart", "-progress", "pipe:1", str(out)]
    return args, D


def render_clip(clip: Clip, settings: ExportSettings, out: str | Path,
                on_progress: Callable[[float], None] | None = None,
                cancel: Callable[[], bool] | None = None, fades: bool = True) -> str:
    tmp = Path(tempfile.mkdtemp(prefix="rumblecut_"))
    try:
        args, D = build_clip_command(clip, settings, out, tmp, fades=fades)
        ft.run(args, cancel=cancel, on_progress=on_progress, duration=D)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return str(out)


# ----------------------------------------------------------------------------- compilation
def _resolve_transition(label: str, default: str) -> str:
    if label in ("Default", "", None):
        label = default
    name = TRANSITIONS.get(label, label)
    if name == "random":
        name = random.choice(RANDOM_POOL)
    return name


def render_compilation(clips: list[Clip], settings: ExportSettings, out: str | Path,
                       on_progress: Callable[[float, str], None] | None = None,
                       cancel: Callable[[], bool] | None = None) -> str:
    """Render each clip to a matching intermediate, then join them with transitions."""
    clips = [c for c in clips if c.enabled]
    if not clips:
        raise ValueError("No clips selected.")
    tmp = Path(tempfile.mkdtemp(prefix="rumblecut_comp_"))
    try:
        parts: list[tuple[str, float]] = []
        n = len(clips)
        for i, c in enumerate(clips):
            p = tmp / f"part{i:03d}.mp4"
            # inner fades are replaced by the transitions; keep the first fade-in and last fade-out
            cc = Clip(**{**asdict(c), "overlays": c.overlays})
            if i > 0:
                cc.fade_in = 0
            if i < n - 1:
                cc.fade_out = 0
            render_clip(cc, settings, p, cancel=cancel,
                        on_progress=(lambda fr, i=i: on_progress((i + fr) / (n + 1),
                                     f"Rendering clip {i + 1}/{n}")) if on_progress else None)
            parts.append((str(p), cc.out_length))

        T = max(0.1, settings.transition_duration)
        names = [_resolve_transition(c.transition, settings.transition) for c in clips]
        args = [ft.ffmpeg(), "-hide_banner", "-y", "-nostats", "-loglevel", "error"]
        for p, _ in parts:
            args += ["-i", p]
        f: list[str] = []
        if n == 1:
            f.append("[0:v]null[vout]")
            f.append("[0:a]anull[aout]")
            total = parts[0][1]
        else:
            vcur, acur = "[0:v]", "[0:a]"
            total = parts[0][1]
            for i in range(1, n):
                d_next = parts[i][1]
                name = names[i]
                t = min(T, total / 2.5, d_next / 2.5)
                if name == "cut":
                    f.append(f"{vcur}[{i}:v]concat=n=2:v=1:a=0[v{i}]")
                    f.append(f"{acur}[{i}:a]concat=n=2:v=0:a=1[a{i}]")
                    total += d_next
                else:
                    off = total - t
                    f.append(f"{vcur}settb=AVTB[vl{i}];[{i}:v]settb=AVTB[vr{i}];"
                             f"[vl{i}][vr{i}]xfade=transition={name}:duration={t:.3f}:offset={off:.3f}[v{i}]")
                    f.append(f"{acur}[{i}:a]acrossfade=d={t:.3f}:c1=tri:c2=tri[a{i}]")
                    total += d_next - t
                vcur, acur = f"[v{i}]", f"[a{i}]"
            f.append(f"{vcur}format=yuv420p[vout]")
            f.append(f"{acur}anull[aout]")
        args += ["-filter_complex", ";".join(f), "-map", "[vout]", "-map", "[aout]",
                 *ft.video_encoder_args(settings.quality), "-c:a", "aac", "-b:a", "192k",
                 "-movflags", "+faststart", "-progress", "pipe:1", str(out)]
        ft.run(args, cancel=cancel, duration=total,
               on_progress=(lambda fr: on_progress((n + fr) / (n + 1), "Joining clips with transitions"))
               if on_progress else None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return str(out)


# ----------------------------------------------------------------------------- batch export
def export_project(project: Project, out_dir: str | Path,
                   on_progress: Callable[[float, str], None] | None = None,
                   cancel: Callable[[], bool] | None = None) -> list[str]:
    s = project.export
    clips = [c for c in project.clips if c.enabled]
    if not clips:
        raise ValueError("Tick at least one clip to export.")
    base = ft.safe_filename(project.title or Path(project.source).stem, 60)
    out_dir = Path(out_dir) / base
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    jobs = (len(clips) if s.individual else 0) + (len(clips) + 1 if s.compilation else 0)
    done = 0

    def prog(fr: float, msg: str):
        if on_progress:
            on_progress(min(1.0, (done + fr) / max(1, jobs)), msg)

    rows = []
    if s.individual:
        for i, c in enumerate(clips, 1):
            if cancel and cancel():
                raise Cancelled()
            name = f"{i:02d} - {ft.safe_filename((c.title or f'Highlight {i}').replace(':', '.'))}"
            out = out_dir / f"{name}.mp4"
            render_clip(c, s, out, cancel=cancel,
                        on_progress=lambda fr, i=i: prog(fr, f"Exporting clip {i}/{len(clips)}"))
            written.append(str(out))
            if s.thumbnails:
                try:
                    ft.extract_frame(str(out), min(c.out_length * 0.4, c.out_length - 0.1),
                                     str(out_dir / f"{name}.jpg"), width=1280)
                except Exception:
                    pass
            rows.append([i, c.title, _ts(project.to_stream(c.start + c.offset)),
                         _ts(project.to_stream(c.end + c.offset)),
                         f"{c.out_length:.1f}", c.score,
                         "; ".join(c.reasons), out.name])
            done += 1
    if s.compilation:
        out = out_dir / f"{base} - Best Moments.mp4"
        render_compilation(clips, s, out, cancel=cancel,
                           on_progress=lambda fr, msg: prog(fr * (len(clips) + 1), msg))
        written.append(str(out))
        done += len(clips) + 1
    if rows:
        with open(out_dir / "clips.csv", "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["#", "Title", "Start in stream", "End in stream", "Length (s)", "Score",
                        "Why it was picked", "File"])
            w.writerows(rows)
    if on_progress:
        on_progress(1.0, f"Exported {len(written)} file(s)")
    return written


def _ts(t: float) -> str:
    t = int(t)
    return f"{t // 3600:d}:{t % 3600 // 60:02d}:{t % 60:02d}"
