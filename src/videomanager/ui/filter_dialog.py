"""Tag-first filters with optional metadata and date conditions."""

from __future__ import annotations

import sqlite3
from datetime import datetime, time, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout,
    QGroupBox, QLabel, QLineEdit, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from videomanager.ui.tag_picker import TagPicker
from videomanager.ui.entity_picker import EntityPicker
from videomanager.db import repository as repo


class FilterDialog(QDialog):
    def __init__(self, connection: sqlite3.Connection, current: dict | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("進階篩選")
        area = (parent.screen() if parent is not None else QApplication.primaryScreen()).availableGeometry()
        self.resize(min(1080, area.width() - 32), min(760, area.height() - 64))
        self.setMinimumSize(700, 560)
        current = current or {}
        layout = QVBoxLayout(self)
        heading = QLabel("設定篩選條件")
        heading.setStyleSheet("font-size: 16px; font-weight: 700; color: #e1f4f0;")
        layout.addWidget(heading)
        layout.addWidget(QLabel("各分頁的條件會一起套用；未選取的項目不限制。"))
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        tags_page = QWidget()
        tag_columns = QHBoxLayout(tags_page)
        self.tag_all = TagPicker(connection)
        self.tag_any = TagPicker(connection)
        self.tag_not = TagPicker(connection)
        for title, picker, key in (
            ("全部符合 · AND", self.tag_all, "tag_all"),
            ("任一符合 · OR", self.tag_any, "tag_any"),
            ("排除 · NOT", self.tag_not, "tag_not"),
        ):
            group = QGroupBox(title)
            column = QVBoxLayout(group)
            picker.set_selected(current.get(key, []))
            column.addWidget(picker, 1)
            tag_columns.addWidget(group, 1)
        self.tabs.addTab(tags_page, "標籤")

        entities_page = QWidget()
        entity_row = QHBoxLayout(entities_page)
        self.entity_filters = {}
        for category in repo.list_relation_types(connection):
            kind = category["id"]
            picker = EntityPicker(connection, kind, editable=False)
            picker.scroll.setMinimumHeight(0)
            picker.scroll.setMaximumHeight(16777215)
            selected = set(current.get("entity_ids", [])) | set(current.get(f"{kind}_ids", []))
            picker.set_selected(selected & {row["id"] for row in repo.list_entities(connection, kind)})
            self.entity_filters[kind] = picker
            group = QGroupBox(category["name"])
            column = QVBoxLayout(group)
            column.addWidget(picker, 1)
            entity_row.addWidget(group, 1)
        self.tabs.addTab(entities_page, "關聯")
        self.person_filter = self.entity_filters.get("person")
        self.studio_filter = self.entity_filters.get("studio")

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        fields = QWidget()
        form = QGridLayout(fields)
        form.setContentsMargins(10, 8, 10, 8)
        form.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.duration_min = QLineEdit(str(current.get("duration_min_ms", "") // 1000) if current.get("duration_min_ms") else "")
        self.duration_max = QLineEdit(str(current.get("duration_max_ms", "") // 1000) if current.get("duration_max_ms") else "")
        self.size_min = QLineEdit(str(current.get("size_min", "") / 1024**3) if current.get("size_min") else "")
        self.size_max = QLineEdit(str(current.get("size_max", "") / 1024**3) if current.get("size_max") else "")
        self.rating = QSpinBox()
        self.rating.setRange(0, 5)
        self.rating.setValue(current.get("rating_min", 0))
        self.favorite = QComboBox()
        self.favorite.addItem("不限", None)
        self.favorite.addItem("只看收藏", True)
        self.favorite.addItem("排除收藏", False)
        if "favorite" in current:
            self.favorite.setCurrentIndex(1 if current["favorite"] else 2)
        self.added_from = QLineEdit(current.get("added_from", "")[:10])
        self.added_to = QLineEdit(current.get("added_to", "")[:10])
        self.modified_from = QLineEdit(self._modified_date(current.get("modified_from_ns")))
        self.modified_to = QLineEdit(self._modified_date(current.get("modified_to_ns")))
        self.minimum_width = QLineEdit(str(current.get("width_min", "")))
        self.minimum_height = QLineEdit(str(current.get("height_min", "")))
        for field in (self.added_from, self.added_to, self.modified_from, self.modified_to):
            field.setPlaceholderText("YYYY-MM-DD")
        def pair(first, second, separator="～"):
            first.setFixedWidth(96)
            second.setFixedWidth(96)
            first.setPlaceholderText("最小")
            second.setPlaceholderText("最大")
            group = QWidget()
            row = QHBoxLayout(group)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(first)
            row.addWidget(QLabel(separator))
            row.addWidget(second)
            return group

        self.rating.setFixedWidth(76)
        self.favorite.setFixedWidth(120)
        rows = (
            ("片長（秒）", pair(self.duration_min, self.duration_max)),
            ("容量（GB）", pair(self.size_min, self.size_max)),
            ("最低星等", self.rating), ("收藏", self.favorite),
            ("加入日期", pair(self.added_from, self.added_to)),
            ("修改日期", pair(self.modified_from, self.modified_to)),
            ("最小解析度", pair(self.minimum_width, self.minimum_height, "×")),
        )
        for field in (self.added_from, self.added_to, self.modified_from, self.modified_to):
            field.setPlaceholderText("YYYY-MM-DD")
        self.minimum_width.setPlaceholderText("寬")
        self.minimum_height.setPlaceholderText("高")
        for field in (self.added_from, self.added_to, self.modified_from, self.modified_to):
            field.setFixedWidth(112)
        for index, (title, entries) in enumerate((
            ("片長與容量", rows[:2]), ("星等與收藏", rows[2:4]),
            ("日期範圍", rows[4:6]), ("解析度", rows[6:]),
        )):
            group = QGroupBox(title)
            fields_layout = QGridLayout(group)
            for number, (label, field) in enumerate(entries):
                fields_layout.addWidget(QLabel(label), number, 0)
                fields_layout.addWidget(field, number, 1, Qt.AlignLeft)
            form.addWidget(group, index // 2, index % 2)
        scroll.setWidget(fields)
        self.tabs.addTab(scroll, "其他條件")
        self.selection_summary = QLabel()
        layout.addWidget(self.selection_summary)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("套用篩選")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.addButton("清空條件", QDialogButtonBox.ResetRole).clicked.connect(self.clear_all)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for picker in (self.tag_all, self.tag_any, self.tag_not):
            picker.tag_clicked.connect(self._update_counts)
        for picker in self.entity_filters.values():
            picker.selectionChanged.connect(self._update_counts)
        for field in self._condition_fields():
            field.textChanged.connect(self._update_counts)
        self.rating.valueChanged.connect(self._update_counts)
        self.favorite.currentIndexChanged.connect(self._update_counts)
        self._update_counts()

    @staticmethod
    def _modified_date(value) -> str:
        return datetime.fromtimestamp(value / 1e9).astimezone().date().isoformat() if value is not None else ""

    def _condition_fields(self):
        return (self.duration_min, self.duration_max, self.size_min, self.size_max,
                self.added_from, self.added_to, self.modified_from, self.modified_to, self.minimum_width, self.minimum_height)

    def _update_counts(self, *_args):
        tags = sum(len(picker.selected_ids()) for picker in (self.tag_all, self.tag_any, self.tag_not))
        entities = len(set().union(*(picker.selected_ids() for picker in self.entity_filters.values())))
        other = (sum(bool(field.text().strip()) for field in self._condition_fields())
                 + int(self.rating.value() > 0) + int(self.favorite.currentData() is not None))
        for index, (title, count) in enumerate((("標籤", tags), ("關聯", entities), ("其他條件", other))):
            self.tabs.setTabText(index, f"{title}（{count}）" if count else title)
        self.selection_summary.setText(f"已選：標籤 {tags} 項 · 關聯 {entities} 項 · 其他 {other} 項"
                                       if tags or entities or other else "尚未設定條件，套用後顯示全部影片。")

    def clear_all(self) -> None:
        for picker in (self.tag_all, self.tag_any, self.tag_not, *self.entity_filters.values()):
            picker.set_selected(set())
            picker.search.clear()
            picker.reload()
        for field in (self.duration_min, self.duration_max, self.size_min, self.size_max,
                      self.added_from, self.added_to, self.modified_from, self.modified_to,
                      self.minimum_width, self.minimum_height):
            field.clear()
        self.rating.setValue(0)
        self.favorite.setCurrentIndex(0)
        self._update_counts()

    @staticmethod
    def _utc_date(text: str, *, end: bool = False) -> str:
        day = datetime.strptime(text.strip(), "%Y-%m-%d").date()
        local = datetime.combine(day, time.max if end else time.min).astimezone(timezone.utc)
        return local.isoformat()

    def filters(self) -> dict:
        result: dict = {}
        for widget, key, factor in (
            (self.duration_min, "duration_min_ms", 1000),
            (self.duration_max, "duration_max_ms", 1000),
            (self.size_min, "size_min", 1024**3),
            (self.size_max, "size_max", 1024**3),
            (self.minimum_width, "width_min", 1),
            (self.minimum_height, "height_min", 1),
        ):
            if widget.text().strip():
                value = float(widget.text().strip())
                if value < 0:
                    raise ValueError("範圍值不可為負數")
                result[key] = round(value * factor)
        if self.rating.value():
            result["rating_min"] = self.rating.value()
        if self.favorite.currentData() is not None:
            result["favorite"] = self.favorite.currentData()
        for widget, key, end in (
            (self.added_from, "added_from", False),
            (self.added_to, "added_to", True),
        ):
            if widget.text().strip():
                result[key] = self._utc_date(widget.text(), end=end)
        for widget, key, end in (
            (self.modified_from, "modified_from_ns", False),
            (self.modified_to, "modified_to_ns", True),
        ):
            if widget.text().strip():
                result[key] = round(datetime.fromisoformat(self._utc_date(widget.text(), end=end)).timestamp() * 1e9)
        for picker, key in ((self.tag_all, "tag_all"), (self.tag_any, "tag_any"), (self.tag_not, "tag_not")):
            selected = sorted(picker.selected_ids())
            if selected:
                result[key] = selected
        selected = sorted(set().union(*(picker.selected_ids() for picker in self.entity_filters.values())))
        if selected:
            result["entity_ids"] = selected
        return result
