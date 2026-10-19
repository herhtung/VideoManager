"""Small nonblocking notices with a bounded local history."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QListWidget, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)


class NotificationHistory:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            loaded = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
            self.items = loaded[-100:] if isinstance(loaded, list) else []
        except (OSError, ValueError):
            self.items = []

    def add(self, message: str) -> None:
        self.items.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                           "message": message})
        self.items = self.items[-100:]
        self._save()

    def clear(self) -> None:
        self.items.clear()
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".notifications-", suffix=".json",
                                                  dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.items, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


class Toast(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.setObjectName("noticeToast")
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet("QWidget#noticeToast { background: #204047; color: #f1faf8;"
                           " border: 1px solid #6ad8cb; border-radius: 9px; }"
                           "QLabel { color: #f1faf8; padding: 8px; }")
        layout = QHBoxLayout(self)
        self.label = QLabel()
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)
        self.hide()

    def announce(self, message: str) -> None:
        self.label.setText(message)
        self.setFixedWidth(min(400, max(250, self.parentWidget().width() - 24)))
        self.adjustSize()
        window = self.parentWidget()
        point = window.mapToGlobal(window.rect().bottomRight())
        geometry = window.screen().availableGeometry()
        x = min(point.x() - self.width() - 16, geometry.right() - self.width() - 8)
        y = min(point.y() - self.height() - 44, geometry.bottom() - self.height() - 8)
        self.move(max(geometry.left() + 8, x), max(geometry.top() + 8, y))
        self.show()
        self.raise_()
        self.timer.start(3200)


class NotificationDialog(QDialog):
    def __init__(self, history: NotificationHistory, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("通知中心")
        self.resize(560, 480)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("最近 100 則通知 · 新通知在最上方"))
        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        clear = QPushButton("清空通知")
        clear.clicked.connect(lambda: (history.clear(), self.refresh(history)))
        close = QPushButton("關閉")
        close.clicked.connect(self.accept)
        row.addWidget(clear)
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)
        self.refresh(history)

    def refresh(self, history: NotificationHistory) -> None:
        self.list.clear()
        for item in reversed(history.items):
            self.list.addItem(f"{item['time']}  {item['message']}")


def notify_information(parent: QWidget, title: str, message: str) -> None:
    """Replace acknowledgement-only message boxes inside the main window."""
    owner: QWidget | None = parent
    while owner is not None:
        callback = getattr(owner, "notify", None)
        if callable(callback):
            callback(title, message)
            return
        owner = owner.parentWidget()
    QMessageBox.information(parent, title, message)
