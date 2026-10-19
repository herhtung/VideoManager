"""Library navigation keeps context while organization and playback change."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from videomanager.config import AppPaths
from videomanager.db import repository as repo
from videomanager.db.database import open_database
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.services.video_group_service import create_video_group
from videomanager.ui.main_window import MainWindow


class _FakePlayer(QWidget):
    playback_closed = Signal()
    compression_requested = Signal(str)
    cut_requested = Signal(str)
    metadata_saved = Signal()

    def __init__(self, *_args, **_kwargs) -> None:
        super().__init__()


class LibraryNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="vm-nav-")
        self.base = Path(self.directory.name)
        self.root = self.base / "library"
        self.root.mkdir()
        self.paths = AppPaths(self.base / "data")
        self.window: MainWindow | None = None

    def tearDown(self) -> None:
        if self.window is not None:
            self.window.close()
        self.directory.cleanup()

    def make_window(self, names: list[str], *, group: bool = False, legacy: bool = False) -> MainWindow:
        for name in names:
            (self.root / name).write_bytes(b"disposable test MP4")
        library_id = open_library(self.paths, self.root, create=True).marker.library_id
        connection = open_database(self.paths.database, self.paths.backups)
        try:
            synchronize(connection, library_id, self.root, scan_library(self.root, library_id))
            if group:
                ids = [row["id"] for row in connection.execute(
                    "SELECT id FROM videos WHERE file_name IN ('part1.mp4','part2.mp4') ORDER BY file_name"
                )]
                create_video_group(connection, library_id, "測試組", ids)
            if legacy:
                with connection:
                    connection.execute("UPDATE videos SET status='in_progress' WHERE file_name='single.mp4'")
                    connection.execute("DELETE FROM schema_migrations WHERE version=9")
        finally:
            connection.close()
        with patch.object(MainWindow, "start_scan"):
            self.window = MainWindow(self.paths)
        self.window.show()
        self.app.processEvents()
        return self.window

    def test_migration_save_and_section_switch_keep_unfinished_video_in_triage(self) -> None:
        window = self.make_window(["single.mp4", "part1.mp4", "part2.mp4"], group=True, legacy=True)
        labels = [window.navigation.item(i).text() for i in range(window.navigation.count())]
        self.assertEqual(labels[0], "全部影片")
        self.assertFalse(any("整理中" in label or "回收桶" in label for label in labels))
        self.assertIs(window.library_task_strip.parent(), window.statusBar())
        self.assertEqual(window.model.rowCount(), 1)
        video_id = window.model.rows[0]["id"]
        self.assertEqual(repo.video_by_id(window.connection, video_id)["status"], "unprocessed")
        window.browser.setCurrentIndex(window.model.index(0))
        window.details.title.setText("改過名稱")
        self.assertTrue(window.details.save_current())
        self.assertEqual(repo.video_by_id(window.connection, video_id)["status"], "unprocessed")
        with window.connection:
            window.connection.execute(
                "INSERT INTO saved_filters(id,name,filter_json,created_at) VALUES (?,?,?,?)",
                ("legacy-filter", "舊篩選", '{"status":"in_progress"}', "2026-01-01"),
            )
        window.refresh_navigation()
        window._select_nav_key("saved:legacy-filter")
        self.assertEqual(window._effective_filters()["status"], "unprocessed")
        window._select_nav_key("all")
        window.group_section_button.click()
        self.assertEqual(window.model.rowCount(), 1)
        self.assertTrue(window.model.rows[0]["_group_cover"])
        window.single_section_button.click()
        self.assertEqual([row["id"] for row in window.model.rows], [video_id])

    def test_delete_and_complete_stay_in_triage_and_complete_returns_to_top(self) -> None:
        window = self.make_window([f"clip-{n:02}.mp4" for n in range(20)])
        window.resize(900, 530)
        self.app.processEvents()
        window._select_nav_key("unprocessed")
        window.browser.setCurrentIndex(window.model.index(0))
        with patch.object(QMessageBox, "exec", return_value=QMessageBox.Yes):
            window.recycle_selected()
        self.assertEqual(window._nav_key(), "unprocessed")
        self.assertEqual(window.model.rowCount(), 19)
        window.browser.setCurrentIndex(window.model.index(8))
        window.browser.verticalScrollBar().setValue(200)
        window.details.finish_current()
        self.app.processEvents()
        self.assertEqual(window._nav_key(), "unprocessed")
        self.assertEqual(window.model.rowCount(), 18)
        self.assertEqual(window.browser.currentIndex().row(), 0)
        self.assertEqual(window.browser.verticalScrollBar().value(), 0)

    def test_player_return_restores_card_and_scroll(self) -> None:
        window = self.make_window([f"clip-{n:02}.mp4" for n in range(20)])
        window.resize(900, 530)
        self.app.processEvents()
        selected = window.model.index(9)
        window.browser.setCurrentIndex(selected)
        scroll_bar = window.browser.verticalScrollBar()
        self.assertGreater(scroll_bar.maximum(), 0)
        scroll_bar.setValue(min(200, scroll_bar.maximum()))
        previous_scroll = scroll_bar.value()
        video_id = window.model.rows[9]["id"]
        with patch("videomanager.ui.main_window.PlayerView", _FakePlayer):
            window._show_player(video_id, self.root / window.model.rows[9]["file_name"])
            window.browser.verticalScrollBar().setValue(0)
            window.player.playback_closed.emit()
        self.app.processEvents()
        self.assertIs(window.main_stack.currentWidget(), window.browser_page)
        self.assertEqual(window.browser.currentIndex().data(Qt.UserRole), video_id)
        self.assertEqual(scroll_bar.value(), previous_scroll)


if __name__ == "__main__":
    unittest.main()
