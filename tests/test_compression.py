"""Compression never touches a real media library in automated tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QTextBrowser

from videomanager.compression.capability_probe import probe_capabilities
from videomanager.compression.estimator import estimate_size
from videomanager.compression.profiles import Profile, target_video_options
from videomanager.compression.repository import (
    cancel_waiting_job, get_job, list_jobs, prune_terminal_jobs, queue_video,
    schedule_encode, update_job, resume_job, has_resume_checkpoint,
)
from videomanager.compression.safe_replace import (
    prune_preserved_originals, replace_original, restore_original,
)
from videomanager.compression.simple_editor import create_edit, get_edit, parse_time, replace_edit, run_edit
from videomanager.compression.sampler import sample_windows
from videomanager.compression.worker import CompressionInterrupted, _probe, fail_job, run_analysis, run_encode
from videomanager.config import AppPaths
from videomanager.db import repository as repo
from videomanager.db.database import open_database
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.services.video_group_service import (
    add_group_members, create_video_group, group_members, remove_group_member,
)
from videomanager.ui.main_window import MainWindow
from videomanager.ui.compression_center import (
    CompressionCenter, _analysis_html, _compression_advice, _low_bitrate_warning,
    _planned_method, _result_html,
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CompressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_sample_windows_and_negative_estimate(self) -> None:
        for length in (5, 30, 120, 3600):
            for count in (2, 4, 8):
                windows = sample_windows(length, count)
                self.assertTrue(all(0 <= item.start < item.start + item.duration <= length
                                    for item in windows))
                self.assertTrue(all(a.start + a.duration <= b.start
                                    for a, b in zip(windows, windows[1:])))
                self.assertTrue(all(item.start >= length * .4 - .001 and
                                    item.start + item.duration <= length * .9 + .001
                                    for item in windows))
        estimate = estimate_size(1000, 10, [
            {"duration": 2, "video_bytes": 400, "audio_bytes": 20, "overhead_bytes": 10},
            {"duration": 2, "video_bytes": 500, "audio_bytes": 20, "overhead_bytes": 10},
        ])
        self.assertLess(estimate.reduction_pct, 0)
        self.assertLess(estimate.minimum, estimate.midpoint)
        self.assertGreater(estimate.maximum, estimate.midpoint)
        self.assertEqual(Profile(target_reduction_pct=60).validate().target_reduction_pct, 60)
        with self.assertRaises(ValueError):
            Profile(target_reduction_pct=61).validate()
        with self.assertRaises(ValueError):
            Profile(target_passes=3).validate()
        self.assertIn("hevc_nvenc", target_video_options(
            Profile(encoder="hevc_nvenc", target_passes=1), 1080, 1_200_000, "hevc_nvenc"))
        self.assertIn("hevc_qsv", target_video_options(
            Profile(encoder="hevc_qsv", target_passes=1), 1080, 1_200_000, "hevc_qsv"))
        self.assertIn("偏低", _low_bitrate_warning({
            "sample_quality_details_json": json.dumps({
                "source_width": 1920, "source_height": 1080, "source_fps": "30/1",
                "source_video_bitrate": 1_000_000, "target_video_bitrate": 800_000,
            })
        }))
        self.assertEqual(_low_bitrate_warning({
            "sample_quality_details_json": json.dumps({
                "source_width": 1920, "source_height": 1080, "source_fps": "30/1",
                "source_video_bitrate": 8_000_000, "target_video_bitrate": 5_000_000,
            })
        }), "")

    def test_analysis_names_planned_method_and_bitrate_comparison(self) -> None:
        source = round(6.77 * 2**30)
        details = {"source_duration": 439 * 60, "source_video_bitrate": 1_940_000,
                   "source_audio_bitrate": 256_000, "source_container_bitrate": 2_210_000,
                   "target_video_bitrate": 1_220_000,
                   "target_size_bytes": int(source * .70)}
        row = {"source_size": source, "estimated_size_mid": round(10 * 2**30),
               "sample_quality_details_json": json.dumps(details), "status": "analyzed",
               "error_message": None, "sample_quality_score": 96.511,
               "sample_quality_metric": "vmaf", "estimated_size_min": round(9 * 2**30),
               "estimated_size_max": round(11 * 2**30)}
        profile = Profile(target_reduction_pct=30).snapshot()
        planned, method = _planned_method(row, profile)
        self.assertTrue(planned)
        self.assertIn("雙遍", method)
        self.assertIn("較小", _compression_advice(row, details, planned, profile)[0])
        sixty = Profile(target_reduction_pct=60).snapshot()
        self.assertIn("接近 60%", _compression_advice(row, details, True, sixty)[1])
        browser = QTextBrowser()
        browser.setHtml(_analysis_html(row, profile, False))
        summary = browser.toPlainText()
        self.assertIn("正式輸出會怎麼做", summary)
        self.assertIn("1.94 Mbps", summary)
        self.assertIn("1.22 Mbps", summary)
        self.assertIn("14 小時 38 分", summary)
        self.assertNotIn("VMAF 96.511", summary)
        browser.setHtml(_analysis_html(row, profile, True))
        self.assertIn("VMAF 96.511", browser.toPlainText())
        quick = Profile(target_reduction_pct=30, target_passes=1).snapshot()
        self.assertIn("快速單遍", _planned_method(row, quick)[1])
        intel = Profile(encoder="hevc_qsv", target_reduction_pct=30, target_passes=1).snapshot()
        self.assertIn("Intel GPU", _planned_method(row, intel)[1])
        details["samples"] = [{"duration": 60, "encode_elapsed_seconds": 10,
                               "video_bytes": 9_000_000}]
        row["sample_quality_details_json"] = json.dumps(details)
        browser.setHtml(_analysis_html(row, intel, False))
        self.assertIn("不會因超過兩小時而自動停止", browser.toPlainText())

    def test_history_keeps_fifty_terminal_jobs_and_pending(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-compression-history-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            (root / "video.mp4").write_bytes(b"disposable")
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                video_id = connection.execute("SELECT id FROM videos").fetchone()[0]
                identifiers = []
                for index in range(55):
                    identifier = queue_video(connection, root, library_id, video_id, Profile())
                    update_job(connection, identifier, status="completed",
                               finished_at=f"2026-10-16T00:{index:02d}:00+00:00")
                    identifiers.append(identifier)
                pending = queue_video(connection, root, library_id, video_id, Profile())
                staging = root / ".videomanager" / "compression-staging" / identifiers[0]
                staging.mkdir(parents=True)
                (staging / "sample-1.mp4").write_bytes(b"temporary")
                saved_copy = root / ".videomanager" / "compressed" / "saved.mp4"
                saved_copy.parent.mkdir(parents=True)
                saved_copy.write_bytes(b"keep copy")
                self.assertEqual(prune_terminal_jobs(connection, library_id, root), 5)
                rows = list_jobs(connection, library_id)
                self.assertEqual(rows[0]["id"], pending)
                self.assertEqual(sum(row["status"] == "completed" for row in rows), 50)
                self.assertIn(pending, {row["id"] for row in rows})
                self.assertIsNone(get_job(connection, identifiers[0]))
                self.assertFalse(staging.exists())
                self.assertEqual(saved_copy.read_bytes(), b"keep copy")
            finally:
                connection.close()

    def test_cancel_analyzed_job_removes_samples_and_keeps_video(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-compression-cancel-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            source = root / "original.mp4"
            source.write_bytes(b"original media stays")
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                video_id = connection.execute("SELECT id FROM videos").fetchone()[0]
                job_id = queue_video(connection, root, library_id, video_id, Profile())
                update_job(connection, job_id, status="analyzed")
                staging = root / ".videomanager" / "compression-staging" / job_id
                staging.mkdir(parents=True)
                (staging / "sample-1.mp4").write_bytes(b"temporary sample")
                with connection:
                    connection.execute(
                        """INSERT INTO compression_samples
                           (id,job_id,start_seconds,duration_seconds,encoded_video_bytes,sample_stats_json)
                           VALUES (?,?,?,?,?,?)""",
                        (str(uuid4()), job_id, 0, 1, 16, "{}"),
                    )
                self.assertTrue(cancel_waiting_job(connection, root, job_id))
                self.assertEqual(get_job(connection, job_id)["status"], "cancelled")
                self.assertFalse(staging.exists())
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM compression_samples WHERE job_id=?", (job_id,)
                ).fetchone()[0], 0)
                self.assertEqual(source.read_bytes(), b"original media stays")
            finally:
                connection.close()

    def test_only_newest_three_preserved_originals_remain(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-compression-retention-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            source = root / "video.mp4"
            source.write_bytes(b"current video")
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                video_id = connection.execute("SELECT id FROM videos").fetchone()[0]
                identifiers = []
                for index in range(4):
                    job_id = queue_video(connection, root, library_id, video_id, Profile())
                    identifiers.append(job_id)
                    relative = f".videomanager/compression-originals/{job_id}.mp4"
                    preserved = root / relative
                    preserved.parent.mkdir(parents=True, exist_ok=True)
                    preserved.write_bytes(f"old {index}".encode())
                    with connection:
                        connection.execute("UPDATE compression_jobs SET status='replaced' WHERE id=?", (job_id,))
                        connection.execute(
                            """INSERT INTO compression_replacements
                               (job_id,source_rel_path,output_rel_path,preserved_rel_path,state,updated_at)
                               VALUES (?,?,?,?,?,?)""",
                            (job_id, "video.mp4", f"compressed-{index}.mp4", relative,
                             "complete", f"2026-10-16T00:0{index}:00+00:00"),
                        )
                self.assertEqual(prune_preserved_originals(connection, root), 1)
                self.assertFalse((root / ".videomanager" / "compression-originals" /
                                  f"{identifiers[0]}.mp4").exists())
                self.assertEqual(connection.execute(
                    "SELECT state FROM compression_replacements WHERE job_id=?", (identifiers[0],)
                ).fetchone()[0], "original_expired")
                self.assertEqual(sum((root / ".videomanager" / "compression-originals" /
                                      f"{identifier}.mp4").exists() for identifier in identifiers), 3)
                self.assertEqual(source.read_bytes(), b"current video")
            finally:
                connection.close()

    def test_hidden_compression_center_keeps_work_and_progress_reopens_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-compression-background-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            (root / "clip.mp4").write_bytes(b"disposable")
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            synchronize(connection, library_id, root, scan_library(root, library_id))
            connection.close()
            with patch.object(MainWindow, "start_scan"):
                window = MainWindow(paths)
            try:
                window.show()
                window.open_compression_center()
                center = window.compression_center
                self.assertIsNotNone(center)
                center.cap_thread.wait(30000)
                self.app.processEvents()
                self.assertEqual(window.main_stack.currentWidget(), center)
                active_worker = object()
                center.worker = active_worker
                window.show_library_page()
                self.assertFalse(center.isVisible())
                self.assertIs(window.compression_center, center)
                self.assertIs(center.worker, active_worker)
                self.assertFalse(center._closing)
                window._compression_progress_changed(42, "壓縮中")
                self.assertTrue(window.compression_progress.isVisible())
                QTest.mouseClick(window.compression_progress, Qt.LeftButton)
                self.assertTrue(center.isVisible())
                self.assertEqual(window.main_stack.currentWidget(), center)
                center.worker = None
            finally:
                window.close()

    def test_analysis_thread_continues_after_center_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-compression-live-background-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            (root / "clip.mp4").write_bytes(b"disposable")
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            started = threading.Event()
            release = threading.Event()
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                video_id = connection.execute("SELECT id FROM videos").fetchone()[0]
                job_id = queue_video(connection, root, library_id, video_id, Profile())
                center = CompressionCenter(paths, connection, root, library_id)
                center.show()
                center.cap_thread.wait(30000)
                self.app.processEvents()

                def fake_analysis(worker_connection, identifier, _root, _cap, _count,
                                  cancelled, progress) -> None:
                    update_job(worker_connection, identifier, status="analyzing")
                    started.set()
                    if not release.wait(5):
                        raise TimeoutError("test worker was not released")
                    if cancelled():
                        raise RuntimeError("hiding the window cancelled analysis")
                    progress(100)
                    update_job(worker_connection, identifier, status="analyzed", progress=100)

                with patch("videomanager.ui.compression_center.run_analysis", side_effect=fake_analysis):
                    center._start_job(job_id, "analyze")
                    self.assertTrue(started.wait(5))
                    center.close()
                    self.assertFalse(center.isVisible())
                    release.set()
                    end = time.monotonic() + 5
                    while center.analysis_worker is not None and time.monotonic() < end:
                        self.app.processEvents()
                        QTest.qWait(10)
                    self.assertIsNone(center.analysis_worker)
                    self.assertEqual(get_job(connection, job_id)["status"], "analyzed")
                center.close()
            finally:
                release.set()
                connection.close()

    def test_next_video_can_be_analyzed_and_scheduled_during_encoding(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-compression-pipeline-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            (root / "one.mp4").write_bytes(b"first disposable file")
            (root / "two.mp4").write_bytes(b"second disposable file")
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            first_started = threading.Event()
            release_first = threading.Event()
            second_started = threading.Event()
            center = None
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                ids = [row[0] for row in connection.execute(
                    "SELECT id FROM videos ORDER BY relative_path"
                )]
                center = CompressionCenter(paths, connection, root, library_id)
                center.show()
                center.cap_thread.wait(30000)
                self.app.processEvents()
                first = queue_video(connection, root, library_id, ids[0], Profile())
                update_job(connection, first, status="analyzed")
                schedule_encode(connection, first)
                second = queue_video(connection, root, library_id, ids[1], Profile())
                center.refresh_jobs(first)

                def fake_encode(worker_connection, identifier, _root, _cap,
                                cancelled, progress) -> None:
                    update_job(worker_connection, identifier, status="encoding")
                    if identifier == first:
                        first_started.set()
                        if not release_first.wait(10):
                            raise TimeoutError("first encode did not resume")
                    else:
                        second_started.set()
                    if cancelled():
                        raise RuntimeError("unexpected cancellation")
                    progress(100)
                    update_job(worker_connection, identifier, status="completed", progress=100)

                def fake_analysis(worker_connection, identifier, _root, _cap, _count,
                                  cancelled, progress) -> None:
                    self.assertEqual(identifier, second)
                    update_job(worker_connection, identifier, status="analyzing")
                    progress(100)
                    update_job(worker_connection, identifier, status="analyzed", progress=100)

                with patch("videomanager.ui.compression_center.run_encode", side_effect=fake_encode), \
                     patch("videomanager.ui.compression_center.run_analysis", side_effect=fake_analysis):
                    center._pump_encode()
                    self.assertTrue(first_started.wait(5))
                    center._start_job(second, "analyze")
                    end = time.monotonic() + 5
                    while center.analysis_worker is not None and time.monotonic() < end:
                        self.app.processEvents()
                        QTest.qWait(10)
                    self.assertIsNone(center.analysis_worker)
                    self.assertEqual(get_job(connection, second)["status"], "analyzed")
                    center.refresh_jobs(second)
                    center.schedule_selected()
                    self.assertEqual(get_job(connection, second)["encode_queued"], 1)
                    self.assertFalse(second_started.is_set())
                    release_first.set()
                    end = time.monotonic() + 5
                    while (center.worker is not None or not second_started.is_set()) and time.monotonic() < end:
                        self.app.processEvents()
                        QTest.qWait(10)
                    self.assertTrue(second_started.is_set())
                    self.assertIsNone(center.worker)
                    self.assertEqual(get_job(connection, second)["status"], "completed")
            finally:
                release_first.set()
                if center is not None:
                    end = time.monotonic() + 5
                    while center.has_active_work() and time.monotonic() < end:
                        self.app.processEvents()
                        QTest.qWait(10)
                    center.close()
                connection.close()

    def test_target_bitrate_thirty_and_sixty_percent_on_disposable_mp4(self) -> None:
        cap = probe_capabilities(test_nvenc=False)
        if "libx265" not in cap.encoders or "libx264" not in cap.encoders:
            self.skipTest("Test FFmpeg lacks CPU encoders")
        with tempfile.TemporaryDirectory(prefix="vm-compression-target-") as directory:
            base = Path(directory)
            root = base / "測試影片庫"
            root.mkdir()
            source = root / "目標.mp4"
            result = subprocess.run(
                [cap.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "testsrc2=size=320x180:rate=24", "-f", "lavfi", "-i", "sine=frequency=400",
                 "-t", "5", "-c:v", "libx264", "-crf", "8", "-preset", "ultrafast",
                 "-c:a", "aac", "-y", str(source)], capture_output=True, timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertEqual(result.returncode, 0, result.stderr[-500:])
            before = digest(source)
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                video_id = connection.execute("SELECT id FROM videos").fetchone()[0]
                job_id = queue_video(connection, root, library_id, video_id,
                                     Profile(encoder="libx265", target_reduction_pct=30))
                update_job(connection, job_id, status="analyzed",
                           estimated_size_mid=source.stat().st_size * 2)
                run_encode(connection, job_id, root, cap)
                row = get_job(connection, job_id)
                self.assertEqual(row["status"], "completed")
                self.assertLessEqual(row["actual_size"], int(row["source_size"] * .70))
                outcome = json.loads(row["sample_quality_details_json"])
                self.assertEqual(outcome["actual_method"], "two_pass_target")
                self.assertEqual(outcome["actual_encoder"], "libx265")
                self.assertGreater(outcome["actual_video_bitrate"], 0)
                browser = QTextBrowser()
                browser.setHtml(_result_html(row, Profile(target_reduction_pct=30).snapshot()))
                self.assertIn("雙遍目標位元率", browser.toPlainText())
                self.assertEqual(digest(source), before)
                self.assertTrue((root / row["output_rel_path"]).is_file())
                quick_id = queue_video(connection, root, library_id, video_id,
                                       Profile(encoder="libx265", target_reduction_pct=30,
                                               target_passes=1))
                update_job(connection, quick_id, status="analyzed",
                           estimated_size_mid=source.stat().st_size * 2)
                run_encode(connection, quick_id, root, cap)
                quick_row = get_job(connection, quick_id)
                self.assertEqual(quick_row["status"], "completed")
                self.assertLessEqual(quick_row["actual_size"], int(quick_row["source_size"] * .70))
                self.assertEqual(json.loads(quick_row["sample_quality_details_json"])["actual_method"],
                                 "one_pass_target")
                self.assertEqual(digest(source), before)
                sixty_id = queue_video(connection, root, library_id, video_id,
                                       Profile(encoder="libx265", target_reduction_pct=60,
                                               target_passes=1))
                update_job(connection, sixty_id, status="analyzed",
                           estimated_size_mid=source.stat().st_size * 2)
                run_encode(connection, sixty_id, root, cap)
                sixty_row = get_job(connection, sixty_id)
                self.assertEqual(sixty_row["status"], "completed")
                self.assertLessEqual(sixty_row["actual_size"], int(sixty_row["source_size"] * .40))
                self.assertEqual(digest(source), before)
                if cap.qsv_usable and "hevc_qsv" in cap.encoders:
                    for reduction in (30, 60):
                        intel_id = queue_video(connection, root, library_id, video_id,
                                               Profile(encoder="hevc_qsv", target_reduction_pct=reduction,
                                                       target_passes=1))
                        run_analysis(connection, intel_id, root, cap, 2)
                        analysis_details = json.loads(get_job(connection, intel_id)["sample_quality_details_json"])
                        self.assertGreater(analysis_details["samples"][0]["encode_elapsed_seconds"], 0)
                        run_encode(connection, intel_id, root, cap)
                        intel_row = get_job(connection, intel_id)
                        intel_details = json.loads(intel_row["sample_quality_details_json"])
                        self.assertEqual(intel_row["status"], "completed")
                        self.assertEqual(intel_details["actual_encoder"], "hevc_qsv")
                        self.assertEqual(intel_details["actual_method"], "one_pass_target")
                        self.assertLessEqual(intel_row["actual_size"],
                                             int(intel_row["source_size"] * (1 - reduction / 100)))
                        self.assertEqual(digest(source), before)
            finally:
                connection.close()

    def test_interrupted_encode_resumes_verified_chunk_after_reopen(self) -> None:
        cap = probe_capabilities(test_nvenc=False)
        if "libx264" not in cap.encoders:
            self.skipTest("Test FFmpeg lacks libx264")
        with tempfile.TemporaryDirectory(prefix="vm-resume-synthetic-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            source = root / "synthetic.mp4"
            result = subprocess.run(
                [cap.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "testsrc2=size=128x72:rate=24", "-t", "7", "-c:v", "libx264",
                 "-crf", "0", "-preset", "ultrafast", "-y", str(source)],
                capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr[-500:])
            original = digest(source)
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                video_id = connection.execute("SELECT id FROM videos").fetchone()[0]
                job_id = queue_video(connection, root, library_id, video_id,
                                     Profile(encoder="libx264", target_passes=1,
                                             target_reduction_pct=10, speed="ultrafast"))
                update_job(connection, job_id, status="analyzed",
                           estimated_size_mid=source.stat().st_size * 2)
                checkpoint = root / ".videomanager" / "compression-staging" / job_id / "checkpoint.json"

                class CloseAfterOneChunk:
                    def __call__(self):
                        return checkpoint.is_file() and bool(json.loads(
                            checkpoint.read_text(encoding="utf-8"))["done"])

                    def preserve_checkpoint_requested(self):
                        return True

                with patch("videomanager.compression.worker._chunk_windows",
                           return_value=[(0.0, 3.5), (3.5, 3.5)]):
                    with self.assertRaises(CompressionInterrupted) as interrupted:
                        run_encode(connection, job_id, root, cap, CloseAfterOneChunk())
                    fail_job(connection, job_id, interrupted.exception)
                self.assertTrue(has_resume_checkpoint(root, job_id))
                self.assertEqual(json.loads(checkpoint.read_text(encoding="utf-8"))["done"], [0])
                self.assertEqual(digest(source), original)
            finally:
                connection.close()
            connection = open_database(paths.database, paths.backups)
            try:
                self.assertTrue(resume_job(connection, root, job_id))
                import videomanager.compression.worker as compression_worker
                original_command = compression_worker._target_command
                starts = []

                def recorded_command(*args, **kwargs):
                    starts.append(kwargs.get("start"))
                    return original_command(*args, **kwargs)

                with patch("videomanager.compression.worker._chunk_windows",
                           return_value=[(0.0, 3.5), (3.5, 3.5)]), patch(
                               "videomanager.compression.worker._target_command", side_effect=recorded_command):
                    run_encode(connection, job_id, root, cap)
                self.assertEqual(starts[0], 3.5)
                # A size retry may then re-encode both chunks at a lower bitrate.
                self.assertEqual(get_job(connection, job_id)["status"], "completed")
                self.assertEqual(digest(source), original)
                self.assertFalse(checkpoint.exists())
            finally:
                connection.close()

    def test_replacement_preserves_extra_mp4_subtitle_stream(self) -> None:
        cap = probe_capabilities(test_nvenc=False)
        if "libx264" not in cap.encoders:
            self.skipTest("Test FFmpeg lacks libx264")
        with tempfile.TemporaryDirectory(prefix="vm-stream-synthetic-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            plain = base / "plain.mp4"
            source = root / "with-data.mp4"
            payload = base / "payload.srt"
            payload.write_text("1\n00:00:00,500 --> 00:00:02,500\nSynthetic subtitle\n", encoding="utf-8")
            generated = subprocess.run(
                [cap.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "testsrc2=size=128x72:rate=24", "-t", "3", "-c:v", "libx264", "-y", str(plain)],
                capture_output=True, timeout=60)
            self.assertEqual(generated.returncode, 0, generated.stderr[-500:])
            muxed = subprocess.run(
                [cap.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(plain),
                 "-i", str(payload), "-map", "0:v:0", "-map", "1:s:0",
                 "-c:v", "copy", "-c:s", "mov_text", "-y", str(source)],
                capture_output=True, timeout=60)
            self.assertEqual(muxed.returncode, 0, muxed.stderr[-500:])
            self.assertEqual(sum(s["codec_type"] == "subtitle" for s in _probe(cap.ffprobe, source)["streams"]), 1)
            original = digest(source)
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                video_id = connection.execute("SELECT id FROM videos").fetchone()[0]
                job_id = queue_video(connection, root, library_id, video_id, Profile(encoder="libx264"))
                output = root / ".videomanager" / "compressed" / "synthetic-copy.mp4"
                output.parent.mkdir(parents=True)
                shutil.copy2(plain, output)
                with connection:
                    connection.execute(
                        "UPDATE compression_jobs SET status='completed', output_rel_path=?, "
                        "sample_quality_details_json=? WHERE id=?",
                        (output.relative_to(root).as_posix(), json.dumps({"extra_streams": True}), job_id))
                preserved = replace_original(connection, root, paths.cache, job_id, cap)
                self.assertEqual(digest(preserved), original)
                self.assertEqual(get_job(connection, job_id)["status"], "replaced")
                self.assertEqual(sum(s["codec_type"] == "subtitle" for s in _probe(cap.ffprobe, source)["streams"]), 1)
            finally:
                connection.close()

    def test_two_pass_chunk_concat_keeps_audio_and_full_duration(self) -> None:
        cap = probe_capabilities(test_nvenc=False)
        if "libx264" not in cap.encoders:
            self.skipTest("Test FFmpeg lacks libx264")
        with tempfile.TemporaryDirectory(prefix="vm-two-pass-chunks-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            source = root / "synthetic-audio.mp4"
            result = subprocess.run(
                [cap.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "testsrc2=size=128x72:rate=24", "-f", "lavfi", "-i", "sine=frequency=440",
                 "-t", "6", "-c:v", "libx264", "-crf", "8", "-preset", "ultrafast",
                 "-c:a", "aac", "-y", str(source)], capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr[-500:])
            before = digest(source)
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                video_id = connection.execute("SELECT id FROM videos").fetchone()[0]
                job_id = queue_video(connection, root, library_id, video_id,
                                     Profile(encoder="libx264", target_passes=2,
                                             target_reduction_pct=10))
                update_job(connection, job_id, status="analyzed",
                           estimated_size_mid=source.stat().st_size * 2)
                with patch("videomanager.compression.worker._chunk_windows",
                           return_value=[(0.0, 3.0), (3.0, 3.0)]):
                    run_encode(connection, job_id, root, cap)
                row = get_job(connection, job_id)
                self.assertEqual(row["status"], "completed")
                output = _probe(cap.ffprobe, root / row["output_rel_path"])
                self.assertTrue(output["audio"])
                self.assertAlmostEqual(output["duration"], 6, delta=.5)
                self.assertEqual(digest(source), before)
            finally:
                connection.close()

    def test_lgpl_cpu_fallback_encoders_can_make_disposable_copy(self) -> None:
        cap = probe_capabilities(test_nvenc=False)
        available = [name for name in ("libkvazaar", "libopenh264") if name in cap.encoders]
        if not available:
            self.skipTest("Test FFmpeg lacks LGPL-compatible CPU encoders")
        with tempfile.TemporaryDirectory(prefix="vm-lgpl-synthetic-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            source = root / "source.mp4"
            result = subprocess.run(
                [cap.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "testsrc2=size=128x72:rate=24", "-t", "3", "-c:v", "mpeg4",
                 "-q:v", "1", "-y", str(source)], capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr[-500:])
            before = digest(source)
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                video_id = connection.execute("SELECT id FROM videos").fetchone()[0]
                for encoder in available:
                    job_id = queue_video(connection, root, library_id, video_id,
                                         Profile(encoder=encoder, target_passes=1,
                                                 target_reduction_pct=10, speed="ultrafast"))
                    run_analysis(connection, job_id, root, cap, 2)
                    run_encode(connection, job_id, root, cap)
                    self.assertEqual(get_job(connection, job_id)["status"], "completed")
                    self.assertEqual(digest(source), before)
                if "libopenh264" in cap.encoders:
                    edit_id = create_edit(connection, root, library_id, video_id, 1, 2)
                    edited = run_edit(connection, edit_id, root, cap)
                    self.assertAlmostEqual(_probe(cap.ffprobe, edited)["duration"], 2, delta=.5)
                    self.assertEqual(digest(source), before)
            finally:
                connection.close()

    def test_group_add_remove_and_empty_tag_batch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-compression-group-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            for name in ("a.mp4", "b.mp4", "c.mp4", "d.mp4", "e.mp4"):
                (root / name).write_bytes(b"disposable")
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            synchronize(connection, library_id, root, scan_library(root, library_id))
            ids = [row[0] for row in connection.execute("SELECT id FROM videos ORDER BY file_name")]
            group = create_video_group(connection, library_id, "組", ids[:2])
            tag_group = repo.create_tag_group(connection, "類型")
            tag = repo.create_tag(connection, tag_group, "測試")
            repo.save_video_details(connection, ids[0], title="a", notes="整組備註", rating=3,
                                    is_favorite=False, recorded_at=None, status="done", tag_ids={tag})
            self.assertEqual(add_group_members(connection, group, [ids[2]]), 1)
            self.assertEqual(len(group_members(connection, group)), 3)
            self.assertEqual(repo.video_by_id(connection, ids[2])["notes"], "整組備註")
            self.assertEqual(repo.video_tag_ids(connection, ids[2]), {tag})
            self.assertFalse(remove_group_member(connection, group, ids[2]))
            self.assertEqual(repo.video_tag_ids(connection, ids[2]), {tag})
            self.assertTrue((root / "c.mp4").is_file())
            self.assertTrue(remove_group_member(connection, group, ids[1]))
            self.assertEqual(len(group_members(connection, group)), 0)
            connection.close()

            # Only two untagged single videos may enter right-pane batch mode.
            with patch.object(MainWindow, "start_scan"):
                window = MainWindow(paths)
            try:
                window.show()
                self.app.processEvents()
                indexes = [next(index for index, row in enumerate(window.model.rows) if row["id"] == identifier)
                           for identifier in (ids[3], ids[4])]
                selection = window.browser.selectionModel()
                for index in indexes:
                    selection.select(window.model.index(index), QItemSelectionModel.Select)
                window.show_selected_details()
                self.assertEqual(set(window.details.batch_ids), {ids[3], ids[4]})
                window.details.tags.set_selected({tag})
                self.assertTrue(window.details.save_current())
                for identifier in (ids[3], ids[4]):
                    self.assertEqual(repo.video_tag_ids(window.connection, identifier), {tag})
                    self.assertEqual(repo.video_by_id(window.connection, identifier)["status"], "unprocessed")
            finally:
                window.close()

    def test_real_analysis_copy_and_confirmed_replace_on_disposable_mp4(self) -> None:
        cap = probe_capabilities(test_nvenc=False)
        if "libx265" not in cap.encoders or "libx264" not in cap.encoders:
            self.skipTest("Test FFmpeg lacks libx265/libx264")
        with tempfile.TemporaryDirectory(prefix="vm-compression-media-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            source = root / "中文 測試.mp4"
            result = subprocess.run(
                [cap.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "testsrc2=size=128x72:rate=24", "-f", "lavfi", "-i", "sine=frequency=400",
                 "-t", "5", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-y", str(source)],
                capture_output=True, timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertEqual(result.returncode, 0, result.stderr[-500:])
            before = digest(source)
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                video_id = connection.execute("SELECT id FROM videos").fetchone()[0]
                tag_group = repo.create_tag_group(connection, "測試類型")
                tag = repo.create_tag(connection, tag_group, "保留")
                repo.save_video_details(connection, video_id, title="我的影片", notes="私人備註",
                                        rating=4, is_favorite=True, recorded_at=None, status="done",
                                        tag_ids={tag})
                profile = Profile(encoder="libx265", quality=30, speed="ultrafast")
                job_id = queue_video(connection, root, library_id, video_id, profile)
                run_analysis(connection, job_id, root, cap, 2)
                analyzed = get_job(connection, job_id)
                self.assertEqual(analyzed["status"], "analyzed")
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM compression_samples WHERE job_id=?", (job_id,)
                ).fetchone()[0], 2)
                self.assertIsNotNone(analyzed["estimated_size_mid"])
                details = json.loads(analyzed["sample_quality_details_json"])
                self.assertGreater(int(details["source_video_bitrate"]), 0)
                self.assertEqual(details["target_size_bytes"], int(analyzed["source_size"] * .90))
                self.assertGreater(details["target_video_bitrate"], 0)
                if cap.vmaf:
                    self.assertEqual(analyzed["sample_quality_metric"], "vmaf")
                self.assertEqual(digest(source), before)
                run_encode(connection, job_id, root, cap)
                completed = get_job(connection, job_id)
                self.assertEqual(completed["status"], "completed")
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM compression_samples WHERE job_id=?", (job_id,)
                ).fetchone()[0], 0)
                self.assertFalse((root / ".videomanager" / "compression-staging" / job_id).exists())
                self.assertTrue((root / completed["output_rel_path"]).is_file())
                self.assertEqual(digest(source), before)
                self.assertEqual(len(scan_library(root, library_id)), 1)
                compressed = root / completed["output_rel_path"]
                original_rename = Path.rename

                def fail_new_rename(path, destination):
                    if path == compressed:
                        raise OSError("disposable simulated rename failure")
                    return original_rename(path, destination)

                with patch.object(Path, "rename", new=fail_new_rename):
                    with self.assertRaisesRegex(Exception, "simulated rename failure"):
                        replace_original(connection, root, paths.cache, job_id, cap)
                self.assertEqual(digest(source), before)
                self.assertTrue(compressed.is_file())
                self.assertEqual(connection.execute(
                    "SELECT state FROM compression_replacements WHERE job_id=?", (job_id,)
                ).fetchone()[0], "recovered")
                preserved = replace_original(connection, root, paths.cache, job_id, cap)
                self.assertEqual(digest(preserved), before)
                self.assertNotEqual(digest(source), before)
                self.assertEqual(get_job(connection, job_id)["status"], "replaced")
                self.assertEqual(repo.list_videos(connection, library_id)[0]["is_compressed"], 1)
                self.assertTrue((paths.cache / video_id / "00.webp").is_file())
                self.assertEqual(connection.execute(
                    "SELECT state FROM thumbnail_jobs WHERE video_id=?", (video_id,)
                ).fetchone()[0], "done")
                after = repo.video_by_id(connection, video_id)
                self.assertEqual(after["display_title"], "我的影片")
                self.assertEqual(after["notes"], "私人備註")
                self.assertEqual(after["rating"], 4)
                self.assertEqual(after["status"], "done")
                self.assertEqual(repo.video_tag_ids(connection, video_id), {tag})
                center = CompressionCenter(paths, connection, root, library_id)
                center.cap_thread.wait(30000)
                self.app.processEvents()
                self.assertEqual(center.jobs_list.count(), 0)
                self.assertEqual(center.history.rowCount(), 1)
                center.history.selectRow(0)
                self.assertTrue(center.restore_button.isEnabled())
                center.close()
                second_job = queue_video(connection, root, library_id, video_id, Profile())
                second_relative = ".videomanager/compressed/second-copy.mp4"
                second_copy = root / second_relative
                shutil.copy2(source, second_copy)
                with connection:
                    connection.execute(
                        """UPDATE compression_jobs SET status='completed', output_rel_path=?,
                           actual_size=?, actual_reduction_pct=0, sample_quality_details_json='{}'
                           WHERE id=?""",
                        (second_relative, second_copy.stat().st_size, second_job),
                    )
                    connection.execute(
                        """INSERT INTO video_variants
                           (id,source_video_id,library_id,rel_path,size_bytes,created_at)
                           VALUES (?,?,?,?,?,datetime('now'))""",
                        (str(uuid4()), video_id, library_id, second_relative,
                         second_copy.stat().st_size),
                    )
                replace_original(connection, root, paths.cache, second_job, cap)
                self.assertTrue((root / ".videomanager" / "compression-originals" /
                                 f"{second_job}.mp4").is_file())
                restore_original(connection, root, paths.cache, second_job, cap)
                saved_copy = restore_original(connection, root, paths.cache, job_id, cap)
                self.assertEqual(digest(source), before)
                self.assertEqual(repo.list_videos(connection, library_id)[0]["is_compressed"], 0)
                self.assertTrue(saved_copy.is_file())
                self.assertEqual(connection.execute(
                    "SELECT state FROM compression_replacements WHERE job_id=?", (job_id,)
                ).fetchone()[0], "restored")
                self.assertTrue((paths.cache / video_id / "00.webp").is_file())
            finally:
                connection.close()

    def test_compression_center_opens_and_queues_without_reading_mp4(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-compression-ui-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            source = root / "waiting.mp4"
            source.write_bytes(b"not a real video; queue must not decode")
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                video_id = connection.execute("SELECT id FROM videos").fetchone()[0]
                center = CompressionCenter(paths, connection, root, library_id, [video_id])
                try:
                    center.show()
                    center.cap_thread.wait(30000)
                    self.app.processEvents()
                    self.assertIsNotNone(center.capabilities)
                    if center.capabilities.qsv_usable:
                        self.assertEqual(center.encoder.currentData(), "hevc_qsv")
                        self.assertEqual(center.target_passes.currentData(), 1)
                        available = center.capabilities
                        center._capabilities_ready(replace(available, qsv_usable=False))
                        self.assertEqual(center.encoder.currentData(), "libx265")
                        center._capabilities_ready(available)
                    self.assertEqual(len(center.videos.selectedItems()), 1)
                    self.assertTrue(center.only_selected.isChecked())
                    self.assertEqual(center.selection_count.text(), "這次選了 1 部影片")
                    self.assertEqual([center.tabs.tabText(index) for index in range(center.tabs.count())],
                                     ["設定", "試壓縮分析", "目前狀態"])
                    editor = CompressionCenter(paths, connection, root, library_id,
                                               [video_id], edit_only=True)
                    try:
                        editor.show()
                        editor.cap_thread.wait(30000)
                        self.app.processEvents()
                        editor.focus_cut(video_id)
                        self.assertEqual(editor.tabs.tabText(0), "簡易剪輯")
                        self.assertEqual(editor.cut_videos.currentData(), video_id)
                    finally:
                        editor.close()
                    self.assertEqual(center.target_reduction.maximum(), 60)
                    center.target_reduction.setValue(60)
                    center.queue_selection()
                    self.assertEqual(center.jobs_list.count(), 1)
                    self.assertEqual(center._selected_id(), connection.execute(
                        "SELECT id FROM compression_jobs"
                    ).fetchone()[0])
                    self.assertEqual(connection.execute(
                        "SELECT status FROM compression_jobs"
                    ).fetchone()[0], "queued")
                    snapshot = connection.execute(
                        "SELECT profile_snapshot_json FROM compression_jobs"
                    ).fetchone()[0]
                    self.assertEqual(json.loads(snapshot)["target_reduction_pct"], 60)
                    self.assertEqual(source.read_bytes(), b"not a real video; queue must not decode")
                finally:
                    center.close()
            finally:
                connection.close()

    def test_simple_cut_copy_then_confirmed_replace(self) -> None:
        cap = probe_capabilities(test_nvenc=False)
        if "libx264" not in cap.encoders:
            self.skipTest("Test FFmpeg lacks libx264")
        self.assertEqual(parse_time("1:02:03.5"), 3723.5)
        with self.assertRaises(ValueError):
            parse_time("1:99")
        with tempfile.TemporaryDirectory(prefix="vm-cut-media-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            source = root / "要剪輯.mp4"
            result = subprocess.run(
                [cap.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "testsrc2=size=128x72:rate=24", "-f", "lavfi", "-i", "sine=frequency=440",
                 "-t", "5", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-y", str(source)],
                capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertEqual(result.returncode, 0, result.stderr[-500:])
            before = digest(source)
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id))
                video_id = connection.execute("SELECT id FROM videos").fetchone()[0]
                group = repo.create_tag_group(connection, "類型")
                tag = repo.create_tag(connection, group, "保留")
                repo.save_video_details(connection, video_id, title="剪輯測試", notes="原備註", rating=3,
                                        is_favorite=False, recorded_at=None, status="done", tag_ids={tag})
                edit_id = create_edit(connection, root, library_id, video_id, 1, 2)
                copy = run_edit(connection, edit_id, root, cap)
                self.assertTrue(copy.is_file())
                self.assertEqual(digest(source), before)
                self.assertEqual(get_edit(connection, edit_id)["status"], "completed")
                self.assertAlmostEqual(get_edit(connection, edit_id)["output_duration"], 4, delta=0.5)
                tail_id = create_edit(connection, root, library_id, video_id, 4, 5)
                tail_copy = run_edit(connection, tail_id, root, cap)
                self.assertTrue(tail_copy.is_file())
                self.assertAlmostEqual(get_edit(connection, tail_id)["output_duration"], 4, delta=0.5)
                self.assertEqual(len(scan_library(root, library_id)), 1)
                preserved = replace_edit(connection, root, paths.cache, edit_id, cap)
                self.assertEqual(digest(preserved), before)
                self.assertNotEqual(digest(source), before)
                self.assertEqual(get_edit(connection, edit_id)["status"], "replaced")
                row = repo.video_by_id(connection, video_id)
                self.assertEqual(row["display_title"], "剪輯測試")
                self.assertEqual(row["notes"], "原備註")
                self.assertEqual(repo.video_tag_ids(connection, video_id), {tag})
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
