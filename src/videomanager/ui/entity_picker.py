"""Compact searchable click-to-toggle chips for people and studios."""
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QInputDialog, QGridLayout, QHBoxLayout, QLineEdit,
                               QMessageBox, QPushButton, QScrollArea, QVBoxLayout,
                               QWidget, QLabel, QSizePolicy)

from videomanager.db import repository as repo
from videomanager.ui.entity_images import show_entity_thumbnail_menu, thumbnail_icon
from videomanager.ui.relation_button import RelationButton
from videomanager.ui.image_crop_dialog import photo_size


class EntityPicker(QWidget):
    selectionChanged = Signal()
    orderChanged = Signal()

    def __init__(self, connection: sqlite3.Connection, entity_type: str, parent=None, *, editable: bool = True) -> None:
        super().__init__(parent)
        self.connection = connection
        self.entity_type = entity_type
        self.editable = editable
        self._selected: set[str] = set()
        self._buttons: dict[str, QPushButton] = {}
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜尋…")
        self.search.setClearButtonEnabled(True)
        self.add_button = QPushButton("＋")
        self.add_button.setFixedWidth(36)
        self.add_button.setToolTip("新增關聯名稱")
        self.add_button.setVisible(editable)
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
        layout.addWidget(self.scroll, 1)
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
        grid.setAlignment(Qt.AlignTop)
        self._buttons = {}
        rows = [row for row in repo.list_entities(self.connection, self.entity_type)
                if not query or query in row["name"].casefold()]
        for index, row in enumerate(rows):
            identifier = row["id"]
            button = RelationButton(row["name"], identifier)
            if not self.editable:
                button.mime_type = None
                button.setAcceptDrops(False)
            button.entity_dropped.connect(lambda source, target, after: QTimer.singleShot(
                0, lambda: self._drop_entity(source, target, after)))
            button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            button.setIcon(thumbnail_icon(row["thumbnail_png"]))
            button.setIconSize(photo_size(96))
            button.setProperty("hasPortrait", bool(row["thumbnail_png"]))
            button.setObjectName("entityChip")
            button.setCheckable(True)
            button.setChecked(identifier in self._selected)
            button.setToolTip(f"{row['name']}\n" + ("點一下加入／移除；拖曳調整順序；右鍵管理照片"
                                                    if self.editable else "點選加入／移除篩選條件"))
            button.clicked.connect(lambda checked, entity_id=identifier: self._toggle(entity_id, checked))
            button.setContextMenuPolicy(Qt.CustomContextMenu if self.editable else Qt.NoContextMenu)
            button.customContextMenuRequested.connect(
                lambda point, entity_id=identifier, chip=button:
                self._thumbnail_menu(entity_id, chip, point)
            )
            grid.addWidget(button, index // 2, index % 2)
            self._buttons[identifier] = button
            row_buttons = list(self._buttons.values())[-(index % 2 + 1):]
            height = 66 if any(chip.property("hasPortrait") for chip in row_buttons) else 36
            for chip in row_buttons:
                chip.ensurePolished()
                chip.setFixedHeight(height)
        if not rows:
            message = QLabel("尚無項目，按「＋」新增。" if not query else "沒有符合項目")
            message.setStyleSheet("color: #9eb1b9; padding: 5px;")
            grid.addWidget(message, 0, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.scroll.setWidget(container)
        self.scroll.verticalScrollBar().setValue(previous_scroll)

    def _thumbnail_menu(self, entity_id: str, button: QPushButton, point) -> None:
        show_entity_thumbnail_menu(self, self.connection, entity_id, button.mapToGlobal(point),
                                   lambda _identifier: self.reload())

    def _drop_entity(self, source: str, target: str, after: bool) -> None:
        try:
            repo.reorder_entity(self.connection, source, target, after)
            self.reload()
            self.orderChanged.emit()
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "移動關聯失敗", str(exc))

    def _remove_thumbnail(self, entity_id: str) -> None:
        try:
            repo.set_entity_thumbnail(self.connection, entity_id, None)
            self.reload()
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "移除縮圖失敗", str(exc))

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
        label = next(row["name"] for row in repo.list_relation_types(self.connection) if row["id"] == self.entity_type)
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
