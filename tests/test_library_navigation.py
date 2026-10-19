"""Library navigation keeps context while organization and playback change."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel, QRect, Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QWidget

from videomanager.config import AppPaths, load_settings
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

    def make_window(self, names: list[str], *, group: bool = False) -> MainWindow:
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
        finally:
            connection.close()
        with patch.object(MainWindow, "start_scan"):
            self.window = MainWindow(self.paths)
        self.window.show()
        self.app.processEvents()
        return self.window

    def test_save_and_section_switch_keep_unfinished_video_in_triage(self) -> None:
        window = self.make_window(["single.mp4", "part1.mp4", "part2.mp4"], group=True)
        self.assertTrue(window.nav_content.isHidden())
        window.nav_toggle.click()
        self.assertFalse(window.nav_content.isHidden())
        window.nav_toggle.click()
        self.assertTrue(window.nav_content.isHidden())
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

    def test_complete_keeps_video_and_scroll_when_it_stays_visible(self) -> None:
        window = self.make_window([f"clip-{n:02}.mp4" for n in range(20)])
        window._select_nav_key("all")
        window.browser.setCurrentIndex(window.model.index(8))
        self.app.processEvents()
        window.browser.verticalScrollBar().setValue(200)
        scroll = window.browser.verticalScrollBar().value()
        identifier = window.selected_video_id()
        window.details.finish_current()
        self.app.processEvents()
        self.assertEqual(window.selected_video_id(), identifier)
        self.assertEqual(window.browser.verticalScrollBar().value(), scroll)

    def test_multiple_groups_can_batch_add_and_remove_tags_and_entities(self) -> None:
        window = self.make_window(["part1.mp4", "part2.mp4", "part3.mp4", "part4.mp4"], group=True)
        ids = [row["id"] for row in window.connection.execute("SELECT id FROM videos WHERE file_name IN ('part3.mp4','part4.mp4')")]
        create_video_group(window.connection, window.library_id, "第二組", ids)
        window.refresh_navigation()
        window._select_nav_key("all")
        window.group_section_button.click()
        selection = window.browser.selectionModel()
        for number in range(2):
            selection.select(window.model.index(number), QItemSelectionModel.Select)
        window.show_selected_details()
        selected = set(window._selected_video_ids())
        self.assertEqual(set(window.details.batch_ids), selected)
        self.assertEqual(len(selected), 4)
        tag = repo.create_tag(window.connection, repo.create_tag_group(window.connection, "測試分類"), "批次標籤")
        person = repo.create_entity(window.connection, "person", "批次人物")
        window.batch_relations()
        window.details.tags.set_selected({tag})
        window.details.people.set_selected({person})
        self.assertTrue(window.details.save_current())
        for identifier in selected:
            self.assertIn(tag, repo.video_tag_ids(window.connection, identifier))
            self.assertIn(person, repo.video_entity_ids(window.connection, identifier))
        window.details.set_batch(list(selected))
        window.details.batch_operation.setCurrentIndex(1)
        window.details.tags.set_selected({tag})
        window.details.people.set_selected({person})
        self.assertTrue(window.details.save_current())
        for identifier in selected:
            self.assertNotIn(tag, repo.video_tag_ids(window.connection, identifier))
            self.assertNotIn(person, repo.video_entity_ids(window.connection, identifier))

    def test_related_popup_full_results_and_card_navigation(self) -> None:
        window = self.make_window([f"clip-{n:02}.mp4" for n in range(10)])
        ids = [row["id"] for row in window.model.rows]
        person = repo.create_entity(window.connection, "person", "共同人物")
        with window.connection:
            window.connection.executemany(
                "INSERT INTO video_entities(video_id,entity_id) VALUES (?,?)",
                ((identifier, person) for identifier in ids),
            )
        window._show_all_related_videos(ids[0])
        self.assertEqual(window._nav_key(), "all")
        self.assertEqual({row["id"] for row in window.model.rows}, set(ids[1:]))
        window._open_related_video(ids[4])
        self.assertEqual(window.selected_video_id(), ids[4])
        self.assertEqual(window.advanced_filters, {})

    def test_group_hover_aggregates_metadata_and_recommendations_without_duplicates(self) -> None:
        window = self.make_window(["source1.mp4", "source2.mp4", "part1.mp4", "part2.mp4", "solo.mp4"], group=True)
        connection = window.connection
        ids = {row["file_name"]: row["id"] for row in connection.execute("SELECT id,file_name FROM videos")}
        source_group = create_video_group(connection, window.library_id, "來源組",
                                         [ids["source1.mp4"], ids["source2.mp4"]])
        person = repo.create_entity(connection, "person", "人物甲")
        studio = repo.create_entity(connection, "studio", "片商甲")
        tag = repo.create_tag(connection, repo.create_tag_group(connection, "測試"), "第二段標籤")
        with connection:
            connection.executemany("INSERT INTO video_entities VALUES (?,?)", [
                (ids["source2.mp4"], person), (ids["source1.mp4"], studio),
                (ids["part2.mp4"], person), (ids["part1.mp4"], person)])
            connection.execute("INSERT INTO video_tags VALUES (?,?)", (ids["source2.mp4"], tag))
        window.refresh_navigation()
        window._set_browser_section("group")
        self.app.processEvents()
        source_index = next(i for i, row in enumerate(window.model.rows) if row["group_id"] == source_group)
        video_id = window.model.rows[source_index]["id"]
        matches = repo.related_videos(connection, video_id, limit=9)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["group_size"], 2)
        self.assertEqual(matches[0]["shared_count"], 1)
        self.assertTrue(matches[0]["_group_cover"])
        self.assertEqual(matches[0]["display_title"], "測試組")
        rect = window.browser.visualRect(window.model.index(source_index))
        window.show_related_videos(video_id, window.browser.viewport().mapToGlobal(rect.bottomLeft()))
        popup = window._related_popup
        self.assertTrue(popup.isVisible())
        self.assertIn("影片組詳情 · 來源組", popup.heading.text())
        labels = [button.text() for button in popup.findChildren(QPushButton)]
        self.assertIn("人物甲", labels)
        self.assertIn("片商甲", labels)
        self.assertIn("第二段標籤", labels)
        self.assertEqual(popup.content.count(), 1)
        card = popup.content.itemAt(0).widget()
        card.click()
        self.assertEqual(window.selected_video_id(), ids["part1.mp4"])
        self.assertTrue(window._nav_key().startswith("group:"))
        window._show_all_related_videos(video_id)
        self.assertEqual(window._browser_section, "group")
        self.assertTrue(window.group_section_button.isChecked())
        self.assertEqual(len(window.model.rows), 1)
        self.assertEqual(window.model.rows[0]["group_name"], "測試組")

    def test_summary_shows_single_and_group_counts_in_both_sections(self) -> None:
        window = self.make_window(["single.mp4", "part1.mp4", "part2.mp4"], group=True)
        self.assertEqual(window.summary.text(), "1 部影片／1 組影片")
        window._set_browser_section("group")
        self.assertEqual(window.summary.text(), "1 部影片／1 組影片")
        window.search.setText("single")
        window.refresh_videos()
        self.assertEqual(window.summary.text(), "1 部影片／0 組影片")

    def test_sidebars_snap_to_rails_and_restore_button_widths(self) -> None:
        window = self.make_window(["single.mp4"])
        splitter = window.nav_splitter
        window.nav_toggle.click()
        self.app.processEvents()
        window._resize_sidebar(0, 180)
        nav_width = splitter.sizes()[0]
        window.nav_toggle.click()
        self.assertEqual(splitter.sizes()[0], 42)
        window.nav_toggle.click()
        self.assertEqual(splitter.sizes()[0], nav_width)
        window._resize_sidebar(2, 390)
        details_width = splitter.sizes()[2]
        window.details_toggle.click()
        self.assertTrue(window.details.isHidden())
        self.assertEqual(splitter.sizes()[2], 42)
        self.assertEqual(load_settings(window.paths.settings).details_sidebar_width, details_width)
        window.details_toggle.click()
        self.assertEqual(splitter.sizes()[2], details_width)
        splitter.moveSplitter(0, 1)
        self.assertTrue(window._navigation_collapsed)
        self.assertEqual(splitter.sizes()[0], 42)
        self.assertEqual(window.settings.nav_sidebar_width, 0)
        splitter.moveSplitter(splitter.width(), 2)
        self.assertTrue(window._details_collapsed)
        self.assertEqual(splitter.sizes()[2], 42)
        self.assertFalse(window.details_toggle.isHidden())
        self.assertEqual(window.settings.details_sidebar_width, 0)
        window.details_toggle.click()
        self.assertEqual(splitter.sizes()[2], window._sidebar_open_width(2))
        splitter.moveSplitter(140, 1)
        self.assertFalse(window._navigation_collapsed)
        self.assertGreaterEqual(splitter.sizes()[0], 150)

    def test_detail_columns_can_be_resized_without_changing_selection(self) -> None:
        window = self.make_window(["single.mp4"])
        window.browser.setCurrentIndex(window.model.index(0))
        window._resize_sidebar(2, 450)
        self.app.processEvents()
        columns = window.details.columns_splitter
        before = columns.sizes()
        video_id = window.details.video_id
        columns.moveSplitter(235, 1)
        self.assertNotEqual(columns.sizes(), before)
        self.assertEqual(window.details.video_id, video_id)
        self.assertGreater(min(columns.sizes()), 0)

    def test_hover_details_without_related_videos_shows_metadata_and_filters(self) -> None:
        window = self.make_window(["single.mp4"])
        video_id = window.model.rows[0]["id"]
        person = repo.create_entity(window.connection, "person", "人物甲")
        studio = repo.create_entity(window.connection, "studio", "片商甲")
        tag_group = repo.create_tag_group(window.connection, "測試標籤分類")
        tag = repo.create_tag(window.connection, tag_group, "測試標籤")
        repo.set_video_relations(window.connection, video_id, tag_ids={tag})
        with window.connection:
            window.connection.executemany(
                "INSERT INTO video_entities(video_id,entity_id) VALUES (?,?)",
                ((video_id, person), (video_id, studio)),
            )
        window.browser._hover_candidate = 0
        window.browser._start_hover()
        window.show_related_videos(video_id, window.mapToGlobal(window.rect().center()))
        popup = window._related_popup
        self.assertTrue(popup.isVisible())
        self.assertEqual(popup.source_id, video_id)
        self.assertEqual(popup.content.count(), 0)
        self.assertTrue(window.browser._cycle.isActive())
        source = window.browser.visualRect(window.model.index(0))
        source = QRect(window.browser.viewport().mapToGlobal(source.topLeft()), source.size())
        self.assertFalse(popup.frameGeometry().intersects(source))
        from PySide6.QtWidgets import QPushButton
        labels = [button.text() for button in popup.findChildren(QPushButton)]
        self.assertIn("人物甲", labels)
        self.assertIn("片商甲", labels)
        self.assertIn("測試標籤", labels)
        self.assertLessEqual(popup.height(), window.screen().availableGeometry().height())
        next(button for button in popup.findChildren(QPushButton) if button.text() == "測試標籤").click()
        self.assertEqual(window.advanced_filters, {"tag_all": [tag]})
        self.assertFalse(popup.isVisible())
        window.settings = replace(window.settings, related_popup_enabled=False)
        window.show_related_videos(video_id, source.bottomLeft())
        self.assertFalse(popup.isVisible())


if __name__ == "__main__":
    unittest.main()
