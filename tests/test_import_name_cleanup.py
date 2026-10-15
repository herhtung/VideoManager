"""Configured phrases are removed once during batch import planning."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from videomanager.config import AppPaths, Settings, load_settings, save_settings
from videomanager.db import repository as repo
from videomanager.db.database import open_database
from videomanager.services.existing_name_cleanup import apply_existing_name_changes, scan_existing_names
from videomanager.services.import_service import cleaned_import_name, plan_group_import, plan_import
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.services.video_group_service import create_video_group


class ImportNameCleanupTests(unittest.TestCase):
    def test_regular_and_group_plans_show_clean_destinations_and_resolve_duplicates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-import-clean-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            source = base / "source"
            source.mkdir()
            paths = AppPaths(base / "data")
            save_settings(paths.settings, Settings(import_remove_phrases=("ABC",)))
            marker = open_library(paths, root, create=True).marker
            self.assertEqual(load_settings(paths.settings).import_remove_phrases, ("ABC",))
            videos = [source / name for name in ("ABC - 01.mp4", "abc_01.mp4", "ABC - 02.mp4")]
            for video in videos:
                video.write_bytes(b"disposable test video")
            planned = plan_import(root, marker.library_id, videos, month="2026-10",
                                  remove_phrases=("ABC",))
            self.assertEqual([item.destination.name for item in planned],
                             ["01.mp4", "01 (2).mp4", "02.mp4"])
            group = plan_group_import(root, marker.library_id, videos, month="2026-10",
                                      remove_phrases=("ABC",))
            self.assertEqual(group[0].destination.parent.name, "01")
            self.assertEqual([item.destination.name for item in group],
                             ["01.mp4", "01 (2).mp4", "02.mp4"])
            self.assertEqual(cleaned_import_name(Path("abc.MP4"), ("ABC",)), "未命名影片.MP4")
            self.assertEqual(
                cleaned_import_name(
                    Path("這雙腳極品出全集 - 免費自拍A片性愛AV情色成人影片線上看 - 色色啦.mp4"),
                    ("免費自拍A片性愛AV情色成人影片線上看", "色色啦"),
                ),
                "這雙腳極品出全集.mp4",
            )

    def test_existing_files_preview_then_rename_without_losing_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-existing-name-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            for name in ("ABC - one.mp4", "one.mp4", "ABC - two.mp4", "ABC - three.mp4"):
                (root / name).write_bytes(f"disposable {name}".encode())
            paths = AppPaths(base / "data")
            marker = open_library(paths, root, create=True).marker
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, marker.library_id, root, scan_library(root, marker.library_id))
                ids = {row["file_name"]: row["id"] for row in connection.execute("SELECT id, file_name FROM videos")}
                group = repo.create_tag_group(connection, "分類")
                tag = repo.create_tag(connection, group, "測試")
                repo.set_video_relations(connection, ids["ABC - two.mp4"], tag_ids={tag})
                repo.update_video(connection, ids["ABC - three.mp4"], display_title="自訂名稱")
                plan = scan_existing_names(connection, marker.library_id, root, ("ABC",))
                self.assertEqual(len(plan.changes), 2)
                self.assertEqual(len(plan.skipped), 1)
                self.assertIn("已存在", plan.skipped[0][1])
                self.assertTrue((root / "ABC - two.mp4").exists())
                success, groups, failures = apply_existing_name_changes(connection, marker.library_id, root, plan.changes)
                self.assertEqual((success, groups, failures), (2, 0, []))
                self.assertTrue((root / "two.mp4").exists())
                self.assertTrue((root / "three.mp4").exists())
                self.assertTrue((root / "ABC - one.mp4").exists())
                two = repo.video_by_id(connection, ids["ABC - two.mp4"])
                three = repo.video_by_id(connection, ids["ABC - three.mp4"])
                self.assertEqual((two["file_name"], two["display_title"]), ("two.mp4", "two"))
                self.assertEqual((three["file_name"], three["display_title"]), ("three.mp4", "自訂名稱"))
                self.assertEqual(repo.video_tag_ids(connection, two["id"]), {tag})
            finally:
                connection.close()

    def test_existing_file_change_after_preview_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-existing-name-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            (root / "ABC - clip.mp4").write_bytes(b"original")
            paths = AppPaths(base / "data")
            marker = open_library(paths, root, create=True).marker
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, marker.library_id, root, scan_library(root, marker.library_id))
                plan = scan_existing_names(connection, marker.library_id, root, ("ABC",))
                self.assertEqual(len(plan.changes), 1)
                (root / "clip.mp4").write_bytes(b"another video")
                success, groups, failures = apply_existing_name_changes(connection, marker.library_id, root, plan.changes)
                self.assertEqual(success, 0)
                self.assertEqual(groups, 0)
                self.assertEqual(len(failures), 1)
                self.assertEqual((root / "clip.mp4").read_bytes(), b"another video")
                self.assertTrue((root / "ABC - clip.mp4").exists())
            finally:
                connection.close()

    def test_existing_group_name_is_cleaned_even_when_mp4_names_already_clean(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-existing-group-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            for name in ("part1.mp4", "part2.mp4"):
                (root / name).write_bytes(b"disposable test MP4")
            paths = AppPaths(base / "data")
            marker = open_library(paths, root, create=True).marker
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, marker.library_id, root, scan_library(root, marker.library_id))
                ids = [row[0] for row in connection.execute("SELECT id FROM videos ORDER BY file_name")]
                group_id = create_video_group(connection, marker.library_id, "part1 - 廣告", ids)
                plan = scan_existing_names(connection, marker.library_id, root, ("廣告",))
                self.assertEqual(plan.changes, ())
                self.assertEqual([(item.before, item.after) for item in plan.group_changes],
                                 [("part1 - 廣告", "part1")])
                files, groups, failures = apply_existing_name_changes(
                    connection, marker.library_id, root, plan.changes,
                    group_changes=plan.group_changes,
                )
                self.assertEqual((files, groups, failures), (0, 1, []))
                self.assertEqual(connection.execute("SELECT name FROM video_groups WHERE id = ?",
                                                    (group_id,)).fetchone()[0], "part1")
                self.assertTrue((root / "part1.mp4").exists())
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
