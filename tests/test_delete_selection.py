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
from videomanager.db.database import open_database
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
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
                     patch("videomanager.ui.main_window.recycle_video") as recycle:
                    window.recycle_button.click()
                    recycle.assert_not_called()
                    window.recycle_button.click()
                    self.assertEqual([call.args[3] for call in recycle.call_args_list], selected)
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
