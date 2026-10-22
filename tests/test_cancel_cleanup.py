import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from videomanager.compression.profiles import Profile
from videomanager.compression.repository import cancel_waiting_job, get_job, queue_video, update_job
from videomanager.config import AppPaths
from videomanager.db.database import open_database, utc_now
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize


class CancelCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "library"
        self.root.mkdir()
        self.source = self.root / "original.mp4"
        self.source.write_bytes(b"original media")
        paths = AppPaths(self.base / "data")
        self.library = open_library(paths, self.root, create=True).marker.library_id
        self.connection = open_database(paths.database, paths.backups)
        synchronize(self.connection, self.library, self.root, scan_library(self.root, self.library))
        self.video = self.connection.execute("SELECT id FROM videos").fetchone()[0]
        self.job = queue_video(self.connection, self.root, self.library, self.video, Profile())
        self.copy = self.root / ".videomanager" / "compressed" / "copy.mp4"
        self.copy.parent.mkdir(parents=True)
        self.copy.write_bytes(b"completed copy")
        self.relative = self.copy.relative_to(self.root).as_posix()
        update_job(self.connection, self.job, status="completed", output_rel_path=self.relative, actual_size=14)
        with self.connection:
            self.connection.execute(
                "INSERT INTO video_variants (id,source_video_id,library_id,rel_path,size_bytes,created_at) "
                "VALUES (?,?,?,?,?,?)", (str(uuid4()), self.video, self.library, self.relative, 14, utc_now()))
        self.staging = self.root / ".videomanager" / "compression-staging" / self.job
        self.staging.mkdir(parents=True)
        (self.staging / "sample.mp4").write_bytes(b"temporary sample")

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_cancel_deletes_copy_samples_and_variant_but_keeps_original_and_other_copies(self):
        other = self.copy.with_name("other.mp4")
        other.write_bytes(b"unrelated copy")
        self.assertTrue(cancel_waiting_job(self.connection, self.root, self.job))
        self.assertFalse(self.copy.exists())
        self.assertFalse(self.staging.exists())
        self.assertEqual(self.source.read_bytes(), b"original media")
        self.assertEqual(other.read_bytes(), b"unrelated copy")
        row = get_job(self.connection, self.job)
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(row["output_rel_path"])
        self.assertIsNone(row["actual_size"])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM video_variants").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_source_outside_and_redirected_paths_are_rejected(self):
        for relative in ("original.mp4", "../outside.mp4", ".videomanager/compressed/../original.mp4"):
            with self.subTest(path=relative):
                update_job(self.connection, self.job, output_rel_path=relative)
                with self.assertRaises(ValueError):
                    cancel_waiting_job(self.connection, self.root, self.job)
                self.assertEqual(get_job(self.connection, self.job)["status"], "completed")
                self.assertEqual(self.source.read_bytes(), b"original media")
                self.assertTrue(self.copy.exists())
        update_job(self.connection, self.job, output_rel_path=self.relative)
        with patch.object(Path, "is_junction", return_value=True):
            with self.assertRaises(ValueError):
                cancel_waiting_job(self.connection, self.root, self.job)
        self.assertTrue(self.staging.exists())
        self.assertTrue(self.copy.exists())

    def test_locked_copy_keeps_job_and_variant_retryable(self):
        with patch.object(Path, "unlink", side_effect=PermissionError("copy in use")):
            with self.assertRaises(PermissionError):
                cancel_waiting_job(self.connection, self.root, self.job)
        self.assertEqual(get_job(self.connection, self.job)["status"], "completed")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM video_variants").fetchone()[0], 1)
        self.assertTrue(self.copy.exists())
        self.assertTrue(cancel_waiting_job(self.connection, self.root, self.job))
        self.assertFalse(self.copy.exists())
        self.assertEqual(self.source.read_bytes(), b"original media")

    def test_offline_library_or_missing_original_preserves_only_remaining_copy(self):
        with self.assertRaises(ValueError):
            cancel_waiting_job(self.connection, None, self.job)
        self.source.unlink()
        with self.assertRaises(ValueError):
            cancel_waiting_job(self.connection, self.root, self.job)
        self.assertEqual(get_job(self.connection, self.job)["status"], "completed")
        self.assertEqual(self.copy.read_bytes(), b"completed copy")

    def test_copy_referenced_by_another_job_is_not_deleted(self):
        other = queue_video(self.connection, self.root, self.library, self.video, Profile())
        update_job(self.connection, other, status="completed", output_rel_path=self.relative)
        with self.assertRaises(ValueError):
            cancel_waiting_job(self.connection, self.root, self.job)
        self.assertEqual(get_job(self.connection, self.job)["status"], "completed")
        self.assertEqual(self.copy.read_bytes(), b"completed copy")
        self.assertEqual(self.source.read_bytes(), b"original media")
