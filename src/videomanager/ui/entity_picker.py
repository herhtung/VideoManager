"""Small searchable multi-select control for people and studios."""
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from videomanager.db import repository as repo


class EntityPicker(QWidget):
    def __init__(self, connection: sqlite3.Connection, entity_type: str, parent=None) -> None:
        super().__init__(parent)
        self.connection = connection
        self.entity_type = entity_type
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜尋…")
        self.list = QListWidget()
        self.add_button = QPushButton("＋ 新增")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.search)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.add_button)
        self.search.textChanged.connect(self._filter)
        self.add_button.clicked.connect(self.add_entity)
        self.list.itemDoubleClicked.connect(self.rename_entity)
        self.reload()

    def reload(self) -> None:
        selected = self.selected_ids()
        self.list.clear()
        for row in repo.list_entities(self.connection, self.entity_type):
            item = QListWidgetItem(row["name"])
            item.setData(Qt.UserRole, row["id"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if row["id"] in selected else Qt.Unchecked)
            self.list.addItem(item)
        self._filter(self.search.text())

    def selected_ids(self) -> set[str]:
        return {self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked}

    def set_selected(self, identifiers: set[str]) -> None:
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in identifiers else Qt.Unchecked)

    def _filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for i in range(self.list.count()):
            self.list.item(i).setHidden(needle not in self.list.item(i).text().casefold())

    def add_entity(self) -> None:
        label = "人物" if self.entity_type == "person" else "片商"
        name, ok = QInputDialog.getText(self, f"新增{label}", f"{label}名稱")
        if not ok or not name.strip():
            return
        try:
            identifier = repo.create_entity(self.connection, self.entity_type, name)
            self.reload()
            self.set_selected(self.selected_ids() | {identifier})
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, f"新增{label}失敗", str(exc))

    def rename_entity(self, item: QListWidgetItem) -> None:
        name, ok = QInputDialog.getText(self, "修改名稱", "新名稱", text=item.text())
        if not ok or not name.strip():
            return
        try:
            repo.rename_entity(self.connection, item.data(Qt.UserRole), name)
            self.reload()
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "修改名稱失敗", str(exc))
