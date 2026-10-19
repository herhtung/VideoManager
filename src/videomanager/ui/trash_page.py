"""Browse, play, restore, and permanently clear the library's own trash."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QListWidget, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from videomanager.ui.notifications import notify_information
from videomanager.services.trash_service import empty_trash, list_trashed, restore_video
from videomanager.ui.video_picker import configure_video_grid, placeholder_icon, video_item


class TrashPage(QWidget):
    back_requested = Signal()
    play_requested = Signal(str, str)
    library_changed = Signal()

    def __init__(self, connection: sqlite3.Connection, root: Path | None,
                 library_id: str | None, cache_root: Path, parent=None) -> None:
        super().__init__(parent)
        self.connection = connection
        self.root = root
        self.library_id = library_id
        self.cache_root = cache_root
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        title = QLabel("程式回收桶")
        title.setObjectName("sectionLabel")
        heading.addWidget(title, 1)
        back = QPushButton("返回影片庫")
        back.clicked.connect(self.back_requested.emit)
        heading.addWidget(back)
        layout.addLayout(heading)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        self.items = QListWidget()
        configure_video_grid(self.items)
        self.items.setSelectionMode(QAbstractItemView.SingleSelection)
        self.items.itemDoubleClicked.connect(lambda *_: self.play_selected())
        self.items.itemSelectionChanged.connect(self._update_actions)
        layout.addWidget(self.items, 1)
        actions = QHBoxLayout()
        self.play_button = QPushButton("播放選取影片")
        self.play_button.clicked.connect(self.play_selected)
        self.restore_button = QPushButton("還原到原位置")
        self.restore_button.clicked.connect(self.restore_selected)
        self.empty_button = QPushButton("清空回收桶…")
        self.empty_button.setObjectName("destructiveAction")
        self.empty_button.clicked.connect(self.clear_all)
        actions.addWidget(self.play_button)
        actions.addWidget(self.restore_button)
        actions.addStretch()
        actions.addWidget(self.empty_button)
        layout.addLayout(actions)
        self.refresh()

    def set_library(self, root: Path | None, library_id: str | None) -> None:
        self.root, self.library_id = root, library_id
        self.refresh()

    def refresh(self) -> None:
        selected = self._selected_id()
        self.items.clear()
        rows = list_trashed(self.connection, self.library_id)
        fallback = placeholder_icon()
        for row in rows:
            item = video_item({"id": row["id"], "display_title": row["display_title"],
                               "relative_path": row["original_rel_path"]},
                              self.cache_root, fallback)
            item.setData(Qt.UserRole, row["id"])
            item.setToolTip(f"{row['display_title']}\n原位置：{row['original_rel_path']}")
            self.items.addItem(item)
            if row["id"] == selected:
                self.items.setCurrentItem(item)
        size = sum(row["file_size"] for row in rows)
        self.summary.setText(f"{len(rows)} 部影片 · {size / 1024**3:.2f} GB · 影片保存在影片庫磁碟")
        self._update_actions()

    def _selected_id(self) -> str | None:
        item = self.items.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _selected_row(self):
        identifier = self._selected_id()
        return next((row for row in list_trashed(self.connection, self.library_id)
                     if row["id"] == identifier), None)

    def _update_actions(self) -> None:
        available = self.root is not None and self._selected_id() is not None
        self.play_button.setEnabled(available)
        self.restore_button.setEnabled(available)
        self.empty_button.setEnabled(self.root is not None and self.items.count() > 0)

    def play_selected(self) -> None:
        row = self._selected_row()
        if row is None or self.root is None:
            return
        path = self.root / row["trash_rel_path"]
        if not path.is_file():
            QMessageBox.warning(self, "檔案不在回收桶", "找不到這部影片的 MP4，仍可清空索引資料。")
            return
        self.play_requested.emit(row["id"], str(path))

    def restore_selected(self) -> None:
        row = self._selected_row()
        if row is None or self.root is None or self.library_id is None:
            return
        try:
            restore_video(self.connection, self.library_id, self.root, row["id"])
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
            QMessageBox.warning(self, "還原失敗", str(exc))
            return
        self.refresh()
        self.library_changed.emit()

    def clear_all(self) -> None:
        count = self.items.count()
        if not count or self.root is None or self.library_id is None:
            return
        message = (f"永久刪除回收桶中的 {count} 部 MP4，並清除它們的標籤、評分、紀錄與縮圖？"
                   "\n\n此動作無法從程式還原。")
        if QMessageBox.question(self, "確認清空程式回收桶", message,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            removed = empty_trash(self.connection, self.library_id, self.root, self.cache_root)
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
            QMessageBox.warning(self, "清空未完成", str(exc))
            self.refresh()
            return
        self.refresh()
        self.library_changed.emit()
        notify_information(self, "清空完成", f"已永久清除 {removed} 部影片及資料。")
