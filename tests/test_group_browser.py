"""A video group uses one playable folder card until its segments are opened."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from videomanager.ui.browser_view import BrowserView, VideoModel
from videomanager.ui.main_window import compact_browser_rows


def video(identifier: str, group_position: int | None = None) -> dict:
    return {
        "id": identifier, "display_title": identifier, "relative_path": f"{identifier}.mp4",
        "file_size": 100, "mtime_ns": 1, "duration_ms": 1000,
        "missing_since": None, "thumbnail_state": "done", "thumbnail_signature": "old",
        "group_id": "group" if group_position else None,
        "group_name": "測試影片組" if group_position else None,
        "group_position": group_position, "group_size": 2 if group_position else None,
    }


class GroupBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_folder_card_is_compact_playable_and_opens_segments(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-browser-group-") as directory:
            cache = Path(directory)
            for identifier, color in (("first", "#df3030"), ("second", "#305fdf")):
                folder = cache / identifier
                folder.mkdir()
                image = QImage(320, 180, QImage.Format_RGB32)
                image.fill(QColor(color))
                self.assertTrue(image.save(str(folder / "00.webp"), "WEBP"))
            source = [video("second", 2), video("single"), video("first", 1)]
            rows = compact_browser_rows(source)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["id"], "single")
            self.assertTrue(rows[1]["_group_cover"])
            self.assertEqual([member["id"] for member in rows[1]["_group_members"]], ["first", "second"])
            model = VideoModel(cache)
            browser = BrowserView(model)
            browser.resize(610, 440)
            browser.show()
            try:
                model.set_rows(rows)
                QTest.qWait(30)
                self.app.processEvents()
                single_rect = browser.visualRect(model.index(0))
                group_rect = browser.visualRect(model.index(1))
                self.assertGreater(group_rect.y(), single_rect.y())
                self.assertEqual([title for title, _ in browser._headers], ["單部影片", "影片組 · 點開查看分段"])
                rendered = browser.viewport().grab().toImage()
                cover_top = group_rect.y() + 70
                red = rendered.pixelColor(group_rect.x() + 50, cover_top)
                blue = rendered.pixelColor(group_rect.x() + 145, cover_top)
                self.assertGreater(red.red(), red.blue() + 70)
                self.assertGreater(blue.blue(), blue.red() + 70)
                played: list[str] = []
                opened: list[str] = []
                browser.group_play_requested.connect(played.append)
                browser.group_open_requested.connect(opened.append)
                title_y = group_rect.y() + 7 + round(browser._thumbnail_width * 104 / 180) + 6
                play, open_parts = browser.itemDelegate().group_action_rects(
                    group_rect, title_y, False, browser._thumbnail_width,
                )
                QTest.mouseClick(browser.viewport(), Qt.LeftButton, pos=play.center())
                QTest.mouseClick(browser.viewport(), Qt.LeftButton, pos=open_parts.center())
                self.assertEqual(played, ["group"])
                self.assertEqual(opened, ["group"])
                browser.set_list_mode(True)
                self.app.processEvents()
                list_rect = browser.visualRect(model.index(1))
                _, list_open = browser.itemDelegate().group_action_rects(
                    list_rect, list_rect.y() + 15, True, browser._thumbnail_width,
                )
                QTest.mouseClick(browser.viewport(), Qt.LeftButton, pos=list_open.center())
                self.assertEqual(opened, ["group", "group"])
                browser.set_list_mode(False)
                browser.set_folder_title("測試影片組 · 分段")
                model.set_rows([video("first", 1), video("second", 2)])
                QTest.qWait(30)
                self.assertEqual(model.rowCount(), 2)
                self.assertFalse(any(row.get("_group_cover") for row in model.rows))
            finally:
                browser.close()
                browser.deleteLater()
                self.app.processEvents()

    def test_many_group_cards_wrap_without_expanding_their_segments(self) -> None:
        model = VideoModel(Path(tempfile.gettempdir()) / "unused-vm-cache")
        browser = BrowserView(model)
        browser.resize(610, 440)
        browser.show()
        try:
            rows = [video("single")]
            for index in range(5):
                first = video(f"part-{index}-1", 1)
                second = video(f"part-{index}-2", 2)
                for row in (first, second):
                    row["group_id"] = f"group-{index}"
                    row["group_name"] = f"第 {index + 1} 組"
                rows.extend((first, second))
            model.set_rows(compact_browser_rows(rows))
            QTest.qWait(30)
            self.assertEqual(model.rowCount(), 6)
            rects = [browser.visualRect(model.index(index)) for index in range(6)]
            self.assertGreater(rects[1].y(), rects[0].y())
            self.assertEqual(rects[1].y(), rects[2].y())
            self.assertGreater(rects[3].y(), rects[2].y())
        finally:
            browser.close()
            browser.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
