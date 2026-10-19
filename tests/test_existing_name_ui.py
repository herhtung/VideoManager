"""The existing-file scan confirms and applies names through the desktop flow."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from videomanager.config import AppPaths, Settings, save_settings
from videomanager.db.database import open_database
from videomanager.services.import_service import import_mp4, plan_group_import
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.services.video_group_service import create_video_group
from videomanager.ui.existing_name_dialog import ExistingNameDialog
from videomanager.ui.main_window import MainWindow


class ExistingNameUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_confirmed_existing_name_scan_updates_library_card(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-existing-ui-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            (root / "ABC - clip.mp4").write_bytes(b"disposable test MP4")
            paths = AppPaths(base / "data")
            marker = open_library(paths, root, create=True).marker
            save_settings(paths.settings, Settings(
                library_id=marker.library_id, last_root_path=str(root),
                import_remove_phrases=("ABC",),
            ))
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, marker.library_id, root, scan_library(root, marker.library_id))
            finally:
                connection.close()
            with patch.object(MainWindow, "start_scan"):
                window = MainWindow(paths)
                try:
                    with patch.object(ExistingNameDialog, "exec", return_value=QDialog.Accepted), \
                         patch.object(QMessageBox, "information"):
                        window.scan_existing_video_names()
                    self.assertTrue((root / "clip.mp4").exists())
                    self.assertEqual(window.connection.execute("SELECT file_name FROM videos").fetchone()[0],
                                     "clip.mp4")
                    self.assertEqual(window.model.rows[0]["display_title"], "clip")
                finally:
                    window.close()

    def test_new_group_uses_cleaned_first_part_name(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-clean-group-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            source = base / "outside"
            source.mkdir()
            files = [source / "第一段 - 廣告.mp4", source / "第二段.mp4"]
            for path in files:
                path.write_bytes(b"disposable test MP4")
            paths = AppPaths(base / "data")
            marker = open_library(paths, root, create=True).marker
            items = plan_group_import(root, marker.library_id, files, remove_phrases=("廣告",))
            completed = [str(import_mp4(item, root, marker.library_id).destination) for item in items]
            with patch.object(MainWindow, "start_scan"), patch.object(QMessageBox, "information"):
                window = MainWindow(paths)
                try:
                    window._group_import_items = items
                    window._import_result = {
                        "completed": completed, "retained": [], "failed": [], "cancelled": False,
                    }
                    window._on_import_finished()
                    group = window.connection.execute("SELECT name, folder_rel_path FROM video_groups").fetchone()
                    self.assertIsNotNone(group)
                    self.assertEqual(group["name"], "第一段")
                    self.assertEqual(group["name"], items[0].destination.stem)
                finally:
                    window.close()

    def test_group_only_scan_updates_existing_group_card(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-existing-group-ui-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            for name in ("first.mp4", "second.mp4"):
                (root / name).write_bytes(b"disposable test MP4")
            paths = AppPaths(base / "data")
            marker = open_library(paths, root, create=True).marker
            save_settings(paths.settings, Settings(
                library_id=marker.library_id, last_root_path=str(root),
                import_remove_phrases=("廣告",),
            ))
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, marker.library_id, root, scan_library(root, marker.library_id))
                ids = [row[0] for row in connection.execute("SELECT id FROM videos ORDER BY file_name")]
                create_video_group(connection, marker.library_id, "第一段 - 廣告", ids)
            finally:
                connection.close()
            with patch.object(MainWindow, "start_scan"):
                window = MainWindow(paths)
                try:
                    window.group_section_button.click()
                    with patch.object(ExistingNameDialog, "exec", return_value=QDialog.Accepted), \
                         patch.object(QMessageBox, "information"):
                        window.scan_existing_video_names()
                    self.assertEqual(window.connection.execute("SELECT name FROM video_groups").fetchone()[0],
                                     "第一段")
                    self.assertEqual(window.model.rows[0]["group_name"], "第一段")
                finally:
                    window.close()


if __name__ == "__main__":
    unittest.main()
