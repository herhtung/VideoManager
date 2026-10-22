"""Roomy, searchable tag and tag-category management."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QDialog, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidgetItem, QMessageBox, QSizePolicy,
    QPushButton, QScrollArea, QVBoxLayout, QWidget, QGridLayout,
    QTabWidget,
)

from videomanager.ui.notifications import notify_information
from videomanager.db import repository as repo
from videomanager.ui.tag_picker import CategoryChooser, TagPicker
from videomanager.ui.entity_images import (choose_entity_thumbnail, show_entity_thumbnail_menu,
                                          paste_entity_thumbnail, show_entity_photo, thumbnail_icon)
from videomanager.ui.relation_button import RelationButton
from videomanager.ui.image_crop_dialog import photo_size


class EntityButtonList(QScrollArea):
    """Four equally sized columns of relation buttons."""
    currentItemChanged = Signal(object, object)
    entity_dropped = Signal(str, str, bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        self.grid = QGridLayout(content)
        self.grid.setAlignment(Qt.AlignTop)
        self.grid.setContentsMargins(6, 6, 6, 6)
        self.grid.setSpacing(6)
        for column in range(4):
            self.grid.setColumnStretch(column, 1)
        self.setWidget(content)
        self._items = []
        self._buttons = []
        self._current = None

    def add_relation(self, item: QListWidgetItem) -> None:
        button = RelationButton(item.text(), item.data(Qt.UserRole))
        button.setIcon(item.icon())
        button.entity_dropped.connect(self.entity_dropped)
        button.setToolTip(item.text() + "\n拖曳調整順序，也能移到其他關聯類別。")
        button.setIconSize(photo_size(112))
        button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        button.setCheckable(True)
        button.setStyleSheet("QPushButton { text-align: left; min-height: 32px; } "
                             "QPushButton:checked { background: #286e69; border-color: #79d9ca; }")
        button.clicked.connect(lambda: self.setCurrentItem(item))
        index = len(self._items)
        self._items.append(item)
        self._buttons.append(button)
        self.grid.addWidget(button, index // 4, index % 4)
        row_buttons = self._buttons[index // 4 * 4:]
        height = 77 if any(not chip.icon().isNull() for chip in row_buttons) else 40
        for chip in row_buttons:
            chip.ensurePolished()
            chip.setFixedHeight(height)

    def count(self) -> int:
        return len(self._items)

    def item(self, index: int):
        return self._items[index]

    def currentItem(self):
        return self._current

    def itemWidget(self, item):
        return self._buttons[self._items.index(item)]

    def setCurrentRow(self, index: int) -> None:
        self.setCurrentItem(self.item(index))

    def setCurrentItem(self, item) -> None:
        previous = self._current
        self._current = item
        for candidate, button in zip(self._items, self._buttons):
            button.setChecked(candidate is item)
        if item is not previous:
            self.currentItemChanged.emit(item, previous)

    def clear(self) -> None:
        self.setCurrentItem(None)
        while self.grid.count():
            button = self.grid.takeAt(0).widget()
            button.hide()
            button.deleteLater()
        self._items.clear()
        self._buttons.clear()


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
        self.setWindowTitle("管理標籤／關聯")
        screen = parent.screen() if parent is not None else QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        width = min(1260, available.width() - 40) if available else 1260
        height = min(820, available.height() - 60) if available else 820
        self.resize(max(600, width), max(480, height))
        self.setMinimumSize(min(760, self.width()), min(520, self.height()))
        outer = QVBoxLayout(self)
        tabs = QTabWidget()
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
        tag_page = QWidget()
        tag_layout = QVBoxLayout(tag_page)
        tag_layout.addWidget(QLabel("可拖曳分類與標籤排序；拖曳標籤至分類可移動。"))
        tag_layout.addLayout(main, 1)
        tabs.addTab(tag_page, "標籤")
        tabs.addTab(self._entity_management_page(), "關聯")
        tabs.setTabText(0, "標籤")
        tabs.setTabText(1, "關聯")
        outer.addWidget(tabs, 1)
        close = QPushButton("完成")
        close.clicked.connect(self.accept)
        outer.addWidget(close)
        self._category_selected(self.categories.selected_id())
        self.refresh_entities()

    def _entity_management_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("最多三類關聯；越上面的類別，相關影片推薦越優先。"))
        self.add_relation_type_button = QPushButton("新增關聯類別")
        self.add_relation_type_button.clicked.connect(self.add_relation_type)
        layout.addWidget(self.add_relation_type_button)
        self.entity_sections = QScrollArea()
        self.entity_sections.setWidgetResizable(True)
        layout.addWidget(self.entity_sections, 1)
        self._reload_entity_sections()
        self.paste_photo_shortcut = QShortcut(QKeySequence.Paste, page)
        self.paste_photo_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.paste_photo_shortcut.activated.connect(self.paste_active_entity_image)
        return page

    def _reload_entity_sections(self) -> None:
        container = QWidget()
        layout = QVBoxLayout(container)
        self.entity_lists: dict[str, EntityButtonList] = {}
        self.entity_names: dict[str, QLineEdit] = {}
        self.entity_headers = {}
        self.entity_previews = {}
        self.entity_image_remove_buttons = {}
        categories = repo.list_relation_types(self.connection)
        self.add_relation_type_button.setEnabled(len(categories) < 3)
        for index, category in enumerate(categories):
            entity_type, title = category["id"], category["name"]
            section = QGroupBox()
            section_layout = QVBoxLayout(section)
            type_actions = QHBoxLayout()
            header = RelationButton(title, entity_type, category=True)
            header.setToolTip("拖曳調整類別順序；將關聯項目拖到這裡可移入類別。")
            header.type_dropped.connect(lambda source, target, after: QTimer.singleShot(
                0, lambda: self._relation_type_dropped(source, target, after)))
            header.entity_dropped.connect(lambda source, target, _after: QTimer.singleShot(
                0, lambda: self._entity_dropped(source, None, False, target)))
            self.entity_headers[entity_type] = header
            type_actions.addWidget(header)
            type_actions.addStretch()
            for text, action in (("類別改名", "rename"), ("↑", "up"), ("↓", "down"), ("移除空類別", "remove")):
                button = QPushButton(text)
                if action in {"up", "down"}:
                    button.setFixedWidth(32)
                    button.setEnabled(index > 0 if action == "up" else index < len(categories) - 1)
                if action == "remove":
                    button.setEnabled(len(categories) > 1 and not repo.list_entities(self.connection, entity_type))
                    button.setToolTip("只有沒有關聯項目的類別可以移除。")
                button.clicked.connect(lambda _=False, kind=entity_type, operation=action:
                                       self.change_relation_type(kind, operation))
                type_actions.addWidget(button)
            section_layout.addLayout(type_actions)
            listing = EntityButtonList()
            listing.setMinimumHeight(180)
            listing.entity_dropped.connect(lambda source, target, after: QTimer.singleShot(
                0, lambda: self._entity_dropped(source, target, after)))
            listing.currentItemChanged.connect(
                lambda current, _previous, kind=entity_type: self._entity_selected(kind, current))
            section_layout.addWidget(listing, 1)
            self.entity_lists[entity_type] = listing
            name = QLineEdit()
            name.setPlaceholderText(f"{title}名稱")
            name.returnPressed.connect(lambda kind=entity_type: self.add_entity(kind))
            editor_row = QHBoxLayout()
            preview = QPushButton("未設定照片")
            preview.setIconSize(photo_size(192))
            preview.setStyleSheet("QPushButton { padding: 4px; }")
            preview.ensurePolished()
            preview.setFixedSize(photo_size(192) + QSize(12, 12))
            preview.setToolTip("點一下放大檢視照片；沒有照片時可選擇圖片。")
            preview.setContextMenuPolicy(Qt.CustomContextMenu)
            preview.customContextMenuRequested.connect(
                lambda point, kind=entity_type, button=preview: self._entity_photo_menu(kind, button, point))
            preview.clicked.connect(lambda _=False, kind=entity_type: self.view_entity_image(kind))
            self.entity_previews[entity_type] = preview
            editor_row.addWidget(preview)
            editor = QVBoxLayout()
            editor.addWidget(name)
            self.entity_names[entity_type] = name
            actions = QGridLayout()
            for text, action in (("新增", self.add_entity), ("儲存名稱", self.rename_entity),
                                 ("更換照片…", self.set_entity_image), ("移除照片", self.remove_entity_image),
                                 ("貼上照片", self.paste_entity_image),
                                 ("項目上移", lambda kind: self.move_entity_item(kind, -1)),
                                 ("項目下移", lambda kind: self.move_entity_item(kind, 1)),
                                 ("刪除", self.delete_entity)):
                button = QPushButton(text)
                button.clicked.connect(lambda _=False, kind=entity_type, callback=action: callback(kind))
                number = actions.count()
                actions.addWidget(button, number // 4, number % 4)
                if text == "移除照片":
                    self.entity_image_remove_buttons[entity_type] = button
            editor.addLayout(actions)
            editor_row.addLayout(editor, 1)
            section_layout.addLayout(editor_row)
            layout.addWidget(section, 1)
        self.entity_sections.setWidget(container)
        self.refresh_entities()

    def add_relation_type(self) -> None:
        name, ok = QInputDialog.getText(self, "新增關聯類別", "類別名稱（最多三類）")
        if ok:
            try:
                repo.create_relation_type(self.connection, name)
                self._reload_entity_sections()
            except (sqlite3.Error, ValueError) as exc:
                QMessageBox.warning(self, "無法新增類別", str(exc))

    def change_relation_type(self, kind: str, action: str) -> None:
        try:
            if action == "rename":
                category = next(row for row in repo.list_relation_types(self.connection) if row["id"] == kind)
                name, ok = QInputDialog.getText(self, "關聯類別改名", "類別名稱", text=category["name"])
                if not ok:
                    return
                repo.rename_relation_type(self.connection, kind, name)
            elif action == "remove":
                repo.delete_relation_type(self.connection, kind)
            else:
                repo.move_relation_type(self.connection, kind, -1 if action == "up" else 1)
            self._reload_entity_sections()
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "無法修改類別", str(exc))

    def refresh_entities(self, preferred: str | None = None) -> None:
        if not hasattr(self, "entity_lists"):
            return
        for entity_type, listing in self.entity_lists.items():
            current = listing.currentItem()
            wanted = preferred or (current.data(Qt.UserRole) if current else None)
            listing.clear()
            for row in repo.list_entities(self.connection, entity_type):
                item = QListWidgetItem(row["name"])
                item.setIcon(thumbnail_icon(row["thumbnail_png"]))
                item.setData(Qt.UserRole, row["id"])
                listing.add_relation(item)
                if row["id"] == wanted:
                    listing.setCurrentItem(item)
            if listing.currentItem() is None and listing.count():
                listing.setCurrentRow(0)
            if listing.currentItem() is None:
                self.entity_names[entity_type].clear()

    def _entity_selected(self, entity_type: str, current) -> None:
        if current:
            self._active_entity_type = entity_type
        self.entity_names[entity_type].setText(current.text() if current else "")
        row = self.connection.execute("SELECT thumbnail_png FROM entities WHERE id=?",
                                      (current.data(Qt.UserRole),)).fetchone() if current else None
        png = row["thumbnail_png"] if row else None
        self.entity_previews[entity_type].setIcon(thumbnail_icon(png))
        self.entity_previews[entity_type].setText("" if png else "未設定照片")
        self.entity_image_remove_buttons[entity_type].setEnabled(bool(png))

    def paste_active_entity_image(self) -> None:
        focus = QApplication.focusWidget()
        if isinstance(focus, QLineEdit):
            focus.paste()
            return
        kind = getattr(self, "_active_entity_type", None)
        if kind in self.entity_lists:
            self.paste_entity_image(kind)

    def paste_entity_image(self, entity_type: str) -> None:
        item = self.entity_lists[entity_type].currentItem()
        if item and paste_entity_thumbnail(self, self.connection, item.data(Qt.UserRole)):
            self.refresh_entities(item.data(Qt.UserRole))

    def _entity_photo_menu(self, kind, button, point) -> None:
        item = self.entity_lists[kind].currentItem()
        if item is None:
            return
        identifier = item.data(Qt.UserRole)
        show_entity_thumbnail_menu(self, self.connection, identifier, button.mapToGlobal(point), self.refresh_entities)

    def view_entity_image(self, entity_type: str) -> None:
        item = self.entity_lists[entity_type].currentItem()
        row = self.connection.execute("SELECT name,thumbnail_png FROM entities WHERE id=?",
                                      (item.data(Qt.UserRole),)).fetchone() if item else None
        if row and row["thumbnail_png"]:
            show_entity_photo(self, row["thumbnail_png"], row["name"])
        elif item:
            self.set_entity_image(entity_type)

    def remove_entity_image(self, entity_type: str) -> None:
        item = self.entity_lists[entity_type].currentItem()
        if item:
            try:
                repo.set_entity_thumbnail(self.connection, item.data(Qt.UserRole), None)
                self.refresh_entities(item.data(Qt.UserRole))
            except (sqlite3.Error, ValueError) as exc:
                QMessageBox.warning(self, "移除照片失敗", str(exc))

    def _entity_dropped(self, source: str, target: str | None, after: bool,
                        target_type: str | None = None) -> None:
        try:
            repo.reorder_entity(self.connection, source, target, after, target_type=target_type)
            self.refresh_entities(source)
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "移動關聯失敗", str(exc))

    def _relation_type_dropped(self, source: str, target: str, after: bool) -> None:
        try:
            repo.reorder_relation_type(self.connection, source, target, after)
            self._reload_entity_sections()
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "移動關聯類別失敗", str(exc))

    def set_entity_image(self, entity_type: str) -> None:
        item = self.entity_lists[entity_type].currentItem()
        if item and choose_entity_thumbnail(self, self.connection, item.data(Qt.UserRole)):
            self.refresh_entities(item.data(Qt.UserRole))

    def move_entity_item(self, kind: str, step: int) -> None:
        item = self.entity_lists[kind].currentItem()
        if item is not None:
            try:
                repo.move_entity(self.connection, item.data(Qt.UserRole), step)
                self.refresh_entities(item.data(Qt.UserRole))
            except (sqlite3.Error, ValueError) as exc:
                QMessageBox.warning(self, "移動關聯項目失敗", str(exc))

    def add_entity(self, entity_type: str) -> None:
        name = self.entity_names[entity_type].text().strip()
        if not name:
            return
        try:
            identifier = repo.create_entity(self.connection, entity_type, name)
            self.refresh_entities(identifier)
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.critical(self, "新增關聯失敗", str(exc))

    def rename_entity(self, entity_type: str) -> None:
        item = self.entity_lists[entity_type].currentItem()
        if not item:
            return
        try:
            repo.rename_entity(self.connection, item.data(Qt.UserRole), self.entity_names[entity_type].text())
            self.refresh_entities(item.data(Qt.UserRole))
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.critical(self, "修改名稱失敗", str(exc))

    def delete_entity(self, entity_type: str) -> None:
        item = self.entity_lists[entity_type].currentItem()
        if not item:
            return
        linked = self.connection.execute("SELECT COUNT(*) FROM video_entities WHERE entity_id=?",
                                         (item.data(Qt.UserRole),)).fetchone()[0]
        answer = QMessageBox.question(
            self, "刪除關聯名稱",
            f"確定刪除「{item.text()}」？這會移除 {linked} 部影片上的此關聯，不會刪除影片或標籤。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            repo.delete_entity(self.connection, item.data(Qt.UserRole))
            self.entity_names[entity_type].clear()
            self.refresh_entities()
        except (sqlite3.Error, ValueError) as exc:
            QMessageBox.critical(self, "刪除關聯失敗", str(exc))

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
