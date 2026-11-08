# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for RumbleCut.exe   (run via build_exe.bat)
#   default          -> dist\RumbleCut\RumbleCut.exe   (folder build, starts fastest)
#   RUMBLECUT_ONEFILE=1 -> dist\RumbleCut.exe          (single file, slower first start)
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ONEFILE = os.environ.get("RUMBLECUT_ONEFILE") == "1"
ICON = "assets/icon.ico"

binaries = [(p, "ffmpeg") for p in ("vendor/ffmpeg/ffmpeg.exe", "vendor/ffmpeg/ffprobe.exe",
                                     "vendor/ffmpeg/ffmpeg", "vendor/ffmpeg/ffprobe")
            if os.path.exists(p)]
datas = [("assets", "assets")]
hidden = collect_submodules("yt_dlp")

try:  # browser impersonation (Rumble returns 403 without it)
    from PyInstaller.utils.hooks import collect_all
    _d, _b, _h = collect_all("curl_cffi")
    datas += _d
    binaries += _b
    hidden += _h
except Exception:
    pass

try:  # speech recognition (Whisper) - model files download on first use
    import faster_whisper  # noqa: F401
    from PyInstaller.utils.hooks import collect_all
    for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "tokenizers", "av"):
        try:
            _d, _b, _h = collect_all(pkg)
            datas += _d
            binaries += _b
            hidden += _h
        except Exception:
            pass
except ImportError:
    pass

excludes = ["tkinter", "matplotlib", "pkg_resources", "setuptools", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
            "PySide6.QtWebEngineQuick", "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtQuick3D",
            "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtDesigner"]

a = Analysis(["app.py"], pathex=[], binaries=binaries, datas=datas, hiddenimports=hidden,
             hookspath=[], runtime_hooks=[], excludes=excludes, noarchive=False)
pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="RumbleCut", console=False,
              icon=ICON, upx=False, debug=False, strip=False, runtime_tmpdir=None)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="RumbleCut", console=False,
              icon=ICON, upx=False, debug=False, strip=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="RumbleCut", upx=False, strip=False)
