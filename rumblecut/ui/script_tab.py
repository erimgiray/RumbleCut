"""✂ Script: edit a clip by deleting words from its transcript."""
from __future__ import annotations

import bisect
import copy
import os
import queue
import time

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QShortcut, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QMenu, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QTextEdit,
    QVBoxLayout, QWidget,
)

from .. import ai, editor, script_edit as se, transcribe
from .timeline import ClipTimeline, Timeline, _origin_colour, fmt_time
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
    time_clicked = Signal(float)        # a word outside the moment (project time)
    menu_requested = Signal(int, object)  # right-click: char position, global position

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        f = QFont(self.font())
        f.setPointSizeF(f.pointSizeF() * 1.4)
        self.setFont(f)
        self.spans: list[tuple[int, int, int]] = []      # (char start, char end, token index) - this moment
        self.wspans: list[tuple[int, int, float, float]] = []   # every word: (char a, char b, t0, t1) project time
        self._wkeys: list[int] = []

    def word_at(self, pos: int):
        """(index into wspans) of the word at a character position, or the nearest one before it."""
        if not self.wspans:
            return None
        if len(self._wkeys) != len(self.wspans):
            self._wkeys = [x[0] for x in self.wspans]
        i = bisect.bisect_right(self._wkeys, pos) - 1
        return max(0, i)

    def contextMenuEvent(self, ev):
        pos = self.cursorForPosition(ev.pos()).position()
        c = self.textCursor()
        if not (c.hasSelection() and c.selectionStart() <= pos <= c.selectionEnd()):
            c.setPosition(pos)
            self.setTextCursor(c)
        self.menu_requested.emit(pos, ev.globalPos())

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
            pos = self.cursorForPosition(ev.position().toPoint()).position()
            k = self.token_at(pos)
            if k is not None:
                self.word_clicked.emit(k)
                return
            i = self.word_at(pos)
            if i is not None and self.wspans[i][0] <= pos <= self.wspans[i][1]:
                self.time_clicked.emit(self.wspans[i][2])

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
        self._preview = False              # playing the rendered "with effects" preview
        self._loading = False
        self._push_clip = None
        self._script_q: queue.Queue = queue.Queue()      # written-out stretches arriving from the job
        self._drain = QTimer(self)
        self._drain.timeout.connect(self._drain_script)
        self._last_render = 0.0
        self._free_play = False             # playing the stream outside the moment
        self._push_timer = QTimer(self)
        self._push_timer.setSingleShot(True)
        self._push_timer.timeout.connect(self._do_push)
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
        self.btn_load.setToolTip("Write out every word of the whole stream with its exact timing - the moments "
                                 "first, then everything before and after. You can stop and carry on later.")
        self.btn_load.clicked.connect(self.load_script)
        top.addWidget(self.btn_load)
        v.addLayout(top)

        # where every moment sits in the stream (click one to switch, drag to move, right-click for options)
        self.overview = Timeline()
        self.overview.setMinimumHeight(60)
        self.overview.setFixedHeight(74)
        self.overview.clip_clicked.connect(self._overview_clicked)
        self.overview.clip_menu.connect(lambda i, t, g: self._menu(i, g, t))
        self.overview.clip_moved.connect(self._overview_moved)
        self.overview.clip_move_done.connect(lambda i, a, b, w: self._span_done())
        self.overview.add_at.connect(self._add_at)
        v.addWidget(self.overview)
        self.clip_tl_title = QLabel("")
        self.clip_tl_title.setObjectName("muted")
        v.addWidget(self.clip_tl_title)
        self.clip_tl = ClipTimeline()
        self.clip_tl.span_changed.connect(self._span_dragged)
        self.clip_tl.span_committed.connect(lambda a, b, w: self._span_done())
        self.clip_tl.seek.connect(self._tl_seek)
        self.clip_tl.menu_requested.connect(lambda t, g: self._menu(None, g, t))
        self.clip_tl.other_menu.connect(lambda i, t, g: self._menu(i, g, t))
        v.addWidget(self.clip_tl)

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
        self.view.time_clicked.connect(self._play_free)
        self.view.menu_requested.connect(self._word_menu)
        lv.addWidget(self.view, 1)
        brow = QHBoxLayout()
        self.btn_from = QPushButton("⟦ Start here")
        self.btn_from.setToolTip("Start the moment at the word you clicked (or the first selected word)")
        self.btn_from.clicked.connect(lambda: self._edge_from_cursor("start"))
        self.btn_to = QPushButton("End here ⟧")
        self.btn_to.setToolTip("End the moment after the word you clicked (or the last selected word)")
        self.btn_to.clicked.connect(lambda: self._edge_from_cursor("end"))
        self.btn_sel = QPushButton("⟦ Selection ⟧")
        self.btn_sel.setToolTip("Make the selected words the moment")
        self.btn_sel.clicked.connect(self._selection_to_moment)
        self.btn_find = QPushButton("⌖ Go to the moment")
        self.btn_find.clicked.connect(lambda: self._scroll_to_moment())
        for b in (self.btn_from, self.btn_to, self.btn_sel, self.btn_find):
            brow.addWidget(b)
        brow.addStretch()
        lv.addLayout(brow)
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
            f"<span style='color:{COL['unheard']}'>[… not in the script]</span> · "
            "<b>⟦3</b> … <b>3⟧</b> where moment 3 starts and ends   —   right-click a word to start or end the "
            "moment there (or at its sentence) · select words and press Delete to cut, Enter to bring them back · "
            "click a word to play from it")
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
        self.btn_fx = QPushButton("👁 Preview with effects")
        self.btn_fx.setToolTip("Renders the moment with its cuts, speed, look, fades, caption and pictures "
                               "(small, quick) and plays it here")
        self.btn_fx.clicked.connect(self.preview_effects)
        trow.addWidget(self.btn_play)
        trow.addWidget(self.btn_fx)
        trow.addWidget(self.pos_label, 1)
        rv.addLayout(trow)
        self.lengths = QLabel("")
        self.lengths.setWordWrap(True)
        rv.addWidget(self.lengths)
        rv.addWidget(self._build_props())
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([720, 480])
        v.addWidget(split, 1)

        # Ctrl+Z / Ctrl+Shift+Z come from the main window, which hands them to this panel while it's open
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
            self.moment.addItem(self._label(i, c), id(c))
        idx = self.moment.findData(keep) if keep is not None else -1
        if idx < 0 and self.win.current_clip() is not None:
            idx = self.moment.findData(id(self.win.current_clip()))
        self.moment.setCurrentIndex(max(0, idx) if self.moment.count() else -1)
        self.moment.blockSignals(False)
        self._moment_changed()

    def _label(self, i, c) -> str:
        a = self.project.to_stream(c.start + c.offset)
        cut = f"   ·   ✂ {c.length / max(.25, c.speed):.0f}s → {c.out_length:.0f}s" if c.cuts else ""
        return f"{'☑' if c.enabled else '☐'} {i + 1}. {c.title}   ({fmt_time(a)}){cut}"

    def _relabel(self, c):
        if c in self.project.clips:
            i = self.project.clips.index(c)
            k = self.moment.findData(id(c))
            if k >= 0:
                self.moment.setItemText(k, self._label(i, c))

    def select_clip(self, c):
        i = self.moment.findData(id(c))
        if i >= 0:
            self.moment.setCurrentIndex(i)

    def _moment_changed(self):
        c = self.current_clip()
        self._undo.clear()
        self._redo.clear()
        self._stop()
        self._preview = False
        self._sync_views()
        self._load_props(c)
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
            self._sync_words(c)
        self._render(scroll="moment")
        self._update_buttons()

    def _update_buttons(self):
        c = self.current_clip()
        has_words = bool(c and c.words)
        busy = self._worker is not None and self._worker.isRunning()
        writing = busy and getattr(self, "_writing", False)
        self.btn_load.setEnabled(bool(self.project.source) and (not busy or writing))
        frac = self._written_fraction()
        self.btn_load.setText("■ Stop writing" if writing else
                              "📝 Get the script" if frac <= 0 else
                              f"📝 Carry on with the script ({frac * 100:.0f}% done)" if frac < 0.995 else
                              "📝 Get the script again")
        for b in (self.btn_from, self.btn_to, self.btn_sel):
            b.setEnabled(c is not None and bool(self.view.wspans))
        self.btn_find.setEnabled(c is not None)
        for b in (self.btn_clean, self.btn_ai, self.btn_cut, self.btn_restore, self.btn_play):
            b.setEnabled(has_words and not busy)
        self.btn_fx.setEnabled(c is not None and not busy)
        self.props.setEnabled(c is not None)
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
    # ------------------------------------------------------------------ the whole script
    def _covered(self) -> list[tuple[float, float]]:
        """Stretches of the stream (project time) that have been written out."""
        if self.project.script_done:
            return [(float(a), float(b)) for a, b in self.project.script_done]
        out = []
        for c in self.project.clips:
            if c.words:
                out.append((c.words[0][0] + c.offset, c.words[-1][1] + c.offset))
        return sorted(out)

    def _written_fraction(self) -> float:
        dur = self.win.info.duration if getattr(self.win, "info", None) else 0
        if not dur or not self.project.script_done:
            return 0.0
        return min(1.0, sum(b - a for a, b in self.project.script_done) / dur)

    def _script_words(self) -> list:
        """Every word known, in project time - the whole-stream script, or (older projects) the moments' own."""
        if self.project.script:
            return self.project.script
        seen, ws = set(), []
        for c in self.project.clips:
            for a, b, w in c.words:
                k = (round(a + c.offset, 2), w)
                if k not in seen:
                    seen.add(k)
                    ws.append([a + c.offset, b + c.offset, w])
        ws.sort(key=lambda x: x[0])
        return ws

    def _sync_words(self, c):
        """Give a moment the words around it from the whole script (its cuts stay as they are)."""
        sc = self.project.script
        if not (c and sc):
            return
        a, b = c.start + c.offset - 60, c.end + c.offset + 60
        keys = getattr(self, "_sc_keys", None)
        if keys is None or len(keys) != len(sc):
            keys = self._sc_keys = [w[0] for w in sc]
        i, j = bisect.bisect_left(keys, a), bisect.bisect_right(keys, b)
        ws = [[round(x - c.offset, 3), round(y - c.offset, 3), w] for x, y, w in sc[i:j]]
        lo, hi = c.start + c.offset, c.end + c.offset
        if ws and any(x <= lo + 1 and y >= hi - 1 for x, y in self._covered()) or len(ws) > len(c.words):
            c.words = ws

    def _render(self, scroll: str = "keep"):
        """Write out the whole script: every moment marked ⟦n … n⟧ and tinted, the selected one editable
        (cuts struck through, suggestions highlighted, pauses shown)."""
        c = self.current_clip()
        v = self.view
        sb = v.verticalScrollBar()
        keep_val = sb.value()
        v.setUpdatesEnabled(False)
        v.clear()
        v.spans, v.wspans, v._wkeys = [], [], []
        self._toks = []
        self._moment_pos = None
        ws = self._script_words()
        if not ws and not (c and c.words):
            v.setPlainText("Press “📝 Get the script” to write out every word of the stream - the moments first, "
                           "then everything before and after them.\n\n"
                           "Each moment is marked ⟦1 … 1⟧ in the text. Right-click a word to start or end a moment "
                           "there, or at the start or end of its sentence. Select words in a moment and press "
                           "Delete to cut them from the clip - the video follows the text.")
            v.setUpdatesEnabled(True)
            self._lengths()
            return
        clips = self.project.clips
        cur = QTextCursor(v.document())
        cur.beginEditBlock()
        base = QTextCharFormat()
        base.setForeground(QBrush(QColor(COL["word"])))
        stamp = QTextCharFormat()
        stamp.setForeground(QBrush(QColor("#6b7280")))
        stamp.setFontPointSize(max(7.0, v.font().pointSizeF() * 0.62))
        gapf = QTextCharFormat(stamp)
        gapf.setFontItalic(True)

        def tint(origin, alpha):
            col = QColor(_origin_colour(origin))
            col.setAlpha(alpha)
            return QBrush(col)

        marks = []          # (time, order, kind, clip index): starts before ends at the same time
        for i, x in enumerate(clips):
            marks.append((x.start + x.offset, 0, "start", i))
            marks.append((x.end + x.offset, 1, "end", i))
        marks.sort()
        mi = 0
        open_ = set()

        def put_marks(t, inclusive=True):
            nonlocal mi
            while mi < len(marks) and (marks[mi][0] <= t if inclusive else marks[mi][0] < t):
                mt, _, kind, i = marks[mi]
                mi += 1
                x = clips[i]
                f = QTextCharFormat()
                col = QColor(_origin_colour(x.origin))
                if x.hidden:
                    col = QColor("#9ca3af")
                f.setForeground(QBrush(col))
                f.setFontWeight(QFont.Black if x is c else QFont.Bold)
                if x is c:
                    f.setFontPointSize(v.font().pointSizeF() * 1.15)
                tag = "?" if x.hidden else str(i + 1)
                f.setToolTip(f"{i + 1}. {x.title}")
                if kind == "start":
                    if x is c:
                        self._moment_pos = cur.position()
                    cur.insertText(f"⟦{tag} ", f)
                    open_.add(i)
                else:
                    cur.insertText(f" {tag}⟧ ", f)
                    open_.discard(i)

        def word_tint(t):
            for i in sorted(open_):
                x = clips[i]
                if x is not c and not x.hidden:
                    return tint(x.origin, 45)
            return None

        par_t = [0.0]

        def new_par(t):
            cur.insertBlock()
            cur.insertText(f"{fmt_time(self.project.to_stream(t))}   ", stamp)
            par_t[0] = t

        covered = self._covered()

        def is_covered(a, b):
            return any(x <= a + 1 and y >= b - 1 for x, y in covered)

        cs, ce = (c.start + c.offset, c.end + c.offset) if c else (None, None)
        q = self._quiet.get(id(c)) if c else None
        ctoks = se.tokens(c, q) if (c and c.words) else []
        sugg = c.cut_suggestions if c else []
        done_current = c is None
        prev = None                                  # previous word (a, b, text)
        first = True

        def insert_current():
            nonlocal done_current, prev, first
            done_current = True
            put_marks(cs)
            if not ctoks:
                cur.insertText(" (not written out yet - press 📝 Get the script) ", gapf)
            for k, tk in enumerate(ctoks):
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
                sg = next((s_ for s_ in sugg if s_["start"] - 0.02 <= (a + b) / 2 <= s_["end"] + 0.02
                           or (tk["kind"] == "pause" and s_["kind"] == "pause" and a <= s_["start"] <= b)), None)
                if cut:
                    fmt.setForeground(QBrush(QColor(COL["cut"])))
                    fmt.setFontStrikeOut(True)
                elif sg:
                    fmt.setForeground(QBrush(QColor(COL["sugg_fg"])))
                    fmt.setBackground(QBrush(QColor(COL["ai_bg"] if sg["kind"] == "ai" else COL["sugg_bg"])))
                    fmt.setFontStrikeOut(True)
                    fmt.setToolTip(se.KIND_NAMES.get(sg["kind"], sg["kind"]) + (f" - {sg['why']}" if sg.get("why") else ""))
                else:
                    fmt.setBackground(tint(c.origin, 70))
                x0 = cur.position()
                cur.insertText(tk["text"], fmt)
                v.spans.append((x0, cur.position(), k))
                if tk["kind"] == "word":
                    v.wspans.append((x0, cur.position(), a + c.offset, b + c.offset))
                    prev = (a + c.offset, b + c.offset, tk["text"])
                self._toks.append(tk)
                cur.insertText(" ", base)
            put_marks(ce)
            first = False

        for a, b, w in ws:
            if c is not None and not done_current and (a >= cs - 0.01 or b > cs):
                if prev is not None and cs - prev[1] > 1.2:
                    put_marks(cs, inclusive=False)
                    new_par(cs)
                insert_current()
            if c is not None and done_current and a < ce and b > cs:
                continue                              # this moment's words are written above
            # paragraphs: a new line after a sentence and a pause, or after a long pause
            if first:
                cur.insertText(f"{fmt_time(self.project.to_stream(a))}   ", stamp)
                par_t[0] = a
                first = False
            elif prev is not None:
                gap = a - prev[1]
                if gap > 30 and not is_covered(prev[1], a):
                    put_marks(prev[1] + 0.01)
                    cur.insertBlock()
                    cur.insertText(f"[… {fmt_time(self.project.to_stream(prev[1]))} – "
                                   f"{fmt_time(self.project.to_stream(a))} not written out yet]", gapf)
                    new_par(a)
                elif gap > 3.0 or (gap > 1.2 and prev[2][-1:] in ".?!") or \
                        (a - par_t[0] > 45 and prev[2][-1:] in ".?!"):      # keep paragraphs readable
                    put_marks(a, inclusive=False)
                    new_par(a)
            put_marks(a)
            fmt = QTextCharFormat(base)
            tb = word_tint(a)
            if tb is not None:
                fmt.setBackground(tb)
            x0 = cur.position()
            cur.insertText(w, fmt)
            v.wspans.append((x0, cur.position(), float(a), float(b)))
            cur.insertText(" ", base)
            prev = (float(a), float(b), w)
        if c is not None and not done_current:
            if prev is not None:
                new_par(cs)
            insert_current()
        put_marks(1e12)
        cur.endEditBlock()
        v._wkeys = [x[0] for x in v.wspans]
        v.setUpdatesEnabled(True)
        if scroll == "moment":
            self._scroll_to_moment()
        else:
            sb.setValue(keep_val)
        self._last_render = time.monotonic()
        self._lengths()

    def _scroll_to_moment(self):
        if self._moment_pos is None:
            return
        v = self.view
        cur = v.textCursor()
        cur.setPosition(self._moment_pos)
        v.setTextCursor(cur)
        v.ensureCursorVisible()
        r = v.cursorRect(cur)
        sb = v.verticalScrollBar()
        sb.setValue(sb.value() + r.top() - v.viewport().height() // 5)

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
        """📝 Write out the whole stream - the moments first, then everything before and after them.
        Stretches arrive as they're done; it can be stopped and carried on later."""
        if self._worker is not None and self._worker.isRunning():
            if getattr(self, "_writing", False):
                self._worker.cancel()
                self.status.setText("Stopping after this stretch…")
            return
        src = self.project.source
        info = getattr(self.win, "info", None)
        if not src or not info or not info.duration:
            return
        if not transcribe.available():
            QMessageBox.information(self, "Script", "Speech-to-text isn't included in this copy of RumbleCut.")
            return
        dur = float(info.duration)
        if self._written_fraction() >= 0.995:
            if QMessageBox.question(self, "Script", "Write out the whole stream again? Your cuts stay where they "
                                    "are.") != QMessageBox.Yes:
                return
            self.project.script, self.project.script_done = [], []
        # what still needs writing, in 120 s pieces: around the moments first, then the rest in order
        done = [(float(x), float(y)) for x, y in self.project.script_done]

        def todo(a, b):
            out, pos = [], a
            for x, y in sorted(done):
                if y <= pos or x >= b:
                    continue
                if x > pos:
                    out.append((pos, min(x, b)))
                pos = max(pos, y)
            if pos < b:
                out.append((pos, b))
            return out
        pieces = []
        for a, b in [(0.0, dur)]:
            for x, y in todo(a, b):
                t = x
                while t < y - 0.5:
                    pieces.append((t, min(y, t + 120.0)))
                    t += 120.0
        cur = self.current_clip()
        focus = [x for x in self.project.clips if x.source == src or True]
        focus.sort(key=lambda x: (x is not cur, x.start + x.offset))

        def prio(p):
            for n, x in enumerate(focus):
                a, b = x.start + x.offset - 90, x.end + x.offset + 90
                if p[1] > a and p[0] < b:
                    return (0, n, p[0])
            return (1, 0, p[0])
        pieces.sort(key=prio)
        total = sum(b - a for a, b in pieces) or 1.0
        q = self._script_q
        t0 = time.monotonic()

        def job(on_progress=None, cancel=None):
            got = 0.0
            for a, b in pieces:
                if cancel and cancel():
                    break
                ws = transcribe.transcribe_words(src, max(0.0, a - 1.0), min(dur, b + 1.0), cancel=cancel)
                q.put((a, b, [(x, y, w) for x, y, w in ws if a <= x < b]))
                got += b - a
                el = time.monotonic() - t0
                left = el / got * (total - got) if got else 0
                on_progress(got / total, f"📝 Writing out the stream… {self._done_minutes(got)} of "
                            f"{total / 60:.0f} min" + (f" · about {left / 60:.0f} min left" if got > 60 else ""))
            return got

        def finished_ok(got):
            self._drain_script()
            frac = self._written_fraction()
            self.progress.setValue(int(frac * 1000))
            self.status.setText("📝 The whole stream is written out" if frac >= 0.995 else
                                f"📝 {frac * 100:.0f}% of the stream written out - press the button to carry on")
        self._writing = True
        if not pieces:
            self.status.setText("📝 The whole stream is already written out")
            self._writing = False
            return
        self._run(job, finished_ok, "📝 Writing out the stream - the moments first…")
        self._worker.finished.connect(lambda: (setattr(self, "_writing", False), self._drain.stop(),
                                               self._drain_script(), self._update_buttons()))
        self._worker.cancelled.connect(lambda: self.status.setText(
            f"Stopped - {self._written_fraction() * 100:.0f}% written out. Press the button to carry on."))
        self._drain.start(1500)
        self._update_buttons()

    @staticmethod
    def _done_minutes(secs: float) -> str:
        return f"{secs / 60:.0f}"

    def _drain_script(self):
        """Merge written-out stretches into the project's script and show them."""
        got = []
        while True:
            try:
                got.append(self._script_q.get_nowait())
            except queue.Empty:
                break
        if not got:
            return
        sc = list(self.project.script)
        done = [list(x) for x in self.project.script_done]
        for a, b, ws in got:
            sc = [w for w in sc if not (a <= w[0] < b)] + [[round(x, 3), round(y, 3), w] for x, y, w in ws]
            done.append([a, b])
        sc.sort(key=lambda w: w[0])
        done.sort()
        merged = []
        for a, b in done:
            if merged and a <= merged[-1][1] + 0.01:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        self.project.script, self.project.script_done = sc, merged
        self._sc_keys = None
        for c in self.project.clips:
            lo, hi = c.start + c.offset, c.end + c.offset
            if any(a < hi and b > lo for a, b, _ in got):
                self._sync_words(c)
                if c is not self.current_clip():
                    self.win._clip_edited(c)
        busy = self._worker is not None and self._worker.isRunning()
        if not busy or time.monotonic() - self._last_render > 4.0 or self._moment_pos is None:
            self._render(scroll="moment" if self._moment_pos is None else "keep")
        self._update_buttons()

    # ------------------------------------------------------------------ moments from the words
    def _word_index(self, t: float) -> int:
        ws = self._script_words()
        keys = [w[0] for w in ws]
        return max(0, min(len(ws) - 1, bisect.bisect_right(keys, t + 1e-3) - 1))

    def _sentence(self, t: float) -> tuple[float, float, str]:
        """The sentence around the word at t (project time): from after the last . ? ! or long pause to the
        next one."""
        ws = self._script_words()
        if not ws:
            return t, t, ""
        i = self._word_index(t)
        a = i
        while a > 0 and ws[a - 1][2][-1:] not in ".?!" and ws[a][0] - ws[a - 1][1] < 1.5:
            a -= 1
        b = i
        while b < len(ws) - 1 and ws[b][2][-1:] not in ".?!" and ws[b + 1][0] - ws[b][1] < 1.5:
            b += 1
        return float(ws[a][0]), float(ws[b][1]), " ".join(w for _, _, w in ws[a:b + 1])

    def _edge_time(self, edge: str, t0: float, t1: float) -> float:
        """Project time for a moment edge at a word: a breath before it starts, or just after it ends
        (never into the next word)."""
        ws = self._script_words()
        i = self._word_index(t0)
        if edge == "start":
            before = ws[i - 1][1] if i > 0 and ws[i][0] >= t0 - 0.01 else t0 - 1
            return max(0.0, max(before + 0.02, t0 - 0.15))
        nxt = ws[i + 1][0] if i + 1 < len(ws) else t1 + 1
        return min(nxt - 0.02, t1 + 0.25)

    def set_edge(self, edge: str, t0: float, t1: float, c=None, word: str = ""):
        c = c or self.current_clip()
        if not c:
            return
        t = self._edge_time(edge, t0, t1) - c.offset
        if edge == "start":
            if t >= c.end - 1.0:
                self.status.setText("That's after the moment ends - set the end first, or use ⟦ Selection ⟧.")
                return
            c.start = round(max(0.0, t), 2)
        else:
            if t <= c.start + 1.0:
                self.status.setText("That's before the moment starts - set the start first, or use ⟦ Selection ⟧.")
                return
            c.end = round(t, 2)
        note = f"You set the {edge} on the script" + (f" at “{word}”." if word else ".")
        c.timing = [x for x in c.timing if not x.startswith(f"You set the {edge} on the script")] + [note]
        self._after_edges(c)

    def _after_edges(self, c):
        self._stop()
        self._sync_words(c)
        self._render(scroll="keep")
        self._sync_views()
        self._relabel(c)
        self._lengths()
        self._update_buttons()
        self._push(c)

    def _cursor_words(self):
        """(first, last) words under the text cursor / selection: (t0, t1, text) in project time."""
        v = self.view
        cur = v.textCursor()
        if not v.wspans:
            return None
        a, b = (cur.selectionStart(), cur.selectionEnd()) if cur.hasSelection() else (cur.position(), cur.position())
        i, j = v.word_at(a), v.word_at(max(a, b - 1) if cur.hasSelection() else b)
        if i is None:
            return None
        doc = v.document()

        def text(k):
            x0, x1 = v.wspans[k][0], v.wspans[k][1]
            cc = QTextCursor(doc)
            cc.setPosition(x0)
            cc.setPosition(x1, QTextCursor.KeepAnchor)
            return cc.selectedText()
        return (v.wspans[i][2], v.wspans[i][3], text(i)), (v.wspans[j][2], v.wspans[j][3], text(j))

    def _edge_from_cursor(self, edge: str):
        r = self._cursor_words()
        if not r:
            return
        first, last = r
        w = first if edge == "start" else last
        self.set_edge(edge, w[0], w[1], word=w[2])

    def _selection_to_moment(self):
        c = self.current_clip()
        r = self._cursor_words()
        if not (c and r):
            return
        first, last = r
        if last[1] - first[0] < 1.0:
            self.status.setText("Select at least a second of words to make them the moment.")
            return
        c.start = round(self._edge_time("start", first[0], first[1]) - c.offset, 2)
        c.end = round(self._edge_time("end", last[0], last[1]) - c.offset, 2)
        c.timing = [x for x in c.timing if not x.startswith("You set the")] + \
            [f"You set it on the script: “{first[2]}” … “{last[2]}”."]
        self._after_edges(c)

    def _new_moment(self, a: float, b: float, text: str):
        if b - a < 1.0:
            b = a + 1.0
        src = self.project.source
        nc = editor.Clip(source=src, start=round(self._edge_time("start", a, a) , 2),
                         end=round(self._edge_time("end", b, b), 2),
                         title=(text[:40] + "…") if len(text) > 40 else (text or "My moment"), origin="manual",
                         reasons=["Made on the script"], timing=["You made it from the script."])
        nc.orig = [nc.start, nc.end]
        self.project.clips.append(nc)
        self.win._fill_clip_list()
        self.win.clip_list.setCurrentRow(len(self.project.clips) - 1)
        self.refresh()
        self.select_clip(nc)

    def _word_menu(self, pos: int, gpos):
        v = self.view
        i = v.word_at(pos)
        if i is None:
            return
        t0, t1 = v.wspans[i][2], v.wspans[i][3]
        cc = QTextCursor(v.document())
        cc.setPosition(v.wspans[i][0])
        cc.setPosition(v.wspans[i][1], QTextCursor.KeepAnchor)
        word = cc.selectedText()
        c = self.current_clip()
        sa, sb_, stext = self._sentence(t0)
        has_sel = v.textCursor().hasSelection()
        m = QMenu(self)
        if c is not None:
            n = self.project.clips.index(c) + 1
            m.addSection(f"Moment {n}: {c.title[:40]}")
            m.addAction(f"⟦  Start it at “{word}”", lambda: self.set_edge("start", t0, t1, word=word))
            m.addAction(f"End it after “{word}”  ⟧", lambda: self.set_edge("end", t0, t1, word=word))
            m.addAction("⟦  Start it where this sentence starts",
                        lambda: self.set_edge("start", sa, sa, word=stext.split(" ")[0] if stext else ""))
            m.addAction("End it where this sentence ends  ⟧",
                        lambda: self.set_edge("end", sb_, sb_, word=stext.split(" ")[-1] if stext else ""))
            if has_sel:
                m.addAction("⟦ Make the selected words the moment ⟧", self._selection_to_moment)
            inside = c.start + c.offset <= t0 < c.end + c.offset
            if inside:
                m.addSeparator()
                m.addAction("✂  Cut selected  (Del)", self.cut_selected)
                m.addAction("↩  Restore selected  (Enter)", self.restore_selected)
        others = [(k, x) for k, x in enumerate(self.project.clips)
                  if x is not c and x.start + x.offset <= t0 < x.end + x.offset]
        if others:
            m.addSeparator()
            for k, x in others:
                m.addAction(f"Go to moment {k + 1}: {x.title[:40]}", lambda x=x: self.select_clip(x))
        m.addSeparator()
        m.addAction("＋  New moment from this sentence", lambda: self._new_moment(sa, sb_, stext))
        if has_sel:
            r = self._cursor_words()
            if r:
                m.addAction("＋  New moment from the selected words",
                            lambda: self._new_moment(r[0][0], r[1][1], f"{r[0][2]} … {r[1][2]}"))
        m.addAction("▶  Play from here", lambda: self._play_free(t0))
        self._exec_menu(m, gpos)

    def _exec_menu(self, m, gpos):
        m.exec(gpos)

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
        self._restore_source()
        self._free_play = False
        if self.player.source() != QUrl.fromLocalFile(c.source):
            self.player.setSource(QUrl.fromLocalFile(c.source))
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
        self._free_play = False
        self.btn_play.setText("▶ Play the edit")
        self.view.setExtraSelections([])
        self.clip_tl.set_playing(False)
        self.overview.set_playing(-1)

    def _token_clicked(self, k):
        tk = self._toks[k]
        if self.player:
            self._play_from(tk["start"])

    def _play_free(self, t: float):
        """Play the stream from a word outside the moment (project time)."""
        if not self.player or not self.project.source:
            return
        c = self.current_clip()
        if c is not None and c.start + c.offset <= t < c.end + c.offset - 0.1:
            self._play_from(t - c.offset)
            return
        self._stop()
        self._preview = False
        src = self.project.source
        if self.player.source() != QUrl.fromLocalFile(src):
            self.player.setSource(QUrl.fromLocalFile(src))
        self._free_play = True
        self._stop_at = 1e12
        self.player.setPosition(int(max(0.0, t) * 1000))
        self.player.play()
        self.btn_play.setText("■ Stop")

    def _highlight_time(self, tp: float):
        """Mark the word being heard (project time) and keep it in view."""
        v = self.view
        if not v.wspans:
            return
        keys = [x[2] for x in v.wspans] if len(getattr(self, "_wt_keys", [])) != len(v.wspans) else self._wt_keys
        self._wt_keys = keys
        i = bisect.bisect_right(keys, tp + 0.02) - 1
        if i < 0 or v.wspans[i][3] + 0.3 < tp:
            v.setExtraSelections([])
            return
        sel = QTextEdit.ExtraSelection()
        cur = v.textCursor()
        cur.setPosition(v.wspans[i][0])
        cur.setPosition(v.wspans[i][1], QTextCursor.KeepAnchor)
        sel.cursor = cur
        sel.format.setBackground(QBrush(QColor(COL["now"])))
        v.setExtraSelections([sel])
        r = v.cursorRect(cur)
        if r.bottom() > v.viewport().height() - 10 or r.top() < 0:
            sb = v.verticalScrollBar()
            sb.setValue(sb.value() + r.top() - v.viewport().height() // 3)

    def _pos(self, ms):
        c = self.current_clip()
        if self._preview and c:
            self.pos_label.setText(f"👁 with effects  {fmt_time(ms / 1000)} / {fmt_time(c.out_length)}")
            return
        if self._free_play:
            t = ms / 1000
            self.pos_label.setText(f"{fmt_time(self.project.to_stream(t))}  (the stream)")
            self.overview.set_playhead(t)
            self._highlight_time(t)
            return
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
        self.clip_tl.set_playhead(t + c.offset)
        self.clip_tl.set_playing(True)
        self.overview.set_playhead(t + c.offset)
        self.overview.set_playing(self.project.clips.index(c) if c in self.project.clips else -1)
        self._highlight_time(t + c.offset)

    # ------------------------------------------------------------------ where the moment is
    def _sync_views(self, keep_view: bool = False):
        win, c = self.win, self.current_clip()
        tl, ov = win.timeline, self.overview
        have = bool(tl.duration) and bool(self.project.source)
        ov.setVisible(have)
        self.clip_tl.setVisible(have and c is not None)
        self.clip_tl_title.setVisible(have and c is not None)
        if not have:
            return
        if abs(ov.duration - tl.duration) > 0.01 or ov.times is not tl.times:
            ov.set_data(tl.duration, tl.times, tl.score)
        ov.time_fn, ov.joins, ov.sections = tl.time_fn, list(tl.joins), list(tl.sections)
        clips = self.project.clips
        ov.hidden = [x.hidden for x in clips]
        ov.limits = [win._clip_limits(x) for x in clips]
        idx = clips.index(c) if c in clips else -1
        ov.set_clips(win._spans(), idx, [win._why_html(x) for x in clips], [x.origin for x in clips])
        if c is None:
            return
        a, b = c.start + c.offset, c.end + c.offset
        if ov.zoom > 1.001 and not (ov.view[0] <= a and b <= ov.view[1]):
            span = max(ov.view[1] - ov.view[0], (b - a) * 1.4)
            ov.set_view((a + b) / 2 - span / 2, (a + b) / 2 + span / 2)
        ct = self.clip_tl
        ct.time_fn, ct.end_fn = win.clip_tl.time_fn, win.clip_tl.end_fn
        ct.cuts = [(x + c.offset, y + c.offset) for x, y in c.cuts]
        ct.others = win._others_for(c)
        ct.hidden = c.hidden
        ct.set_clip(a, b, win._clip_limits(c), tl.times, tl.score, user=c.origin, keep_view=keep_view)
        who = {"user": "★ Your pick", "manual": "✎ Your moment"}.get(c.origin, f"Moment {idx + 1}")
        self.clip_tl_title.setText(f"{who} of {len(clips)} - drag the handles to change where it starts and ends"
                                   + (" · hatched = cut" if c.cuts else "") + " · right-click for options")

    def _overview_clicked(self, i: int):
        if 0 <= i < len(self.project.clips):
            self.select_clip(self.project.clips[i])

    def _overview_moved(self, i: int, a: float, b: float, what: str):
        if not (0 <= i < len(self.project.clips)):
            return
        c = self.project.clips[i]
        if c is not self.current_clip():
            self.select_clip(c)
        c.start, c.end = round(a - c.offset, 2), round(b - c.offset, 2)
        self.clip_tl.cuts = [(x + c.offset, y + c.offset) for x, y in c.cuts]
        self.clip_tl.set_clip(a, b, self.win._clip_limits(c), self.win.timeline.times, self.win.timeline.score,
                              user=c.origin, keep_view=True)
        self._lengths()

    def _span_dragged(self, a: float, b: float, what: str):
        c = self.current_clip()
        if not c:
            return
        self._stop()
        c.start, c.end = round(a - c.offset, 2), round(b - c.offset, 2)
        clips = self.project.clips
        self.overview.set_clips(self.win._spans(), clips.index(c) if c in clips else -1,
                                self.overview.tips, self.overview.user)
        self._lengths()

    def _span_done(self):
        c = self.current_clip()
        if not c:
            return
        note = "You set the start and end yourself on the moment's timeline."
        if note not in c.timing:
            c.timing = list(c.timing) + [note]
        self._sync_words(c)
        self._render()
        self._sync_views(keep_view=True)
        self._relabel(c)
        self._words_note(c)
        self._push(c)

    def _words_note(self, c):
        if not c.words:
            return
        w0, w1 = min(w[0] for w in c.words), max(w[1] for w in c.words)
        if c.start < w0 - 1.5 or c.end > w1 + 1.5:
            self.status.setText("The moment now runs past the script - press 📝 Get the script again to see "
                                "the new words")
        elif self.status.text().startswith("The moment now runs past"):
            self.status.setText("")

    def _tl_seek(self, t: float):
        c = self.current_clip()
        if not (c and self.player):
            return
        ft = t - c.offset
        if c.start <= ft < c.end - 0.1:
            self._play_from(ft)
        else:
            self._stop()
            self._restore_source()
            self.player.setPosition(int(ft * 1000))
            self.clip_tl.set_playhead(t)

    def _add_at(self, t: float):
        self.win.add_clip_at(t)
        self.refresh()
        self.select_clip(self.win.current_clip())

    def _menu(self, i, gpos, t=None):
        clips = self.project.clips
        c = clips[i] if i is not None and 0 <= i < len(clips) else self.current_clip()
        if c is None:
            return
        self.select_clip(c)
        self.win._clip_menu(c, gpos, t, play=lambda: self._play_moment(c))
        if self.win.tabs.currentIndex() == self.win.ctx_index:
            self.refresh()

    def _play_moment(self, c):
        self.select_clip(c)
        if c.words:
            self._play_from(c.kept[0][0])
        elif self.player:
            self._restore_source()
            self._stop_at = c.end
            self.player.setPosition(int(c.start * 1000))
            self.player.play()
            self.btn_play.setText("■ Stop")

    # ------------------------------------------------------------------ the moment's look
    def _build_props(self) -> QWidget:
        self.props = QGroupBox("This moment's look")
        g = QGridLayout(self.props)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(6)
        self.p_title = QLineEdit()
        self.p_title.setPlaceholderText("Title (also the file name)")
        self.p_speed = QComboBox()
        self.p_speed.addItems(["0.5×", "0.75×", "1×", "1.25×", "1.5×", "2×"])
        self.p_zoom = QSpinBox()
        self.p_zoom.setRange(100, 200)
        self.p_zoom.setSingleStep(5)
        self.p_zoom.setSuffix(" %")
        self.p_zoom.setToolTip("Punch-in zoom for the whole moment")
        self.p_color = QComboBox()
        self.p_color.addItems(list(editor.COLOR_LOOKS))
        self.p_vig = QCheckBox("Vignette")
        self.p_fin, self.p_fout = QDoubleSpinBox(), QDoubleSpinBox()
        for sb in (self.p_fin, self.p_fout):
            sb.setRange(0, 5)
            sb.setSingleStep(0.1)
            sb.setSuffix(" s")
        self.p_caption = QPlainTextEdit()
        self.p_caption.setPlaceholderText("Caption (optional)")
        self.p_caption.setFixedHeight(48)
        self.p_cappos = QComboBox()
        self.p_cappos.addItems(["bottom", "top"])
        more = QPushButton("More in the Clip studio →")
        more.setToolTip("Transitions, loudness and pictures on screen")
        more.clicked.connect(self._open_in_studio)
        lab = lambda t: QLabel(t)
        g.addWidget(lab("Title"), 0, 0)
        g.addWidget(self.p_title, 0, 1, 1, 3)
        g.addWidget(lab("Speed"), 1, 0)
        g.addWidget(self.p_speed, 1, 1)
        g.addWidget(lab("Zoom"), 1, 2)
        g.addWidget(self.p_zoom, 1, 3)
        g.addWidget(lab("Look"), 2, 0)
        g.addWidget(self.p_color, 2, 1)
        g.addWidget(self.p_vig, 2, 2, 1, 2)
        g.addWidget(lab("Fade in"), 3, 0)
        g.addWidget(self.p_fin, 3, 1)
        g.addWidget(lab("Fade out"), 3, 2)
        g.addWidget(self.p_fout, 3, 3)
        g.addWidget(lab("Caption"), 4, 0)
        g.addWidget(self.p_caption, 4, 1, 1, 2)
        g.addWidget(self.p_cappos, 4, 3, Qt.AlignTop)
        g.addWidget(more, 5, 0, 1, 4)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        self.p_title.textEdited.connect(lambda t: self._pset("title", t))
        self.p_speed.currentTextChanged.connect(lambda t: self._pset("speed", float(t.rstrip("×"))))
        self.p_zoom.valueChanged.connect(lambda x: self._pset("zoom", x / 100))
        self.p_color.currentTextChanged.connect(lambda t: self._pset("color", t))
        self.p_vig.toggled.connect(lambda b: self._pset("vignette", b))
        self.p_fin.valueChanged.connect(lambda x: self._pset("fade_in", x))
        self.p_fout.valueChanged.connect(lambda x: self._pset("fade_out", x))
        self.p_caption.textChanged.connect(lambda: self._pset("caption", self.p_caption.toPlainText()))
        self.p_cappos.currentTextChanged.connect(lambda t: self._pset("caption_pos", t))
        return self.props

    def _load_props(self, c):
        self._loading = True
        try:
            if c is None:
                self.p_title.clear()
                self.p_caption.clear()
                return
            self.p_title.setText(c.title)
            self.p_title.setCursorPosition(0)
            self.p_speed.setCurrentText(f"{c.speed:g}×")
            self.p_zoom.setValue(int(round(c.zoom * 100)))
            self.p_color.setCurrentText(c.color)
            self.p_vig.setChecked(c.vignette)
            self.p_fin.setValue(c.fade_in)
            self.p_fout.setValue(c.fade_out)
            if self.p_caption.toPlainText() != c.caption:
                self.p_caption.setPlainText(c.caption)
            self.p_cappos.setCurrentText(c.caption_pos)
        finally:
            self._loading = False

    def _pset(self, attr, value):
        if self._loading:
            return
        c = self.current_clip()
        if not c:
            return
        setattr(c, attr, value)
        if attr in ("speed", "title"):
            self._lengths()
            self._relabel(c)
        self._push(c)

    def _open_in_studio(self):
        c = self.current_clip()
        self._stop()
        self.win.tabs.setCurrentIndex(1)
        if c in self.project.clips:
            self.win.clip_list.setCurrentRow(self.project.clips.index(c))

    def _push(self, c):
        """Tell the Clip studio (moment list, timeline, inspector) about a change - batched while typing."""
        self._push_clip = c
        self._push_timer.start(250)

    def _do_push(self):
        c, win = self._push_clip, self.win
        if c not in self.project.clips:
            return
        r = self.project.clips.index(c)
        win._clip_edited(c)
        if win.clip_list.currentRow() == r:
            win.select_clip(r)
        else:
            win._sync_timeline(win.clip_list.currentRow())

    # ------------------------------------------------------------------ preview with effects
    def _restore_source(self):
        c = self.current_clip()
        if self._preview and self.player and c:
            self._preview = False
            self.player.setSource(QUrl.fromLocalFile(c.source))

    def preview_effects(self):
        c = self.current_clip()
        if not c or (self._worker is not None and self._worker.isRunning()):
            return
        self._stop()
        st = editor.ExportSettings(**{**self.project.export.__dict__})
        st.height, st.quality = 480, "small"
        out = self.win.thumb_dir / f"fxpreview_{abs(hash((id(c), len(c.cuts))))}.mp4"

        def job(on_progress=None, cancel=None):
            return editor.render_clip(c, st, out, on_progress=lambda f: on_progress(f, "Rendering the preview…"),
                                      cancel=cancel)

        def done(path):
            self.progress.setValue(1000)
            self.status.setText("👁 Playing the moment with its effects - ▶ Play the edit goes back to the script")
            if c is not self.current_clip() or not self.player:
                return
            self._preview = True
            self.stack.setCurrentIndex(0)
            self.player.setSource(QUrl.fromLocalFile(str(path)))
            self.player.play()
            self.btn_play.setText("■ Stop")
        self._run(job, done, "Rendering the preview…")
