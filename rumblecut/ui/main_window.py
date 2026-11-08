"""Main window: Get Video → Clip Studio → Export, plus the Context Images (coming soon) tab."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QKeySequence, QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QSplitter, QStatusBar,
    QStyle, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from .. import APP_NAME, __version__, analysis, config, context_images, downloader, editor
from .. import ffmpeg_tools as ft
from .. import transcribe, updater
from .timeline import Timeline, fmt_time
from .workers import Worker

try:
    if os.environ.get("RUMBLECUT_NO_PREVIEW"):
        raise ImportError("preview disabled")
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAVE_MM = True
except Exception:  # pragma: no cover - multimedia missing
    HAVE_MM = False

ACCENT = "#85C742"


def asset(name: str) -> str:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return str(base / "assets" / name)


def open_folder(path: str | Path):
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def section(title: str) -> QLabel:
    l = QLabel(title)
    l.setObjectName("section")
    return l


# ============================================================================ Main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        if os.path.exists(asset("icon.png")):
            self.setWindowIcon(QIcon(asset("icon.png")))
        self.resize(1440, 900)
        self.project = editor.Project()
        self.result: analysis.AnalysisResult | None = None
        self.info: ft.MediaInfo | None = None
        self.project_path: str | None = None
        self.workers: list[Worker] = []
        self.thumb_dir = Path(tempfile.mkdtemp(prefix="rumblecut_thumbs_"))
        self._loading_inspector = False
        self._clip_stop_at: float | None = None

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._build_get_tab(), "①  Get video")
        self.tabs.addTab(self._build_studio_tab(), "②  Clip studio")
        self.tabs.addTab(self._build_export_tab(), "③  Export")
        self.tabs.addTab(self._build_context_tab(), "✨  Context images  (coming soon)")
        self.settings_index = self.tabs.addTab(self._build_settings_tab(), "⚙  Settings")
        self.tabs.currentChanged.connect(lambda i: i == self.settings_index and self._load_about())
        self._build_menu()
        self.setStatusBar(QStatusBar())
        self._check_ffmpeg()
        self._refresh_history()
        if config.load_settings().get("auto_update_check", True):
            QTimer.singleShot(3000, lambda: self.check_updates(silent=True))

    # ------------------------------------------------------------------ menu
    def _build_menu(self):
        m = self.menuBar().addMenu("&File")
        for text, key, fn in [
            ("Open video…", QKeySequence.Open, self.open_video_dialog),
            ("Open project…", "Ctrl+Shift+O", self.open_project),
            ("Save project", QKeySequence.Save, self.save_project),
            ("Save project as…", "Ctrl+Shift+S", lambda: self.save_project(as_new=True)),
        ]:
            a = QAction(text, self)
            a.setShortcut(key)
            a.triggered.connect(fn)
            m.addAction(a)
        m.addSeparator()
        a = QAction("Settings…", self)
        a.triggered.connect(self.open_settings)
        m.addAction(a)
        a = QAction("Quit", self)
        a.triggered.connect(self.close)
        m.addAction(a)
        h = self.menuBar().addMenu("&Help")
        a = QAction("About", self)
        a.triggered.connect(lambda: QMessageBox.about(
            self, APP_NAME, f"<b>{APP_NAME} {__version__}</b><br>Download Rumble streams, find the best "
            "moments automatically, and export them as polished clips.<br><br>Only download and re-publish "
            "videos you own or have permission to use."))
        h.addAction(a)

    def _check_ffmpeg(self):
        try:
            ft.ffmpeg(), ft.ffprobe()
        except ft.FFmpegError as e:
            QTimer.singleShot(300, lambda: QMessageBox.warning(self, "FFmpeg not found", str(e)))

    # ------------------------------------------------------------------ tab 1: get video
    def _build_get_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(40, 30, 40, 30)
        v.setSpacing(14)
        title = QLabel("Download a Rumble video or past livestream")
        title.setObjectName("h1")
        v.addWidget(title)
        v.addWidget(QLabel("Paste the link from the video's page (e.g. https://rumble.com/v4abcd-…). "
                           "Long streams are fine - downloads resume if interrupted."))
        row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://rumble.com/…")
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.returnPressed.connect(self.start_download)
        self.url_edit.textChanged.connect(lambda _: setattr(self, "_link_duration", 0.0))
        self.quality = QComboBox()
        self.quality.addItems(list(downloader.QUALITY_FORMATS))
        self.quality.setCurrentText(config.load_settings().get("download_quality", "1080p"))
        self.btn_info = QPushButton("Check link")
        self.btn_info.clicked.connect(self.check_link)
        self.btn_dl = QPushButton("Download")
        self.btn_dl.setObjectName("primary")
        self.btn_dl.clicked.connect(self.start_download)
        row.addWidget(self.url_edit, 1)
        row.addWidget(self.quality)
        row.addWidget(self.btn_info)
        row.addWidget(self.btn_dl)
        v.addLayout(row)
        self.link_info = QLabel("")
        self.link_info.setWordWrap(True)
        v.addWidget(self.link_info)

        part = QGroupBox()
        pg = QGridLayout(part)
        self.part_only = QCheckBox("Only download part of the video")
        self.part_only.setToolTip("Skip the parts you don't need - much faster for long streams")
        self.ranges_edit = QLineEdit()
        self.ranges_edit.setPlaceholderText("e.g.  1:05:00-1:20:00, 2:10:30-2:16:00     (h:mm:ss or mm:ss; "
                                            "leave the end blank for 'to the end')")
        self.ranges_edit.setEnabled(False)
        self.ranges_edit.returnPressed.connect(self.start_download)
        self.ranges_info = QLabel("Each range is saved as its own file. Tip: press “Check link” first "
                                  "to see the video's length.")
        self.ranges_info.setObjectName("muted")
        self.ranges_info.setWordWrap(True)
        self.part_only.toggled.connect(self.ranges_edit.setEnabled)
        self.part_only.toggled.connect(lambda on: on and self.ranges_edit.setFocus())
        self.part_only.toggled.connect(lambda _: self._update_ranges_info())
        self.ranges_edit.textChanged.connect(lambda _: self._update_ranges_info())
        pg.addWidget(self.part_only, 0, 0)
        pg.addWidget(self.ranges_edit, 0, 1)
        pg.addWidget(self.ranges_info, 1, 1)
        pg.setColumnStretch(1, 1)
        v.addWidget(part)
        self._link_duration = 0.0
        prow = QHBoxLayout()
        self.dl_progress = QProgressBar()
        self.dl_progress.setTextVisible(True)
        self.dl_progress.setFormat("")
        self.btn_dl_cancel = QPushButton("Cancel")
        self.btn_dl_cancel.setEnabled(False)
        prow.addWidget(self.dl_progress, 1)
        prow.addWidget(self.btn_dl_cancel)
        v.addLayout(prow)
        self.dl_status = QLabel("")
        v.addWidget(self.dl_status)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        v.addWidget(sep)
        orow = QHBoxLayout()
        orow.addWidget(section("Your downloads"))
        orow.addStretch()
        b = QPushButton("Open a video from my PC…")
        b.clicked.connect(self.open_video_dialog)
        orow.addWidget(b)
        b2 = QPushButton("Open downloads folder")
        b2.clicked.connect(lambda: open_folder(config.download_dir()))
        orow.addWidget(b2)
        v.addLayout(orow)
        self.history = QListWidget()
        self.history.itemDoubleClicked.connect(self._open_history_item)
        v.addWidget(self.history, 1)
        hint = QLabel("Double-click a download to start clipping it.")
        hint.setObjectName("muted")
        v.addWidget(hint)
        return w

    def _refresh_history(self):
        self.history.clear()
        hist = config.load_settings().get("history", [])
        for h in hist:
            if not os.path.exists(h.get("path", "")):
                continue
            length = h.get("length") or h.get("duration", 0)
            it = QListWidgetItem(f"{h.get('title', Path(h['path']).stem)}    ·    {fmt_time(length)}"
                                 f"    ·    {h.get('uploader', '')}")
            it.setData(Qt.UserRole, h["path"])
            it.setData(Qt.UserRole + 1, h)
            it.setToolTip(h["path"])
            self.history.addItem(it)
        if self.history.count() == 0:
            for p in sorted(config.download_dir().glob("*.mp4"), key=os.path.getmtime, reverse=True)[:30]:
                it = QListWidgetItem(p.stem)
                it.setData(Qt.UserRole, str(p))
                self.history.addItem(it)

    def check_link(self):
        url = self.url_edit.text().strip()
        if not url:
            return
        self.link_info.setText("Checking…")
        w = Worker(lambda url, on_progress=None, cancel=None: downloader.fetch_info(url), url)
        w.done.connect(lambda info: setattr(self, "_link_duration", float(info.get("duration") or 0))
                       or self._update_ranges_info())
        w.done.connect(lambda info: self.link_info.setText(
            f"<b>{info.get('title', '?')}</b> — {info.get('uploader', '')} — "
            f"{fmt_time(info.get('duration') or 0)}"
            + ("  <span style='color:#f59e0b'>(still live - wait for the replay)</span>"
               if info.get("is_live") else "")))
        w.failed.connect(lambda m: self.link_info.setText(f"<span style='color:#f87171'>{m}</span>"))
        self._start(w)

    def start_download(self):
        url = self.url_edit.text().strip()
        if not url:
            self.url_edit.setFocus()
            return
        if not downloader.is_rumble_url(url):
            if QMessageBox.question(self, "Not a Rumble link",
                                    "This doesn't look like a rumble.com link. Try anyway?") != QMessageBox.Yes:
                return
        ranges = None
        if self.part_only.isChecked():
            try:
                ranges = downloader.parse_ranges(self.ranges_edit.text(), self._link_duration or None)
            except ValueError as e:
                QMessageBox.warning(self, "Check the times", str(e))
                self.ranges_edit.setFocus()
                return
        config.save_settings({"download_quality": self.quality.currentText()})
        self.btn_dl.setEnabled(False)
        self.btn_dl_cancel.setEnabled(True)
        self.dl_progress.setValue(0)
        self.dl_status.setText("Starting download…")
        w = Worker(downloader.download, url, self.quality.currentText(), config.download_dir(), ranges)
        self.btn_dl_cancel.clicked.connect(w.cancel)

        def prog(f, msg):
            if f < 0:                       # size unknown -> moving "busy" bar
                self.dl_progress.setRange(0, 0)
            else:
                self.dl_progress.setRange(0, 1000)
                self.dl_progress.setValue(int(f * 1000))
                self.dl_progress.setFormat(f"{f * 100:.1f}%")
            self.dl_status.setText(msg)

        def finished(res: downloader.DownloadResult):
            if res.sections:
                entries = [{"path": s.path, "url": res.url, "uploader": res.uploader,
                            "title": f"{res.title}  [{downloader.fmt(s.start)} – "
                                     f"{downloader.fmt(s.end) if s.end else 'end'}]",
                            "offset": s.start, "length": (s.end - s.start) if s.end else 0,
                            "duration": res.duration} for s in res.sections]
            else:
                entries = [{"path": res.path, "title": res.title, "uploader": res.uploader,
                            "duration": res.duration, "url": res.url}]
            for e in entries:   # open-ended parts: read the real length from the file
                if not e.get("length") and res.sections:
                    try:
                        e["length"] = ft.MediaInfo(e["path"]).duration
                    except Exception:
                        pass
            paths = {e["path"] for e in entries}
            hist = [h for h in config.load_settings().get("history", []) if h.get("path") not in paths]
            config.save_settings({"history": (entries + hist)[:200]})
            self._refresh_history()
            if len(entries) > 1:
                self.dl_status.setText(f"Saved {len(entries)} parts to {config.download_dir()}. Opened part 1 - "
                                       f"double-click the others under “Your downloads”.")
            else:
                self.dl_status.setText(f"Saved to {entries[0]['path']}")
            e = entries[0]
            self.load_video(e["path"], title=e["title"], url=res.url, offset=e.get("offset", 0.0))

        def reset():
            self.btn_dl.setEnabled(True)
            self.btn_dl_cancel.setEnabled(False)
            if self.dl_progress.maximum() == 0:
                self.dl_progress.setRange(0, 1000)
                self.dl_progress.setValue(0)
            try:
                self.btn_dl_cancel.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass

        w.progress.connect(prog)
        w.done.connect(finished)
        w.failed.connect(lambda m: QMessageBox.warning(self, "Download failed", m))
        w.cancelled.connect(lambda: self.dl_status.setText("Download cancelled (partial file kept - "
                                                           "download again to resume)."))
        w.finished.connect(reset)
        self.dl_progress.setRange(0, 1000)
        self._start(w)

    # ------------------------------------------------------------------ tab 2: studio
    def _build_studio_tab(self) -> QWidget:
        root = QWidget()
        v = QVBoxLayout(root)
        v.setContentsMargins(10, 8, 10, 8)

        # analysis controls
        bar = QHBoxLayout()
        self.src_label = QLabel("No video loaded")
        self.src_label.setObjectName("h2")
        bar.addWidget(self.src_label, 1)
        s = config.load_settings()
        self.n_clips = QSpinBox(); self.n_clips.setRange(1, 60); self.n_clips.setValue(int(s["num_clips"]))
        self.clip_len = QSpinBox(); self.clip_len.setRange(5, 300); self.clip_len.setSuffix(" s")
        self.clip_len.setValue(int(s["clip_length"]))
        self.min_gap = QSpinBox(); self.min_gap.setRange(0, 1800); self.min_gap.setSuffix(" s")
        self.min_gap.setValue(int(s["min_gap"]))
        self.use_scenes = QCheckBox("Scene changes"); self.use_scenes.setChecked(bool(s["scene_detection"]))
        self.use_words = QCheckBox("Hype words (transcribe)")
        self.use_words.setChecked(bool(s["transcript_boost"]) and transcribe.available())
        self.use_words.setEnabled(transcribe.available())
        self.use_words.setToolTip("Transcribes the stream and boosts moments where hype phrases are said."
                                  if transcribe.available() else
                                  "Install the optional 'faster-whisper' package to enable.")
        for lbl, wdg in [("Clips", self.n_clips), ("Length", self.clip_len), ("Min gap", self.min_gap)]:
            bar.addWidget(QLabel(lbl))
            bar.addWidget(wdg)
        bar.addWidget(self.use_scenes)
        bar.addWidget(self.use_words)
        self.btn_analyse = QPushButton("Find highlights")
        self.btn_analyse.setObjectName("primary")
        self.btn_analyse.clicked.connect(self.run_analysis)
        bar.addWidget(self.btn_analyse)
        v.addLayout(bar)
        prow = QHBoxLayout()
        self.an_progress = QProgressBar(); self.an_progress.setRange(0, 1000); self.an_progress.setMaximumHeight(14)
        self.an_progress.setTextVisible(False)
        self.an_status = QLabel(""); self.an_status.setObjectName("muted")
        self.btn_an_cancel = QPushButton("Cancel"); self.btn_an_cancel.setEnabled(False)
        prow.addWidget(self.an_progress, 1); prow.addWidget(self.an_status); prow.addWidget(self.btn_an_cancel)
        v.addLayout(prow)

        self.timeline = Timeline()
        self.timeline.seek.connect(self.seek_to)
        self.timeline.clip_clicked.connect(lambda i: self.clip_list.setCurrentRow(i))
        v.addWidget(self.timeline)

        split = QSplitter(Qt.Horizontal)
        v.addWidget(split, 1)

        # left: clip list
        left = QWidget(); lv = QVBoxLayout(left); lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(section("Highlights  (tick = include in export, drag to reorder)"))
        self.clip_list = QListWidget()
        self.clip_list.setIconSize(QSize(128, 72))
        self.clip_list.setSpacing(2)
        self.clip_list.setWordWrap(True)
        self.clip_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.clip_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.clip_list.model().rowsMoved.connect(self._rows_moved)
        self.clip_list.currentRowChanged.connect(self.select_clip)
        self.clip_list.itemChanged.connect(self._item_checked)
        lv.addWidget(self.clip_list, 1)
        brow = QHBoxLayout()
        for text, tip, fn in [("＋ Add at playhead", "New clip around the current position", self.add_clip_here),
                              ("⧉ Duplicate", "", self.duplicate_clip), ("✖ Remove", "", self.remove_clip)]:
            b = QPushButton(text); b.setToolTip(tip); b.clicked.connect(fn); brow.addWidget(b)
        lv.addLayout(brow)
        split.addWidget(left)

        # centre: preview
        mid = QWidget(); mv = QVBoxLayout(mid); mv.setContentsMargins(0, 0, 0, 0)
        if HAVE_MM:
            self.video = QVideoWidget()
            self.video.setMinimumSize(480, 270)
            self.player = QMediaPlayer(self)
            self.audio = None
            if not os.environ.get("RUMBLECUT_NO_AUDIO"):   # for headless test machines
                self.audio = QAudioOutput(self)
                self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video)
            self.player.positionChanged.connect(self._pos_changed)
            mv.addWidget(self.video, 1)
        else:
            self.player = None
            mv.addWidget(QLabel("Video preview unavailable (Qt Multimedia missing)."), 1)
        tr = QHBoxLayout()
        st = self.style()
        self.btn_play = QToolButton(); self.btn_play.setIcon(st.standardIcon(QStyle.SP_MediaPlay))
        self.btn_play.setToolTip("Play / pause (Space)")
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_play_clip = QPushButton("▶ Play clip")
        self.btn_play_clip.clicked.connect(self.play_clip)
        self.pos_label = QLabel("0:00"); self.pos_label.setMinimumWidth(110)
        self.btn_set_in = QPushButton("[  Set start"); self.btn_set_in.setShortcut("I")
        self.btn_set_in.clicked.connect(lambda: self._set_edge("start"))
        self.btn_set_out = QPushButton("Set end  ]"); self.btn_set_out.setShortcut("O")
        self.btn_set_out.clicked.connect(lambda: self._set_edge("end"))
        back = QToolButton(); back.setText("−5s"); back.clicked.connect(lambda: self.nudge(-5))
        fwd = QToolButton(); fwd.setText("+5s"); fwd.clicked.connect(lambda: self.nudge(5))
        self.btn_preview = QPushButton("Preview with effects")
        self.btn_preview.setToolTip("Quick low-res render of the selected clip with all effects applied")
        self.btn_preview.clicked.connect(self.render_preview)
        self.vol = QSlider(Qt.Horizontal); self.vol.setRange(0, 100); self.vol.setValue(80); self.vol.setMaximumWidth(90)
        if self.player and self.audio:
            self.audio.setVolume(0.8)
            self.vol.valueChanged.connect(lambda x: self.audio.setVolume(x / 100))
        for wdg in [self.btn_play, back, fwd, self.pos_label, self.btn_play_clip, self.btn_set_in,
                    self.btn_set_out]:
            tr.addWidget(wdg)
        tr.addStretch()
        tr.addWidget(self.btn_preview)
        tr.addWidget(QLabel("🔊")); tr.addWidget(self.vol)
        mv.addLayout(tr)
        sp = QAction(self); sp.setShortcut(Qt.Key_Space); sp.triggered.connect(self.toggle_play)
        self.addAction(sp)
        split.addWidget(mid)

        # right: inspector
        split.addWidget(self._build_inspector())
        split.setSizes([400, 680, 360])
        return root

    def _build_inspector(self) -> QWidget:
        box = QWidget()
        f = QVBoxLayout(box)
        f.setContentsMargins(6, 0, 6, 0)

        g1 = QGroupBox("Clip")
        fl = QFormLayout(g1)
        self.i_title = QLineEdit(); self.i_title.setPlaceholderText("Clip title (used as the file name)")
        self.i_start = QDoubleSpinBox(); self.i_end = QDoubleSpinBox()
        for sb in (self.i_start, self.i_end):
            sb.setDecimals(1); sb.setSingleStep(0.5); sb.setRange(0, 1e7); sb.setSuffix(" s")
        self.i_len = QLabel("")
        self.i_why = QLabel(""); self.i_why.setWordWrap(True); self.i_why.setObjectName("muted")
        fl.addRow("Title", self.i_title)
        fl.addRow("Start", self.i_start)
        fl.addRow("End", self.i_end)
        fl.addRow("Length", self.i_len)
        fl.addRow("Why", self.i_why)
        f.addWidget(g1)

        g2 = QGroupBox("Effects")
        fl = QFormLayout(g2)
        self.i_speed = QComboBox(); self.i_speed.addItems(["0.5×", "0.75×", "1×", "1.25×", "1.5×", "2×"])
        self.i_zoom = QSlider(Qt.Horizontal); self.i_zoom.setRange(100, 200)
        self.i_zoom_lbl = QLabel("100%")
        zrow = QHBoxLayout(); zrow.addWidget(self.i_zoom, 1); zrow.addWidget(self.i_zoom_lbl)
        zw = QWidget(); zw.setLayout(zrow); zrow.setContentsMargins(0, 0, 0, 0)
        self.i_color = QComboBox(); self.i_color.addItems(list(editor.COLOR_LOOKS))
        self.i_vignette = QCheckBox("Vignette")
        self.i_fadein = QDoubleSpinBox(); self.i_fadeout = QDoubleSpinBox()
        for sb in (self.i_fadein, self.i_fadeout):
            sb.setRange(0, 5); sb.setSingleStep(0.1); sb.setSuffix(" s")
        self.i_norm = QCheckBox("Even out loudness")
        self.i_caption = QPlainTextEdit(); self.i_caption.setPlaceholderText("Caption text (optional)")
        self.i_caption.setMaximumHeight(60)
        self.i_cappos = QComboBox(); self.i_cappos.addItems(["bottom", "top"])
        self.i_trans = QComboBox(); self.i_trans.addItems(["Default"] + list(editor.TRANSITIONS))
        fl.addRow("Speed", self.i_speed)
        fl.addRow("Punch-in zoom", zw)
        fl.addRow("Colour look", self.i_color)
        fl.addRow("", self.i_vignette)
        fl.addRow("Fade in", self.i_fadein)
        fl.addRow("Fade out", self.i_fadeout)
        fl.addRow("Audio", self.i_norm)
        fl.addRow("Caption", self.i_caption)
        fl.addRow("Caption at", self.i_cappos)
        fl.addRow("Transition in", self.i_trans)
        f.addWidget(g2)

        g3 = QGroupBox("Images on screen")
        gl = QVBoxLayout(g3)
        self.i_ovl = QListWidget(); self.i_ovl.setMaximumHeight(90)
        self.i_ovl.currentRowChanged.connect(self._overlay_selected)
        gl.addWidget(self.i_ovl)
        orow = QHBoxLayout()
        b = QPushButton("＋ Image at playhead"); b.clicked.connect(self.add_overlay)
        b2 = QPushButton("✖"); b2.setMaximumWidth(36); b2.clicked.connect(self.remove_overlay)
        self.btn_auto_img = QPushButton("✨ Auto"); self.btn_auto_img.setEnabled(False)
        self.btn_auto_img.setToolTip("Context Images - coming soon")
        orow.addWidget(b); orow.addWidget(b2); orow.addWidget(self.btn_auto_img)
        gl.addLayout(orow)
        of = QFormLayout()
        self.o_pos = QComboBox(); self.o_pos.addItems(editor.OVERLAY_POSITIONS)
        self.o_start = QDoubleSpinBox(); self.o_start.setRange(0, 3600); self.o_start.setSuffix(" s")
        self.o_dur = QDoubleSpinBox(); self.o_dur.setRange(0.5, 600); self.o_dur.setSuffix(" s")
        self.o_scale = QSpinBox(); self.o_scale.setRange(10, 100); self.o_scale.setSuffix(" %")
        of.addRow("Position", self.o_pos); of.addRow("Appears at", self.o_start)
        of.addRow("Shows for", self.o_dur); of.addRow("Size", self.o_scale)
        gl.addLayout(of)
        f.addWidget(g3)
        f.addStretch()

        # wiring
        self.i_title.textEdited.connect(lambda t: self._set("title", t))
        self.i_start.valueChanged.connect(lambda x: self._set("start", x))
        self.i_end.valueChanged.connect(lambda x: self._set("end", x))
        self.i_speed.currentTextChanged.connect(lambda t: self._set("speed", float(t.rstrip("×"))))
        self.i_zoom.valueChanged.connect(lambda x: (self.i_zoom_lbl.setText(f"{x}%"), self._set("zoom", x / 100)))
        self.i_color.currentTextChanged.connect(lambda t: self._set("color", t))
        self.i_vignette.toggled.connect(lambda b: self._set("vignette", b))
        self.i_fadein.valueChanged.connect(lambda x: self._set("fade_in", x))
        self.i_fadeout.valueChanged.connect(lambda x: self._set("fade_out", x))
        self.i_norm.toggled.connect(lambda b: self._set("normalize_audio", b))
        self.i_caption.textChanged.connect(lambda: self._set("caption", self.i_caption.toPlainText()))
        self.i_cappos.currentTextChanged.connect(lambda t: self._set("caption_pos", t))
        self.i_trans.currentTextChanged.connect(lambda t: self._set("transition", t))
        for wdg, attr, conv in [(self.o_pos, "position", None), (self.o_start, "start", None),
                                (self.o_dur, "duration", None), (self.o_scale, "scale", lambda x: x / 100)]:
            sig = wdg.currentTextChanged if isinstance(wdg, QComboBox) else wdg.valueChanged
            sig.connect(lambda val, a=attr, c=conv: self._set_overlay(a, c(val) if c else val))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(box)
        scroll.setMinimumWidth(330)
        self.inspector = box
        box.setEnabled(False)
        return scroll

    # ------------------------------------------------------------------ video loading
    def open_video_dialog(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open video", str(config.download_dir()),
                                           "Video files (*.mp4 *.mkv *.mov *.webm *.avi *.flv *.ts *.m4v);;All files (*)")
        if p:
            self.load_video(p)

    def _open_history_item(self, it: QListWidgetItem):
        h = it.data(Qt.UserRole + 1) or {}
        self.load_video(it.data(Qt.UserRole), title=h.get("title", ""), url=h.get("url", ""),
                        offset=float(h.get("offset", 0.0)))

    def _update_ranges_info(self):
        if not self.part_only.isChecked():
            self.ranges_info.setText("Each range is saved as its own file. Tip: press “Check link” first "
                                     "to see the video's length.")
            return
        txt = self.ranges_edit.text().strip()
        if not txt:
            self.ranges_info.setText("Type the start and end of each part you want, separated by commas.")
            return
        dur = self._link_duration or None
        try:
            rs = downloader.parse_ranges(txt, dur)
        except ValueError as e:
            self.ranges_info.setText(f"<span style='color:#f87171'>{e}</span>")
            return
        total = downloader.ranges_total(rs, dur)
        desc = ",  ".join(f"{downloader.fmt(s)} → {downloader.fmt(e) if e is not None else 'end'}" for s, e in rs)
        msg = f"<span style='color:{ACCENT}'>✔ {len(rs)} part{'s' if len(rs) > 1 else ''}</span>:  {desc}"
        if total is not None:
            msg += f"   ·   {fmt_time(total)} in total"
            if dur:
                msg += f"  (skips {100 * (1 - total / dur):.0f}% of the video)"
        self.ranges_info.setText(msg)

    def load_video(self, path: str, title: str = "", url: str = "", offset: float = 0.0):
        try:
            self.info = ft.MediaInfo(path)
        except Exception as e:
            QMessageBox.warning(self, "Can't open video", str(e))
            return
        self.project = editor.Project(source=path, title=title or Path(path).stem, url=url,
                                      offset=float(offset or 0.0), export=self.project.export)
        self.project_path = None
        self.result = None
        part = (f"   ·   part starting at {fmt_time(self.project.offset)} in the stream"
                if self.project.offset else "")
        self.src_label.setText(f"{self.project.title}   ·   {fmt_time(self.info.duration)}   ·   "
                               f"{self.info.width}×{self.info.height}{part}")
        self.timeline.set_data(self.info.duration)
        self.timeline.set_clips([])
        self._fill_clip_list()
        if self.player:
            self.player.setSource(QUrl.fromLocalFile(path))
            self.player.pause()
        self.tabs.setCurrentIndex(1)
        self.statusBar().showMessage(f"Loaded {path}", 5000)
        self.btn_analyse.setFocus()

    # ------------------------------------------------------------------ analysis
    def run_analysis(self):
        if not self.project.source:
            QMessageBox.information(self, APP_NAME, "Load a video first (tab ①).")
            return
        if self.project.clips and QMessageBox.question(
                self, "Replace clips?", "Finding highlights again replaces your current clip list. Continue?") \
                != QMessageBox.Yes:
            return
        config.save_settings({"num_clips": self.n_clips.value(), "clip_length": self.clip_len.value(),
                              "min_gap": self.min_gap.value(), "scene_detection": self.use_scenes.isChecked(),
                              "transcript_boost": self.use_words.isChecked()})
        src = self.project.source
        n, L, gap = self.n_clips.value(), self.clip_len.value(), self.min_gap.value()
        scenes, words = self.use_scenes.isChecked(), self.use_words.isChecked()
        dur = self.info.duration if self.info else 0
        model = config.load_settings().get("whisper_model", "small")

        def job(on_progress=None, cancel=None):
            transcript = None
            if words:
                transcript = transcribe.transcribe(
                    src, model, dur, cancel=cancel,
                    on_progress=lambda f, m: on_progress(f * 0.5, m))
                prog = lambda f, m: on_progress(0.5 + f * 0.5, m)
            else:
                prog = on_progress
            return analysis.find_highlights(src, n, L, gap, scenes, transcript,
                                            on_progress=prog, cancel=cancel)

        w = Worker(job)
        self.btn_analyse.setEnabled(False)
        self.btn_an_cancel.setEnabled(True)
        self.btn_an_cancel.clicked.connect(w.cancel)
        w.progress.connect(lambda f, m: (self.an_progress.setValue(int(f * 1000)), self.an_status.setText(m)))
        w.done.connect(self._analysis_done)
        w.failed.connect(lambda m: QMessageBox.warning(self, "Analysis failed", m))
        w.cancelled.connect(lambda: self.an_status.setText("Cancelled"))

        def reset():
            self.btn_analyse.setEnabled(True)
            self.btn_an_cancel.setEnabled(False)
            try:
                self.btn_an_cancel.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass
        w.finished.connect(reset)
        self._start(w)

    def _analysis_done(self, res: analysis.AnalysisResult):
        self.result = res
        self.project.clips = [
            editor.Clip(source=self.project.source, start=h.start, end=h.end, score=h.score,
                        reasons=h.reasons,
                        title=f"Highlight {i} - {fmt_time(h.peak + self.project.offset).replace(':', '.')}")
            for i, h in enumerate(res.highlights, 1)
        ]
        self.timeline.set_data(res.duration, res.times, res.score)
        self._fill_clip_list()
        if self.project.clips:
            self.clip_list.setCurrentRow(0)
        self._make_thumbnails()
        self.an_status.setText(f"Found {len(res.highlights)} highlights - ranked best first")

    def _make_thumbnails(self):
        src = self.project.source
        jobs = [(i, (c.start + c.end) / 2) for i, c in enumerate(self.project.clips)]
        out_dir = self.thumb_dir

        def job(on_progress=None, cancel=None):
            res = {}
            for i, t in jobs:
                if cancel():
                    break
                p = out_dir / f"{abs(hash((src, round(t, 1))))}.jpg"
                try:
                    if not p.exists():
                        ft.extract_frame(src, t, str(p), 256)
                    res[round(t, 1)] = str(p)
                    on_progress((i + 1) / len(jobs), "thumb")
                except Exception:
                    pass
            return res
        w = Worker(job)
        w.background = True
        w.done.connect(self._apply_thumbs)
        self._start(w)

    def _apply_thumbs(self, res: dict):
        self._thumbs = getattr(self, "_thumbs", {})
        self._thumbs.update(res)
        self._fill_clip_list(keep_row=True)

    # ------------------------------------------------------------------ clip list
    def _fill_clip_list(self, keep_row=False):
        row = self.clip_list.currentRow()
        self.clip_list.blockSignals(True)
        self.clip_list.clear()
        thumbs = getattr(self, "_thumbs", {})
        for i, c in enumerate(self.project.clips):
            reason = c.reasons[0] if c.reasons else "Manual clip"
            it = QListWidgetItem(f"{i + 1}. {c.title}\n{fmt_time(c.start)} – {fmt_time(c.end)}  ·  "
                                 f"{c.out_length:.0f}s  ·  score {c.score:.0f}\n{reason}")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
            it.setCheckState(Qt.Checked if c.enabled else Qt.Unchecked)
            t = thumbs.get(round((c.start + c.end) / 2, 1))
            if t:
                it.setIcon(QIcon(QPixmap(t)))
            it.setData(Qt.UserRole, id(c))
            self.clip_list.addItem(it)
        self.clip_list.blockSignals(False)
        self.timeline.set_clips([(c.start, c.end) for c in self.project.clips], row if keep_row else -1)
        if keep_row and 0 <= row < self.clip_list.count():
            self.clip_list.setCurrentRow(row)
        elif not self.project.clips:
            self.inspector.setEnabled(False)

    def _rows_moved(self, *_):
        by_id = {id(c): c for c in self.project.clips}
        self.project.clips = [by_id[self.clip_list.item(i).data(Qt.UserRole)]
                              for i in range(self.clip_list.count())]
        QTimer.singleShot(0, lambda: self._fill_clip_list(keep_row=True))

    def _item_checked(self, it: QListWidgetItem):
        r = self.clip_list.row(it)
        if 0 <= r < len(self.project.clips):
            self.project.clips[r].enabled = it.checkState() == Qt.Checked

    def current_clip(self) -> editor.Clip | None:
        r = self.clip_list.currentRow()
        return self.project.clips[r] if 0 <= r < len(self.project.clips) else None

    def select_clip(self, row: int):
        c = self.current_clip()
        self.inspector.setEnabled(c is not None)
        self.timeline.set_clips([(x.start, x.end) for x in self.project.clips], row)
        if not c:
            return
        self._loading_inspector = True
        dur = self.info.duration if self.info else 1e7
        self.i_start.setMaximum(dur); self.i_end.setMaximum(dur)
        self.i_title.setText(c.title)
        self.i_start.setValue(c.start)
        self.i_end.setValue(c.end)
        self.i_len.setText(f"{c.length:.1f}s  →  {c.out_length:.1f}s exported")
        self.i_why.setText(" · ".join(c.reasons) or "Added manually")
        self.i_speed.setCurrentText(f"{c.speed:g}×")
        self.i_zoom.setValue(int(round(c.zoom * 100)))
        self.i_color.setCurrentText(c.color)
        self.i_vignette.setChecked(c.vignette)
        self.i_fadein.setValue(c.fade_in)
        self.i_fadeout.setValue(c.fade_out)
        self.i_norm.setChecked(c.normalize_audio)
        self.i_caption.setPlainText(c.caption)
        self.i_cappos.setCurrentText(c.caption_pos)
        self.i_trans.setCurrentText(c.transition)
        self._fill_overlays()
        self._loading_inspector = False
        self.seek_to(c.start)

    def _set(self, attr, value):
        if self._loading_inspector:
            return
        c = self.current_clip()
        if not c:
            return
        setattr(c, attr, value)
        if attr in ("start", "end"):
            if c.end <= c.start + 0.5:
                if attr == "start":
                    c.end = c.start + 1
                    self.i_end.blockSignals(True); self.i_end.setValue(c.end); self.i_end.blockSignals(False)
                else:
                    c.start = max(0, c.end - 1)
                    self.i_start.blockSignals(True); self.i_start.setValue(c.start); self.i_start.blockSignals(False)
            self.seek_to(c.start if attr == "start" else max(c.start, c.end - 3))
        if attr in ("start", "end", "speed"):
            self.i_len.setText(f"{c.length:.1f}s  →  {c.out_length:.1f}s exported")
        if attr in ("title", "start", "end", "speed"):
            self._update_item_text(self.clip_list.currentRow())

    def _update_item_text(self, r):
        if not (0 <= r < len(self.project.clips)):
            return
        c = self.project.clips[r]
        reason = c.reasons[0] if c.reasons else "Manual clip"
        self.clip_list.item(r).setText(f"{r + 1}. {c.title}\n{fmt_time(c.start)} – {fmt_time(c.end)}  ·  "
                                       f"{c.out_length:.0f}s  ·  score {c.score:.0f}\n{reason}")
        self.timeline.set_clips([(x.start, x.end) for x in self.project.clips], r)

    def add_clip_here(self):
        if not self.project.source:
            return
        t = self._player_pos()
        dur = self.info.duration if self.info else t + 30
        L = self.clip_len.value()
        s = max(0.0, t - L * 0.3)
        c = editor.Clip(source=self.project.source, start=round(s, 1), end=round(min(dur, s + L), 1),
                        title=f"My clip - {fmt_time(t).replace(':', '.')}", reasons=["Added manually"])
        r = max(0, self.clip_list.currentRow() + 1)
        self.project.clips.insert(r, c)
        self._fill_clip_list()
        self.clip_list.setCurrentRow(r)

    def duplicate_clip(self):
        c = self.current_clip()
        if not c:
            return
        from dataclasses import replace
        d = replace(c, title=c.title + " (copy)", overlays=[replace(o) for o in c.overlays],
                    reasons=list(c.reasons))
        r = self.clip_list.currentRow() + 1
        self.project.clips.insert(r, d)
        self._fill_clip_list()
        self.clip_list.setCurrentRow(r)

    def remove_clip(self):
        r = self.clip_list.currentRow()
        if 0 <= r < len(self.project.clips):
            del self.project.clips[r]
            self._fill_clip_list()
            if self.project.clips:
                self.clip_list.setCurrentRow(min(r, len(self.project.clips) - 1))

    # ------------------------------------------------------------------ overlays
    def _fill_overlays(self):
        c = self.current_clip()
        self.i_ovl.blockSignals(True)
        self.i_ovl.clear()
        for o in (c.overlays if c else []):
            self.i_ovl.addItem(f"{Path(o.image).name}  @ {o.start:.1f}s  ({o.position})")
        self.i_ovl.blockSignals(False)
        has = bool(c and c.overlays)
        for w in (self.o_pos, self.o_start, self.o_dur, self.o_scale):
            w.setEnabled(has)
        if has:
            self.i_ovl.setCurrentRow(0)

    def _overlay_selected(self, r):
        c = self.current_clip()
        if not c or not (0 <= r < len(c.overlays)):
            return
        o = c.overlays[r]
        prev = self._loading_inspector
        self._loading_inspector = True
        self.o_pos.setCurrentText(o.position)
        self.o_start.setValue(o.start)
        self.o_dur.setValue(o.duration)
        self.o_scale.setValue(int(o.scale * 100))
        self._loading_inspector = prev

    def _set_overlay(self, attr, val):
        if self._loading_inspector:
            return
        c = self.current_clip()
        r = self.i_ovl.currentRow()
        if c and 0 <= r < len(c.overlays):
            setattr(c.overlays[r], attr, val)
            o = c.overlays[r]
            self.i_ovl.item(r).setText(f"{Path(o.image).name}  @ {o.start:.1f}s  ({o.position})")

    def add_overlay(self):
        c = self.current_clip()
        if not c:
            return
        p, _ = QFileDialog.getOpenFileName(self, "Choose image", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not p:
            return
        rel = max(0.0, min(c.length - 1, self._player_pos() - c.start)) / max(0.25, c.speed)
        c.overlays.append(editor.ImageOverlay(image=p, start=round(rel, 1), duration=4.0))
        self._fill_overlays()
        self.i_ovl.setCurrentRow(len(c.overlays) - 1)

    def remove_overlay(self):
        c = self.current_clip()
        r = self.i_ovl.currentRow()
        if c and 0 <= r < len(c.overlays):
            del c.overlays[r]
            self._fill_overlays()

    # ------------------------------------------------------------------ playback
    def _player_pos(self) -> float:
        return self.player.position() / 1000 if self.player else self.timeline.playhead

    def seek_to(self, t: float):
        self.timeline.set_playhead(t)
        self.pos_label.setText(fmt_time(t))
        if self.player:
            if self.player.source() != QUrl.fromLocalFile(self.project.source) and self.project.source:
                self.player.setSource(QUrl.fromLocalFile(self.project.source))
            self.player.setPosition(int(t * 1000))

    def nudge(self, dt):
        self.seek_to(max(0, self._player_pos() + dt))

    def toggle_play(self):
        if not self.player:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        else:
            self._clip_stop_at = None
            self.player.play()
            self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))

    def play_clip(self):
        c = self.current_clip()
        if not (c and self.player):
            return
        self.seek_to(c.start)
        self._clip_stop_at = c.end
        self.player.play()
        self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))

    def _pos_changed(self, ms):
        t = ms / 1000
        if self.player.source() == QUrl.fromLocalFile(self.project.source):
            self.timeline.set_playhead(t)
        self.pos_label.setText(fmt_time(t))
        if self._clip_stop_at is not None and t >= self._clip_stop_at:
            self._clip_stop_at = None
            self.player.pause()
            self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

    def _set_edge(self, which):
        c = self.current_clip()
        if not c:
            return
        t = round(self._player_pos(), 1)
        (self.i_start if which == "start" else self.i_end).setValue(t)

    def render_preview(self):
        c = self.current_clip()
        if not c:
            return
        s = editor.ExportSettings(**{**self.project.export.__dict__})
        s.height, s.quality = 480, "small"
        out = self.thumb_dir / f"preview_{abs(hash(id(c)))}.mp4"
        self.btn_preview.setEnabled(False)
        self.btn_preview.setText("Rendering…")
        w = Worker(lambda on_progress=None, cancel=None: editor.render_clip(
            c, s, out, on_progress=lambda f: on_progress(f, ""), cancel=cancel))
        w.progress.connect(lambda f, _: self.btn_preview.setText(f"Rendering… {f * 100:.0f}%"))

        def show(path):
            if self.player:
                self._clip_stop_at = None
                self.player.setSource(QUrl.fromLocalFile(str(path)))
                self.player.play()
                self.statusBar().showMessage("Playing effects preview - click the timeline or "
                                             "a clip to go back to the stream", 8000)
        w.done.connect(show)
        w.failed.connect(lambda m: QMessageBox.warning(self, "Preview failed", m))
        w.finished.connect(lambda: (self.btn_preview.setEnabled(True), self.btn_preview.setText("Preview with effects")))
        self._start(w)

    # ------------------------------------------------------------------ tab 3: export
    def _build_export_tab(self) -> QWidget:
        w = QWidget()
        outer = QHBoxLayout(w)
        outer.setContentsMargins(30, 20, 30, 20)
        left = QVBoxLayout()
        title = QLabel("Export your best clips")
        title.setObjectName("h1")
        left.addWidget(title)
        g = QGroupBox("Format")
        fl = QFormLayout(g)
        self.e_aspect = QComboBox(); self.e_aspect.addItems(list(editor.ASPECTS))
        self.e_res = QComboBox(); self.e_res.addItems(["2160", "1440", "1080", "720", "480"])
        self.e_res.setCurrentText("1080")
        self.e_fps = QComboBox(); self.e_fps.addItems(["24", "25", "30", "50", "60"]); self.e_fps.setCurrentText("30")
        self.e_quality = QComboBox(); self.e_quality.addItems(["high", "balanced", "small"])
        self.e_quality.setCurrentText(config.load_settings().get("export_quality", "high"))
        self.e_wm = QLineEdit(); self.e_wm.setPlaceholderText("e.g. @yourchannel  (optional)")
        fl.addRow("Shape", self.e_aspect)
        fl.addRow("Resolution", self.e_res)
        fl.addRow("Frame rate", self.e_fps)
        fl.addRow("Quality", self.e_quality)
        fl.addRow("Watermark text", self.e_wm)
        hint = QLabel("Vertical 9:16 is ready for YouTube Shorts / TikTok / Reels. Resolution is the "
                      "short side for vertical and square.")
        hint.setWordWrap(True); hint.setObjectName("muted")
        fl.addRow("", hint)
        left.addWidget(g)

        g2 = QGroupBox("What to export")
        fl2 = QFormLayout(g2)
        self.e_indiv = QCheckBox("Each ticked clip as its own video"); self.e_indiv.setChecked(True)
        self.e_thumbs = QCheckBox("Save a thumbnail image with each clip"); self.e_thumbs.setChecked(True)
        self.e_comp = QCheckBox("A 'Best moments' compilation joined with transitions")
        self.e_trans = QComboBox(); self.e_trans.addItems([k for k in editor.TRANSITIONS])
        self.e_trans.setCurrentText("Cross-fade")
        self.e_tdur = QDoubleSpinBox(); self.e_tdur.setRange(0.1, 3); self.e_tdur.setSingleStep(0.1)
        self.e_tdur.setValue(0.6); self.e_tdur.setSuffix(" s")
        fl2.addRow(self.e_indiv)
        fl2.addRow(self.e_thumbs)
        fl2.addRow(self.e_comp)
        fl2.addRow("Default transition", self.e_trans)
        fl2.addRow("Transition length", self.e_tdur)
        left.addWidget(g2)

        orow = QHBoxLayout()
        self.e_out = QLineEdit(str(config.export_dir()))
        b = QPushButton("Browse…")
        b.clicked.connect(lambda: (d := QFileDialog.getExistingDirectory(self, "Export folder", self.e_out.text()))
                          and self.e_out.setText(d))
        orow.addWidget(QLabel("Save to")); orow.addWidget(self.e_out, 1); orow.addWidget(b)
        left.addLayout(orow)
        brow = QHBoxLayout()
        self.btn_export = QPushButton("Export")
        self.btn_export.setObjectName("primary")
        self.btn_export.setMinimumHeight(40)
        self.btn_export.clicked.connect(self.run_export)
        self.btn_ex_cancel = QPushButton("Cancel"); self.btn_ex_cancel.setEnabled(False)
        brow.addWidget(self.btn_export, 1); brow.addWidget(self.btn_ex_cancel)
        left.addLayout(brow)
        self.ex_progress = QProgressBar(); self.ex_progress.setRange(0, 1000)
        self.ex_status = QLabel("")
        left.addWidget(self.ex_progress)
        left.addWidget(self.ex_status)
        left.addStretch()
        outer.addLayout(left, 3)

        right = QVBoxLayout()
        right.addWidget(section("Exported files"))
        self.ex_files = QListWidget()
        self.ex_files.itemDoubleClicked.connect(lambda it: QDesktopServices.openUrl(
            QUrl.fromLocalFile(it.data(Qt.UserRole))))
        right.addWidget(self.ex_files, 1)
        self.btn_open_out = QPushButton("Open export folder")
        self.btn_open_out.clicked.connect(lambda: open_folder(getattr(self, "_last_out", self.e_out.text())))
        right.addWidget(self.btn_open_out)
        outer.addLayout(right, 2)
        return w

    def _collect_export_settings(self):
        e = self.project.export
        e.aspect = self.e_aspect.currentText()
        e.height = int(self.e_res.currentText())
        e.fps = int(self.e_fps.currentText())
        e.quality = self.e_quality.currentText()
        e.watermark = self.e_wm.text()
        e.individual = self.e_indiv.isChecked()
        e.compilation = self.e_comp.isChecked()
        e.thumbnails = self.e_thumbs.isChecked()
        e.transition = self.e_trans.currentText()
        e.transition_duration = self.e_tdur.value()

    def _apply_export_settings(self):
        e = self.project.export
        self.e_aspect.setCurrentText(e.aspect); self.e_res.setCurrentText(str(e.height))
        self.e_fps.setCurrentText(str(e.fps)); self.e_quality.setCurrentText(e.quality)
        self.e_wm.setText(e.watermark); self.e_indiv.setChecked(e.individual)
        self.e_comp.setChecked(e.compilation); self.e_thumbs.setChecked(e.thumbnails)
        self.e_trans.setCurrentText(e.transition); self.e_tdur.setValue(e.transition_duration)

    def run_export(self):
        if not any(c.enabled for c in self.project.clips):
            QMessageBox.information(self, APP_NAME, "There are no ticked clips to export. "
                                    "Find highlights or add clips in the Clip studio first.")
            return
        self._collect_export_settings()
        if not (self.project.export.individual or self.project.export.compilation):
            QMessageBox.information(self, APP_NAME, "Choose individual clips and/or a compilation.")
            return
        config.save_settings({"export_quality": self.project.export.quality})
        out_dir = self.e_out.text().strip() or str(config.export_dir())
        w = Worker(editor.export_project, self.project, out_dir)
        self.btn_export.setEnabled(False); self.btn_ex_cancel.setEnabled(True)
        self.btn_ex_cancel.clicked.connect(w.cancel)
        w.progress.connect(lambda f, m: (self.ex_progress.setValue(int(f * 1000)), self.ex_status.setText(m)))

        def done(files):
            self.ex_files.clear()
            for f in files:
                it = QListWidgetItem(Path(f).name)
                it.setData(Qt.UserRole, f)
                self.ex_files.addItem(it)
            if files:
                self._last_out = str(Path(files[0]).parent)
            self.ex_status.setText(f"Done - {len(files)} video(s) saved. Double-click one to play it.")
        w.done.connect(done)
        w.failed.connect(lambda m: QMessageBox.warning(self, "Export failed", m))
        w.cancelled.connect(lambda: self.ex_status.setText("Export cancelled"))

        def reset():
            self.btn_export.setEnabled(True); self.btn_ex_cancel.setEnabled(False)
            try:
                self.btn_ex_cancel.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass
        w.finished.connect(reset)
        self._start(w)

    # ------------------------------------------------------------------ tab 4: context images
    def _build_context_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(40, 30, 40, 30)
        badge = QLabel("COMING SOON")
        badge.setObjectName("badge")
        badge.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
        v.addWidget(badge)
        t = QLabel("Context images: pictures that match what's being said")
        t.setObjectName("h1")
        v.addWidget(t)
        d = QLabel(
            "When the streamer mentions a person, place, product, headline or chart, RumbleCut will pop a "
            "relevant image onto the clip at exactly that moment - like an editor adding B-roll.<br><br>"
            "<b>How it will work</b><br>"
            "1. <b>Listen</b> - the clip's speech is transcribed with word-level timing (runs on your GPU).<br>"
            "2. <b>Understand</b> - the key topics and names are pulled out of each sentence.<br>"
            "3. <b>Find</b> - matching pictures are fetched from the sources you allow (your own image folder, "
            "free-licence libraries, or a search provider with your API key).<br>"
            "4. <b>Place</b> - images are timed to the words, spaced out so they never clutter, and "
            "animated in and out.<br>"
            "5. <b>Review</b> - every suggestion appears in the clip's “Images on screen” list, where you can "
            "swap, move, resize or delete it before exporting.<br><br>"
            "The rendering half is already built: you can add images to any clip by hand today in the "
            "Clip studio (“＋ Image at playhead”), and they export with fades and borders.")
        d.setWordWrap(True)
        d.setTextFormat(Qt.RichText)
        v.addWidget(d)
        g = QGroupBox("Settings (available when the feature ships)")
        g.setEnabled(False)
        fl = QFormLayout(g)
        src = QComboBox(); src.addItems(["My image folder", "Wikimedia Commons (free licence)", "Pexels (API key)",
                                         "Web image search (API key)"])
        fl.addRow("Image sources", src)
        dens = QComboBox(); dens.addItems(["Light (1 image per ~15 s)", "Medium (per ~8 s)", "Heavy (per ~5 s)"])
        fl.addRow("How often", dens)
        pos = QComboBox(); pos.addItems(editor.OVERLAY_POSITIONS)
        fl.addRow("Default position", pos)
        fl.addRow("", QPushButton("✨ Add context images to all ticked clips"))
        v.addWidget(g)
        v.addStretch()
        return w

    # ------------------------------------------------------------------ projects / settings
    def save_project(self, as_new=False):
        if not self.project.source:
            return
        self._collect_export_settings()
        path = self.project_path
        if as_new or not path:
            path, _ = QFileDialog.getSaveFileName(
                self, "Save project", str(config.export_dir() / f"{ft.safe_filename(self.project.title)}.rcproj"),
                "RumbleCut project (*.rcproj)")
            if not path:
                return
        self.project.save(path)
        self.project_path = path
        self.statusBar().showMessage(f"Saved {path}", 4000)

    def open_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open project", str(config.export_dir()),
                                              "RumbleCut project (*.rcproj)")
        if not path:
            return
        try:
            proj = editor.Project.load(path)
        except Exception as e:
            QMessageBox.warning(self, "Couldn't open project", str(e))
            return
        if not os.path.exists(proj.source):
            QMessageBox.warning(self, "Video missing", f"The project's video wasn't found:\n{proj.source}")
            return
        self.load_video(proj.source, proj.title, proj.url, proj.offset)
        self.project = proj
        self.project_path = path
        self._apply_export_settings()
        self._fill_clip_list()
        self._make_thumbnails()
        if proj.clips:
            self.clip_list.setCurrentRow(0)

    # ------------------------------------------------------------------ tab 5: settings & updates
    def open_settings(self):
        self.tabs.setCurrentIndex(self.settings_index)

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(40, 24, 40, 24)
        v.setSpacing(12)
        title = QLabel("Settings")
        title.setObjectName("h1")
        v.addWidget(title)
        cols = QHBoxLayout()
        v.addLayout(cols, 1)
        left, right = QVBoxLayout(), QVBoxLayout()
        cols.addLayout(left, 1)
        cols.addSpacing(24)
        cols.addLayout(right, 1)
        s = config.load_settings()

        # ---- updates
        g = QGroupBox("Updates")
        gl = QGridLayout(g)
        gl.setColumnStretch(1, 1)
        gl.addWidget(QLabel("<b>RumbleCut app</b>"), 0, 0)
        self.u_app_ver = QLabel(f"v{__version__}")
        gl.addWidget(self.u_app_ver, 0, 1)
        self.u_app_btn = QPushButton("Download && relaunch")
        self.u_app_btn.setObjectName("primary")
        self.u_app_btn.hide()
        self.u_app_btn.clicked.connect(self.update_app)
        gl.addWidget(self.u_app_btn, 0, 2)
        self.u_app_status = QLabel("Not checked yet")
        self.u_app_status.setObjectName("muted")
        self.u_app_status.setWordWrap(True)
        gl.addWidget(self.u_app_status, 1, 0, 1, 3)

        gl.addWidget(QLabel("<b>Rumble downloader</b>"), 2, 0)
        self.u_dl_ver = QLabel(updater.ytdlp_installed_update_version() or updater.ytdlp_version())
        gl.addWidget(self.u_dl_ver, 2, 1)
        self.u_dl_btn = QPushButton("Update")
        self.u_dl_btn.setObjectName("primary")
        self.u_dl_btn.hide()
        self.u_dl_btn.clicked.connect(self.update_downloader)
        gl.addWidget(self.u_dl_btn, 2, 2)
        self.u_dl_status = QLabel("Keeps Rumble downloads working when Rumble changes its site. "
                                  "Updates without rebuilding the app.")
        self.u_dl_status.setObjectName("muted")
        self.u_dl_status.setWordWrap(True)
        gl.addWidget(self.u_dl_status, 3, 0, 1, 3)

        brow = QHBoxLayout()
        self.u_check_btn = QPushButton("Check for updates")
        self.u_check_btn.clicked.connect(lambda: self.check_updates(silent=False))
        self.u_relaunch_btn = QPushButton("Relaunch RumbleCut")
        self.u_relaunch_btn.clicked.connect(self.relaunch)
        self.u_reset_btn = QPushButton("Undo downloader update")
        self.u_reset_btn.setToolTip("Go back to the downloader that came inside RumbleCut.exe")
        self.u_reset_btn.clicked.connect(self.reset_downloader)
        self.u_reset_btn.setVisible(updater.ytdlp_installed_update_version() is not None)
        brow.addWidget(self.u_check_btn)
        brow.addWidget(self.u_relaunch_btn)
        brow.addStretch()
        brow.addWidget(self.u_reset_btn)
        gl.addLayout(brow, 4, 0, 1, 3)
        self.u_progress = QProgressBar()
        self.u_progress.setRange(0, 1000)
        self.u_progress.setTextVisible(False)
        self.u_progress.setMaximumHeight(12)
        self.u_progress.hide()
        gl.addWidget(self.u_progress, 5, 0, 1, 3)
        self.u_status = QLabel("")
        self.u_status.setWordWrap(True)
        gl.addWidget(self.u_status, 6, 0, 1, 3)
        self.u_auto = QCheckBox("Check for updates when RumbleCut starts")
        self.u_auto.setChecked(bool(s.get("auto_update_check", True)))
        self.u_auto.toggled.connect(lambda b: self._save_setting("auto_update_check", b))
        gl.addWidget(self.u_auto, 7, 0, 1, 3)
        self.u_repo = QLineEdit(s.get("update_repo") or updater.DEFAULT_REPO)
        self.u_repo.setToolTip("GitHub repository whose Releases hold new RumbleCut.exe versions")
        self.u_repo.editingFinished.connect(lambda: self._save_setting("update_repo", self.u_repo.text().strip()))
        gl.addWidget(QLabel("App updates from"), 8, 0)
        rr = QHBoxLayout()
        rr.addWidget(QLabel("github.com/"))
        rr.addWidget(self.u_repo, 1)
        gl.addLayout(rr, 8, 1, 1, 2)
        kind = updater.install_kind()
        note = {"onefile": "Single-file RumbleCut.exe - app updates install themselves.",
                "folder": "Folder build - app updates open the download page (build with "
                          "build_onefile.bat for one-click updates).",
                "source": "Running from source - app updates open the download page."}[kind]
        nl = QLabel(note)
        nl.setObjectName("muted")
        nl.setWordWrap(True)
        gl.addWidget(nl, 9, 0, 1, 3)
        left.addWidget(g)
        left.addStretch()

        # ---- folders & performance
        g2 = QGroupBox("Folders && performance")
        fl = QFormLayout(g2)
        self.s_dl = self._path_row(s.get("download_dir") or str(config.download_dir()), "download_dir")
        self.s_ex = self._path_row(s.get("export_dir") or str(config.export_dir()), "export_dir")
        self.s_ff = self._path_row(s.get("ffmpeg_dir", ""), "ffmpeg_dir", placeholder="(built in)")
        fl.addRow("Downloads go to", self.s_dl[0])
        fl.addRow("Exports go to", self.s_ex[0])
        fl.addRow("FFmpeg folder", self.s_ff[0])
        self.s_gpu = QCheckBox("Use the NVIDIA graphics card to encode (much faster)")
        self.s_gpu.setChecked(bool(s.get("use_gpu", True)))
        self.s_gpu.toggled.connect(lambda b: self._save_setting("use_gpu", b))
        fl.addRow("", self.s_gpu)
        self.s_whisper = QComboBox()
        self.s_whisper.addItems(["tiny", "base", "small", "medium", "large-v3"])
        self.s_whisper.setCurrentText(s.get("whisper_model", "small"))
        self.s_whisper.currentTextChanged.connect(lambda t: self._save_setting("whisper_model", t))
        self.s_whisper.setEnabled(transcribe.available())
        fl.addRow("Speech model", self.s_whisper)
        right.addWidget(g2)

        g3 = QGroupBox("About this copy")
        al = QFormLayout(g3)
        self.a_ffmpeg = QLabel("…")
        self.a_gpu = QLabel("…")
        self.a_python = QLabel(f"Python {sys.version.split()[0]}  ·  Qt {__import__('PySide6').__version__}")
        self.a_data = QLabel(str(config.app_data_dir()))
        self.a_data.setTextInteractionFlags(Qt.TextSelectableByMouse)
        al.addRow("FFmpeg", self.a_ffmpeg)
        al.addRow("GPU encoder", self.a_gpu)
        al.addRow("Runtime", self.a_python)
        al.addRow("Settings stored in", self.a_data)
        right.addWidget(g3)
        right.addStretch()
        self._about_loaded = False
        self._checking_updates = False
        return page

    def _path_row(self, value, key, placeholder=""):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        e = QLineEdit(value)
        e.setPlaceholderText(placeholder)
        b = QPushButton("Browse…")

        def pick():
            d = QFileDialog.getExistingDirectory(self, "Choose folder", e.text())
            if d:
                e.setText(d)
                self._save_setting(key, d)
        b.clicked.connect(pick)
        e.editingFinished.connect(lambda: self._save_setting(key, e.text().strip()))
        h.addWidget(e)
        h.addWidget(b)
        return w, e

    def _save_setting(self, key, value):
        if config.load_settings().get(key) == value:
            return
        config.save_settings({key: value})
        if key in ("ffmpeg_dir", "use_gpu"):
            ft.find_binary.cache_clear()
            ft.nvenc_available.cache_clear()
            self._about_loaded = False
            self._load_about()
        if key == "export_dir":
            self.e_out.setText(str(config.export_dir()))
        if key == "download_dir":
            self._refresh_history()
        self.statusBar().showMessage("Settings saved ✓", 2500)

    def _load_about(self):
        if self._about_loaded:
            return
        self._about_loaded = True

        def job(on_progress=None, cancel=None):
            try:
                out = subprocess.run([ft.ffmpeg(), "-hide_banner", "-version"], capture_output=True, text=True,
                                     creationflags=ft.NO_WINDOW).stdout.splitlines()[0]
                ver = out.replace("ffmpeg version ", "").split(" Copyright")[0]
            except Exception as e:
                return str(e), "-"
            return ver, ("NVIDIA NVENC ✔" if ft.nvenc_available() else "none found - using the CPU")
        w = Worker(job)
        w.background = True
        w.done.connect(lambda r: (self.a_ffmpeg.setText(r[0]), self.a_gpu.setText(r[1])))
        self._start(w)

    # ---- update actions
    def _updates_busy(self, busy: bool, msg: str = ""):
        for b in (self.u_check_btn, self.u_app_btn, self.u_dl_btn, self.u_reset_btn):
            b.setEnabled(not busy)
        self.u_progress.setVisible(busy)
        if busy:
            self.u_progress.setRange(0, 0)
        if msg:
            self.u_status.setText(msg)

    def _update_progress(self, f, msg):
        if f < 0:
            self.u_progress.setRange(0, 0)
        else:
            self.u_progress.setRange(0, 1000)
            self.u_progress.setValue(int(f * 1000))
        self.u_status.setText(msg)

    def check_updates(self, silent=False):
        if self._checking_updates:
            return
        self._checking_updates = True
        if not silent:
            self._updates_busy(True, "Checking for updates…")
        repo = self.u_repo.text().strip()

        def job(on_progress=None, cancel=None):
            out = {}
            try:
                out["dl"] = updater.check_ytdlp()
            except Exception as e:
                out["dl_err"] = str(e)
            try:
                out["app"] = updater.check_app(repo)
            except Exception as e:
                out["app_err"] = str(e)
            return out
        w = Worker(job)
        w.background = True
        w.done.connect(lambda r: self._show_update_results(r, silent))

        def end():
            self._checking_updates = False
            if not silent:
                self._updates_busy(False)
        w.finished.connect(end)
        self._start(w)

    def _show_update_results(self, r: dict, silent: bool):
        self._update_info = r
        found = []
        if "app" in r:
            a = r["app"]
            if a["available"]:
                found.append(f"RumbleCut v{a['latest']}")
                self.u_app_status.setText(f"<span style='color:{ACCENT}'>Version {a['latest']} is available.</span>"
                                          + (f"<br>{a['notes'][:300]}" if a.get("notes") else ""))
                self.u_app_btn.setText("Download && relaunch" if updater.install_kind() == "onefile"
                                       else "Open download page")
                self.u_app_btn.show()
            else:
                self.u_app_status.setText(f"You have the latest version (v{a['current']}).")
                self.u_app_btn.hide()
        else:
            self.u_app_status.setText(f"Couldn't check: {r.get('app_err', 'unknown error')}")
            self.u_app_btn.hide()
        if "dl" in r:
            d = r["dl"]
            if d["available"]:
                found.append(f"downloader {d['latest']}")
                self.u_dl_status.setText(f"<span style='color:{ACCENT}'>Version {d['latest']} is available "
                                         f"(you have {d['current']}).</span>")
                self.u_dl_btn.show()
            else:
                self.u_dl_status.setText(f"Up to date ({d['current']}).")
                self.u_dl_btn.hide()
        else:
            self.u_dl_status.setText(f"Couldn't check: {r.get('dl_err', 'unknown error')}")
            self.u_dl_btn.hide()
        self.tabs.setTabText(self.settings_index, "⚙  Settings  ●" if found else "⚙  Settings")
        if found:
            self.u_status.setText("Update available: " + ", ".join(found))
            if silent:
                self.statusBar().showMessage("Update available: " + ", ".join(found) + " - see Settings", 15000)
        elif not silent:
            self.u_status.setText("Everything is up to date ✓")

    def update_downloader(self):
        d = (getattr(self, "_update_info", {}) or {}).get("dl")
        if not d:
            return
        self._updates_busy(True, "Downloading downloader update…")
        w = Worker(updater.install_ytdlp, d["url"], d["latest"])
        w.progress.connect(self._update_progress)

        def done(ver):
            self.u_dl_ver.setText(f"{ver}  (relaunch to start using it)")
            self.u_dl_status.setText(f"<span style='color:{ACCENT}'>Installed {ver}.</span> "
                                     "Relaunch RumbleCut to start using it.")
            self.u_dl_btn.hide()
            self.u_reset_btn.show()
            self._mark_restart_needed()
        w.done.connect(done)
        w.failed.connect(lambda m: QMessageBox.warning(self, "Update failed", m))
        w.finished.connect(lambda: self._updates_busy(False))
        self._start(w)

    def reset_downloader(self):
        if QMessageBox.question(self, "Undo downloader update",
                                "Go back to the downloader that came with RumbleCut.exe?") != QMessageBox.Yes:
            return
        updater.reset_ytdlp()
        self.u_reset_btn.hide()
        self.u_dl_ver.setText("built-in version (relaunch to switch back)")
        self._mark_restart_needed()

    def update_app(self):
        a = (getattr(self, "_update_info", {}) or {}).get("app")
        if not a:
            return
        if updater.install_kind() != "onefile" or not a.get("url"):
            QDesktopServices.openUrl(QUrl(a["page"]))
            return
        if [w for w in self.workers if w.isRunning()] and QMessageBox.question(
                self, "Update RumbleCut", "A job is still running. It will be stopped when RumbleCut "
                "relaunches. Continue?") != QMessageBox.Yes:
            return
        self._updates_busy(True, "Downloading RumbleCut update…")
        w = Worker(updater.download_app, a["url"])
        w.progress.connect(self._update_progress)

        def done(new_path):
            try:
                updater.swap_in_new_exe(new_path)
            except Exception as e:
                QMessageBox.warning(self, "Update failed", f"Couldn't replace RumbleCut.exe:\n{e}")
                return
            self.u_status.setText("Update installed - relaunching…")
            QTimer.singleShot(400, lambda: self.relaunch(ask=False))
        w.done.connect(done)
        w.failed.connect(lambda m: QMessageBox.warning(self, "Update failed", m))
        w.finished.connect(lambda: self._updates_busy(False))
        self._start(w)

    def _mark_restart_needed(self):
        self.u_relaunch_btn.setObjectName("primary")
        self.u_relaunch_btn.setText("Relaunch now to finish updating")
        self.u_relaunch_btn.style().unpolish(self.u_relaunch_btn)
        self.u_relaunch_btn.style().polish(self.u_relaunch_btn)

    def relaunch(self, ask: bool = True):
        busy = [w for w in self.workers if w.isRunning() and not getattr(w, "background", False)]
        if busy and ask and QMessageBox.question(
                self, "Relaunch", "A job is still running. Stop it and relaunch?") != QMessageBox.Yes:
            return
        if self.project.clips and ask and QMessageBox.question(
                self, "Relaunch", "Relaunching closes the current project. Save it first?",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.save_project()
        for w in busy:
            w.cancel()
        for w in busy:
            w.wait(3000)
        try:
            updater.spawn_new_instance()
        except Exception as e:
            QMessageBox.warning(self, "Couldn't relaunch", f"{e}\n\nPlease close and reopen RumbleCut.")
            return
        QApplication.quit()

    # ------------------------------------------------------------------ workers / lifecycle
    def _start(self, w: Worker):
        self.workers.append(w)
        # drop our reference only after Qt has fully finished with the thread
        w.finished.connect(lambda: QTimer.singleShot(0, lambda: self.workers.remove(w)
                                                     if w in self.workers else None))
        w.start()

    def closeEvent(self, ev):
        busy = [w for w in self.workers if w.isRunning() and not getattr(w, "background", False)]
        if busy and QMessageBox.question(self, "Quit?", "A job is still running. Stop it and quit?") \
                != QMessageBox.Yes:
            ev.ignore()
            return
        for w in self.workers:
            w.cancel()
        for w in list(self.workers):
            w.wait(3000)
        ev.accept()


# ============================================================================ theme
STYLE = f"""
* {{ font-size: 10pt; }}
QMainWindow, QWidget {{ background: #1b1d22; color: #e5e7eb; }}
QLabel#h1 {{ font-size: 17pt; font-weight: 600; }}
QLabel#h2 {{ font-size: 11pt; font-weight: 600; }}
QLabel#section {{ font-weight: 600; color: #cbd5e1; padding: 4px 0; }}
QLabel#muted {{ color: #8b95a3; }}
QLabel#badge {{ background: {ACCENT}; color: #0b0d10; font-weight: 700; padding: 3px 10px; border-radius: 9px; }}
QTabBar::tab {{ padding: 10px 22px; background: #15171b; color: #9ca3af; border: none; }}
QTabBar::tab:selected {{ color: #fff; border-bottom: 3px solid {ACCENT}; background: #1b1d22; }}
QGroupBox {{ border: 1px solid #2c3038; border-radius: 8px; margin-top: 14px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #cbd5e1; font-weight: 600; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{ background: #121418; border: 1px solid #2f343d;
    border-radius: 6px; padding: 5px 7px; selection-background-color: {ACCENT}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QPushButton, QToolButton {{ background: #2a2e36; border: 1px solid #353a44; border-radius: 6px; padding: 6px 12px; }}
QPushButton:hover, QToolButton:hover {{ background: #333844; }}
QPushButton:disabled {{ color: #5b6370; background: #20232a; }}
QPushButton#primary {{ background: {ACCENT}; color: #0b0d10; font-weight: 700; border: none; padding: 7px 18px; }}
QPushButton#primary:hover {{ background: #9bd85c; }}
QPushButton#primary:disabled {{ background: #3d5a26; color: #1b1d22; }}
QListWidget {{ background: #121418; border: 1px solid #2c3038; border-radius: 8px; }}
QListWidget::item {{ padding: 6px; border-radius: 6px; }}
QListWidget::item:selected {{ background: #2d3a22; color: #fff; }}
QProgressBar {{ background: #121418; border: 1px solid #2c3038; border-radius: 6px; text-align: center; height: 16px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
QSlider::groove:horizontal {{ height: 4px; background: #333844; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {ACCENT}; width: 14px; margin: -6px 0; border-radius: 7px; }}
QScrollArea {{ border: none; }}
QCheckBox::indicator, QListWidget::indicator {{ width: 15px; height: 15px; border: 1px solid #4b5260;
    border-radius: 4px; background: #121418; }}
QCheckBox::indicator:checked, QListWidget::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 16px; background: #2a2e36; border: none; }}
QSplitter::handle {{ background: #2a2e36; width: 2px; }}
QStatusBar {{ color: #8b95a3; }}
QMenuBar {{ background: #15171b; }} QMenuBar::item:selected {{ background: #2a2e36; }}
QMenu {{ background: #1f2228; border: 1px solid #2c3038; }} QMenu::item:selected {{ background: #2d3a22; }}
"""


def run_app():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RumbleCut.App")
        except Exception:
            pass
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        win.load_video(sys.argv[1])
    code = app.exec()
    for w in list(win.workers):
        w.cancel()
        w.wait(2000)
    if any(w.isRunning() for w in win.workers):
        os._exit(code)          # a slow network check is still running - exit without waiting on it
    return code
