"""Required tag categories gate completion and stay configurable per category."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from videomanager.config import AppPaths
from videomanager.db import repository as repo
from videomanager.db.database import open_database
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.ui.details_panel import DetailsPanel
from videomanager.ui.metadata_dialog import MetadataDialog


class RequiredTagGroupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_category_toggle_blocks_only_completion_until_tag_is_selected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-required-tag-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            (root / "one.mp4").write_bytes(b"disposable test video")
            paths = AppPaths(base / "data")
            marker = open_library(paths, root, create=True).marker
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, marker.library_id, root, scan_library(root, marker.library_id))
                video_id = repo.list_videos(connection, marker.library_id, {})[0]["id"]
                group_id = repo.create_tag_group(connection, "女優")
                tag_id = repo.create_tag(connection, group_id, "示範")
                dialog = MetadataDialog(connection)
                dialog.categories.select(group_id)
                self.assertFalse(dialog.category_required.isChecked())
                dialog.category_required.setChecked(True)
                self.assertEqual(repo.list_tag_groups(connection)[0]["is_required"], 1)
                self.assertIn("必選", dialog.categories._buttons[group_id].text())
                dialog.close()

                panel = DetailsPanel(connection)
                panel.set_video(video_id)
                self.assertTrue(panel.save_current())
                self.assertEqual(repo.video_by_id(connection, video_id)["status"], "in_progress")
                with patch.object(QMessageBox, "warning") as warning:
                    panel.finish_current()
                    self.assertEqual(warning.call_count, 1)
                    self.assertIn("女優", warning.call_args.args[2])
                self.assertEqual(repo.video_by_id(connection, video_id)["status"], "in_progress")
                panel.tags.set_selected({tag_id})
                panel.finish_current()
                self.assertEqual(repo.video_by_id(connection, video_id)["status"], "done")
                self.assertEqual(repo.video_tag_ids(connection, video_id), {tag_id})

                destination_group = repo.create_tag_group(connection, "內容")
                manager = MetadataDialog(connection)
                manager._tag_selected(tag_id)
                manager.categories.select(destination_group)
                with patch.object(QMessageBox, "critical", side_effect=AssertionError("tag move failed")):
                    manager.move_tag_to_selected_category()
                moved = next(row for row in repo.list_tags(connection) if row["id"] == tag_id)
                self.assertEqual(moved["group_id"], destination_group)
                self.assertEqual(repo.video_tag_ids(connection, video_id), {tag_id})
                manager.close()

                repo.set_tag_group_required(connection, group_id, False)
                self.assertEqual(next(row for row in repo.list_tag_groups(connection)
                                      if row["id"] == group_id)["is_required"], 0)
                panel.close()
                panel.deleteLater()
                manager.deleteLater()
                dialog.deleteLater()
                self.app.processEvents()
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
