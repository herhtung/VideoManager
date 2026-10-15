from __future__ import annotations

import os
import site
import tempfile
import unittest
from pathlib import Path

from videomanager.config import AppPaths
from videomanager.db.database import open_database
from videomanager.services.file_operations import recycle_video
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize


if (Path(__file__).resolve().parent.parent / ".deps").is_dir():
    site.addsitedir(str(Path(__file__).resolve().parent.parent / ".deps"))


@unittest.skipUnless(os.name == "nt", "Windows Recycle Bin test")
class RecycleTests(unittest.TestCase):
    def test_disposable_mp4_goes_to_recycle_bin(self) -> None:
        from send2trash.win import send2trash
        if send2trash.__module__ != "send2trash.win.modern":
            self.skipTest("Modern Windows Recycle Bin backend unavailable")
        with tempfile.TemporaryDirectory(prefix="vm-test-", dir=Path(__file__).resolve().parent) as scratch:
            base = Path(scratch)
            root = base / "library"
            root.mkdir()
            video = root / "disposable.mp4"
            video.write_bytes(b"disposable test only")
            paths = AppPaths(base / "data")
            marker = open_library(paths, root, create=True).marker
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, marker.library_id, root, scan_library(root, marker.library_id))
                video_id = connection.execute("SELECT id FROM videos").fetchone()[0]
                recycle_video(connection, marker.library_id, root, video_id)
                self.assertFalse(video.exists())
                self.assertIsNotNone(connection.execute("SELECT missing_since FROM videos").fetchone()[0])
                self.assertEqual(connection.execute("SELECT state FROM file_operations").fetchone()[0], "done")
            finally:
                connection.close()
