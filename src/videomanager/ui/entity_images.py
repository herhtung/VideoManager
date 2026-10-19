"""Small entity portraits stored in SQLite alongside their relations."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QBuffer, QIODevice, QSize, Qt
from PySide6.QtGui import QIcon, QImageReader, QPixmap
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from videomanager.db import repository as repo


def thumbnail_icon(png: bytes | None) -> QIcon:
    image = QPixmap()
    if png:
        image.loadFromData(png, "PNG")
    return QIcon(image)


def choose_entity_thumbnail(parent: QWidget, connection: sqlite3.Connection, entity_id: str) -> bool:
    path, _ = QFileDialog.getOpenFileName(parent, "選擇人物／片商縮圖", "", "圖片 (*.png *.jpg *.jpeg *.webp *.bmp)")
    if not path:
        return False
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    source_size = reader.size()
    if source_size.isValid():
        reader.setScaledSize(source_size.scaled(QSize(256, 256), Qt.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        QMessageBox.warning(parent, "縮圖讀取失敗", reader.errorString())
        return False
    image = image.scaled(72, 72, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    x = (image.width() - 72) // 2
    y = (image.height() - 72) // 2
    output = image.copy(x, y, 72, 72)
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    if not output.save(buffer, "PNG"):
        QMessageBox.warning(parent, "縮圖儲存失敗", "無法將圖片轉成縮圖。")
        return False
    try:
        repo.set_entity_thumbnail(connection, entity_id, bytes(buffer.data()))
    except (sqlite3.Error, ValueError) as exc:
        QMessageBox.warning(parent, "縮圖儲存失敗", str(exc))
        return False
    return True
