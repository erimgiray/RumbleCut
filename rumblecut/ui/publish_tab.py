"""🚀 Publish tab: post moments to YouTube, Instagram, TikTok and X straight from RumbleCut."""
from __future__ import annotations

import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QDateTime, Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateTimeEdit, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QRadioButton, QScrollArea, QSizePolicy, QSplitter, QVBoxLayout, QWidget,
)

from .. import ai, config, publish as pb, transcribe
from .timeline import fmt_time
from .workers import Worker

ICONS = {"youtube": "▶", "instagram": "◎", "tiktok": "♪", "x": "𝕏"}
FORMAT_NAMES = {"vertical": "Vertical 9:16", "original": "Original shape"}


class PlatformCard(QGroupBox):
    """One platform: tick to post there, its text fields, format and (YouTube) privacy."""

    def __init__(self, tab, platform: str):
        super().__init__()
        self.tab, self.platform = tab, platform
        self.setCheckable(True)
        self.toggled.connect(lambda on: tab._card_changed(self))
        v = QVBoxLayout(self)
        v.setSpacing(6)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        self.btn_connect = QPushButton("Connect…")
        self.btn_connect.setObjectName("link")
        self.btn_connect.clicked.connect(tab.open_accounts)
        srow = QHBoxLayout()
        srow.addWidget(self.status, 1)
        srow.addWidget(self.btn_connect)
        v.addLayout(srow)
        f = QFormLayout()
        f.setLabelAlignment(Qt.AlignRight)
        self.fields: dict[str, QWidget] = {}
        if platform == "youtube":
            self._line(f, "title", "Title", 100)
            self._text(f, "description", "Description", 70)
            self._line(f, "tags", "Tags", 480, "comma separated")
            self.privacy = QComboBox()
            self.privacy.addItems(["public", "unlisted", "private"])
            self.privacy.currentTextChanged.connect(lambda t: tab._field_changed(self.platform, "privacy", t))
            f.addRow("Visibility", self.privacy)
        elif platform == "x":
            self._text(f, "text", "Post", 60)
            self.counter = QLabel("")
            self.counter.setObjectName("muted")
            f.addRow("", self.counter)
        else:
            self._text(f, "caption", "Caption", 70)
            if platform == "tiktok":
                n = QLabel("Arrives in your TikTok inbox as a draft - RumbleCut copies this caption so you "
                           "can paste it, then press Post in TikTok.")
                n.setObjectName("muted")
                n.setWordWrap(True)
                f.addRow("", n)
        self.fmt = QComboBox()
        for k, name in FORMAT_NAMES.items():
            self.fmt.addItem(name, k)
        self.fmt.currentIndexChanged.connect(lambda _: tab._field_changed(self.platform, "format", self.fmt.currentData()))
        f.addRow("Shape", self.fmt)
        v.addLayout(f)

    def _line(self, f, key, label, maxlen, ph=""):
        e = QLineEdit()
        e.setMaxLength(maxlen)
        e.setPlaceholderText(ph)
        e.textEdited.connect(lambda t, k=key: self.tab._field_changed(self.platform, k, t))
        self.fields[key] = e
        f.addRow(label, e)

    def _text(self, f, key, label, h):
        e = QPlainTextEdit()
        e.setFixedHeight(h)
        e.textChanged.connect(lambda k=key, e=e: self.tab._field_changed(self.platform, k, e.toPlainText()))
        self.fields[key] = e
        f.addRow(label, e)

    def set_values(self, d: dict, fmt: str):
        for k, w in self.fields.items():
            val = d.get(k, "")
            if k == "tags" and isinstance(val, list):
                val = ", ".join(val)
            w.blockSignals(True)
            (w.setPlainText if isinstance(w, QPlainTextEdit) else w.setText)(str(val or ""))
            w.blockSignals(False)
        if self.platform == "youtube":
            self.privacy.blockSignals(True)
            self.privacy.setCurrentText(d.get("privacy", "public"))
            self.privacy.blockSignals(False)
        self.fmt.blockSignals(True)
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(fmt)))
        self.fmt.blockSignals(False)
        self.update_counter()

    def update_counter(self):
        if self.platform == "x":
            n = len(self.fields["text"].toPlainText())
            self.counter.setText(f"{n}/280" + ("  - too long" if n > 280 else ""))

    def refresh_status(self):
        cid, _ = pb.get_credentials(self.platform)
        name = pb.PLATFORMS[self.platform]
        if pb.is_connected(self.platform):
            self.setTitle(f"{ICONS[self.platform]}  {name}  ·  {pb.account_name(self.platform)}")
            self.status.setText({"youtube": "Posts straight to your channel (≤ 3 min vertical = a Short).",
                                 "instagram": "Posts a Reel to your profile.",
                                 "tiktok": "Sends a draft to your TikTok inbox.",
                                 "x": "Posts with the video attached (about $0.015 per post)."}[self.platform])
            self.btn_connect.hide()
        else:
            self.setTitle(f"{ICONS[self.platform]}  {name}  ·  not connected")
            self.status.setText("Connect your account in Settings → Publishing accounts." if cid else
                                "Set up once in Settings → Publishing accounts (free).")
            self.btn_connect.show()


class PublishTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._worker: Worker | None = None
        self._loading = False
        self._texts: dict = {}
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(8)
        h = QLabel("🚀  Publish")
        h.setObjectName("h2")
        v.addWidget(h)

        split = QSplitter(Qt.Horizontal)
        # ---- moments
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("Moment"))
        self.moments = QListWidget()
        self.moments.setWordWrap(True)
        self.moments.currentRowChanged.connect(lambda _: self._moment_changed())
        lv.addWidget(self.moments, 1)
        self.btn_ai_all = QPushButton("✨ Write posts for all ticked")
        self.btn_ai_all.setToolTip("Let your AI write titles, captions and hashtags for every ticked moment")
        self.btn_ai_all.clicked.connect(lambda: self.write_with_ai(all_ticked=True))
        lv.addWidget(self.btn_ai_all)
        split.addWidget(left)

        # ---- platforms
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.btn_ai = QPushButton("✨ Write titles, captions && hashtags with AI")
        self.btn_ai.clicked.connect(lambda: self.write_with_ai(all_ticked=False))
        top.addWidget(self.btn_ai)
        top.addStretch()
        rv.addLayout(top)
        grid = QGridLayout()
        grid.setSpacing(10)
        self.cards: dict[str, PlatformCard] = {}
        for i, p in enumerate(pb.PLATFORMS):
            c = PlatformCard(self, p)
            self.cards[p] = c
            grid.addWidget(c, i // 2, i % 2)
        cards_w = QWidget()
        cards_w.setLayout(grid)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(cards_w)
        rv.addWidget(sc, 1)

        when = QHBoxLayout()
        self.r_now = QRadioButton("Post now")
        self.r_later = QRadioButton("Schedule for")
        self.r_now.setChecked(True)
        self.when = QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600))
        self.when.setDisplayFormat("ddd d MMM yyyy  HH:mm")
        self.when.setCalendarPopup(True)
        self.when.setEnabled(False)
        self.r_later.toggled.connect(self.when.setEnabled)
        when.addWidget(self.r_now)
        when.addSpacing(12)
        when.addWidget(self.r_later)
        when.addWidget(self.when)
        when.addStretch()
        rv.addLayout(when)
        self.when_note = QLabel("YouTube schedules the post itself; the others are sent by Windows at that time "
                                "(RumbleCut can be closed, but the PC must be on).")
        self.when_note.setObjectName("muted")
        self.when_note.setWordWrap(True)
        self.when_note.setVisible(False)
        self.r_later.toggled.connect(self.when_note.setVisible)
        rv.addWidget(self.when_note)

        prow = QHBoxLayout()
        self.btn_post = QPushButton("🚀 Post this moment")
        self.btn_post.setObjectName("primary")
        self.btn_post.clicked.connect(lambda: self.post(all_ticked=False))
        self.btn_post_all = QPushButton("🚀 Post all ticked moments")
        self.btn_post_all.clicked.connect(lambda: self.post(all_ticked=True))
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setFixedHeight(18)
        self.progress.setFormat("")
        self.progress.valueChanged.connect(
            lambda x: self.progress.setFormat(f"{x / 10:.0f}%" if x > 0 and self.progress.maximum() else ""))
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setObjectName("danger")
        keep = self.btn_cancel.sizePolicy(); keep.setRetainSizeWhenHidden(True); self.btn_cancel.setSizePolicy(keep)
        self.btn_cancel.hide()
        self.btn_cancel.clicked.connect(self._cancel)
        prow.addWidget(self.btn_post)
        prow.addWidget(self.btn_post_all)
        prow.addWidget(self.progress, 1)
        prow.addWidget(self.btn_cancel)
        rv.addLayout(prow)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        rv.addWidget(self.status)

        rv.addWidget(QLabel("Posted and scheduled"))
        self.history = QListWidget()
        self.history.setMaximumHeight(130)
        self.history.itemDoubleClicked.connect(self._open_result)
        self.history.setToolTip("Double-click to open the post")
        rv.addWidget(self.history)
        hrow = QHBoxLayout()
        self.btn_unschedule = QPushButton("Cancel scheduled post")
        self.btn_unschedule.clicked.connect(self._unschedule)
        hrow.addStretch()
        hrow.addWidget(self.btn_unschedule)
        rv.addLayout(hrow)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 4)
        split.setSizes([260, 980])
        v.addWidget(split, 1)

        # post anything that came due while RumbleCut was open
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._run_due)
        self._timer.start(60_000)
        self.refresh()

    # ------------------------------------------------------------------ data
    @property
    def project(self):
        return self.win.project

    def current_clip(self):
        r = self.moments.currentRow()
        return self.project.clips[r] if 0 <= r < len(self.project.clips) else None

    def _pub(self, c) -> dict:
        d = c.publish
        for p in pb.PLATFORMS:
            d.setdefault(p, {})
        d.setdefault("formats", {})
        d.setdefault("platforms", [p for p in pb.PLATFORMS if pb.is_connected(p)])
        y = d["youtube"]
        y.setdefault("title", c.title)
        y.setdefault("privacy", "public")
        return d

    def refresh(self):
        keep = self.moments.currentRow()
        self.moments.blockSignals(True)
        self.moments.clear()
        for i, c in enumerate(self.project.clips):
            a = self.project.to_stream(c.start + c.offset)
            posted = c.publish.get("posted", {}) if c.publish else {}
            done = "  ·  " + " ".join(ICONS[p] for p in pb.PLATFORMS if p in posted) if posted else ""
            it = QListWidgetItem(f"{'☑' if c.enabled else '☐'} {i + 1}. {c.title}\n{fmt_time(a)} · "
                                 f"{c.out_length:.0f}s{done}")
            if not c.enabled:
                it.setForeground(QBrush(QColor("#6b7280")))
            self.moments.addItem(it)
        self.moments.blockSignals(False)
        if self.moments.count():
            self.moments.setCurrentRow(max(0, min(keep, self.moments.count() - 1)) if keep >= 0 else 0)
        for c in self.cards.values():
            c.refresh_status()
        self._moment_changed()
        self._fill_history()
        busy = self._worker is not None and self._worker.isRunning()
        has = self.moments.count() > 0
        for b in (self.btn_post, self.btn_post_all, self.btn_ai, self.btn_ai_all):
            b.setEnabled(has and not busy)

    def _moment_changed(self):
        c = self.current_clip()
        self._loading = True
        for p, card in self.cards.items():
            if c is None:
                card.setEnabled(False)
                continue
            card.setEnabled(True)
            d = self._pub(c)
            card.blockSignals(True)
            card.setChecked(p in d["platforms"])
            card.blockSignals(False)
            card.set_values(d[p], d["formats"].get(p) or pb.default_format(p, c.out_length))
        self._loading = False

    def _field_changed(self, platform, key, val):
        if self._loading:
            return
        c = self.current_clip()
        if not c:
            return
        d = self._pub(c)
        if key == "format":
            d["formats"][platform] = val
        elif key == "tags":
            d[platform]["tags"] = [t.strip() for t in str(val).split(",") if t.strip()]
        else:
            d[platform][key] = val
        if platform == "x":
            self.cards["x"].update_counter()

    def _card_changed(self, card):
        if self._loading:
            return
        c = self.current_clip()
        if c:
            d = self._pub(c)
            ps = set(d["platforms"])
            (ps.add if card.isChecked() else ps.discard)(card.platform)
            d["platforms"] = [p for p in pb.PLATFORMS if p in ps]

    def open_accounts(self):
        self.win.tabs.setCurrentIndex(self.win.settings_index)
        if hasattr(self.win, "pub_accounts_group"):
            QTimer.singleShot(50, lambda: self.win._settings_scroll.ensureWidgetVisible(self.win.pub_accounts_group))

    # ------------------------------------------------------------------ AI
    def write_with_ai(self, all_ticked: bool):
        if self._busy():
            return
        targets = [c for c in self.project.clips if c.enabled] if all_ticked else [x for x in [self.current_clip()] if x]
        if not targets:
            return
        if not ai.is_configured():
            QMessageBox.information(self, "Publish", "Set up an AI model in Settings → AI models first.")
            return
        if not transcribe.available():
            QMessageBox.information(self, "Publish", "Speech-to-text isn't included in this copy of RumbleCut.")
            return
        stream = self.project.title

        def job(on_progress=None, cancel=None):
            out = []
            for i, c in enumerate(targets):
                if cancel():
                    break
                on_progress(i / len(targets), f"Listening to “{c.title}”…")
                segs = transcribe.transcribe(c.source, start=c.start, end=c.end, cancel=cancel)
                on_progress((i + 0.6) / len(targets), f"Writing posts for “{c.title}”…")
                try:
                    out.append((c, pb.write_posts(" ".join(t for _, _, t in segs), stream, c.title), None))
                except Exception as e:
                    out.append((c, None, str(e)))
            return out

        def done(results):
            errs = []
            for c, posts, err in results:
                if not posts:
                    errs.append(err)
                    continue
                d = self._pub(c)
                for p, vals in posts.items():
                    keep_priv = d[p].get("privacy")
                    d[p].update({k: v for k, v in vals.items() if v})
                    if keep_priv:
                        d[p]["privacy"] = keep_priv
            self._moment_changed()
            ok = sum(1 for _, p, _ in results if p)
            self.status.setText(f"✨ Wrote posts for {ok} moment{'s' if ok != 1 else ''} with {ai.describe()} - "
                                "check them before posting")
            if errs:
                QMessageBox.warning(self, "Publish", "\n".join(dict.fromkeys(errs)))
        self._run(job, done, "Writing posts…")

    # ------------------------------------------------------------------ posting
    def _when_iso(self) -> str:
        if not self.r_later.isChecked():
            return ""
        dt = self.when.dateTime().toPython()
        return dt.astimezone().astimezone(timezone.utc).isoformat(timespec="seconds")

    def _plan(self, clips) -> tuple[list, list[str]]:
        """(clip, platform, format, meta) for everything that's ticked, plus problems to fix first."""
        plan, problems = [], []
        for c in clips:
            d = self._pub(c)
            for p in d["platforms"]:
                name = pb.PLATFORMS[p]
                if not pb.is_connected(p):
                    problems.append(f"{name} isn't connected (Settings → Publishing accounts).")
                    continue
                meta = dict(d[p])
                if p == "youtube":
                    meta["tags"] = d[p].get("tags", [])
                    if not meta.get("title", "").strip():
                        problems.append(f"“{c.title}”: YouTube needs a title.")
                if p == "x" and len(meta.get("text", "")) > 280:
                    problems.append(f"“{c.title}”: the X post is over 280 characters.")
                if c.out_length > pb.MAX_SECONDS[p]:
                    problems.append(f"“{c.title}” is {c.out_length:.0f}s - {name} allows up to "
                                    f"{pb.MAX_SECONDS[p]}s. Trim it or untick {name}.")
                fmt = d["formats"].get(p) or pb.default_format(p, c.out_length)
                plan.append((c, p, fmt, meta))
        return plan, list(dict.fromkeys(problems))

    def post(self, all_ticked: bool):
        if self._busy():
            return
        clips = [c for c in self.project.clips if c.enabled] if all_ticked else [x for x in [self.current_clip()] if x]
        if not clips:
            return
        plan, problems = self._plan(clips)
        if problems:
            QMessageBox.warning(self, "Publish", "Fix these first:\n\n• " + "\n• ".join(problems))
            return
        if not plan:
            QMessageBox.information(self, "Publish", "Tick at least one platform to post to.")
            return
        when = self._when_iso()
        if when and datetime.fromisoformat(when) < datetime.now(timezone.utc) + timedelta(minutes=2):
            QMessageBox.information(self, "Publish", "Pick a time at least a few minutes from now.")
            return
        names = ", ".join(sorted({pb.PLATFORMS[p] for _, p, _, _ in plan}))
        what = f"{len(clips)} moment{'s' if len(clips) != 1 else ''}"
        verb = f"Schedule {what} for {self.when.dateTime().toString('ddd d MMM HH:mm')}" if when else f"Post {what} now"
        if QMessageBox.question(self, "Publish", f"{verb} to {names}?") != QMessageBox.Yes:
            return
        out_dir = config.app_data_dir() / "publish" / "renders"

        def job(on_progress=None, cancel=None):
            results = []
            n = len(plan)
            for i, (c, p, fmt, meta) in enumerate(plan):
                if cancel():
                    break
                base = i / n
                step = lambda f, m, base=base: on_progress(base + max(0.0, f) / n, m)
                step(0.0, f"“{c.title}”: rendering for {pb.PLATFORMS[p]}…")
                video = pb.render_for(c, fmt, out_dir, on_progress=lambda f, m: step(f * 0.5, m), cancel=cancel)
                job_ = pb.PostJob(p, video, meta, c.title, when="" if p == "youtube" else when)
                if when and p == "youtube":
                    job_.meta = dict(meta, publish_at=when)
                if job_.when:
                    pb.enqueue(job_)
                    results.append((c, job_))
                    continue
                pb.post(job_, on_progress=lambda f, m: step(0.5 + f * 0.5, m), cancel=cancel)
                if job_.status == "done":
                    jobs = pb.load_queue()                      # keep a record in the history too
                    jobs.append(job_)
                    pb.save_queue(jobs)
                results.append((c, job_))
            return results

        def done(results):
            ok = [j for _, j in results if j.status == "done"]
            waiting = [j for _, j in results if j.status == "waiting"]
            failed = [(c, j) for c, j in results if j.status == "failed"]
            for c, j in results:
                if j.status in ("done", "waiting"):
                    c.publish.setdefault("posted", {})[j.platform] = j.result or f"scheduled {j.when}"
            tt = [j for j in ok if j.platform == "tiktok"]
            if tt:
                cap = next((c.publish.get("tiktok", {}).get("caption", "") for c, j in results if j in tt), "")
                if cap:
                    QApplication.clipboard().setText(cap)
            msg = []
            if ok:
                msg.append(f"✓ Posted {len(ok)}")
            if waiting:
                msg.append(f"⏰ {len(waiting)} scheduled")
            if failed:
                msg.append(f"⚠ {len(failed)} failed")
            if tt:
                msg.append("TikTok caption copied - paste it in the TikTok app")
            self.status.setText("  ·  ".join(msg))
            self.refresh()
            if failed:
                QMessageBox.warning(self, "Publish", "\n".join(f"{c.title} → {pb.PLATFORMS[j.platform]}: {j.error}"
                                                                for c, j in failed))
        self._run(job, done, "Posting…")

    # ------------------------------------------------------------------ history / queue
    def _fill_history(self):
        self.history.clear()
        jobs = pb.load_queue()
        for j in reversed(jobs[-60:]):
            name = pb.PLATFORMS.get(j.platform, j.platform)
            if j.status == "waiting":
                try:
                    t = datetime.fromisoformat(j.when).astimezone().strftime("%a %d %b %H:%M")
                except ValueError:
                    t = j.when
                text, col = f"⏰ {t} · {j.clip_title} → {name} (scheduled)", "#f5d48a"
            elif j.status == "done":
                t = (datetime.fromisoformat(j.finished_at).astimezone().strftime("%d %b %H:%M")
                     if j.finished_at else "")
                text, col = f"✓ {t} · {j.clip_title} → {name} · {j.result}", "#c7ef9e"
            else:
                text, col = f"⚠ {j.clip_title} → {name} · {j.error}", "#fca5a5"
            it = QListWidgetItem(text)
            it.setForeground(QBrush(QColor(col)))
            it.setData(Qt.UserRole, j.id)
            self.history.addItem(it)

    def _open_result(self, it):
        j = next((x for x in pb.load_queue() if x.id == it.data(Qt.UserRole)), None)
        if j and j.result.startswith("http"):
            webbrowser.open(j.result)

    def _unschedule(self):
        it = self.history.currentItem()
        if not it:
            return
        jobs = pb.load_queue()
        j = next((x for x in jobs if x.id == it.data(Qt.UserRole)), None)
        if not j or j.status != "waiting":
            QMessageBox.information(self, "Publish", "Pick a scheduled (⏰) post first. YouTube schedules are "
                                    "managed in YouTube Studio.")
            return
        jobs.remove(j)
        pb.save_queue(jobs)
        self._fill_history()
        self.status.setText(f"Cancelled the scheduled post of “{j.clip_title}” to {pb.PLATFORMS[j.platform]}")

    def _run_due(self):
        if self._busy():
            return
        now = datetime.now(timezone.utc)
        due = [j for j in pb.load_queue() if j.status == "waiting" and j.when
               and datetime.fromisoformat(j.when) <= now]
        if not due:
            return
        w = Worker(lambda on_progress=None, cancel=None: pb.run_due())
        w.background = True
        w.done.connect(lambda jobs: (self._fill_history(), jobs and self.win._show_toast(
            f"🚀 Posted {sum(1 for j in jobs if j.status == 'done')} scheduled clip(s)")))
        self.win._start(w)

    # ------------------------------------------------------------------ worker plumbing
    def _busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _run(self, job, done, label):
        w = Worker(job)
        self._worker = w
        self.progress.setValue(0)
        self.status.setText(label)
        self.btn_cancel.setText("Cancel")
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.show()
        for b in (self.btn_post, self.btn_post_all, self.btn_ai, self.btn_ai_all):
            b.setEnabled(False)
        w.progress.connect(lambda f, m: (self.progress.setValue(int(max(f, 0) * 1000)), self.status.setText(m)))
        w.done.connect(done)
        w.failed.connect(lambda m: (self.status.setText("⚠ " + m), QMessageBox.warning(self, "Publish", m)))
        w.cancelled.connect(lambda: self.status.setText("Cancelled"))

        def finished():
            self._worker = None
            self.btn_cancel.hide()
            self.progress.setValue(1000 if "Cancel" not in self.status.text() else 0)
            self.refresh()
        w.finished.connect(finished)
        self.win._start(w)

    def _cancel(self):
        if self._worker is not None:
            self._worker.cancel()
            self.btn_cancel.setEnabled(False)
            self.btn_cancel.setText("Stopping…")
