"""🎬 Edit clips tab - context pictures: find pictures for what's being said, then review them.

For the moment you pick, RumbleCut transcribes it, asks your AI what's worth showing, and fetches
pictures from the sources you allow. Each suggestion waits for your approval: swap it for another
candidate, search again with your own words, change how it looks, or remove it. Only approved
pictures are exported.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
    QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from .flow import FlowLayout
from .. import ai, config, context_images as ci, editor, transcribe
from .. import ffmpeg_tools as ft
from .timeline import fmt_time
from .workers import Worker

POSITIONS = ["top-right", "top-left", "bottom-right", "bottom-left", "center"]
STYLES = [("corner", "Corner pop-up"), ("full", "Full-screen cutaway")]


class PreviewLabel(QLabel):
    """Shows the picture on top of the clip's frame, the way it will look in the export."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAlignment(Qt.AlignCenter)
        self.frame: QPixmap | None = None
        self.image: QPixmap | None = None
        self.ov = None
        self.note = ""

    def set_content(self, frame, image, ov, note=""):
        self.frame, self.image, self.ov, self.note = frame, image, ov, note
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        w = min(W, H * 16 / 9)
        h = w * 9 / 16
        r = QRectF((W - w) / 2, (H - h) / 2, w, h)
        p.fillRect(self.rect(), QColor("#121418"))
        if self.frame and not self.frame.isNull():
            p.drawPixmap(r, self.frame, QRectF(self.frame.rect()))
        else:
            p.fillRect(r, QColor("#0b0c0f"))
            if self.note:
                p.setPen(QColor("#6b7280"))
                p.drawText(r.adjusted(0, r.height() * 0.55, 0, 0), Qt.AlignHCenter | Qt.AlignTop, self.note)
        ov, img = self.ov, self.image
        if ov is not None and img and not img.isNull():
            if ov.style == "full":
                # cover the frame (the export also zooms in slowly)
                s = max(r.width() / img.width(), r.height() / img.height())
                sw, sh = r.width() / s, r.height() / s
                src = QRectF((img.width() - sw) / 2, (img.height() - sh) / 2, sw, sh)
                p.drawPixmap(r, img, src)
            else:
                iw = r.width() * ov.scale
                ih = iw * img.height() / max(1, img.width())
                bw = 6 * r.width() / 1920 * 2 if ov.border else 0
                lab_h = 0
                fs = max(8, int(r.width() * 0.022))
                if ov.show_label and ov.label.strip():
                    lab_h = fs * 1.9
                tw, th = iw + 2 * bw, ih + 2 * bw + lab_h
                m = r.width() * 0.04
                pos = ov.position
                x = r.left() + m if "left" in pos else (r.right() - m - tw if "right" in pos else r.center().x() - tw / 2)
                y = r.top() + m if "top" in pos else (r.bottom() - m - th if "bottom" in pos else r.center().y() - th / 2)
                if bw:
                    p.fillRect(QRectF(x, y, tw, ih + 2 * bw), QColor("white"))
                p.drawPixmap(QRectF(x + bw, y + bw, iw, ih), img, QRectF(img.rect()))
                if lab_h:
                    p.fillRect(QRectF(x, y + ih + 2 * bw, tw, lab_h), QColor("#15171c"))
                    f = QFont(self.font())
                    f.setPixelSize(fs)
                    f.setBold(True)
                    p.setFont(f)
                    p.setPen(QColor("white"))
                    p.drawText(QRectF(x, y + ih + 2 * bw, tw, lab_h), Qt.AlignCenter, ov.label.strip())
        p.setPen(QPen(QColor("#2c3038"), 1))
        p.drawRect(r)


class ContextImagesTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._transcripts: dict = {}
        self._frames: dict = {}
        self._pix: dict = {}
        self._worker: Worker | None = None
        self._loading = False
        self._tmp = Path(tempfile.mkdtemp(prefix="rumblecut_ctx_"))
        s = config.load_settings()

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(8)

        # ---- moment + find
        top = FlowLayout()
        top.addWidget(QLabel("Moment"))
        self.moment = QComboBox()
        self.moment.setMinimumWidth(320)
        self.moment.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.moment.currentIndexChanged.connect(lambda _: self._moment_changed())
        top.addWidget(self.moment, 1)
        self.btn_find = QPushButton("✨ Find pictures for this moment")
        self.btn_find.setObjectName("primary")
        self.btn_find.clicked.connect(lambda: self.find(all_ticked=False))
        self.btn_find_all = QPushButton("✨ Find for all ticked moments")
        self.btn_find_all.clicked.connect(lambda: self.find(all_ticked=True))
        top.addWidget(self.btn_find)
        top.addWidget(self.btn_find_all)
        v.addLayout(top)

        # ---- sources
        srow = FlowLayout()
        srow.addWidget(QLabel("Look in"))
        self.src_boxes: dict[str, QCheckBox] = {}
        chosen = set(s.get("ctx_sources") or ["wikimedia"])
        for key, name in ci.SOURCES.items():
            cb = QCheckBox(name)
            cb.setChecked(key in chosen)
            cb.toggled.connect(lambda _=False: self._save_sources())
            self.src_boxes[key] = cb
            srow.addWidget(cb)
        self.keys_link = QPushButton("Add API keys…")
        self.keys_link.setObjectName("link")
        self.keys_link.clicked.connect(self._open_key_settings)
        srow.addWidget(self.keys_link)
        srow.addStretch()
        srow.addWidget(QLabel("Pictures per moment"))
        self.per_clip = QSpinBox()
        self.per_clip.setRange(1, 10)
        self.per_clip.setValue(int(s.get("ctx_per_clip", 4)))
        self.per_clip.valueChanged.connect(lambda x: config.save_settings({"ctx_per_clip": int(x)}))
        srow.addWidget(self.per_clip)
        v.addLayout(srow)

        # ---- progress
        prow = FlowLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setFixedHeight(18)
        self.progress.setFormat("")
        self.progress.valueChanged.connect(
            lambda x: self.progress.setFormat(f"{x / 10:.0f}%" if x > 0 and self.progress.maximum() else ""))
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setObjectName("danger")
        keep = self.btn_cancel.sizePolicy(); keep.setRetainSizeWhenHidden(True); self.btn_cancel.setSizePolicy(keep)
        self.btn_cancel.hide()
        self.btn_cancel.clicked.connect(self._cancel)
        prow.addWidget(self.progress, 2)
        prow.addWidget(self.btn_cancel)
        prow.addWidget(self.status, 3)
        v.addLayout(prow)

        # ---- suggestions | preview + controls
        split = QSplitter(Qt.Vertical)          # list above the preview: fits beside the player
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.sugg = QListWidget()
        self.sugg.setIconSize(QSize(96, 54))
        self.sugg.setWordWrap(True)
        self.sugg.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sugg.currentRowChanged.connect(self._sugg_selected)
        lv.addWidget(self.sugg, 1)
        brow = FlowLayout()
        self.btn_approve_all = QPushButton("✓ Approve all")
        self.btn_approve_all.setToolTip("Approve every picture suggested for this moment")
        self.btn_approve_all.clicked.connect(self.approve_all)
        self.btn_clear = QPushButton("Remove suggestions")
        self.btn_clear.setToolTip("Remove the pictures you haven't approved for this moment")
        self.btn_clear.clicked.connect(self.clear_pending)
        brow.addWidget(self.btn_approve_all)
        brow.addWidget(self.btn_clear)
        lv.addLayout(brow)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.preview = PreviewLabel()
        rv.addWidget(self.preview, 3)
        self.alt_label = QLabel("Other pictures for this - click one to use it")
        self.alt_label.setObjectName("muted")
        rv.addWidget(self.alt_label)
        self.alts = QListWidget()
        self.alts.setViewMode(QListWidget.IconMode)
        self.alts.setFlow(QListWidget.LeftToRight)
        self.alts.setWrapping(False)
        self.alts.setIconSize(QSize(128, 72))
        self.alts.setFixedHeight(112)
        self.alts.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.alts.setMovement(QListWidget.Static)
        self.alts.setSpacing(4)
        self.alts.itemClicked.connect(self._alt_clicked)
        rv.addWidget(self.alts)

        ctl = QGridLayout()
        ctl.setHorizontalSpacing(10)
        self.e_label = QLineEdit()
        self.e_label.setPlaceholderText("Name on screen")
        self.e_label.textEdited.connect(lambda t: self._set("label", t))
        self.e_show_label = QCheckBox("Show the name under it")
        self.e_show_label.toggled.connect(lambda on: self._set("show_label", on))
        self.e_style = QComboBox()
        for key, name in STYLES:
            self.e_style.addItem(name, key)
        self.e_style.currentIndexChanged.connect(lambda _: self._set("style", self.e_style.currentData()))
        self.e_pos = QComboBox()
        self.e_pos.addItems(POSITIONS)
        self.e_pos.currentTextChanged.connect(lambda t: self._set("position", t))
        self.e_start = QDoubleSpinBox(); self.e_start.setRange(0, 3600); self.e_start.setDecimals(1)
        self.e_start.setSuffix(" s"); self.e_start.setSingleStep(0.5)
        self.e_start.valueChanged.connect(lambda x: self._set("start", x))
        self.e_dur = QDoubleSpinBox(); self.e_dur.setRange(0.5, 60); self.e_dur.setDecimals(1)
        self.e_dur.setSuffix(" s"); self.e_dur.setSingleStep(0.5)
        self.e_dur.valueChanged.connect(lambda x: self._set("duration", x))
        self.e_scale = QSpinBox(); self.e_scale.setRange(10, 90); self.e_scale.setSuffix(" %")
        self.e_scale.valueChanged.connect(lambda x: self._set("scale", x / 100))
        ctl.addWidget(QLabel("Name"), 0, 0); ctl.addWidget(self.e_label, 0, 1); ctl.addWidget(self.e_show_label, 0, 2, 1, 2)
        ctl.addWidget(QLabel("Look"), 1, 0); ctl.addWidget(self.e_style, 1, 1)
        ctl.addWidget(QLabel("Corner"), 1, 2); ctl.addWidget(self.e_pos, 1, 3)
        ctl.addWidget(QLabel("Appears at"), 2, 0); ctl.addWidget(self.e_start, 2, 1)
        ctl.addWidget(QLabel("Shows for"), 2, 2); ctl.addWidget(self.e_dur, 2, 3)
        ctl.addWidget(QLabel("Size"), 3, 0); ctl.addWidget(self.e_scale, 3, 1)
        rv.addLayout(ctl)

        srch = FlowLayout()
        self.e_query = QLineEdit()
        self.e_query.setPlaceholderText("Search again with your own words, e.g. “Vivek Ramaswamy rally”")
        self.e_query.returnPressed.connect(self.search_again)
        self.btn_search = QPushButton("🔍 Search again")
        self.btn_search.clicked.connect(self.search_again)
        srch.addWidget(self.e_query, 1)
        srch.addWidget(self.btn_search)
        rv.addLayout(srch)

        self.credit = QLabel("")
        self.credit.setObjectName("muted")
        self.credit.setWordWrap(True)
        self.credit.setTextFormat(Qt.RichText)
        self.credit.setOpenExternalLinks(True)
        rv.addWidget(self.credit)

        arow = FlowLayout()
        self.btn_approve = QPushButton("✓ Approve")
        self.btn_approve.setObjectName("primary")
        self.btn_approve.clicked.connect(self.approve)
        self.btn_remove = QPushButton("✖ Remove")
        self.btn_remove.setObjectName("danger")
        self.btn_remove.clicked.connect(self.remove)
        self.btn_play = QPushButton("▶ Play this bit")
        self.btn_play.setToolTip("Jump to this point in the Clip studio")
        self.btn_play.clicked.connect(self.play_here)
        arow.addWidget(self.btn_approve)
        arow.addWidget(self.btn_remove)
        arow.addStretch()
        arow.addWidget(self.btn_play)
        rv.addLayout(arow)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 5)
        split.setSizes([340, 860])
        v.addWidget(split, 1)
        self._editors = [self.e_label, self.e_show_label, self.e_style, self.e_pos, self.e_start, self.e_dur,
                         self.e_scale, self.e_query, self.btn_search, self.btn_approve, self.btn_remove,
                         self.btn_play, self.alts]
        self._sugg_selected(-1)
        self._update_source_hints()

    # ------------------------------------------------------------------ helpers
    @property
    def project(self):
        return self.win.project

    def sources(self) -> list[str]:
        return [k for k, cb in self.src_boxes.items() if cb.isChecked()]

    def _save_sources(self):
        config.save_settings({"ctx_sources": self.sources()})
        self._update_source_hints()

    def _update_source_hints(self):
        for key, cb in self.src_boxes.items():
            if key == "wikimedia":
                cb.setToolTip("Wikipedia article pictures and Wikimedia Commons - free to reuse with credit. No key needed.")
                continue
            has = bool(ci.get_key(key))
            base = ("Free stock photos (Pexels licence)." if key == "pexels" else
                    "Brave web image search - widest coverage, but most pictures are copyrighted: check before you publish.")
            cb.setToolTip(base + ("" if has else "  Needs an API key - add it in Settings → Context images."))
            cb.setText(ci.SOURCES[key] + ("" if has else "  (needs key)"))

    def _open_key_settings(self):
        self.win.tabs.setCurrentIndex(self.win.settings_index)
        if hasattr(self.win, "ctx_keys_group"):
            QTimer.singleShot(50, lambda: self.win._settings_scroll.ensureWidgetVisible(self.win.ctx_keys_group))

    def current_clip(self):
        cid = self.moment.currentData()
        for c in self.project.clips:
            if id(c) == cid:
                return c
        return None

    def current_overlay(self):
        c = self.current_clip()
        r = self.sugg.currentRow()
        if c and 0 <= r < len(c.overlays):
            return c.overlays[r]
        return None

    def _pixmap(self, path: str, max_w: int = 1280) -> QPixmap:
        key = (path, max_w)
        if key not in self._pix:
            pm = QPixmap(path)
            if not pm.isNull() and pm.width() > max_w:
                pm = pm.scaledToWidth(max_w, Qt.SmoothTransformation)
            self._pix[key] = pm
        return self._pix[key]

    def _frame(self, c, t_out: float):
        """A still of the clip at t_out seconds into the export (None for audio-only clips)."""
        if not self.win._has_video(c.source):
            return None
        t_file = c.start + t_out * max(0.25, c.speed)
        key = (c.source, round(t_file, 1))
        if key not in self._frames:
            out = self._tmp / f"f{len(self._frames)}.jpg"
            try:
                ft.extract_frame(c.source, t_file, str(out), width=960)
                self._frames[key] = QPixmap(str(out))
            except Exception:
                self._frames[key] = None
        return self._frames[key]

    # ------------------------------------------------------------------ fill
    def refresh(self, keep_row: int | None = None):
        """Called when the tab is shown or the moments change."""
        keep = self.moment.currentData()
        self.moment.blockSignals(True)
        self.moment.clear()
        for i, c in enumerate(self.project.clips):
            n_ok = sum(1 for o in c.overlays if o.status == "approved")
            n_p = sum(1 for o in c.overlays if o.status == "pending")
            a = self.project.to_stream(c.start + c.offset)
            tick = "☑" if c.enabled else "☐"
            pics = f"   ·   🖼 {n_ok} approved" + (f", {n_p} to review" if n_p else "") if (n_ok or n_p) else ""
            self.moment.addItem(f"{tick} {i + 1}. {c.title}   ({fmt_time(a)}, {c.out_length:.0f}s){pics}", id(c))
        idx = self.moment.findData(keep) if keep is not None else -1
        if idx < 0 and self.win.current_clip() is not None:
            idx = self.moment.findData(id(self.win.current_clip()))
        self.moment.setCurrentIndex(max(0, idx) if self.moment.count() else -1)
        self.moment.blockSignals(False)
        has = self.moment.count() > 0
        busy = self._worker is not None and self._worker.isRunning()
        self.btn_find.setEnabled(has and not busy)
        self.btn_find_all.setEnabled(has and not busy and any(c.enabled for c in self.project.clips))
        self._update_source_hints()
        self._fill_suggestions(keep_row=keep_row)

    def select_clip(self, c):
        i = self.moment.findData(id(c))
        if i >= 0:
            self.moment.setCurrentIndex(i)

    def _moment_changed(self):
        self._fill_suggestions()

    def _fill_suggestions(self, keep_row: int | None = None):
        c = self.current_clip()
        self.sugg.blockSignals(True)
        self.sugg.clear()
        if c is None:
            it = QListWidgetItem("No moments yet - find highlights in the Clip studio first.")
            it.setFlags(Qt.NoItemFlags)
            self.sugg.addItem(it)
        elif not c.overlays:
            it = QListWidgetItem("No pictures for this moment yet.\n\nPress ✨ Find pictures: RumbleCut listens "
                                 "to what's said and suggests pictures for the people, places and things "
                                 "mentioned. You approve each one.")
            it.setFlags(Qt.NoItemFlags)
            self.sugg.addItem(it)
        else:
            for o in c.overlays:
                it = QListWidgetItem(self._sugg_text(o))
                th = (o.candidates[o.chosen].get("extra", {}).get("thumb_path")
                      if o.candidates and 0 <= o.chosen < len(o.candidates) else None) or o.image
                pm = self._pixmap(th, 256)
                if not pm.isNull():
                    it.setIcon(QIcon(pm))
                self._style_item(it, o)
                self.sugg.addItem(it)
        self.sugg.blockSignals(False)
        if c is not None and c.overlays:
            r = keep_row if keep_row is not None else 0
            self.sugg.setCurrentRow(max(0, min(r, len(c.overlays) - 1)))
        else:
            self._sugg_selected(-1)
        has_p = bool(c and any(o.status == "pending" for o in c.overlays))
        self.btn_approve_all.setEnabled(has_p)
        self.btn_clear.setEnabled(has_p)

    def _sugg_text(self, o) -> str:
        name = o.label or Path(o.image).stem
        state = "✓ approved" if o.status == "approved" else "⏳ to review"
        look = "full screen" if o.style == "full" else o.position
        lic = ""
        if o.candidates and 0 <= o.chosen < len(o.candidates):
            cd = o.candidates[o.chosen]
            lic = f"\n{ci.SOURCES.get(cd.get('source'), cd.get('source', ''))} · {cd.get('licence', '')}"
        return f"{fmt_time(o.start)} · {name}\n{state} · {look} · {o.duration:.0f}s{lic}"

    @staticmethod
    def _style_item(it, o):
        if o.status == "approved":
            it.setForeground(QBrush(QColor("#c7ef9e")))
        else:
            it.setForeground(QBrush(QColor("#f5d48a")))
            it.setBackground(QBrush(QColor("#2a2414")))

    def _update_item(self):
        c, r = self.current_clip(), self.sugg.currentRow()
        if c and 0 <= r < len(c.overlays) and self.sugg.item(r):
            it = self.sugg.item(r)
            it.setText(self._sugg_text(c.overlays[r]))
            self._style_item(it, c.overlays[r])

    # ------------------------------------------------------------------ selection / editing
    def _sugg_selected(self, r):
        c = self.current_clip()
        o = c.overlays[r] if (c and 0 <= r < len(c.overlays)) else None
        for w in self._editors:
            w.setEnabled(o is not None)
        self._loading = True
        self.alts.clear()
        if o is None:
            self.preview.set_content(None, None, None, "")
            self.credit.setText("")
            self.alt_label.setText("")
            self.e_label.clear(); self.e_query.clear()
            self._loading = False
            return
        self.e_label.setText(o.label)
        self.e_show_label.setChecked(o.show_label)
        self.e_style.setCurrentIndex(max(0, self.e_style.findData(o.style)))
        self.e_pos.setCurrentText(o.position if o.position in POSITIONS else "top-right")
        self.e_pos.setEnabled(o.style != "full")
        self.e_scale.setEnabled(o.style != "full")
        self.e_show_label.setEnabled(o.style != "full")
        self.e_start.setMaximum(max(0.5, c.out_length - 0.5))
        self.e_start.setValue(o.start)
        self.e_dur.setValue(o.duration)
        self.e_scale.setValue(int(round(o.scale * 100)))
        self.e_query.setText(o.query or o.label)
        self.btn_approve.setText("✓ Approved" if o.status == "approved" else "✓ Approve")
        self.btn_approve.setEnabled(o.status != "approved")
        for i, cd in enumerate(o.candidates):
            th = cd.get("extra", {}).get("thumb_path")
            if not th:
                continue
            it = QListWidgetItem(QIcon(self._pixmap(th, 256)), "✓ in use" if i == o.chosen else "")
            it.setData(Qt.UserRole, i)
            it.setToolTip(f"{cd.get('title', '')}\n{ci.SOURCES.get(cd.get('source'), cd.get('source', ''))} · "
                          f"{cd.get('licence', '')}\nby {cd.get('credit') or 'unknown'}")
            self.alts.addItem(it)
        self.alt_label.setText("Other pictures for this - click one to use it" if self.alts.count() > 1
                               else ("Only one picture found - search again for more" if o.candidates else
                                     "Your own picture"))
        self._show_credit(o)
        self._loading = False
        self._refresh_preview()

    def _show_credit(self, o):
        if o.candidates and 0 <= o.chosen < len(o.candidates):
            cd = o.candidates[o.chosen]
            page = cd.get("extra", {}).get("file_page") or cd.get("page_url", "")
            lic = cd.get("licence", "")
            warn = " ⚠" if cd.get("source") == "web" else ""
            link = f" · <a href='{page}' style='color:#85C742'>source</a>" if page else ""
            why = f"<br>Said here: {o.why}" if o.why else ""
            self.credit.setText(f"<b>{cd.get('title', '')}</b> - by {cd.get('credit') or 'unknown'} · "
                                f"{lic}{warn}{link}{why}")
        else:
            self.credit.setText(o.source or "")

    def _refresh_preview(self):
        c, o = self.current_clip(), self.current_overlay()
        if not o:
            return
        frame = self._frame(c, o.start + min(1.0, o.duration / 2))
        note = "" if frame else "🔊 audio only - no picture of the stream to show behind it"
        self.preview.set_content(frame, self._pixmap(o.image), o, note)

    def _set(self, attr, val):
        if self._loading:
            return
        o = self.current_overlay()
        if not o:
            return
        setattr(o, attr, val)
        if attr == "style":
            self.e_pos.setEnabled(val != "full")
            self.e_scale.setEnabled(val != "full")
            self.e_show_label.setEnabled(val != "full")
        self._update_item()
        self._refresh_preview()
        self.win._overlays_changed()

    def _alt_clicked(self, it):
        o = self.current_overlay()
        i = it.data(Qt.UserRole)
        if o is None or i is None or i == o.chosen:
            return
        try:
            ci.use_candidate(o, i)
        except ci.ImageSearchError as e:
            QMessageBox.warning(self, "Context images", f"Couldn't use that picture: {e}")
            return
        r = self.sugg.currentRow()
        self._fill_suggestions(keep_row=r)
        self.win._overlays_changed()

    def approve(self):
        o = self.current_overlay()
        if not o:
            return
        o.status = "approved"
        r = self.sugg.currentRow()
        c = self.current_clip()
        nxt = next((k for k in range(r + 1, len(c.overlays)) if c.overlays[k].status == "pending"), r)
        self._after_change(nxt)

    def approve_all(self):
        c = self.current_clip()
        if not c:
            return
        for o in c.overlays:
            o.status = "approved"
        self._after_change(self.sugg.currentRow())

    def remove(self):
        c, r = self.current_clip(), self.sugg.currentRow()
        if c and 0 <= r < len(c.overlays):
            del c.overlays[r]
            self._after_change(r)

    def clear_pending(self):
        c = self.current_clip()
        if c:
            c.overlays = [o for o in c.overlays if o.status != "pending"]
            self._after_change(0)

    def _after_change(self, keep_row: int | None = None):
        self.win._overlays_changed()
        self.refresh(keep_row)

    def play_here(self):
        c, o = self.current_clip(), self.current_overlay()
        if not (c and o):
            return
        self.win.tabs.setCurrentIndex(1)
        r = self.project.clips.index(c)
        self.win.clip_list.setCurrentRow(r)
        self.win.seek_clip(c, c.start + o.start * max(0.25, c.speed))
        if self.win.player:
            self.win.player.play()

    # ------------------------------------------------------------------ finding
    def _check_ready(self) -> bool:
        if not self.sources():
            QMessageBox.information(self, "Context images", "Tick at least one place to look for pictures.")
            return False
        usable = [k for k in self.sources() if k == "wikimedia" or ci.get_key(k)]
        if not usable:
            QMessageBox.information(self, "Context images", "The sources you ticked need API keys - add them in "
                                    "Settings → Context images, or tick Wikipedia / Wikimedia (no key needed).")
            return False
        if not transcribe.available():
            QMessageBox.information(self, "Context images", "Speech-to-text isn't included in this copy of "
                                    "RumbleCut. Install the latest version from Settings → Updates.")
            return False
        return True

    def _transcript(self, c, cancel, on_progress):
        key = (c.source, round(c.start, 1), round(c.end, 1))
        if key not in self._transcripts:
            self._transcripts[key] = transcribe.transcribe(c.source, start=c.start, end=c.end,
                                                           cancel=cancel, on_progress=on_progress)
        return self._transcripts[key]

    def find(self, all_ticked: bool = False):
        if self._worker is not None and self._worker.isRunning():
            return
        targets = ([c for c in self.project.clips if c.enabled] if all_ticked else
                   [c for c in [self.current_clip()] if c])
        if not targets or not self._check_ready():
            return
        sources = self.sources()
        n = self.per_clip.value()
        gap = float(config.load_settings().get("ctx_gap", 6))
        guidance = getattr(self.win, "guidance_edit", None)
        guidance = guidance.text().strip() if guidance is not None else ""
        use_ai = ai.is_configured()

        def job(on_progress=None, cancel=None):
            results = []
            for i, c in enumerate(targets):
                if cancel():
                    break
                base = i / len(targets)
                pre = f"Moment {self.project.clips.index(c) + 1}" if len(targets) > 1 else "This moment"

                def step(f, m, base=base, pre=pre):
                    on_progress(base + max(0.0, f) / len(targets), f"{pre}: {m}")
                step(0.0, "listening to what's said…")
                segs = self._transcript(c, cancel, lambda f, m: step(max(0, f) * 0.3, m))
                ovs = ci.suggest_for_clip(
                    c, segs, sources, n=n, gap=gap, guidance=guidance, use_ai=use_ai,
                    on_progress=lambda f, m: step(0.3 + 0.7 * f, m), cancel=cancel,
                    on_log=lambda line: w.log.emit(line))
                results.append((c, ovs))
            return results

        w = Worker(job)
        self._worker = w
        self.progress.setValue(0)
        self.btn_cancel.setText("Cancel")
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.show()
        self.btn_find.setEnabled(False)
        self.btn_find_all.setEnabled(False)
        w.progress.connect(lambda f, m: (self.progress.setValue(int(max(f, 0) * 1000)), self.status.setText(m)))
        w.log.connect(self.win._ai_log_line)

        def done(results):
            total = 0
            for c, ovs in results:
                # new suggestions replace old unapproved ones; approved and your own pictures stay,
                # and a suggestion that lands on top of one of those is dropped
                kept = [o for o in c.overlays if o.status != "pending"]
                ovs = [o for o in ovs if not any(abs(o.start - k.start) < max(2.0, gap * 0.8) or
                                                 (o.label and o.label.lower() == k.label.lower()) for k in kept)]
                c.overlays = kept + ovs
                c.overlays.sort(key=lambda o: o.start)
                total += len(ovs)
            self.progress.setValue(1000)
            self.status.setText(f"✨ {total} picture{'s' if total != 1 else ''} to review" if total else
                                "Nothing to picture found - try other sources, or add your own in the Clip studio")
            self._after_change(0)

        def failed(msg):
            self.status.setText("⚠ " + msg)
            QMessageBox.warning(self, "Context images", msg)

        def finished():
            self._worker = None
            self.btn_cancel.hide()
            self.refresh()

        w.done.connect(done)
        w.failed.connect(failed)
        w.cancelled.connect(lambda: self.status.setText("Cancelled"))
        w.finished.connect(finished)
        self.win._start(w)

    def _cancel(self):
        if self._worker is not None:
            self._worker.cancel()
            self.btn_cancel.setEnabled(False)
            self.btn_cancel.setText("Stopping…")
            self.status.setText("Stopping - finishing the step in progress…")

    def search_again(self):
        o = self.current_overlay()
        q = self.e_query.text().strip()
        if not o or not q or (self._worker is not None and self._worker.isRunning()):
            return
        if not self._check_ready():
            return
        sources = self.sources()
        r = self.sugg.currentRow()

        def job(on_progress=None, cancel=None):
            on_progress(-1.0, f"Searching for “{q}”…")
            ci.research(o, q, sources, on_log=lambda line: w.log.emit(line))
            return True

        w = Worker(job)
        self._worker = w
        self.btn_search.setEnabled(False)
        self.progress.setRange(0, 0)
        w.progress.connect(lambda f, m: self.status.setText(m))
        w.log.connect(self.win._ai_log_line)

        def done(_):
            self.status.setText(f"Found {len(o.candidates)} picture(s) for “{q}”")
            self._fill_suggestions(keep_row=r)
            self.win._overlays_changed()

        def finished():
            self._worker = None
            self.progress.setRange(0, 1000)
            self.btn_search.setEnabled(True)

        w.done.connect(done)
        w.failed.connect(lambda m: (self.status.setText("⚠ " + m),
                                    QMessageBox.warning(self, "Context images", m)))
        w.finished.connect(finished)
        self.win._start(w)
