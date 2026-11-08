"""In-app updates.

1. Rumble downloader (yt-dlp) - the part that breaks when Rumble changes its site.
   The newest release is fetched from PyPI and unpacked into
   %APPDATA%/RumbleCut/updates/yt-dlp. At start-up that folder is put first on
   sys.path, so the newer copy is used instead of the one baked into the .exe.
   No rebuild needed.

2. RumbleCut itself - the newest RumbleCut.exe from the GitHub Releases of the
   update repository (default erimgiray/RumbleCut). The running .exe is renamed
   to RumbleCut.old.exe (Windows allows renaming a running program), the new one
   takes its place, and the app relaunches. The old copy is deleted next start.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from . import __version__, config
from .ffmpeg_tools import NO_WINDOW, Cancelled

DEFAULT_REPO = "erimgiray/RumbleCut"
UA = {"User-Agent": "RumbleCut-updater", "Accept": "application/vnd.github+json"}
PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
GITHUB_API = os.environ.get("RUMBLECUT_GITHUB_API", "https://api.github.com")

ProgressFn = Callable[[float, str], None]


# ----------------------------------------------------------------------------- helpers
def updates_dir() -> Path:
    d = config.app_data_dir() / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ytdlp_dir() -> Path:
    return updates_dir() / "yt-dlp"


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0"))


def is_newer(latest: str, current: str) -> bool:
    return version_tuple(latest) > version_tuple(current)


def _get_json(url: str, timeout: float = 20) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.load(r)


def _download(url: str, on_progress: ProgressFn | None, cancel: Callable[[], bool] | None,
              label: str, dest: Path | None = None) -> bytes | Path:
    req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        buf = io.BytesIO() if dest is None else open(dest, "wb")
        done = 0
        try:
            while True:
                if cancel and cancel():
                    raise Cancelled()
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                buf.write(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done / total if total else -1.0,
                                f"{label}  {done / 1e6:.1f}" + (f" / {total / 1e6:.1f} MB" if total else " MB"))
        finally:
            if dest is not None:
                buf.close()
    if dest is None:
        return buf.getvalue()
    return dest


# ----------------------------------------------------------------------------- start-up hooks
def activate_downloader_override() -> None:
    """Call before anything imports yt_dlp."""
    d = ytdlp_dir()
    if (d / "yt_dlp" / "__init__.py").is_file() and str(d) not in sys.path:
        sys.path.insert(0, str(d))


def cleanup_old_exe() -> None:
    if getattr(sys, "frozen", False):
        old = Path(sys.executable).with_name(Path(sys.executable).stem + ".old.exe")
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass


# ----------------------------------------------------------------------------- downloader (yt-dlp)
def ytdlp_version() -> str:
    try:
        from yt_dlp.version import __version__ as v
        return v
    except Exception:
        return "unknown"


def ytdlp_installed_update_version() -> str | None:
    f = ytdlp_dir() / "VERSION"
    return f.read_text().strip() if f.is_file() else None


def check_ytdlp() -> dict:
    data = _get_json(PYPI_URL)
    latest = data["info"]["version"]
    wheel = next((u for u in data.get("urls", [])
                  if u.get("packagetype") == "bdist_wheel" and u["filename"].endswith("py3-none-any.whl")), None)
    if not wheel:
        raise RuntimeError("Couldn't find a download for the latest yt-dlp.")
    current = ytdlp_installed_update_version() or ytdlp_version()
    return {"current": current, "latest": latest, "url": wheel["url"],
            "available": is_newer(latest, current)}


def install_ytdlp(url: str, version: str, on_progress: ProgressFn | None = None,
                  cancel: Callable[[], bool] | None = None) -> str:
    data = _download(url, on_progress, cancel, "Downloading downloader update…")
    stage = updates_dir() / "yt-dlp.new"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        members = [m for m in z.namelist() if m.startswith("yt_dlp/")]
        if not any(m == "yt_dlp/__init__.py" for m in members):
            raise RuntimeError("The downloaded update looks broken - nothing was changed.")
        for m in members:
            z.extract(m, stage)
    (stage / "VERSION").write_text(version)
    target = ytdlp_dir()
    old = updates_dir() / "yt-dlp.old"
    shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        target.rename(old)
    stage.rename(target)
    shutil.rmtree(old, ignore_errors=True)
    if on_progress:
        on_progress(1.0, f"Downloader updated to {version} - relaunch to use it")
    return version


def reset_ytdlp() -> None:
    """Go back to the downloader that shipped inside the .exe."""
    shutil.rmtree(ytdlp_dir(), ignore_errors=True)


# ----------------------------------------------------------------------------- RumbleCut app
def install_kind() -> str:
    """'onefile' (single RumbleCut.exe - self-update supported), 'folder', or 'source'."""
    if not getattr(sys, "frozen", False):
        return "source"
    meipass = Path(getattr(sys, "_MEIPASS", ""))
    return "folder" if meipass.parent == Path(sys.executable).parent else "onefile"


def check_app(repo: str | None = None) -> dict:
    repo = (repo or config.load_settings().get("update_repo") or DEFAULT_REPO).strip().strip("/")
    repo = re.sub(r"^https?://github\.com/", "", repo)
    try:
        rel = _get_json(f"{GITHUB_API}/repos/{repo}/releases/latest")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(f"No published releases found at github.com/{repo} yet.") from e
        raise
    latest = rel.get("tag_name") or rel.get("name") or "0"
    asset = next((a for a in rel.get("assets", []) if a["name"].lower().endswith(".exe")), None)
    return {"current": __version__, "latest": latest.lstrip("vV"), "notes": (rel.get("body") or "").strip(),
            "url": asset["browser_download_url"] if asset else None,
            "page": rel.get("html_url") or f"https://github.com/{repo}/releases",
            "available": is_newer(latest, __version__)}


def download_app(url: str, on_progress: ProgressFn | None = None,
                 cancel: Callable[[], bool] | None = None) -> str:
    if install_kind() != "onefile":
        raise RuntimeError("Self-update works for the single-file RumbleCut.exe. "
                           "Download the new version from the release page instead.")
    exe = Path(sys.executable)
    new = exe.with_name(exe.stem + ".new.exe")
    try:
        _download(url, on_progress, cancel, "Downloading RumbleCut update…", dest=new)
    except PermissionError as e:
        raise RuntimeError(f"Can't write next to {exe.name}. Move RumbleCut.exe to a folder you own "
                           f"(e.g. Documents or Desktop) and try again.") from e
    with open(new, "rb") as fh:
        if fh.read(2) != b"MZ" or new.stat().st_size < 1_000_000:
            new.unlink(missing_ok=True)
            raise RuntimeError("The downloaded file isn't a valid program - nothing was changed.")
    return str(new)


def swap_in_new_exe(new_path: str) -> None:
    exe = Path(sys.executable)
    old = exe.with_name(exe.stem + ".old.exe")
    old.unlink(missing_ok=True)
    exe.rename(old)                 # allowed on Windows even while running
    try:
        Path(new_path).rename(exe)
    except OSError:
        old.rename(exe)             # roll back
        raise


# ----------------------------------------------------------------------------- relaunch
def relaunch_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve().parent.parent / "app.py")]


def spawn_new_instance() -> None:
    env = dict(os.environ)
    # a fresh onefile instance must not reuse this process's (soon deleted) unpack folder
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for k in list(env):
        if k.startswith("_PYI") or k.startswith("_MEIPASS"):
            env.pop(k, None)
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(relaunch_command(), env=env, close_fds=True, creationflags=flags,
                     cwd=str(Path(relaunch_command()[-1]).parent))
