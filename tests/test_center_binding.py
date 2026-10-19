"""Each compression job owns its settings and progress in the center UI."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from videomanager.config import AppPaths
from videomanager.compression.profiles import Profile
from videomanager.compression.repository import queue_video, update_job
from videomanager.db.database import open_database
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.ui.compression_center import CompressionCenter


class CenterBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_selection_loads_only_that_videos_profile_and_progress(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-center-binding-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            for name in ("alpha.mp4", "beta.mp4"):
                (root / name).write_bytes(b"disposable")
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                video_ids = [row["id"] for row in connection.execute(
                    "SELECT id FROM videos ORDER BY file_name"
                )]
                center = CompressionCenter(paths, connection, root, library_id, video_ids)
                try:
                    center.show()
                    center.cap_thread.wait(30000)
                    self.app.processEvents()
                    jobs_by_video = {row["video_id"]: row["id"] for row in connection.execute(
                        "SELECT id,video_id FROM compression_jobs"
                    )}
                    self.assertEqual(len(jobs_by_video), 2)

                    def select(job_id: str) -> None:
                        item = next(center.jobs_list.item(index)
                                    for index in range(center.jobs_list.count())
                                    if center.jobs_list.item(index).data(Qt.UserRole) == job_id)
                        center.jobs_list.setCurrentItem(item)
                        self.app.processEvents()

                    first, second = (jobs_by_video[identifier] for identifier in video_ids)
                    select(first)
                    center.target_reduction.setValue(31)
                    self.app.processEvents()
                    select(second)
                    self.assertEqual(center.target_reduction.value(), 10)
                    center.target_reduction.setValue(42)
                    self.app.processEvents()
                    select(first)
                    self.assertEqual(center.target_reduction.value(), 31)
                    self.assertEqual(json.loads(connection.execute(
                        "SELECT profile_snapshot_json FROM compression_jobs WHERE id=?", (second,)
                    ).fetchone()[0])["target_reduction_pct"], 42)
                    before = center.progress.value()
                    center._progress_changed(SimpleNamespace(job_id=second, mode="analyze"), 67.0)
                    self.assertEqual(center.progress.value(), before)
                    self.assertIn("alpha.mp4", center.job_context.text())
                finally:
                    center.close()
            finally:
                connection.close()

    def test_extra_data_stream_reaches_safe_replacement_worker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-center-streams-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            (root / "source.mp4").write_bytes(b"disposable source")
            (root / "source_compressed.mp4").write_bytes(b"disposable copy")
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                video_id = connection.execute(
                    "SELECT id FROM videos WHERE file_name='source.mp4'"
                ).fetchone()[0]
                job_id = queue_video(connection, root, library_id, video_id, Profile())
                update_job(connection, job_id, status="completed",
                           output_rel_path="source_compressed.mp4", actual_size=15,
                           actual_reduction_pct=20.0,
                           sample_quality_details_json=json.dumps({"extra_streams": True}))
                center = CompressionCenter(paths, connection, root, library_id, [])
                try:
                    center.cap_thread.wait(30000)
                    self.app.processEvents()
                    center.refresh_jobs(job_id)
                    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                         patch.object(center, "_start_replace") as start:
                        center.confirm_replace()
                    start.assert_called_once_with(job_id)
                    self.assertTrue((root / "source.mp4").is_file())
                    self.assertTrue((root / "source_compressed.mp4").is_file())
                finally:
                    center.close()
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
