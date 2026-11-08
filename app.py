"""RumbleCut entry point (this is what becomes RumbleCut.exe)."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from rumblecut import updater
    updater.activate_downloader_override()   # use an updated downloader if one was installed
    updater.cleanup_old_exe()                # remove the previous version after a self-update
    if "--versions" in sys.argv:             # diagnostics:  RumbleCut.exe --versions
        from rumblecut import __version__
        import yt_dlp
        print(f"RumbleCut {__version__} ({updater.install_kind()}) | yt-dlp {updater.ytdlp_version()} from {yt_dlp.__file__}")
        sys.exit(0)
    from rumblecut.ui.main_window import run_app
    sys.exit(run_app())
