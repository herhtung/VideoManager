from __future__ import annotations

import json
from importlib import resources
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from videomanager.config import AppPaths, Settings, load_settings, save_settings
from videomanager.services.backup_service import export_backup, restore_backup
from videomanager.db.database import DatabaseError, backup_database, normalize_relative_path, open_database
from videomanager.services.library_locator import (
    LibraryAmbiguousError,
    MarkerError,
    create_marker,
    drive_letter_candidates,
    locate_library,
    marker_path,
    read_marker,
)
from videomanager.services.library_service import open_library, reopen_last_library, reset_library_root


class P0Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="vm-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "影片庫 (測試)"
        (self.root / "子資料夾").mkdir(parents=True)
        (self.root / "子資料夾" / "片.mp4").write_bytes(b"sample only")
        self.paths = AppPaths(self.base / "local-data")

    def test_marker_creation_is_exclusive_and_does_not_change_video(self) -> None:
        video = self.root / "子資料夾" / "片.mp4"
        before = (video.stat().st_size, video.stat().st_mtime_ns, video.read_bytes())
        marker = create_marker(self.root)
        self.assertEqual(marker, read_marker(self.root))
        self.assertEqual(marker.schema_version, 1)
        self.assertEqual(before, (video.stat().st_size, video.stat().st_mtime_ns, video.read_bytes()))
        with self.assertRaises(MarkerError):
            create_marker(self.root)
        self.assertEqual(marker, read_marker(self.root))

    def test_invalid_marker_refused_without_overwrite(self) -> None:
        path = marker_path(self.root)
        path.parent.mkdir()
        path.write_text('{"library_id":"invalid"}', encoding="utf-8")
        with self.assertRaises(MarkerError):
            open_library(self.paths, self.root, create=True)
        self.assertEqual(path.read_text(encoding="utf-8"), '{"library_id":"invalid"}')

    def test_database_reopens_and_keeps_same_library_id(self) -> None:
        first = open_library(self.paths, self.root, create=True)
        second = reopen_last_library(self.paths)
        self.assertIsNotNone(second)
        self.assertEqual(first.marker.library_id, second.marker.library_id)
        settings = load_settings(self.paths.settings)
        self.assertEqual(settings.library_id, first.marker.library_id)
        with closing(open_database(self.paths.database, self.paths.backups)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 8)
            self.assertEqual(connection.execute("SELECT id FROM libraries").fetchone()[0], first.marker.library_id)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_existing_v1_database_migrates_with_backup(self) -> None:
        self.paths.database.parent.mkdir(parents=True, exist_ok=True)
        migration = resources.files("videomanager.db").joinpath("migrations/001_initial.sql").read_text(encoding="utf-8")
        with closing(sqlite3.connect(self.paths.database)) as old:
            old.executescript(migration)
            old.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-01-01')")
            old.commit()
        with closing(open_database(self.paths.database, self.paths.backups)) as upgraded:
            self.assertEqual(upgraded.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 8)
            self.assertIn("unique_category_id", [row[1] for row in upgraded.execute("PRAGMA table_info(videos)")])
        self.assertEqual(len(list(self.paths.backups.glob("*.db"))), 1)

    def test_old_classifications_become_tags_without_moving_video(self) -> None:
        self.paths.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.paths.database)) as old:
            for version, filename in enumerate((
                "001_initial.sql", "002_unique_categories.sql", "003_video_groups.sql",
            ), 1):
                sql = resources.files("videomanager.db").joinpath(f"migrations/{filename}").read_text(encoding="utf-8")
                old.executescript(sql)
                old.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, '2026-01-01')", (version,))
            old.execute("INSERT INTO libraries VALUES ('library', 'test', 'X:/library', 1, '2026-01-01')")
            old.execute("INSERT INTO unique_categories(id, name, folder_name) VALUES ('unique', '精選', '精選')")
            old.execute("INSERT INTO categories(id, name) VALUES ('category', '旅行')")
            old.execute("""INSERT INTO videos(id, library_id, relative_path, path_key, file_name,
                display_title, file_size, mtime_ns, added_at, unique_category_id)
                VALUES ('video', 'library', '唯一分類/精選/old.mp4',
                '唯一分類/精選/old.mp4', 'old.mp4', 'old', 1, 1, '2026-01-01', 'unique')""")
            old.execute("INSERT INTO video_categories(video_id, category_id) VALUES ('video', 'category')")
            old.execute(
                "INSERT INTO saved_filters(id, name, filter_json, created_at) VALUES (?, ?, ?, ?)",
                ("filter", "舊分類篩選", json.dumps({"unique_category_id": "unique", "tag_all": ["previous"]}), "2026-01-01"),
            )
            old.commit()
        with closing(open_database(self.paths.database, self.paths.backups)) as upgraded:
            row = upgraded.execute("SELECT relative_path, unique_category_id FROM videos WHERE id = 'video'").fetchone()
            self.assertEqual(tuple(row), ("唯一分類/精選/old.mp4", None))
            names = {row[0] for row in upgraded.execute(
                "SELECT t.name FROM video_tags vt JOIN tags t ON t.id = vt.tag_id WHERE vt.video_id = 'video'"
            )}
            self.assertEqual(names, {"旅行", "精選"})
            saved = json.loads(upgraded.execute("SELECT filter_json FROM saved_filters WHERE id = 'filter'").fetchone()[0])
            self.assertNotIn("unique_category_id", saved)
            self.assertEqual(saved["tag_all"], ["previous", "migrated-unique-unique"])
        self.assertEqual(len(list(self.paths.backups.glob("*.db"))), 1)

    def test_offline_keeps_database_untouched(self) -> None:
        open_library(self.paths, self.root, create=True)
        settings = load_settings(self.paths.settings)
        save_settings(self.paths.settings, Settings(library_id=settings.library_id, last_root_path=str(self.base / "offline")))
        self.assertIsNone(reopen_last_library(self.paths))
        with closing(open_database(self.paths.database, self.paths.backups)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM libraries").fetchone()[0], 1)

    def test_second_library_cannot_bind_to_first_database(self) -> None:
        original = open_library(self.paths, self.root, create=True)
        other = self.base / "other"
        other.mkdir()
        create_marker(other)
        with self.assertRaises(DatabaseError):
            open_library(self.paths, other)
        self.assertEqual(load_settings(self.paths.settings).library_id, original.marker.library_id)

    def test_reset_to_empty_root_backs_up_index_and_preserves_original_video(self) -> None:
        opened = open_library(self.paths, self.root, create=True)
        source = self.root / "子資料夾" / "片.mp4"
        new_root = self.base / "new-managed-root"
        new_root.mkdir()
        with closing(open_database(self.paths.database, self.paths.backups)) as connection:
            with connection:
                connection.execute(
                    """INSERT INTO videos(id, library_id, relative_path, path_key, file_name,
                       display_title, file_size, mtime_ns, added_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid4()), opened.marker.library_id, "子資料夾/片.mp4", "子資料夾/片.mp4",
                     "片.mp4", "片", source.stat().st_size, source.stat().st_mtime_ns,
                     "2026-10-14T00:00:00+00:00"),
                )
        replacement, backup = reset_library_root(self.paths, self.root, new_root)
        self.assertNotEqual(replacement.marker.library_id, opened.marker.library_id)
        self.assertEqual(read_marker(self.root), opened.marker)
        self.assertEqual(read_marker(new_root), replacement.marker)
        self.assertEqual(source.read_bytes(), b"sample only")
        self.assertEqual(load_settings(self.paths.settings).last_root_path, str(new_root))
        with closing(sqlite3.connect(backup)) as saved:
            self.assertEqual(saved.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 1)
        with closing(open_database(self.paths.database, self.paths.backups)) as current:
            self.assertEqual(current.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 0)
            self.assertEqual(current.execute("SELECT id FROM libraries").fetchone()[0], replacement.marker.library_id)

    def test_reset_rejects_nonempty_or_nested_root_without_changing_library(self) -> None:
        opened = open_library(self.paths, self.root, create=True)
        nonempty = self.base / "nonempty"
        nonempty.mkdir()
        (nonempty / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(MarkerError):
            reset_library_root(self.paths, self.root, nonempty)
        with self.assertRaises(MarkerError):
            reset_library_root(self.paths, self.root, self.root / "子資料夾")
        self.assertEqual(load_settings(self.paths.settings).library_id, opened.marker.library_id)
        self.assertEqual((nonempty / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_explicit_location_and_ambiguity(self) -> None:
        marker = create_marker(self.root)
        copy = self.base / "copy"
        marker_path(copy).parent.mkdir(parents=True)
        marker_path(copy).write_text(marker_path(self.root).read_text(encoding="utf-8"), encoding="utf-8")
        self.assertEqual(locate_library(marker.library_id, [self.base / "missing", self.root]), self.root)
        with self.assertRaises(LibraryAmbiguousError):
            locate_library(marker.library_id, [self.root, copy])

    def test_drive_candidates_only_check_same_relative_folder(self) -> None:
        candidates = drive_letter_candidates(Path("E:/Movies/影片庫"), ["E", "F"])
        self.assertIn(Path("F:/Movies/影片庫"), candidates)
        self.assertEqual(len(candidates), 3)

    def test_schema_constraints_and_casefold_path(self) -> None:
        opened = open_library(self.paths, self.root, create=True)
        relative, key = normalize_relative_path("子資料夾\\片.MP4")
        self.assertEqual(relative, "子資料夾/片.MP4")
        self.assertEqual(key, "子資料夾/片.mp4")
        with self.assertRaises(ValueError):
            normalize_relative_path("../outside.mp4")
        with closing(open_database(self.paths.database, self.paths.backups)) as connection:
            fields = (str(uuid4()), opened.marker.library_id, relative, key, "片.MP4", "片", 1, 1, "2026-10-14T00:00:00+00:00")
            connection.execute(
                """INSERT INTO videos(id, library_id, relative_path, path_key, file_name,
                   display_title, file_size, mtime_ns, added_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                fields,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO videos(id, library_id, relative_path, path_key, file_name,
                       display_title, file_size, mtime_ns, added_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (str(uuid4()), *fields[1:3], key, *fields[4:]),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO videos(id, library_id, relative_path, path_key, file_name,
                       display_title, file_size, mtime_ns, added_at, status) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid4()), opened.marker.library_id, "bad.mp4", "bad.mp4", "bad.mp4", "bad", 1, 1, fields[-1], "missing"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO video_tags(video_id, tag_id) VALUES (?, ?)", (str(uuid4()), str(uuid4())))

    def test_backup_api_preserves_committed_rows(self) -> None:
        opened = open_library(self.paths, self.root, create=True)
        connection = open_database(self.paths.database, self.paths.backups)
        try:
            saved = backup_database(connection, self.paths.backups)
        finally:
            connection.close()
        with closing(sqlite3.connect(saved)) as snapshot:
            self.assertEqual(snapshot.execute("SELECT id FROM libraries").fetchone()[0], opened.marker.library_id)

    def test_unrecognized_existing_database_is_not_migrated(self) -> None:
        self.paths.database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.paths.database)) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
        with self.assertRaises(DatabaseError):
            open_database(self.paths.database, self.paths.backups)
        with closing(sqlite3.connect(self.paths.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM unrelated").fetchone()[0], 0)

    def test_export_and_restore_preserve_library_binding(self) -> None:
        opened = open_library(self.paths, self.root, create=True)
        saved = export_backup(self.paths, self.base / "export.db")
        with closing(open_database(self.paths.database, self.paths.backups)) as connection:
            with connection:
                connection.execute("UPDATE libraries SET name = 'changed' WHERE id = ?", (opened.marker.library_id,))
        previous = restore_backup(self.paths, saved)
        self.assertTrue(previous.is_file())
        with closing(open_database(self.paths.database, self.paths.backups)) as connection:
            row = connection.execute("SELECT id, name FROM libraries").fetchone()
            self.assertEqual(row[0], opened.marker.library_id)
            self.assertNotEqual(row[1], "changed")


if __name__ == "__main__":
    unittest.main()
