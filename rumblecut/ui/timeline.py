"""Stream-wide 'excitement' timeline: score curve, highlight regions, playhead. Click to seek."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QToolTip, QWidget

ACCENT = QColor("#85C742")
USER = QColor("#f5b942")          # your picks (added by hand or from the Summary) - gold, not green


def fmt_time(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 3600:d}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:d}:{t % 60:02d}"


class Timeline(QWidget):
    seek = Signal(float)
    clip_clicked = Signal(int)

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
        self.user: list[bool] = []          # which clips are your picks
        self.sections: list[tuple[float, float, float, str]] = []   # AI summary: start, end, interest, text
        self._dragging = False

    def set_data(self, duration, times=None, score=None):
        self.duration = float(duration or 0)
        self.times, self.score = times, score
        self.update()

    def set_clips(self, clips: list[tuple[float, float]], selected: int = -1, tips: list[str] | None = None,
                  user: list[bool] | None = None):
        self.clips, self.selected = clips, selected
        self.tips = tips or []
        if user is not None:
            self.user = user
        self.update()

    def set_sections(self, sections):
        self.sections = list(sections)
        self.update()

    def set_playhead(self, t: float):
        self.playhead = t
        self.update()

    def _x(self, t: float) -> float:
        return 8 + (self.width() - 16) * (t / self.duration if self.duration else 0)

    def _t(self, x: float) -> float:
        return max(0.0, min(self.duration, (x - 8) / max(1, self.width() - 16) * self.duration))

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
        steps = 10
        for i in range(steps + 1):
            x = self._x(self.duration * i / steps)
            p.setPen(QPen(QColor("#262a31"), 1))
            p.drawLine(QPointF(x, top), QPointF(x, bottom))
            p.setPen(QColor("#7c8591"))
            lx = min(max(x - 30, 0), self.width() - 60)
            align = Qt.AlignLeft if i == 0 else (Qt.AlignRight if i == steps else Qt.AlignCenter)
            p.drawText(QRectF(lx, bottom + 2, 60, 14), align, fmt_time(self.time_fn(self.duration * i / steps)))
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
        # highlight regions
        for i, (s, e) in enumerate(self.clips):
            x0, x1 = self._x(s), self._x(e)
            sel = i == self.selected
            base_col = USER if (i < len(self.user) and self.user[i]) else ACCENT
            col = QColor(base_col)
            col.setAlpha(110 if sel else 50)
            p.fillRect(QRectF(x0, top, max(2, x1 - x0), h), col)
            edge = QColor(base_col)
            edge.setAlpha(255 if sel else 160)
            p.setPen(QPen(edge, 2 if sel else 1))
            p.drawRect(QRectF(x0, top, max(2, x1 - x0), h))
            p.setPen(QColor("#e5e7eb"))
            star = "★" if (i < len(self.user) and self.user[i]) else ""
            p.drawText(QRectF(x0 - 10, 1, max(20, x1 - x0 + 20), 16), Qt.AlignCenter, f"{star}{i + 1}")
        # score curve
        if self.score is not None and len(self.score) > 1:
            w = max(2, self.width() - 16)
            idx = np.linspace(0, len(self.score) - 1, w).astype(int)
            # max-pool so short spikes stay visible when zoomed out
            bins = np.array_split(self.score, w) if len(self.score) > w else None
            vals = np.array([b.max() if len(b) else 0 for b in bins]) if bins else self.score[idx]
            path = QPainterPath(QPointF(8, bottom))
            for j, v in enumerate(vals):
                path.lineTo(QPointF(8 + j * (self.width() - 16) / (len(vals) - 1), bottom - v * h))
            path.lineTo(QPointF(self.width() - 8, bottom))
            grad = QLinearGradient(0, top, 0, bottom)
            grad.setColorAt(0, QColor(255, 120, 60, 200))
            grad.setColorAt(0.5, QColor(133, 199, 66, 150))
            grad.setColorAt(1, QColor(60, 120, 200, 60))
            p.fillPath(path, grad)
        # playhead
        x = self._x(self.playhead)
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.drawLine(QPointF(x, top - 4), QPointF(x, bottom))

    def mousePressEvent(self, ev):
        if not self.duration or ev.button() != Qt.LeftButton:
            return
        t = self._t(ev.position().x())
        for i, (s, e) in enumerate(self.clips):
            if s <= t <= e:
                self.clip_clicked.emit(i)
                break
        self._dragging = True
        self.seek.emit(t)

    def mouseReleaseEvent(self, ev):
        self._dragging = False

    def mouseMoveEvent(self, ev):
        if not self.duration:
            return
        t = self._t(ev.position().x())
        if self._dragging and ev.buttons() & Qt.LeftButton:      # drag to scrub
            self.seek.emit(t)
            return
        tip = fmt_time(self.time_fn(t))
        for i, (s, e) in enumerate(self.clips):
            if s <= t <= e and i < len(getattr(self, "tips", [])):
                tip = f"<b>#{i + 1}</b> · {self.tips[i]}"
                break
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
        # the moment
        col = USER if self.user else ACCENT
        xs, xe = self._x(self.start), self._x(self.end)
        fill = QColor(col)
        fill.setAlpha(70)
        p.fillRect(QRectF(xs, top, xe - xs, h), fill)
        p.setPen(QPen(col, 1))
        p.drawLine(QPointF(xs, top), QPointF(xe, top))
        p.drawLine(QPointF(xs, bottom), QPointF(xe, bottom))
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
        p.drawText(QRectF(xs, 2, max(1, xe - xs), 16), Qt.AlignCenter, f"{self.end - self.start:.1f} s")
        # playhead
        if a <= self.playhead <= b:
            x = self._x(self.playhead)
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.drawLine(QPointF(x, top - 4), QPointF(x, bottom))

    # -- mouse
    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        x = ev.position().x()
        hit = self._hit(x)
        if hit:
            self._drag = (hit, self._t(x) - self.start)
        else:
            self.seek.emit(max(self.limits[0], min(self.limits[1], self._t(x))))

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
        lo, hi = self.limits
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
