"""Stream-wide 'excitement' timeline: score curve, highlight regions, playhead. Click to seek."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QToolTip, QWidget

ACCENT = QColor("#85C742")
USER = QColor("#f5b942")          # your picks from the Summary - gold, not green
MANUAL = QColor("#5aa9e6")        # moments you made yourself (right-click / ＋ Add) - blue, dashed


def _origin_colour(origin) -> QColor:
    return {"user": USER, "manual": MANUAL}.get(origin, ACCENT)


def fmt_time(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 3600:d}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:d}:{t % 60:02d}"



def _hatch(p, rect, colour, step=7):
    """See-through box with thin diagonal lines (a hidden moment)."""
    p.save()
    p.setClipRect(rect)
    p.setPen(QPen(colour, 1))
    hgt = rect.height()
    k = -hgt
    while k < rect.width():
        p.drawLine(QPointF(rect.left() + k, rect.bottom()), QPointF(rect.left() + k + hgt, rect.top()))
        k += step
    p.restore()

class Timeline(QWidget):
    seek = Signal(float)
    clip_clicked = Signal(int)
    zoom_changed = Signal(float)        # how far zoomed in (1 = whole stream)
    add_at = Signal(float)              # right-click on empty space: make a new moment here
    clip_menu = Signal(int, float, object)   # right-click on a moment: index, time, global position
    clip_moved = Signal(int, float, float, str)       # live while dragging a moment (index, start, end, what)
    clip_move_done = Signal(int, float, float, str)

    MAX_ZOOM_SPAN = 10.0                # seconds: the closest you can zoom

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(110)
        self.setMouseTracking(True)
        self.duration = 0.0
        self.times: np.ndarray | None = None
        self.score: np.ndarray | None = None
        self.clips: list[tuple[float, float]] = []
        self.selected = -1
        self.tips: list[str] = []
        self.time_fn = lambda t: t          # file time -> stream time for labels
        self.joins: list[float] = []        # where separately downloaded parts were joined
        self.playhead = 0.0
        self.user: list = []                # origin of each clip: "ai" | "user" | "manual"
        self.hidden: list = []              # True = drawn as a see-through hatched box with "?"
        self.limits: list[tuple[float, float]] = []      # how far each clip may go (its source file)
        self._clip_drag = None              # (index, what, grab offset, press x, moved?)
        self.playing = -1                   # the moment the playhead is in while playing
        self.sections: list[tuple[float, float, float, str]] = []   # AI summary: start, end, interest, text
        self._dragging = False
        self.view = (0.0, 0.0)              # visible stretch (start, end); (0, duration) = zoomed out

    def set_data(self, duration, times=None, score=None):
        new = float(duration or 0)
        if abs(new - self.duration) > 0.01 or self.view[1] <= self.view[0]:
            self.view = (0.0, new)
        self.duration = new
        self.times, self.score = times, score
        self.update()
        self.zoom_changed.emit(self.zoom)

    # ---- zoom
    @property
    def zoom(self) -> float:
        span = self.view[1] - self.view[0]
        return self.duration / span if span > 0 and self.duration else 1.0

    def set_view(self, a: float, b: float):
        if not self.duration:
            return
        span = max(min(self.MAX_ZOOM_SPAN, self.duration), min(self.duration, b - a))
        a = max(0.0, min(a, self.duration - span))
        self.view = (a, a + span)
        self.update()
        self.zoom_changed.emit(self.zoom)

    def zoom_by(self, factor: float, around: float | None = None):
        """factor > 1 zooms in. `around` (seconds) stays under the same spot."""
        if not self.duration:
            return
        a, b = self.view
        c = self.playhead if around is None else around
        if not (a <= c <= b):
            c = (a + b) / 2
        frac = (c - a) / max(1e-9, b - a)
        span = (b - a) / factor
        self.set_view(c - frac * span, c - frac * span + span)

    def zoom_fit(self):
        self.set_view(0.0, self.duration)

    def pan_by(self, seconds: float):
        a, b = self.view
        self.set_view(a + seconds, b + seconds)

    def wheelEvent(self, ev):
        if not self.duration:
            return
        d = ev.angleDelta()
        if ev.modifiers() & Qt.ShiftModifier or abs(d.x()) > abs(d.y()):     # Shift+wheel / sideways: pan
            step = (d.x() if abs(d.x()) > abs(d.y()) else d.y()) / 120
            self.pan_by(-step * (self.view[1] - self.view[0]) * 0.15)
        elif d.y():
            self.zoom_by(1.25 ** (d.y() / 120), self._t(ev.position().x()))   # up = in, down = out
        ev.accept()

    def set_clips(self, clips: list[tuple[float, float]], selected: int = -1, tips: list[str] | None = None,
                  user: list[bool] | None = None):
        self.clips, self.selected = clips, selected
        self.tips = tips or []
        if user is not None:
            self.user = user
        self.update()

    def set_playing(self, i: int):
        if i != self.playing:
            self.playing = i
            self.update()

    def set_sections(self, sections):
        self.sections = list(sections)
        self.update()

    def set_playhead(self, t: float):
        self.playhead = t
        a, b = self.view
        if self.zoom > 1.001 and not self._dragging and not (a <= t <= b):   # keep the playhead in view
            span = b - a
            self.set_view(t - span * 0.1, t + span * 0.9)
        self.update()

    def _x(self, t: float) -> float:
        a, b = self.view
        return 8 + (self.width() - 16) * ((t - a) / (b - a) if b > a else 0)

    def _t(self, x: float) -> float:
        a, b = self.view
        return max(0.0, min(self.duration, a + (x - 8) / max(1, self.width() - 16) * (b - a)))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        p.fillRect(r, QColor("#16181c"))
        top, bottom = 18, r.height() - 18
        h = bottom - top
        if not self.duration:
            p.setPen(QColor("#6b7280"))
            p.drawText(r, Qt.AlignCenter, "Load a video and press “Find highlights” to see the excitement timeline")
            return
        # grid + time labels
        p.setFont(QFont(self.font().family(), 8))
        va, vb = self.view
        p.save()
        p.setClipRect(QRectF(0, 0, self.width(), r.height()))
        steps = 10
        for i in range(steps + 1):
            tt = va + (vb - va) * i / steps
            x = self._x(tt)
            p.setPen(QPen(QColor("#262a31"), 1))
            p.drawLine(QPointF(x, top), QPointF(x, bottom))
            p.setPen(QColor("#7c8591"))
            lx = min(max(x - 30, 0), self.width() - 60)
            align = Qt.AlignLeft if i == 0 else (Qt.AlignRight if i == steps else Qt.AlignCenter)
            p.drawText(QRectF(lx, bottom + 2, 60, 14), align, fmt_time(self.time_fn(tt)))
        # joins between separately downloaded parts
        for jt in self.joins:
            x = self._x(jt)
            p.setPen(QPen(QColor("#f59e0b"), 1, Qt.DashLine))
            p.drawLine(QPointF(x, top), QPointF(x, bottom))
        # the AI's section summary: a thin strip along the bottom, brighter = more interesting
        for a, b, inter, _ in self.sections:
            x0, x1 = self._x(a), self._x(b)
            c = QColor(USER) if inter >= 0.6 else QColor("#6b7280")
            c.setAlpha(int(60 + 160 * max(0.0, min(1.0, inter))))
            p.fillRect(QRectF(x0 + 0.5, bottom - 4, max(1.5, x1 - x0 - 1), 4), c)
        # highlight regions - the selected one is drawn last, on top of the others
        order = [i for i in range(len(self.clips)) if i not in (self.selected, self.playing)]
        if 0 <= self.selected < len(self.clips):
            order.append(self.selected)
        if 0 <= self.playing < len(self.clips) and self.playing != self.selected:
            order.append(self.playing)
        for i in order:
            s, e = self.clips[i]
            x0, x1 = self._x(s), self._x(e)
            sel = i == self.selected
            now = i == self.playing
            origin = self.user[i] if i < len(self.user) else "ai"
            origin = "user" if origin is True else ("ai" if origin is False else origin)
            base_col = _origin_colour(origin)
            col = QColor(base_col)
            col.setAlpha(170 if now else (120 if sel else 50))
            if now:                                  # playing: a soft glow around it
                glow = QColor(base_col)
                glow.setAlpha(60)
                p.fillRect(QRectF(x0 - 4, top - 4, max(2, x1 - x0) + 8, h + 8), glow)
            hid = i < len(self.hidden) and bool(self.hidden[i])
            if hid:                                  # hidden: see-through, faint diagonal lines
                hc = QColor("#cbd5e1")
                hc.setAlpha(110 if sel or now else 70)
                _hatch(p, QRectF(x0, top, max(2, x1 - x0), h), hc)
            else:
                p.fillRect(QRectF(x0, top, max(2, x1 - x0), h), col)
            edge = QColor(base_col)
            edge.setAlpha(255 if sel else 160)
            pen = QPen(QColor("#ffffff") if now else edge, 3 if now else (2 if sel else 1))
            if origin == "manual" or hid:
                pen.setStyle(Qt.DashLine)
            if hid and not (sel or now):
                pen.setColor(QColor(203, 213, 225, 120))
            p.setPen(pen)
            p.drawRect(QRectF(x0, top, max(2, x1 - x0), h))
            if sel:                                  # grips on the edges: drag to resize
                p.setPen(Qt.NoPen)
                p.setBrush(base_col)
                for gx in (x0, x1):
                    p.drawRoundedRect(QRectF(gx - 2.5, top + h / 2 - 9, 5, 18), 2, 2)
                p.setBrush(Qt.NoBrush)
            p.setPen(QColor("#e5e7eb"))
            mark = ("▶ " if now else "") + {"user": "★", "manual": "✎"}.get(origin, "")
            if hid:
                f0 = p.font()
                fb = QFont(f0)
                fb.setBold(True)
                p.setFont(fb)
                p.drawText(QRectF(x0 - 10, 1, max(20, x1 - x0 + 20), 16), Qt.AlignCenter, ("▶ " if now else "") + "?")
                p.setFont(f0)
            else:
                p.drawText(QRectF(x0 - 10, 1, max(20, x1 - x0 + 20), 16), Qt.AlignCenter, f"{mark}{i + 1}")
        # score curve (just the visible stretch)
        sc = self.score
        xa, xb = 8.0, self.width() - 8.0
        if sc is not None and len(sc) > 1 and self.times is not None and len(self.times) == len(sc):
            i0 = max(0, int(np.searchsorted(self.times, va)) - 1)
            i1 = min(len(sc), int(np.searchsorted(self.times, vb)) + 2)
            xa, xb = self._x(float(self.times[i0])), self._x(float(self.times[i1 - 1]))
            sc = sc[i0:i1]
        if sc is not None and len(sc) > 1:
            w = max(2, self.width() - 16)
            idx = np.linspace(0, len(sc) - 1, w).astype(int)
            # max-pool so short spikes stay visible when zoomed out
            bins = np.array_split(sc, w) if len(sc) > w else None
            vals = np.array([b.max() if len(b) else 0 for b in bins]) if bins else sc[idx]
            path = QPainterPath(QPointF(xa, bottom))
            for j, v in enumerate(vals):
                path.lineTo(QPointF(xa + j * (xb - xa) / (len(vals) - 1), bottom - v * h))
            path.lineTo(QPointF(xb, bottom))
            grad = QLinearGradient(0, top, 0, bottom)
            grad.setColorAt(0, QColor(255, 120, 60, 200))
            grad.setColorAt(0.5, QColor(133, 199, 66, 150))
            grad.setColorAt(1, QColor(60, 120, 200, 60))
            p.fillPath(path, grad)
        # playhead
        x = self._x(self.playhead)
        if va <= self.playhead <= vb:
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.drawLine(QPointF(x, top - 4), QPointF(x, bottom))
        p.restore()

    def _clip_at(self, x: float):
        """(index, what) under x: the selected moment's edges first, then the selected moment (it's on
        top), then the others."""
        t = self._t(x)
        sel = self.selected
        if 0 <= sel < len(self.clips):
            s, e = self.clips[sel]
            if abs(x - self._x(s)) <= 6:
                return sel, "start"
            if abs(x - self._x(e)) <= 6:
                return sel, "end"
            if s <= t <= e:
                return sel, "move"
        for i in reversed(range(len(self.clips))):
            s, e = self.clips[i]
            if s <= t <= e:
                return i, "move"
        return None, None

    def mousePressEvent(self, ev):
        if not self.duration:
            return
        x = ev.position().x()
        t = self._t(x)
        if ev.button() == Qt.RightButton:
            i, _ = self._clip_at(x)
            if i is not None:
                self.clip_menu.emit(i, t, ev.globalPosition().toPoint())
            else:
                self.add_at.emit(t)
            return
        if ev.button() != Qt.LeftButton:
            return
        i, what = self._clip_at(x)
        if i is not None:
            self._dragging = True
            s, e = self.clips[i]
            self._clip_drag = [i, what, t - s, x, False]
            if i != self.selected:
                self.clip_clicked.emit(i)
            return
        self._dragging = True
        self.seek.emit(t)

    def contextMenuEvent(self, ev):
        ev.accept()                         # right-click adds a moment instead

    def mouseReleaseEvent(self, ev):
        d = self._clip_drag
        self._clip_drag = None
        self._dragging = False
        if d is None:
            return
        i, what, _, _, moved = d
        if moved and 0 <= i < len(self.clips):
            s, e = self.clips[i]
            self.clip_move_done.emit(i, s, e, what)
        else:
            self.seek.emit(self._t(ev.position().x()))          # a plain click still jumps there

    def mouseMoveEvent(self, ev):
        if not self.duration:
            return
        x = ev.position().x()
        t = self._t(x)
        d = self._clip_drag
        if d is not None and ev.buttons() & Qt.LeftButton:
            i, what, grab, x0, moved = d
            if not moved and abs(x - x0) < 4:
                return
            d[4] = True
            if not (0 <= i < len(self.clips)):
                return
            s, e = self.clips[i]
            lo, hi = self.limits[i] if i < len(self.limits) else (0.0, self.duration)
            lo, hi = max(lo, self.view[0], 0.0), min(hi, self.view[1], self.duration)   # never off-screen
            t = round(t * 10) / 10
            if what == "start":
                s = max(lo, min(t, e - 1.0))
            elif what == "end":
                e = min(hi, max(t, s + 1.0))
            else:
                ln = e - s
                s = max(lo, min(hi - ln, round((self._t(x) - grab) * 10) / 10))
                e = s + ln
            self.clips[i] = (s, e)
            self.update()
            self.clip_moved.emit(i, s, e, what)
            return
        if self._dragging and ev.buttons() & Qt.LeftButton:      # drag empty space to scrub
            self.seek.emit(t)
            return
        i, what = self._clip_at(x)
        self.setCursor(Qt.SizeHorCursor if what in ("start", "end") else
                       Qt.OpenHandCursor if what == "move" else Qt.ArrowCursor)
        tip = fmt_time(self.time_fn(t)) + "  ·  right-click to add a moment here"
        tip_more = "<br><i>Drag to move it · drag its edges to resize · right-click for options</i>"
        if i is not None and i < len(getattr(self, "tips", [])):
            tip = f"<b>#{i + 1}</b> · {self.tips[i]}{tip_more}"
        else:
            for a, b, inter, text in self.sections:
                if a <= t <= b:
                    tip = (f"<b>{fmt_time(self.time_fn(a))} – {fmt_time(self.time_fn(b))}</b> · "
                           f"{text} <i>({inter * 10:.0f}/10)</i>")
                    break
        QToolTip.showText(ev.globalPosition().toPoint(), tip, self)


# ============================================================================ scrubber
class Scrubber(QWidget):
    """Thin seek bar under the player: where you are, drag anywhere to move. Parts of the stream
    with video are shaded blue; the rest is audio only."""
    seek = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(22)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.duration = 0.0
        self.position = 0.0
        self.video_spans: list[tuple[float, float]] = []
        self.time_fn = lambda t: t
        self._drag = False

    def set_duration(self, d):
        self.duration = float(d or 0)
        self.update()

    def set_position(self, t):
        if not self._drag:
            self.position = float(t)
            self.update()

    def set_video_spans(self, spans):
        self.video_spans = list(spans)
        self.update()

    def _x(self, t):
        return 7 + (self.width() - 14) * (t / self.duration if self.duration else 0)

    def _t(self, x):
        return max(0.0, min(self.duration, (x - 7) / max(1, self.width() - 14) * self.duration))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cy = self.height() / 2
        track = QRectF(7, cy - 3, self.width() - 14, 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#2a2e36"))
        p.drawRoundedRect(track, 3, 3)
        if not self.duration:
            return
        for a, b in self.video_spans:
            x0, x1 = self._x(a), self._x(b)
            p.setBrush(QColor("#3b6fb6"))
            p.drawRoundedRect(QRectF(x0, cy - 3, max(2, x1 - x0), 6), 2, 2)
        x = self._x(self.position)
        played = QColor(ACCENT)
        played.setAlpha(170)
        p.setBrush(played)
        p.drawRoundedRect(QRectF(7, cy - 1.5, max(0, x - 7), 3), 1.5, 1.5)
        p.setBrush(QColor("#ffffff"))
        p.setPen(QPen(ACCENT, 2))
        p.drawEllipse(QPointF(x, cy), 6, 6)

    def mousePressEvent(self, ev):
        if self.duration and ev.button() == Qt.LeftButton:
            self._drag = True
            self.position = self._t(ev.position().x())
            self.update()
            self.seek.emit(self.position)

    def mouseMoveEvent(self, ev):
        if not self.duration:
            return
        t = self._t(ev.position().x())
        if self._drag:
            self.position = t
            self.update()
            self.seek.emit(t)
        has_video = any(a <= t <= b for a, b in self.video_spans)
        QToolTip.showText(ev.globalPosition().toPoint(),
                          f"{fmt_time(self.time_fn(t))}" + ("" if has_video else "  ·  🔊 audio only"), self)

    def mouseReleaseEvent(self, ev):
        self._drag = False


# ============================================================================ one moment, zoomed in
class ClipTimeline(QWidget):
    """Zoomed timeline for the selected moment. Drag the handles to set the exact start and end,
    drag the middle to move the whole moment, click outside it to jump there."""
    span_changed = Signal(float, float, str)     # start, end, what moved ("start" | "end" | "move") - live
    span_committed = Signal(float, float, str)   # on release
    seek = Signal(float)
    menu_requested = Signal(float, object)       # right-click: time, global position
    other_menu = Signal(int, float, object)      # right-click on another moment: its index, time, position

    HANDLE = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(72)
        self.setMouseTracking(True)
        self.view = (0.0, 1.0)
        self.start = self.end = 0.0
        self.limits = (0.0, 1e9)
        self.playhead = 0.0
        self.times = self.score = None
        self.time_fn = lambda t: t
        self.user = False
        self.end_fn = None         # stream time for an end point (defaults to time_fn)
        self.cuts: list[tuple[float, float]] = []   # ✂ script cuts inside the moment (hatched)
        self.others: list[tuple] = []  # other moments nearby: (start, end, index, origin, hidden)
        self.hidden = False            # this moment is hidden: see-through hatched box with "?"
        self.playing = False
        self._drag = None          # ("start" | "end" | "move", grab offset)

    # -- data
    def set_clip(self, start, end, limits, times=None, score=None, user=False, keep_view=False):
        self.start, self.end = float(start), float(end)
        self.limits = (float(limits[0]), float(limits[1]))
        self.times, self.score, self.user = times, score, user
        if not keep_view or not (self.view[0] <= self.start and self.end <= self.view[1]):
            self._fit()
        self.update()

    def _fit(self):
        ln = max(1.0, self.end - self.start)
        pad = max(5.0, ln * 0.6)
        lo, hi = max(self.limits[0], self.start - pad), min(self.limits[1], self.end + pad)
        if hi - lo < 2:
            hi = lo + 2
        self.view = (lo, hi)

    def set_playhead(self, t):
        self.playhead = float(t)
        self.update()

    def set_playing(self, on: bool):
        if on != self.playing:
            self.playing = on
            self.update()

    # -- geometry
    def _x(self, t):
        a, b = self.view
        return 10 + (self.width() - 20) * ((t - a) / max(1e-6, b - a))

    def _t(self, x):
        a, b = self.view
        return a + (x - 10) / max(1, self.width() - 20) * (b - a)

    def _hit(self, x):
        xs, xe = self._x(self.start), self._x(self.end)
        if abs(x - xs) <= self.HANDLE + 2:
            return "start"
        if abs(x - xe) <= self.HANDLE + 2:
            return "end"
        if xs < x < xe:
            return "move"
        return None

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        p.fillRect(r, QColor("#121418"))
        top, bottom = 20, r.height() - 16
        h = bottom - top
        a, b = self.view
        # ticks every "nice" step
        span = b - a
        step = next((s for s in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800) if span / s <= 8), 3600)
        p.setFont(QFont(self.font().family(), 8))
        t = (int(a // step) + 1) * step
        while t < b:
            x = self._x(t)
            p.setPen(QPen(QColor("#262a31"), 1))
            p.drawLine(QPointF(x, top), QPointF(x, bottom))
            if 30 <= x <= self.width() - 30:
                p.setPen(QColor("#6b7280"))
                p.drawText(QRectF(x - 30, bottom + 1, 60, 14), Qt.AlignCenter, fmt_time(self.time_fn(t)))
            t += step
        # score curve for this stretch
        if self.score is not None and self.times is not None and len(self.times) > 1:
            sel = (self.times >= a) & (self.times <= b)
            ts, vs = self.times[sel], self.score[sel]
            if len(ts) > 1:
                path = QPainterPath(QPointF(self._x(ts[0]), bottom))
                for tt, vv in zip(ts, vs):
                    path.lineTo(QPointF(self._x(tt), bottom - float(vv) * h * 0.9))
                path.lineTo(QPointF(self._x(ts[-1]), bottom))
                p.fillPath(path, QColor(133, 199, 66, 45))
        # other moments that overlap this stretch: faint boxes, numbered (or "?" when hidden)
        p.setFont(QFont(self.font().family(), 8))
        boxes = []
        for (oa, ob, oi, oo, oh) in self.others:
            if ob < a or oa > b:
                continue
            x0, x1 = self._x(max(oa, a)), self._x(min(ob, b))
            oc = QColor(_origin_colour(oo))
            rr = QRectF(x0, top + 6, max(2, x1 - x0), h - 6)
            if oh:
                hc = QColor("#cbd5e1")
                hc.setAlpha(60)
                _hatch(p, rr, hc)
            else:
                oc.setAlpha(40)
                p.fillRect(rr, oc)
            boxes.append((rr, oa, ob, oi, oo, oh))
        # the moment
        col = _origin_colour(self.user) if isinstance(self.user, str) else (USER if self.user else ACCENT)
        xs, xe = self._x(self.start), self._x(self.end)
        if self.hidden:
            hc = QColor("#cbd5e1")
            hc.setAlpha(140 if self.playing else 90)
            _hatch(p, QRectF(xs, top, xe - xs, h), hc)
        else:
            fill = QColor(col)
            fill.setAlpha(150 if self.playing else 70)
            p.fillRect(QRectF(xs, top, xe - xs, h), fill)
        # other moments' outlines on top, so the overlap with this one shows
        for rr, oa, ob, oi, oo, oh in boxes:
            oc = QColor(_origin_colour(oo)) if not oh else QColor("#cbd5e1")
            oc.setAlpha(200)
            pen = QPen(oc, 1, Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(rr)
            p.setPen(QColor("#e5e7eb") if not oh else QColor("#cbd5e1"))
            tag = "?" if oh else ({"user": "★", "manual": "✎"}.get(oo, "") + str(oi + 1))
            if rr.width() >= 14:
                p.drawText(QRectF(rr.left() + 3, rr.top() + 1, max(12.0, rr.width() - 6), 13),
                           Qt.AlignLeft | Qt.AlignVCenter, tag)
        p.setPen(QPen(QColor("#ffffff") if self.playing else col, 2 if self.playing else 1))
        p.drawLine(QPointF(xs, top), QPointF(xe, top))
        p.drawLine(QPointF(xs, bottom), QPointF(xe, bottom))
        # script cuts: hatched, so you can see what's been taken out
        for a, b in self.cuts:
            x0, x1 = self._x(max(a, self.start)), self._x(min(b, self.end))
            if x1 - x0 < 0.5:
                continue
            r_ = QRectF(x0, top, x1 - x0, h)
            p.fillRect(r_, QColor(18, 20, 24, 200))
            p.save()
            p.setClipRect(r_)
            p.setPen(QPen(QColor(239, 68, 68, 150), 1))
            k = -h
            while k < r_.width():
                p.drawLine(QPointF(x0 + k, bottom), QPointF(x0 + k + h, top))
                k += 6
            p.restore()
        # handles
        for x in (xs, xe):
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(x - self.HANDLE / 2, top - 2, self.HANDLE, h + 4), 3, 3)
            p.setPen(QPen(QColor("#1b1d22"), 1))
            for k in (-4, 0, 4):
                p.drawLine(QPointF(x - 1.5, top + h / 2 + k), QPointF(x + 1.5, top + h / 2 + k))
        # labels: start, length, end
        p.setPen(QColor("#e5e7eb"))
        p.setFont(QFont(self.font().family(), 9))
        p.drawText(QRectF(xs - 70, 2, 66, 16), Qt.AlignRight | Qt.AlignVCenter, fmt_time(self.time_fn(self.start)))
        p.drawText(QRectF(xe + 4, 2, 70, 16), Qt.AlignLeft | Qt.AlignVCenter, fmt_time((self.end_fn or self.time_fn)(self.end)))
        p.setPen(QColor("#9ca3af"))
        span = self.end - self.start
        removed = sum(max(0.0, min(b, self.end) - max(a, self.start)) for a, b in self.cuts)
        lab = f"{span:.1f} s → {span - removed:.1f} s after cuts" if removed > 0.05 else f"{span:.1f} s"
        if self.hidden:
            lab = "?   " + lab
        if self.playing:
            p.setPen(QColor("#ffffff"))
            lab = "▶ playing  ·  " + lab
        if self.hidden:
            fb = QFont(self.font().family(), 9)
            fb.setBold(True)
            p.setFont(fb)
        p.drawText(QRectF(xs - 60, 2, max(1, xe - xs) + 120, 16), Qt.AlignCenter, lab)
        # playhead
        if a <= self.playhead <= b:
            x = self._x(self.playhead)
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.drawLine(QPointF(x, top - 4), QPointF(x, bottom))

    # -- mouse
    def mousePressEvent(self, ev):
        if ev.button() == Qt.RightButton:
            t = self._t(ev.position().x())
            if not (self.start <= t <= self.end):
                hits = [o for o in self.others if o[0] <= t <= o[1]]
                if hits:                      # the shortest one under the pointer
                    o = min(hits, key=lambda o: o[1] - o[0])
                    self.other_menu.emit(o[2], t, ev.globalPosition().toPoint())
                    return
            self.menu_requested.emit(t, ev.globalPosition().toPoint())
            return
        if ev.button() != Qt.LeftButton:
            return
        x = ev.position().x()
        hit = self._hit(x)
        if hit:
            self._drag = (hit, self._t(x) - self.start)
        else:
            self.seek.emit(max(self.limits[0], min(self.limits[1], self._t(x))))

    def contextMenuEvent(self, ev):
        ev.accept()

    def mouseMoveEvent(self, ev):
        x = ev.position().x()
        if not self._drag:
            hit = self._hit(x)
            self.setCursor(Qt.SizeHorCursor if hit in ("start", "end") else
                           Qt.OpenHandCursor if hit == "move" else Qt.PointingHandCursor)
            QToolTip.showText(ev.globalPosition().toPoint(),
                              {"start": "Drag to change where the clip starts",
                               "end": "Drag to change where the clip ends",
                               "move": "Drag to move the whole clip"}.get(hit, fmt_time(self.time_fn(self._t(x)))),
                              self)
            return
        what, grab = self._drag
        t = round(self._t(x) * 10) / 10            # 0.1 s steps
        lo, hi = max(self.limits[0], self.view[0]), min(self.limits[1], self.view[1])   # never off-screen
        if what == "start":
            self.start = max(lo, min(t, self.end - 1.0))
        elif what == "end":
            self.end = min(hi, max(t, self.start + 1.0))
        else:
            ln = self.end - self.start
            s0 = max(lo, min(hi - ln, round((self._t(x) - grab) * 10) / 10))
            self.start, self.end = s0, s0 + ln
        self.update()
        self.span_changed.emit(self.start, self.end, what)

    def mouseReleaseEvent(self, ev):
        if self._drag:
            what = self._drag[0]
            self._drag = None
            if self.start < self.view[0] + 0.5 or self.end > self.view[1] - 0.5:
                self._fit()
                self.update()
            self.span_committed.emit(self.start, self.end, what)
