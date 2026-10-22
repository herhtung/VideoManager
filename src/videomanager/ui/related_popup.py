"""Hover preview of videos sharing a person or studio."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget
from videomanager.ui.entity_images import thumbnail_icon


class RelatedVideoCard(QPushButton):
    def __init__(self, row, cache_root: Path | None, frame_count: int = 10,
                 interval_ms: int = 500, parent=None, *, preview_width: int = 143) -> None:
        super().__init__(parent)
        self.video_id = row["id"]
        self.cache_root = cache_root
        self.frame_count = frame_count
        self.frame = 0
        self._card_size = QSize(preview_width + 12, round(preview_width * 9 / 16) + 77)
        self.setFixedSize(self._card_size)
        self.setToolTip("點選播放影片" if preview_width > 143 else
                        f"共用：{row['shared_names'] or '人物／片商'}\n點選後在影片清單定位")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(3)
        is_group = bool(dict(row).get("_group_cover"))
        title = QLabel(("影片組 · " if is_group else "") + row["display_title"])
        title.setFixedHeight(24)
        title.setToolTip(row["display_title"])
        title.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.image = QLabel("尚無縮圖")
        self.image.setFixedSize(preview_width, round(preview_width * 9 / 16))
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setStyleSheet("background: #0b1116; border-radius: 4px;")
        self.image.setAttribute(Qt.WA_TransparentForMouseEvents)
        shared = QLabel((f"{row['group_size']} 段 · " if is_group else "") + (row["shared_names"] or "共同關聯"))
        shared.setFixedHeight(28)
        shared.setWordWrap(True)
        shared.setToolTip(f"共用：{row['shared_names'] or ''}")
        shared.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(title)
        layout.addWidget(self.image)
        layout.addWidget(shared)
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self._next_frame)
        self._show_frame(0)

    def sizeHint(self) -> QSize:
        return self._card_size

    def minimumSizeHint(self) -> QSize:
        return self._card_size

    def _show_frame(self, frame: int) -> None:
        if self.cache_root is None:
            return
        cache_frame = round(frame * (self.frame_count - 1) / 9)
        path = self.cache_root / self.video_id / f"{cache_frame:02d}.webp"
        if not path.is_file():
            path = self.cache_root / self.video_id / "00.webp"
        pixmap = QPixmap(str(path)) if path.is_file() else QPixmap()
        if not pixmap.isNull():
            self.image.setPixmap(pixmap.scaled(self.image.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.image.clear()
            self.image.setText("尚無縮圖")

    def _next_frame(self) -> None:
        self.frame = (self.frame + 1) % 10
        self._show_frame(self.frame)

    def enterEvent(self, event) -> None:
        self.timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.timer.stop()
        self.frame = 0
        self._show_frame(0)
        super().leaveEvent(event)


class RelatedVideosPopup(QFrame):
    video_activated = Signal(str)
    show_all_requested = Signal(str)
    entity_requested = Signal(str, str)
    tag_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setObjectName("relatedVideosPopup")
        self.setStyleSheet("""
            QFrame#relatedVideosPopup { background: #172129; border: 1px solid #3c5964; border-radius: 10px; }
            QLabel { color: #eaf0f2; padding: 2px; }
            QPushButton { background: #22333d; color: #dcebed; border: 1px solid #3a5660;
                border-radius: 6px; padding: 4px; text-align: left; }
            QPushButton:hover { background: #286e69; border-color: #79d9ca; }
        """)
        self.layout_root = QVBoxLayout(self)
        self.layout_root.setContentsMargins(9, 7, 9, 9)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(9)
        self.body_layout.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(body)
        self.layout_root.addWidget(self.scroll)
        self.heading = QLabel("相似影片")
        self.heading.setWordWrap(True)
        self.heading.setStyleSheet("font-weight: bold; font-size: 15px;")
        self.body_layout.addWidget(self.heading)
        self.details_layout = QVBoxLayout()
        self.details_layout.setSpacing(6)
        self.body_layout.addLayout(self.details_layout)
        self.related_heading = QLabel("相關影片")
        self.body_layout.addWidget(self.related_heading)
        self.content = QGridLayout()
        self.content.setSpacing(5)
        self.content.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.body_layout.addLayout(self.content)
        self.show_all_button = QPushButton("顯示全部相關影片")
        self.show_all_button.hide()
        self.body_layout.addWidget(self.show_all_button)
        self._show_all_slot = None
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(650)
        self.hide_timer.timeout.connect(self.hide)

    def show_videos(self, rows, cache_root: Path | None = None,
                    source_title: str = "", source_id: str = "", frame_count: int = 10,
                    interval_ms: int = 500, *, source=None, entities=(), tags=(),
                    max_width: int = 700, max_height: int = 850, relation_types=None) -> None:
        self._clear_layout(self.details_layout)
        self.source_id = source_id
        desired_width = 680 if len(rows) >= 4 else max(420, len(rows) * 160 + 40)
        width = min(desired_width, max_width)
        columns = max(1, min(4, (width - 40) // 160))
        if source is not None:
            relations = QWidget()
            relation_row = QHBoxLayout(relations)
            relation_row.setContentsMargins(0, 0, 0, 0)
            relation_row.setSpacing(12)
            categories = relation_types if relation_types is not None else (
                {"name": "人物", "id": "person"}, {"name": "片商", "id": "studio"})
            for category in categories:
                title, kind = category["name"], category["id"]
                section = QWidget()
                column = QVBoxLayout(section)
                column.setContentsMargins(0, 0, 0, 0)
                column.setSpacing(4)
                column.setAlignment(Qt.AlignTop)
                heading = QLabel(title)
                heading.setWordWrap(True)
                heading.setToolTip(title)
                heading.setStyleSheet("color: #9db4bd; font-weight: bold;")
                column.addWidget(heading)
                entries = [entry for entry in entities if entry["type"] == kind]
                column.addWidget(self._chips(entries, kind, (width - 40 - 12 * (len(categories) - 1)) // len(categories)), 0, Qt.AlignTop)
                relation_row.addWidget(section, 1)
            self.details_layout.addWidget(relations)
            tag_heading = QLabel(f"標籤 · {len(tags)}")
            tag_heading.setStyleSheet("color: #9db4bd; font-weight: bold;")
            self.details_layout.addWidget(tag_heading)
            self.details_layout.addWidget(self._chips(tags, "tag", width - 40))
        while self.content.count():
            item = self.content.takeAt(0)
            widget = item.widget()
            if widget:
                if isinstance(widget, RelatedVideoCard):
                    widget.timer.stop()
                widget.hide()
                widget.deleteLater()
        source_kind = "影片組詳情" if source is not None and dict(source).get("_group_cover") else "影片詳情"
        self.heading.setText(f"{source_kind} · {source_title}" if source is not None else
                             f"與「{source_title}」相關的影片" if source_title else "相似影片 · 共用人物／片商")
        self.heading.setToolTip(source["file_name"] if source is not None else source_title)
        self.related_heading.setVisible(bool(rows))
        for index, row in enumerate(rows[:8]):
            card = RelatedVideoCard(row, cache_root, frame_count, interval_ms, self)
            card.clicked.connect(lambda _=False, identifier=row["id"]: self.video_activated.emit(identifier))
            self.content.addWidget(card, index // columns, index % columns, Qt.AlignLeft | Qt.AlignTop)
        self.show_all_button.setVisible(len(rows) > 8)
        if self._show_all_slot is not None:
            self.show_all_button.clicked.disconnect(self._show_all_slot)
            self._show_all_slot = None
        if source_id:
            self._show_all_slot = lambda: self.show_all_requested.emit(source_id)
            self.show_all_button.clicked.connect(self._show_all_slot)
        self.setFixedWidth(width)
        self.scroll.widget().adjustSize()
        height = min(max_height, max(self.body_layout.sizeHint().height(),
                                     self.body_layout.minimumSize().height()) + 24)
        self.setFixedHeight(max(150, height))
        self.scroll.verticalScrollBar().setValue(0)

    def _chips(self, entries, kind: str, width: int) -> QWidget:
        widget = QWidget(self)
        lines = QVBoxLayout(widget)
        lines.setContentsMargins(0, 0, 0, 0)
        lines.setSpacing(4)
        lines.setAlignment(Qt.AlignTop)
        row = QHBoxLayout()
        row.setSpacing(5)
        row.setAlignment(Qt.AlignLeft)
        lines.addLayout(row)
        occupied = 0
        for entry in entries:
            button = QPushButton(entry["name"], widget)
            button.setToolTip(entry["name"])
            button.setStyleSheet("QPushButton { min-height: 22px; padding: 2px 6px; border-radius: 9px; }")
            extra = 0
            if kind == "tag":
                button.clicked.connect(lambda _=False, identifier=entry["id"]: self.tag_requested.emit(identifier))
            else:
                icon = thumbnail_icon(entry["thumbnail_png"])
                button.setIcon(icon)
                button.setIconSize(QSize(32, 18))
                extra = 36 if not icon.isNull() else 0
                button.clicked.connect(lambda _=False, identifier=entry["id"], entity_type=kind:
                                       self.entity_requested.emit(entity_type, identifier))
            button.ensurePolished()
            chip_width = min(width, 220, max(32, button.fontMetrics().horizontalAdvance(entry["name"]) + 20 + extra))
            button.setFixedWidth(chip_width)
            if occupied and occupied + 5 + chip_width > width:
                row = QHBoxLayout()
                row.setSpacing(5)
                row.setAlignment(Qt.AlignLeft)
                lines.addLayout(row)
                occupied = 0
            row.addWidget(button)
            occupied += chip_width + (5 if occupied else 0)
        if not entries:
            empty = QLabel("未設定")
            empty.setStyleSheet("color: #7f969f;")
            row.addWidget(empty)
        return widget

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                for timer in widget.findChildren(QTimer):
                    timer.stop()
                widget.hide()
                widget.deleteLater()

    def schedule_hide(self) -> None:
        self.hide_timer.start()

    def hideEvent(self, event) -> None:
        for index in range(self.content.count()):
            card = self.content.itemAt(index).widget()
            if isinstance(card, RelatedVideoCard):
                card.timer.stop()
        super().hideEvent(event)

    def enterEvent(self, event) -> None:
        self.hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.schedule_hide()
        super().leaveEvent(event)
