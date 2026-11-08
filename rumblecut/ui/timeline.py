"""Stream-wide 'excitement' timeline: score curve, highlight regions, playhead. Click to seek."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QToolTip, QWidget

ACCENT = QColor("#85C742")


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
        self.playhead = 0.0

    def set_data(self, duration, times=None, score=None):
        self.duration = float(duration or 0)
        self.times, self.score = times, score
        self.update()

    def set_clips(self, clips: list[tuple[float, float]], selected: int = -1, tips: list[str] | None = None):
        self.clips, self.selected = clips, selected
        self.tips = tips or []
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
            p.drawText(QRectF(lx, bottom + 2, 60, 14), align, fmt_time(self.duration * i / steps))
        # highlight regions
        for i, (s, e) in enumerate(self.clips):
            x0, x1 = self._x(s), self._x(e)
            sel = i == self.selected
            col = QColor(ACCENT)
            col.setAlpha(110 if sel else 50)
            p.fillRect(QRectF(x0, top, max(2, x1 - x0), h), col)
            p.setPen(QPen(ACCENT if sel else QColor(133, 199, 66, 160), 2 if sel else 1))
            p.drawRect(QRectF(x0, top, max(2, x1 - x0), h))
            p.setPen(QColor("#e5e7eb"))
            p.drawText(QRectF(x0 - 10, 1, max(20, x1 - x0 + 20), 16), Qt.AlignCenter, str(i + 1))
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
        if not self.duration:
            return
        t = self._t(ev.position().x())
        for i, (s, e) in enumerate(self.clips):
            if s <= t <= e:
                self.clip_clicked.emit(i)
                break
        self.seek.emit(t)

    def mouseMoveEvent(self, ev):
        if self.duration:
            t = self._t(ev.position().x())
            tip = fmt_time(t)
            for i, (s, e) in enumerate(self.clips):
                if s <= t <= e and i < len(getattr(self, "tips", [])):
                    tip = f"<b>#{i + 1}</b> · {self.tips[i]}"
                    break
            QToolTip.showText(ev.globalPosition().toPoint(), tip, self)
