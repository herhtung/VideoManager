"""Roomy, searchable tag and tag-category management."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QVBoxLayout, QWidget, QGridLayout,
)

from videomanager.ui.notifications import notify_information
from videomanager.db import repository as repo
from videomanager.ui.tag_picker import CategoryChooser, TagPicker


class TagTargetDialog(QDialog):
    def __init__(self, connection: sqlite3.Connection, source_id: str, parent=None) -> None:
        super().__init__(parent)
        self.source_id = source_id
        self.target_id: str | None = None
        self.setWindowTitle("選擇合併目標標籤")
        self.resize(560, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("搜尋並點選要保留的標籤，再按「合併」。"))
        self.picker = TagPicker(connection, multi_select=False)
        layout.addWidget(self.picker, 1)
        buttons = QHBoxLayout()
        buttons.addStretch()
        merge = QPushButton("合併到所選標籤")
        merge.clicked.connect(self._accept_target)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(merge)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def _accept_target(self) -> None:
        selected = self.picker.selected_ids()
        if not selected or self.source_id in selected:
            QMessageBox.warning(self, "請選擇目標", "請選另一個要保留的標籤。")
            return
        self.target_id = next(iter(selected))
        self.accept()


class MetadataDialog(QDialog):
    def __init__(self, connection: sqlite3.Connection, parent=None, *, root: Path | None = None,
                 library_id: str | None = None) -> None:
        super().__init__(parent)
        self.connection = connection
        self._selected_tag_id: str | None = None
        self.setWindowTitle("管理標籤與標籤分類")
        self.resize(1260, 820)
        self.setMinimumSize(980, 660)
        outer = QVBoxLayout(self)
        outer.addWidget(QLabel("左側可拖曳分類標題排序、拖曳標籤排序；把標籤拖到另一個分類標題可移動。右側分類清單也可拖曳。"))
        main = QHBoxLayout()
        self.tags = TagPicker(connection, multi_select=False, drag_tags=True, show_empty_groups=True)
        self.tags.tag_clicked.connect(self._tag_selected)
        self.tags.category_reordered.connect(
            lambda source, target, after: QTimer.singleShot(
                0, lambda: self._category_dropped(source, target, after),
            )
        )
        self.tags.tag_reordered.connect(
            lambda source, target, after: QTimer.singleShot(
                0, lambda: self._tag_reordered(source, target, after),
            )
        )
        self.tags.tag_group_dropped.connect(
            lambda tag, target: QTimer.singleShot(0, lambda: self._tag_dropped(tag, target))
        )
        main.addWidget(self.tags, 3)

        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setSpacing(8)
        side_layout.addWidget(self._heading("選擇標籤分類"))
        self.categories = CategoryChooser(connection, reorderable=True, accept_tags=True)
        self.categories.scroll.setMinimumHeight(220)
        self.categories.scroll.setMaximumHeight(300)
        self.categories.category_changed.connect(self._category_selected)
        self.categories.category_reordered.connect(
            lambda source, target, after: QTimer.singleShot(
                0, lambda: self._category_dropped(source, target, after),
            )
        )
        self.categories.tag_dropped.connect(
            lambda tag, target: QTimer.singleShot(0, lambda: self._tag_dropped(tag, target))
        )
        side_layout.addWidget(self.categories)

        side_layout.addWidget(self._heading("新增標籤"))
        self.new_tag_name = QLineEdit()
        self.new_tag_name.setPlaceholderText("輸入新標籤名稱")
        self.new_tag_name.returnPressed.connect(self.add_tag)
        side_layout.addWidget(self.new_tag_name)
        self._button(side_layout, "新增到所選分類", self.add_tag)

        side_layout.addWidget(self._heading("新增標籤分類"))
        self.new_category_name = QLineEdit()
        self.new_category_name.setPlaceholderText("輸入新分類名稱")
        self.new_category_name.returnPressed.connect(self.add_category)
        side_layout.addWidget(self.new_category_name)
        self._button(side_layout, "新增標籤分類", self.add_category)

        side_layout.addWidget(self._heading("編輯所選標籤"))
        self.selected_label = QLabel("尚未選擇標籤")
        side_layout.addWidget(self.selected_label)
        self.tag_name = QLineEdit()
        self.tag_name.setPlaceholderText("標籤名稱")
        side_layout.addWidget(self.tag_name)
        tag_actions = QGridLayout()
        for index, (label, action) in enumerate((
            ("儲存名稱", self.rename_tag), ("顏色", self.color_tag),
            ("上移", lambda: self.move_tag(-1)), ("下移", lambda: self.move_tag(1)),
            ("合併", self.merge_tag), ("刪除", self.delete_tag),
            ("移到所選分類", self.move_tag_to_selected_category),
        )):
            button = QPushButton(label)
            button.clicked.connect(action)
            tag_actions.addWidget(button, index // 2, index % 2)
        side_layout.addLayout(tag_actions)

        side_layout.addWidget(self._heading("編輯所選標籤分類"))
        self.category_name = QLineEdit()
        self.category_name.setPlaceholderText("分類名稱")
        side_layout.addWidget(self.category_name)
        self.category_required = QCheckBox("完成整理時必選")
        self.category_required.setToolTip("開啟後，每部影片完成整理前都須從此分類選至少一個標籤。")
        self.category_required.toggled.connect(self._required_changed)
        side_layout.addWidget(self.category_required)
        category_actions = QGridLayout()
        for index, (label, action) in enumerate((
            ("儲存名稱", self.rename_category), ("刪除空分類", self.delete_category),
            ("上移", lambda: self.move_category(-1)), ("下移", lambda: self.move_category(1)),
        )):
            button = QPushButton(label)
            button.clicked.connect(action)
            category_actions.addWidget(button, index // 2, index % 2)
        side_layout.addLayout(category_actions)
        side_layout.addStretch()
        side_scroll.setWidget(side)
        main.addWidget(side_scroll, 2)
        outer.addLayout(main, 1)
        close = QPushButton("完成")
        close.clicked.connect(self.accept)
        outer.addWidget(close)
        self._category_selected(self.categories.selected_id())

    @staticmethod
    def _heading(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("color: #d8eeee; font-weight: 700; margin-top: 8px;")
        return label

    @staticmethod
    def _button(layout: QVBoxLayout, text: str, action) -> None:
        button = QPushButton(text)
        button.clicked.connect(action)
        layout.addWidget(button)

    def _category_selected(self, category_id: str | None) -> None:
        row = self.connection.execute("SELECT name, is_required FROM tag_groups WHERE id = ?", (category_id,)).fetchone()
        self.category_name.setText(row["name"] if row else "")
        self.category_required.blockSignals(True)
        self.category_required.setEnabled(row is not None)
        self.category_required.setChecked(bool(row["is_required"]) if row else False)
        self.category_required.blockSignals(False)

    def _required_changed(self, checked: bool) -> None:
        category_id = self.categories.selected_id()
        if category_id is None:
            return
        try:
            repo.set_tag_group_required(self.connection, category_id, checked)
            self.refresh(preferred_category=category_id)
        except (sqlite3.Error, ValueError) as exc:
            self._category_selected(category_id)
            QMessageBox.critical(self, "設定必選失敗", str(exc))

    def _tag_selected(self, tag_id: str, *, select_category: bool = True) -> None:
        self._selected_tag_id = tag_id
        row = self.connection.execute("SELECT name, group_id FROM tags WHERE id = ?", (tag_id,)).fetchone()
        if row:
            self.selected_label.setText(f"目前選取：{row['name']}")
            self.tag_name.setText(row["name"])
            if select_category:
                self.categories.select(row["group_id"])

    def refresh(self, *, preferred_category: str | None = None, preferred_tag: str | None = None) -> None:
        self.categories.reload(preferred_category)
        self.tags.reload()
        tag_id = preferred_tag or self._selected_tag_id
        if tag_id and self.connection.execute("SELECT 1 FROM tags WHERE id = ?", (tag_id,)).fetchone():
            self.tags.set_selected({tag_id})
            self._tag_selected(tag_id, select_category=preferred_category is None)
        else:
            self._selected_tag_id = None
            self.tags.set_selected(set())
            self.selected_label.setText("尚未選擇標籤")
            self.tag_name.clear()

    def _run(self, action, *, preferred_category: str | None = None,
             preferred_tag: str | None = None) -> None:
        try:
            action()
            self.refresh(preferred_category=preferred_category, preferred_tag=preferred_tag)
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.critical(self, "操作失敗", str(exc))

    def add_category(self) -> None:
        name = self.new_category_name.text().strip()
        if not name:
            return
        try:
            group_id = repo.create_tag_group(self.connection, name)
            self.new_category_name.clear()
            self.refresh(preferred_category=group_id)
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.critical(self, "新增標籤分類失敗", str(exc))

    def add_tag(self) -> None:
        name = self.new_tag_name.text().strip()
        if not name:
            return
        group_id = self.categories.selected_id()
        if group_id is None:
            group_id = repo.ensure_default_tag_group(self.connection)
        try:
            tag_id = repo.create_tag(self.connection, group_id, name)
            self.new_tag_name.clear()
            self.refresh(preferred_category=group_id, preferred_tag=tag_id)
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.critical(self, "新增標籤失敗", str(exc))

    def rename_tag(self) -> None:
        tag_id = self._selected_tag_id
        name = self.tag_name.text().strip()
        if tag_id and name:
            color = self.connection.execute("SELECT color FROM tags WHERE id = ?", (tag_id,)).fetchone()[0]
            self._run(lambda: repo.rename_tag(self.connection, tag_id, name, color), preferred_tag=tag_id)

    def color_tag(self) -> None:
        tag_id = self._selected_tag_id
        if not tag_id:
            return
        row = self.connection.execute("SELECT name, color FROM tags WHERE id = ?", (tag_id,)).fetchone()
        color = QColorDialog.getColor(QColor(row["color"] or "#4d8eff"), self, "選擇標籤顏色")
        if color.isValid():
            self._run(lambda: repo.rename_tag(self.connection, tag_id, row["name"], color.name()), preferred_tag=tag_id)

    def move_tag(self, direction: int) -> None:
        tag_id = self._selected_tag_id
        if not tag_id:
            return
        row = self.connection.execute("SELECT group_id FROM tags WHERE id = ?", (tag_id,)).fetchone()
        ids = [tag["id"] for tag in repo.list_tags(self.connection) if tag["group_id"] == row["group_id"]]
        index = ids.index(tag_id)
        target = index + direction
        if 0 <= target < len(ids):
            ids[index], ids[target] = ids[target], ids[index]
            self._run(lambda: repo.move_sort_order(self.connection, "tags", ids), preferred_tag=tag_id)

    def move_tag_to_selected_category(self) -> None:
        tag_id = self._selected_tag_id
        category_id = self.categories.selected_id()
        if tag_id is None or category_id is None:
            notify_information(self, "請先選擇", "先點選既有標籤，再選右側的目標標籤分類。")
            return
        self._run(lambda: repo.move_tag_to_group(self.connection, tag_id, category_id),
                  preferred_category=category_id, preferred_tag=tag_id)

    def merge_tag(self) -> None:
        source_id = self._selected_tag_id
        if not source_id:
            return
        dialog = TagTargetDialog(self.connection, source_id, self)
        if dialog.exec() and dialog.target_id:
            target_id = dialog.target_id
            if QMessageBox.question(self, "確認合併", "來源標籤會移除，影片會保留目標標籤。繼續？",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
                self._run(lambda: repo.merge_tags(self.connection, source_id, target_id), preferred_tag=target_id)

    def delete_tag(self) -> None:
        tag_id = self._selected_tag_id
        if tag_id and QMessageBox.question(self, "確認刪除", "只刪除標籤關聯，不刪除影片。繼續？",
                                           QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self._run(lambda: repo.delete_tag(self.connection, tag_id))

    def rename_category(self) -> None:
        category_id = self.categories.selected_id()
        name = self.category_name.text().strip()
        if category_id and name:
            self._run(lambda: repo.rename_tag_group(self.connection, category_id, name), preferred_category=category_id)

    def delete_category(self) -> None:
        category_id = self.categories.selected_id()
        if category_id and QMessageBox.question(self, "確認刪除", "只可刪除空的標籤分類。繼續？",
                                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self._run(lambda: repo.delete_tag_group(self.connection, category_id))

    def move_category(self, direction: int) -> None:
        category_id = self.categories.selected_id()
        if not category_id:
            return
        ids = [row["id"] for row in repo.list_tag_groups(self.connection)]
        index = ids.index(category_id)
        target = index + direction
        if 0 <= target < len(ids):
            ids[index], ids[target] = ids[target], ids[index]
            self._run(lambda: repo.move_sort_order(self.connection, "tag_groups", ids), preferred_category=category_id)

    def _category_dropped(self, source_id: str, target_id: str, after: bool) -> None:
        ids = [row["id"] for row in repo.list_tag_groups(self.connection)]
        if source_id not in ids or target_id not in ids or source_id == target_id:
            return
        ids.remove(source_id)
        ids.insert(ids.index(target_id) + int(after), source_id)
        self._run(lambda: repo.move_sort_order(self.connection, "tag_groups", ids),
                  preferred_category=source_id)

    def _tag_dropped(self, tag_id: str, category_id: str) -> None:
        if not self.connection.execute("SELECT 1 FROM tags WHERE id = ?", (tag_id,)).fetchone():
            return
        self._run(lambda: repo.move_tag_to_group(self.connection, tag_id, category_id),
                  preferred_category=category_id, preferred_tag=tag_id)

    def _tag_reordered(self, source_id: str, target_id: str, after: bool) -> None:
        rows = self.connection.execute(
            "SELECT id, group_id FROM tags WHERE id IN (?, ?)", (source_id, target_id),
        ).fetchall()
        by_id = {row["id"]: row["group_id"] for row in rows}
        if source_id == target_id or source_id not in by_id or target_id not in by_id:
            return
        target_group = by_id[target_id]

        def move() -> None:
            if by_id[source_id] != target_group:
                repo.move_tag_to_group(self.connection, source_id, target_group)
            ids = [row["id"] for row in repo.list_tags(self.connection)
                   if row["group_id"] == target_group]
            ids.remove(source_id)
            ids.insert(ids.index(target_id) + int(after), source_id)
            repo.move_sort_order(self.connection, "tags", ids)

        self._run(move, preferred_category=target_group, preferred_tag=source_id)
