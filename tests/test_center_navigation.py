"""Embedded tool pages must never leave an empty stacked page after Escape."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from videomanager.config import AppPaths
from videomanager.ui.main_window import MainWindow


class CenterNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_escape_returns_from_both_centers_and_reopen_keeps_pages(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-center-nav-") as directory:
            with patch.object(MainWindow, "start_scan"):
                window = MainWindow(AppPaths(Path(directory)))
            try:
                window.show()
                for opener, attribute in ((window.open_compression_center, "compression_center"),
                                          (window.open_edit_center, "edit_center")):
                    opener()
                    center = getattr(window, attribute)
                    center.cap_thread.wait(30000)
                    self.app.processEvents()
                    self.assertIs(window.main_stack.currentWidget(), center)
                    QTest.keyClick(center, Qt.Key_Escape)
                    self.app.processEvents()
                    self.assertIs(window.main_stack.currentWidget(), window.browser_page)
                    self.assertIsNotNone(getattr(window, attribute))
                    opener()
                    self.app.processEvents()
                    self.assertIs(window.main_stack.currentWidget(), center)
                    if attribute == "compression_center":
                        center.show_history()
                        self.assertEqual(center.workspace.currentIndex(), 1)
                    window.show_library_page()
                    if attribute == "compression_center":
                        window.open_compression_center()
                        self.assertEqual(center.workspace.currentIndex(), 0)
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
