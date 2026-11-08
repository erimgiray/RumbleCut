"""RumbleCut entry point (this is what becomes RumbleCut.exe)."""
import multiprocessing
import sys


def diagnose(out_path: str, with_asr: bool) -> int:
    """RumbleCut.exe --diagnose report.json [--asr]
    Checks every bundled component inside the built .exe (used by the release build)."""
    import json
    import traceback
    report, ok = {}, True

    def check(name, fn):
        nonlocal ok
        try:
            report[name] = {"ok": True, "info": fn()}
        except Exception as e:
            ok = False
            report[name] = {"ok": False, "error": f"{e}", "trace": traceback.format_exc()[-800:]}

    from rumblecut import __version__, updater
    report["version"] = __version__
    check("yt_dlp", lambda: updater.ytdlp_version())
    check("impersonation", lambda: __import__("rumblecut.downloader", fromlist=["x"])._impersonation()
          and "chrome via curl_cffi " + __import__("curl_cffi").__version__)
    check("ffmpeg", lambda: __import__("rumblecut.ffmpeg_tools", fromlist=["x"]).ffmpeg())
    check("qt_multimedia", lambda: __import__("PySide6.QtMultimedia", fromlist=["x"]) and "ok")
    check("ai_module", lambda: __import__("rumblecut.ai", fromlist=["x"]).describe())
    check("faster_whisper", lambda: __import__("faster_whisper").__version__)
    check("gpu_speedup_source", lambda: __import__("rumblecut.gpu_pack", fromlist=["x"]).find_wheel()["filename"])

    def voice_check():
        import tempfile, os
        from rumblecut import ffmpeg_tools as ft, voice
        d = tempfile.mkdtemp()
        p = os.path.join(d, "voice.mp3")
        ft.run([ft.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                "sine=f=180:d=6", "-c:a", "libmp3lame", p])
        vf = voice.analyse(p, 6)
        import numpy as np
        f0 = float(np.nanmedian(vf.pitch))
        assert 160 < f0 < 200, f"pitch tracker read {f0:.0f} Hz for a 180 Hz tone"
        return f"pitch tracker ok ({f0:.0f} Hz on a 180 Hz tone)"
    check("voice_analysis", voice_check)
    check("diction", lambda: len(__import__("rumblecut.diction", fromlist=["x"]).lexicon_spans(
        [(0.0, 3.0, "No way, that is insane! Ha ha ha")])) == 1 and "word list ok")
    if with_asr:
        def asr():
            import tempfile, os
            from rumblecut import ffmpeg_tools as ft, transcribe
            d = tempfile.mkdtemp()
            p = os.path.join(d, "tone.mp4")
            ft.run([ft.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=f=300:d=3", "-c:a", "aac", p])
            return f"model loaded and ran, {len(transcribe.transcribe(p, 'tiny'))} segment(s) on a test tone"
        check("speech_to_text", asr)
    report["all_ok"] = ok
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    return 0 if ok else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    from rumblecut import updater
    updater.activate_downloader_override()   # use an updated downloader if one was installed
    from rumblecut import gpu_pack
    gpu_pack.activate()                      # NVIDIA libraries for GPU speech-to-text, if installed
    updater.cleanup_old_exe()                # remove the previous version after a self-update
    if "--versions" in sys.argv:             # diagnostics:  RumbleCut.exe --versions
        from rumblecut import __version__
        import yt_dlp
        print(f"RumbleCut {__version__} ({updater.install_kind()}) | yt-dlp {updater.ytdlp_version()} from {yt_dlp.__file__}")
        sys.exit(0)
    if "--diagnose" in sys.argv:
        i = sys.argv.index("--diagnose")
        sys.exit(diagnose(sys.argv[i + 1] if len(sys.argv) > i + 1 else "diagnose.json", "--asr" in sys.argv))
    from rumblecut.ui.main_window import run_app
    sys.exit(run_app())
