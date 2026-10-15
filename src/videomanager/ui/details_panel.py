"""Shared metadata editor for browser and player."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTextEdit,
    QVBoxLayout, QWidget,
)

from videomanager.db import repository as repo
from videomanager.ui.tag_picker import TagCreateDialog, TagPicker


class StarRating(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._value = 0
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.buttons = []
        for number in range(1, 6):
            button = QPushButton("☆")
            button.setFixedSize(34, 34)
            button.setToolTip(f"{number} 顆星；再按一次可清除評分")
            button.setAccessibleName(f"評 {number} 顆星")
            button.clicked.connect(lambda _=False, n=number: self.setValue(0 if self._value == n else n))
            layout.addWidget(button)
            self.buttons.append(button)
        layout.addStretch()
        self.setValue(0)

    def value(self) -> int:
        return self._value

    def setValue(self, value: int) -> None:
        self._value = max(0, min(5, int(value)))
        for index, button in enumerate(self.buttons, 1):
            selected = index <= self._value
            button.setText("★" if selected else "☆")
            button.setStyleSheet(
                "QPushButton { color: " + ("#ffc35a" if selected else "#8799a5") +
                "; font-size: 24px; background: transparent; border: 0; padding: 0; }"
                "QPushButton:hover { color: #ffd47b; }"
            )


class DetailsPanel(QWidget):
    saved = Signal()
    completed = Signal()
    skipped = Signal()

    def __init__(self, connection: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self.connection = connection
        self.root: Path | None = None
        self.library_id: str | None = None
        self.video_id: str | None = None
        self.group_id: str | None = None
        self._saved_state: tuple | None = None
        self.title = QLineEdit()
        self.title_label = QLabel("顯示名稱")
        self.rating = StarRating()
        self.favorite = QCheckBox("收藏")
        self.recorded = QLineEdit()
        self.recorded.setPlaceholderText("YYYY-MM-DD（可留空）")
        self.notes = QTextEdit()
        self.notes.setMaximumHeight(100)
        self.tags = TagPicker(connection)
        self.status = QLabel("尚未選擇影片")
        self.file_info = QLabel()
        self.file_info.setWordWrap(True)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow(self.title_label, self.title)
        form.addRow("評分", self.rating)
        form.addRow("", self.favorite)
        form.addRow("拍攝日期", self.recorded)
        form.addRow("備註", self.notes)
        layout.addLayout(form)
        tag_header = QHBoxLayout()
        tag_header.addWidget(QLabel("標籤"))
        tag_header.addStretch()
        add_tag = QPushButton("＋")
        add_tag.setToolTip("新增標籤並勾選；按儲存套用到影片")
        add_tag.setFixedWidth(38)
        add_tag.clicked.connect(self.quick_add_tag)
        tag_header.addWidget(add_tag)
        layout.addLayout(tag_header)
        layout.addWidget(self.tags, 1)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status)
        status_row.addStretch()
        self.return_button = QPushButton("退回待整理")
        self.return_button.setToolTip("保留目前的名稱、標籤、評分與備註，只把狀態改回待整理。")
        self.return_button.clicked.connect(self.return_to_unprocessed)
        self.return_button.hide()
        status_row.addWidget(self.return_button)
        layout.addLayout(status_row)
        layout.addWidget(self.file_info)
        buttons = QHBoxLayout()
        save = QPushButton("儲存")
        save.clicked.connect(lambda: self.save_current())
        done = QPushButton("完成整理")
        done.clicked.connect(lambda: self.finish_current())
        skip = QPushButton("跳過")
        skip.clicked.connect(lambda: self.skipped.emit())
        buttons.addWidget(save)
        buttons.addWidget(done)
        buttons.addWidget(skip)
        layout.addLayout(buttons)

    def set_library(self, root: Path | None, library_id: str | None) -> None:
        self.root = root
        self.library_id = library_id
        if self.group_id:
            self.set_group(self.group_id)
        elif self.video_id:
            self.set_video(self.video_id)

    def set_video(self, video_id: str | None) -> None:
        self.group_id = None
        self.video_id = video_id
        self.title_label.setText("顯示名稱")
        enabled = video_id is not None
        self.setEnabled(enabled)
        self.tags.connection = self.connection
        self.tags.set_selected(set())
        if video_id is None:
            self.title.clear()
            self.rating.setValue(0)
            self.favorite.setChecked(False)
            self.recorded.clear()
            self.notes.clear()
            self.status.setText("尚未選擇影片")
            self.return_button.hide()
            self.file_info.clear()
            self._capture_state()
            return
        row = repo.video_by_id(self.connection, video_id)
        if row is None:
            self.set_video(None)
            return
        self.title.setText(row["display_title"])
        self.rating.setValue(row["rating"] or 0)
        self.favorite.setChecked(bool(row["is_favorite"]))
        self.recorded.setText(row["recorded_at"] or "")
        self.notes.setPlainText(row["notes"] or "")
        self.status.setText({"unprocessed": "待整理", "in_progress": "整理中", "done": "已整理"}[row["status"]])
        self.return_button.setVisible(row["status"] != "unprocessed")
        self.file_info.setText(f"{row['relative_path']}\n{row['width'] or '?'} × {row['height'] or '?'} · {row['video_codec'] or '?'}")
        self.tags.set_selected(repo.video_tag_ids(self.connection, video_id))
        self.tags.reload()
        self._capture_state()

    def set_group(self, group_id: str) -> None:
        group = self.connection.execute(
            "SELECT name FROM video_groups WHERE id = ?", (group_id,),
        ).fetchone()
        members = repo.group_member_ids(self.connection, group_id)
        if group is None or not members:
            self.set_video(None)
            return
        self.set_video(members[0])
        self.group_id = group_id
        self.title_label.setText("影片組名稱")
        self.title.setText(group["name"])
        self.status.setText(f"影片組 · {len(members)} 段 · {self.status.text()}")
        self.file_info.setText(f"共 {len(members)} 段 MP4；標籤、評分、收藏、日期、備註與整理狀態由整組共用。")
        shared_tags = set().union(*(repo.video_tag_ids(self.connection, part) for part in members))
        self.tags.set_selected(shared_tags)
        self.tags.reload()
        self._capture_state()

    def _form_state(self) -> tuple:
        return (self.video_id, self.group_id, self.title.text(), self.rating.value(),
                self.favorite.isChecked(), self.recorded.text(), self.notes.toPlainText(),
                frozenset(self.tags.selected_ids()))

    def _capture_state(self) -> None:
        self._saved_state = self._form_state()

    def is_dirty(self) -> bool:
        return self.video_id is not None and self._form_state() != self._saved_state

    def quick_add_tag(self) -> None:
        if self.video_id is None:
            return
        dialog = TagCreateDialog(self.connection, self)
        if dialog.exec() and dialog.created_tag_id:
            self.tags.search.clear()
            self.tags.set_selected(self.tags.selected_ids() | {dialog.created_tag_id})
            self.tags.reload()

    def save_current(self, *, complete: bool = False, return_to_queue: bool = False) -> bool:
        if self.video_id is None:
            return False
        title = self.title.text().strip()
        if not title:
            QMessageBox.warning(self, "缺少名稱", "顯示名稱不可空白。")
            return False
        row = repo.video_by_id(self.connection, self.video_id)
        if row is None:
            return False
        if complete:
            selected = self.tags.selected_ids()
            selected_groups = {tag["group_id"] for tag in repo.list_tags(self.connection)
                               if tag["id"] in selected}
            missing = [group["name"] for group in repo.list_tag_groups(self.connection)
                       if group["is_required"] and group["id"] not in selected_groups]
            if missing:
                self.tags.search.clear()
                QMessageBox.warning(
                    self, "尚未完成必選標籤",
                    "完成整理前，以下標籤分類各需至少選一個標籤：\n" +
                    "\n".join(f"• {name}" for name in missing),
                )
                return False
        status = ("unprocessed" if return_to_queue else "done" if complete else
                  "in_progress" if row["status"] == "unprocessed" else row["status"])
        try:
            repo.save_video_details(
                self.connection, self.video_id, title=title,
                notes=self.notes.toPlainText(),
                rating=self.rating.value(),
                is_favorite=self.favorite.isChecked(),
                recorded_at=self.recorded.text().strip() or None,
                status=status,
                tag_ids=self.tags.selected_ids(),
                group_cover=self.group_id is not None,
            )
        except (sqlite3.Error, ValueError, OSError) as exc:
            QMessageBox.critical(self, "儲存失敗", str(exc))
            return False
        if self.group_id:
            count = len(repo.group_member_ids(self.connection, self.group_id))
            self.file_info.setText(f"共 {count} 段 MP4；標籤、評分、收藏、日期、備註與整理狀態由整組共用。")
        else:
            self.file_info.setText(f"{repo.video_by_id(self.connection, self.video_id)['relative_path']}\n"
                                   f"{row['width'] or '?'} × {row['height'] or '?'} · {row['video_codec'] or '?'}")
        status_text = {"in_progress": "整理中", "done": "已整理"}.get(status, "待整理")
        self.status.setText(f"影片組 · {count} 段 · {status_text}" if self.group_id else status_text)
        self.return_button.setVisible(status != "unprocessed")
        self._capture_state()
        self.saved.emit()
        return True

    def return_to_unprocessed(self) -> None:
        self.save_current(return_to_queue=True)

    def finish_current(self) -> None:
        if self.save_current(complete=True):
            self.completed.emit()
