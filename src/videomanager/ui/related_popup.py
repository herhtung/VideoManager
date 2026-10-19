"""Hover card for videos sharing a person or studio."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QPushButton, QVBoxLayout


class RelatedVideosPopup(QFrame):
    video_activated = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setObjectName("relatedVideosPopup")
        self.setStyleSheet("""
            QFrame#relatedVideosPopup { background: #172129; border: 1px solid #79d9ca; border-radius: 8px; }
            QLabel { color: #eaf0f2; padding: 3px; }
            QPushButton { background: #22333d; color: #dcebed; border: 1px solid #3a5660;
                border-radius: 6px; padding: 7px; text-align: left; }
            QPushButton:hover { background: #286e69; border-color: #79d9ca; }
        """)
        self.layout_root = QVBoxLayout(self)
        self.layout_root.setContentsMargins(9, 7, 9, 9)
        self.heading = QLabel("相似影片")
        self.layout_root.addWidget(self.heading)
        self.content = QGridLayout()
        self.content.setSpacing(5)
        self.layout_root.addLayout(self.content)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(650)
        self.hide_timer.timeout.connect(self.hide)

    def show_videos(self, rows) -> None:
        while self.content.count():
            item = self.content.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.heading.setText("相似影片 · 共用「人物／片商」")
        for index, row in enumerate(rows):
            button = QPushButton(row["display_title"])
            names = row["shared_names"] or ""
            button.setToolTip(f"共用：{names}")
            button.clicked.connect(lambda _=False, identifier=row["id"]: self.video_activated.emit(identifier))
            self.content.addWidget(button, index // 3, index % 3)
        self.adjustSize()

    def schedule_hide(self) -> None:
        self.hide_timer.start()

    def enterEvent(self, event) -> None:
        self.hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.schedule_hide()
        super().leaveEvent(event)
