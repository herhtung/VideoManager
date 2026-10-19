"""Compact searchable click-to-toggle chips for people and studios."""
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QInputDialog, QGridLayout, QHBoxLayout, QLineEdit,
                               QMessageBox, QPushButton, QScrollArea, QVBoxLayout,
                               QWidget, QLabel)

from videomanager.db import repository as repo


class EntityPicker(QWidget):
    selectionChanged = Signal()

    def __init__(self, connection: sqlite3.Connection, entity_type: str, parent=None) -> None:
        super().__init__(parent)
        self.connection = connection
        self.entity_type = entity_type
        self._selected: set[str] = set()
        self._buttons: dict[str, QPushButton] = {}
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜尋…")
        self.search.setClearButtonEnabled(True)
        self.add_button = QPushButton("＋")
        self.add_button.setFixedWidth(36)
        self.add_button.setToolTip("新增關聯名稱")
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFixedHeight(64)
        self.scroll.setStyleSheet("QScrollArea { background: #172129; border: 1px solid #2b3a43; border-radius: 7px; }")
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.search, 1)
        header.addWidget(self.add_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(header)
        layout.addWidget(self.scroll)
        self.setStyleSheet("""
            QPushButton#entityChip { text-align: left; background: #22333d; color: #dcebed;
                border: 1px solid #3a5660; border-radius: 7px; padding: 3px 7px; min-height: 22px; }
            QPushButton#entityChip:hover { border-color: #74cabc; background: #2c4a50; }
            QPushButton#entityChip:checked { background: #286e69; border-color: #79d9ca;
                color: white; font-weight: 700; }
        """)
        self.search.textChanged.connect(self.reload)
        self.add_button.clicked.connect(self.add_entity)
        self.reload()

    def reload(self) -> None:
        previous_scroll = self.scroll.verticalScrollBar().value()
        query = self.search.text().strip().casefold()
        container = QWidget()
        grid = QGridLayout(container)
        grid.setContentsMargins(5, 4, 5, 4)
        grid.setSpacing(4)
        self._buttons = {}
        rows = [row for row in repo.list_entities(self.connection, self.entity_type)
                if not query or query in row["name"].casefold()]
        for index, row in enumerate(rows):
            identifier = row["id"]
            button = QPushButton(row["name"])
            button.setObjectName("entityChip")
            button.setCheckable(True)
            button.setChecked(identifier in self._selected)
            button.setToolTip("點一下加入／移除；名稱可在「管理標籤／關聯」修改")
            button.clicked.connect(lambda checked, entity_id=identifier: self._toggle(entity_id, checked))
            grid.addWidget(button, index // 2, index % 2)
            self._buttons[identifier] = button
        if not rows:
            message = QLabel("尚無項目，按「＋」新增。" if not query else "沒有符合項目")
            message.setStyleSheet("color: #9eb1b9; padding: 5px;")
            grid.addWidget(message, 0, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.scroll.setWidget(container)
        self.scroll.verticalScrollBar().setValue(previous_scroll)

    def _toggle(self, identifier: str, checked: bool) -> None:
        if checked:
            self._selected.add(identifier)
        else:
            self._selected.discard(identifier)
        self.selectionChanged.emit()

    def selected_ids(self) -> set[str]:
        return set(self._selected)

    def set_selected(self, identifiers: set[str]) -> None:
        self._selected = set(identifiers)
        for identifier, button in self._buttons.items():
            button.setChecked(identifier in self._selected)
        self.selectionChanged.emit()

    def add_entity(self) -> None:
        label = "人物" if self.entity_type == "person" else "片商"
        name, ok = QInputDialog.getText(self, f"新增{label}", f"{label}名稱")
        if not ok or not name.strip():
            return
        try:
            identifier = repo.create_entity(self.connection, self.entity_type, name)
            self._selected.add(identifier)
            self.reload()
            self.selectionChanged.emit()
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, f"新增{label}失敗", str(exc))
