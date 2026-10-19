"""Group cover and parts edit the same metadata without merging part filenames."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from videomanager.config import AppPaths
from videomanager.db import repository as repo
from videomanager.db.database import open_database
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.services.video_group_service import create_video_group
from videomanager.ui.filter_dialog import FilterDialog
from videomanager.ui.main_window import MainWindow


class SharedGroupDetailsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_group_cover_and_part_save_shared_fields_and_keep_part_titles(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-shared-details-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            for name in ("part-one.mp4", "part-two.mp4"):
                (root / name).write_bytes(b"disposable test video")
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                rows = connection.execute(
                    "SELECT id, display_title FROM videos ORDER BY file_name",
                ).fetchall()
                ids = [row["id"] for row in rows]
                original_titles = [row["display_title"] for row in rows]
                group_id = create_video_group(connection, library_id, "原組名", ids)
                tag_group = repo.create_tag_group(connection, "類型")
                first_tag = repo.create_tag(connection, tag_group, "甲")
                second_tag = repo.create_tag(connection, tag_group, "乙")
            finally:
                connection.close()

            with patch.object(MainWindow, "start_scan"):
                window = MainWindow(paths)
            try:
                window.show()
                self.app.processEvents()
                window.search.setText("part-one")
                window.refresh_videos()
                self.assertEqual(window.model.rowCount(), 1)
                window.open_group_folder(group_id)
                self.assertEqual(window.model.rowCount(), 2)
                self.assertEqual(window.search.text(), "")
                window.close_group_folder()
                self.assertEqual(window.search.text(), "part-one")
                window.clear_filters()
                cover_index = next(i for i, row in enumerate(window.model.rows) if row.get("_group_cover"))
                window.browser.setCurrentIndex(window.model.index(cover_index))
                self.app.processEvents()
                self.assertTrue(window.details.isVisible())
                self.assertEqual(window.details.group_id, group_id)
                self.assertEqual(window.details.title.text(), "原組名")

                window.details.title.setText("更新後的組名")
                window.details.rating.setValue(4)
                window.details.favorite.setChecked(True)
                window.details.notes.setPlainText("整組備註")
                window.details.tags.set_selected({first_tag})
                self.assertTrue(window.details.save_current())
                self.assertEqual(window.model.rows[cover_index]["group_name"], "更新後的組名")
                for identifier, original_title in zip(ids, original_titles):
                    video = repo.video_by_id(window.connection, identifier)
                    self.assertEqual(video["display_title"], original_title)
                    self.assertEqual(video["rating"], 4)
                    self.assertEqual(video["notes"], "整組備註")
                    self.assertEqual(video["is_favorite"], 1)
                    self.assertEqual(repo.video_tag_ids(window.connection, identifier), {first_tag})

                window.open_group_folder(group_id)
                self.app.processEvents()
                self.assertTrue(window.details.isVisible())
                self.assertIsNone(window.details.video_id)
                self.assertFalse(window.details.isEnabled())
                self.assertEqual(window.details.title.text(), "")
                part_index = next(i for i, row in enumerate(window.model.rows) if row["id"] == ids[1])
                window.browser.setCurrentIndex(window.model.index(part_index))
                self.assertTrue(window.details.isEnabled())
                blank = QPoint(window.browser.viewport().width() - 5, window.browser.viewport().height() - 5)
                self.assertFalse(window.browser.indexAt(blank).isValid())
                QTest.mouseClick(window.browser.viewport(), Qt.LeftButton, pos=blank)
                self.app.processEvents()
                self.assertIsNone(window.details.video_id)
                self.assertFalse(window.details.isEnabled())
                self.assertEqual(window.details.title.text(), "")
                window.browser.setCurrentIndex(window.model.index(part_index))
                window.details.title.setText("第二段新名稱")
                window.details.rating.setValue(5)
                window.details.tags.set_selected({second_tag})
                self.assertTrue(window.details.save_current())
                self.assertEqual(repo.video_by_id(window.connection, ids[0])["display_title"], original_titles[0])
                self.assertEqual(repo.video_by_id(window.connection, ids[1])["display_title"], "第二段新名稱")
                self.assertEqual(window.connection.execute(
                    "SELECT name FROM video_groups WHERE id = ?", (group_id,),
                ).fetchone()[0], "更新後的組名")
                for identifier in ids:
                    self.assertEqual(repo.video_by_id(window.connection, identifier)["rating"], 5)
                    self.assertEqual(repo.video_tag_ids(window.connection, identifier), {second_tag})

                window.open_player(ids[1])
                self.app.processEvents()
                player = window.player
                self.assertIsNotNone(player)
                self.assertFalse(window.menuBar().isVisible())
                self.assertFalse(window.statusBar().isVisible())
                player.toggle_fullscreen()
                self.app.processEvents()
                player.exit_fullscreen()
                self.app.processEvents()
                self.assertIsNone(player.details)
                player.toggle_details()
                self.assertIsNotNone(player.details)
                player.details.rating.setValue(3)
                player.details.tags.set_selected({first_tag})
                player.close()
                self.app.processEvents()
                self.assertTrue(window.menuBar().isVisible())
                self.assertTrue(window.statusBar().isVisible())
                window.showNormal()
                window.open_player(ids[0])
                self.app.processEvents()
                window.player.toggle_fullscreen()
                self.app.processEvents()
                window.player.close()
                self.app.processEvents()
                self.assertTrue(window.isMaximized())
                self.assertEqual(window.details.rating.value(), 3)
                self.assertEqual(window.details.tags.selected_ids(), {first_tag})
                for identifier in ids:
                    self.assertEqual(repo.video_by_id(window.connection, identifier)["rating"], 3)
                    self.assertEqual(repo.video_tag_ids(window.connection, identifier), {first_tag})

                window.details.finish_current()
                self.assertTrue(all(repo.video_by_id(window.connection, identifier)["status"] == "done"
                                    for identifier in ids))
                window.return_selected_to_unprocessed()
                self.assertTrue(all(repo.video_by_id(window.connection, identifier)["status"] == "unprocessed"
                                    for identifier in ids))

                filters = FilterDialog(window.connection, {"rating_min": 5, "tag_all": [second_tag]}, window)
                filters.clear_all()
                self.assertEqual(filters.filters(), {})
                filters.close()

                window.search.setText("沒有這部影片")
                window.search.returnPressed.emit()
                self.assertEqual(window.model.rowCount(), 0)
                window.clear_filters()
                self.assertEqual(window._nav_key(), f"group:{group_id}")
                self.assertEqual(window.search.text(), "")
                self.assertFalse(window.clear_filter_button.isEnabled())
                self.assertEqual(window.model.rowCount(), 2)
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
