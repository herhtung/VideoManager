from __future__ import annotations

import shutil
import subprocess
import tempfile
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from videomanager.config import AppPaths
from videomanager.db.database import open_database
from videomanager.services.index_service import process_pending_media, retry_thumbnail
from videomanager.services.library_service import open_library
from videomanager.services.media_probe import probe_media
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.services.thumbnail_service import (
    ThumbnailCancelled, ThumbnailError, _run_frame_command, cache_signature,
    cleanup_stale_staging, frame_fractions, generate_thumbnails,
)


class ThumbnailFallbackTests(unittest.TestCase):
    def test_one_failed_frame_uses_nearest_successful_frame(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-thumb-") as directory:
            cache = Path(directory) / "cache"

            def fake_run(command, _cancelled):
                output = Path(command[-1])
                if output.stem == "06":
                    return 1, "decode error"
                output.write_bytes(f"frame-{output.stem}".encode())
                return 0, ""

            with patch("videomanager.services.thumbnail_service._run_frame_command", side_effect=fake_run):
                frames = generate_thumbnails(Path(directory) / "clip.mp4", cache, "sample", 100_000,
                                             ffmpeg="mock-ffmpeg")
            self.assertEqual(len(frames), 10)
            self.assertTrue(all(path.is_file() for path in frames))
            self.assertEqual(frames[6].read_bytes(), frames[5].read_bytes())

    def test_all_failed_frames_still_report_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-thumb-") as directory:
            cache = Path(directory) / "cache"
            with patch("videomanager.services.thumbnail_service._run_frame_command",
                       return_value=(1, "decode error")):
                with self.assertRaises(ThumbnailError):
                    generate_thumbnails(Path(directory) / "clip.mp4", cache, "sample", 100_000,
                                        ffmpeg="mock-ffmpeg")
            self.assertFalse((cache / "sample").exists())

    def test_active_frame_process_stops_when_playback_pauses_indexing(self) -> None:
        started = time.monotonic()
        with self.assertRaises(ThumbnailCancelled):
            _run_frame_command(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                lambda: time.monotonic() - started > 0.15,
            )
        self.assertLess(time.monotonic() - started, 2)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/FFprobe unavailable")
class MediaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="vm-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "library"
        self.root.mkdir()
        self.video = self.root / "color.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=24",
             "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(self.video)],
            check=True, timeout=30,
        )
        self.paths = AppPaths(self.base / "data")
        opened = open_library(self.paths, self.root, create=True)
        self.library_id = opened.marker.library_id
        self.connection = open_database(self.paths.database, self.paths.backups)
        self.addCleanup(self.connection.close)
        synchronize(self.connection, self.library_id, self.root, scan_library(self.root, self.library_id))
        self.row = self.connection.execute("SELECT * FROM videos").fetchone()

    def test_probe_and_three_preview_densities(self) -> None:
        info = probe_media(self.video)
        self.assertGreater(info.duration_ms, 0)
        self.assertEqual((info.width, info.height), (320, 180))
        for count in (10, 30, 60):
            fractions = frame_fractions(count)
            self.assertEqual((len(fractions), round(fractions[0], 2), round(fractions[-1], 2)), (count, 0.05, 0.90))
        done, failed = process_pending_media(self.connection, self.root, self.paths.cache, frame_count=10)
        self.assertEqual((done, failed), (1, 0))
        first_signature = self.connection.execute("SELECT cache_signature FROM thumbnail_jobs").fetchone()[0]
        self.assertEqual(first_signature, cache_signature(self.row["file_size"], self.row["mtime_ns"], 10))
        self.assertEqual(len(list((self.paths.cache / self.row["id"]).glob("*.webp"))), 10)
        retry_thumbnail(self.connection, self.row["id"])
        done, failed = process_pending_media(self.connection, self.root, self.paths.cache, frame_count=30)
        self.assertEqual((done, failed), (1, 0))
        self.assertEqual(len(list((self.paths.cache / self.row["id"]).glob("*.webp"))), 30)
        retry_thumbnail(self.connection, self.row["id"])
        done, failed = process_pending_media(self.connection, self.root, self.paths.cache, frame_count=60)
        self.assertEqual((done, failed), (1, 0))
        self.assertEqual(len(list((self.paths.cache / self.row["id"]).glob("*.webp"))), 60)
        signature = self.connection.execute("SELECT cache_signature FROM thumbnail_jobs").fetchone()[0]
        self.assertEqual(signature, cache_signature(self.row["file_size"], self.row["mtime_ns"], 60))

    def test_corrupt_mp4_does_not_stop_other_index_jobs(self) -> None:
        (self.root / "broken.mp4").write_bytes(b"not a valid MP4")
        synchronize(self.connection, self.library_id, self.root, scan_library(self.root, self.library_id))
        done, failed = process_pending_media(self.connection, self.root, self.paths.cache, frame_count=10)
        self.assertEqual((done, failed), (1, 1))
        states = {row["file_name"]: row["state"] for row in self.connection.execute(
            "SELECT v.file_name, j.state FROM videos v JOIN thumbnail_jobs j ON j.video_id = v.id"
        )}
        self.assertEqual(states, {"color.mp4": "done", "broken.mp4": "error"})

    def test_interrupted_running_job_resumes(self) -> None:
        with self.connection:
            self.connection.execute("UPDATE thumbnail_jobs SET state = 'running' WHERE video_id = ?", (self.row["id"],))
        done, failed = process_pending_media(self.connection, self.root, self.paths.cache, frame_count=10)
        self.assertEqual((done, failed), (1, 0))
        self.assertEqual(len(list((self.paths.cache / self.row["id"]).glob("*.webp"))), 10)

    def test_paused_thumbnail_job_resumes_same_video(self) -> None:
        state = {"paused": False, "calls": 0}

        def fake_generate(*_args, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                state["paused"] = True
                self.assertTrue(kwargs["cancelled"]())
                state["paused"] = False
                raise ThumbnailCancelled("player opened")
            return []

        with patch("videomanager.services.index_service.probe_media", return_value=probe_media(self.video)):
            with patch("videomanager.services.index_service.generate_thumbnails", side_effect=fake_generate):
                done, failed = process_pending_media(
                    self.connection, self.root, self.paths.cache,
                    paused=lambda: state["paused"],
                )
        self.assertEqual((done, failed, state["calls"]), (1, 0, 2))
        job = self.connection.execute("SELECT state, attempts FROM thumbnail_jobs").fetchone()
        self.assertEqual((job["state"], job["attempts"]), ("done", 1))

    def test_first_failed_frame_job_retries_once(self) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE thumbnail_jobs SET state = 'error', attempts = 1,
                   error_message = 'Frame 7 failed: decode error' WHERE video_id = ?""",
                (self.row["id"],),
            )
        done, failed = process_pending_media(self.connection, self.root, self.paths.cache)
        self.assertEqual((done, failed), (1, 0))
        job = self.connection.execute(
            "SELECT state, attempts FROM thumbnail_jobs WHERE video_id = ?", (self.row["id"],)
        ).fetchone()
        self.assertEqual((job["state"], job["attempts"]), ("done", 2))

    def test_old_staging_is_cleaned_without_touching_recent_work(self) -> None:
        old = self.paths.cache / f".{self.row['id']}-{uuid4().hex}.tmp"
        recent = self.paths.cache / f".{self.row['id']}-{uuid4().hex}.tmp"
        old.mkdir(parents=True)
        recent.mkdir(parents=True)
        (old / "00.webp").write_bytes(b"unfinished")
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        self.assertEqual(cleanup_stale_staging(self.paths.cache), 1)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_h265_mp4_can_be_probed_and_cached(self) -> None:
        hevc = self.root / "hevc.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-f", "lavfi", "-i", "color=c=green:s=160x90:r=12", "-t", "1",
             "-c:v", "libx265", "-x265-params", "pools=1:frame-threads=1:log-level=error",
             "-tag:v", "hvc1", "-threads", "1", "-y", str(hevc)],
            check=True, timeout=30,
        )
        self.assertEqual(probe_media(hevc).video_codec, "hevc")
        synchronize(self.connection, self.library_id, self.root, scan_library(self.root, self.library_id))
        done, failed = process_pending_media(self.connection, self.root, self.paths.cache, frame_count=10)
        self.assertEqual((done, failed), (2, 0))


if __name__ == "__main__":
    unittest.main()
