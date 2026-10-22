import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QBuffer, QCoreApplication, QEvent, QIODevice, QMimeData, QPoint, QPointF, Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage
from PySide6.QtWidgets import QApplication, QDialog
from videomanager.config import AppPaths
from videomanager.db import repository as repo
from videomanager.db.database import open_database
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.ui.details_panel import DetailsPanel
from videomanager.ui.entity_images import choose_entity_thumbnail
from videomanager.ui.metadata_dialog import MetadataDialog
from videomanager.ui.relation_button import ENTITY_DRAG_MIME, RELATION_TYPE_DRAG_MIME


class RelationDragTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.root = self.base / "library"
        self.root.mkdir()
        (self.root / "test.mp4").write_bytes(b"test fixture")
        paths = AppPaths(self.base / "data")
        self.library = open_library(paths, self.root, create=True).marker.library_id
        self.connection = open_database(paths.database, paths.backups)
        synchronize(self.connection, self.library, self.root, scan_library(self.root, self.library))
        self.video = self.connection.execute("SELECT id FROM videos").fetchone()[0]
        self.first = repo.create_entity(self.connection, "person", "人物甲")
        self.second = repo.create_entity(self.connection, "person", "人物乙")
        self.studio = repo.create_entity(self.connection, "studio", "片商甲")
        with self.connection:
            self.connection.execute("INSERT INTO video_entities VALUES (?,?)", (self.video, self.first))
        image = QImage(192, 192, QImage.Format_RGB32)
        image.fill(Qt.blue)
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, "PNG")
        self.png = bytes(buffer.data())
        repo.set_entity_thumbnail(self.connection, self.first, self.png)
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.connection.close()
        self.directory.cleanup()

    def drop(self, button, mime_type, identifier, *, after=False):
        mime = QMimeData()
        mime.setData(mime_type, identifier.encode())
        point = QPoint(button.width() * (3 if after else 1) // 4,
                       button.height() * (3 if after else 1) // 4)
        enter = QDragEnterEvent(point, Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
        drop = QDropEvent(QPointF(point), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(button, enter)
        self.assertTrue(enter.isAccepted())
        QApplication.sendEvent(button, drop)
        self.assertTrue(drop.isAccepted())
        self.app.processEvents()

    def test_manager_drag_move_category_and_remove_photo_keep_video_links(self):
        manager = MetadataDialog(self.connection)
        self.widgets.append(manager)
        listing = manager.entity_lists["person"]
        self.assertEqual(listing.itemWidget(listing.item(0)).iconSize().width(), 112)
        self.drop(listing.itemWidget(listing.item(1)), ENTITY_DRAG_MIME, self.first, after=True)
        self.assertEqual([row["id"] for row in repo.list_entities(self.connection, "person")], [self.second, self.first])
        self.drop(manager.entity_headers["studio"], ENTITY_DRAG_MIME, self.first)
        moved = self.connection.execute("SELECT * FROM entities WHERE id=?", (self.first,)).fetchone()
        self.assertEqual(moved["type"], "studio")
        self.assertEqual(moved["thumbnail_png"], self.png)
        self.drop(manager.entity_headers["studio"], RELATION_TYPE_DRAG_MIME, "person", after=True)
        self.assertEqual([row["id"] for row in repo.list_relation_types(self.connection)], ["studio", "person"])
        manager.refresh_entities(self.first)
        self.assertTrue(manager.entity_image_remove_buttons["studio"].isEnabled())
        manager.entity_image_remove_buttons["studio"].click()
        self.assertIsNone(self.connection.execute("SELECT thumbnail_png FROM entities WHERE id=?", (self.first,)).fetchone()[0])
        self.assertFalse(manager.entity_image_remove_buttons["studio"].isEnabled())
        self.assertEqual(repo.video_entity_ids(self.connection, self.video), {self.first})
        self.assertTrue((self.root / "test.mp4").exists())

    def test_editor_drag_updates_both_categories_and_preserves_selection(self):
        panel = DetailsPanel(self.connection)
        self.widgets.append(panel)
        panel.set_library(self.root, self.library)
        panel.set_video(self.video)
        self.assertEqual(panel.people._buttons[self.first].iconSize().width(), 96)
        self.drop(panel.studios._buttons[self.studio], ENTITY_DRAG_MIME, self.first)
        self.assertNotIn(self.first, panel.people._buttons)
        self.assertIn(self.first, panel.studios._buttons)
        self.assertEqual(panel.studios.selected_ids(), {self.first})
        self.assertFalse(panel.is_dirty())
        panel.studios._remove_thumbnail(self.first)
        self.assertTrue(panel.studios._buttons[self.first].icon().isNull())
        self.assertEqual(repo.video_entity_ids(self.connection, self.video), {self.first})

    def test_duplicate_name_rejects_cross_category_move_without_partial_changes(self):
        duplicate = repo.create_entity(self.connection, "studio", "人物甲")
        with self.assertRaises(ValueError):
            repo.reorder_entity(self.connection, self.first, duplicate)
        row = self.connection.execute("SELECT type,thumbnail_png FROM entities WHERE id=?", (self.first,)).fetchone()
        self.assertEqual(row["type"], "person")
        self.assertEqual(row["thumbnail_png"], self.png)
        self.assertEqual(repo.video_entity_ids(self.connection, self.video), {self.first})

    def test_new_photos_keep_larger_resolution(self):
        source = self.base / "portrait.png"
        image = QImage(640, 640, QImage.Format_RGB32)
        image.fill(Qt.green)
        self.assertTrue(image.save(str(source)))
        manager = MetadataDialog(self.connection)
        self.widgets.append(manager)
        with patch("videomanager.ui.entity_images.QFileDialog.getOpenFileName", return_value=(str(source), "")), \
                patch("videomanager.ui.entity_images.ImageCropDialog.exec", return_value=QDialog.Accepted):
            self.assertTrue(choose_entity_thumbnail(manager, self.connection, self.first))
        png = self.connection.execute("SELECT thumbnail_png FROM entities WHERE id=?", (self.first,)).fetchone()[0]
        output = QImage.fromData(png)
        self.assertEqual((output.width(), output.height()), (320, 180))
