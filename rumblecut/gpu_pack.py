"""Optional 'GPU speed-up' for speech-to-text on NVIDIA cards.

The Windows speech engine (CTranslate2) already ships cuDNN; what it still needs is
NVIDIA's cuBLAS (cublas64_12.dll + cublasLt64_12.dll). They are fetched once, from
NVIDIA's official package on PyPI, into %APPDATA%/RumbleCut/gpu and put on the DLL
search path at start-up. Without them, speech-to-text simply runs on the CPU.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from . import config
from .ffmpeg_tools import Cancelled

NEEDED = ("cublas64_12.dll", "cublasLt64_12.dll")
PYPI = "https://pypi.org/pypi/nvidia-cublas-cu12/json"


def gpu_dir() -> Path:
    return config.app_data_dir() / "gpu"


def installed() -> bool:
    return all((gpu_dir() / n).is_file() for n in NEEDED)


def activate() -> None:
    """Put the downloaded NVIDIA libraries on the DLL search path (call early at start-up)."""
    d = gpu_dir()
    if os.name != "nt" or not installed():
        return
    try:
        os.add_dll_directory(str(d))
    except (OSError, AttributeError):
        pass
    if str(d) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def nvidia_gpu_present() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def find_wheel() -> dict:
    """Newest Windows build of NVIDIA cuBLAS 12 on PyPI."""
    with urllib.request.urlopen(urllib.request.Request(PYPI, headers={"User-Agent": "RumbleCut"}), timeout=30) as r:
        data = json.load(r)
    best = None
    for ver, files in data.get("releases", {}).items():
        if not ver.startswith("12."):
            continue
        for f in files:
            if f["filename"].endswith("win_amd64.whl") and not f.get("yanked"):
                key = tuple(int(x) for x in re.findall(r"\d+", ver)[:4])
                if best is None or key > best[0]:
                    best = (key, {"version": ver, "url": f["url"], "size": f["size"], "filename": f["filename"]})
    if not best:
        raise RuntimeError("Couldn't find NVIDIA's Windows libraries on PyPI right now. Try again later.")
    return best[1]


def install(on_progress: Callable[[float, str], None] | None = None,
            cancel: Callable[[], bool] | None = None) -> str:
    w = find_wheel()
    tmp = Path(tempfile.mkdtemp(prefix="rumblecut_gpu_"))
    whl = tmp / w["filename"]
    try:
        req = urllib.request.Request(w["url"], headers={"User-Agent": "RumbleCut"})
        with urllib.request.urlopen(req, timeout=60) as r, open(whl, "wb") as fh:
            total = int(r.headers.get("Content-Length") or w["size"] or 0)
            done = 0
            while True:
                if cancel and cancel():
                    raise Cancelled()
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done / total if total else -1.0,
                                f"Downloading NVIDIA libraries… {done / 1e6:,.0f} / {total / 1e6:,.0f} MB")
        if on_progress:
            on_progress(-1.0, "Unpacking…")
        out = gpu_dir()
        stage = out.with_name("gpu.new")
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        with zipfile.ZipFile(whl) as z:
            names = {Path(n).name: n for n in z.namelist() if n.lower().endswith(".dll")}
            missing = [n for n in NEEDED if n not in names]
            if missing:
                raise RuntimeError(f"The NVIDIA package didn't contain {', '.join(missing)}.")
            for n in NEEDED:
                with z.open(names[n]) as src, open(stage / n, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
        (stage / "VERSION").write_text(w["version"])
        shutil.rmtree(out, ignore_errors=True)
        stage.rename(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    activate()
    return w["version"]


def remove() -> None:
    shutil.rmtree(gpu_dir(), ignore_errors=True)


def version() -> str | None:
    f = gpu_dir() / "VERSION"
    return f.read_text().strip() if f.is_file() else None
