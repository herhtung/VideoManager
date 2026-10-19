"""Choose existing ungrouped videos by their cover images."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QLineEdit, QListWidget, QVBoxLayout

from videomanager.ui.video_picker import configure_video_grid, placeholder_icon, video_item


class GroupMembersDialog(QDialog):
    def __init__(self, rows, cache_root: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("加入影片組 · 選擇分段")
        self.resize(1040, 760)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("點選縮圖可選取多段；再點一次可取消。原檔位置不變，整組資料會套用到新分段。"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜尋顯示名稱或檔名…")
        layout.addWidget(self.search)
        self.count = QLabel("尚未選取分段")
        layout.addWidget(self.count)
        self.items = QListWidget()
        configure_video_grid(self.items)
        fallback = placeholder_icon()
        for row in rows:
            item = video_item(row, cache_root, fallback)
            item.setData(Qt.UserRole, row["id"])
            item.setData(Qt.UserRole + 1, f"{row['display_title']} {row['relative_path']}".casefold())
            self.items.addItem(item)
        layout.addWidget(self.items, 1)
        self.search.textChanged.connect(self._filter)
        self.items.itemSelectionChanged.connect(self._selection_changed)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.add_button = buttons.button(QDialogButtonBox.Ok)
        self.add_button.setText("加入選取分段")
        self.add_button.setEnabled(False)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _filter(self, text: str) -> None:
        needle = text.casefold().strip()
        for index in range(self.items.count()):
            item = self.items.item(index)
            item.setHidden(needle not in item.data(Qt.UserRole + 1))

    def _selection_changed(self) -> None:
        count = len(self.selected_ids())
        self.count.setText(f"已選 {count} 段" if count else "尚未選取分段")
        self.add_button.setEnabled(count > 0)

    def selected_ids(self) -> list[str]:
        return [item.data(Qt.UserRole) for item in self.items.selectedItems()]
