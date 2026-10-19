"""Normal window size follows the monitor it is moved to."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from videomanager.config import AppPaths
from videomanager.ui.main_window import MainWindow


class _Screen:
    def availableGeometry(self):
        return QRect(1920, 0, 1024, 768)


class _Handle:
    def screen(self):
        return _Screen()


class WindowSizingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_normal_window_clamps_to_smaller_second_screen(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-screen-") as directory:
            with patch.object(MainWindow, "start_scan"):
                window = MainWindow(AppPaths(Path(directory)))
            try:
                window.show()
                self.app.processEvents()
                window.resize(1500, 900)
                window.move(2100, 100)
                with patch.object(MainWindow, "windowHandle", return_value=_Handle()):
                    window._fit_to_current_screen()
                    self.assertTrue(window._compact_toolbar)
                    self.assertLessEqual(window.frameGeometry().width(), 1024)
                    self.assertLessEqual(window.frameGeometry().height(), 768)
                    self.assertGreaterEqual(window.frameGeometry().left(), 1920)
            finally:
                window.close()

    def test_main_work_progress_remains_visible_on_small_window(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-task-strip-") as directory:
            with patch.object(MainWindow, "start_scan"):
                window = MainWindow(AppPaths(Path(directory)))
            try:
                window.resize(920, 650)
                window.show()
                self.app.processEvents()
                window._on_import_bytes(2, 4, 5 * 2**20, 10 * 2**20)
                self.assertTrue(window.library_task_strip.isVisible())
                self.assertIn("2/4", window.library_task_label.text())
                self.assertGreater(window.library_task_progress.value(), 0)
                window._on_index_progress(3, 6)
                self.assertIn("縮圖：3/6", window.library_task_label.text())
                self.assertEqual(window.library_task_progress.value(), 3)
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
