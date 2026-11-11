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
    check("publish", lambda: (lambda pb: f"{', '.join(pb.PLATFORMS.values())} via {pb.REDIRECT_URI}")(
        __import__("rumblecut.publish", fromlist=["x"])))
    check("publish_tab", lambda: __import__("rumblecut.ui.publish_tab", fromlist=["x"]) and "ok")
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

        def script_editing():
            # real speech on Windows (built-in voice), word timings, clean-up suggestions, cut render
            import tempfile, os, subprocess
            from rumblecut import editor, ffmpeg_tools as ft, script_edit as se, transcribe
            d = tempfile.mkdtemp()
            wav = os.path.join(d, "speech.wav")
            said = "So I I think the attack ad totally backfired. And then the the voters noticed."
            if os.name == "nt":
                ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                      f"$s.SetOutputToWaveFile('{wav}'); $s.Speak('{said}'); $s.Dispose()")
                subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=120)
            if not os.path.exists(wav):
                ft.run([ft.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=f=300:d=4", wav])
            src = os.path.join(d, "talk.mp4")
            ft.run([ft.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=640x360:d=30", "-i", wav, "-shortest", "-c:v", "libx264", "-c:a", "aac", src])
            dur = ft.MediaInfo(src).duration
            words = transcribe.transcribe_words(src, 0.0, dur, "tiny")
            c = editor.Clip(source=src, start=0.0, end=dur, words=[list(w) for w in words])
            t, db = se.energy(src, 0.0, dur)
            sugg = se.quick_cleanup(c, se.Quiet(t, db))
            se.apply_cuts(c, [[x["start"], x["end"]] for x in sugg] or [[dur * 0.3, dur * 0.45]])
            out = os.path.join(d, "cut.mp4")
            editor.render_clip(c, editor.ExportSettings(height=360), out)
            got = ft.MediaInfo(out).duration
            if abs(got - c.out_length) > 0.35:
                raise RuntimeError(f"cut render is {got:.2f}s, expected {c.out_length:.2f}s")
            return (f"{len(words)} words: {' '.join(w for _, _, w in words)[:90]!r}; suggestions: "
                    f"{[x['kind'] + ':' + x['text'][:12] for x in sugg]}; {dur:.1f}s -> {got:.1f}s rendered")
        check("script_editing", script_editing)

        def context_images():
            # live: Wikimedia search + download, then render both looks with the bundled ffmpeg
            import tempfile, os
            from rumblecut import context_images as ci, editor, ffmpeg_tools as ft
            cands = ci.search_topic(ci.Topic("Strait of Hormuz", 0, 3, kind="place"), ["wikimedia"])
            if not cands:
                raise RuntimeError("Wikimedia returned no pictures")
            img = ci.download(cands[0].path_or_url)
            d = tempfile.mkdtemp()
            src = os.path.join(d, "src.mp4")
            ft.run([ft.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=640x360:d=4", "-f", "lavfi", "-i", "sine=d=4", "-shortest",
                    "-c:v", "libx264", "-c:a", "aac", src])
            c = editor.Clip(source=src, start=0, end=4, overlays=[
                editor.ImageOverlay(image=img, start=0.2, duration=1.5, label="Strait of Hormuz", show_label=True),
                editor.ImageOverlay(image=img, start=2.0, duration=1.5, style="full")])
            out = os.path.join(d, "out.mp4")
            editor.render_clip(c, editor.ExportSettings(height=360), out)
            return (f"{len(cands)} pictures; first: {cands[0].title} by {cands[0].credit} ({cands[0].licence}), "
                    f"{os.path.getsize(img) // 1024} KB {os.path.splitext(img)[1]}; render ok "
                    f"{os.path.getsize(out) // 1024} KB")
        check("context_images", context_images)
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
    if "--publish-due" in sys.argv:          # started by Windows at a scheduled post's time
        from datetime import datetime
        from rumblecut import config, publish
        logf = config.app_data_dir() / "publish.log"

        def log(line):
            with open(logf, "a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")
        try:
            publish.run_due(on_log=log)
        except Exception as e:
            log(f"failed: {e}")
        sys.exit(0)
    from rumblecut.ui.main_window import run_app
    sys.exit(run_app())
