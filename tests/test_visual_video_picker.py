"""Selected videos stay visible and group members can be chosen by cover image."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication, QListView

from videomanager.config import AppPaths
from videomanager.db.database import open_database
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.ui.group_members_dialog import GroupMembersDialog
from videomanager.ui.main_window import MainWindow


class VisualVideoPickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_group_picker_uses_covers_and_preserves_selection_while_searching(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-picker-") as directory:
            cache = Path(directory)
            image = cache / "a" / "00.webp"
            image.parent.mkdir()
            pixmap = QPixmap(160, 90)
            pixmap.fill(QColor("#234c64"))
            self.assertTrue(pixmap.save(str(image), "WEBP"))
            rows = [
                {"id": "a", "display_title": "甲影片", "relative_path": "甲.mp4"},
                {"id": "b", "display_title": "乙影片", "relative_path": "乙.mp4"},
            ]
            dialog = GroupMembersDialog(rows, cache)
            try:
                self.assertEqual(dialog.items.viewMode(), QListView.IconMode)
                self.assertFalse(dialog.items.item(0).icon().isNull())
                self.assertFalse(dialog.items.item(0).icon().pixmap(176, 100).isNull())
                dialog.items.item(0).setSelected(True)
                self.assertEqual(dialog.selected_ids(), ["a"])
                dialog.search.setText("乙")
                dialog.items.item(1).setSelected(True)
                self.assertEqual(set(dialog.selected_ids()), {"a", "b"})
                self.assertEqual(dialog.count.text(), "已選 2 段")
                self.assertTrue(dialog.add_button.isEnabled())
            finally:
                dialog.close()

    def test_compress_selected_card_needs_no_second_video_choice(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-picker-main-") as directory:
            base = Path(directory)
            library = base / "library"
            library.mkdir()
            for name in ("one.mp4", "two.mp4"):
                (library / name).write_bytes(b"disposable index entry")
            paths = AppPaths(base / "data")
            library_id = open_library(paths, library, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, library_id, library, scan_library(library, library_id))
            finally:
                connection.close()
            with patch.object(MainWindow, "start_scan"):
                window = MainWindow(paths)
            try:
                window.show()
                self.app.processEvents()
                target = window.model.rows[1]["id"]
                window.browser.setCurrentIndex(window.model.index(1))
                self.app.processEvents()
                self.assertTrue(window.compress_button.isEnabled())
                window.compress_button.click()
                center = window.compression_center
                self.assertIsNotNone(center)
                self.assertEqual([item.data(Qt.UserRole) for item in center.videos.selectedItems()], [target])
                self.assertTrue(center.only_selected.isChecked())
                self.assertEqual(sum(not center.videos.item(i).isHidden()
                                     for i in range(center.videos.count())), 1)
                self.assertLess(center.videos.visualItemRect(center.videos.selectedItems()[0]).top(), 160)
                other = window.model.rows[0]["id"]
                window.open_compression_center([other])
                self.assertEqual([item.data(Qt.UserRole) for item in center.videos.selectedItems()], [other])
                self.assertEqual(sum(not center.videos.item(i).isHidden()
                                     for i in range(center.videos.count())), 1)
                center.cap_thread.wait(30000)
                self.app.processEvents()
                center.close()
            finally:
                window.close()
