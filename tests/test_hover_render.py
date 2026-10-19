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
from PySide6.QtTest import QSignalSpy, QTest

from videomanager.services.thumbnail_service import cache_signature
from videomanager.ui.browser_view import BrowserView, VideoModel
from videomanager.ui.related_popup import RelatedVideosPopup


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

    def test_related_video_hover_uses_its_own_configurable_delay(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-related-hover-") as directory:
            model = VideoModel(Path(directory))
            model.set_rows([{"id": "video-1", "display_title": "影片一", "file_size": 1,
                             "mtime_ns": 1, "duration_ms": 1000, "missing_since": None,
                             "thumbnail_state": "done", "thumbnail_signature": cache_signature(1, 1, 10)}])
            browser = BrowserView(model, hover_delay_ms=2000, related_hover_delay_ms=35)
            browser.resize(440, 280)
            browser.show()
            requested = QSignalSpy(browser.related_hover_requested)
            ended = QSignalSpy(browser.related_hover_ended)
            try:
                self.app.processEvents()
                rect = browser.visualRect(model.index(0))
                QTest.mouseMove(browser.viewport(), rect.center())
                QTest.qWait(80)
                self.assertEqual(requested.count(), 1)
                self.assertEqual(requested.at(0)[0], "video-1")
                browser.stop_hover()
                self.assertEqual(ended.count(), 1)
                group_row = dict(model.rows[0], _group_cover=True, group_id="group-1",
                                 group_name="測試組", group_size=2, compressed_count=0)
                group_row["_group_members"] = [dict(model.rows[0])]
                model.set_rows([group_row])
                self.app.processEvents()
                browser._on_entered(model.index(0))
                QTest.qWait(80)
                self.assertEqual(requested.count(), 2)
                self.assertEqual(requested.at(1)[0], "video-1")
                browser.stop_hover()
            finally:
                browser.close()

    def test_related_popup_has_eight_preview_cards_and_full_results_action(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-related-cards-") as directory:
            cache = Path(directory)
            folder = cache / "video-0"
            folder.mkdir()
            for number, color in ((0, "#d72020"), (1, "#205ed7")):
                image = QImage(160, 90, QImage.Format_RGB32)
                image.fill(QColor(color))
                self.assertTrue(image.save(str(folder / f"{number:02d}.webp"), "WEBP"))
            popup = RelatedVideosPopup()
            activated = QSignalSpy(popup.video_activated)
            show_all = QSignalSpy(popup.show_all_requested)
            rows = [{"id": f"video-{n}", "display_title": f"相似片 {n}",
                     "shared_names": "人物甲"} for n in range(9)]
            popup.show_videos(rows, cache, "來源影片", "source-id")
            try:
                self.assertEqual(popup.content.count(), 8)
                card = popup.content.itemAt(0).widget()
                self.assertIn("來源影片", popup.heading.text())
                self.assertFalse(card.image.pixmap().isNull())
                card._next_frame()
                self.assertEqual(card.frame, 1)
                card.click()
                self.assertEqual(activated.at(0)[0], "video-0")
                self.assertFalse(popup.show_all_button.isHidden())
                popup.show_all_button.click()
                self.assertEqual(show_all.at(0)[0], "source-id")
            finally:
                popup.close()


if __name__ == "__main__":
    unittest.main()
