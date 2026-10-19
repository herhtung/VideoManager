"""Analysis and full encoding keep separate, single-worker queues."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from videomanager.compression.profiles import Profile
from videomanager.compression.worker import _run_ffmpeg
from videomanager.compression.repository import get_job, queue_video, schedule_encode, update_job
from videomanager.config import AppPaths
from videomanager.db.database import open_database
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.ui.compression_center import CompressionCenter


class CompressionQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_analysis_queue_advances_while_encode_queue_is_busy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-two-queues-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            for name in ("first.mp4", "second.mp4", "encode.mp4"):
                (root / name).write_bytes(b"disposable")
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            release_encode = threading.Event()
            release_first_analysis = threading.Event()
            release_second_analysis = threading.Event()
            encode_started = threading.Event()
            first_analysis_started = threading.Event()
            second_analysis_started = threading.Event()
            center = None
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                ids = {row["file_name"]: row["id"] for row in connection.execute(
                    "SELECT id,file_name FROM videos"
                )}
                encode_id = queue_video(connection, root, library_id, ids["encode.mp4"], Profile())
                update_job(connection, encode_id, status="analyzed")
                schedule_encode(connection, encode_id)
                first_id = queue_video(connection, root, library_id, ids["first.mp4"], Profile())
                second_id = queue_video(connection, root, library_id, ids["second.mp4"], Profile())
                center = CompressionCenter(paths, connection, root, library_id)
                center.show()
                center.cap_thread.wait(30000)
                self.app.processEvents()

                def fake_analysis(worker_connection, identifier, _root, _cap, _count,
                                  cancelled, progress) -> None:
                    update_job(worker_connection, identifier, status="analyzing")
                    if identifier == second_id:
                        first_analysis_started.set()
                        self.assertTrue(release_first_analysis.wait(10))
                    else:
                        second_analysis_started.set()
                        self.assertTrue(release_second_analysis.wait(10))
                    self.assertFalse(cancelled())
                    progress(100)
                    update_job(worker_connection, identifier, status="analyzed", progress=100)

                def fake_encode(worker_connection, identifier, _root, _cap,
                                cancelled, progress) -> None:
                    self.assertEqual(identifier, encode_id)
                    update_job(worker_connection, identifier, status="encoding")
                    encode_started.set()
                    self.assertTrue(release_encode.wait(10))
                    self.assertFalse(cancelled())
                    progress(100)
                    update_job(worker_connection, identifier, status="completed", progress=100)

                with patch("videomanager.ui.compression_center.run_analysis", side_effect=fake_analysis), \
                     patch("videomanager.ui.compression_center.run_encode", side_effect=fake_encode):
                    center._pump_encode()
                    self.assertTrue(encode_started.wait(5))
                    center._start_batch("analyze")
                    self.assertTrue(first_analysis_started.wait(5))
                    self.assertIsNotNone(center.worker)
                    self.assertIsNotNone(center.analysis_worker)
                    release_first_analysis.set()
                    deadline = time.monotonic() + 5
                    while not second_analysis_started.is_set() and time.monotonic() < deadline:
                        self.app.processEvents()
                        QTest.qWait(10)
                    self.assertTrue(second_analysis_started.is_set())
                    self.assertIsNotNone(center.worker)
                    release_second_analysis.set()
                    release_encode.set()
                    deadline = time.monotonic() + 5
                    while (center.worker or center.analysis_worker) and time.monotonic() < deadline:
                        self.app.processEvents()
                        QTest.qWait(10)
                    self.assertIsNone(center.worker)
                    self.assertIsNone(center.analysis_worker)
                    self.assertEqual(get_job(connection, first_id)["status"], "analyzed")
                    self.assertEqual(get_job(connection, second_id)["status"], "analyzed")
                    self.assertEqual(get_job(connection, encode_id)["status"], "completed")
            finally:
                release_encode.set()
                release_first_analysis.set()
                release_second_analysis.set()
                if center is not None:
                    center.close()
                connection.close()

    def test_running_process_can_pause_and_resume_without_losing_progress(self) -> None:
        paused = threading.Event()
        cancelled = threading.Event()
        values: list[float] = []
        outcome: list[Exception] = []

        class Control:
            def __call__(self) -> bool:
                return cancelled.is_set()

            def pause_requested(self) -> bool:
                return paused.is_set()

        script = ("import time\n"
                  "for index in range(12):\n"
                  " print(f'out_time_us={index * 100000}', flush=True)\n"
                  " time.sleep(0.18)\n")
        with tempfile.TemporaryDirectory(prefix="vm-pause-process-") as directory:
            def run() -> None:
                try:
                    _run_ffmpeg([sys.executable, "-u", "-c", script], 1.2, Control(),
                                values.append, space_path=Path(directory))
                except Exception as exc:
                    outcome.append(exc)

            thread = threading.Thread(target=run)
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while len(values) < 2 and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertGreaterEqual(len(values), 2)
                paused.set()
                time.sleep(0.5)
                count_while_paused = len(values)
                time.sleep(0.5)
                self.assertEqual(len(values), count_while_paused)
                paused.clear()
                thread.join(timeout=8)
                self.assertFalse(thread.is_alive())
                self.assertEqual(outcome, [])
                self.assertEqual(values[-1], 100.0)
            finally:
                paused.clear()
                cancelled.set()
                thread.join(timeout=5)

    def test_completed_video_can_be_replaced_while_other_video_encodes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-replace-during-encode-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            for name in ("finished.mp4", "encoding.mp4"):
                (root / name).write_bytes(b"disposable media")
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            center = None
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                ids = {row["file_name"]: row["id"] for row in connection.execute(
                    "SELECT id,file_name FROM videos"
                )}
                finished_id = queue_video(connection, root, library_id, ids["finished.mp4"], Profile())
                update_job(connection, finished_id, status="completed")
                encoding_id = queue_video(connection, root, library_id, ids["encoding.mp4"], Profile())
                update_job(connection, encoding_id, status="encoding")
                center = CompressionCenter(paths, connection, root, library_id)
                center.cap_thread.wait(30000)
                self.app.processEvents()
                center.refresh_jobs(finished_id)
                encoding_worker = MagicMock(job_id=encoding_id)
                center.worker = encoding_worker
                center._show_job()
                self.assertEqual(center._selected_id(), finished_id)
                self.assertTrue(center.replace_button.isEnabled())
                fake_replacement = MagicMock(job_id=finished_id)
                with patch("videomanager.ui.compression_center.CompressionThread",
                           return_value=fake_replacement):
                    center._start_replace(finished_id)
                fake_replacement.start.assert_called_once()
                self.assertIs(center.worker, encoding_worker)
                self.assertIs(center.replacement_worker, fake_replacement)
                self.assertEqual(center.next_step.text(),
                                 "正在替換原片並更新縮圖；完成後可從歷史紀錄還原。")
            finally:
                if center is not None:
                    center.worker = None
                    center.replacement_worker = None
                    center.close()
                connection.close()


if __name__ == "__main__":
    unittest.main()
