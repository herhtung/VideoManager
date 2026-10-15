"""Tag-first filters with optional metadata and date conditions."""

from __future__ import annotations

import sqlite3
from datetime import datetime, time, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from videomanager.ui.tag_picker import TagPicker


class FilterDialog(QDialog):
    def __init__(self, connection: sqlite3.Connection, current: dict | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("進階篩選")
        self.resize(1080, 760)
        self.setMinimumSize(840, 580)
        current = current or {}
        layout = QVBoxLayout(self)
        heading = QLabel("先選標籤")
        heading.setStyleSheet("font-size: 16px; font-weight: 700; color: #e1f4f0;")
        layout.addWidget(heading)
        layout.addWidget(QLabel("同一欄可點多個標籤；每個標籤分類可收合，清單可用滾輪捲動。"))
        tag_columns = QHBoxLayout()
        self.tag_all = TagPicker(connection)
        self.tag_any = TagPicker(connection)
        self.tag_not = TagPicker(connection)
        for title, picker, key in (
            ("全部符合 · AND", self.tag_all, "tag_all"),
            ("任一符合 · OR", self.tag_any, "tag_any"),
            ("排除 · NOT", self.tag_not, "tag_not"),
        ):
            column = QVBoxLayout()
            label = QLabel(title)
            label.setStyleSheet("font-weight: 700; color: #b9e7df;")
            column.addWidget(label)
            picker.set_selected(current.get(key, []))
            column.addWidget(picker, 1)
            tag_columns.addLayout(column, 1)
        layout.addLayout(tag_columns, 3)

        other = QLabel("其他條件")
        other.setStyleSheet("font-size: 15px; font-weight: 700; color: #e1f4f0; margin-top: 7px;")
        layout.addWidget(other)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        fields = QWidget()
        form = QFormLayout(fields)
        form.setContentsMargins(10, 8, 10, 8)
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
        self.modified_from = QLineEdit()
        self.modified_to = QLineEdit()
        self.width = QLineEdit(str(current.get("width_min", "")))
        self.height = QLineEdit(str(current.get("height_min", "")))
        for field in (self.added_from, self.added_to, self.modified_from, self.modified_to):
            field.setPlaceholderText("YYYY-MM-DD")
        for label, field in (
            ("片長最短（秒）", self.duration_min), ("片長最長（秒）", self.duration_max),
            ("大小最小（GB）", self.size_min), ("大小最大（GB）", self.size_max),
            ("最低星等", self.rating), ("收藏", self.favorite),
            ("加入日期起", self.added_from), ("加入日期迄", self.added_to),
            ("修改日期起", self.modified_from), ("修改日期迄", self.modified_to),
            ("最小寬度", self.width), ("最小高度", self.height),
        ):
            form.addRow(label, field)
        scroll.setWidget(fields)
        layout.addWidget(scroll, 2)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("套用篩選")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.addButton("清空條件", QDialogButtonBox.ResetRole).clicked.connect(self.clear_all)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def clear_all(self) -> None:
        for picker in (self.tag_all, self.tag_any, self.tag_not):
            picker.set_selected(set())
            picker.search.clear()
            picker.reload()
        for field in (self.duration_min, self.duration_max, self.size_min, self.size_max,
                      self.added_from, self.added_to, self.modified_from, self.modified_to,
                      self.width, self.height):
            field.clear()
        self.rating.setValue(0)
        self.favorite.setCurrentIndex(0)

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
            (self.width, "width_min", 1),
            (self.height, "height_min", 1),
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
        return result
