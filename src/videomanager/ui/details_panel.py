"""Shared metadata editor for browser and player."""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSizePolicy,
    QSplitter, QVBoxLayout, QWidget,
)

from videomanager.db import repository as repo
from videomanager.ui.tag_picker import TagCreateDialog, TagPicker
from videomanager.ui.entity_picker import EntityPicker


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
            button.setFixedSize(26, 26)
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
                "; font-size: 20px; background: transparent; border: 0; padding: 0; }"
                "QPushButton:hover { color: #ffd47b; }"
            )


class DetailsPanel(QWidget):
    saved = Signal()
    completed = Signal()
    skipped = Signal()
    entity_requested = Signal(str, str)

    def __init__(self, connection: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self.connection = connection
        self.root: Path | None = None
        self.library_id: str | None = None
        self.video_id: str | None = None
        self.group_id: str | None = None
        self.batch_ids: list[str] = []
        self._saved_state: tuple | None = None
        self.completed_identity = (None, None)
        self.title = QLineEdit()
        self.title_label = QLabel("顯示名稱")
        self.rating = StarRating()
        self.favorite = QCheckBox("收藏")
        self.tags = TagPicker(connection)
        self.entity_pickers: dict[str, EntityPicker] = {}
        self._relation_signature = None
        self.batch_operation = QComboBox()
        self.batch_operation.addItems(["批次新增關聯", "批次移除關聯"])
        self.batch_operation.hide()
        self.status = QLabel("尚未選擇影片")
        self.file_info = QLabel()
        self.file_info.setWordWrap(True)
        self.entity_summary = QLabel("人物／片商：無")
        self.entity_summary.setOpenExternalLinks(False)
        self.entity_summary.linkActivated.connect(self._entity_link_activated)

        layout = QVBoxLayout(self)
        self.columns_splitter = QSplitter(Qt.Horizontal)
        columns = self.columns_splitter
        columns.setChildrenCollapsible(False)
        columns.setHandleWidth(6)
        columns.setStyleSheet("QSplitter::handle { background: #2b424c; } "
                             "QSplitter::handle:hover { background: #79d9ca; }")
        left = QWidget()
        left.setMinimumWidth(150)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        right = QWidget()
        right.setMinimumWidth(125)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(7)
        left_layout.addWidget(self.title_label)
        left_layout.addWidget(self.title)
        left_layout.addWidget(QLabel("評分"))
        left_layout.addWidget(self.rating)
        left_layout.addWidget(self.favorite)
        tag_header = QHBoxLayout()
        tag_header.addWidget(QLabel("標籤"))
        tag_header.addStretch()
        add_tag = QPushButton("＋")
        add_tag.setToolTip("新增標籤並勾選；按儲存套用到影片")
        add_tag.setFixedWidth(38)
        add_tag.clicked.connect(self.quick_add_tag)
        tag_header.addWidget(add_tag)
        right_layout.addLayout(tag_header)
        right_layout.addWidget(self.tags, 1)
        self.relations_box = QWidget()
        self.relations_layout = QVBoxLayout(self.relations_box)
        self.relations_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.relations_box, 1)
        self._reload_relation_pickers()
        columns.addWidget(left)
        columns.addWidget(right)
        columns.setSizes([260, 260])
        layout.addWidget(columns, 1)
        layout.addWidget(self.batch_operation)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status)
        status_row.addStretch()
        self.return_button = QPushButton("退回待整理")
        self.return_button.setToolTip("保留目前的名稱、標籤與評分，只把狀態改回待整理。")
        self.return_button.clicked.connect(self.return_to_unprocessed)
        self.return_button.hide()
        status_row.addWidget(self.return_button)
        layout.addLayout(status_row)
        layout.addWidget(self.file_info)
        layout.addWidget(self.entity_summary)
        buttons = QHBoxLayout()
        self.save_button = QPushButton("儲存")
        self.save_button.clicked.connect(lambda: self.save_current())
        self.done_button = QPushButton("完成整理")
        self.done_button.clicked.connect(lambda: self.finish_current())
        self.skip_button = QPushButton("跳過")
        self.skip_button.clicked.connect(lambda: self.skipped.emit())
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.done_button)
        buttons.addWidget(self.skip_button)
        layout.addLayout(buttons)

    def set_library(self, root: Path | None, library_id: str | None) -> None:
        self.root = root
        self.library_id = library_id
        if self.group_id:
            self.set_group(self.group_id)
        elif self.video_id:
            self.set_video(self.video_id)

    def _reload_relation_pickers(self) -> None:
        categories = repo.list_relation_types(self.connection)
        signature = tuple((row["id"], row["name"]) for row in categories)
        if signature == self._relation_signature:
            return
        selected = {key: picker.selected_ids() for key, picker in self.entity_pickers.items()}
        while self.relations_layout.count():
            widget = self.relations_layout.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        self.entity_pickers = {}
        for row in categories:
            section = QWidget()
            section.setSizePolicy(section.sizePolicy().horizontalPolicy(), QSizePolicy.Ignored)
            layout = QVBoxLayout(section)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(4)
            heading = QLabel(row["name"])
            heading.setToolTip(row["name"])
            layout.addWidget(heading)
            picker = EntityPicker(self.connection, row["id"])
            picker.scroll.setMinimumHeight(0)
            picker.scroll.setMaximumHeight(16777215)
            picker.set_selected(selected.get(row["id"], set()))
            picker.selectionChanged.connect(self._refresh_entity_summary)
            picker.orderChanged.connect(self._reload_entity_order)
            layout.addWidget(picker, 1)
            self.relations_layout.addWidget(section, 1)
            self.entity_pickers[row["id"]] = picker
        # Retain the original accessors for existing integrations.
        self.people = self.entity_pickers.get("person")
        self.studios = self.entity_pickers.get("studio")
        self._relation_signature = signature

    def _reload_entity_order(self) -> None:
        selected = self._selected_entities()
        for kind, picker in self.entity_pickers.items():
            picker.reload()
            picker.set_selected(selected & {row["id"] for row in repo.list_entities(self.connection, kind)})

    def _selected_entities(self) -> set[str]:
        return set().union(*(picker.selected_ids() for picker in self.entity_pickers.values()))

    def set_video(self, video_id: str | None) -> None:
        self._reload_relation_pickers()
        self.batch_operation.hide()
        self.batch_ids = []
        self.group_id = None
        self.video_id = video_id
        self.title_label.setText("顯示名稱")
        for widget in (self.title, self.rating, self.favorite,
                       self.done_button, self.skip_button):
            widget.setEnabled(True)
        self.save_button.setText("儲存")
        enabled = video_id is not None
        self.setEnabled(enabled)
        self.tags.connection = self.connection
        self.tags.set_selected(set())
        for picker in self.entity_pickers.values():
            picker.connection = self.connection
            picker.set_selected(set())
        if video_id is None:
            self.title.clear()
            self.rating.setValue(0)
            self.favorite.setChecked(False)
            self.status.setText("尚未選擇影片")
            self.return_button.hide()
            self.file_info.clear()
            self._refresh_entity_summary()
            self._capture_state()
            return
        row = repo.video_by_id(self.connection, video_id)
        if row is None:
            self.set_video(None)
            return
        self.title.setText(row["display_title"])
        self.rating.setValue(row["rating"] or 0)
        self.favorite.setChecked(bool(row["is_favorite"]))
        self.status.setText("已整理" if row["status"] == "done" else "待整理")
        self.return_button.setVisible(row["status"] == "done")
        self.file_info.setText(f"{row['relative_path']}\n{row['width'] or '?'} × {row['height'] or '?'} · {row['video_codec'] or '?'}")
        self.tags.set_selected(repo.video_tag_ids(self.connection, video_id))
        self.tags.reload()
        for kind, picker in self.entity_pickers.items():
            picker.set_selected(repo.video_entity_ids(self.connection, video_id, kind))
            picker.reload()
        self._refresh_entity_summary()
        self._capture_state()

    def set_batch(self, video_ids: list[str]) -> None:
        """Add or remove selected metadata without replacing other video data."""
        self.set_video(None)
        self.batch_ids = list(dict.fromkeys(video_ids))
        self.setEnabled(True)
        self.title_label.setText("批次標籤")
        self.title.setText(f"已選 {len(self.batch_ids)} 部影片")
        for widget in (self.title, self.rating, self.favorite,
                       self.done_button, self.skip_button):
            widget.setEnabled(False)
        self.tags.set_selected(set())
        for picker in self.entity_pickers.values():
            picker.set_selected(set())
        self.batch_operation.show()
        self.tags.reload()
        self.status.setText("批次新增／移除關聯")
        self.file_info.setText("選取要操作的標籤、人物或片商；預設新增，切換可移除。未選欄位不變。")
        self.save_button.setText("套用關聯")
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
        self.file_info.setText(f"共 {len(members)} 段 MP4；標籤、評分、收藏與整理狀態由整組共用。")
        shared_tags = set().union(*(repo.video_tag_ids(self.connection, part) for part in members))
        self.tags.set_selected(shared_tags)
        self.tags.reload()
        for kind, picker in self.entity_pickers.items():
            picker.set_selected(set.intersection(*(repo.video_entity_ids(self.connection, part, kind) for part in members)))
            picker.reload()
        self._refresh_entity_summary()
        self._capture_state()

    def _refresh_entity_summary(self) -> None:
        links = []
        for entity_type, picker in self.entity_pickers.items():
            selected = picker.selected_ids()
            if not selected:
                continue
            names = {row["id"]: row["name"] for row in repo.list_entities(self.connection, entity_type)}
            links.extend(f'<a href="{entity_type}:{identifier}">{escape(names[identifier])}</a>'
                         for identifier in sorted(selected) if identifier in names)
        self.entity_summary.setText("關聯：" + ("、".join(links) if links else "無") + "（點名稱查看相關影片）")

    def _entity_link_activated(self, url: str) -> None:
        entity_type, separator, identifier = url.partition(":")
        if separator and entity_type in self.entity_pickers:
            self.entity_requested.emit(entity_type, identifier)

    def _form_state(self) -> tuple:
        return (self.video_id, self.group_id, tuple(self.batch_ids), self.title.text(), self.rating.value(),
                self.favorite.isChecked(),
                frozenset(self.tags.selected_ids()), frozenset(self._selected_entities()), self.batch_operation.currentIndex())

    def _capture_state(self) -> None:
        self._saved_state = self._form_state()

    def is_dirty(self) -> bool:
        return (self.video_id is not None or bool(self.batch_ids)) and self._form_state() != self._saved_state

    def quick_add_tag(self) -> None:
        if self.video_id is None and not self.batch_ids:
            return
        dialog = TagCreateDialog(self.connection, self)
        if dialog.exec() and dialog.created_tag_id:
            self.tags.search.clear()
            self.tags.set_selected(self.tags.selected_ids() | {dialog.created_tag_id})
            self.tags.reload()

    def save_current(self, *, complete: bool = False, return_to_queue: bool = False) -> bool:
        if self.batch_ids:
            if complete or return_to_queue:
                return False
            ids = self.batch_ids
            placeholders = ",".join("?" for _ in ids)
            eligible = self.connection.execute(
                f"""SELECT COUNT(*) FROM videos v WHERE v.id IN ({placeholders})
                    AND v.deleted_at IS NULL AND v.missing_since IS NULL""", ids,
            ).fetchone()[0]
            if eligible != len(ids):
                QMessageBox.warning(self, "無法批次套用", "選取影片已不可用，請重新選取。")
                return False
            with self.connection:
                for table, id_column, selected in (
                    ("video_tags", "tag_id", self.tags.selected_ids()),
                    ("video_entities", "entity_id", self._selected_entities()),
                ):
                    verb = "INSERT OR IGNORE INTO" if self.batch_operation.currentIndex() == 0 else "DELETE FROM"
                    if self.batch_operation.currentIndex() == 0:
                        self.connection.executemany(
                            f"{verb} {table}(video_id,{id_column}) VALUES (?,?)",
                            ((identifier, value) for identifier in ids for value in selected),
                        )
                    else:
                        self.connection.executemany(
                            f"{verb} {table} WHERE video_id=? AND {id_column}=?",
                            ((identifier, value) for identifier in ids for value in selected),
                        )
            self._capture_state()
            self.saved.emit()
            return True
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
                  "done" if row["status"] == "done" else "unprocessed")
        try:
            repo.save_video_details(
                self.connection, self.video_id, title=title,
                notes=row["notes"] or "",
                rating=self.rating.value(),
                is_favorite=self.favorite.isChecked(),
                recorded_at=row["recorded_at"],
                status=status,
                tag_ids=self.tags.selected_ids(),
                group_cover=self.group_id is not None,
                entity_ids=self._selected_entities(),
                preserve_hidden_fields=True,
            )
        except (sqlite3.Error, ValueError, OSError) as exc:
            QMessageBox.critical(self, "儲存失敗", str(exc))
            return False
        if self.group_id:
            count = len(repo.group_member_ids(self.connection, self.group_id))
            self.file_info.setText(f"共 {count} 段 MP4；標籤、評分、收藏與整理狀態由整組共用。")
        else:
            self.file_info.setText(f"{repo.video_by_id(self.connection, self.video_id)['relative_path']}\n"
                                   f"{row['width'] or '?'} × {row['height'] or '?'} · {row['video_codec'] or '?'}")
        status_text = "已整理" if status == "done" else "待整理"
        self.status.setText(f"影片組 · {count} 段 · {status_text}" if self.group_id else status_text)
        self.return_button.setVisible(status == "done")
        self._capture_state()
        self.saved.emit()
        return True

    def return_to_unprocessed(self) -> None:
        self.save_current(return_to_queue=True)

    def finish_current(self) -> None:
        identity = (self.video_id, self.group_id)
        if self.save_current(complete=True):
            self.completed_identity = identity
            self.completed.emit()
