"""Confirm segment order before moving any original MP4."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget, QPushButton, QVBoxLayout


class GroupOrderDialog(QDialog):
    def __init__(self, paths: list[Path], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("確認影片組分段順序")
        self.resize(640, 450)
        layout = QVBoxLayout(self)
        label = QLabel(
            f"以下 {len(paths)} 個 MP4 會建立為同一組。請確認播放順序；"
            "第一段的檔名會作為影片組資料夾名稱。"
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        self.list = QListWidget()
        for path in paths:
            self.list.addItem(str(path))
        self.list.setCurrentRow(0)
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        up = QPushButton("上移")
        down = QPushButton("下移")
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        row.addWidget(up)
        row.addWidget(down)
        row.addStretch()
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("確認同組與順序")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _move(self, step: int) -> None:
        current = self.list.currentRow()
        target = current + step
        if current < 0 or target < 0 or target >= self.list.count():
            return
        item = self.list.takeItem(current)
        self.list.insertItem(target, item)
        self.list.setCurrentRow(target)

    def ordered_paths(self) -> list[Path]:
        return [Path(self.list.item(index).text()) for index in range(self.list.count())]
