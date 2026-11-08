"""Locating and running FFmpeg / FFprobe.

Search order for the binaries:
  1. A path the user set in Settings
  2. Bundled inside the frozen .exe   (<_MEIPASS>/ffmpeg/)
  3. Next to the source tree          (vendor/ffmpeg/)
  4. Whatever is on PATH
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

from . import config

IS_WINDOWS = os.name == "nt"
# Stop a console window flashing up for every ffmpeg call in the windowed .exe
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


class FFmpegError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    custom = config.load_settings().get("ffmpeg_dir")
    if custom:
        dirs.append(Path(custom))
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        dirs += [base / "ffmpeg", Path(sys.executable).parent / "ffmpeg"]
    root = Path(__file__).resolve().parent.parent
    dirs.append(root / "vendor" / "ffmpeg")
    return dirs


@lru_cache(maxsize=None)
def find_binary(name: str) -> str:
    exe = name + (".exe" if IS_WINDOWS else "")
    for d in _candidate_dirs():
        p = d / exe
        if p.is_file():
            return str(p)
    found = shutil.which(name)
    if found:
        return found
    raise FFmpegError(
        f"Could not find {name}. Put ffmpeg.exe and ffprobe.exe in vendor/ffmpeg "
        f"(build_exe.bat does this automatically) or set the folder in Settings."
    )


def ffmpeg() -> str:
    return find_binary("ffmpeg")


def ffprobe() -> str:
    return find_binary("ffprobe")


def ffmpeg_dir() -> str | None:
    try:
        return str(Path(ffmpeg()).parent)
    except FFmpegError:
        return None


def popen(args: list[str], **kw) -> subprocess.Popen:
    return subprocess.Popen(args, creationflags=NO_WINDOW, **kw)


def run(args: list[str], cancel: Callable[[], bool] | None = None,
        on_progress: Callable[[float], None] | None = None,
        duration: float | None = None) -> str:
    """Run ffmpeg/ffprobe; return stderr text. Reports progress (0-1) from `-progress pipe:1`
    style output when `duration` is given and the command contains `-progress pipe:1`."""
    proc = popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    err_lines: list[str] = []

    import threading

    def _drain_err():
        assert proc.stderr
        for line in proc.stderr:
            err_lines.append(line)
            if len(err_lines) > 400:
                del err_lines[:200]

    t = threading.Thread(target=_drain_err, daemon=True)
    t.start()
    assert proc.stdout
    for line in proc.stdout:
        if cancel and cancel():
            proc.kill()
            proc.wait()
            raise Cancelled()
        if on_progress and duration and line.startswith("out_time_us="):
            try:
                us = int(line.split("=", 1)[1])
                on_progress(max(0.0, min(1.0, us / 1e6 / duration)))
            except ValueError:
                pass
    proc.wait()
    t.join(timeout=2)
    if cancel and cancel():
        raise Cancelled()
    stderr = "".join(err_lines)
    if proc.returncode != 0:
        tail = "\n".join(stderr.strip().splitlines()[-15:])
        raise FFmpegError(f"ffmpeg failed (code {proc.returncode}):\n{tail}")
    return stderr


def probe(path: str | Path) -> dict:
    out = subprocess.run(
        [ffprobe(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, creationflags=NO_WINDOW, encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        raise FFmpegError(f"ffprobe could not read {path}:\n{out.stderr.strip()[-500:]}")
    return json.loads(out.stdout or "{}")


class MediaInfo:
    def __init__(self, path: str | Path):
        info = probe(path)
        self.path = str(path)
        self.duration = float(info.get("format", {}).get("duration") or 0.0)
        v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
        a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
        self.has_video = v is not None
        self.has_audio = a is not None
        self.width = int(v.get("width", 0)) if v else 0
        self.height = int(v.get("height", 0)) if v else 0
        fps = 30.0
        if v and v.get("avg_frame_rate") and v["avg_frame_rate"] != "0/0":
            n, _, d = v["avg_frame_rate"].partition("/")
            try:
                fps = float(n) / float(d or 1)
            except (ValueError, ZeroDivisionError):
                pass
        self.fps = fps if 1 <= fps <= 240 else 30.0
        if not self.duration and v and v.get("duration"):
            self.duration = float(v["duration"])

    def __repr__(self):
        return f"<MediaInfo {self.width}x{self.height} {self.fps:.2f}fps {self.duration:.1f}s>"


@lru_cache(maxsize=None)
def nvenc_available() -> bool:
    """True only if an NVIDIA encoder actually works on this machine (not just compiled in)."""
    try:
        r = subprocess.run(
            [ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "color=black:s=256x256:d=0.1", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=20, creationflags=NO_WINDOW,
        )
        return r.returncode == 0
    except Exception:
        return False


def video_encoder_args(quality: str = "high", use_gpu: bool | None = None) -> list[str]:
    """Encoder flags. quality: 'high' | 'balanced' | 'small'."""
    if use_gpu is None:
        use_gpu = config.load_settings().get("use_gpu", True)
    cq = {"high": 19, "balanced": 23, "small": 28}.get(quality, 23)
    if use_gpu and nvenc_available():
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(cq),
                "-b:v", "0", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(cq), "-pix_fmt", "yuv420p"]


def escape_filter_path(p: str | Path) -> str:
    """Escape a filesystem path for use inside an ffmpeg filter option (drawtext fontfile, etc.)."""
    s = str(p).replace("\\", "/")
    s = s.replace(":", r"\:").replace("'", r"\'")
    return s


def default_font() -> str | None:
    candidates: Iterable[str] = (
        r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    )
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def extract_frame(src: str, t: float, out: str, width: int = 320) -> str:
    run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0.0, t):.3f}",
         "-i", src, "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", out])
    return out


def safe_filename(name: str, max_len: int = 80) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" ._")
    return (name or "clip")[:max_len]
