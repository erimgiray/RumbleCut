"""Background jobs so the window never freezes during downloads, analysis or export."""
from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

from ..ffmpeg_tools import Cancelled


class Worker(QThread):
    progress = Signal(float, str)
    done = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, fn: Callable[..., Any], *args, **kwargs):
        super().__init__()
        self._fn, self._args, self._kwargs = fn, args, kwargs
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def is_cancelled(self) -> bool:
        return self._cancel

    def run(self):
        try:
            kw = dict(self._kwargs)
            kw.setdefault("on_progress", lambda f, m="": self.progress.emit(float(f), str(m)))
            kw.setdefault("cancel", self.is_cancelled)
            result = self._fn(*self._args, **kw)
            if self._cancel:
                self.cancelled.emit()
            else:
                self.done.emit(result)
        except Cancelled:
            self.cancelled.emit()
        except Exception as e:  # surfaced to the user in a dialog
            msg = str(e) or e.__class__.__name__
            print(traceback.format_exc())
            self.failed.emit(msg)
