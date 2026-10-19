"""Statistics count indexed videos and tag membership without scanning media."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from videomanager.config import AppPaths
from videomanager.db.database import open_database
from videomanager.ui.stats_dialog import StatsDialog, library_stats, readable_size


class StatsDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_library_totals_and_tag_counts_exclude_deleted_videos(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-stats-") as directory:
            paths = AppPaths(Path(directory))
            connection = open_database(paths.database, paths.backups)
            try:
                with connection:
                    connection.execute("INSERT INTO libraries VALUES ('lib', 'Test', 'X:/test', 1, '2026-01-01')")
                    for identifier, size, status, deleted in (
                        ("a", 1024**3, "done", None),
                        ("b", 2 * 1024**3, "unprocessed", None),
                        ("gone", 3 * 1024**3, "done", "2026-01-02"),
                    ):
                        connection.execute(
                            """INSERT INTO videos(id, library_id, relative_path, path_key, file_name,
                               display_title, file_size, mtime_ns, duration_ms, added_at, status, deleted_at)
                               VALUES (?, 'lib', ?, ?, ?, ?, ?, 1, 3600000, '2026-01-01', ?, ?)""",
                            (identifier, f"{identifier}.mp4", identifier, f"{identifier}.mp4", identifier,
                             size, status, deleted),
                        )
                    connection.execute("INSERT INTO tag_groups(id, name) VALUES ('category', '演員')")
                    connection.execute("INSERT INTO tags(id, group_id, name) VALUES ('tag', 'category', '甲')")
                    connection.executemany("INSERT INTO video_tags VALUES (?, 'tag')", [("a",), ("b",), ("gone",)])
                    connection.execute("INSERT INTO video_groups(id, library_id, name, created_at) VALUES ('group', 'lib', '一組', '2026-01-01')")
                    connection.executemany("INSERT INTO video_group_members VALUES (?, 'group', ?)", [("a", 1), ("b", 2)])
                data = library_stats(connection, "lib")
                self.assertEqual(data["overview"]["video_count"], 2)
                self.assertEqual(data["overview"]["total_bytes"], 3 * 1024**3)
                self.assertEqual(data["overview"]["duration_ms"], 7200000)
                self.assertEqual(data["overview"]["done"], 1)
                self.assertEqual(data["overview"]["unprocessed"], 1)
                self.assertEqual(data["groups"], 1)
                self.assertEqual(data["tags"][0]["video_count"], 2)
                self.assertEqual(readable_size(3 * 1024**3), "3.0 GB")
                dialog = StatsDialog(connection, "lib")
                try:
                    self.assertFalse(any("整理中" in label.text() for label in dialog.findChildren(QLabel)))
                    self.assertEqual(dialog.tags.topLevelItem(0).child(0).text(2), "2 / 2 部")
                    self.assertEqual(dialog.tags.topLevelItem(0).child(0).data(1, Qt.UserRole), (2, 2))
                    dialog.search.setText("找不到")
                    self.assertTrue(dialog.tags.topLevelItem(0).isHidden())
                finally:
                    dialog.close()
                    dialog.deleteLater()
                    self.app.processEvents()
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
