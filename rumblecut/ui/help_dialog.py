"""The Help page: topics on the left (with search), the explanation on the right."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextBlockFormat, QTextCursor
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QTextBrowser,
                               QVBoxLayout)

from .. import APP_NAME, __version__
from ..help import TOPICS


class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} help")
        self.setObjectName("helpdlg")
        self.resize(1040, 720)
        h = QHBoxLayout(self)
        h.setContentsMargins(16, 16, 16, 16)
        h.setSpacing(14)
        left = QVBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search the help…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        left.addWidget(self.search)
        self.topics = QListWidget()
        self.topics.setObjectName("helptopics")
        self.topics.setFixedWidth(250)
        for icon, title, _ in TOPICS:
            self.topics.addItem(QListWidgetItem(f"{icon}   {title}"))
        self.topics.currentRowChanged.connect(self._show)
        left.addWidget(self.topics, 1)
        h.addLayout(left)
        self.text = QTextBrowser()
        self.text.setObjectName("helptext")
        self.text.setOpenExternalLinks(True)
        self.text.document().setDocumentMargin(18)
        h.addWidget(self.text, 1)
        self.topics.setCurrentRow(0)

    def _show(self, row: int):
        if 0 <= row < len(TOPICS):
            md = TOPICS[row][2].strip()
            if row == 0:
                md += f"\n\n---\n\n*{APP_NAME} {__version__}*"
            self.text.setMarkdown(md)
            # room between paragraphs (Markdown in Qt packs them tightly)
            doc = self.text.document()
            cur = QTextCursor(doc)
            cur.beginEditBlock()
            b = doc.begin()
            while b.isValid():
                f = QTextBlockFormat(b.blockFormat())
                in_list = b.textList() is not None
                f.setBottomMargin(3 if in_list else 10)
                f.setLineHeight(118, 1)                 # 1 = proportional line height
                c = QTextCursor(b)
                c.setBlockFormat(f)
                b = b.next()
            cur.endEditBlock()
            q = self.search.text().strip()
            if q:
                self.text.find(q)

    def _filter(self, q: str):
        q = q.strip().lower()
        first = None
        for i, (_, title, md) in enumerate(TOPICS):
            hit = not q or q in title.lower() or q in md.lower()
            self.topics.item(i).setHidden(not hit)
            if hit and first is None:
                first = i
        if first is not None and (self.topics.currentRow() < 0 or self.topics.currentItem().isHidden()):
            self.topics.setCurrentRow(first)
        else:
            self._show(self.topics.currentRow())

    def show_topic(self, title: str):
        for i, (_, t, _) in enumerate(TOPICS):
            if t.lower() == title.lower():
                self.topics.setCurrentRow(i)
                return
