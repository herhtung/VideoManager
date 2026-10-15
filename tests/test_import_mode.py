"""Dragging MP4s follows the visible import mode."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtWidgets import QApplication, QFileDialog

from videomanager.config import AppPaths
from videomanager.ui.main_window import MainWindow


class _Drop:
    def __init__(self, paths: list[Path]) -> None:
        self.mime = QMimeData()
        self.mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        self.accepted = False

    def mimeData(self) -> QMimeData:
        return self.mime

    def acceptProposedAction(self) -> None:
        self.accepted = True


class ImportModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_multiple_files_and_folder_follow_visible_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-import-mode-") as directory:
            root = Path(directory)
            sources = root / "sources"
            sources.mkdir()
            first, second = sources / "part1.mp4", sources / "part2.mp4"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            window = MainWindow(AppPaths(root / "app-data"))
            try:
                self.assertTrue(window.separate_mode_button.isChecked())
                with patch.object(window, "_begin_import") as begin:
                    files = _Drop([first, second])
                    window.dropEvent(files)
                    self.assertTrue(files.accepted)
                    begin.assert_called_once_with([first, second], group=False)
                    begin.reset_mock()

                    folder = _Drop([sources])
                    window.dropEvent(folder)
                    self.assertTrue(folder.accepted)
                    begin.assert_called_once_with(sources, group=False)
                    begin.reset_mock()

                    window.group_mode_button.click()
                    self.assertTrue(window.group_mode_button.isChecked())
                    self.assertEqual(window.import_button.text(), "選分段 MP4")
                    files = _Drop([first, second])
                    window.dropEvent(files)
                    begin.assert_called_once_with([first, second], group=True)
                    begin.reset_mock()

                    folder = _Drop([sources])
                    window.dropEvent(folder)
                    begin.assert_called_once_with(sources, group=True)
            finally:
                window.close()

    def test_file_picker_uses_current_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-import-picker-") as directory:
            root = Path(directory)
            first, second = root / "one.mp4", root / "two.mp4"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            window = MainWindow(AppPaths(root / "app-data"))
            window.root = root
            window.library_id = "test-library"
            try:
                with patch.object(QFileDialog, "getOpenFileNames", return_value=([str(first), str(second)], "")), \
                     patch.object(window, "_begin_import") as begin:
                    window.import_files()
                    begin.assert_called_once_with([first, second], group=False)
                    begin.reset_mock()
                    window.group_mode_button.click()
                    window.import_files()
                    begin.assert_called_once_with([first, second], group=True)
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
