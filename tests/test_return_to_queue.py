"""Finished videos can be sent back to triage without losing their metadata."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication

from videomanager.config import AppPaths
from videomanager.db import repository as repo
from videomanager.db.database import open_database
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.ui.main_window import MainWindow


class ReturnToQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_single_and_multi_return_preserve_tags_and_context_selection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-return-queue-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            for name in ("one.mp4", "two.mp4"):
                (root / name).write_bytes(b"disposable test video")
            paths = AppPaths(base / "data")
            marker = open_library(paths, root, create=True).marker
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, marker.library_id, root, scan_library(root, marker.library_id))
                ids = [row["id"] for row in repo.list_videos(connection, marker.library_id, {})]
                tag_group = repo.ensure_default_tag_group(connection)
                tag_id = repo.create_tag(connection, tag_group, "保留標籤")
                for video_id in ids:
                    repo.update_video(connection, video_id, status="done", rating=4, notes="保留備註")
                    repo.set_video_relations(connection, video_id, tag_ids={tag_id})
            finally:
                connection.close()

            with patch.object(MainWindow, "start_scan"):
                window = MainWindow(paths)
                try:
                    window.show()
                    self.app.processEvents()
                    first = window.model.index(0)
                    window.browser.setCurrentIndex(first)
                    window.details.set_video(ids[0])
                    self.assertTrue(window.details.return_button.isVisible())
                    window.details.return_button.click()
                    self.assertEqual(repo.video_by_id(window.connection, ids[0])["status"], "unprocessed")
                    self.assertEqual(repo.video_tag_ids(window.connection, ids[0]), {tag_id})

                    repo.update_video(window.connection, ids[0], status="done")
                    window.refresh_videos()
                    selection = window.browser.selectionModel()
                    for row in range(2):
                        selection.select(window.model.index(row), QItemSelectionModel.Select)
                    labels = [action.text() for action in window._build_video_context_menu(ids).actions()]
                    self.assertIn("將 2 部退回待整理", labels)
                    self.assertIn("建立為同一組…", labels)
                    self.assertEqual(len(window._selected_video_ids()), 2)
                    class FakeMenu:
                        popped = False

                        def setAttribute(self, *_):
                            pass

                        def popup(self, *_):
                            self.popped = True

                    fake_menu = FakeMenu()
                    with patch.object(window, "_build_video_context_menu", return_value=fake_menu):
                        point = window.browser.visualRect(window.model.index(0)).center()
                        self.assertTrue(window.browser.indexAt(point).isValid(),
                                        f"invalid context target: {point}")
                        event = QContextMenuEvent(QContextMenuEvent.Mouse, point,
                                                  window.browser.viewport().mapToGlobal(point))
                        QApplication.sendEvent(window.browser.viewport(), event)
                    self.assertTrue(fake_menu.popped)
                    self.assertEqual(len(window._selected_video_ids()), 2)

                    window.return_selected_to_unprocessed()
                    for video_id in ids:
                        row = repo.video_by_id(window.connection, video_id)
                        self.assertEqual(row["status"], "unprocessed")
                        self.assertEqual(row["rating"], 4)
                        self.assertEqual(row["notes"], "保留備註")
                        self.assertEqual(repo.video_tag_ids(window.connection, video_id), {tag_id})
                finally:
                    window.close()


if __name__ == "__main__":
    unittest.main()
