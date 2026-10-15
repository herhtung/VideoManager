"""Scrollable confirmation of existing MP4 filename changes."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from videomanager.services.existing_name_cleanup import ExistingNameScan


class ExistingNameDialog(QDialog):
    def __init__(self, scan: ExistingNameScan, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("掃描現有影片檔名")
        self.resize(850, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"找到 {len(scan.changes)} 部 MP4、{len(scan.group_changes)} 個影片組可改名；"
            f"{len(scan.skipped)} 部略過。影片組只改顯示名稱，不搬動資料夾。"
        ))
        self.table = QTableWidget(len(scan.changes) + len(scan.group_changes) + len(scan.skipped), 3)
        self.table.setHorizontalHeaderLabels(("目前名稱／路徑", "改後名稱／路徑", "結果"))
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        for index, change in enumerate(scan.changes):
            self._row(index, change.before, change.after, "MP4 可改名")
        for index, change in enumerate(scan.group_changes, start=len(scan.changes)):
            self._row(index, change.before, change.after, "影片組可改名")
        for index, (before, reason) in enumerate(scan.skipped,
                                                start=len(scan.changes) + len(scan.group_changes)):
            self._row(index, before, "—", f"略過：{reason}")
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        count = len(scan.changes) + len(scan.group_changes)
        buttons.button(QDialogButtonBox.Ok).setText(f"確認改名 {count} 項")
        buttons.button(QDialogButtonBox.Ok).setEnabled(bool(count))
        buttons.button(QDialogButtonBox.Cancel).setText("取消" if count else "關閉")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _row(self, number: int, before: str, after: str, status: str) -> None:
        for column, value in enumerate((before, after, status)):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            self.table.setItem(number, column, item)
