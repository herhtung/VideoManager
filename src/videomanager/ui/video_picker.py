"""Thumbnail cards shared by video selection dialogs."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListView, QListWidget, QListWidgetItem


def configure_video_grid(widget: QListWidget) -> None:
    widget.setViewMode(QListView.IconMode)
    widget.setMovement(QListView.Static)
    widget.setResizeMode(QListView.Adjust)
    widget.setWrapping(True)
    widget.setWordWrap(True)
    widget.setUniformItemSizes(True)
    widget.setIconSize(QSize(176, 100))
    widget.setGridSize(QSize(196, 154))
    widget.setSpacing(6)
    widget.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    widget.setSelectionMode(QAbstractItemView.MultiSelection)


def _placeholder() -> QIcon:
    canvas = QPixmap(176, 100)
    canvas.fill(QColor("#182a33"))
    painter = QPainter(canvas)
    painter.setPen(QColor("#83aaa9"))
    painter.drawText(canvas.rect(), Qt.AlignCenter, "尚無縮圖")
    painter.end()
    return QIcon(canvas)


def placeholder_icon() -> QIcon:
    return _placeholder()


def video_item(row, cache_root: Path, fallback: QIcon | None = None) -> QListWidgetItem:
    path = cache_root / row["id"] / "00.webp"
    title = row["display_title"] or Path(row["relative_path"]).stem
    item = QListWidgetItem(QIcon(str(path)) if path.is_file() else (fallback or _placeholder()), title)
    item.setToolTip(f"{title}\n{row['relative_path']}")
    return item
