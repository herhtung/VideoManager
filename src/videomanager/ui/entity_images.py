"""Small entity portraits stored in SQLite alongside their relations."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QLabel, QMenu, QMessageBox, QVBoxLayout, QWidget

from videomanager.db import repository as repo
from videomanager.ui.image_crop_dialog import ImageCropDialog, clipboard_photo, read_photo, photo_size


def thumbnail_pixmap(png: bytes | None) -> QPixmap:
    image = QPixmap()
    if png:
        image.loadFromData(png, "PNG")
    if image.isNull():
        return image
    # Older square photos use an identical display crop everywhere, without rewriting stored data.
    size = photo_size(320)
    image = image.scaled(size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    return image.copy((image.width() - size.width()) // 2,
                      (image.height() - size.height()) // 2, size.width(), size.height())


def thumbnail_icon(png: bytes | None) -> QIcon:
    return QIcon(thumbnail_pixmap(png))


def show_entity_thumbnail_menu(parent, connection, entity_id, position, changed):
    """Keep photo actions identical in the sidebar and relationship manager."""
    row = connection.execute("SELECT name,thumbnail_png FROM entities WHERE id=?", (entity_id,)).fetchone()
    if row is None:
        return
    menu = QMenu(parent)

    def apply(callback):
        try:
            if callback(parent, connection, entity_id):
                changed(entity_id)
        except (sqlite3.Error, ValueError) as error:
            QMessageBox.warning(parent, "照片更新失敗", str(error))

    def remove(*_args):
        repo.set_entity_thumbnail(connection, entity_id, None)
        return True

    menu.addAction("選擇圖片…", lambda: apply(choose_entity_thumbnail))
    menu.addAction("貼上照片（Ctrl+V）", lambda: apply(paste_entity_thumbnail))
    png = row["thumbnail_png"]
    if png:
        menu.addAction("放大檢視照片", lambda: show_entity_photo(parent, png, row["name"]))
        menu.addAction("調整裁切…", lambda: apply(
            lambda owner, database, identifier: edit_entity_thumbnail(owner, database, identifier, QImage.fromData(png))))
        menu.addAction("複製照片", lambda: QApplication.clipboard().setImage(QImage.fromData(png)))
        menu.addAction("移除照片", lambda: apply(remove))
    try:
        menu.exec(position)
    finally:
        menu.deleteLater()


def choose_entity_thumbnail(parent: QWidget, connection: sqlite3.Connection, entity_id: str) -> bool:
    path, _ = QFileDialog.getOpenFileName(parent, "選擇關聯照片", "", "圖片 (*.png *.jpg *.jpeg *.webp *.bmp)")
    if not path:
        return False
    try:
        image = read_photo(path)
    except ValueError as error:
        QMessageBox.warning(parent, "圖片讀取失敗", str(error))
        return False
    return edit_entity_thumbnail(parent, connection, entity_id, image)


def paste_entity_thumbnail(parent: QWidget, connection: sqlite3.Connection, entity_id: str) -> bool:
    try:
        image = clipboard_photo()
    except ValueError as error:
        QMessageBox.information(parent, "無法貼上照片", str(error))
        return False
    return edit_entity_thumbnail(parent, connection, entity_id, image)


def edit_entity_thumbnail(parent, connection, entity_id, image):
    if image.isNull():
        QMessageBox.warning(parent, "圖片讀取失敗", "無法讀取這張照片。")
        return False
    dialog = ImageCropDialog(image, parent)
    try:
        if dialog.exec() != QDialog.Accepted:
            return False
        for size in (320, 256, 192):
            output = dialog.canvas.cropped_image(size)
            buffer = QBuffer()
            buffer.open(QIODevice.WriteOnly)
            if not output.save(buffer, "PNG"):
                QMessageBox.warning(parent, "照片儲存失敗", "無法將圖片轉成照片。")
                return False
            png = bytes(buffer.data())
            if len(png) <= 128 * 1024:
                break
        repo.set_entity_thumbnail(connection, entity_id, png)
        return True
    except (sqlite3.Error, ValueError) as exc:
        QMessageBox.warning(parent, "縮圖儲存失敗", str(exc))
        return False
    finally:
        dialog.deleteLater()


def show_entity_photo(parent: QWidget, png: bytes, name: str) -> None:
    image = thumbnail_pixmap(png)
    if image.isNull():
        return
    dialog = QDialog(parent)
    dialog.setWindowTitle(name)
    layout = QVBoxLayout(dialog)
    photo = QLabel()
    photo.setPixmap(image.scaled(320, 320, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    layout.addWidget(photo)
    dialog.exec()
