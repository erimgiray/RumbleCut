"""✂ Script: edit a clip by deleting words from its transcript."""
from __future__ import annotations

import copy
import os

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QShortcut, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton, QSizePolicy, QSplitter,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .. import ai, script_edit as se, transcribe
from .timeline import fmt_time
from .workers import Worker

try:
    if os.environ.get("RUMBLECUT_NO_PREVIEW"):
        raise ImportError
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAVE_MM = True
except Exception:
    HAVE_MM = False

COL = {"word": "#e5e7eb", "cut": "#5b6270", "pause": "#60a5fa", "unheard": "#c084fc",
       "sugg_fg": "#f5d48a", "sugg_bg": "#3a2f12", "ai_bg": "#3b2614", "now": "#2d3a22"}


class ScriptView(QTextEdit):
    """The clip's words as text. Select words and press Delete to cut them."""
    cut_requested = Signal()
    restore_requested = Signal()
    word_clicked = Signal(int)          # token index
    toggle_requested = Signal(int)

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        f = QFont(self.font())
        f.setPointSizeF(f.pointSizeF() * 1.4)
        self.setFont(f)
        self.spans: list[tuple[int, int, int]] = []      # (char start, char end, token index)

    def token_at(self, pos: int) -> int | None:
        for a, b, k in self.spans:
            if a <= pos < b:
                return k
        return None

    def selected_tokens(self) -> list[int]:
        c = self.textCursor()
        if not c.hasSelection():
            k = self.token_at(c.position())
            return [k] if k is not None else []
        a, b = c.selectionStart(), c.selectionEnd()
        return [k for x, y, k in self.spans if y > a and x < b]

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.cut_requested.emit()
            return
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Insert):
            self.restore_requested.emit()
            return
        super().keyPressEvent(ev)

    def mouseReleaseEvent(self, ev):
        super().mouseReleaseEvent(ev)
        if ev.button() == Qt.LeftButton and not self.textCursor().hasSelection():
            k = self.token_at(self.cursorForPosition(ev.position().toPoint()).position())
            if k is not None:
                self.word_clicked.emit(k)

    def mouseDoubleClickEvent(self, ev):
        k = self.token_at(self.cursorForPosition(ev.position().toPoint()).position())
        if k is not None:
            self.toggle_requested.emit(k)


