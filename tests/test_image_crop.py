import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QBuffer, QCoreApplication, QEvent, QIODevice, QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QTabWidget
from videomanager.db import repository as repo
from videomanager.db.database import open_database
from videomanager.ui.entity_images import (edit_entity_thumbnail, paste_entity_thumbnail,
                                          thumbnail_pixmap, show_entity_thumbnail_menu)
from videomanager.ui.image_crop_dialog import CropCanvas, ImageCropDialog, clipboard_photo, photo_size
from videomanager.ui.entity_picker import EntityPicker
from videomanager.ui.metadata_dialog import MetadataDialog


class ImageCropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.connection = open_database(base / "data.db", base / "backups")
        self.entity = repo.create_entity(self.connection, "person", "人物甲")
        self.image = QImage(600, 300, QImage.Format_RGB32)
        self.image.fill(Qt.red)
        painter = QPainter(self.image)
        painter.fillRect(300, 0, 300, 300, Qt.blue)
        painter.end()
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        self.image.save(buffer, "PNG")
        self.original = bytes(buffer.data())
        repo.set_entity_thumbnail(self.connection, self.entity, self.original)
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.connection.close()
        self.temp.cleanup()

    def test_drag_zoom_stays_inside_image_and_saves_landscape(self):
        canvas = CropCanvas(self.image)
        self.widgets.append(canvas)
        canvas.resize(400, 320)
        canvas.show()
        self.app.processEvents()
        canvas.set_zoom(200)
        start = canvas.rect().center()
        QTest.mousePress(canvas, Qt.LeftButton, pos=start)
        QTest.mouseMove(canvas, start - QPoint(150, 0))
        QTest.mouseRelease(canvas, Qt.LeftButton, pos=start - QPoint(150, 0))
        output = canvas.cropped_image()
        self.assertEqual(output.size(), photo_size(320))
        self.assertAlmostEqual(canvas.frame().width() / canvas.frame().height(), 16 / 9)
        self.assertGreater(output.pixelColor(160, 90).blue(), 200)
        canvas.center = canvas.center * 10000
        rect = canvas.source_rect()
        self.assertGreaterEqual(rect.left(), -0.001)
        self.assertLessEqual(rect.right(), self.image.width() + 0.001)
        self.assertLessEqual(rect.bottom(), self.image.height() + 0.001)
        canvas.set_image(self.image)
        self.assertEqual(canvas.zoom, 100)

    def test_clipboard_crop_confirm_and_cancel_preserve_previous_photo(self):
        parent = QDialog()
        self.widgets.append(parent)
        clipboard = Mock()
        clipboard.image.return_value = self.image
        with patch("videomanager.ui.image_crop_dialog.QApplication.clipboard", return_value=clipboard), \
                patch.object(ImageCropDialog, "exec", return_value=QDialog.Rejected):
            self.assertFalse(paste_entity_thumbnail(parent, self.connection, self.entity))
        self.assertEqual(self.connection.execute("SELECT thumbnail_png FROM entities WHERE id=?", (self.entity,)).fetchone()[0], self.original)

        def accept(dialog):
            dialog.canvas.set_zoom(200)
            dialog.canvas.center.setX(450)
            return QDialog.Accepted

        with patch("videomanager.ui.image_crop_dialog.QApplication.clipboard", return_value=clipboard), \
                patch.object(ImageCropDialog, "exec", accept):
            self.assertTrue(paste_entity_thumbnail(parent, self.connection, self.entity))
        png = self.connection.execute("SELECT thumbnail_png FROM entities WHERE id=?", (self.entity,)).fetchone()[0]
        saved = QImage.fromData(png)
        self.assertEqual(saved.size(), photo_size(320))
        self.assertEqual(saved.pixelColor(160, 90), QColor(Qt.blue))
        self.assertEqual(thumbnail_pixmap(png).toImage().pixelColor(160, 90), saved.pixelColor(160, 90))

    def test_empty_clipboard_is_rejected_without_changing_photo(self):
        clipboard = Mock()
        clipboard.image.return_value = QImage()
        clipboard.mimeData.return_value.urls.return_value = []
        with patch("videomanager.ui.image_crop_dialog.QApplication.clipboard", return_value=clipboard):
            with self.assertRaises(ValueError):
                clipboard_photo()
            clipboard.mimeData.return_value = None
            with self.assertRaises(ValueError):
                clipboard_photo()
        self.assertEqual(self.connection.execute("SELECT thumbnail_png FROM entities WHERE id=?", (self.entity,)).fetchone()[0], self.original)

    def test_manager_paste_and_text_paste_and_portrait_geometry(self):
        manager = MetadataDialog(self.connection)
        self.widgets.append(manager)
        manager.findChild(QTabWidget).setCurrentIndex(1)
        manager.show()
        self.app.processEvents()
        listing = manager.entity_lists["person"]
        card = listing.itemWidget(listing.item(0))
        QTest.mouseClick(card, Qt.LeftButton)
        self.assertGreaterEqual(card.height(), card.iconSize().height() + 8)
        self.assertEqual(card.iconSize(), photo_size(112))
        preview = manager.entity_previews["person"]
        self.assertEqual(preview.iconSize(), photo_size(192))
        self.assertGreaterEqual(preview.height(), preview.iconSize().height() + 8)
        self.assertGreaterEqual(preview.width(), preview.iconSize().width() + 8)
        with patch("videomanager.ui.entity_images.clipboard_photo", return_value=self.image), \
                patch.object(ImageCropDialog, "exec", return_value=QDialog.Accepted) as edit:
            QTest.keyClick(card, Qt.Key_V, Qt.ControlModifier)
            self.assertEqual(edit.call_count, 1)
            field = manager.entity_names["person"]
            field.setText("")
            field.setFocus()
            self.app.processEvents()
            QApplication.clipboard().setText("paste-name")
            QTest.keyClick(field, Qt.Key_V, Qt.ControlModifier)
            self.assertEqual(field.text(), "paste-name")
            self.assertEqual(edit.call_count, 1)

    def test_crop_dialog_slider_reset_and_clipboard_shortcut(self):
        dialog = ImageCropDialog(self.image)
        self.widgets.append(dialog)
        dialog.show()
        self.app.processEvents()
        dialog.zoom.setValue(250)
        self.assertEqual(dialog.canvas.zoom, 250)
        dialog.reset_crop()
        self.assertEqual(dialog.zoom.value(), 100)
        portrait = QImage(100, 300, QImage.Format_RGB32)
        portrait.fill(Qt.green)
        with patch("videomanager.ui.image_crop_dialog.clipboard_photo", return_value=portrait):
            QTest.keyClick(dialog, Qt.Key_V, Qt.ControlModifier)
            self.assertEqual(dialog.canvas.image.size(), portrait.size())

    def test_portrait_and_landscape_crop_match_all_photo_previews(self):
        for width, height in ((300, 600), (900, 200), (600, 600)):
            with self.subTest(size=(width, height)):
                image = QImage(width, height, QImage.Format_RGB32)
                painter = QPainter(image)
                painter.fillRect(0, 0, width, height // 3, Qt.red)
                painter.fillRect(0, height // 3, width, height // 3, Qt.green)
                painter.fillRect(0, 2 * height // 3, width, height - 2 * height // 3, Qt.blue)
                painter.end()
                canvas = CropCanvas(image)
                self.widgets.append(canvas)
                canvas.resize(500, 300)
                source = canvas.source_rect()
                self.assertAlmostEqual(source.width() / source.height(), 16 / 9)
                self.assertGreaterEqual(source.left(), -0.001)
                self.assertGreaterEqual(source.top(), -0.001)
                self.assertLessEqual(source.right(), width + 0.001)
                self.assertLessEqual(source.bottom(), height + 0.001)
                output = canvas.cropped_image()
                buffer = QBuffer()
                buffer.open(QIODevice.WriteOnly)
                output.save(buffer, "PNG")
                png = bytes(buffer.data())
                self.assertEqual(thumbnail_pixmap(png).toImage().convertToFormat(QImage.Format_ARGB32),
                                 output.convertToFormat(QImage.Format_ARGB32))
                repo.set_entity_thumbnail(self.connection, self.entity, png)
                manager = MetadataDialog(self.connection)
                picker = EntityPicker(self.connection, "person")
                self.widgets.extend((manager, picker))
                listing = manager.entity_lists["person"]
                listing.setCurrentRow(0)
                for button in (listing.itemWidget(listing.item(0)),
                               manager.entity_previews["person"], picker._buttons[self.entity]):
                    pixmap = button.icon().pixmap(button.iconSize())
                    self.assertEqual(pixmap.size(), button.iconSize())
                    expected = output.scaled(button.iconSize(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                    actual = pixmap.toImage()
                    # QIcon can use a different storage format; compare visible pixels.
                    self.assertEqual(actual.pixelColor(actual.width() // 2, actual.height() // 2),
                                     expected.pixelColor(expected.width() // 2, expected.height() // 2))

    def test_old_square_photo_has_one_display_crop_without_rewriting_data(self):
        image = QImage(192, 192, QImage.Format_RGB32)
        image.fill(Qt.green)
        painter = QPainter(image)
        painter.fillRect(0, 0, 192, 30, Qt.red)
        painter.fillRect(0, 162, 192, 30, Qt.blue)
        painter.end()
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, "PNG")
        png = bytes(buffer.data())
        repo.set_entity_thumbnail(self.connection, self.entity, png)
        displayed = thumbnail_pixmap(png).toImage()
        self.assertEqual(displayed.size(), photo_size(320))
        self.assertEqual(displayed.pixelColor(160, 0), QColor(Qt.green))
        self.assertEqual(displayed.pixelColor(160, 179), QColor(Qt.green))
        stored = self.connection.execute("SELECT thumbnail_png FROM entities WHERE id=?", (self.entity,)).fetchone()[0]
        self.assertEqual(stored, png)

    def test_shared_photo_menu_cancellation_copy_and_removal(self):
        parent = QDialog()
        self.widgets.append(parent)
        changed = Mock()

        def trigger(title):
            def create(owner):
                menu = QMenu(owner)
                def execute():
                    try:
                        action = next(action for action in menu.actions() if action.text() == title)
                        action.trigger()
                    finally:
                        menu.close()
                QTimer.singleShot(0, execute)
                return menu
            return create

        with patch("videomanager.ui.entity_images.QMenu", side_effect=trigger("選擇圖片…")), \
                patch("videomanager.ui.entity_images.choose_entity_thumbnail", return_value=False):
            show_entity_thumbnail_menu(parent, self.connection, self.entity, QPoint(), changed)
        changed.assert_not_called()
        with patch("videomanager.ui.entity_images.QMenu", side_effect=trigger("複製照片")):
            show_entity_thumbnail_menu(parent, self.connection, self.entity, QPoint(), changed)
        self.assertEqual(QApplication.clipboard().image().size(), self.image.size())
        with patch("videomanager.ui.entity_images.QMenu", side_effect=trigger("移除照片")):
            show_entity_thumbnail_menu(parent, self.connection, self.entity, QPoint(), changed)
        changed.assert_called_once_with(self.entity)
        self.assertIsNone(self.connection.execute("SELECT thumbnail_png FROM entities WHERE id=?", (self.entity,)).fetchone()[0])
        show_entity_thumbnail_menu(parent, self.connection, "deleted-entity", QPoint(), changed)
