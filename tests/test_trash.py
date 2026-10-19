from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from videomanager.config import AppPaths
from videomanager.db.database import open_database
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.services.trash_service import empty_trash, list_trashed, restore_video, trash_video


class TrashTests(unittest.TestCase):
    def test_move_playable_restore_and_permanently_clear(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-trash-") as directory:
            base = Path(directory)
            root = base / "library"
            source = root / "匯入" / "2026-10" / "001" / "a.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"disposable test MP4")
            paths = AppPaths(base / "data")
            library_id = open_library(paths, root, create=True).marker.library_id
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, library_id, root, scan_library(root, library_id,
                                                               included_folders=()))
                video_id = connection.execute("SELECT id FROM videos").fetchone()[0]
                cache = paths.cache / video_id
                cache.mkdir(parents=True)
                (cache / "00.webp").write_bytes(b"cache")
                trashed = trash_video(connection, library_id, root, video_id)
                self.assertTrue(trashed.is_file())
                self.assertFalse(source.exists())
                self.assertEqual(len(list_trashed(connection, library_id)), 1)
                self.assertIsNotNone(connection.execute(
                    "SELECT deleted_at FROM videos WHERE id=?", (video_id,)
                ).fetchone()[0])
                restored = restore_video(connection, library_id, root, video_id)
                self.assertEqual(restored, source)
                self.assertEqual(source.read_bytes(), b"disposable test MP4")
                self.assertEqual(len(list_trashed(connection, library_id)), 0)
                copy = root / ".videomanager" / "compressed" / "a_copy.mp4"
                copy.parent.mkdir(parents=True)
                copy.write_bytes(b"private copy")
                with connection:
                    connection.execute(
                        """INSERT INTO video_variants
                           (id,source_video_id,library_id,rel_path,size_bytes,created_at)
                           VALUES (?,?,?,?,?,?)""",
                        (str(uuid4()), video_id, library_id,
                         ".videomanager/compressed/a_copy.mp4", copy.stat().st_size,
                         "2026-10-16T00:00:00+00:00"),
                    )
                trash_video(connection, library_id, root, video_id)
                self.assertEqual(empty_trash(connection, library_id, root, paths.cache), 1)
                self.assertIsNone(connection.execute("SELECT id FROM videos WHERE id=?",
                                                     (video_id,)).fetchone())
                self.assertFalse(source.exists())
                self.assertFalse(copy.exists())
                self.assertFalse((cache / "00.webp").exists())
            finally:
                connection.close()
