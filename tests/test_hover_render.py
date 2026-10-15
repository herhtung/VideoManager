"""Verify that preview frames replace the thumbnail within the same card."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent
from PySide6.QtWidgets import QApplication

from videomanager.services.thumbnail_service import cache_signature
from videomanager.ui.browser_view import BrowserView, VideoModel


class HoverRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_preview_replaces_top_and_bottom_of_original_image(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-hover-") as directory:
            cache = Path(directory)
            folder = cache / "test-video"
            folder.mkdir()
            for number, color in ((0, "#d72020"), (1, "#205ed7")):
                image = QImage(320, 180, QImage.Format_RGB32)
                image.fill(QColor(color))
                self.assertTrue(image.save(str(folder / f"{number:02d}.webp"), "WEBP"))
            model = VideoModel(cache)
            model.set_rows([{
                "id": "test-video", "display_title": "測試影片", "file_size": 1,
                "mtime_ns": 1, "duration_ms": 1000, "missing_since": None,
                "thumbnail_state": "done", "thumbnail_signature": cache_signature(1, 1, 10),
            }])
            browser = BrowserView(model)
            browser.resize(440, 280)
            browser.show()
            try:
                self.app.processEvents()
                rect = browser.visualRect(model.index(0))
                x = rect.x() + rect.width() // 2
                top = rect.y() + 7 + 20
                bottom = rect.y() + 7 + 80

                def colors() -> tuple[QColor, QColor]:
                    screenshot = browser.viewport().grab().toImage()
                    return screenshot.pixelColor(x, top), screenshot.pixelColor(x, bottom)

                first = colors()
                self.assertTrue(all(c.red() > c.blue() + 80 for c in first))
                model.show_frame(0, 1)
                self.app.processEvents()
                second = colors()
                self.assertTrue(all(c.blue() > c.red() + 80 for c in second))
                browser.stop_hover()
                self.app.processEvents()
                restored = colors()
                self.assertTrue(all(c.red() > c.blue() + 80 for c in restored))
                browser._on_entered(model.index(0))
                browser._start_hover()
                browser._next_frame()
                self.assertTrue(browser._cycle.isActive())
                blank = QMouseEvent(QEvent.MouseMove, QPointF(400, 260), QPointF(400, 260),
                                    Qt.NoButton, Qt.NoButton, Qt.NoModifier)
                QApplication.sendEvent(browser.viewport(), blank)
                self.assertEqual(browser._hover_candidate, -1)
                self.assertFalse(browser._cycle.isActive())
                self.assertEqual(model.hover_row, -1)
                browser._on_entered(model.index(0))
                browser._start_hover()
                QApplication.sendEvent(browser.viewport(), QEvent(QEvent.Leave))
                self.assertFalse(browser._cycle.isActive())
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