class ScriptPanel(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._worker: Worker | None = None
        self._quiet: dict = {}             # id(clip) -> se.Quiet
        self._undo: list = []
        self._redo: list = []
        self._toks: list[dict] = []
        self._stop_at = None
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(8)

        top = QHBoxLayout()
        top.addWidget(QLabel("Moment"))
        self.moment = QComboBox()
        self.moment.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.moment.currentIndexChanged.connect(lambda _: self._moment_changed())
        top.addWidget(self.moment, 1)
        self.btn_load = QPushButton("📝 Get the script")
        self.btn_load.setObjectName("primary")
        self.btn_load.setToolTip("Write out every word of this moment with its exact timing")
        self.btn_load.clicked.connect(self.load_script)
        top.addWidget(self.btn_load)
        v.addLayout(top)

        tools = QHBoxLayout()
        self.btn_clean = QPushButton("🧹 Quick clean-up")
        self.btn_clean.setToolTip("Suggest cutting “um”/“uh”, stutters and repeats, long pauses, and bits "
                                  "the script missed (usually fillers). You approve them.")
        self.btn_clean.clicked.connect(self.quick_cleanup)
        self.btn_ai = QPushButton("✨ Tighten with AI")
        self.btn_ai.setToolTip("Your AI suggests asides, false starts and repeated points to cut")
        self.btn_ai.clicked.connect(self.ai_tighten)
        self.btn_accept_all = QPushButton("✓ Accept all suggestions")
        self.btn_accept_all.clicked.connect(lambda: self._accept(all_=True))
        self.btn_dismiss_all = QPushButton("Dismiss suggestions")
        self.btn_dismiss_all.clicked.connect(lambda: self._dismiss(all_=True))
        self.jump = QComboBox()
        self.jump.addItem("Jump cuts: punch-in zoom", "punch")
        self.jump.addItem("Jump cuts: hard cut", "hard")
        self.jump.setToolTip("Punch-in zooms in a little on every other piece, so cuts look like a camera "
                             "change instead of a twitch")
        self.jump.currentIndexChanged.connect(self._jump_changed)
        self.btn_undo = QPushButton("↶ Undo")
        self.btn_undo.clicked.connect(self.undo)
        self.btn_redo = QPushButton("↷ Redo")
        self.btn_redo.clicked.connect(self.redo)
        for b in (self.btn_clean, self.btn_ai, self.btn_accept_all, self.btn_dismiss_all):
            tools.addWidget(b)
        tools.addStretch()
        tools.addWidget(self.jump)
        tools.addWidget(self.btn_undo)
        tools.addWidget(self.btn_redo)
        v.addLayout(tools)

        prow = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setFixedHeight(18)
        self.progress.setFormat("")
        self.progress.valueChanged.connect(
            lambda x: self.progress.setFormat(f"{x / 10:.0f}%" if x > 0 and self.progress.maximum() else ""))
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        prow.addWidget(self.progress, 1)
        prow.addWidget(self.status, 3)
        v.addLayout(prow)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.view = ScriptView()
        self.view.cut_requested.connect(self.cut_selected)
        self.view.restore_requested.connect(self.restore_selected)
        self.view.word_clicked.connect(self._token_clicked)
        self.view.toggle_requested.connect(self._toggle_token)
        lv.addWidget(self.view, 1)
        erow = QHBoxLayout()
        self.btn_cut = QPushButton("✂ Cut selected  (Del)")
        self.btn_cut.clicked.connect(self.cut_selected)
        self.btn_restore = QPushButton("↩ Restore selected  (Enter)")
        self.btn_restore.clicked.connect(self.restore_selected)
        self.btn_accept = QPushButton("✓ Accept")
        self.btn_accept.setToolTip("Accept the suggestions you've selected")
        self.btn_accept.clicked.connect(lambda: self._accept(all_=False))
        self.btn_reject = QPushButton("✖ Keep")
        self.btn_reject.setToolTip("Dismiss the suggestions you've selected (keep those words)")
        self.btn_reject.clicked.connect(lambda: self._dismiss(all_=False))
        for b in (self.btn_cut, self.btn_restore, self.btn_accept, self.btn_reject):
            erow.addWidget(b)
        erow.addStretch()
        lv.addLayout(erow)
        self.legend = QLabel(
            f"<span style='color:{COL['word']}'>kept</span> · <s style='color:{COL['cut']}'>cut</s> · "
            f"<span style='color:{COL['sugg_fg']};background:{COL['sugg_bg']}'>&nbsp;suggested cut&nbsp;</span> · "
            f"<span style='color:{COL['pause']}'>⏸ pause</span> · "
            f"<span style='color:{COL['unheard']}'>[… not in the script]</span>   —   select words and press "
            "Delete to cut, Enter to bring them back · click a word to play from it · double-click to toggle")
        self.legend.setObjectName("muted")
        self.legend.setWordWrap(True)
        lv.addWidget(self.legend)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.stack.setMinimumSize(320, 180)
        self.note = QLabel("")
        self.note.setAlignment(Qt.AlignCenter)
        self.note.setWordWrap(True)
        self.note.setObjectName("novideo")
        if HAVE_MM:
            self.video = QVideoWidget()
            self.player = QMediaPlayer(self)
            if not os.environ.get("RUMBLECUT_NO_AUDIO"):
                self.audio = QAudioOutput(self)
                self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video)
            self.player.positionChanged.connect(self._pos)
            self.stack.addWidget(self.video)
        else:
            self.player = None
        self.stack.addWidget(self.note)
        rv.addWidget(self.stack, 1)
        trow = QHBoxLayout()
        self.btn_play = QPushButton("▶ Play the edit")
        self.btn_play.setObjectName("primary")
        self.btn_play.setToolTip("Plays the moment skipping everything you've cut")
        self.btn_play.clicked.connect(self.play_toggle)
        self.pos_label = QLabel("")
        self.pos_label.setObjectName("muted")
        trow.addWidget(self.btn_play)
        trow.addWidget(self.pos_label, 1)
        rv.addLayout(trow)
        self.lengths = QLabel("")
        self.lengths.setWordWrap(True)
        rv.addWidget(self.lengths)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([720, 480])
        v.addWidget(split, 1)

        QShortcut(QKeySequence.Undo, self, activated=self.undo)
        QShortcut(QKeySequence.Redo, self, activated=self.redo)
        self._moment_changed()

    # ------------------------------------------------------------------ basics
    @property
    def project(self):
        return self.win.project

    def current_clip(self):
        cid = self.moment.currentData()
        return next((c for c in self.project.clips if id(c) == cid), None)

    def refresh(self):
        keep = self.moment.currentData()
        self.moment.blockSignals(True)
        self.moment.clear()
        for i, c in enumerate(self.project.clips):
            a = self.project.to_stream(c.start + c.offset)
            cut = f"   ·   ✂ {c.length / max(.25, c.speed):.0f}s → {c.out_length:.0f}s" if c.cuts else ""
            self.moment.addItem(f"{'☑' if c.enabled else '☐'} {i + 1}. {c.title}   ({fmt_time(a)}){cut}", id(c))
        idx = self.moment.findData(keep) if keep is not None else -1
        if idx < 0 and self.win.current_clip() is not None:
            idx = self.moment.findData(id(self.win.current_clip()))
        self.moment.setCurrentIndex(max(0, idx) if self.moment.count() else -1)
        self.moment.blockSignals(False)
        self._moment_changed()

    def select_clip(self, c):
        i = self.moment.findData(id(c))
        if i >= 0:
            self.moment.setCurrentIndex(i)

    def _moment_changed(self):
        c = self.current_clip()
        self._undo.clear()
        self._redo.clear()
        self._stop()
        if c is not None:
            self.jump.blockSignals(True)
            self.jump.setCurrentIndex(max(0, self.jump.findData(c.jump_cut)))
            self.jump.blockSignals(False)
            if self.player and os.path.exists(c.source):
                self.player.setSource(QUrl.fromLocalFile(c.source))
                self.player.setPosition(int(c.start * 1000))
            has_v = self.win._has_video(c.source) if c.source else False
            self.note.setText("<p style='font-size:36px;margin:0'>🔊</p>Audio only - you'll hear the edit"
                              if not has_v else "")
            self.stack.setCurrentIndex(0 if (self.player and has_v) else self.stack.count() - 1)
        self._render()
        self._update_buttons()

    def _update_buttons(self):
        c = self.current_clip()
        has_words = bool(c and c.words)
        busy = self._worker is not None and self._worker.isRunning()
        self.btn_load.setEnabled(c is not None and not busy)
        self.btn_load.setText("📝 Get the script again" if has_words else "📝 Get the script")
        for b in (self.btn_clean, self.btn_ai, self.btn_cut, self.btn_restore, self.btn_play):
            b.setEnabled(has_words and not busy)
        has_s = bool(c and c.cut_suggestions)
        for b in (self.btn_accept_all, self.btn_dismiss_all, self.btn_accept, self.btn_reject):
            b.setEnabled(has_s and not busy)
        self.btn_undo.setEnabled(bool(self._undo))
        self.btn_redo.setEnabled(bool(self._redo))

    def _quiet_for(self, c) -> se.Quiet:
        q = self._quiet.get(id(c))
        if q is None or getattr(q, "_span", None) != (c.source, c.start, c.end):
            try:
                t, db = se.energy(c.source, c.start, c.end)
                q = se.Quiet(t, db)
            except Exception:
                q = se.Quiet()
            q._span = (c.source, c.start, c.end)
            self._quiet[id(c)] = q
        return q

    # ------------------------------------------------------------------ the text
    def _render(self):
        c = self.current_clip()
        self.view.clear()
        self.view.spans = []
        self._toks = []
        if c is None:
            self.view.setPlainText("No moments yet - find highlights in the Clip studio first.")
            self.lengths.setText("")
            return
        if not c.words:
            self.view.setPlainText("Press “📝 Get the script” to write out every word of this moment.\n\n"
                                   "Then select words and press Delete to cut them from the clip - the video "
                                   "follows the text. 🧹 Quick clean-up and ✨ Tighten with AI suggest cuts "
                                   "for you to accept or keep.")
            self._lengths()
            return
        q = self._quiet.get(id(c))
        self._toks = se.tokens(c, q)
        sugg = c.cut_suggestions
        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.Start)
        base = QTextCharFormat()
        base.setForeground(QBrush(QColor(COL["word"])))
        for k, tk in enumerate(self._toks):
            fmt = QTextCharFormat(base)
            a, b = tk["start"], tk["end"]
            if tk["kind"] == "pause":
                fmt.setForeground(QBrush(QColor(COL["pause"])))
                fmt.setFontItalic(True)
                ca, cb = se.pause_cut_range(a, b)
                cut = se.is_cut(c, ca, cb)
            else:
                cut = se.is_cut(c, a, b)
                if tk["kind"] == "unheard":
                    fmt.setForeground(QBrush(QColor(COL["unheard"])))
            sg = next((s for s in sugg if s["start"] - 0.02 <= (a + b) / 2 <= s["end"] + 0.02
                       or (tk["kind"] == "pause" and s["kind"] == "pause" and a <= s["start"] <= b)), None)
            if cut:
                fmt.setForeground(QBrush(QColor(COL["cut"])))
                fmt.setFontStrikeOut(True)
            elif sg:
                fmt.setForeground(QBrush(QColor(COL["sugg_fg"])))
                fmt.setBackground(QBrush(QColor(COL["ai_bg"] if sg["kind"] == "ai" else COL["sugg_bg"])))
                fmt.setFontStrikeOut(True)
                fmt.setToolTip(se.KIND_NAMES.get(sg["kind"], sg["kind"]) + (f" - {sg['why']}" if sg.get("why") else ""))
            start = cur.position()
            cur.insertText(tk["text"], fmt)
            self.view.spans.append((start, cur.position(), k))
            cur.insertText(" ", base)
        self.view.moveCursor(QTextCursor.Start)
        self._lengths()

    def _lengths(self):
        c = self.current_clip()
        if not c:
            self.lengths.setText("")
            return
        full = c.length / max(0.25, c.speed)
        now = c.out_length
        nsug = len(c.cut_suggestions)
        sug_s = sum(s["end"] - s["start"] for s in c.cut_suggestions) / max(0.25, c.speed)
        txt = f"<b>{full:.1f}s → {now:.1f}s</b>"
        if c.cuts:
            n = len(c.cuts)
            txt += f"  (cut {full - now:.1f}s in {n} place{'s' if n != 1 else ''})"
        if nsug:
            txt += f"<br><span style='color:{COL['sugg_fg']}'>{nsug} suggestion{'s' if nsug != 1 else ''} " \
                   f"would save another {sug_s:.1f}s</span>"
        self.lengths.setText(txt)

    # ------------------------------------------------------------------ edits
    def _snapshot(self):
        c = self.current_clip()
        self._undo.append((copy.deepcopy(c.cuts), copy.deepcopy(c.cut_suggestions)))
        self._undo = self._undo[-100:]
        self._redo.clear()

    def _changed(self):
        c = self.current_clip()
        self._render()
        self._update_buttons()
        self.win._clip_edited(c)

    def _ranges_for(self, ks: list[int]) -> list[tuple[float, float]]:
        """File-time ranges covering the given tokens (words grouped so cuts land between words)."""
        c = self.current_clip()
        q = self._quiet_for(c)
        words = [(float(a), float(b), str(w)) for a, b, w in c.words if b > c.start and a < c.end]
        out, run = [], []
        for k in sorted(ks) + [None]:
            tk = self._toks[k] if k is not None else None
            if tk is not None and tk["kind"] == "word" and (not run or tk["i"] == run[-1] + 1):
                run.append(tk["i"])
                continue
            if run:
                out.append(se.word_cut_range(words, run[0], run[-1], c, q))
                run = []
            if tk is None:
                break
            if tk["kind"] == "word":
                run = [tk["i"]]
            elif tk["kind"] == "pause":
                out.append(se.pause_cut_range(tk["start"], tk["end"]))
            else:
                out.append((tk["start"], tk["end"]))
        return out

    def cut_selected(self):
        c = self.current_clip()
        ks = self.view.selected_tokens()
        if not (c and ks):
            return
        self._snapshot()
        rs = self._ranges_for(ks)
        se.apply_cuts(c, list(c.cuts) + [list(r) for r in rs])
        c.cut_suggestions = [s for s in c.cut_suggestions if not se.is_cut(c, s["start"], s["end"])]
        self._changed()

    def restore_selected(self):
        c = self.current_clip()
        ks = self.view.selected_tokens()
        if not (c and ks):
            return
        self._snapshot()
        cuts = [list(x) for x in c.cuts]
        for a, b in self._ranges_for(ks):
            cuts = se.subtract(cuts, a, b)
        se.apply_cuts(c, cuts)
        self._changed()

    def _toggle_token(self, k):
        c = self.current_clip()
        tk = self._toks[k]
        a, b = (se.pause_cut_range(tk["start"], tk["end"]) if tk["kind"] == "pause" else (tk["start"], tk["end"]))
        cur = self.view.textCursor()
        x, y, _ = self.view.spans[k]
        cur.setPosition(x)
        cur.setPosition(y, QTextCursor.KeepAnchor)
        self.view.setTextCursor(cur)
        if se.is_cut(c, a, b):
            self.restore_selected()
        else:
            sg = self._suggestions_in([k])
            if sg:
                self._accept(all_=False)
            else:
                self.cut_selected()

    def _suggestions_in(self, ks):
        c = self.current_clip()
        out = []
        for k in ks:
            tk = self._toks[k]
            mid = (tk["start"] + tk["end"]) / 2
            for s in c.cut_suggestions:
                hit = s["start"] - 0.02 <= mid <= s["end"] + 0.02 or (
                    tk["kind"] == "pause" and s["kind"] == "pause" and tk["start"] <= s["start"] <= tk["end"])
                if hit and s not in out:
                    out.append(s)
        return out

    def _accept(self, all_: bool):
        c = self.current_clip()
        if not c:
            return
        sel = list(c.cut_suggestions) if all_ else self._suggestions_in(self.view.selected_tokens())
        if not sel:
            return
        self._snapshot()
        se.apply_cuts(c, list(c.cuts) + [[s["start"], s["end"]] for s in sel])
        c.cut_suggestions = [s for s in c.cut_suggestions if s not in sel]
        self._changed()

    def _dismiss(self, all_: bool):
        c = self.current_clip()
        if not c:
            return
        sel = list(c.cut_suggestions) if all_ else self._suggestions_in(self.view.selected_tokens())
        if not sel:
            return
        self._snapshot()
        c.cut_suggestions = [s for s in c.cut_suggestions if s not in sel]
        self._changed()

    def undo(self):
        c = self.current_clip()
        if not (c and self._undo):
            return
        self._redo.append((copy.deepcopy(c.cuts), copy.deepcopy(c.cut_suggestions)))
        cuts, sugg = self._undo.pop()
        se.apply_cuts(c, cuts)
        c.cut_suggestions = sugg
        self._changed()

    def redo(self):
        c = self.current_clip()
        if not (c and self._redo):
            return
        self._undo.append((copy.deepcopy(c.cuts), copy.deepcopy(c.cut_suggestions)))
        cuts, sugg = self._redo.pop()
        se.apply_cuts(c, cuts)
        c.cut_suggestions = sugg
        self._changed()

    def _jump_changed(self):
        c = self.current_clip()
        if c:
            c.jump_cut = self.jump.currentData()
            self.win._clip_edited(c)

    # ------------------------------------------------------------------ background jobs
    def _run(self, job, done, label):
        w = Worker(job)
        self._worker = w
        self.progress.setValue(0)
        self.status.setText(label)
        w.progress.connect(lambda f, m: (self.progress.setValue(int(max(f, 0) * 1000)), self.status.setText(m)))
        w.done.connect(done)
        w.failed.connect(lambda m: (self.status.setText("⚠ " + m), QMessageBox.warning(self, "Script", m)))
        w.cancelled.connect(lambda: self.status.setText("Cancelled"))

        def finished():
            self._worker = None
            self._update_buttons()
        w.finished.connect(finished)
        self._update_buttons()
        self.win._start(w)

    def load_script(self):
        c = self.current_clip()
        if not c or (self._worker is not None and self._worker.isRunning()):
            return
        if not transcribe.available():
            QMessageBox.information(self, "Script", "Speech-to-text isn't included in this copy of RumbleCut.")
            return
        if c.cuts and QMessageBox.question(
                self, "Script", "Get the script again? Your cuts stay where they are in the clip.") != QMessageBox.Yes:
            return

        def job(on_progress=None, cancel=None):
            ws = transcribe.transcribe_words(c.source, c.start, c.end, on_progress=on_progress, cancel=cancel)
            on_progress(0.98, "Measuring the pauses…")
            t, db = se.energy(c.source, c.start, c.end)
            return ws, t, db

        def done(r):
            ws, t, db = r
            c.words = [[round(a, 3), round(b, 3), w] for a, b, w in ws]
            q = se.Quiet(t, db)
            q._span = (c.source, c.start, c.end)
            self._quiet[id(c)] = q
            self.progress.setValue(1000)
            self.status.setText(f"📝 {len(ws)} words" if ws else "No speech found in this moment")
            self._render()
            self._update_buttons()
            self.win._clip_edited(c)
        self._run(job, done, "Writing out every word…")

    def quick_cleanup(self):
        c = self.current_clip()
        if not (c and c.words):
            return
        self._snapshot()
        new = se.quick_cleanup(c, self._quiet_for(c))
        keep = [s for s in c.cut_suggestions if s["kind"] == "ai"]
        c.cut_suggestions = sorted(keep + [s for s in new if not any(
            not (s["end"] <= k["start"] or s["start"] >= k["end"]) for k in keep)], key=lambda s: s["start"])
        n = len(new)
        kinds = {}
        for s in new:
            kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
        self.status.setText(("🧹 " + ", ".join(f"{v} {se.KIND_NAMES[k]}{'s' if v != 1 and k != 'unheard' else ''}"
                                               for k, v in kinds.items())) if n else "🧹 Nothing to clean up")
        self._changed()

    def ai_tighten(self):
        c = self.current_clip()
        if not (c and c.words) or (self._worker is not None and self._worker.isRunning()):
            return
        if not ai.is_configured():
            QMessageBox.information(self, "Script", "Set up an AI model in Settings → AI models first.")
            return
        q = self._quiet_for(c)
        guide = getattr(self.win, "guidance_edit", None)
        guide = guide.text().strip() if guide is not None else ""

        def job(on_progress=None, cancel=None):
            on_progress(-1.0, f"{ai.describe()} is reading the script…")
            return se.ai_tighten(c, q, guide)

        def done(sugg):
            self._snapshot()
            others = [s for s in c.cut_suggestions if s["kind"] != "ai"]
            others = [s for s in others if not any(not (s["end"] <= a["start"] or s["start"] >= a["end"]) for a in sugg)]
            c.cut_suggestions = sorted(others + sugg, key=lambda s: s["start"])
            self.progress.setRange(0, 1000)
            self.progress.setValue(1000)
            secs = sum(s["end"] - s["start"] for s in sugg)
            self.status.setText(f"✨ {len(sugg)} suggested cut{'s' if len(sugg) != 1 else ''} ({secs:.1f}s) - "
                                "hover them to see why" if sugg else "✨ Your AI thinks it's already tight")
            self._changed()
        self.progress.setRange(0, 0)
        self._run(job, done, "Reading the script…")
        self._worker.finished.connect(lambda: self.progress.setRange(0, 1000))

    # ------------------------------------------------------------------ live preview that skips cuts
    def play_toggle(self):
        if not self.player:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self._stop()
            return
        c = self.current_clip()
        if not c:
            return
        start = c.kept[0][0]
        self._play_from(start)

    def _play_from(self, t: float):
        c = self.current_clip()
        if not (self.player and c):
            return
        for a, b in c.cuts:
            if a <= t < b:
                t = b
        self._stop_at = c.end
        self.player.setPosition(int(t * 1000))
        self.player.play()
        self.btn_play.setText("■ Stop")

    def _stop(self):
        if self.player:
            self.player.pause()
        self._stop_at = None
        self.btn_play.setText("▶ Play the edit")
        self.view.setExtraSelections([])

    def _token_clicked(self, k):
        tk = self._toks[k]
        if self.player:
            self._play_from(tk["start"])

    def _pos(self, ms):
        c = self.current_clip()
        if not c or self._stop_at is None:
            return
        t = ms / 1000
        for a, b in c.cuts:
            if a <= t < b - 0.02:
                self.player.setPosition(int(b * 1000))       # skip what's been cut
                return
        if t >= self._stop_at:
            self._stop()
            return
        out = c.to_out(t)
        self.pos_label.setText(f"{fmt_time(out)} / {fmt_time(c.out_length)}")
        k = next((i for i, tk in enumerate(self._toks) if tk["start"] <= t <= tk["end"] + 0.05), None)
        if k is not None and k < len(self.view.spans):
            sel = QTextEdit.ExtraSelection()
            x, y, _ = self.view.spans[k]
            cur = self.view.textCursor()
            cur.setPosition(x)
            cur.setPosition(y, QTextCursor.KeepAnchor)
            sel.cursor = cur
            sel.format.setBackground(QBrush(QColor(COL["now"])))
            self.view.setExtraSelections([sel])
