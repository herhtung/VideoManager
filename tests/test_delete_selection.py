"""The visible delete action confirms and targets every selected card."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QApplication, QMessageBox

from videomanager.config import AppPaths
from videomanager.compression.profiles import Profile
from videomanager.compression.repository import queue_video, update_job
from videomanager.db.database import open_database
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.services.video_group_service import create_video_group
from videomanager.ui.main_window import MainWindow


class DeleteSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_confirmation_precedes_recycling_all_selected_videos(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-delete-selection-") as directory:
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
            finally:
                connection.close()
            with patch.object(MainWindow, "start_scan"):
                window = MainWindow(paths)
            try:
                self.assertEqual(window.model.rowCount(), 2)
                selection = window.browser.selectionModel()
                for row in range(2):
                    selection.select(window.model.index(row), QItemSelectionModel.Select)
                self.assertTrue(window.recycle_button.isEnabled())
                self.assertEqual(window.recycle_button.text(), "刪除選取（2）")
                self.assertFalse(hasattr(window.details, "recycle_button"))
                selected = window._selected_video_ids()
                with patch.object(QMessageBox, "exec", side_effect=[QMessageBox.No, QMessageBox.Yes]), \
                     patch("videomanager.ui.main_window.trash_video") as recycle:
                    window.recycle_button.click()
                    recycle.assert_not_called()
                    window.recycle_button.click()
                    self.assertEqual([call.args[3] for call in recycle.call_args_list], selected)
            finally:
                window.close()

    def test_other_video_work_does_not_block_batch_delete_and_waiting_job_is_cancelled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-delete-per-video-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            for name in ("active.mp4", "waiting.mp4", "free.mp4"):
                (root / name).write_bytes(b"disposable test video")
            paths = AppPaths(base / "data")
            marker = open_library(paths, root, create=True).marker
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, marker.library_id, root, scan_library(root, marker.library_id))
            finally:
                connection.close()
            with patch.object(MainWindow, "start_scan"):
                window = MainWindow(paths)
            try:
                ids = {row["file_name"]: row["id"] for row in window.connection.execute(
                    "SELECT id,file_name FROM videos"
                )}
                active_job = queue_video(window.connection, root, marker.library_id,
                                         ids["active.mp4"], Profile())
                update_job(window.connection, active_job, status="analyzing")
                waiting_job = queue_video(window.connection, root, marker.library_id,
                                          ids["waiting.mp4"], Profile())
                selection = window.browser.selectionModel()
                for row in range(window.model.rowCount()):
                    selection.select(window.model.index(row), QItemSelectionModel.Select)
                with patch.object(QMessageBox, "warning") as warning, \
                     patch.object(QMessageBox, "exec", return_value=QMessageBox.Yes):
                    window.recycle_selected()
                self.assertIn("active", warning.call_args.args[2])
                self.assertTrue((root / "active.mp4").is_file())
                self.assertFalse((root / "waiting.mp4").exists())
                self.assertFalse((root / "free.mp4").exists())
                self.assertEqual(window.connection.execute(
                    "SELECT status FROM compression_jobs WHERE id=?", (waiting_job,)
                ).fetchone()[0], "cancelled")
                self.assertEqual(window.connection.execute(
                    "SELECT status FROM compression_jobs WHERE id=?", (active_job,)
                ).fetchone()[0], "analyzing")
            finally:
                window.close()

    def test_outer_group_card_delete_moves_every_part_to_app_trash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-delete-group-card-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            for name in ("part-one.mp4", "part-two.mp4", "unrelated.mp4"):
                (root / name).write_bytes(b"disposable test video")
            paths = AppPaths(base / "data")
            marker = open_library(paths, root, create=True).marker
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, marker.library_id, root, scan_library(root, marker.library_id))
                ids = {row["file_name"]: row["id"] for row in connection.execute(
                    "SELECT id,file_name FROM videos"
                )}
                create_video_group(connection, marker.library_id, "test group",
                                   [ids["part-one.mp4"], ids["part-two.mp4"]])
            finally:
                connection.close()
            with patch.object(MainWindow, "start_scan"):
                window = MainWindow(paths)
            try:
                window.group_section_button.click()
                cover_row = next(index for index, row in enumerate(window.model.rows)
                                 if row.get("_group_cover"))
                window.browser.selectionModel().select(
                    window.model.index(cover_row), QItemSelectionModel.ClearAndSelect
                )
                self.assertTrue(window.recycle_button.isEnabled())
                self.assertEqual(window.recycle_button.text(), "刪除整組（2 段）")
                self.assertEqual(set(window._selected_video_ids()),
                                 {ids["part-one.mp4"], ids["part-two.mp4"]})
                with patch.object(QMessageBox, "exec", return_value=QMessageBox.Yes):
                    window.recycle_button.click()
                self.assertFalse((root / "part-one.mp4").exists())
                self.assertFalse((root / "part-two.mp4").exists())
                self.assertTrue((root / "unrelated.mp4").exists())
                self.assertEqual(window.connection.execute(
                    "SELECT COUNT(*) FROM trash_entries WHERE video_id IN (?,?)",
                    (ids["part-one.mp4"], ids["part-two.mp4"]),
                ).fetchone()[0], 2)
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
