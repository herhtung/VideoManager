"""Tag category refreshes must stay inside the owning panel."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QToolButton, QWidget

from videomanager.config import AppPaths
from videomanager.db.database import open_database
from videomanager.db.repository import create_tag, create_tag_group, set_video_relations
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.ui.tag_picker import TagPicker


class _TopLevelShows(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.widgets: list[QWidget] = []

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Show and isinstance(watched, QWidget) and watched.isWindow():
            self.widgets.append(watched)
        return False


class TagPickerWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_reload_does_not_show_category_body_as_a_window(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-tag-picker-") as directory:
            root = Path(directory)
            connection = open_database(root / "data.sqlite", root / "backups")
            try:
                group_id = create_tag_group(connection, "分類")
                create_tag(connection, group_id, "標籤")
                parent = QWidget()
                watch = _TopLevelShows()
                self.app.installEventFilter(watch)
                try:
                    picker = TagPicker(connection, parent)
                    picker.reload()
                    self.app.processEvents()
                    self.assertEqual(watch.widgets, [])
                finally:
                    self.app.removeEventFilter(watch)
                    parent.close()
            finally:
                connection.close()

    def test_used_tags_appear_only_in_their_category(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-tag-picker-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            (root / "example.mp4").write_bytes(b"disposable test video")
            paths = AppPaths(base / "data")
            marker = open_library(paths, root, create=True).marker
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, marker.library_id, root, scan_library(root, marker.library_id))
                video_id = connection.execute("SELECT id FROM videos").fetchone()[0]
                group_id = create_tag_group(connection, "分類")
                tag_id = create_tag(connection, group_id, "標籤")
                set_video_relations(connection, video_id, tag_ids={tag_id})
                picker = TagPicker(connection)
                try:
                    headers = [button.text() for button in picker.findChildren(QToolButton)
                               if button.objectName() == "tagSection"]
                    self.assertEqual(len(headers), 1)
                    self.assertIn("分類", headers[0])
                    self.assertNotIn("常用標籤", headers[0])
                finally:
                    picker.close()
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
