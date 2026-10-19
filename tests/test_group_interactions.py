"""Dropping onto the card area and grouping selected library videos work."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QMessageBox

from videomanager.config import AppPaths
from videomanager.db.database import open_database
from videomanager.services.library_service import open_library
from videomanager.services.import_service import plan_group_import
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.services.trash_service import restore_video
from videomanager.services.video_group_service import create_video_group, list_groups
from videomanager.ui.group_order_dialog import GroupOrderDialog
from videomanager.ui.main_window import MainWindow


class GroupInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_card_drop_starts_one_group_import_and_selected_cards_form_group(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-group-ui-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            existing = [root / "one.mp4", root / "two.mp4"]
            for path in existing:
                path.write_bytes(b"disposable test video")
            source = base / "source"
            source.mkdir()
            incoming = [source / "part1.mp4", source / "part2.mp4"]
            for path in incoming:
                path.write_bytes(b"disposable incoming video")
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
                    window.show()
                    self.app.processEvents()
                    window._set_import_mode(True)
                    mime = QMimeData()
                    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in incoming])
                    viewport = window.browser.viewport()
                    enter = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, mime,
                                           Qt.LeftButton, Qt.NoModifier)
                    drop = QDropEvent(QPointF(10, 10), Qt.CopyAction, mime,
                                      Qt.LeftButton, Qt.NoModifier)
                    with patch.object(window, "_begin_import") as begin:
                        QApplication.sendEvent(viewport, enter)
                        QApplication.sendEvent(viewport, drop)
                        begin.assert_called_once_with(incoming, group=True)

                    window._import_is_group = True
                    window._planned_items = plan_group_import(root, marker.library_id, incoming)
                    with patch.object(GroupOrderDialog, "exec", return_value=QDialog.Accepted), \
                         patch.object(window, "_confirm_import") as confirm_import:
                        window._on_import_plan_finished()
                        confirm_import.assert_called_once()
                        self.assertEqual(len(confirm_import.call_args.args[0]), 2)

                    selection = window.browser.selectionModel()
                    for row in range(2):
                        selection.select(window.model.index(row), QItemSelectionModel.Select)
                    class ActiveWorker:
                        interrupted = False

                        def isRunning(self):
                            return True

                        def requestInterruption(self):
                            self.interrupted = True

                    active = ActiveWorker()
                    window.worker = active
                    window.group_selected()
                    self.assertTrue(active.interrupted)
                    self.assertEqual(len(window._pending_group_ids), 2)
                    window.worker = None
                    window._pending_group_ids = None
                    with patch.object(GroupOrderDialog, "exec", return_value=QDialog.Accepted), \
                         patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                         patch.object(QMessageBox, "critical", side_effect=AssertionError("group failed")):
                        window.group_selected()
                    groups = list_groups(window.connection, marker.library_id)
                    self.assertEqual(len(groups), 1)
                    self.assertEqual(groups[0]["part_count"], 2)
                    self.assertTrue((root / groups[0]["folder_rel_path"] / "one.mp4").is_file())
                    window.group_section_button.click()
                    self.assertEqual(window.model.rowCount(), 1)
                    self.assertTrue(window.model.rows[0]["_group_cover"])
                    self.assertIsNone(window.selected_video_id())
                    with patch.object(QInputDialog, "getText", return_value=("新的影片組名稱", True)):
                        window.rename_video_group(groups[0]["id"])
                    self.assertEqual(window.model.rows[0]["group_name"], "新的影片組名稱")
                    self.assertEqual(
                        window.connection.execute("SELECT name FROM video_groups WHERE id = ?", (groups[0]["id"],)).fetchone()[0],
                        "新的影片組名稱",
                    )
                    self.assertTrue(any("新的影片組名稱" in window.navigation.item(index).text()
                                        for index in range(window.navigation.count())))
                    with patch.object(window, "open_player") as play:
                        window.play_group_from_start(groups[0]["id"])
                        self.assertEqual(play.call_count, 1)
                        self.assertEqual(play.call_args.args[0], window.model.rows[0]["id"])
                    window.open_group_folder(groups[0]["id"])
                    self.assertEqual(window.model.rowCount(), 2)
                    self.assertFalse(any(row.get("_group_cover") for row in window.model.rows))
                    self.assertTrue(window.group_breadcrumb.isVisible())
                    self.assertEqual(window.group_breadcrumb_title.text(), "新的影片組名稱")
                    with patch.object(QInputDialog, "getText", return_value=("第二次改名", True)):
                        window.group_rename_button.click()
                    self.assertEqual(window.group_breadcrumb_title.text(), "第二次改名")
                    self.assertEqual(window.model.rows[0]["group_name"], "第二次改名")
                    window.close_group_folder()
                    self.assertEqual(window.model.rowCount(), 1)
                    self.assertEqual(window.model.rows[0]["group_name"], "第二次改名")
                    window.open_group_folder(groups[0]["id"])
                    with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
                        window.group_dissolve_button.click()
                    self.assertEqual(len(list_groups(window.connection, marker.library_id)), 1)
                    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
                        window.group_dissolve_button.click()
                    self.assertEqual(list_groups(window.connection, marker.library_id), [])
                    window.single_section_button.click()
                    self.assertEqual(window.model.rowCount(), 2)
                    self.assertTrue(all(not row.get("group_id") for row in window.model.rows))
                    self.assertEqual(len(list(root.rglob("*.mp4"))), 2)
                    self.assertEqual(window.connection.execute(
                        "SELECT COUNT(*) FROM videos WHERE library_id=? AND deleted_at IS NULL",
                        (marker.library_id,),
                    ).fetchone()[0], 2)
                finally:
                    window.close()

    def test_delete_whole_group_moves_all_parts_to_trash_and_restore_brings_group_back(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-group-delete-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            for name in ("part-1.mp4", "part-2.mp4"):
                (root / name).write_bytes(b"disposable group part")
            paths = AppPaths(base / "data")
            marker = open_library(paths, root, create=True).marker
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, marker.library_id, root, scan_library(root, marker.library_id))
                ids = [row[0] for row in connection.execute("SELECT id FROM videos ORDER BY file_name")]
                group_id = create_video_group(connection, marker.library_id, "可刪除測試組", ids)
            finally:
                connection.close()
            with patch.object(MainWindow, "start_scan"):
                window = MainWindow(paths)
                try:
                    window.show()
                    self.app.processEvents()
                    with patch.object(QMessageBox, "exec", return_value=QMessageBox.Yes):
                        window.recycle_group(group_id)
                    self.assertEqual(window.connection.execute(
                        "SELECT COUNT(*) FROM trash_entries"
                    ).fetchone()[0], 2)
                    self.assertEqual(list_groups(window.connection, marker.library_id), [])
                    self.assertEqual(len(list((root / ".videomanager" / "trash").glob("*.mp4"))), 2)
                    for video_id in ids:
                        restore_video(window.connection, marker.library_id, root, video_id)
                    self.assertEqual(len(list_groups(window.connection, marker.library_id)), 1)
                    self.assertEqual(list_groups(window.connection, marker.library_id)[0]["part_count"], 2)
                    self.assertEqual(len(list(root.glob("part-*.mp4"))), 2)
                finally:
                    window.close()


if __name__ == "__main__":
    unittest.main()
