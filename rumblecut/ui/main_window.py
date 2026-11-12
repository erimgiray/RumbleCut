"""Main window: Get Video → Clip Studio → Export, plus the Context Images tab."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, fields
import subprocess
import sys
import tempfile
from pathlib import Path

import re

import numpy as np

from PySide6.QtCore import QEasingCurve, QPointF, QPropertyAnimation, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QBrush, QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter, QPainterPath, QPalette,
                           QPen, QPixmap, QValidator)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QRadioButton, QScrollArea, QScrollBar, QSizePolicy, QSlider, QSpinBox, QSplitter, QStatusBar,
    QStackedWidget, QStyle, QStyledItemDelegate, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from .. import APP_NAME, __version__, analysis, config, context_images, downloader, editor
from .. import ffmpeg_tools as ft
from .. import ai, diction, gpu_pack, transcribe, updater
from .timeline import ClipTimeline, Scrubber, Timeline, fmt_time
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


class ElidedLabel(QLabel):
    """A label that shortens long text with '…' instead of forcing the window wider."""
    def __init__(self, text=""):
        super().__init__()
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(40)
        self.setText(text)

    def setText(self, text):
        self._full = text or ""
        self.setToolTip(self._full if len(self._full) > 60 else "")
        self._elide()

    def text(self):
        return self._full

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._elide()

    def _elide(self):
        fm = self.fontMetrics()
        QLabel.setText(self, fm.elidedText(self._full, Qt.ElideRight, max(20, self.width() - 4)))


def _to_recycle_bin(path: str):
    """Move a file to the Recycle Bin on Windows (so a mistake can be undone); delete elsewhere."""
    if not os.path.exists(path):
        return
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", ctypes.c_uint), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
        FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
        op = SHFILEOPSTRUCTW(None, FO_DELETE, os.path.abspath(path) + "\0", None,
                             FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if res != 0 or os.path.exists(path):
            raise OSError(f"Windows couldn't move it to the Recycle Bin (code {res}) - is it open in another program?")
    else:
        os.remove(path)


def asset(name: str) -> str:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return str(base / "assets" / name)


def open_folder(path: str | Path):
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class TimeSpinBox(QDoubleSpinBox):
    """Shows and accepts times as h:mm:ss.s in *stream* time; the value itself is project time."""
    def __init__(self):
        super().__init__()
        self.to_disp = lambda t: t
        self.from_disp = lambda t: t
        self.setDecimals(1)
        self.setSingleStep(0.5)
        self.setRange(0, 1e7)
        self.setKeyboardTracking(False)        # apply when you press Enter / leave the box
        self.setToolTip("Time in the stream (h:mm:ss.s). Type a time, use the arrows, or drag the "
                        "handles on the moment's timeline.")

    def textFromValue(self, v):
        t = max(0.0, float(self.to_disp(v)))
        h, rem = divmod(t, 3600)
        m, sec = divmod(rem, 60)
        sec = round(sec, 1)
        if sec >= 60:
            sec, m = 0.0, m + 1
        return f"{int(h)}:{int(m):02d}:{sec:04.1f}" if h >= 1 else f"{int(m)}:{sec:04.1f}"

    def valueFromText(self, text):
        secs = 0.0
        try:
            for part in text.strip().split(":"):
                secs = secs * 60 + float(part or 0)
        except ValueError:
            return self.value()
        return float(self.from_disp(secs))

    def validate(self, text, pos):
        import re
        ok = re.fullmatch(r"[\d:.\s]*", text) is not None
        return (QValidator.Acceptable if ok else QValidator.Invalid), text, pos


def _num_icon(n: int, px: int = 26) -> QIcon:
    """The circled step number on the Get video / Clip studio / Export tabs, drawn crisp at any DPI."""
    icon = QIcon()
    for state, col in ((QIcon.Off, QColor("#9ca3af")), (QIcon.On, QColor("#ffffff"))):
        for scale in (1, 2):
            pm = QPixmap(px * scale, px * scale)
            pm.setDevicePixelRatio(scale)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(col, 1.6))
            p.drawEllipse(QRectF(1.5, 1.5, px - 3, px - 3))
            f = QFont()
            f.setPixelSize(int(px * 0.56))
            f.setBold(True)
            p.setFont(f)
            p.drawText(QRectF(0, 0, px, px), Qt.AlignCenter, str(n))
            p.end()
            icon.addPixmap(pm, QIcon.Normal, state)
    return icon


def _glyph_icon(glyph: str, px: int = 26) -> QIcon:
    """An emoji/symbol drawn as a 26 px tab icon, so every tab has the same icon + text layout
    (an emoji inside the tab text makes the tab bar a few pixels shorter, and the page jumps)."""
    icon = QIcon()
    for state, col in ((QIcon.Off, QColor("#9ca3af")), (QIcon.On, QColor("#ffffff"))):
        for scale in (1, 2):
            pm = QPixmap(px * scale, px * scale)
            pm.setDevicePixelRatio(scale)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.TextAntialiasing)
            f = QFont()
            f.setPixelSize(int(px * 0.62))
            p.setFont(f)
            p.setPen(col)
            p.drawText(QRectF(0, 0, px, px), Qt.AlignCenter, glyph)
            p.end()
            icon.addPixmap(pm, QIcon.Normal, state)
    return icon


def _menu_icon(w: int = 18, h: int = 14) -> QIcon:
    """Three equally spaced lines (the ☰ glyph renders unevenly in most fonts)."""
    icon = QIcon()
    for col, mode in ((QColor("#d1d5db"), QIcon.Normal), (QColor("#ffffff"), QIcon.Active)):
        for scale in (1, 2):
            pm = QPixmap(w * scale, h * scale)
            pm.setDevicePixelRatio(scale)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            pen = QPen(col, 2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            for y in (1.5, h / 2, h - 1.5):          # top, middle, bottom: equal gaps
                p.drawLine(QPointF(1.5, y), QPointF(w - 1.5, y))
            p.end()
            icon.addPixmap(pm, mode)
    return icon


_UNITS = {"h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
          "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
          "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "": 1}


def fmt_secs(v: int) -> str:
    v = int(v)
    return f"{v // 60} min" if v >= 60 and v % 60 == 0 else f"{v} seconds"


def parse_secs(text: str):
    t = text.strip().lower()
    m = re.fullmatch(r"(\d+):(\d{1,2})", t)                 # 1:30
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    total, pos = 0.0, 0
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([a-z]*)", t):   # 1 min 30 s, 90, 2m
        if m.group(2) not in _UNITS or t[pos:m.start()].strip():
            return None
        total += float(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    return int(round(total)) if pos and not t[pos:].strip() else None


class RangesEditor(QWidget):
    """Parts of the video to download: one row per part, each with “Starts from” and “Ends at”.
    “To the end” makes a part run to the end of the video. Reads/writes the same text as before
    ("1:05:00-1:20:00, 2:10:30-"), so saved settings and the downloader are unchanged."""
    START_HINT = "0:00"      # an empty start is the very beginning
    EMPTY = "—"              # an empty end shows a dash until you type a time
    textChanged = Signal(str)
    returnPressed = Signal()
    editingFinished = Signal()

    def __init__(self):
        super().__init__()
        self._rows: list[dict] = []
        self._duration = 0.0
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(0, 0, 0, 0)
        self._v.setSpacing(4)
        self._rows_box = QVBoxLayout()
        self._rows_box.setSpacing(4)
        self._v.addLayout(self._rows_box)
        self._add_row()

    # -- rows
    def _add_row(self, start: str = "", end: str = "", to_end: bool = False):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        st = QLineEdit(start); st.setPlaceholderText(self.START_HINT); st.setMaximumWidth(130)
        st.setToolTip("Where this part starts (h:mm:ss). Empty = the very beginning (0:00).")
        en = QLineEdit(end); en.setPlaceholderText(self.EMPTY); en.setMaximumWidth(170)
        en.setToolTip("Where this part ends (h:mm:ss)")
        te = QPushButton("⏭ To the end")
        te.setCheckable(True)
        te.setToolTip("Download from “Starts from” right to the end of the video")
        add = QPushButton("＋ Add part")
        add.setToolTip("Download another part of the video as well")
        add.clicked.connect(lambda: (self._add_row(), self._rows[-1]["start"].setFocus()))
        rm = QToolButton(); rm.setText("✖"); rm.setToolTip("Remove this part")
        h.addWidget(QLabel("Starts from")); h.addWidget(st)
        h.addSpacing(10)
        h.addWidget(QLabel("Ends at")); h.addWidget(en); h.addWidget(te); h.addWidget(add); h.addWidget(rm)
        h.addStretch()
        row = {"w": w, "start": st, "end": en, "to_end": te, "add": add, "rm": rm}
        self._rows.append(row)
        self._rows_box.addWidget(w)
        for le in (st, en):
            le.textChanged.connect(lambda _: self.textChanged.emit(self.text()))
            le.returnPressed.connect(self.returnPressed)
            le.editingFinished.connect(self.editingFinished)
        te.toggled.connect(lambda on, r=row: self._set_to_end(r, on))
        rm.clicked.connect(lambda _=False, r=row: self._remove_row(r))
        te.setChecked(to_end)
        self._update_rm()
        return row

    def _set_to_end(self, row, on: bool):
        en = row["end"]
        if on:
            row["saved_end"] = en.text()
            en.blockSignals(True); en.clear(); en.blockSignals(False)
            en.setPlaceholderText("end of video" + (f" ({fmt_time(self._duration)})" if self._duration else ""))
        else:
            en.setPlaceholderText(self.EMPTY)
            en.setText(row.pop("saved_end", ""))
        en.setEnabled(not on and self.isEnabled())
        self.textChanged.emit(self.text())
        self.editingFinished.emit()

    def _remove_row(self, row):
        if len(self._rows) <= 1:
            for k in ("start", "end"):
                row[k].clear()
            row["to_end"].setChecked(False)
            return
        self._drop(row)
        self._update_rm()
        self.textChanged.emit(self.text())
        self.editingFinished.emit()

    def _drop(self, row):
        self._rows.remove(row)
        self._rows_box.removeWidget(row["w"])
        row["w"].hide()
        row["w"].setParent(None)
        row["w"].deleteLater()

    def _update_rm(self):
        for r in self._rows:
            r["rm"].setVisible(len(self._rows) > 1)
            r["add"].setVisible(r is self._rows[-1])     # “Add part” sits on the last row only

    # -- QLineEdit-like API used by the rest of the window
    def text(self) -> str:
        parts = []
        for r in self._rows:
            a = r["start"].text().strip()
            b = "" if r["to_end"].isChecked() else r["end"].text().strip()
            if not a and not b and not r["to_end"].isChecked():
                continue
            parts.append(f"{a or '0'}-{b}")
        return ", ".join(parts)

    def setText(self, text: str):
        for r in list(self._rows):
            self._drop(r)
        pieces = [p.strip() for p in (text or "").split(",") if p.strip()]
        for p in pieces or [""]:
            a, sep, b = p.partition("-")
            self._add_row(a.strip(), b.strip(), to_end=bool(sep) and not b.strip())
        self.textChanged.emit(self.text())

    def row_height(self) -> int:
        r = self._rows[0] if self._rows else None
        return r["w"].sizeHint().height() if r else 30

    def set_duration(self, d: float):
        self._duration = float(d or 0)
        for r in self._rows:
            if r["to_end"].isChecked():
                r["end"].setPlaceholderText("end of video" + (f" ({fmt_time(self._duration)})" if self._duration else ""))

    def setFocus(self):
        if self._rows:
            self._rows[0]["start"].setFocus()

    def setEnabled(self, on: bool):
        super().setEnabled(on)
        for r in self._rows:
            r["end"].setEnabled(on and not r["to_end"].isChecked())

    def setPlaceholderText(self, _):
        pass


class StarDelegate(QStyledItemDelegate):
    """Paints the item as usual, then five equal stars in its top-right corner, filled to the
    rating (0-10: each star is 2 points, so 5 = two full stars and a half)."""
    ROLE = Qt.UserRole + 1
    SIZE = 13

    @staticmethod
    def _star(cx, cy, r):
        import math
        path = QPainterPath()
        for k in range(10):
            ang = -math.pi / 2 + k * math.pi / 5
            rr = r if k % 2 == 0 else r * 0.45
            pt = QPointF(cx + rr * math.cos(ang), cy + rr * math.sin(ang))
            path.moveTo(pt) if k == 0 else path.lineTo(pt)
        path.closeSubpath()
        return path

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        rating = index.data(self.ROLE)
        if rating is None:
            return
        n = max(0, min(10, int(rating)))
        size, gap = self.SIZE, 3
        total = 5 * size + 4 * gap
        x0 = option.rect.right() - total - 8
        cy = option.rect.top() + 6 + size / 2
        dim = n < 4
        fill = QColor("#6b7280") if dim else QColor("#f5b942")
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        for k in range(5):
            cx = x0 + k * (size + gap) + size / 2
            star = self._star(cx, cy, size / 2)
            painter.setPen(QPen(fill, 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(star)
            amount = max(0, min(2, n - 2 * k))            # 0 empty, 1 half, 2 full
            if amount:
                painter.save()
                w = size if amount == 2 else size / 2
                painter.setClipRect(QRectF(cx - size / 2, cy - size / 2, w, size))
                painter.fillPath(star, fill)
                painter.restore()
        painter.restore()


class PresetBox(QComboBox):
    """A dropdown of common values that you can also type into (e.g. "90 s", "2 min", "1:30").
    Behaves like a QSpinBox for the rest of the code: value(), setValue(), valueChanged."""
    valueChanged = Signal(int)

    def __init__(self, presets, fmt=str, parse=None, lo=0, hi=10 ** 6):
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self._fmt, self._parse = fmt, parse or (lambda t: int(t) if t.strip().isdigit() else None)
        self._lo, self._hi = lo, hi
        self._value = int(presets[0])
        for v in presets:
            self.addItem(fmt(v), int(v))
        self.setEditText(fmt(self._value))
        self.activated.connect(lambda i: self.setValue(self.itemData(i)))
        self.lineEdit().editingFinished.connect(self._typed)

    def value(self) -> int:
        return self._value

    def setValue(self, v):
        v = int(max(self._lo, min(self._hi, int(v))))
        changed = v != self._value
        self._value = v
        self.blockSignals(True)
        i = self.findData(v)
        if i >= 0:
            self.setCurrentIndex(i)
        self.setEditText(self._fmt(v))
        self.blockSignals(False)
        if changed:
            self.valueChanged.emit(v)

    def _typed(self):
        v = self._parse(self.currentText())
        self.setValue(self._value if v is None else v)


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
        self._media_offset: float | None = 0.0     # where the loaded media sits on the project timeline
        self.candidates: list = []                  # ranked moments not yet shown
        self._has_video_cache: dict[str, bool] = {}

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._build_get_tab(), _num_icon(1), "Get video")
        self.tabs.addTab(self._build_studio_tab(), _num_icon(2), "Clip studio")
        # ③ Export & publish: save the clips as files, or post them straight online - one page, two tabs
        from .publish_tab import PublishTab
        self.out_tabs = QTabWidget()
        self.out_tabs.setObjectName("lefttabs")
        self.out_tabs.setDocumentMode(True)
        self.out_tabs.addTab(self._build_export_tab(), "💾  Save as files")
        self.pub_tab = PublishTab(self)
        self.out_tabs.addTab(self.pub_tab, "🚀  Post online")
        self.out_tabs.currentChanged.connect(lambda i: self.pub_tab.refresh() if i == 1 else self._fill_export_rows())
        _ow = QWidget()
        _ol = QVBoxLayout(_ow)
        _ol.setContentsMargins(16, 8, 16, 0)
        _ol.addWidget(self.out_tabs)
        self.export_index = self.tabs.addTab(_ow, _num_icon(3), "Export && publish")
        self.pub_index = self.export_index
        self.tabs.setIconSize(QSize(26, 26))
        self.ctx_index = 1                        # Script and Pictures now live in the Clip studio
        _sc = QScrollArea()
        self._settings_scroll = _sc
        _sc.setWidgetResizable(True)
        _sc.setWidget(self._build_settings_tab())
        self.settings_index = self.tabs.addTab(_sc, _glyph_icon("⚙"), "Settings")
        self.tabs.currentChanged.connect(lambda i: i == self.settings_index and self._load_about())
        self.tabs.currentChanged.connect(lambda i: i == 1 and self._side_tab_changed(self.side_tabs.currentIndex()))
        # leaving the Clip studio stops what it was playing
        self.tabs.currentChanged.connect(lambda i: i != 1 and self.pause_studio())
        self.tabs.currentChanged.connect(lambda i: i == self.export_index and (
            self.pub_tab.refresh() if self.out_tabs.currentIndex() == 1 else self._fill_export_rows()))
        self._build_menu()
        self._update_no_video("")
        from PySide6.QtWidgets import QAbstractSpinBox
        for sb in self.findChildren(QAbstractSpinBox):     # a little breathing room before the numbers
            le = sb.findChild(QLineEdit)
            if le is not None:
                le.setTextMargins(6, 0, 0, 0)
        sb = QStatusBar()                  # kept for messages, but never shown as a strip
        self.setStatusBar(sb)
        sb.hide()
        self._toast = QLabel(self)
        self._toast.setObjectName("toast")
        self._toast.hide()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._toast.hide)
        sb.messageChanged.connect(self._show_toast)
        self._check_ffmpeg()
        self._refresh_history()
        if config.load_settings().get("auto_update_check", True):
            QTimer.singleShot(3000, lambda: self.check_updates(silent=True))
        # undo / redo: the moments are checked a few times a second and each settled change is one step
        self._undo_stack: list = []
        self._redo_stack: list = []
        self._hist = None                  # (key, refs, row) of the state on screen
        self._hist_poll = None
        self._hist_project = None
        self._hist_timer = QTimer(self)
        self._hist_timer.timeout.connect(self._history_tick)
        self._hist_timer.start(300)
        # your work is kept between runs (and across updates): saved every half minute and on quit
        self._session_sig = ""
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_timer.start(30_000)
        QTimer.singleShot(0, self._restore_session)

    @staticmethod
    def _cancel_shown(btn, on: bool):
        """Cancel buttons only appear while there's something to cancel."""
        btn.setEnabled(on)
        btn.setVisible(on)
        if on:
            btn.setText("Cancel")

    def _cancelling(self, w, btn, status_lbl):
        w.cancel()
        btn.setEnabled(False)
        btn.setText("Stopping…")
        status_lbl.setText("Stopping - finishing the step in progress…")

    def _show_toast(self, msg: str):
        if not msg:
            return
        self._toast.setText(msg)
        self._toast.adjustSize()
        w = min(self._toast.width(), self.width() - 40)
        self._toast.resize(w, self._toast.height())
        self._toast.move(self.width() - w - 12, 2)           # top-right, in the empty end of the tab bar
        self._toast.raise_()
        self._toast.show()
        self._toast_timer.start(4000)

    # ------------------------------------------------------------------ menu
    def _build_menu(self):
        """☰ button in the tab bar's corner; File and Help live in its menu (room for more later)."""
        self.menuBar().hide()
        root = QMenu(self)
        root.setObjectName("hamburger")
        m = root.addMenu("File")
        for text, key, fn in [
            ("Open video…", QKeySequence.Open, self.open_video_dialog),
            ("Open project…", "Ctrl+Shift+O", self.open_project),
            ("Save project", QKeySequence.Save, self.save_project),
            ("Save project as…", "Ctrl+Shift+S", lambda: self.save_project(as_new=True)),
        ]:
            a = QAction(text, self)
            a.setShortcut(key)
            a.setShortcutContext(Qt.ApplicationShortcut)
            a.triggered.connect(fn)
            self.addAction(a)                  # shortcuts work even while the menu is closed
            m.addAction(a)
        m.addSeparator()
        em = root.addMenu("Edit")
        self.act_undo = QAction("Undo", self)
        self.act_undo.setShortcut(QKeySequence("Ctrl+Z"))
        self.act_undo.triggered.connect(self.undo)
        self.act_redo = QAction("Redo", self)
        self.act_redo.setShortcuts([QKeySequence("Ctrl+Shift+Z"), QKeySequence("Ctrl+Y")])
        self.act_redo.triggered.connect(self.redo)
        for a in (self.act_undo, self.act_redo):
            self.addAction(a)                  # work anywhere in the window (text boxes keep their own undo)
            em.addAction(a)
        a = QAction("Settings…", self)
        a.triggered.connect(self.open_settings)
        m.addAction(a)
        a = QAction("Quit", self)
        a.setShortcut("Ctrl+Q")
        a.triggered.connect(self.close)
        self.addAction(a)
        m.addAction(a)
        h = root.addMenu("Help")
        a = QAction("About", self)
        a.triggered.connect(lambda: QMessageBox.about(
            self, APP_NAME, f"<b>{APP_NAME} {__version__}</b><br>Download Rumble streams, find the best "
            "moments automatically, and export them as polished clips.<br><br>Only download and re-publish "
            "videos you own or have permission to use."))
        h.addAction(a)
        self.menu_root = root
        btn = QToolButton()
        btn.setObjectName("hamburgerBtn")
        btn.setIcon(_menu_icon())
        btn.setIconSize(QSize(18, 14))
        btn.setFixedSize(40, 32)
        btn.setToolTip("Menu")
        btn.setPopupMode(QToolButton.InstantPopup)
        btn.setMenu(root)
        btn.setCursor(Qt.PointingHandCursor)
        self.menu_btn = btn
        # sits on the tab bar's left end (the tabs are pushed right by `QTabWidget::tab-bar { left }`),
        # centred on the tabs' height - a corner widget gets placed too low by Qt
        btn.setParent(self.tabs)
        btn.raise_()
        self._place_menu_btn()
        btn.show()

    def _place_menu_btn(self):
        h = self.tabs.tabBar().height() or self.tabs.tabBar().sizeHint().height()
        self.menu_btn.move(6, max(0, (h - self.menu_btn.height()) // 2))

    def showEvent(self, ev):
        super().showEvent(ev)
        if hasattr(self, "menu_btn"):
            QTimer.singleShot(0, self._place_menu_btn)

    def _check_ffmpeg(self):
        try:
            ft.ffmpeg(), ft.ffprobe()
        except ft.FFmpegError as e:
            QTimer.singleShot(300, lambda: QMessageBox.warning(self, "FFmpeg not found", str(e)))

    # ------------------------------------------------------------------ tab 1: get video
    def _build_get_tab(self) -> QWidget:
        self._link_duration = 0.0
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
        self.btn_scan = QPushButton("⚡ Scan audio first")
        self.btn_scan.setToolTip("Download only the sound (kept exactly as in the stream), find the best moments, then fetch video just "
                                 "for the moments you pick - much faster for long streams")
        self.btn_scan.clicked.connect(lambda: self.start_download(audio_only=True))
        row.addWidget(self.btn_scan)
        v.addLayout(row)
        self.link_info = QLabel("")
        self.link_info.setWordWrap(True)
        self.link_info.hide()                                  # shown once “Check link” has something to say
        v.addWidget(self.link_info)

        part = QGroupBox()
        pg = QGridLayout(part)
        self.part_only = QCheckBox("Only download part of the video")
        self.part_only.setToolTip("Skip the parts you don't need - much faster for long streams")
        self.ranges_edit = RangesEditor()
        self.ranges_edit.setEnabled(False)
        self.ranges_edit.returnPressed.connect(self.start_download)
        self.ranges_info = QLabel("")             # only shown to confirm the parts or flag a typo
        self.ranges_info.setObjectName("muted")
        self.ranges_info.hide()
        self.ranges_info.setWordWrap(True)
        self.part_only.toggled.connect(self.ranges_edit.setEnabled)
        self.part_only.toggled.connect(lambda on: on and self.ranges_edit.setFocus())
        self.part_only.toggled.connect(lambda _: self._update_ranges_info())
        self.ranges_edit.textChanged.connect(lambda _: self._update_ranges_info())
        # remember the box and the times across restarts (updates relaunch the app)
        st = config.load_settings()
        self.ranges_edit.setText(st.get("part_ranges", ""))
        self.part_only.setChecked(bool(st.get("part_only", False)))
        self.part_only.toggled.connect(lambda on: config.save_settings({"part_only": on}))
        self.ranges_edit.editingFinished.connect(
            lambda: config.save_settings({"part_ranges": self.ranges_edit.text().strip()}))
        # as tall as one Starts from / Ends at row, so the tick box lines up with the first row
        self.part_only.setFixedHeight(max(self.ranges_edit.row_height(), self.part_only.sizeHint().height()))
        pg.addWidget(self.part_only, 0, 0, Qt.AlignTop)
        pg.addWidget(self.ranges_edit, 0, 1)
        pg.addWidget(self.ranges_info, 1, 1)
        pg.setColumnStretch(1, 1)
        v.addWidget(part)
        prow = QHBoxLayout()
        self.dl_progress = QProgressBar()
        self.dl_progress.setTextVisible(True)
        self.dl_progress.setFormat("")
        self.btn_dl_cancel = QPushButton("Cancel")
        self.btn_dl_cancel.setObjectName("danger")
        _keep = self.btn_dl_cancel.sizePolicy(); _keep.setRetainSizeWhenHidden(True); self.btn_dl_cancel.setSizePolicy(_keep)
        self._cancel_shown(self.btn_dl_cancel, False)
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
        lists = QHBoxLayout()
        self.hist_audio = QListWidget()
        self.hist_video = QListWidget()
        self.hist_audio_lbl = section("🔊 Audio scans")
        self.hist_video_lbl = section("🎬 Videos")
        for lst, lbl in ((self.hist_audio, self.hist_audio_lbl), (self.hist_video, self.hist_video_lbl)):
            lst.setSelectionMode(QAbstractItemView.ExtendedSelection)
            lst.itemDoubleClicked.connect(self._open_history_item)
            lst.itemSelectionChanged.connect(self._update_delete_button)
            col = QVBoxLayout()
            col.addWidget(lbl)
            col.addWidget(lst, 1)
            lists.addLayout(col, 1)
        v.addLayout(lists, 1)
        brow2 = QHBoxLayout()
        hint = QLabel("Double-click to start clipping · Ctrl/Shift-click to select several")
        hint.setObjectName("muted")
        brow2.addWidget(hint, 1)
        self.btn_del = QPushButton("🗑 Delete selected")
        self.btn_del.setObjectName("danger")
        self.btn_del.setEnabled(False)
        self.btn_del.setToolTip("Move the selected downloads to the Recycle Bin (Delete key)")
        self.btn_del.clicked.connect(self.delete_downloads)
        brow2.addWidget(self.btn_del)
        v.addLayout(brow2)
        dk = QAction(w)
        dk.setShortcut(QKeySequence.Delete)
        dk.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        dk.triggered.connect(self.delete_downloads)
        w.addAction(dk)
        return w

    AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac"}
    VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".m4v"}

    def _refresh_history(self):
        """Audio scans and videos in separate lists: everything in the history that still exists,
        plus any other media files sitting in the downloads folder."""
        for lst in (self.hist_audio, self.hist_video):
            lst.clear()
        hist = config.load_settings().get("history", [])
        seen = set()
        entries = []
        for h in hist:
            path = h.get("path", "")
            if path and os.path.exists(path) and os.path.normcase(path) not in seen:
                seen.add(os.path.normcase(path))
                entries.append(h)
        try:
            extra = sorted((p for p in config.download_dir().iterdir()
                            if p.suffix.lower() in self.AUDIO_EXTS | self.VIDEO_EXTS
                            and os.path.normcase(str(p)) not in seen),
                           key=os.path.getmtime, reverse=True)
        except OSError:
            extra = []
        for p in extra:
            entries.append({"path": str(p), "title": p.stem})
        try:   # unfinished downloads left by a cancel (resume by downloading again, or delete)
            for p in sorted(config.download_dir().glob("*.part"), key=os.path.getmtime, reverse=True):
                inner = Path(p.stem)                       # "name.aac" from "name.aac.part"
                if inner.suffix.lower() in self.AUDIO_EXTS | self.VIDEO_EXTS or "audio" in p.name:
                    entries.append({"path": str(p), "title": "⏸ Unfinished: " + inner.stem, "partial": True,
                                    "audio": inner.suffix.lower() in self.AUDIO_EXTS or " audio" in p.name})
        except OSError:
            pass
        for h in entries:
            path = h["path"]
            ext = Path(path).suffix.lower()
            is_audio = h.get("audio") or ext in self.AUDIO_EXTS
            try:
                mb = os.path.getsize(path) / 1e6
            except OSError:
                mb = 0
            length = h.get("length") or h.get("duration", 0)
            bits = [h.get("title") or Path(path).stem]
            if length:
                bits.append(fmt_time(length))
            bits.append(f"{mb / 1000:.1f} GB" if mb >= 1000 else f"{mb:.0f} MB")
            it = QListWidgetItem("   ·   ".join(bits))
            it.setData(Qt.UserRole, path)
            it.setData(Qt.UserRole + 1, h)
            it.setToolTip(path)
            (self.hist_audio if is_audio else self.hist_video).addItem(it)
        self.hist_audio_lbl.setText(f"🔊 Audio scans ({self.hist_audio.count()})")
        self.hist_video_lbl.setText(f"🎬 Videos ({self.hist_video.count()})")
        self._update_delete_button()

    def _selected_downloads(self) -> list[str]:
        return [it.data(Qt.UserRole) for lst in (self.hist_audio, self.hist_video) for it in lst.selectedItems()]

    def _update_delete_button(self):
        n = len(self._selected_downloads())
        self.btn_del.setEnabled(n > 0)
        self.btn_del.setText(f"🗑 Delete {n} selected" if n > 1 else "🗑 Delete selected")

    def delete_downloads(self):
        paths = self._selected_downloads()
        if not paths:
            return
        total = sum(os.path.getsize(p) for p in paths if os.path.exists(p)) / 1e6
        in_use = [p for p in paths if os.path.normcase(p) == os.path.normcase(self.project.source or "")
                  or any(os.path.normcase(c.source) == os.path.normcase(p) for c in self.project.clips)]
        names = "\n".join("•  " + Path(p).name for p in paths[:8]) + ("\n…" if len(paths) > 8 else "")
        msg = (f"Move {len(paths)} download{'s' if len(paths) > 1 else ''} ({total:,.0f} MB) to the Recycle Bin?\n\n"
               f"{names}")
        if in_use:
            msg += ("\n\nSome of these are open in the Clip studio - it will be cleared. "
                    "Save the project first if you want to keep your clip list.")
        if QMessageBox.question(self, "Delete downloads", msg) != QMessageBox.Yes:
            return
        if in_use:                               # release the files so Windows lets us remove them
            if self.player:
                self.player.stop()
                self.player.setSource(QUrl())
            self.project = editor.Project(export=self.project.export)
            self.result, self.info, self.candidates = None, None, []
            self.src_label.setText("")
            self.src_label.hide()
            self.timeline.set_data(0)
            self.timeline.set_clips([])
            self.timeline.set_sections([])
            self._fill_clip_list()
            self._fill_summary()
            self._update_more_button()
            self._update_audio_banner()
            self._refresh_scrubber()
            self._update_no_video("")
        failed = []
        for p in paths:
            try:
                _to_recycle_bin(p)
                for side in (p + ".ytdl", str(Path(p).with_suffix("")) + ".ytdl"):   # yt-dlp resume notes
                    if os.path.exists(side):
                        os.remove(side)
            except Exception as e:
                failed.append(f"{Path(p).name}: {e}")
        gone = {os.path.normcase(p) for p in paths if not os.path.exists(p)}
        hist = [h for h in config.load_settings().get("history", [])
                if os.path.normcase(h.get("path", "")) not in gone]
        config.save_settings({"history": hist})
        self._has_video_cache.clear()
        self._refresh_history()
        if failed:
            QMessageBox.warning(self, "Couldn't delete everything", "\n".join(failed))
        else:
            self.statusBar().showMessage(f"Moved {len(paths)} download{'s' if len(paths) > 1 else ''} "
                                         f"({total:,.0f} MB) to the Recycle Bin", 5000)

    def check_link(self):
        url = self.url_edit.text().strip()
        if not url:
            return
        self.link_info.show()
        self.link_info.setText("Checking…")
        w = Worker(lambda url, on_progress=None, cancel=None: downloader.fetch_info(url), url)
        w.done.connect(lambda info: setattr(self, "_link_duration", float(info.get("duration") or 0))
                       or self.ranges_edit.set_duration(self._link_duration) or self._update_ranges_info())
        w.done.connect(lambda info: self.link_info.setText(
            f"<b>{info.get('title', '?')}</b> — {info.get('uploader', '')} — "
            f"{fmt_time(info.get('duration') or 0)}"
            + ("  <span style='color:#f59e0b'>(still live - wait for the replay)</span>"
               if info.get("is_live") else "")))
        w.failed.connect(lambda m: self.link_info.setText(f"<span style='color:#f87171'>{m}</span>"))
        self._start(w)

    def start_download(self, audio_only: bool = False):
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
        config.save_settings({"download_quality": self.quality.currentText(),
                              "part_ranges": self.ranges_edit.text().strip()})
        dur = self._link_duration or 0
        if ranges:
            total = downloader.ranges_total(ranges, dur or None)
            amount = (f" ({fmt_time(total)}" + (f" of {fmt_time(dur)})" if dur else ")")) if total else ""
            scope = (f"Audio of {len(ranges)} part{'s' if len(ranges) > 1 else ''}{amount}" if audio_only
                     else f"Video: {len(ranges)} part{'s' if len(ranges) > 1 else ''}{amount}")
        else:
            scope = "Audio of the whole stream" if audio_only else "The whole video"
        self.btn_dl.setEnabled(False)
        self.btn_scan.setEnabled(False)
        self._cancel_shown(self.btn_dl_cancel, True)
        self.dl_progress.setValue(0)
        self.dl_status.setText("Starting download…")
        w = Worker(downloader.download, url, self.quality.currentText(), config.download_dir(), ranges,
                   audio_only=audio_only)
        self.dl_status.setText(scope + "  ·  starting…"
                               + ("   (to fetch only some parts, tick “Only download part of the video”)"
                                  if not ranges else ""))
        self.btn_dl_cancel.clicked.connect(lambda: self._cancelling(w, self.btn_dl_cancel, self.dl_status))

        def prog(f, msg):
            if f < 0:                       # size unknown -> moving "busy" bar
                self.dl_progress.setRange(0, 0)
            else:
                self.dl_progress.setRange(0, 1000)
                self.dl_progress.setValue(int(f * 1000))
                self.dl_progress.setFormat(f"{f * 100:.1f}%")
            self.dl_status.setText(f"{scope}  ·  {msg}")

        def finished(res: downloader.DownloadResult):
            if audio_only:
                ttl = res.title
                if res.segments:
                    ttl += "  [" + ", ".join(f"{downloader.fmt(sg[1])}–{downloader.fmt(sg[1] + sg[2])}"
                                             for sg in res.segments) + "]"
                entries = [{"path": res.path, "title": ttl, "uploader": res.uploader,
                            "duration": res.duration, "url": res.url, "audio": True,
                            "segments": res.segments, "length": sum(sg[2] for sg in res.segments) or 0}]
            elif res.sections:
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
            self.load_video(e["path"], title=e["title"], url=res.url, offset=e.get("offset", 0.0),
                            segments=e.get("segments") or [])
            if audio_only:
                self.dl_status.setText(f"Audio saved ({Path(res.path).stat().st_size / 1e6:.0f} MB) - finding "
                                       "the best moments…")
                QTimer.singleShot(200, self.run_analysis)

        def reset():
            self.btn_dl.setEnabled(True)
            self.btn_scan.setEnabled(True)
            self._cancel_shown(self.btn_dl_cancel, False)
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
        self.src_label = ElidedLabel("")
        self.src_label.setObjectName("h2")
        self.src_label.hide()                   # the player already says when nothing is loaded
        v.addWidget(self.src_label)
        bar = QHBoxLayout()
        s = config.load_settings()
        self.n_clips = PresetBox([5, 10, 15], str, lo=1, hi=50)
        self.n_clips.setValue(int(s.get("batch_size", 5)))
        self.n_clips.setToolTip("How many moments to show at a time - press “5 more” under the list for the next batch")
        self.min_len = PresetBox([300, 600, 900, 1200, 1800], fmt_secs, parse_secs, lo=3, hi=3600)
        self.min_len.setValue(int(s.get("clip_min", 15)))
        self.min_len.setToolTip("Shortest a clip can be. Pick one or type your own, e.g. “20 s” or “1:30”. "
                                "Short moments get extra lead-in time.")
        self.max_len = PresetBox([300, 600, 900, 1200, 1800], fmt_secs, parse_secs, lo=5, hi=3600)
        self.max_len.setValue(int(s.get("clip_max", 45)))
        self.max_len.setToolTip("Longest a clip can be. Pick one or type your own, e.g. “45 s” or “2 min”. "
                                "Long heated stretches are trimmed around their peak.")
        self.min_len.valueChanged.connect(lambda x: self.max_len.value() < x and self.max_len.setValue(x))
        self.max_len.valueChanged.connect(lambda x: self.min_len.value() > x and self.min_len.setValue(x))
        self.min_gap = PresetBox([0, 10, 20, 30], fmt_secs, parse_secs, lo=0, hi=1800)
        self.min_gap.setValue(int(s["min_gap"]))
        for b, wdt in ((self.n_clips, 64), (self.min_len, 118), (self.max_len, 118), (self.min_gap, 118)):
            b.setMinimumWidth(wdt)
        self.use_scenes = QCheckBox("Scene changes"); self.use_scenes.setChecked(bool(s["scene_detection"]))
        self.judge_by = QComboBox()
        self.judge_by.addItem("Voice & energy only (fast)", "voice")
        self.judge_by.addItem("AI reads lively parts + samples (recommended)", "candidates")
        self.judge_by.addItem("AI reads everything (slowest)", "full")
        self.judge_by.setToolTip(
            "<b>How moments are judged</b><br>"
            "<b>Voice &amp; energy:</b> pitch vs the speaker's normal (upbeat = higher), how animated the voice is, "
            "talking pace, laughter, and loudness.<br>"
            "<b>AI reads lively parts + samples:</b> the ~20 liveliest stretches <i>and</i> a passage every few "
            "minutes across the whole stream are transcribed on your PC; your AI model (Settings → AI models) "
            "rates them - funny, hot take, shocking, heated, quotable… - and <b>only what's said qualifies a "
            "moment</b>: pitch, pace and loudness never pick one, they just break ties.<br>"
            "<b>AI reads everything:</b> the whole stream is read - slowest, misses nothing.<br>"
            "Use <b>Look for</b> to tell the AI what you want. Without an AI model, a built-in word list is used.")
        if not transcribe.available():
            for i in (1, 2):
                self.judge_by.model().item(i).setEnabled(False)
        want = s.get("judge_by", "candidates") if transcribe.available() else "voice"
        self.judge_by.setCurrentIndex(max(0, self.judge_by.findData(want)))
        self.min_gap.setToolTip("Minimum time between two moments, so you don't get near-duplicates")
        for lbl, wdg in [("Show", self.n_clips), ("Min length", self.min_len), ("Max length", self.max_len),
                         ("Gap", self.min_gap)]:
            bar.addWidget(QLabel(lbl))
            bar.addWidget(wdg)
        bar.addWidget(self.use_scenes)
        self._top_bar = bar                      # the player controls are added here too (always in view)
        self._top_bar_at = bar.count()
        bar.addStretch()
        self.btn_analyse = QPushButton("Find highlights")
        self.btn_analyse.setObjectName("primary")
        self.btn_analyse.clicked.connect(self.run_analysis)
        bar.addWidget(self.btn_analyse)
        v.addLayout(bar)
        grow = QHBoxLayout()
        grow.addWidget(QLabel("Judge by"))
        grow.addWidget(self.judge_by)
        grow.addSpacing(10)
        grow.addWidget(QLabel("🧠 Look for"))
        self.guidance = QLineEdit(s.get("ai_guidance", ""))
        self.guidance.setPlaceholderText("Tell the AI what you want, e.g. “funny moments”, “political hot takes”, "
                                         "“anything about Amy Acton”, “rants about the media”  (optional)")
        self.guidance.setClearButtonEnabled(True)
        self.guidance.returnPressed.connect(self.ask_ai_again)
        self.btn_reask = QPushButton("↻ Ask AI again")
        self.btn_reask.setToolTip("Re-rank using the text in “Look for” - reuses the transcript from the last scan, "
                                  "so it's much faster than a new scan")
        self.btn_reask.clicked.connect(self.ask_ai_again)
        self.btn_reask.setEnabled(False)
        grow.addWidget(self.guidance, 1)
        grow.addWidget(self.btn_reask)
        v.addLayout(grow)
        prow = QHBoxLayout()
        self.an_progress = QProgressBar(); self.an_progress.setRange(0, 1000); self.an_progress.setFixedHeight(18)
        self.an_progress.setTextVisible(True)
        self.an_progress.setFormat("")
        self.an_progress.valueChanged.connect(
            lambda v: self.an_progress.setFormat(f"{v / 10:.0f}%" if v > 0 and self.an_progress.maximum() else ""))
        self.an_status = ElidedLabel(""); self.an_status.setObjectName("muted")
        self.btn_an_cancel = QPushButton("Cancel")
        self.btn_an_cancel.setObjectName("danger")
        _keep = self.btn_an_cancel.sizePolicy(); _keep.setRetainSizeWhenHidden(True); self.btn_an_cancel.setSizePolicy(_keep)
        self._cancel_shown(self.btn_an_cancel, False)
        self.an_progress.setMinimumWidth(120)
        prow.addWidget(self.an_progress, 2); prow.addWidget(self.an_status, 3); prow.addWidget(self.btn_an_cancel)
        v.addLayout(prow)

        # audio-first banner
        self.audio_banner = QFrame()
        self.audio_banner.setObjectName("banner")
        ab = QHBoxLayout(self.audio_banner)
        ab.setContentsMargins(12, 6, 12, 6)
        self.audio_banner_text = QLabel("🔊 <b>Audio preview</b> - listen to the moments (▶ Play clip), tick the "
                                        "ones you want, then fetch only their video.")
        self.audio_banner_text.setWordWrap(True)
        self.btn_get_video = QPushButton("⬇ Get video for ticked moments")
        self.btn_get_video.setObjectName("primary")
        self.btn_get_video.clicked.connect(self.get_video_for_ticked)
        ab.addWidget(self.audio_banner_text, 1)
        ab.addWidget(self.btn_get_video)
        self.audio_banner.hide()
        v.addWidget(self.audio_banner)

        self.timeline = Timeline()
        self.timeline.seek.connect(self.scrub_to)
        self.timeline.clip_clicked.connect(lambda i: self.clip_list.setCurrentRow(i))
        self.timeline.add_at.connect(self.add_clip_at)
        self.timeline.clip_menu.connect(self._timeline_clip_menu)
        self.timeline.clip_moved.connect(self._timeline_clip_moved)
        self.timeline.clip_move_done.connect(self._timeline_clip_done)
        self.timeline.setToolTip("")
        trow = QHBoxLayout()
        trow.setSpacing(4)
        tcol = QVBoxLayout()
        tcol.setSpacing(4)
        tcol.addWidget(self.timeline)
        # when zoomed in: a scroll bar under the timeline to move along the stream
        self.tl_scroll = QScrollBar(Qt.Horizontal)
        self.tl_scroll.setObjectName("tlscroll")
        self.tl_scroll.setToolTip("Drag to move along the stream · click either side to jump a screen")
        self.tl_scroll.setCursor(Qt.PointingHandCursor)
        self.tl_scroll.hide()
        self.tl_scroll.valueChanged.connect(self._tl_scrolled)
        tcol.addWidget(self.tl_scroll)
        trow.addLayout(tcol, 1)
        zcol = QVBoxLayout()
        zcol.setSpacing(3)
        self.btn_zoom_in = QToolButton(); self.btn_zoom_in.setText("＋")
        self.btn_zoom_in.setToolTip("Zoom in (or scroll up on the timeline)")
        self.btn_zoom_in.clicked.connect(lambda: self.timeline.zoom_by(2.0))
        self.btn_zoom_out = QToolButton(); self.btn_zoom_out.setText("－")
        self.btn_zoom_out.setToolTip("Zoom out (or scroll down on the timeline)")
        self.btn_zoom_out.clicked.connect(lambda: self.timeline.zoom_by(0.5))
        self.btn_zoom_fit = QToolButton(); self.btn_zoom_fit.setText("Fit")
        self.btn_zoom_fit.setToolTip("Show the whole stream again · Shift+scroll moves left and right when zoomed in")
        self.btn_zoom_fit.clicked.connect(self.timeline.zoom_fit)
        for b in (self.btn_zoom_in, self.btn_zoom_out, self.btn_zoom_fit):
            b.setObjectName("zoomBtn")
            b.setFixedSize(40, 28)
            zcol.addWidget(b)
        self.zoom_lbl = QLabel("1×")
        self.zoom_lbl.setObjectName("muted")
        self.zoom_lbl.setAlignment(Qt.AlignCenter)
        zcol.addWidget(self.zoom_lbl)
        zcol.addStretch()
        trow.addLayout(zcol)
        v.addLayout(trow)

        def zoom_changed(z):
            self.zoom_lbl.setText(f"{z:.0f}×" if z >= 10 else f"{z:.1f}×".replace(".0×", "×"))
            self.btn_zoom_out.setEnabled(z > 1.001)
            self.btn_zoom_fit.setEnabled(z > 1.001)
            self.btn_zoom_in.setEnabled(self.timeline.duration > 0 and
                                        self.timeline.view[1] - self.timeline.view[0] > self.timeline.MAX_ZOOM_SPAN + 0.01)
            # keep the scroll bar in step with what the timeline shows
            a, b = self.timeline.view
            span, dur = b - a, self.timeline.duration
            sb = self.tl_scroll
            sb.blockSignals(True)
            sb.setVisible(z > 1.001)
            sb.setRange(0, max(0, int((dur - span) * 1000)))
            sb.setPageStep(max(1, int(span * 1000)))
            sb.setSingleStep(max(1, int(span * 100)))         # arrow keys / small steps: a tenth of a screen
            sb.setValue(int(a * 1000))
            sb.blockSignals(False)
        self.timeline.zoom_changed.connect(zoom_changed)
        zoom_changed(1.0)

        # the selected moment's own timeline - slides open under the main one
        self.clip_tl_box = QFrame()
        self.clip_tl_box.setObjectName("cliptl")
        ctl = QVBoxLayout(self.clip_tl_box)
        ctl.setContentsMargins(0, 4, 0, 0)
        ctl.setSpacing(2)
        self.clip_tl_title = QLabel("")
        self.clip_tl_title.setObjectName("muted")
        ctl.addWidget(self.clip_tl_title)
        self.clip_tl = ClipTimeline()
        self.clip_tl.time_fn = lambda t: self.project.to_stream(t)
        self.clip_tl.end_fn = lambda t: self.project.to_stream_end(t)
        self.clip_tl.span_changed.connect(self._clip_span_dragged)
        self.clip_tl.span_committed.connect(self._clip_span_committed)
        self.clip_tl.seek.connect(self.scrub_to)
        self.clip_tl.menu_requested.connect(lambda t, g: self.current_clip() and self._clip_menu(self.current_clip(), g, t))
        self.clip_tl.other_menu.connect(lambda i, t, g: 0 <= i < len(self.project.clips) and
                                        self._clip_menu(self.project.clips[i], g, t))
        ctl.addWidget(self.clip_tl)
        self.clip_tl_box.setMaximumHeight(0)
        self._clip_tl_h = 96
        self._clip_anim = QPropertyAnimation(self.clip_tl_box, b"maximumHeight", self)
        self._clip_anim.setDuration(180)
        self._clip_anim.setEasingCurve(QEasingCurve.OutCubic)
        v.addWidget(self.clip_tl_box)

        split = QSplitter(Qt.Horizontal)
        v.addWidget(split, 1)

        # left: clip list
        left = QWidget(); outer_l = QVBoxLayout(left); outer_l.setContentsMargins(0, 0, 0, 0)
        self.left_tabs = QTabWidget()
        self.left_tabs.setObjectName("lefttabs")
        moments_page = QWidget(); lv = QVBoxLayout(moments_page); lv.setContentsMargins(0, 4, 0, 0)
        self.clip_list = QListWidget()
        self.clip_list.setIconSize(QSize(128, 72))
        self.clip_list.setSpacing(2)
        self.clip_list.setWordWrap(True)
        self.clip_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.clip_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.clip_list.model().rowsMoved.connect(self._rows_moved)
        self.clip_list.currentRowChanged.connect(self.select_clip)
        self.clip_list.itemChanged.connect(self._item_checked)
        self.clip_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.clip_list.customContextMenuRequested.connect(self._list_clip_menu)
        lv.addWidget(self.clip_list, 1)
        self.btn_more = QPushButton("＋ 5 more moments")
        self.btn_more.setObjectName("more")
        self.btn_more.clicked.connect(self.show_more)
        self.btn_more.hide()
        lv.addWidget(self.btn_more)
        self.left_tabs.addTab(moments_page, "Moments")
        log_page = QWidget(); lgv = QVBoxLayout(log_page); lgv.setContentsMargins(0, 4, 0, 0)
        self.ai_log = QPlainTextEdit()
        self.ai_log.setReadOnly(True)
        self.ai_log.setObjectName("ailog")
        self.ai_log.setPlaceholderText("What the AI reads and finds shows up here while you scan.\n\n"
                                       "Tip: type what you're after in “Look for” above, then ↻ Ask AI again.")
        lgv.addWidget(self.ai_log, 1)
        lrow = QHBoxLayout()
        b_copy = QPushButton("Copy log"); b_copy.clicked.connect(lambda: QApplication.clipboard().setText(self.ai_log.toPlainText()))
        b_clear = QPushButton("Clear log"); b_clear.clicked.connect(self.ai_log.clear)
        lrow.addStretch(); lrow.addWidget(b_copy); lrow.addWidget(b_clear)
        lgv.addLayout(lrow)
        self.left_tabs.addTab(log_page, "AI log")
        sum_page = QWidget(); smv = QVBoxLayout(sum_page); smv.setContentsMargins(0, 4, 0, 0)
        self.sum_list = QListWidget()
        self.sum_list.setObjectName("summary")
        self.sum_list.setItemDelegate(StarDelegate(self.sum_list))
        self.sum_list.setWordWrap(True)
        self.sum_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sum_list.itemChanged.connect(lambda *_: self._update_sum_buttons())
        self.sum_list.itemClicked.connect(self._summary_clicked)
        self.sum_list.itemDoubleClicked.connect(lambda it: self.add_sections_to_moments([self.sum_list.row(it)]))
        smv.addWidget(self.sum_list, 1)
        srow = QHBoxLayout()
        self.btn_sum_add = QPushButton("★ Add ticked to Moments")
        self.btn_sum_add.setObjectName("primary")
        self.btn_sum_add.setToolTip("Adds the ticked sections to the top of Moments as your picks (★, gold) - "
                                    "trim them with the moment's timeline. Double-click a section to add just that one.")
        self.btn_sum_add.clicked.connect(lambda: self.add_sections_to_moments())
        self.btn_sum_add.setEnabled(False)
        b_scopy = QPushButton("Copy")
        b_scopy.setToolTip("Copy the summary as text")
        b_scopy.clicked.connect(lambda: QApplication.clipboard().setText(self._summary_text()))
        srow.addWidget(self.btn_sum_add, 1); srow.addWidget(b_scopy)
        smv.addLayout(srow)
        self.left_tabs.addTab(sum_page, "Summary")
        outer_l.addWidget(self.left_tabs, 1)
        lv = outer_l
        brow = QGridLayout()
        brow.setSpacing(4)
        for col, (text, tip, fn) in enumerate([
                ("＋ Add", "New clip around the current playhead position", self.add_clip_here),
                ("⧉ Copy", "Duplicate the selected clip", self.duplicate_clip),
                ("✖ Remove", "Remove the selected clip", self.remove_clip)]):
            b = QPushButton(text); b.setToolTip(tip); b.clicked.connect(fn)
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            brow.addWidget(b, 0, col)
        self.btn_ai_all = QPushButton("✨ AI titles for ticked clips")
        self.btn_ai_all.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.btn_ai_all.clicked.connect(lambda: self.ai_titles("all"))
        brow.addWidget(self.btn_ai_all, 1, 0, 1, 3)
        lv.addLayout(brow)
        self.btn_more.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        left.setMinimumWidth(240)
        split.addWidget(left)

        # centre: preview
        mid = QWidget(); mv = QVBoxLayout(mid); mv.setContentsMargins(8, 0, 8, 0)
        if HAVE_MM:
            self.video = QVideoWidget()
            self.video.setMinimumSize(280, 158)
            self.player = QMediaPlayer(self)
            self.audio = None
            if not os.environ.get("RUMBLECUT_NO_AUDIO"):   # for headless test machines
                self.audio = QAudioOutput(self)
                self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video)
            self.player.positionChanged.connect(self._pos_changed)
            self.player.playbackStateChanged.connect(lambda _: self._pos_changed(self.player.position()))
            self.player.playbackStateChanged.connect(self._studio_state)
            self.video_stack = QStackedWidget()
            self.video_stack.addWidget(self.video)
        else:
            self.player = None
            self.video_stack = QStackedWidget()
            self.video_stack.addWidget(QLabel("Video preview unavailable (Qt Multimedia missing)."))
        self.no_video = QLabel()
        self.no_video.setObjectName("novideo")
        self.no_video.setAlignment(Qt.AlignCenter)
        self.no_video.setWordWrap(True)
        self.video_stack.addWidget(self.no_video)
        self.video_stack.setMinimumSize(280, 158)
        self._showing_video = None
        mv.addWidget(self.video_stack, 1)
        self.scrubber = Scrubber()
        self.scrubber.time_fn = lambda t: self.project.to_stream(t)
        self.scrubber.seek.connect(self.scrub_to)
        self.scrubber.setToolTip("Drag to move anywhere · blue = video downloaded, grey = audio only")
        mv.addWidget(self.scrubber)
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
        self.btn_to_start = QToolButton(); self.btn_to_start.setText("⏮")
        self.btn_to_start.setToolTip("Back to the start of the selected moment (Home)")
        self.btn_to_start.setShortcut("Home")
        self.btn_to_start.clicked.connect(lambda: self.skip_to_edge("start"))
        self.btn_to_end = QToolButton(); self.btn_to_end.setText("⏭")
        self.btn_to_end.setToolTip("Jump to the end of the selected moment (End)")
        self.btn_to_end.setShortcut("End")
        self.btn_to_end.clicked.connect(lambda: self.skip_to_edge("end"))
        back = QToolButton(); back.setText("−5s"); back.clicked.connect(lambda: self.nudge(-5))
        fwd = QToolButton(); fwd.setText("+5s"); fwd.clicked.connect(lambda: self.nudge(5))
        self.btn_preview = QPushButton("👁 Preview effects")
        self.btn_preview.setToolTip("Quick low-res render of the selected clip with all effects applied")
        self.btn_preview.clicked.connect(self.render_preview)
        self.vol = QSlider(Qt.Horizontal); self.vol.setRange(0, 100); self.vol.setValue(80); self.vol.setMaximumWidth(90)
        self.vol_lbl = QLabel("80%"); self.vol_lbl.setMinimumWidth(38)
        self.vol.valueChanged.connect(lambda x: self.vol_lbl.setText(f"{x}%"))
        if self.player and self.audio:
            self.audio.setVolume(0.8)
            self.vol.valueChanged.connect(lambda x: self.audio.setVolume(x / 100))
        # playback controls sit in the top row (next to Scene changes) so they're always in view;
        # under the video: clip actions
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setObjectName("vsep")
        k = self._top_bar_at
        for wdg in [sep, self.btn_to_start, self.btn_play, self.btn_to_end, back, fwd, self.pos_label,
                    QLabel("🔊"), self.vol, self.vol_lbl]:
            self._top_bar.insertWidget(k, wdg)
            k += 1
        self.btn_play.setMinimumWidth(44)
        self.btn_play.setObjectName("playbtn")           # green, so it stands out up there
        self.pos_label.setMinimumWidth(92)
        self.vol.setMaximumWidth(72)
        tr2 = QHBoxLayout()
        for wdg in [self.btn_play_clip, self.btn_set_in, self.btn_set_out]:
            tr2.addWidget(wdg)
        tr2.addStretch()
        tr2.addWidget(self.btn_preview)
        mv.addLayout(tr2)
        sp = QAction(self); sp.setShortcut(Qt.Key_Space); sp.triggered.connect(self.toggle_play)
        self.addAction(sp)
        split.addWidget(mid)

        # right: inspector
        from .context_tab import ContextImagesTab
        from .script_tab import ScriptPanel
        self.side_tabs = QTabWidget()
        self.side_tabs.setObjectName("lefttabs")
        self.side_tabs.addTab(self._build_inspector(), "🎛  Clip")
        self.script_tab = ScriptPanel(self, embedded=True)
        self.side_tabs.addTab(self.script_tab, "✂  Script")
        self.ctx_tab = ContextImagesTab(self)
        self.side_tabs.addTab(self.ctx_tab, "🖼  Pictures")
        self.side_tabs.currentChanged.connect(self._side_tab_changed)
        self.edit_tabs = self.side_tabs           # (older name)
        self.studio_split = split
        split.addWidget(self.side_tabs)
        split.setChildrenCollapsible(False)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([360, 640, 340])
        return root

    def _build_inspector(self) -> QWidget:
        box = QWidget()
        f = QVBoxLayout(box)
        f.setContentsMargins(6, 0, 6, 0)

        g1 = QGroupBox("Clip")
        fl = QFormLayout(g1)
        self.i_title = QLineEdit(); self.i_title.setPlaceholderText("Clip title (used as the file name)")
        self.i_start = TimeSpinBox(); self.i_end = TimeSpinBox()
        for sb in (self.i_start, self.i_end):
            sb.to_disp = lambda t: self.project.to_stream(t)
            sb.from_disp = lambda t: self.project.from_stream(t)
        self.i_end.to_disp = lambda t: self.project.to_stream_end(t)
        self.i_len = QLabel("")
        self.i_why = QLabel(""); self.i_why.setWordWrap(True); self.i_why.setObjectName("muted")
        fl.addRow("Title", self.i_title)
        self.btn_ai_title = QPushButton("✨ Write title && caption with AI")
        self.btn_ai_title.setToolTip("Transcribes this clip and asks your AI model (Settings → AI models) "
                                     "for a title and on-screen caption")
        self.btn_ai_title.clicked.connect(lambda: self.ai_titles("current"))
        fl.addRow("", self.btn_ai_title)
        fl.addRow("Start", self.i_start)
        fl.addRow("End", self.i_end)
        fl.addRow("Length", self.i_len)
        self.btn_script = QPushButton("✂ Cut words by script")
        self.btn_script.setToolTip("Open this moment's script in 🎬 Edit clips: delete words in the text and "
                                   "they're cut from the clip")
        self.btn_script.clicked.connect(self.edit_script_for_current)
        fl.addRow("", self.btn_script)
        fl.addRow("Why", self.i_why)
        f.addWidget(g1)

        g2 = QGroupBox("Effects")
        fl = QFormLayout(g2)
        self.i_speed = QComboBox(); self.i_speed.addItems(["0.5×", "0.75×", "1×", "1.25×", "1.5×", "2×"]); self.i_speed.setCurrentText("1×")
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
        self.btn_auto_img = QPushButton("✨ Find pictures")
        self.btn_auto_img.setToolTip("Suggest pictures for what's said in this moment - you review them in the "
                                     "🎬 Edit clips tab")
        self.btn_auto_img.clicked.connect(self.find_pictures_for_current)
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
        self.i_start.valueChanged.connect(lambda x: self._set("start", x - self._cur_offset()))
        self.i_end.valueChanged.connect(lambda x: self._set("end", x - self._cur_offset()))
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
        scroll.setMinimumWidth(280)
        self.inspector = box
        box.setEnabled(False)
        return scroll

    # ------------------------------------------------------------------ video loading
    def open_video_dialog(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open video", str(config.download_dir()),
                                           "Video or audio (*.mp4 *.mkv *.mov *.webm *.avi *.flv *.ts *.m4v *.m4a *.mp3 *.aac *.opus *.wav);;All files (*)")
        if p:
            self.load_video(p)

    def _open_history_item(self, it: QListWidgetItem):
        h = it.data(Qt.UserRole + 1) or {}
        if h.get("partial"):
            QMessageBox.information(self, APP_NAME, "This download didn't finish. Paste the same link and press "
                                    "Download / ⚡ Scan audio first again to resume it - or delete it.")
            return
        self.load_video(it.data(Qt.UserRole), title=h.get("title", ""), url=h.get("url", ""),
                        offset=float(h.get("offset", 0.0)), segments=h.get("segments") or [])

    def _update_ranges_info(self):
        txt = self.ranges_edit.text().strip()
        if not self.part_only.isChecked() or not txt:
            self.ranges_info.setText("")
            self.ranges_info.hide()
            return
        self.ranges_info.show()
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

    def load_video(self, path: str, title: str = "", url: str = "", offset: float = 0.0,
                   segments: list | None = None):
        try:
            self.info = ft.MediaInfo(path)
        except Exception as e:
            QMessageBox.warning(self, "Can't open video", str(e))
            return
        self.project = editor.Project(source=path, title=title or Path(path).stem, url=url,
                                      offset=float(offset or 0.0), segments=list(segments or []),
                                      export=self.project.export)
        self.project_path = None
        self.result = None
        if self.project.segments:
            part = "   ·   parts " + ", ".join(f"{fmt_time(sg[1])}–{fmt_time(sg[1] + sg[2])}"
                                              for sg in self.project.segments)
        else:
            part = (f"   ·   part starting at {fmt_time(self.project.offset)} in the stream"
                    if self.project.offset else "")
        if not self.info.has_video:
            part += "   ·   🔊 audio only"
        dims = f"   ·   {self.info.width}×{self.info.height}" if self.info.has_video else ""
        self.src_label.setText(f"{self.project.title}   ·   {fmt_time(self.info.duration)}{dims}{part}")
        self.src_label.show()
        self.timeline.time_fn = self.project.to_stream
        self.timeline.joins = [sg[0] for sg in self.project.segments[1:]]
        self.timeline.set_data(self.info.duration)
        self.timeline.set_clips([])
        self.candidates = []
        self.btn_reask.setEnabled(False)
        self._fill_clip_list()
        self._update_more_button()
        self._load_media(path, 0.0)
        if self.player:
            self.player.pause()
        self._update_audio_banner()
        self._refresh_scrubber()
        self._show_pos(0.0)
        self._fill_summary()
        self.tabs.setCurrentIndex(1)
        self.statusBar().showMessage(f"Loaded {path}", 5000)
        self.btn_analyse.setFocus()

    # ------------------------------------------------------------------ analysis
    def _log(self, line: str):
        from datetime import datetime
        self.ai_log.appendPlainText(f"{datetime.now():%H:%M:%S}  {line}")

    def ask_ai_again(self):
        """Re-rank with the current guidance, reusing the transcript + voice pass of the last scan."""
        r = self.result
        if not (r is not None and getattr(r, "blocks", None)):
            if self.project.source:
                self.run_analysis()
            return
        if not ai.is_configured():
            QMessageBox.information(self, APP_NAME, "Choose an AI model first: Settings → AI models.")
            return
        self.run_analysis(reuse=True)

    def run_analysis(self, reuse: bool = False):
        if not self.project.source:
            QMessageBox.information(self, APP_NAME, "Load a video first (tab 1, Get video).")
            return
        ai_clips = [c for c in self.project.clips if c.origin == "ai"]
        ticked_fetched = [c for c in ai_clips if c.source != self.project.source]
        mine = len(self.project.clips) - len(ai_clips)
        if ai_clips and not (reuse and not ticked_fetched) and QMessageBox.question(
                self, "Replace moments?", "This replaces the moments the app found"
                + (f" (including {len(ticked_fetched)} you already fetched video for)" if ticked_fetched else "")
                + "." + (f" Your {mine} ★ pick{'s are' if mine != 1 else ' is'} kept." if mine else "")
                + " Continue?") != QMessageBox.Yes:
            return
        config.save_settings({"batch_size": self.n_clips.value(), "clip_min": self.min_len.value(),
                              "clip_max": self.max_len.value(),
                              "min_gap": self.min_gap.value(), "scene_detection": self.use_scenes.isChecked(),
                              "judge_by": self.judge_by.currentData(), "ai_guidance": self.guidance.text().strip()})
        src = self.project.source
        n, gap = 100, self.min_gap.value()   # rank all, show in batches
        lo, hi = self.min_len.value(), self.max_len.value()
        scenes = self.use_scenes.isChecked() and bool(self.info and self.info.has_video)
        words = self.judge_by.currentData() if transcribe.available() else "voice"
        use_ai = ai.is_configured()
        guidance = self.guidance.text().strip()
        cached_blocks = cached_voice = None
        if reuse and self.result is not None and getattr(self.result, "blocks", None):
            cached_blocks, cached_voice = self.result.blocks, getattr(self.result, "voice", None)
            words = words if words != "voice" else "candidates"
        if words != "voice":
            self.left_tabs.setCurrentIndex(1)                # show the AI log while it works
            self._log(("↻ Asking again" if reuse else "▶ New scan") + f" - {Path(src).name}"
                      + (f" - looking for: {guidance}" if guidance else ""))
        self._judged_with = ("voice & energy" if words == "voice" else
                             f"voice + what's said ({ai.describe() if use_ai else 'built-in word list'})")

        first_call = {"done": False}

        def transcriber(path, a, b, cancel=None, on_progress=None):
            segs = transcribe.transcribe(path, start=a, end=b, cancel=cancel, on_progress=on_progress)
            if not first_call["done"] and holder and transcribe.device_note:
                first_call["done"] = True
                holder[0].log.emit(f"🎙 Speech-to-text is running on the {transcribe.device_note}")
            return segs

        holder: list = []

        def job(on_progress=None, cancel=None):
            return analysis.find_highlights(src, n, (lo + hi) / 2, gap, scenes, None,
                                            on_progress=on_progress, cancel=cancel, min_len=lo, max_len=hi,
                                            words=words, transcriber=transcriber,
                                            ai_reader=diction.ai_spans if use_ai else None,
                                            guidance=guidance, on_log=lambda m: holder[0].log.emit(m),
                                            cached_blocks=cached_blocks, cached_voice=cached_voice,
                                            time_fn=self.project.to_stream)

        w = Worker(job)
        holder.append(w)
        w.log.connect(self._log)
        self.btn_analyse.setEnabled(False)
        self._cancel_shown(self.btn_an_cancel, True)
        self.btn_an_cancel.clicked.connect(lambda: self._cancelling(w, self.btn_an_cancel, self.an_status))
        w.progress.connect(lambda f, m: (self.an_progress.setRange(0, 1000), self.an_progress.setValue(int(f * 1000)),
                                         self.an_status.setText(m)))
        w.done.connect(self._analysis_done)
        w.failed.connect(lambda m: QMessageBox.warning(self, "Analysis failed", m))
        w.cancelled.connect(lambda: self.an_status.setText("Cancelled"))

        def reset():
            self.btn_analyse.setEnabled(True)
            self.btn_reask.setEnabled(bool(self.result is not None and getattr(self.result, "blocks", None)))
            self._cancel_shown(self.btn_an_cancel, False)
            try:
                self.btn_an_cancel.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass
        self.btn_reask.setEnabled(False)
        w.finished.connect(reset)
        self._start(w)

    def _analysis_done(self, res: analysis.AnalysisResult):
        self.result = res
        self.candidates = list(res.highlights)       # already ranked best first
        self._shown_total = 0
        self.project.clips = [c for c in self.project.clips if c.origin != "ai"]   # your picks stay
        new_secs = [x.as_dict() for x in getattr(res, "sections", []) or []]
        if new_secs or (getattr(res, "blocks", None) is not None and getattr(res, "word_note", "") == "ai"):
            self.project.sections = new_secs
        self._fill_summary()
        self.timeline.set_data(res.duration, res.times, res.score)
        self.show_more(first=True)
        n = len(res.highlights)
        note = getattr(res, "word_note", "")
        judged = getattr(self, "_judged_with", "voice & energy")
        if note and note not in ("ai", "words"):
            judged = "voice & energy ONLY - the AI didn't read anything"
        msg = (f"Found {n} possible moment{'s' if n != 1 else ''} - showing the best {min(n, self.n_clips.value())}"
               f"  ·  judged by {judged}")
        if note and note not in ("ai", "words"):
            msg += f"  ·  ⚠ {note}"
            self.statusBar().showMessage("⚠ " + note, 10000)
        self.an_status.setText(msg)
        if getattr(res, "blocks", None) is not None:
            ai_n = sum(1 for h in res.highlights if any(w.startswith("What's said") and "from your AI" in w for w in h.why))
            self._log(f"✔ Done: {n} moments ranked"
                      + (f", {ai_n} of them picked for what's said" if ai_n else "")
                      + f". Top: {res.highlights[0].kind} at {fmt_time(self.project.to_stream(res.highlights[0].peak))}"
                      if res.highlights else "✔ Done: nothing found.")
            QTimer.singleShot(1500, lambda: self.left_tabs.setCurrentIndex(0))
        self.btn_reask.setEnabled(bool(getattr(res, "blocks", None)))
        QTimer.singleShot(500, self._autosave)

    def show_more(self, first: bool = False):
        """Move the next batch of ranked moments into the clip list."""
        batch = self.n_clips.value()
        take, self.candidates = self.candidates[:batch], self.candidates[batch:]
        start_row = len(self.project.clips)
        for h in take:
            self._shown_total = getattr(self, "_shown_total", 0) + 1
            self.project.clips.append(editor.Clip(
                source=self.project.source, start=h.start, end=h.end, score=h.score, reasons=h.reasons,
                why=list(h.why), timing=list(h.timing),
                title=f"{h.kind} at {fmt_time(self.project.to_stream(h.peak))}"))
        self._fill_clip_list()
        if take:
            self.clip_list.setCurrentRow(start_row)
            self.clip_list.scrollToItem(self.clip_list.item(start_row))
            self._make_thumbnails()
        self._update_more_button()
        self._update_audio_banner()
        if not first and take:
            self.an_status.setText(f"Added {len(take)} more moment{'s' if len(take) != 1 else ''}"
                                   f" - {len(self.candidates)} left")

    def _update_more_button(self):
        left = len(self.candidates)
        batch = self.n_clips.value()
        if self.result is None and not left:
            self.btn_more.hide()
            return
        self.btn_more.show()
        self.btn_more.setEnabled(left > 0)
        self.btn_more.setText(f"＋ {min(batch, left)} more moment{'s' if min(batch, left) != 1 else ''}"
                              f"   ({left} left)" if left else
                              "No more moments found - lower “Min gap” and search again for more")

    def _make_thumbnails(self):
        jobs = [(c.source, (c.start + c.end) / 2) for c in self.project.clips
                if self._has_video(c.source)]
        out_dir = self.thumb_dir
        have = set(getattr(self, "_thumbs", {}))

        def job(on_progress=None, cancel=None):
            res = {}
            for i, (src, t) in enumerate(jobs):
                if cancel():
                    break
                key = (src, round(t, 1))
                if key in have:
                    continue
                p = out_dir / f"{abs(hash(key))}.jpg"
                try:
                    if not p.exists():
                        ft.extract_frame(src, t, str(p), 256)
                    res[key] = str(p)
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
        for c in self.project.clips:              # remember how each moment was first found (for ↺ Reset)
            if len(c.orig) != 2:
                c.orig = [c.start, c.end]
        row = self.clip_list.currentRow()
        self.clip_list.blockSignals(True)
        self.clip_list.clear()
        thumbs = getattr(self, "_thumbs", {})
        for i, c in enumerate(self.project.clips):
            it = QListWidgetItem(self._item_text(i, c))
            it.setToolTip(self._why_html(c))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
            it.setCheckState(Qt.Checked if c.enabled else Qt.Unchecked)
            t = thumbs.get((c.source, round((c.start + c.end) / 2, 1)))
            if t:
                it.setIcon(QIcon(QPixmap(t)))
            it.setData(Qt.UserRole, id(c))
            if c.origin == "user":                # your picks from the Summary: gold
                it.setBackground(QBrush(QColor("#2a2414")))
                it.setForeground(QBrush(QColor("#f5d48a")))
            elif c.origin == "manual":            # moments you made yourself: blue
                it.setBackground(QBrush(QColor("#16222e")))
                it.setForeground(QBrush(QColor("#a9d1f5")))
            self.clip_list.addItem(it)
        self.clip_list.blockSignals(False)
        self._sync_timeline(row if keep_row else -1)
        self._refresh_scrubber()
        if keep_row and 0 <= row < self.clip_list.count():
            self.clip_list.setCurrentRow(row)
        elif not self.project.clips:
            self.inspector.setEnabled(False)
            self._show_clip_timeline(None)
        self._mark_added_sections()

    def _others_for(self, c) -> list[tuple]:
        """The other moments, for drawing behind c on its own timeline."""
        return [(o.start + o.offset, o.end + o.offset, i, o.origin, o.hidden)
                for i, o in enumerate(self.project.clips) if o is not c]

    def _sync_timeline(self, row: int):
        self.timeline.hidden = [c.hidden for c in self.project.clips]
        c = self.current_clip()
        if c is not None:
            self.clip_tl.others = self._others_for(c)
            self.clip_tl.hidden = c.hidden
            self.clip_tl.update()
        self.timeline.limits = [self._clip_limits(c) for c in self.project.clips]
        self.timeline.set_clips(self._spans(), row, [self._why_html(c) for c in self.project.clips],
                                [c.origin for c in self.project.clips])

    def _rows_moved(self, *_):
        by_id = {id(c): c for c in self.project.clips}
        self.project.clips = [by_id[self.clip_list.item(i).data(Qt.UserRole)]
                              for i in range(self.clip_list.count())]
        QTimer.singleShot(0, lambda: self._fill_clip_list(keep_row=True))

    def _item_checked(self, it: QListWidgetItem):
        r = self.clip_list.row(it)
        if 0 <= r < len(self.project.clips):
            self.project.clips[r].enabled = it.checkState() == Qt.Checked
            self._update_audio_banner()

    def current_clip(self) -> editor.Clip | None:
        r = self.clip_list.currentRow()
        return self.project.clips[r] if 0 <= r < len(self.project.clips) else None

    def select_clip(self, row: int):
        c = self.current_clip()
        if hasattr(self, "side_tabs") and self.tabs.currentIndex() == 1:
            if self.side_tabs.currentIndex() == 1 and getattr(self.script_tab, "_shown_clip", None) is not c:
                QTimer.singleShot(0, self.script_tab.refresh)
            elif self.side_tabs.currentIndex() == 2 and c is not None:
                QTimer.singleShot(0, lambda c=c: self.ctx_tab.select_clip(c))
        self.inspector.setEnabled(c is not None)
        self._sync_timeline(row)
        self._show_clip_timeline(c)
        if not c:
            return
        self._loading_inspector = True
        try:
            src_dur = ft.MediaInfo(c.source).duration if c.source != self.project.source else self.info.duration
        except Exception:
            src_dur = 1e7
        for sb in (self.i_start, self.i_end):
            sb.setRange(c.offset, c.offset + (src_dur or 1e7))
        self.i_title.setText(c.title)
        self.i_title.setCursorPosition(0)
        self.i_start.setValue(c.start + c.offset)
        self.i_end.setValue(c.end + c.offset)
        self.i_len.setText(f"{c.length:.1f}s  →  {c.out_length:.1f}s exported")
        self.i_why.setText({"user": "★ Your pick · ", "manual": "✎ Made by you · "}.get(c.origin, "") + (" · ".join(c.reasons) or "Added manually"))
        self.i_why.setToolTip(self._why_html(c))
        self.i_why.setText(self.i_why.text() + "   ⓘ")
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
        tl, a, b = self.timeline, c.start + c.offset, c.end + c.offset
        if tl.zoom > 1.001 and not (tl.view[0] <= a and b <= tl.view[1]):   # zoomed in: bring the moment into view
            span = max(tl.view[1] - tl.view[0], (b - a) * 1.4)
            mid = (a + b) / 2
            tl.set_view(mid - span / 2, mid + span / 2)
        self.seek_clip(c, c.start)

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
                    self.i_end.blockSignals(True); self.i_end.setValue(c.end + c.offset); self.i_end.blockSignals(False)
                else:
                    c.start = max(0, c.end - 1)
                    self.i_start.blockSignals(True); self.i_start.setValue(c.start + c.offset)
                    self.i_start.blockSignals(False)
            self.seek_clip(c, c.start if attr == "start" else max(c.start, c.end - 3))
        if attr in ("start", "end"):
            self._show_clip_timeline(c, keep_view=True)
        if attr in ("start", "end", "speed"):
            self.i_len.setText(f"{c.length:.1f}s  →  {c.out_length:.1f}s exported")
        if attr in ("title", "start", "end", "speed"):
            self._update_item_text(self.clip_list.currentRow())

    def _item_text(self, i: int, c) -> str:
        if c.hidden:
            a, b = self.project.to_stream(c.start + c.offset), self.project.to_stream_end(c.end + c.offset)
            return f"? {i + 1}. (hidden)  {c.title}\n{fmt_time(a)} – {fmt_time(b)}  ·  {c.out_length:.0f}s"
        reason = c.reasons[0] if c.reasons else "Manual clip"
        a, b = self.project.to_stream(c.start + c.offset), self.project.to_stream_end(c.end + c.offset)
        tag = "   🔊 audio only" if not self._has_video(c.source) else ("   🎬 video fetched" if c.offset else "")
        if c.origin != "ai":
            mark = "✎" if c.origin == "manual" else "★"
            return (f"{mark} {i + 1}. {c.title}\n{fmt_time(a)} – {fmt_time(b)}  ·  {c.out_length:.0f}s  ·  "
                    f"your pick{tag}\n{reason}")
        return (f"{i + 1}. {c.title}\n{fmt_time(a)} – {fmt_time(b)}  ·  {c.out_length:.0f}s  ·  "
                f"score {c.score:.0f}{tag}\n{reason}")

    def _why_html(self, c) -> str:
        """Hover text: why this moment was picked and why it starts/ends where it does."""
        a, b = self.project.to_stream(c.start + c.offset), self.project.to_stream_end(c.end + c.offset)
        why = c.why or c.reasons or ["Added by hand."]
        timing = c.timing or ["You set the start and end yourself."]
        li = lambda xs: "".join(f"<li>{x}</li>" for x in xs)
        return (f"<div style='max-width:420px'><b>Why this moment</b> · score {c.score:.0f}/100"
                f"<ul style='margin:2px 0 6px -20px'>{li(why)}</ul>"
                f"<b>Why {fmt_time(a)} – {fmt_time(b)}</b> ({b - a:.0f} s)"
                f"<ul style='margin:2px 0 0 -20px'>{li(timing)}</ul></div>")

    def _cur_offset(self) -> float:
        c = self.current_clip()
        return c.offset if c else 0.0

    def _spans(self):
        return [(c.start + c.offset, c.end + c.offset) for c in self.project.clips]

    def _has_video(self, path: str) -> bool:
        if path not in self._has_video_cache:
            try:
                self._has_video_cache[path] = ft.MediaInfo(path).has_video
            except Exception:
                self._has_video_cache[path] = False
        return self._has_video_cache[path]

    def _update_item_text(self, r):
        if not (0 <= r < len(self.project.clips)):
            return
        self.clip_list.item(r).setText(self._item_text(r, self.project.clips[r]))
        self._sync_timeline(r)

    def _user_insert_row(self) -> int:
        """Your picks stay together at the top of Moments, newest last."""
        r = 0
        while r < len(self.project.clips) and self.project.clips[r].origin != "ai":
            r += 1
        return r

    # ------------------------------------------------------------------ the moment's own timeline
    def _show_clip_timeline(self, c, keep_view: bool = False):
        want = c is not None and bool(self.project.source)
        if want:
            r = self.project.clips.index(c) if c in self.project.clips else -1
            if c.source == self.project.source:
                lim = (0.0, self.info.duration if self.info else c.end + 60)
            else:
                lim = (c.offset, c.offset + (self._src_duration(c.source) or (c.end + 60)))
            r_ = self.result
            self.clip_tl.cuts = [(a + c.offset, b + c.offset) for a, b in c.cuts]
            self.clip_tl.others = self._others_for(c)
            self.clip_tl.hidden = c.hidden
            self.clip_tl.set_clip(c.start + c.offset, c.end + c.offset, lim,
                                  getattr(r_, "times", None), getattr(r_, "score", None),
                                  user=c.origin, keep_view=keep_view)
            who = {"user": "★ Your pick", "manual": "✎ Your moment"}.get(c.origin, f"Moment {r + 1}")
            self.clip_tl_title.setText(f"{who} - {c.title}   ·   drag the handles for the exact start and end, "
                                       "drag the middle to move it, click outside to jump there")
        target = self._clip_tl_h if want else 0
        if self.clip_tl_box.maximumHeight() != target and self._clip_anim.endValue() != target:
            self._clip_anim.stop()
            self._clip_anim.setStartValue(self.clip_tl_box.maximumHeight())
            self._clip_anim.setEndValue(target)
            self._clip_anim.start()

    def _clip_span_dragged(self, a: float, b: float, what: str):
        c = self.current_clip()
        if not c:
            return
        c.start, c.end = round(a - c.offset, 2), round(b - c.offset, 2)
        for sb, v in ((self.i_start, a), (self.i_end, b)):
            sb.blockSignals(True); sb.setValue(v); sb.blockSignals(False)
        self.i_len.setText(f"{c.length:.1f}s  →  {c.out_length:.1f}s exported")
        self._update_item_text(self.clip_list.currentRow())
        self.seek_clip(c, c.end if what == "end" else c.start)      # see / hear the exact edge

    def _clip_span_committed(self, a: float, b: float, what: str):
        c = self.current_clip()
        if not c:
            return
        note = "You set the start and end yourself on the moment's timeline."
        if note not in c.timing:
            c.timing = list(c.timing) + [note]
        self.clip_list.item(self.clip_list.currentRow()).setToolTip(self._why_html(c))
        self._sync_timeline(self.clip_list.currentRow())
        # land just before the edge you moved, so pressing play lets you hear it
        self.seek_clip(c, max(c.start, c.end - 3) if what == "end" else c.start)

    # ------------------------------------------------------------------ the AI's summary
    def _fill_summary(self):
        secs = list(self.project.sections or [])
        self.timeline.set_sections([(x["start"], x["end"], x["interest"], x["summary"]) for x in secs])
        self.sum_list.blockSignals(True)
        self.sum_list.clear()
        if not secs:
            it = QListWidgetItem("No summary yet.\n\nScan with “AI reads…” (Settings → AI models) and the AI "
                                 "describes each section of the stream here - e.g. “argues with a viewer about "
                                 "tariffs”. Tick the ones you like to add them to Moments.")
            it.setFlags(Qt.NoItemFlags)
            self.sum_list.addItem(it)
        for x in secs:
            a, b = self.project.to_stream(x["start"]), self.project.to_stream_end(x["end"])
            n = round(x["interest"] * 10)
            it = QListWidgetItem(f"{fmt_time(a)} – {fmt_time(b)}\n{x['summary']}")
            it.setData(StarDelegate.ROLE, n)
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            if x["interest"] < 0.4:
                it.setForeground(QBrush(QColor("#6b7280")))
                it.setToolTip("The AI found little of interest here (filler, a transition, chat-reading…)")
            else:
                it.setToolTip(f"{fmt_time(a)} – {fmt_time(b)} ({(b - a) / 60:.1f} min) · rated {n}/10<br>{x['summary']}"
                              "<br><i>Click to listen · double-click to add to Moments</i>")
            self.sum_list.addItem(it)
        self.sum_list.blockSignals(False)
        self._mark_added_sections()
        self._update_sum_buttons()
        n = len(secs)
        self.left_tabs.setTabToolTip(2, f"{n} sections summarised by the AI" if n else "No summary yet")

    def _mark_added_sections(self):
        if not hasattr(self, "sum_list"):
            return
        picks = {(round(c.start, 1), round(c.end, 1)) for c in self.project.clips
                 if c.origin != "ai" and c.source == self.project.source}
        self.sum_list.blockSignals(True)
        for i, x in enumerate(self.project.sections or []):
            it = self.sum_list.item(i)
            if it is None:
                continue
            text = it.text().split("   ★ in Moments")[0]
            first, _, rest = text.partition("\n")
            if (round(x["start"], 1), round(x["end"], 1)) in picks:
                first += "   ★ in Moments"
            it.setText(first + "\n" + rest)
        self.sum_list.blockSignals(False)

    def _update_sum_buttons(self):
        n = sum(1 for i in range(self.sum_list.count())
                if self.sum_list.item(i).flags() & Qt.ItemIsUserCheckable
                and self.sum_list.item(i).checkState() == Qt.Checked)
        self.btn_sum_add.setEnabled(n > 0)
        self.btn_sum_add.setText(f"★ Add {n} to Moments" if n else "★ Add ticked to Moments")

    def _summary_clicked(self, it):
        r = self.sum_list.row(it)
        secs = self.project.sections or []
        if 0 <= r < len(secs):
            self.scrub_to(float(secs[r]["start"]))

    def _summary_text(self) -> str:
        return "\n".join(f"{fmt_time(self.project.to_stream(x['start']))} – "
                         f"{fmt_time(self.project.to_stream_end(x['end']))}  ({round(x['interest'] * 10)}/10)  "
                         f"{x['summary']}" for x in (self.project.sections or []))

    def add_sections_to_moments(self, rows: list[int] | None = None):
        secs = self.project.sections or []
        if rows is None:
            rows = [i for i in range(min(self.sum_list.count(), len(secs)))
                    if self.sum_list.item(i).checkState() == Qt.Checked]
        rows = [r for r in rows if 0 <= r < len(secs)]
        if not rows or not self.project.source:
            return
        have = {(round(c.start, 1), round(c.end, 1)) for c in self.project.clips if c.origin != "ai"}
        at = self._user_insert_row()
        added = 0
        for r in rows:
            x = secs[r]
            a, b = float(x["start"]), float(x["end"])
            if (round(a, 1), round(b, 1)) in have:
                continue
            summ = x["summary"].strip().rstrip(".")
            title = (summ[:1].upper() + summ[1:])[:60].rstrip() or "Section"
            sa, sb = self.project.to_stream(a), self.project.to_stream_end(b)
            c = editor.Clip(source=self.project.source, start=round(a, 2), end=round(b, 2), title=title,
                            score=round(float(x["interest"]) * 100, 1), origin="user",
                            reasons=[f"From the summary: {summ}"[:70]],
                            why=[f"You picked this section from the AI's summary: “{summ}” "
                                 f"(rated {round(x['interest'] * 10)}/10)."],
                            timing=[f"The whole section as the AI summarised it, {fmt_time(sa)} – {fmt_time(sb)} "
                                    f"({(sb - sa) / 60:.1f} min) - trim it on the moment's timeline."])
            self.project.clips.insert(at + added, c)
            added += 1
        for i in range(self.sum_list.count()):
            it = self.sum_list.item(i)
            if it.flags() & Qt.ItemIsUserCheckable:
                it.setCheckState(Qt.Unchecked)
        if not added:
            self._show_toast("Those sections are already in Moments")
            return
        self._fill_clip_list()
        self.left_tabs.setCurrentIndex(0)
        self.clip_list.setCurrentRow(at)
        self.clip_list.scrollToItem(self.clip_list.item(at))
        self._make_thumbnails()
        self._update_audio_banner()
        self._show_toast(f"★ Added {added} section{'s' if added != 1 else ''} to the top of Moments as your pick"
                         f"{'s' if added != 1 else ''}")

    def add_clip_at(self, t: float):
        """Right-click on the timeline: a new moment of your own, centred where you clicked."""
        if not self.project.source:
            return
        dur = self.info.duration if self.info else t + 30
        L = max(1.0, min(float(config.load_settings().get("manual_len", 5.0) or 5.0), dur))
        s0 = max(0.0, min(t - L / 2, dur - L))
        c = editor.Clip(source=self.project.source, start=round(s0, 1), end=round(min(dur, s0 + L), 1),
                        title=f"My moment - {fmt_time(self.project.to_stream(t)).replace(':', '.')}",
                        reasons=["Made by you on the timeline"], origin="manual",
                        timing=["You placed it with a right-click on the timeline - drag it or its edges to adjust."])
        r = self._user_insert_row()
        self.project.clips.insert(r, c)
        self._fill_clip_list()
        self.clip_list.setCurrentRow(r)
        self._update_audio_banner()
        self._show_toast("✎ New moment added - drag it on the timeline to move it")

    def _timeline_clip_menu(self, i: int, t: float, gpos):
        """Right-click on a moment in the timeline (t = where on the project timeline)."""
        if 0 <= i < len(self.project.clips):
            self._clip_menu(self.project.clips[i], gpos, t)

    def _list_clip_menu(self, pos):
        it = self.clip_list.itemAt(pos)
        if it is not None:
            self._clip_menu(self.project.clips[self.clip_list.row(it)], self.clip_list.viewport().mapToGlobal(pos))

    def _clip_menu(self, c, gpos, t: float | None = None, play=None):
        """The same right-click menu everywhere a moment appears (play = how to play it there)."""
        if c not in self.project.clips:
            return
        r = self.project.clips.index(c)
        if self.clip_list.currentRow() != r:
            self.clip_list.setCurrentRow(r)
        m = QMenu(self)
        m.addAction("▶  Play this moment", play or self.play_clip)
        # --- trim
        tm = m.addMenu("✂  Trim")
        if t is not None:
            pos_in = t - c.offset
            here = fmt_time(self.project.to_stream(t))
            inside = c.start + 1.0 < pos_in < c.end - 1.0
            a = tm.addAction(f"Start here ({here})", lambda: self._trim_clip(c, "start", pos_in))
            a.setEnabled(inside)
            a = tm.addAction(f"End here ({here})", lambda: self._trim_clip(c, "end", pos_in))
            a.setEnabled(inside)
        ph = self._player_pos() if self.player and self._media_offset is not None else None
        ph_in = ph is not None and c.start + 1.0 < ph < c.end - 1.0 and \
            self.player.source() == QUrl.fromLocalFile(c.source)
        a = tm.addAction("Start at the playhead", lambda: self._trim_clip(c, "start", ph))
        a.setEnabled(bool(ph_in))
        a = tm.addAction("End at the playhead", lambda: self._trim_clip(c, "end", ph))
        a.setEnabled(bool(ph_in))
        tm.addSeparator()
        tm.addAction("Cut words by script…", self.edit_script_for_current)
        if t is not None:
            pos_in = t - c.offset
            a = m.addAction(f"⫽  Split into two at {fmt_time(self.project.to_stream(t))}",
                            lambda: self._split_clip(c, pos_in))
            a.setEnabled(c.start + 1.0 < pos_in < c.end - 1.0)
        # --- reset
        rm = m.addMenu("↺  Reset")
        o = c.orig if len(c.orig) == 2 else [c.start, c.end]
        moved = abs(o[0] - c.start) > 0.05 or abs(o[1] - c.end) > 0.05
        a = rm.addAction(f"Timing back to how it was found ({fmt_time(self.project.to_stream(o[0] + c.offset))} – "
                         f"{fmt_time(self.project.to_stream_end(o[1] + c.offset))})", lambda: self._reset_clip(c, "timing"))
        a.setEnabled(moved)
        a = rm.addAction(f"Remove script cuts ({len(c.cuts)})", lambda: self._reset_clip(c, "cuts"))
        a.setEnabled(bool(c.cuts))
        rm.addAction("Effects back to normal", lambda: self._reset_clip(c, "effects"))
        rm.addSeparator()
        rm.addAction("Everything", lambda: self._reset_clip(c, "all"))
        # --- AI: where should the clip(s) this moment overlaps start or end?
        m.addSeparator()
        others = [o for o in self.project.clips if o is not c and o.source == c.source]
        starts_in = [o for o in others if c.start - 0.05 <= o.start <= c.end + 0.05]
        ends_in = [o for o in others if c.start - 0.05 <= o.end <= c.end + 0.05 and o not in starts_in]
        ok = ai.is_configured() and transcribe.available()
        tip = "" if ok else "  (needs an AI model and speech-to-text - Settings → AI models)"
        for o in starts_in:
            a = m.addAction(f"✨  AI: find a good start for “{o.title[:40]}”" + tip,
                            lambda o=o: self.ai_boundary(o, "start", near=c))
            a.setEnabled(ok)
        for o in ends_in:
            a = m.addAction(f"✨  AI: find a good end for “{o.title[:40]}”" + tip,
                            lambda o=o: self.ai_boundary(o, "end", near=c))
            a.setEnabled(ok)
        am = m.addMenu("✨  AI: this moment's start or end")
        am.setEnabled(ok)
        am.addAction("Find a good start", lambda: self.ai_boundary(c, "start"))
        am.addAction("Find a good end", lambda: self.ai_boundary(c, "end"))
        m.addSeparator()
        m.addAction("💾  Save as clip…", lambda: self.save_clip_as(c))
        m.addAction("⧉  Duplicate", self.duplicate_clip)
        m.addAction("👁  Show this moment" if c.hidden else "🙈  Hide this moment",
                    lambda: self._set_hidden(c, not c.hidden))
        m.addAction("☐  Deselect (don't export)" if c.enabled else "☑  Select (export it)",
                    lambda: self.clip_list.item(self.project.clips.index(c)).setCheckState(
                        Qt.Unchecked if c.enabled else Qt.Checked))
        m.addSeparator()
        m.addAction("🗑  Delete this moment", self.remove_clip)
        self._exec_menu(m, gpos)

    # ------------------------------------------------------------------ undo / redo
    def _snap(self):
        """The moments as a compact text key (plus references to the big, unchanging parts)."""
        light, refs = [], []
        for c in self.project.clips:
            d = {f.name: getattr(c, f.name) for f in fields(c)
                 if f.name not in ("words", "cut_suggestions", "overlays")}
            d["overlays"] = [{f.name: getattr(o, f.name) for f in fields(o) if f.name != "candidates"}
                             for o in c.overlays]
            light.append(d)
            refs.append((c.words, c.cut_suggestions, [o.candidates for o in c.overlays]))
        return json.dumps(light, sort_keys=True, default=str), refs

    def _history_tick(self, force: bool = False):
        if self.project is not self._hist_project:          # a new video / project: start a fresh history
            self._hist_project = self.project
            self._undo_stack.clear()
            self._redo_stack.clear()
            key, refs = self._snap()
            self._hist, self._hist_poll = (key, refs, self.clip_list.currentRow()), None
            return
        if not force and QApplication.mouseButtons() != Qt.NoButton:
            return                                       # mid-drag: wait for the drop
        key, refs = self._snap()
        if self._hist is not None and key == self._hist[0]:
            self._hist_poll = None
            return
        if not force and key != self._hist_poll:          # still changing (typing, sliding): wait
            self._hist_poll = key
            return
        if self._hist is not None:
            self._undo_stack.append(self._hist)
            del self._undo_stack[:-200]
        self._redo_stack.clear()
        self._hist, self._hist_poll = (key, refs, self.clip_list.currentRow()), None

    def _history_resync(self):
        """Take what's on screen as the current state without making it an undo step."""
        key, refs = self._snap()
        self._hist, self._hist_poll = (key, refs, self.clip_list.currentRow()), None

    def _script_panel_active(self) -> bool:
        st = getattr(self, "script_tab", None)
        return bool(st is not None and self._script_live())

    def undo(self):
        st = getattr(self, "script_tab", None)
        if self._script_panel_active() and st._undo:     # in the Script tab: undo the last cut first
            st.undo()
            self._history_resync()
            return
        self._history_tick(force=True)
        while self._undo_stack and self._undo_stack[-1][0] == self._hist[0]:
            self._undo_stack.pop()
        if not self._undo_stack:
            self.statusBar().showMessage("Nothing to undo", 2500)
            return
        self._redo_stack.append(self._hist)
        self._restore_state(self._undo_stack.pop())
        self.statusBar().showMessage("↶ Undone  (Ctrl+Shift+Z to redo)", 3000)

    def redo(self):
        st = getattr(self, "script_tab", None)
        if self._script_panel_active() and st._redo:
            st.redo()
            self._history_resync()
            return
        self._history_tick(force=True)
        while self._redo_stack and self._redo_stack[-1][0] == self._hist[0]:
            self._redo_stack.pop()
        if not self._redo_stack:
            self.statusBar().showMessage("Nothing to redo", 2500)
            return
        self._undo_stack.append(self._hist)
        self._restore_state(self._redo_stack.pop())
        self.statusBar().showMessage("↷ Redone", 3000)

    def _restore_state(self, state):
        key, refs, row = state
        clips = []
        for d, (words, sugg, cands) in zip(json.loads(key), refs):
            ovs = []
            for k, o in enumerate(d.pop("overlays", [])):
                ovs.append(editor.ImageOverlay(**o, candidates=cands[k] if k < len(cands) else []))
            clips.append(editor.Clip(**d, overlays=ovs, words=words, cut_suggestions=sugg))
        self.project.clips = clips
        self._hist, self._hist_poll = state, None
        self._fill_clip_list()
        if clips:
            r = max(0, min(row, len(clips) - 1)) if row is not None and row >= 0 else 0
            self.clip_list.setCurrentRow(r)
            self.select_clip(r)
        self._sync_timeline(self.clip_list.currentRow())
        st = getattr(self, "script_tab", None)
        if st is not None and st.isVisible():
            st.refresh()
        if self.tabs.currentIndex() == self.export_index:
            self._fill_export_rows()

    def _set_hidden(self, c, on: bool):
        if c not in self.project.clips:
            return
        c.hidden = on
        r = self.project.clips.index(c)
        self._update_item_text(r)
        self._sync_timeline(self.clip_list.currentRow())
        if self.current_clip() is c:
            self._show_clip_timeline(c, keep_view=True)
        if hasattr(self, "script_tab"):
            self.script_tab._sync_views(keep_view=True)
        self._show_toast("🙈 Hidden - right-click it again to show it" if on else "👁 Shown again")

    def ai_boundary(self, c, edge: str, near=None):
        """Ask the AI where clip c should start (or end), reading what's said around that edge.
        near = the moment that was right-clicked (it marks roughly where the edge should be)."""
        if c not in self.project.clips:
            return
        cur = c.start if edge == "start" else c.end
        if near is not None:     # look around the right-clicked moment as well as the clip's own edge
            lo, hi = min(cur, near.start) - 25, max(cur, near.end) + 10
        else:
            lo, hi = cur - 25, cur + 12
        if edge == "end":
            lo, hi = (min(cur, near.start) - 10, max(cur, near.end) + 25) if near is not None else (cur - 12, cur + 25)
        dur = self.info.duration if self.info else hi
        lo, hi = max(0.0, lo), min(dur, hi)
        if edge == "start":
            hi = min(hi, c.end - 1.0)
        else:
            lo = max(lo, c.start + 1.0)
        about = c.title + ((" - " + "; ".join(c.reasons[:2])) if c.reasons else "")

        def job(on_progress=None, cancel=None):
            on_progress(0.05, f"Listening around the {edge} of “{c.title}”…")
            segs = transcribe.transcribe(c.source, start=lo, end=hi, cancel=cancel,
                                         on_progress=lambda f, m: on_progress(0.05 + 0.75 * max(f, 0), m))
            lines = [(a, b, t) for a, b, t in segs if t.strip() and lo - 1 <= a <= hi]
            on_progress(0.85, "Asking the AI where it should " + edge + "…")
            return ai.pick_boundary(lines, edge, cur, about)

        w = Worker(job)
        w.progress.connect(lambda f, m: self.statusBar().showMessage(m))

        def done(r):
            if c not in self.project.clips:
                return
            t = round(float(r["t"]), 2)
            if edge == "start":
                t = min(t, c.end - 1.0)
            else:
                t = max(t, c.start + 1.0)
            old = c.start if edge == "start" else c.end
            setattr(c, edge, t)
            note = f"AI moved the {edge} to {fmt_time(self.project.to_stream(t + c.offset))}" + \
                (f": {r['why']}" if r.get("why") else "")
            c.timing = [x for x in c.timing if not x.startswith(f"AI moved the {edge}")] + [note]
            self._clip_edited(c)
            row = self.project.clips.index(c)
            if self.clip_list.currentRow() == row:
                self.select_clip(row)
            else:
                self._sync_timeline(self.clip_list.currentRow())
            if hasattr(self, "script_tab"):
                self.script_tab.refresh()
            moved = t - old
            self.statusBar().showMessage(note, 8000)
            self._show_toast(f"✨ “{c.title[:40]}” now {'starts' if edge == 'start' else 'ends'} "
                             f"{abs(moved):.1f}s {'earlier' if moved < 0 else 'later'}" if abs(moved) > 0.05
                             else f"✨ The AI kept the {edge} of “{c.title[:40]}” where it was")
        w.done.connect(done)
        w.failed.connect(lambda m: QMessageBox.warning(self, "AI start / end", m))
        self._start(w)

    def _reset_clip(self, c, what: str):
        if c not in self.project.clips:
            return
        if what in ("timing", "all") and len(c.orig) == 2:
            c.start, c.end = float(c.orig[0]), float(c.orig[1])
        if what in ("cuts", "all"):
            from .. import script_edit as se
            se.apply_cuts(c, [])
            c.cut_suggestions = []
        if what in ("effects", "all"):
            fresh = editor.Clip(source=c.source, start=c.start, end=c.end)
            for f_ in editor.Clip.EFFECT_FIELDS:
                setattr(c, f_, getattr(fresh, f_))
        r = self.project.clips.index(c)
        self.select_clip(r)
        self._update_item_text(r)
        self._show_toast({"timing": "↺ Timing reset", "cuts": "↺ Script cuts removed",
                          "effects": "↺ Effects reset", "all": "↺ Moment reset"}[what])

    def save_clip_as(self, c):
        """💾 Export one moment on its own, with its own export options."""
        if c not in self.project.clips:
            return
        i = self.project.clips.index(c) + 1
        default = Path(self.e_out.text().strip() or str(config.export_dir())) / \
            ft.safe_filename(self.project.title or "RumbleCut", 60) / f"{editor.clip_filename(c, i)}.mp4"
        path, _ = QFileDialog.getSaveFileName(self, "Save clip as", str(default), "MP4 video (*.mp4)")
        if not path:
            return
        if not path.lower().endswith(".mp4"):
            path += ".mp4"
        self._collect_export_settings()
        w = Worker(editor.export_clip, self.project, c, path)
        w.progress.connect(lambda f, msg: self._show_toast(f"💾 {msg} {f * 100:.0f}%") if int(f * 100) % 20 == 0 else None)

        def done(out):
            self._show_toast(f"💾 Saved “{Path(out).name}”")
            self._add_exported([out])
        w.done.connect(done)
        w.failed.connect(lambda msg: QMessageBox.warning(self, "Save clip", msg))
        self._show_toast(f"💾 Saving “{c.title}”…")
        self._start(w)

    def _exec_menu(self, m, gpos):
        m.exec(gpos)

    def _trim_clip(self, c, which: str, t: float):
        if c not in self.project.clips:
            return
        r = self.project.clips.index(c)
        self.clip_list.setCurrentRow(r)
        if which == "start":
            c.start = round(t, 2)
        else:
            c.end = round(t, 2)
        self.select_clip(r)                          # refresh boxes + moment timeline
        self._update_item_text(r)
        self._show_toast("✂ Trimmed")

    def _split_clip(self, c, t: float):
        if c not in self.project.clips:
            return
        from dataclasses import replace
        r = self.project.clips.index(c)
        second = replace(c, start=round(t, 2), title=c.title + " (2)", overlays=[replace(o) for o in c.overlays],
                         reasons=list(c.reasons), why=list(c.why), timing=list(c.timing))
        c.end = round(t, 2)
        self.project.clips.insert(r + 1, second)
        self._fill_clip_list()
        self.clip_list.setCurrentRow(r)
        self._show_toast("⫽ Split into two moments")

    def _clip_limits(self, c):
        if c.source == self.project.source:
            return (0.0, self.info.duration if self.info else 1e9)
        return (c.offset, c.offset + (self._src_duration(c.source) or 1e9))

    def _timeline_clip_moved(self, i: int, a: float, b: float, what: str):
        if not (0 <= i < len(self.project.clips)):
            return
        if i != self.clip_list.currentRow():
            self.clip_list.setCurrentRow(i)
        self._clip_span_dragged(a, b, what)
        c = self.project.clips[i]
        self._show_clip_timeline(c, keep_view=True)

    def _timeline_clip_done(self, i: int, a: float, b: float, what: str):
        if 0 <= i < len(self.project.clips) and i == self.clip_list.currentRow():
            self._clip_span_committed(a, b, what)
            self._show_clip_timeline(self.project.clips[i], keep_view=False)

    def add_clip_here(self):
        if not self.project.source:
            return
        t = self._player_pos() + (self._media_offset or 0.0)     # position on the project timeline
        dur = self.info.duration if self.info else t + 30
        L = (self.min_len.value() + self.max_len.value()) / 2
        s = max(0.0, t - L * 0.3)
        c = editor.Clip(source=self.project.source, start=round(s, 1), end=round(min(dur, s + L), 1),
                        title=f"My clip - {fmt_time(self.project.to_stream(t)).replace(':', '.')}",
                        reasons=["Added by you"], origin="manual")
        r = self._user_insert_row()
        self.project.clips.insert(r, c)
        self._fill_clip_list()
        self.clip_list.setCurrentRow(r)

    def duplicate_clip(self):
        c = self.current_clip()
        if not c:
            return
        from dataclasses import replace
        d = replace(c, title=c.title + " (copy)", overlays=[replace(o) for o in c.overlays],
                    reasons=list(c.reasons), origin="manual")
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
            self.i_ovl.addItem(self._ov_text(o))
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
            self.i_ovl.item(r).setText(self._ov_text(o))

    @staticmethod
    def _ov_text(o) -> str:
        name = o.label or Path(o.image).name
        look = "full screen" if o.style == "full" else o.position
        state = "⏳ " if o.status == "pending" else ""
        return f"{state}{name}  @ {o.start:.1f}s  ({look})"

    def add_overlay(self):
        c = self.current_clip()
        if not c:
            return
        p, _ = QFileDialog.getOpenFileName(self, "Choose image", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not p:
            return
        rel = max(0.0, min(c.length - 1, self._pos_in_clip(c) - c.start)) / max(0.25, c.speed)
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
        """Position in the currently loaded media file."""
        if self.player:
            return self.player.position() / 1000
        return self.timeline.playhead - (self._media_offset or 0.0)

    def _pos_in_clip(self, c) -> float:
        """Current position expressed in clip `c`'s own file time."""
        return self._player_pos() + (self._media_offset or 0.0) - c.offset

    def _load_media(self, path: str, offset: float | None, user: bool = False):
        self._media_offset = offset
        if self.player and path and self.player.source() != QUrl.fromLocalFile(path):
            playing = self.player.playbackState() == QMediaPlayer.PlayingState
            self.player.setSource(QUrl.fromLocalFile(path))
            if playing:
                self.player.play()
        self._update_no_video(path, user)

    def _update_no_video(self, path: str | None = None, user: bool = False):
        """Show the picture, or an icon + note when there's nothing to show."""
        if path is None:
            path = self.player.source().toLocalFile() if self.player else self.project.source
        if not path or not os.path.exists(path):
            self.no_video.setText("<p style='font-size:40px; margin:0'>🎬</p><p style='margin:4px'><b>Nothing loaded yet</b></p>"
                                  "<span style='color:#9ca3af'>Open a video, or scan a stream's audio "
                                  "on ① Get video.</span>")
            show = False
        elif not self._has_video(path):
            self.no_video.setText("<p style='font-size:40px; margin:0'>🔊</p><p style='margin:4px'><b>Audio only here</b></p>"
                                  "<span style='color:#9ca3af'>This part was scanned as audio, so there's no "
                                  "picture to show - you'll still hear it.<br>Tick the moment and press "
                                  "⬇ Get video to see it.</span>")
            show = False
        else:
            show = True
        if user and self._showing_video and not show:
            self._show_toast("🔊 No video for this part - playing the audio")
        self._showing_video = show
        self.video_stack.setCurrentIndex(0 if show else 1)

    def _src_duration(self, path: str) -> float:
        cache = self.__dict__.setdefault("_dur_cache", {})
        if path not in cache:
            try:
                cache[path] = ft.MediaInfo(path).duration
            except Exception:
                cache[path] = 0.0
        return cache[path]

    def _fetched_video(self):
        """(clip, start, end) on the project timeline for moments whose video was fetched separately."""
        out = []
        for c in self.project.clips:
            if c.source and c.source != self.project.source and os.path.exists(c.source) and self._has_video(c.source):
                out.append((c, c.offset, c.offset + self._src_duration(c.source)))
        return out

    def _refresh_scrubber(self):
        dur = self.info.duration if (self.info and self.project.source) else 0.0
        self.scrubber.set_duration(dur)
        if dur and self._has_video(self.project.source):
            spans = [(0.0, dur)]
        else:
            spans = sorted((a, b) for _, a, b in self._fetched_video())
        self.scrubber.set_video_spans(spans)

    def _show_pos(self, t: float):
        """t = position on the project timeline."""
        if self._script_live():
            self.script_tab.follow(t)
        total = self.project.to_stream(self.info.duration) if (self.info and self.project.source) else 0
        self.pos_label.setText(fmt_time(self.project.to_stream(t)) + (f" / {fmt_time(total)}" if total else ""))
        self.timeline.set_playhead(t)
        self.scrubber.set_position(t)
        self.clip_tl.set_playhead(t)
        # light up the moment that's playing
        playing = bool(self.player) and self.player.playbackState() == QMediaPlayer.PlayingState
        idx = -1
        if playing:
            sel = self.clip_list.currentRow()
            spans = self._spans()
            if 0 <= sel < len(spans) and spans[sel][0] <= t <= spans[sel][1]:
                idx = sel
            else:
                idx = next((i for i, (a, b) in enumerate(spans) if a <= t <= b), -1)
        self.timeline.set_playing(idx)
        c = self.current_clip()
        self.clip_tl.set_playing(playing and c is not None and c.start + c.offset <= t <= c.end + c.offset)

    def scrub_to(self, t: float):
        """Jump to project time t - shows a fetched video for that part if there is one."""
        if not self.project.source:
            return
        self._clip_stop_at = None
        for c, a, b in self._fetched_video():
            if a <= t <= b:
                self._show_pos(t)
                if self.player:
                    self._load_media(c.source, c.offset, user=True)
                    self.player.setPosition(int((t - c.offset) * 1000))
                return
        self.seek_to(t, user=True)

    def seek_to(self, t: float, user: bool = False):
        """Seek on the project timeline (the stream, or its audio)."""
        self._show_pos(t)
        if self.player and self.project.source:
            self._load_media(self.project.source, 0.0, user=user)
            self.player.setPosition(int(t * 1000))

    def seek_clip(self, c, t: float):
        """Seek to time `t` inside clip c's own file (its downloaded video, or the stream)."""
        self._show_pos(t + c.offset)
        if self.player:
            self._load_media(c.source, c.offset)
            self.player.setPosition(int(t * 1000))

    def _studio_state(self, state):
        playing = state == QMediaPlayer.PlayingState
        self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPause if playing else QStyle.SP_MediaPlay))
        if hasattr(self, "script_tab"):
            self.script_tab.btn_play.setText("■ Stop" if playing else "▶ Play the edit")

    def show_publish(self):
        """Go to Export & publish → Post online."""
        self.tabs.setCurrentIndex(self.export_index)
        self.out_tabs.setCurrentIndex(1)

    def pause_studio(self):
        if self.player and self.player.playbackState() == QMediaPlayer.PlayingState:
            self._clip_stop_at = None
            self.player.pause()

    def skip_to_edge(self, edge: str):
        """⏮ / ⏭: put the playhead on the start or end of the selected moment. If the moment is playing,
        ⏮ replays it from the top; ⏭ goes to its last moment and stops there."""
        c = self.current_clip()
        if c is None:                                   # nothing selected: start / end of the video
            if self.info:
                self.seek_to(0.0 if edge == "start" else max(0.0, self.info.duration - 0.05))
            return
        playing = bool(self.player) and self.player.playbackState() == QMediaPlayer.PlayingState
        if edge == "start":
            t = c.kept[0][0] if c.kept else c.start          # skip a cut at the very start
            self.seek_clip(c, t)
            if playing:
                self._clip_stop_at = c.end
        else:
            self.seek_clip(c, max(c.start, c.end - 0.05))
            if playing and self.player:
                self._clip_stop_at = None
                self.player.pause()
                self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

    def nudge(self, dt):
        if self.player:
            self.player.setPosition(int(max(0.0, self._player_pos() + dt) * 1000))
        else:
            self.seek_to(max(0, self.timeline.playhead + dt))

    def toggle_play(self):
        if not self.player:
            return
        if self._script_live() and self.player.playbackState() != QMediaPlayer.PlayingState:
            self.script_tab.play_toggle()        # Space beside the script plays the edit (skipping cuts)
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
        self.seek_clip(c, c.start)
        self._clip_stop_at = c.end
        self.player.play()
        self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))

    def _pos_changed(self, ms):
        t = ms / 1000
        if self._clip_stop_at is not None and self._media_offset is not None:   # playing a clip: skip its cuts
            c = self.current_clip()
            if c is not None and c.cuts and self.player.source() == QUrl.fromLocalFile(c.source):
                for a, b in c.cuts:
                    if a <= t < b - 0.02:
                        self.player.setPosition(int(b * 1000))
                        return
        if self._media_offset is not None:          # None while an effects preview is playing
            self._show_pos(t + self._media_offset)
        else:
            self.pos_label.setText(fmt_time(t))
        if self._clip_stop_at is not None and t >= self._clip_stop_at:
            self._clip_stop_at = None
            self.player.pause()
            self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

    def _set_edge(self, which):
        c = self.current_clip()
        if not c:
            return
        t = round(max(0.0, self._pos_in_clip(c)), 1)
        (self.i_start if which == "start" else self.i_end).setValue(t + c.offset)

    def _update_audio_banner(self):
        audio_src = bool(self.project.source) and not self._has_video(self.project.source)
        pending = [c for c in self.project.clips if c.enabled and not self._has_video(c.source)]
        self.audio_banner.setVisible(audio_src or bool(pending))
        self.btn_get_video.setEnabled(bool(pending))
        self.btn_get_video.setText(f"⬇ Get video for {len(pending)} ticked moment{'s' if len(pending) != 1 else ''}"
                                   if pending else "⬇ Get video for ticked moments")

    def get_video_for_ticked(self):
        targets = [c for c in self.project.clips if c.enabled and not self._has_video(c.source)]
        if not targets:
            QMessageBox.information(self, APP_NAME, "Tick the moments you want first.")
            return
        url = self.project.url or self.url_edit.text().strip()
        if not url:
            from PySide6.QtWidgets import QInputDialog
            url, ok = QInputDialog.getText(self, "Rumble link", "Paste the Rumble link of this stream:")
            if not ok or not url.strip():
                return
            url = url.strip()
            self.project.url = url
        pad = 3.0
        dur = 0 if self.project.segments else (self.info.duration if self.info else 0) + self.project.offset
        spans = sorted((max(0.0, self.project.to_stream(c.start + c.offset) - pad),
                        self.project.to_stream_end(c.end + c.offset) + pad) for c in targets)
        ranges: list[tuple[float, float | None]] = []
        for a, b in spans:                       # merge overlapping moments into one download
            if dur:
                b = min(b, dur)
            if ranges and a <= ranges[-1][1]:
                ranges[-1] = (ranges[-1][0], max(ranges[-1][1], b))
            else:
                ranges.append((a, b))
        quality = self.quality.currentText()
        if quality == "Audio only":
            quality = "1080p"
        self.btn_get_video.setEnabled(False)
        self.an_status.setText(f"Fetching video for {len(targets)} moment(s)…")
        w = Worker(downloader.download, url, quality, config.download_dir(), ranges)
        self._cancel_shown(self.btn_an_cancel, True)
        self.btn_an_cancel.clicked.connect(lambda: self._cancelling(w, self.btn_an_cancel, self.an_status))

        def prog(f, m):
            if f < 0:
                self.an_progress.setRange(0, 0)
            else:
                self.an_progress.setRange(0, 1000)
                self.an_progress.setValue(int(f * 1000))
            self.an_status.setText("Fetching video: " + m)

        def done(res: downloader.DownloadResult):
            got = 0
            for c in targets:
                pt_start = c.start + c.offset                       # position on the project timeline
                a = self.project.to_stream(pt_start)
                b = self.project.to_stream_end(c.end + c.offset)
                sec = next((x for x in res.sections if x.start <= a + 0.01 and (x.end is None or x.end >= b - 0.6)),
                           None)
                if not sec:
                    continue
                c.source = sec.path
                c.start, c.end = round(a - sec.start, 2), round(b - sec.start, 2)
                c.offset = pt_start - c.start                      # keeps it at the same place on the timeline
                got += 1
            mb = sum(Path(x.path).stat().st_size for x in res.sections if Path(x.path).exists()) / 1e6
            self._fill_clip_list(keep_row=True)
            self.select_clip(self.clip_list.currentRow())
            self._make_thumbnails()
            self._update_audio_banner()
            self.an_status.setText(f"🎬 Got video for {got} moment{'s' if got != 1 else ''} ({mb:.0f} MB) - "
                                   "edit and export them as usual")
            if got < len(targets):
                QMessageBox.warning(self, "Some moments are missing",
                                    f"Video for {len(targets) - got} moment(s) couldn't be matched. Try again.")

        def reset():
            self.an_progress.setRange(0, 1000)
            self._cancel_shown(self.btn_an_cancel, False)
            try:
                self.btn_an_cancel.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass
            self._update_audio_banner()
        w.progress.connect(prog)
        w.done.connect(done)
        w.failed.connect(lambda m: QMessageBox.warning(self, "Couldn't get the video", m))
        w.cancelled.connect(lambda: self.an_status.setText("Cancelled"))
        w.finished.connect(reset)
        self._start(w)

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
                self._load_media(str(path), None)
                self.player.play()
                self.statusBar().showMessage("Playing effects preview - click the timeline or "
                                             "a clip to go back to the stream", 8000)
        w.done.connect(show)
        w.failed.connect(lambda m: QMessageBox.warning(self, "Preview failed", m))
        w.finished.connect(lambda: (self.btn_preview.setEnabled(True), self.btn_preview.setText("👁 Preview effects")))
        self._start(w)

    # ------------------------------------------------------------------ tab 3: export
    def _build_export_tab(self) -> QWidget:
        w = QWidget()
        outer = QHBoxLayout(w)
        outer.setContentsMargins(24, 16, 24, 16)
        outer.setSpacing(18)

        # ---- left: every clip with its own options
        left = QVBoxLayout()
        hrow = QHBoxLayout()
        hl = QLabel("Each clip can have its own name, shape and quality - “Same as all” uses the defaults on "
                    "the right. Export one with 💾, or tick several and export them together.")
        hl.setObjectName("muted")
        hl.setWordWrap(True)
        hrow.addWidget(hl, 1)
        b_all = QPushButton("Select all")
        b_all.clicked.connect(lambda: self._tick_all_export(True))
        b_none = QPushButton("Deselect all")
        b_none.clicked.connect(lambda: self._tick_all_export(False))
        hrow.addWidget(b_all)
        hrow.addWidget(b_none)
        left.addLayout(hrow)
        self.ex_rows_box = QVBoxLayout()
        self.ex_rows_box.setSpacing(6)
        self.ex_rows_box.addStretch()
        rows_w = QWidget()
        rows_w.setLayout(self.ex_rows_box)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(rows_w)
        left.addWidget(sc, 1)
        self.ex_rows: list[dict] = []
        outer.addLayout(left, 5)

        # ---- right: defaults, group export, results
        right = QVBoxLayout()
        g = QGroupBox("Defaults for all clips")
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
        hint = QLabel("Vertical 9:16 suits Shorts, TikTok and Reels. Resolution is the short side for "
                      "vertical and square.")
        hint.setWordWrap(True); hint.setObjectName("muted")
        fl.addRow("", hint)
        right.addWidget(g)

        g2 = QGroupBox("Export the ticked clips")
        fl2 = QFormLayout(g2)
        self.e_indiv = QCheckBox("Each as its own video"); self.e_indiv.setChecked(True)
        self.e_thumbs = QCheckBox("A thumbnail image with each"); self.e_thumbs.setChecked(True)
        self.e_comp = QCheckBox("Joined into one ‘Best moments’ video")
        self.e_trans = QComboBox(); self.e_trans.addItems([k for k in editor.TRANSITIONS])
        self.e_trans.setCurrentText("Cross-fade")
        self.e_tdur = QDoubleSpinBox(); self.e_tdur.setRange(0.1, 3); self.e_tdur.setSingleStep(0.1)
        self.e_tdur.setValue(0.6); self.e_tdur.setSuffix(" s")
        fl2.addRow(self.e_indiv)
        fl2.addRow(self.e_thumbs)
        fl2.addRow(self.e_comp)
        fl2.addRow("Transition", self.e_trans)
        fl2.addRow("Transition length", self.e_tdur)
        orow = QHBoxLayout()
        self.e_out = QLineEdit(str(config.export_dir()))
        b = QPushButton("Browse…")
        b.clicked.connect(lambda: (d := QFileDialog.getExistingDirectory(self, "Export folder", self.e_out.text()))
                          and self.e_out.setText(d))
        orow.addWidget(self.e_out, 1); orow.addWidget(b)
        fl2.addRow("Save to", orow)
        right.addWidget(g2)
        brow = QHBoxLayout()
        self.btn_export = QPushButton("Export ticked clips")
        self.btn_export.setObjectName("primary")
        self.btn_export.setMinimumHeight(40)
        self.btn_export.clicked.connect(self.run_export)
        self.btn_ex_cancel = QPushButton("Cancel"); self.btn_ex_cancel.setEnabled(False)
        brow.addWidget(self.btn_export, 1); brow.addWidget(self.btn_ex_cancel)
        right.addLayout(brow)
        self.ex_progress = QProgressBar(); self.ex_progress.setRange(0, 1000)
        self.ex_status = QLabel("")
        self.ex_status.setWordWrap(True)
        right.addWidget(self.ex_progress)
        right.addWidget(self.ex_status)
        right.addWidget(section("Exported files"))
        self.ex_files = QListWidget()
        self.ex_files.itemDoubleClicked.connect(lambda it: QDesktopServices.openUrl(
            QUrl.fromLocalFile(it.data(Qt.UserRole))))
        self.ex_files.setToolTip("Double-click to play")
        right.addWidget(self.ex_files, 1)
        self.btn_open_out = QPushButton("Open export folder")
        self.btn_open_out.clicked.connect(lambda: open_folder(getattr(self, "_last_out", self.e_out.text())))
        right.addWidget(self.btn_open_out)
        outer.addLayout(right, 3)
        return w

    # ---- export rows
    def _fill_export_rows(self):
        for r in self.ex_rows:
            r["w"].deleteLater()
        self.ex_rows = []
        thumbs = getattr(self, "_thumbs", {})
        if not self.project.clips:
            lab = QLabel("No clips yet - find highlights or add clips in the Clip studio.")
            lab.setObjectName("muted")
            self.ex_rows_box.insertWidget(0, lab)
            self.ex_rows.append({"w": lab})
            return
        for i, c in enumerate(self.project.clips):
            row = QFrame()
            row.setObjectName("exrow")
            g = QGridLayout(row)
            g.setContentsMargins(8, 6, 8, 6)
            g.setHorizontalSpacing(8)
            tick = QCheckBox()
            tick.setChecked(c.enabled)
            tick.setToolTip("Export this clip with the others")
            th = QLabel()
            th.setFixedSize(96, 54)
            tp = thumbs.get((c.source, round((c.start + c.end) / 2, 1)))
            pm = QPixmap(tp) if tp else None
            if pm is not None and not pm.isNull():
                th.setPixmap(pm.scaled(96, 54, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                th.setText("🎬" if self._has_video(c.source) else "🔊")
                th.setAlignment(Qt.AlignCenter)
            name = QLineEdit((c.export_opts or {}).get("name", ""))
            name.setPlaceholderText(c.title or f"Highlight {i + 1}")
            name.setToolTip("File name (leave empty to use the clip's title)")
            a = self.project.to_stream(c.start + c.offset)
            info = QLabel(f"#{i + 1} · {fmt_time(a)} · {c.out_length:.0f}s" + (" · ✂" if c.cuts else "")
                          + (" · 🖼" if any(o.status == 'approved' for o in c.overlays) else "")
                          + ("" if self._has_video(c.source) else " · 🔊 audio only"))
            info.setObjectName("muted")
            shape = QComboBox()
            shape.addItem("Shape: same as all", "")
            for k in editor.ASPECTS:
                shape.addItem(k, k)
            shape.setCurrentIndex(max(0, shape.findData((c.export_opts or {}).get("aspect", ""))))
            qual = QComboBox()
            qual.addItem("Quality: same as all", "")
            for k in ("high", "balanced", "small"):
                qual.addItem(k, k)
            qual.setCurrentIndex(max(0, qual.findData((c.export_opts or {}).get("quality", ""))))
            b_exp = QPushButton("💾 Export")
            b_exp.setToolTip("Export just this clip now, with its own options")
            b_edit = QPushButton("✏")
            b_edit.setToolTip("Open it in the Clip studio")
            b_edit.setFixedWidth(36)
            status = QLabel("")
            status.setObjectName("muted")
            g.addWidget(tick, 0, 0, 2, 1)
            g.addWidget(th, 0, 1, 2, 1)
            g.addWidget(name, 0, 2, 1, 2)
            g.addWidget(b_exp, 0, 4)
            g.addWidget(b_edit, 0, 5)
            g.addWidget(info, 1, 2)
            g.addWidget(shape, 1, 3)
            g.addWidget(qual, 1, 4, 1, 2)
            g.addWidget(status, 2, 2, 1, 4)
            status.hide()
            g.setColumnStretch(2, 3)
            g.setColumnStretch(3, 2)

            def opt(key, val, c=c):
                o = dict(c.export_opts or {})
                if val:
                    o[key] = val
                else:
                    o.pop(key, None)
                c.export_opts = o
            name.textEdited.connect(lambda t, opt=opt: opt("name", t.strip()))
            shape.currentIndexChanged.connect(lambda _, cb=shape, opt=opt: opt("aspect", cb.currentData()))
            qual.currentIndexChanged.connect(lambda _, cb=qual, opt=opt: opt("quality", cb.currentData()))
            tick.toggled.connect(lambda on, c=c: self._set_enabled(c, on))
            b_exp.clicked.connect(lambda _=False, c=c: self._export_one(c))
            b_edit.clicked.connect(lambda _=False, c=c: (self.tabs.setCurrentIndex(1),
                                                         self.clip_list.setCurrentRow(self.project.clips.index(c))))
            self.ex_rows_box.insertWidget(self.ex_rows_box.count() - 1, row)
            self.ex_rows.append({"w": row, "clip": c, "tick": tick, "status": status, "button": b_exp})
        n = sum(1 for c in self.project.clips if c.enabled)
        self.btn_export.setText(f"Export {n} ticked clip{'s' if n != 1 else ''}")

    def _set_enabled(self, c, on: bool):
        if c in self.project.clips:
            r = self.project.clips.index(c)
            it = self.clip_list.item(r)
            if it is not None and (it.checkState() == Qt.Checked) != on:
                it.setCheckState(Qt.Checked if on else Qt.Unchecked)
            c.enabled = on
        n = sum(1 for x in self.project.clips if x.enabled)
        self.btn_export.setText(f"Export {n} ticked clip{'s' if n != 1 else ''}")

    def _tick_all_export(self, on: bool):
        for r in self.ex_rows:
            if "tick" in r:
                r["tick"].setChecked(on)

    def _export_one(self, c):
        row = next((r for r in self.ex_rows if r.get("clip") is c), None)
        self._collect_export_settings()
        i = self.project.clips.index(c) + 1
        out_dir = Path(self.e_out.text().strip() or str(config.export_dir())) / \
            ft.safe_filename(self.project.title or Path(self.project.source).stem or "RumbleCut", 60)
        out = out_dir / f"{editor.clip_filename(c, i)}.mp4"
        w = Worker(editor.export_clip, self.project, c, out, self.e_thumbs.isChecked())
        if row:
            row["button"].setEnabled(False)
            row["status"].show()
            row["status"].setText("Exporting…")
            w.progress.connect(lambda f, m, st=row["status"]: st.setText(f"Exporting… {f * 100:.0f}%"))

        def done(path):
            if row:
                row["status"].setText(f"✓ Saved {Path(path).name}")
            self._add_exported([path])

        def failed(msg):
            if row:
                row["status"].setText("⚠ " + msg[:120])
            QMessageBox.warning(self, "Export failed", msg)
        w.done.connect(done)
        w.failed.connect(failed)
        if row:
            w.finished.connect(lambda b=row["button"]: b.setEnabled(True))
        self._start(w)

    def _add_exported(self, files):
        for f in files:
            if any(self.ex_files.item(k).data(Qt.UserRole) == f for k in range(self.ex_files.count())):
                continue
            it = QListWidgetItem(Path(f).name)
            it.setData(Qt.UserRole, f)
            self.ex_files.insertItem(0, it)
        if files:
            self._last_out = str(Path(files[0]).parent)

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
        audio_only = [c for c in self.project.clips if c.enabled and not self._has_video(c.source)]
        if audio_only and self.project.url:
            ans = QMessageBox.question(
                self, "Some clips have no video yet",
                f"{len(audio_only)} ticked clip(s) only have audio so far.\n\nGet their video first "
                "(recommended)? Choose No to export them over a black screen.",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if ans == QMessageBox.Cancel:
                return
            if ans == QMessageBox.Yes:
                self.tabs.setCurrentIndex(1)
                self.get_video_for_ticked()
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
            self._add_exported(files)
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
    # ---- publishing accounts
    def _pub_status(self, plat):
        from .. import publish as pb
        row = self.pub_rows[plat]
        if pb.is_connected(plat):
            row["status"].setText(f"✔ Connected as {pb.account_name(plat)}")
            row["connect"].setText("Reconnect")
            row["disconnect"].setEnabled(True)
        else:
            cid, _ = pb.get_credentials(plat)
            row["status"].setText("Keys saved - press Connect to sign in" if cid else "Not set up yet")
            row["connect"].setText("Connect")
            row["disconnect"].setEnabled(False)
        if hasattr(self, "pub_tab"):
            for c in self.pub_tab.cards.values():
                c.refresh_status()

    def _pub_steps(self, plat):
        from .. import publish as pb
        info = pb.SETUP[plat]
        box = QMessageBox(self)
        box.setWindowTitle(f"Set up {pb.PLATFORMS[plat]}")
        box.setTextFormat(Qt.RichText)
        box.setText(f"<b>{pb.PLATFORMS[plat]} - one-time setup</b><ol>"
                    + "".join(f"<li style='margin-bottom:6px'>{st}</li>" for st in info["steps"]) + "</ol>")
        open_btn = box.addButton("Open the developer site", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl(info["link"]))

    def _pub_connect(self, plat):
        from .. import publish as pb
        row = self.pub_rows[plat]
        cid = row["id"].text().strip()
        sec = row["secret"].text().strip() or pb.get_credentials(plat)[1]
        if not cid:
            QMessageBox.information(self, "Publishing accounts",
                                    f"Paste your {pb.SETUP[plat]['id_label']} first (see “Setup steps”).")
            return
        pb.set_credentials(plat, cid, sec)
        row["secret"].clear()
        row["secret"].setPlaceholderText("•••••••• saved" if sec else pb.SETUP[plat]["secret_label"])
        row["status"].setText("Waiting for you to sign in in your browser…")
        row["connect"].setEnabled(False)
        w = Worker(lambda on_progress=None, cancel=None: pb.connect(plat, cancel=cancel))

        def done(name):
            self._show_toast(f"✔ {pb.PLATFORMS[plat]} connected as {name}")

        def failed(msg):
            QMessageBox.warning(self, "Publishing accounts", f"{pb.PLATFORMS[plat]}: {msg}")
        w.done.connect(done)
        w.failed.connect(failed)
        w.finished.connect(lambda: (row["connect"].setEnabled(True), self._pub_status(plat)))
        self._start(w)

    def _tl_scrolled(self, v: int):
        a, b = self.timeline.view
        span = b - a
        self.timeline.set_view(v / 1000, v / 1000 + span)

    def _side_tab_changed(self, i: int):
        """🎛 Clip / ✂ Script / 🖼 Pictures beside the player. Script and Pictures get more room."""
        sp = self.studio_split
        sizes = sp.sizes()
        total = sum(sizes) or 1
        if i in (1, 2):
            if sizes[2] < total * 0.4:
                self._narrow_sizes = sizes
                left = min(sizes[0], 300)
                sp.setSizes([left, int((total - left) * 0.42), total - left - int((total - left) * 0.42)])
        elif getattr(self, "_narrow_sizes", None):
            sp.setSizes(self._narrow_sizes)
            self._narrow_sizes = None
        if self.tabs.currentIndex() != 1:
            return
        if i == 1:
            self.script_tab.refresh()
        elif i == 2:
            self.ctx_tab.refresh()
            c = self.current_clip()
            if c is not None:
                self.ctx_tab.select_clip(c)

    def show_script(self):
        self.tabs.setCurrentIndex(1)
        self.side_tabs.setCurrentIndex(1)

    def _script_live(self) -> bool:
        return (hasattr(self, "side_tabs") and self.tabs.currentIndex() == 1
                and self.side_tabs.currentIndex() == 1)

    def _unused_build_context_tab(self) -> QWidget:
        """🎬 Edit clips: ✂ Script (cut words out by editing the text) and 🖼 Pictures."""
        from .context_tab import ContextImagesTab
        from .script_tab import ScriptPanel
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(16, 10, 16, 12)
        self.edit_tabs = QTabWidget()
        self.edit_tabs.setObjectName("lefttabs")
        self.script_tab = ScriptPanel(self)
        self.ctx_tab = ContextImagesTab(self)
        self.edit_tabs.addTab(self.script_tab, "✂  Script")
        self.edit_tabs.addTab(self.ctx_tab, "🖼  Pictures")
        self.edit_tabs.currentChanged.connect(lambda i: (self.script_tab if i == 0 else self.ctx_tab).refresh())
        v.addWidget(self.edit_tabs)
        return page

    def _clip_edited(self, c):
        """A clip's script cuts changed in 🎬 Edit clips: refresh its length everywhere."""
        if c not in self.project.clips:
            return
        r = self.project.clips.index(c)
        if self.clip_list.item(r):
            self.clip_list.item(r).setText(self._item_text(r, c))
        if c is self.current_clip():
            self.i_len.setText(f"{c.length:.1f}s  →  {c.out_length:.1f}s exported" +
                               (f"  (✂ {len(c.cuts)} cut{'s' if len(c.cuts) != 1 else ''})" if c.cuts else ""))
            self._show_clip_timeline(c, keep_view=True)
            self._fill_overlays()

    def edit_script_for_current(self):
        c = self.current_clip()
        if not c:
            return
        self.show_script()

    def _ai_log_line(self, line: str):
        self._log(line)

    def _overlays_changed(self):
        """A picture was added, approved, swapped or removed in the Context images tab."""
        c = self.current_clip()
        if c is not None:
            self._fill_overlays()

    def find_pictures_for_current(self):
        c = self.current_clip()
        if not c:
            return
        self.tabs.setCurrentIndex(1)
        self.side_tabs.setCurrentIndex(2)
        self.ctx_tab.select_clip(c)
        if not any(o.status == "pending" for o in c.overlays):
            self.ctx_tab.find(all_ticked=False)

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
        self._use_project(proj, path)

    # ---- the session: kept between runs and across updates
    @staticmethod
    def _session_file() -> Path:
        return config.app_data_dir() / "last session.rcproj"

    def _pack_analysis(self):
        """The scan (timeline curve + the moments not shown yet) as plain data for the project file."""
        r = self.result
        if r is None or getattr(r, "times", None) is None or not len(r.times):
            return self.project.analysis or {}
        return {"duration": float(r.duration),
                "times": [round(float(x), 2) for x in r.times],
                "score": [round(float(x), 3) for x in r.score],
                "candidates": [asdict(h) for h in self.candidates],
                "transcript": [[round(float(a), 2), round(float(b), 2), str(t)]
                               for a, b, t in (getattr(r, "transcript", None) or []) if str(t).strip()],
                "shown": int(getattr(self, "_shown_total", 0))}

    def _autosave(self):
        if not self.project.source:
            return
        try:
            self._collect_export_settings()
            self.project.analysis = self._pack_analysis()
            sig = json.dumps(asdict(self.project), sort_keys=True, default=str)
            h = hashlib.md5(sig.encode()).hexdigest()
            if h == self._session_sig:
                return
            tmp = self._session_file().with_suffix(".tmp")
            tmp.write_text(sig, encoding="utf-8")
            os.replace(tmp, self._session_file())
            self._session_sig = h
            self._save_setting("session_project_path", getattr(self, "project_path", "") or "")
        except Exception as e:                      # never let autosave get in the way
            self._log(f"(Couldn't save the session: {e})") if hasattr(self, "_log") else None

    def _restore_session(self):
        f = self._session_file()
        if self.project.source or not f.exists():
            return
        try:
            proj = editor.Project.load(f)
        except Exception:
            return
        if not proj.source or not os.path.exists(proj.source):
            return
        self._use_project(proj, config.load_settings().get("session_project_path") or None)
        n = len(proj.clips)
        self._session_sig = ""
        self.tabs.setCurrentIndex(1)
        self._show_toast(f"↺ Picked up where you left off: {proj.title or Path(proj.source).name}"
                         + (f" · {n} moment{'s' if n != 1 else ''}" if n else ""))

    def _use_project(self, proj, path):
        self.load_video(proj.source, proj.title, proj.url, proj.offset, proj.segments)
        self.project = proj
        self.project_path = path
        self._apply_export_settings()
        self.candidates = []
        an = proj.analysis or {}
        if an.get("times") and an.get("score") and len(an["times"]) == len(an["score"]):
            hs = []
            for h in an.get("candidates", []):
                try:
                    hs.append(analysis.Highlight(**{k: v for k, v in h.items()
                                                    if k in analysis.Highlight.__dataclass_fields__}))
                except TypeError:
                    pass
            self.result = analysis.AnalysisResult(
                duration=float(an.get("duration") or 0), times=np.asarray(an["times"], dtype=float),
                score=np.asarray(an["score"], dtype=float), loudness=np.zeros(0), scene_times=[], highlights=hs,
                transcript=[tuple(x) for x in an.get("transcript", [])])
            self.candidates = hs
            self._shown_total = int(an.get("shown", 0))
            self.timeline.set_data(self.result.duration or (self.info.duration if self.info else 0),
                                   self.result.times, self.result.score)
        self._fill_summary()
        self._fill_clip_list()
        self._update_more_button()
        self._update_audio_banner()
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
        # ---- AI models (local vs cloud)
        gai = QGroupBox("AI models")
        av = QVBoxLayout(gai)
        cfg = ai.settings()
        self.ai_local = QRadioButton("On this PC: local models (private, free, uses your GPU)")
        self.ai_cloud = QRadioButton("Cloud: Claude (needs an Anthropic API key)")
        (self.ai_local if cfg["mode"] == "local" else self.ai_cloud).setChecked(True)
        av.addWidget(self.ai_local)
        self.ai_local_box = QWidget()
        lf = QFormLayout(self.ai_local_box)
        lf.setContentsMargins(26, 0, 0, 6)
        self.ai_kind = QComboBox()
        self.ai_kind.addItems(["Ollama", "OpenAI-compatible (LM Studio, llama.cpp, Jan…)"])
        self.ai_kind.setCurrentIndex(0 if cfg["local_kind"] == "ollama" else 1)
        self.ai_url = QLineEdit(cfg["local_url"])
        self.ai_model = QComboBox()
        self.ai_model.setEditable(True)
        self.ai_model.setMinimumWidth(200)
        self.ai_model.setMaxVisibleItems(24)
        if cfg["local_model"]:
            self.ai_model.addItem(cfg["local_model"], cfg["local_model"])
        self.ai_model.lineEdit().setPlaceholderText("pick or type a model name")
        refresh = QToolButton()
        refresh.setText("⟳")
        refresh.setToolTip("Reload the list of installed models")
        refresh.clicked.connect(lambda: self.refresh_local_models(quiet=False))
        mrow = QHBoxLayout()
        mrow.addWidget(self.ai_model, 1)
        mrow.addWidget(refresh)
        self.ai_local_status = QLabel("")
        self.ai_local_status.setObjectName("muted")
        self.ai_local_status.setWordWrap(True)
        lf.addRow("Server", self.ai_kind)
        lf.addRow("Address", self.ai_url)
        lf.addRow("Model", mrow)
        lf.addRow("", self.ai_local_status)
        av.addWidget(self.ai_local_box)
        av.addWidget(self.ai_cloud)
        self.ai_cloud_box = QWidget()
        cf = QFormLayout(self.ai_cloud_box)
        cf.setContentsMargins(26, 0, 0, 6)
        self.ai_key = QLineEdit()
        self.ai_key.setEchoMode(QLineEdit.Password)
        self.ai_key.setPlaceholderText("•••••••• saved (paste a new key to replace)" if ai.get_api_key()
                                       else "sk-ant-…  (from console.anthropic.com)")
        self.ai_cloud_model = QComboBox()
        self.ai_cloud_model.setEditable(True)
        self.ai_cloud_model.setMinimumWidth(200)
        self.ai_cloud_model.setMaxVisibleItems(24)
        self.ai_cloud_model.lineEdit().setPlaceholderText("pick or type a model id")
        self._fill_cloud_models([], cfg["cloud_model"])
        crefresh = QToolButton()
        crefresh.setText("⟳")
        crefresh.setToolTip("Load every Claude model your API key can use")
        crefresh.clicked.connect(lambda: self.refresh_cloud_models(quiet=False))
        crow = QHBoxLayout()
        crow.addWidget(self.ai_cloud_model, 1)
        crow.addWidget(crefresh)
        self.ai_cloud_status = QLabel("")
        self.ai_cloud_status.setObjectName("muted")
        self.ai_cloud_status.setWordWrap(True)
        cf.addRow("API key", self.ai_key)
        cf.addRow("Model", crow)
        cf.addRow("", self.ai_cloud_status)
        av.addWidget(self.ai_cloud_box)
        sf = QFormLayout()
        self.s_whisper = QComboBox()
        for name, size, note in transcribe.WHISPER_MODELS:    # smallest first
            self.s_whisper.addItem("  ·  ".join(x for x in (name, size, note) if x), name)
        self.s_whisper.setMaxVisibleItems(len(transcribe.WHISPER_MODELS))
        self.s_whisper.setCurrentIndex(max(0, self.s_whisper.findData(s.get("whisper_model", "small"))))
        self.s_whisper.setToolTip("Bigger = more accurate but slower. The size is a one-time download.")
        self.s_whisper.currentIndexChanged.connect(
            lambda _: self._save_setting("whisper_model", self.s_whisper.currentData()))
        self.s_whisper.setEnabled(transcribe.available())
        sf.addRow("Speech-to-text", self.s_whisper)
        sl = QLabel("Speech is always turned into text on this PC (Whisper)." if transcribe.available()
                    else "Speech-to-text isn't in this copy. Install the latest version from Updates.")
        sl.setObjectName("muted")
        sl.setWordWrap(True)
        sf.addRow("", sl)
        grow2 = QHBoxLayout()
        self.gpu_lbl = QLabel("")
        self.gpu_lbl.setWordWrap(True)
        self.gpu_btn = QPushButton("")
        self.gpu_btn.clicked.connect(self.toggle_gpu_pack)
        self.gpu_bar = QProgressBar(); self.gpu_bar.setRange(0, 1000); self.gpu_bar.setTextVisible(False)
        self.gpu_bar.setMaximumHeight(10); self.gpu_bar.hide()
        grow2.addWidget(self.gpu_lbl, 1)
        grow2.addWidget(self.gpu_btn)
        sf.addRow("GPU speed-up", grow2)
        sf.addRow("", self.gpu_bar)
        av.addLayout(sf)
        QTimer.singleShot(0, self._update_gpu_row)
        trow = QHBoxLayout()
        self.ai_test_btn = QPushButton("Test AI")
        self.ai_test_btn.clicked.connect(self.test_ai)
        self.ai_test_label = QLabel("")
        self.ai_test_label.setWordWrap(True)
        trow.addWidget(self.ai_test_btn)
        trow.addWidget(self.ai_test_label, 1)
        av.addLayout(trow)
        hint = QLabel("Used for ✨ AI clip titles & captions, finding highlights, and choosing context images.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        av.addWidget(hint)
        left.addWidget(gai)

        # ---- context images: API keys for the picture sources that need one
        from .. import context_images as ci
        gk = QGroupBox("Context images")
        self.ctx_keys_group = gk
        kf = QFormLayout(gk)
        kh = QLabel("Wikipedia / Wikimedia needs no key. Pexels (free stock photos) and web image search "
                    "(Brave) need your own API key. Keys are stored encrypted for your Windows account.")
        kh.setObjectName("muted")
        kh.setWordWrap(True)
        kf.addRow(kh)
        self.ctx_key_edits = {}
        for prov, label, link, hint_txt in (
                ("pexels", "Pexels key", "https://www.pexels.com/api/", "free key from pexels.com/api"),
                ("web", "Brave Search key", "https://api-dashboard.search.brave.com/",
                 "key from the Brave Search API dashboard")):
            row = QHBoxLayout()
            e = QLineEdit()
            e.setEchoMode(QLineEdit.Password)
            e.setPlaceholderText("•••••••• saved (paste a new key to replace)" if ci.get_key(prov) else hint_txt)
            bs = QPushButton("Save")
            bg = QPushButton("Get a key")
            bg.setObjectName("link")
            bg.clicked.connect(lambda _=False, u=link: QDesktopServices.openUrl(QUrl(u)))
            st = QLabel("✔ saved" if ci.get_key(prov) else "")
            st.setObjectName("muted")

            def save(_=False, prov=prov, e=e, st=st):
                ci.set_key(prov, e.text())
                e.clear()
                has = bool(ci.get_key(prov))
                e.setPlaceholderText("•••••••• saved (paste a new key to replace)" if has else "")
                st.setText("✔ saved" if has else "removed")
                if hasattr(self, "ctx_tab"):
                    self.ctx_tab._update_source_hints()
            bs.clicked.connect(save)
            e.returnPressed.connect(save)
            row.addWidget(e, 1); row.addWidget(bs); row.addWidget(bg); row.addWidget(st)
            kf.addRow(label, row)
            self.ctx_key_edits[prov] = e
        left.addWidget(gk)

        # ---- publishing accounts (your own free developer apps)
        from .. import publish as pb
        gp = QGroupBox("Publishing accounts")
        self.pub_accounts_group = gp
        pv = QVBoxLayout(gp)
        ph = QLabel("RumbleCut posts through your own free developer app on each platform - set each one up "
                    "once (press “Setup steps”), paste its keys here and press Connect to sign in. "
                    f"Use this redirect / callback address everywhere it's asked for:")
        ph.setObjectName("muted")
        ph.setWordWrap(True)
        pv.addWidget(ph)
        rr = QHBoxLayout()
        ru = QLineEdit(pb.REDIRECT_URI)
        ru.setReadOnly(True)
        rc = QPushButton("Copy")
        rc.clicked.connect(lambda: QApplication.clipboard().setText(pb.REDIRECT_URI))
        rr.addWidget(ru, 1)
        rr.addWidget(rc)
        pv.addLayout(rr)
        self.pub_rows = {}
        for plat, name in pb.PLATFORMS.items():
            info = pb.SETUP[plat]
            box = QGroupBox(name)
            bf = QFormLayout(box)
            cid, sec = pb.get_credentials(plat)
            e_id = QLineEdit(cid)
            e_id.setPlaceholderText(info["id_label"])
            e_sec = QLineEdit()
            e_sec.setEchoMode(QLineEdit.Password)
            e_sec.setPlaceholderText("•••••••• saved" if sec else info["secret_label"])
            st = QLabel("")
            st.setObjectName("muted")
            st.setWordWrap(True)
            b_steps = QPushButton("Setup steps")
            b_conn = QPushButton("Connect")
            b_conn.setObjectName("primary")
            b_disc = QPushButton("Disconnect")
            brow = QHBoxLayout()
            for b in (b_steps, b_conn, b_disc):
                brow.addWidget(b)
            brow.addStretch()
            bf.addRow(info["id_label"], e_id)
            bf.addRow(info["secret_label"], e_sec)
            bf.addRow("", brow)
            bf.addRow("", st)
            row = {"id": e_id, "secret": e_sec, "status": st, "connect": b_conn, "disconnect": b_disc}
            self.pub_rows[plat] = row
            b_steps.clicked.connect(lambda _=False, p=plat: self._pub_steps(p))
            b_conn.clicked.connect(lambda _=False, p=plat: self._pub_connect(p))
            b_disc.clicked.connect(lambda _=False, p=plat: (pb.disconnect(p), self._pub_status(p)))
            self._pub_status(plat)
            pv.addWidget(box)
        left.addWidget(gp)

        def mode_changed():
            local = self.ai_local.isChecked()
            self.ai_local_box.setEnabled(local)
            self.ai_cloud_box.setEnabled(not local)
            self._save_setting("ai_mode", "local" if local else "cloud")
            self.ai_test_label.setText("")
        self.ai_local.toggled.connect(lambda _: mode_changed())
        self.ai_local_box.setEnabled(cfg["mode"] == "local")
        self.ai_cloud_box.setEnabled(cfg["mode"] == "cloud")

        def kind_changed(i):
            kind = "ollama" if i == 0 else "openai"
            defaults = {"ollama": "http://localhost:11434", "openai": "http://localhost:1234"}
            if self.ai_url.text().strip().rstrip("/") in defaults.values():
                self.ai_url.setText(defaults[kind])
                self._save_setting("local_url", defaults[kind])
            self._save_setting("local_kind", kind)
            self.refresh_local_models(quiet=False)
        self.ai_kind.currentIndexChanged.connect(kind_changed)
        self.ai_url.editingFinished.connect(lambda: (self._save_setting("local_url", self.ai_url.text().strip()),
                                                     self.refresh_local_models(quiet=True)))
        self.ai_model.currentTextChanged.connect(lambda _: (n := self._ai_model_name()) and
                                                 not n.startswith("pull:") and
                                                 self._save_setting("local_model", n))
        self.ai_model.activated.connect(self._local_model_picked)

        def key_entered():
            k = self.ai_key.text().strip()
            if k:
                ai.set_api_key(k)
                self.ai_key.clear()
                self.ai_key.setPlaceholderText("•••••••• saved (paste a new key to replace)")
                self.statusBar().showMessage("API key saved (encrypted for your Windows account) ✓", 4000)
                self.refresh_cloud_models(quiet=False)
        self.ai_key.editingFinished.connect(key_entered)
        self.ai_cloud_model.activated.connect(lambda _: self._save_cloud_model())
        self.ai_cloud_model.lineEdit().editingFinished.connect(self._save_cloud_model)
        if ai.get_api_key():
            QTimer.singleShot(0, lambda: self.refresh_cloud_models(quiet=True))

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
        right.addWidget(g2)

        gm = QGroupBox("Moments you add")
        mf = QFormLayout(gm)
        self.s_manual_len = QDoubleSpinBox()
        self.s_manual_len.setRange(1, 600)
        self.s_manual_len.setDecimals(0)
        self.s_manual_len.setSingleStep(1)
        self.s_manual_len.setSuffix(" s")
        self.s_manual_len.setValue(float(s.get("manual_len", 5.0) or 5.0))
        self.s_manual_len.valueChanged.connect(lambda x: self._save_setting("manual_len", float(x)))
        mf.addRow("Length", self.s_manual_len)
        ml = QLabel("How long a moment is when you right-click the timeline to add one. Drag its edges to change it.")
        ml.setObjectName("muted")
        ml.setWordWrap(True)
        mf.addRow("", ml)
        right.addWidget(gm)

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
        if self.ai_local.isChecked():
            self.refresh_local_models(quiet=True)

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

    # ---- GPU speed-up for speech-to-text
    def _update_gpu_row(self):
        if not transcribe.available():
            self.gpu_lbl.setText("Not available in this copy.")
            self.gpu_btn.hide()
            return
        if gpu_pack.installed():
            self.gpu_lbl.setText(f"<span style='color:{ACCENT}'>✔ Installed</span> - speech-to-text runs on your "
                                 f"NVIDIA GPU (cuBLAS {gpu_pack.version() or ''}).")
            self.gpu_btn.setText("Remove")
            self.gpu_btn.setObjectName("")
        elif gpu_pack.nvidia_gpu_present():
            self.gpu_lbl.setText("Your NVIDIA GPU can transcribe speech ~10-20× faster than the CPU. It needs "
                                 "NVIDIA's free cuBLAS libraries - a one-time 550 MB download (about 800 MB on disk).")
            self.gpu_btn.setText("⚡ Install GPU speed-up")
            self.gpu_btn.setObjectName("primary")
        else:
            self.gpu_lbl.setText("No NVIDIA GPU found - speech-to-text runs on the CPU.")
            self.gpu_btn.hide()
        self.gpu_btn.style().unpolish(self.gpu_btn)
        self.gpu_btn.style().polish(self.gpu_btn)

    def toggle_gpu_pack(self):
        if gpu_pack.installed():
            if QMessageBox.question(self, "GPU speed-up", "Remove the NVIDIA libraries (550 MB)? "
                                    "Speech-to-text will run on the CPU.") == QMessageBox.Yes:
                gpu_pack.remove()
                transcribe.reset()
                self._update_gpu_row()
            return

        def job(on_progress=None, cancel=None):
            ver = gpu_pack.install(on_progress, cancel)
            transcribe.reset()
            on_progress(-1.0, "Testing the GPU with a short transcription…")
            import numpy as np
            m = transcribe._load_model("tiny", on_progress)
            list(m.transcribe(np.zeros(16000, np.float32), beam_size=1)[0])
            return ver, transcribe.device_note, transcribe.gpu_status()
        self.gpu_btn.setEnabled(False)
        self.gpu_bar.show()
        w = Worker(job)

        def prog(f, m):
            if f < 0:
                self.gpu_bar.setRange(0, 0)
            else:
                self.gpu_bar.setRange(0, 1000)
                self.gpu_bar.setValue(int(f * 1000))
            self.gpu_lbl.setText(m)

        def done(r):
            ver, dev, why = r
            self._update_gpu_row()
            if dev.startswith("NVIDIA"):
                self.statusBar().showMessage("⚡ GPU speed-up working - speech now runs on your NVIDIA GPU", 8000)
            else:
                QMessageBox.warning(self, "GPU speed-up", "The libraries were installed, but the GPU still couldn't "
                                    f"run the speech model, so the CPU will be used.\n\nDetails: {why[:300]}\n\n"
                                    "Updating your NVIDIA driver usually fixes this.")
        w.progress.connect(prog)
        w.done.connect(done)
        w.failed.connect(lambda m: (QMessageBox.warning(self, "GPU speed-up", m), self._update_gpu_row()))
        w.cancelled.connect(self._update_gpu_row)
        w.finished.connect(lambda: (self.gpu_btn.setEnabled(True), self.gpu_bar.hide()))
        self._start(w)

    # ---- AI models
    def _ai_model_name(self) -> str:
        """The chosen model's real name (the list shows 'name · params · size')."""
        i = self.ai_model.currentIndex()
        if i >= 0 and self.ai_model.itemText(i) == self.ai_model.currentText():
            return (self.ai_model.itemData(i) or self.ai_model.itemText(i)).strip()
        return self.ai_model.currentText().strip()      # typed by hand

    # cloud (Claude) models
    def _cloud_model_name(self) -> str:
        i = self.ai_cloud_model.currentIndex()
        if i >= 0 and self.ai_cloud_model.itemText(i) == self.ai_cloud_model.currentText():
            return (self.ai_cloud_model.itemData(i) or self.ai_cloud_model.itemText(i)).strip()
        return self.ai_cloud_model.currentText().strip()

    def _save_cloud_model(self):
        self._save_setting("cloud_model", self._cloud_model_name() or ai.DEFAULT_CLOUD_MODEL)

    def _fill_cloud_models(self, models: list[dict], current: str):
        """models: from the API ({"id", "name"}), or [] for the built-in list."""
        cb = self.ai_cloud_model
        cb.blockSignals(True)
        cb.clear()
        ids = []
        for mid, name, note in ai.CLOUD_MODELS:
            if not models or any(m["id"] == mid for m in models):
                cb.addItem(ai.cloud_model_label(mid), mid)
                ids.append(mid)
        extra = [m for m in models if m["id"] not in ids]
        if extra:
            cb.insertSeparator(cb.count())
            for m in extra:
                cb.addItem(f"{m['name']}  ·  {m['id']}", m["id"])
                ids.append(m["id"])
        i = cb.findData(current)
        if i >= 0:
            cb.setCurrentIndex(i)
        elif current:
            cb.addItem(current, current)
            cb.setCurrentIndex(cb.count() - 1)
        cb.blockSignals(False)

    def refresh_cloud_models(self, quiet: bool = True):
        if not hasattr(self, "ai_cloud_model"):
            return
        if not ai.get_api_key():
            self.ai_cloud_status.setText("Paste your API key to see every model your account can use.")
            return
        self.ai_cloud_status.setText("Loading your models…")
        w = Worker(lambda on_progress=None, cancel=None: ai.list_cloud_models())
        w.background = True

        def done(models):
            self._fill_cloud_models(models, self._cloud_model_name() or ai.settings()["cloud_model"])
            self.ai_cloud_status.setText(f"<span style='color:{ACCENT}'>✔</span> {len(models)} model(s) "
                                         "available on your account")
        w.done.connect(done)
        w.failed.connect(lambda m: self.ai_cloud_status.setText(
            m if not quiet else "Showing the usual Claude models (press ⟳ to load your account's list)."))
        self._start(w)

    # local models
    def _local_model_picked(self, i: int):
        data = self.ai_model.itemData(i) or ""
        if not data.startswith("pull:"):
            return
        name = data[5:]
        size = next((gb for n, gb, _ in ai.POPULAR_LOCAL if n == name), None)
        self.ai_model.blockSignals(True)
        k = self.ai_model.findData(ai.settings()["local_model"])
        if k >= 0:
            self.ai_model.setCurrentIndex(k)
        else:
            self.ai_model.setEditText(ai.settings()["local_model"])
        self.ai_model.blockSignals(False)
        if QMessageBox.question(self, "Download model",
                                f"Download {name} into Ollama"
                                + (f" (about {size:g} GB)" if size else "") + "?\n\n"
                                "It's a one-time download; you can keep using RumbleCut meanwhile.") != QMessageBox.Yes:
            return
        url = self.ai_url.text().strip()
        w = Worker(lambda on_progress=None, cancel=None: ai.pull_local_model(name, url, on_progress, cancel))
        w.background = True
        w.progress.connect(lambda f, m: self.ai_local_status.setText(
            m + (f"  ({f * 100:.0f}%)" if f >= 0 else "")))

        def done(n):
            self._save_setting("local_model", n)
            self.ai_model.blockSignals(True)
            self.ai_model.setEditText(n)                 # becomes the chosen model once the list reloads
            self.ai_model.blockSignals(False)
            self.refresh_local_models(quiet=False)
            self.statusBar().showMessage(f"✔ {n} downloaded and chosen", 5000)
        w.done.connect(done)
        w.failed.connect(lambda m: self.ai_local_status.setText(f"<span style='color:#f87171'>{m}</span>"))
        self._start(w)

    def refresh_local_models(self, quiet: bool = True):
        if not hasattr(self, "ai_model"):
            return
        kind = "ollama" if self.ai_kind.currentIndex() == 0 else "openai"
        url = self.ai_url.text().strip()
        self.ai_local_status.setText("Looking for models…")
        w = Worker(lambda on_progress=None, cancel=None: ai.list_local_models(kind, url))
        w.background = True

        def done(models):
            cur = self._ai_model_name() or ai.settings()["local_model"]
            self.ai_model.blockSignals(True)
            self.ai_model.clear()
            for m in models:                                   # already smallest first
                self.ai_model.addItem(ai.model_label(m), m["name"])
            installed = {m["name"] for m in models} | {m["name"].removesuffix(":latest") for m in models}
            more = [x for x in ai.POPULAR_LOCAL if x[0] not in installed] if kind == "ollama" else []
            if more:
                if models:
                    self.ai_model.insertSeparator(self.ai_model.count())
                for n, gb, note in more:
                    self.ai_model.addItem(f"⬇  {n}  ·  about {gb:g} GB  ·  {note}  (not installed)", "pull:" + n)
            i = self.ai_model.findData(cur) if cur else -1
            if i >= 0:
                self.ai_model.setCurrentIndex(i)
            elif cur:
                self.ai_model.setEditText(cur)                 # a name typed by hand
            elif models:
                self.ai_model.setCurrentIndex(0)
            else:
                self.ai_model.setCurrentIndex(-1)
            self.ai_model.blockSignals(False)
            if self._ai_model_name() and not self._ai_model_name().startswith("pull:"):
                self._save_setting("local_model", self._ai_model_name())
            self.ai_local_status.setText(f"<span style='color:{ACCENT}'>✔ Connected</span> · {len(models)} model(s) "
                                         "installed" if models else
                                         "Connected, but no models are installed yet. Pick one marked ⬇ "
                                         "in the list to download it.")
        w.done.connect(done)
        w.failed.connect(lambda m: self.ai_local_status.setText(
            m if not quiet else "Not connected. Start Ollama (or your local server), then press ⟳."))
        self._start(w)

    def test_ai(self):
        self.ai_test_btn.setEnabled(False)
        self.ai_test_label.setText("Asking the model…")
        w = Worker(lambda on_progress=None, cancel=None: ai.test())
        w.done.connect(self.ai_test_label.setText)
        w.failed.connect(lambda m: self.ai_test_label.setText(f"<span style='color:#f87171'>{m}</span>"))
        w.finished.connect(lambda: self.ai_test_btn.setEnabled(True))
        self._start(w)

    def ai_titles(self, which: str = "current"):
        if which == "current":
            c = self.current_clip()
            targets = [c] if c else []
        else:
            targets = [c for c in self.project.clips if c.enabled]
        if not targets:
            return
        if not transcribe.available():
            QMessageBox.information(self, "AI titles", "Speech-to-text isn't included in this copy of RumbleCut. "
                                    "Install the latest version from Settings → Updates.")
            return
        stream_title = self.project.title

        def job(on_progress=None, cancel=None):
            results = []
            for i, c in enumerate(targets):
                if cancel():
                    break
                base = i / len(targets)
                step = lambda f, m, i=i: on_progress(base + max(f, 0) / len(targets) * 0.8,
                                                     f"Clip {i + 1}/{len(targets)}: {m}")
                try:
                    segs = transcribe.transcribe(c.source, start=c.start, end=c.end, cancel=cancel,
                                                 on_progress=step)
                    text = " ".join(t for _, _, t in segs)
                    step(1.0, "writing title…")
                    results.append((c, ai.suggest_title(text, stream_title), None))
                except ft.Cancelled:
                    raise
                except Exception as e:
                    results.append((c, None, str(e)))
            return results

        btns = [self.btn_ai_title, self.btn_ai_all]
        for b in btns:
            b.setEnabled(False)
        w = Worker(job)
        w.progress.connect(lambda f, m: (self.an_progress.setValue(int(max(f, 0) * 1000)), self.an_status.setText(m)))

        def done(results):
            errs = []
            for c, res, err in results:
                if res:
                    c.title = res["title"]
                    if res.get("caption") and not c.caption.strip():
                        c.caption = res["caption"]
                else:
                    errs.append(err)
            ok = sum(1 for _, r, _ in results if r)
            self._fill_clip_list(keep_row=True)
            self.select_clip(self.clip_list.currentRow())
            self.an_status.setText(f"✨ AI wrote {ok} title(s) with {ai.describe()}")
            if errs:
                QMessageBox.warning(self, "AI titles", f"{len(errs)} clip(s) failed:\n\n" + "\n".join(dict.fromkeys(errs)))
        w.done.connect(done)
        w.failed.connect(lambda m: QMessageBox.warning(self, "AI titles", m))
        w.finished.connect(lambda: [b.setEnabled(True) for b in btns])
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
        self.tabs.setTabText(self.settings_index, "Settings  ●" if found else "Settings")
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
        self._autosave()                 # it reopens just as it was after the relaunch
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
        self._autosave()
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
QPushButton#link {{ background: transparent; border: none; color: {ACCENT}; padding: 4px 6px; text-decoration: underline; }}
QPushButton#link:hover {{ color: #a6dc6f; }}
QLabel#badge {{ background: {ACCENT}; color: #0b0d10; font-weight: 700; padding: 3px 10px; border-radius: 9px; }}
QTabWidget::tab-bar {{ left: 48px; }}
QTabWidget#lefttabs::tab-bar {{ left: 0; }}
QTabBar::tab {{ padding: 10px 22px; background: #15171b; color: #9ca3af; border: none; }}
QTabBar::tab:selected {{ color: #fff; border-bottom: 3px solid {ACCENT}; background: #1b1d22; }}
QGroupBox {{ border: 1px solid #2c3038; border-radius: 8px; margin-top: 14px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #cbd5e1; font-weight: 600; }}
QFrame#pubcard {{ background: #171a20; border: 1px solid #2c3038; border-radius: 10px; }}
QFrame#pubcard QLabel, QFrame#pubcard QCheckBox {{ background: transparent; }}
QWidget#cardbody {{ background: transparent; }}
QToolButton#playbtn {{ background: {ACCENT}; border-color: {ACCENT}; }}
QToolButton#playbtn:hover {{ background: #9bd85a; }}
QFrame#vsep {{ color: #3a3f47; margin: 2px 6px; }}
QFrame#cardline {{ background: #2c3038; border: none; }}
QCheckBox#cardtitle, QLabel#cardtitle {{ font-size: 14px; font-weight: 600; color: #e5e7eb; spacing: 8px; }}
QFrame#pubbar {{ border-top: 1px solid #2c3038; }}
QScrollArea#pubscroll, QWidget#pubcontent {{ background: transparent; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{ background: #121418; border: 1px solid #2f343d;
    border-radius: 6px; padding: 5px 7px 5px 11px; selection-background-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: #1f2228; border: 1px solid #2c3038; selection-background-color: #2d3a22; padding: 4px; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QPushButton, QToolButton {{ background: #2a2e36; border: 1px solid #353a44; border-radius: 6px; padding: 6px 12px; }}
QPushButton:hover, QToolButton:hover {{ background: #333844; }}
QPushButton:disabled {{ color: #5b6370; background: #20232a; }}
QPushButton:checked {{ background: #2d3a22; border-color: {ACCENT}; color: #fff; }}
QPushButton#primary {{ background: {ACCENT}; color: #0b0d10; font-weight: 700; border: none; padding: 7px 18px; }}
QPushButton#primary:hover {{ background: #9bd85c; }}
QPushButton#danger {{ color: #fca5a5; border-color: #7f1d1d; }}
QPushButton#danger:hover {{ background: #3b1717; }}
QPushButton#primary:disabled {{ background: #3d5a26; color: #1b1d22; }}
QListWidget {{ background: #121418; border: 1px solid #2c3038; border-radius: 8px; }}
QListWidget::item {{ padding: 6px; border-radius: 6px; }}
QListWidget::item:selected {{ background: #2d3a22; color: #fff; }}
QProgressBar {{ background: #121418; border: 1px solid #2c3038; border-radius: 6px; text-align: center; height: 16px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
QSlider::groove:horizontal {{ height: 4px; background: #333844; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {ACCENT}; width: 14px; margin: -6px 0; border-radius: 7px; }}
QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 3px 2px 3px 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0 3px 2px 3px; }}
QScrollBar::handle:vertical {{ background: #3a3f49; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:horizontal {{ background: #3a3f49; border-radius: 4px; min-width: 30px; }}
QScrollBar::handle:hover {{ background: #545b68; }}
QScrollBar::handle:pressed {{ background: {ACCENT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QAbstractScrollArea::corner {{ background: transparent; }}
QPlainTextEdit#ailog {{ font-family: Consolas, "Cascadia Mono", monospace; font-size: 9pt; }}
QTabWidget#lefttabs::pane {{ border: none; }}
QLabel#novideo {{ background: #0e0f12; color: #e5e7eb; font-size: 14px; border-radius: 6px; }}
QFrame#cliptl {{ background: transparent; }}
QLabel#toast {{ background: #2a3a1e; color: #e5e7eb; border: 1px solid {ACCENT}; border-radius: 8px; padding: 8px 14px; }}
QToolTip {{ background: #1f2228; color: #e5e7eb; border: 1px solid #3a404b; padding: 6px; }}
QFrame#banner {{ background: #1f2a17; border: 1px solid #3d5a26; border-radius: 8px; }}
QFrame#banner QLabel, QFrame#banner QCheckBox {{ background: transparent; }}
QToolButton#hamburgerBtn {{ padding: 0; border: none; background: transparent; }}
QToolButton#hamburgerBtn:hover {{ background: #2a2e36; border-radius: 6px; }}
QToolButton#hamburgerBtn::menu-indicator {{ image: none; width: 0; }}
QScrollBar#tlscroll:horizontal {{ height: 20px; background: #15171c; border: 1px solid #262a31; border-radius: 10px; margin: 0; }}
QScrollBar#tlscroll::handle:horizontal {{ background: #4b5260; border-radius: 7px; min-width: 56px; margin: 2px; }}
QScrollBar#tlscroll::handle:horizontal:hover {{ background: #6b7280; }}
QScrollBar#tlscroll::handle:horizontal:pressed {{ background: {ACCENT}; }}
QScrollBar#tlscroll::add-page:horizontal, QScrollBar#tlscroll::sub-page:horizontal {{ background: none; }}
QToolButton#zoomBtn {{ padding: 0; font-size: 11pt; }}
QToolButton#zoomBtn:disabled {{ color: #4b5260; background: #20232a; }}
QPushButton#more {{ background: #20242b; border: 1px dashed #4b5260; padding: 9px; color: #cbd5e1; }}
QPushButton#more:hover {{ border-color: {ACCENT}; color: #fff; }}
QCheckBox::indicator, QListWidget::indicator {{ width: 15px; height: 15px; border: 1px solid #4b5260;
    border-radius: 4px; background: #121418; }}
QCheckBox::indicator:checked, QListWidget::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QRadioButton::indicator {{ width: 14px; height: 14px; border: 2px solid #4b5260; border-radius: 9px; background: #121418; }}
QRadioButton::indicator:checked {{ border: 4px solid {ACCENT}; background: #0b0d10; }}
QRadioButton:disabled, QLabel:disabled {{ color: #5b6370; }}
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
