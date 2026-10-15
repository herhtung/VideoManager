"""Searchable, collapsible tag buttons shared by editing and management."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QScrollArea, QToolButton, QVBoxLayout, QWidget,
    QGridLayout,
)

from videomanager.db import repository as repo


TAG_DRAG_MIME = "application/x-videomanager-tag-id"
CATEGORY_DRAG_MIME = "application/x-videomanager-category-id"


class _DragButton(QPushButton):
    def __init__(self, label: str, identifier: str, mime_type: str | None, parent=None) -> None:
        super().__init__(label, parent)
        self.identifier = identifier
        self.mime_type = mime_type
        self._press_position = None

    def mousePressEvent(self, event) -> None:
        self._press_position = event.position().toPoint() if event.button() == Qt.LeftButton else None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (self.mime_type and self._press_position is not None
                and event.buttons() & Qt.LeftButton
                and (event.position().toPoint() - self._press_position).manhattanLength()
                >= QApplication.startDragDistance()):
            self._press_position = None
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(self.mime_type, self.identifier.encode("utf-8"))
            drag.setMimeData(mime)
            drag.setPixmap(self.grab())
            drag.exec(Qt.MoveAction)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._press_position = None
        super().mouseReleaseEvent(event)


class _CategoryButton(_DragButton):
    category_dropped = Signal(str, str, bool)
    tag_dropped = Signal(str, str)

    def __init__(self, label: str, identifier: str, *, reorderable: bool,
                 accept_tags: bool) -> None:
        super().__init__(label, identifier, CATEGORY_DRAG_MIME if reorderable else None)
        self.reorderable = reorderable
        self.accept_tags = accept_tags
        self.setAcceptDrops(reorderable or accept_tags)

    def _drop_kind(self, mime) -> str | None:
        if self.reorderable and mime.hasFormat(CATEGORY_DRAG_MIME):
            source = bytes(mime.data(CATEGORY_DRAG_MIME)).decode("utf-8")
            return "category" if source != self.identifier else None
        if self.accept_tags and mime.hasFormat(TAG_DRAG_MIME):
            return "tag"
        return None

    def dragEnterEvent(self, event) -> None:
        if self._drop_kind(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        kind = self._drop_kind(event.mimeData())
        if kind:
            edge = "bottom" if kind == "category" and event.position().y() >= self.height() / 2 else "top"
            self.setStyleSheet(f"border-{edge}: 3px solid #79d9ca;")
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self.setStyleSheet("")
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        kind = self._drop_kind(event.mimeData())
        self.setStyleSheet("")
        if kind == "category":
            source = bytes(event.mimeData().data(CATEGORY_DRAG_MIME)).decode("utf-8")
            self.category_dropped.emit(source, self.identifier, event.position().y() >= self.height() / 2)
        elif kind == "tag":
            source = bytes(event.mimeData().data(TAG_DRAG_MIME)).decode("utf-8")
            self.tag_dropped.emit(source, self.identifier)
        else:
            event.ignore()
            return
        event.acceptProposedAction()


class _TagButton(_DragButton):
    tag_reordered = Signal(str, str, bool)

    def __init__(self, label: str, identifier: str) -> None:
        super().__init__(label, identifier, TAG_DRAG_MIME)
        self.setAcceptDrops(True)

    def _accepts(self, mime) -> bool:
        return (mime.hasFormat(TAG_DRAG_MIME)
                and bytes(mime.data(TAG_DRAG_MIME)).decode("utf-8") != self.identifier)

    def dragEnterEvent(self, event) -> None:
        if self._accepts(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._accepts(event.mimeData()):
            edge = "right" if event.position().x() >= self.width() / 2 else "left"
            self.setStyleSheet(f"border-{edge}: 3px solid #79d9ca;")
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self.setStyleSheet("")
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self.setStyleSheet("")
        if not self._accepts(event.mimeData()):
            event.ignore()
            return
        source = bytes(event.mimeData().data(TAG_DRAG_MIME)).decode("utf-8")
        self.tag_reordered.emit(source, self.identifier, event.position().x() >= self.width() / 2)
        event.acceptProposedAction()


class _SectionButton(QToolButton):
    category_reordered = Signal(str, str, bool)
    tag_dropped = Signal(str, str)

    def __init__(self, identifier: str) -> None:
        super().__init__()
        self.identifier = identifier
        self._press_position = None
        self.setAcceptDrops(True)

    def mousePressEvent(self, event) -> None:
        self._press_position = event.position().toPoint() if event.button() == Qt.LeftButton else None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (self._press_position is not None and event.buttons() & Qt.LeftButton
                and (event.position().toPoint() - self._press_position).manhattanLength()
                >= QApplication.startDragDistance()):
            self._press_position = None
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(CATEGORY_DRAG_MIME, self.identifier.encode("utf-8"))
            drag.setMimeData(mime)
            drag.setPixmap(self.grab())
            drag.exec(Qt.MoveAction)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._press_position = None
        super().mouseReleaseEvent(event)

    def _drop_kind(self, mime) -> str | None:
        if mime.hasFormat(CATEGORY_DRAG_MIME):
            source = bytes(mime.data(CATEGORY_DRAG_MIME)).decode("utf-8")
            return "category" if source != self.identifier else None
        if mime.hasFormat(TAG_DRAG_MIME):
            return "tag"
        return None

    def dragEnterEvent(self, event) -> None:
        if self._drop_kind(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        kind = self._drop_kind(event.mimeData())
        if kind:
            edge = "bottom" if kind == "category" and event.position().y() >= self.height() / 2 else "top"
            self.setStyleSheet(f"border-{edge}: 3px solid #79d9ca;")
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self.setStyleSheet("")
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        kind = self._drop_kind(event.mimeData())
        self.setStyleSheet("")
        if kind == "category":
            source = bytes(event.mimeData().data(CATEGORY_DRAG_MIME)).decode("utf-8")
            self.category_reordered.emit(source, self.identifier, event.position().y() >= self.height() / 2)
        elif kind == "tag":
            source = bytes(event.mimeData().data(TAG_DRAG_MIME)).decode("utf-8")
            self.tag_dropped.emit(source, self.identifier)
        else:
            event.ignore()
            return
        event.acceptProposedAction()


class TagPicker(QWidget):
    tag_clicked = Signal(str)
    tag_reordered = Signal(str, str, bool)
    category_reordered = Signal(str, str, bool)
    tag_group_dropped = Signal(str, str)

    def __init__(self, connection: sqlite3.Connection, parent=None, *, multi_select: bool = True,
                 drag_tags: bool = False, show_empty_groups: bool = False) -> None:
        super().__init__(parent)
        self.connection = connection
        self.multi_select = multi_select
        self.drag_tags = drag_tags
        self.show_empty_groups = show_empty_groups
        self._selected: set[str] = set()
        self._expanded: dict[str, bool] = {}
        self._buttons: dict[str, list[QPushButton]] = {}
        self._headers: dict[str, QToolButton] = {}
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜尋標籤或標籤分類…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.reload)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { background: #172129; border: 1px solid #2b3a43; border-radius: 7px; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.search)
        layout.addWidget(self.scroll, 1)
        self.setMinimumHeight(180)
        self.setStyleSheet("""
            QToolButton#tagSection { text-align: left; color: #d8e8eb; background: #24343e;
                border: 0; border-radius: 5px; padding: 8px 10px; font-weight: 700; }
            QToolButton#tagSection:hover { background: #2e4852; }
            QPushButton#tagChip { text-align: left; background: #22333d; color: #dcebed;
                border: 1px solid #3a5660; border-radius: 7px; padding: 5px 9px; min-height: 26px; }
            QPushButton#tagChip:hover { border-color: #74cabc; background: #2c4a50; }
            QPushButton#tagChip:checked { background: #286e69; border-color: #79d9ca;
                color: white; font-weight: 700; }
        """)
        self.reload()

    def selected_ids(self) -> set[str]:
        return set(self._selected)

    def set_selected(self, ids: set[str] | list[str]) -> None:
        self._selected = set(ids)
        self._sync_buttons()

    def select_tag(self, tag_id: str) -> None:
        if self.multi_select:
            if tag_id in self._selected:
                self._selected.remove(tag_id)
            else:
                self._selected.add(tag_id)
        else:
            self._selected = {tag_id}
        self._sync_buttons()
        self.tag_clicked.emit(tag_id)

    def _sync_buttons(self) -> None:
        for tag_id, buttons in self._buttons.items():
            for button in buttons:
                button.setChecked(tag_id in self._selected)

    def _section(self, layout: QVBoxLayout, key: str, title: str, tags: list[sqlite3.Row],
                 *, force_open: bool = False) -> None:
        header = _SectionButton(key) if self.drag_tags else QToolButton()
        header.setObjectName("tagSection")
        header.setCheckable(True)
        expanded = True if force_open else self._expanded.get(key, True)
        header.setChecked(expanded)
        header.setText(f"{'▾' if expanded else '▸'}  {title}  ({len(tags)})")
        self._headers[key] = header
        if self.drag_tags:
            header.category_reordered.connect(self.category_reordered.emit)
            header.tag_dropped.connect(self.tag_group_dropped.emit)
        layout.addWidget(header)
        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(7, 5, 7, 9)
        grid.setSpacing(6)
        for index, tag in enumerate(tags):
            button = (_TagButton(tag["name"], tag["id"])
                      if self.drag_tags else QPushButton(tag["name"]))
            button.setObjectName("tagChip")
            button.setCheckable(True)
            button.setChecked(tag["id"] in self._selected)
            button.setToolTip(f"{tag['group_name']} › {tag['name']}")
            button.clicked.connect(lambda _=False, identifier=tag["id"]: self.select_tag(identifier))
            if self.drag_tags:
                button.tag_reordered.connect(self.tag_reordered.emit)
            grid.addWidget(button, index // 2, index % 2)
            self._buttons.setdefault(tag["id"], []).append(button)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addWidget(body)
        body.setVisible(expanded)

        def toggle(opened: bool) -> None:
            body.setVisible(opened)
            header.setText(f"{'▾' if opened else '▸'}  {title}  ({len(tags)})")
            if not self.search.text().strip():
                self._expanded[key] = opened

        header.toggled.connect(toggle)

    def reload(self) -> None:
        previous_scroll = self.scroll.verticalScrollBar().value()
        self._buttons = {}
        self._headers = {}
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(7, 7, 7, 7)
        layout.setSpacing(5)
        query = self.search.text().strip().casefold()
        rows = repo.list_tags(self.connection)
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            if query and query not in row["name"].casefold() and query not in row["group_name"].casefold():
                continue
            grouped.setdefault(row["group_id"], []).append(row)
        for group in repo.list_tag_groups(self.connection):
            tags = grouped.get(group["id"], [])
            if tags or (self.show_empty_groups and (not query or query in group["name"].casefold())):
                title = f"{group['name']} · 必選" if group["is_required"] else group["name"]
                self._section(layout, group["id"], title, tags, force_open=bool(query))
        if not grouped:
            message = QLabel("沒有符合的標籤" if query else "尚無標籤，按「＋」新增。")
            message.setStyleSheet("color: #9eb1b9; padding: 14px;")
            layout.addWidget(message)
        layout.addStretch()
        self.scroll.setWidget(container)
        self.scroll.verticalScrollBar().setValue(previous_scroll)


class CategoryChooser(QWidget):
    """Visible, scrollable category rows without a drop-down menu."""

    category_changed = Signal(str)
    category_reordered = Signal(str, str, bool)
    tag_dropped = Signal(str, str)

    def __init__(self, connection: sqlite3.Connection, parent=None, *, reorderable: bool = False,
                 accept_tags: bool = False) -> None:
        super().__init__(parent)
        self.connection = connection
        self.reorderable = reorderable
        self.accept_tags = accept_tags
        self._selected: str | None = None
        self._buttons: dict[str, QPushButton] = {}
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setMinimumHeight(115)
        self.scroll.setMaximumHeight(180)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll)
        self.setStyleSheet("""
            QPushButton#categoryRow { text-align: left; background: #22333d; color: #dcebed;
                border: 1px solid #3a5660; border-radius: 6px; padding: 5px 10px; }
            QPushButton#categoryRow:checked { background: #286e69; color: white;
                border-color: #79d9ca; font-weight: 700; }
        """)
        self.reload()

    def selected_id(self) -> str | None:
        return self._selected

    def select(self, category_id: str) -> None:
        if category_id not in self._buttons:
            return
        self._selected = category_id
        for identifier, button in self._buttons.items():
            button.setChecked(identifier == category_id)
        self.scroll.ensureWidgetVisible(self._buttons[category_id])
        self.category_changed.emit(category_id)

    def reload(self, preferred: str | None = None) -> None:
        rows = repo.list_tag_groups(self.connection)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(4)
        self._buttons = {}
        for row in rows:
            button = _CategoryButton(
                f"{row['name']} · 必選" if row["is_required"] else row["name"], row["id"],
                reorderable=self.reorderable, accept_tags=self.accept_tags,
            )
            button.setObjectName("categoryRow")
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, identifier=row["id"]: self.select(identifier))
            button.category_dropped.connect(self.category_reordered.emit)
            button.tag_dropped.connect(self.tag_dropped.emit)
            layout.addWidget(button)
            self._buttons[row["id"]] = button
        layout.addStretch()
        self.scroll.setWidget(container)
        wanted = preferred or self._selected
        if wanted not in self._buttons:
            wanted = rows[0]["id"] if rows else None
        if wanted:
            self.select(wanted)
        else:
            self._selected = None


class TagCreateDialog(QDialog):
    def __init__(self, connection: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self.connection = connection
        self.created_tag_id: str | None = None
        self.setWindowTitle("新增標籤")
        self.resize(540, 350)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("選擇標籤分類並輸入名稱。新增後會先選取，按「儲存」套用到影片。"))
        form = QFormLayout()
        groups = repo.list_tag_groups(connection)
        if not groups:
            repo.ensure_default_tag_group(connection)
        self.group = CategoryChooser(connection)
        self.name = QLineEdit()
        self.name.setPlaceholderText("標籤名稱")
        form.addRow("標籤分類", self.group)
        form.addRow("名稱", self.name)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("新增並選取")
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.name.setFocus()

    def _create(self) -> None:
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, "缺少名稱", "請輸入標籤名稱。")
            return
        try:
            group_id = self.group.selected_id()
            row = self.connection.execute(
                "SELECT id FROM tags WHERE group_id = ? AND name = ?", (group_id, name),
            ).fetchone()
            self.created_tag_id = row[0] if row else repo.create_tag(self.connection, group_id, name)
            self.accept()
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.critical(self, "新增標籤失敗", str(exc))
