from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from videomanager.config import AppPaths
from videomanager.services.cleanup_service import cleanup_missing_records
from videomanager.db.database import open_database
from videomanager.db.repository import (
    category_delete_impact, create_category, create_tag, create_tag_group,
    create_unique_category, delete_category, list_videos, merge_tags,
    set_video_relations, update_video,
)
from videomanager.services.file_operations import FileOperationError, move_video, recover_file_operations
from videomanager.services.library_service import open_library
from videomanager.services.import_service import ImportCancelled, import_mp4, plan_group_import, plan_import
from videomanager.services.video_group_service import create_group_in_folder, expand_group_ids, group_members
from videomanager.services.scanner import ScanCancelled, ScanError, scan_library
from videomanager.services.sync_service import synchronize
from videomanager.services.thumbnail_service import frame_fractions
from videomanager.services.unique_category_service import assign_unique_category, delete_unique_category
from videomanager.ui.index_worker import IndexWorker


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="vm-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "library"
        (self.root / "nested").mkdir(parents=True)
        self.video_path = self.root / "nested" / "A.mp4"
        self.video_path.write_bytes(b"not a real video")
        self.paths = AppPaths(self.base / "data")
        self.opened = open_library(self.paths, self.root, create=True)
        self.library_id = self.opened.marker.library_id
        self._marker_json = (self.root / ".videomanager" / "library.json").read_text(encoding="utf-8")
        self.connection = open_database(self.paths.database, self.paths.backups)
        self.addCleanup(self.connection.close)

    def sync(self):
        return synchronize(
            self.connection, self.opened.marker.library_id, self.root,
            scan_library(self.root, self.opened.marker.library_id),
        )

    def first_video(self):
        return self.connection.execute("SELECT * FROM videos").fetchone()

    def test_large_library_lists_videos_without_starting_preview_jobs(self) -> None:
        for index in range(100):
            (self.root / "nested" / f"{index:03}.mp4").write_bytes(b"placeholder")
        worker = IndexWorker(self.paths, self.root, self.library_id, 10, preview_policy="auto",
                             scan_folders=("nested",))
        failures: list[str] = []
        outcomes: list[str] = []
        worker.failed.connect(failures.append)
        worker.outcome.connect(outcomes.append)
        worker.run()
        self.assertEqual(failures, [])
        self.assertTrue(outcomes and "暫緩" in outcomes[-1])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 101)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM thumbnail_jobs WHERE state = 'pending'"
        ).fetchone()[0], 101)

    def test_scan_sync_new_changed_missing_restored(self) -> None:
        first = self.sync()
        self.assertEqual((first.total, first.added, first.pending_cleanup), (1, 1, 0))
        identifier = self.first_video()["id"]
        update_video(self.connection, identifier, status="in_progress", notes="keep me")
        self.video_path.write_bytes(b"changed")
        second = self.sync()
        self.assertEqual((second.added, second.changed), (0, 1))
        self.assertEqual(self.first_video()["id"], identifier)
        self.video_path.unlink()
        missing = self.sync()
        self.assertEqual(missing.pending_cleanup, 1)
        self.assertIsNotNone(self.first_video()["missing_since"])
        self.video_path.write_bytes(b"changed")
        restored = self.sync()
        self.assertEqual(restored.restored, 1)
        row = self.first_video()
        self.assertEqual((row["id"], row["notes"], row["status"]), (identifier, "keep me", "in_progress"))
        self.assertIsNone(row["missing_since"])

    def test_new_mp4_after_database_reopen_enters_triage(self) -> None:
        self.sync()
        new_video = self.root / "nested" / "新增片.MP4"
        new_video.write_bytes(b"disposable fixture")
        self.connection.close()
        with closing(open_database(self.paths.database, self.paths.backups)) as reopened:
            result = synchronize(reopened, self.library_id, self.root, scan_library(self.root, self.library_id))
            self.assertEqual(result.added, 1)
            row = reopened.execute("SELECT status FROM videos WHERE file_name = ?", (new_video.name,)).fetchone()
            self.assertEqual(row["status"], "unprocessed")

    def test_failed_or_cancelled_scan_cannot_mark_missing(self) -> None:
        self.sync()
        self.video_path.unlink()
        with self.assertRaises(ScanCancelled):
            scan_library(self.root, self.opened.marker.library_id, cancelled=lambda: True)
        self.assertIsNone(self.first_video()["missing_since"])
        marker = self.root / ".videomanager" / "library.json"
        marker.write_text("broken", encoding="utf-8")
        with self.assertRaises(ScanError):
            scan_library(self.root, self.opened.marker.library_id)
        self.assertIsNone(self.first_video()["missing_since"])

    def test_tags_categories_and_filter_logic(self) -> None:
        self.sync()
        video_id = self.first_video()["id"]
        group = create_tag_group(self.connection, "地點")
        taiwan = create_tag(self.connection, group, "台灣")
        japan = create_tag(self.connection, group, "日本")
        category = create_category(self.connection, "旅行")
        child = create_category(self.connection, "亞洲", category)
        set_video_relations(self.connection, video_id, tag_ids=[taiwan, japan], category_ids=[child])
        library_id = self.opened.marker.library_id
        with closing(open_database(self.paths.database, self.paths.backups)) as reopened:
            self.assertEqual(len(list_videos(reopened, library_id, {"tag_all": [taiwan, japan]})), 1)
            self.assertEqual(len(list_videos(reopened, library_id, {"category_ids": [child]})), 1)
        self.assertEqual(len(list_videos(self.connection, library_id, {"tag_all": [taiwan, japan]})), 1)
        self.assertEqual(len(list_videos(self.connection, library_id, {"tag_any": [str(uuid4()), japan]})), 1)
        self.assertEqual(len(list_videos(self.connection, library_id, {"tag_not": [taiwan]})), 0)
        self.assertEqual(len(list_videos(self.connection, library_id, {"category_ids": [category], "include_descendants": True})), 1)
        merge_tags(self.connection, taiwan, japan)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM video_tags").fetchone()[0], 1)

    def test_move_conflict_and_recovery(self) -> None:
        self.sync()
        row = self.first_video()
        destination = self.root / "nested" / "B.mp4"
        destination.write_bytes(b"existing")
        with self.assertRaises(FileOperationError):
            move_video(self.connection, self.opened.marker.library_id, self.root, row["id"], "nested/B.mp4")
        self.assertTrue(self.video_path.exists())
        destination.unlink()
        with self.assertRaises(FileOperationError):
            move_video(self.connection, self.opened.marker.library_id, self.root, row["id"], "../outside.mp4")
        self.connection.execute(
            """CREATE TRIGGER fail_move BEFORE UPDATE OF relative_path ON videos
               BEGIN SELECT RAISE(ABORT, 'simulated DB failure'); END"""
        )
        with self.assertRaises(FileOperationError):
            move_video(self.connection, self.opened.marker.library_id, self.root, row["id"], "nested/B.mp4")
        self.assertFalse(self.video_path.exists())
        self.assertTrue(destination.exists())
        self.assertEqual(self.first_video()["id"], row["id"])
        self.connection.execute("DROP TRIGGER fail_move")
        messages = recover_file_operations(self.connection, self.opened.marker.library_id, self.root)
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.first_video()["relative_path"], "nested/B.mp4")
        self.assertEqual(self.first_video()["id"], row["id"])

    def test_successful_move_preserves_id_and_tags(self) -> None:
        self.sync()
        row = self.first_video()
        group = create_tag_group(self.connection, "內容")
        tag = create_tag(self.connection, group, "教學")
        set_video_relations(self.connection, row["id"], tag_ids=[tag])
        move_video(self.connection, self.opened.marker.library_id, self.root, row["id"], "nested/Renamed.mp4")
        self.assertFalse(self.video_path.exists())
        self.assertTrue((self.root / "nested" / "Renamed.mp4").exists())
        self.assertEqual(self.first_video()["id"], row["id"])
        self.assertEqual(self.connection.execute("SELECT tag_id FROM video_tags").fetchone()[0], tag)

    def test_frame_positions_follow_corrected_spec(self) -> None:
        fractions = frame_fractions()
        self.assertEqual(len(fractions), 10)
        self.assertAlmostEqual(fractions[0], 0.05)
        self.assertAlmostEqual(fractions[-1], 0.90)
        steps = [b - a for a, b in zip(fractions, fractions[1:])]
        self.assertTrue(all(abs(step - steps[0]) < 1e-12 for step in steps))

    def test_import_plans_bounded_folders_and_moves_disposable_mp4(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        sources = []
        for number in range(205):
            source = outside / f"{number:03d}.mp4"
            source.write_bytes(b"fixture")
            sources.append(source)
        items = plan_import(self.root, self.library_id, sources, month="2026-10")
        counts = {}
        for item in items:
            counts[item.destination.parent.name] = counts.get(item.destination.parent.name, 0) + 1
        self.assertEqual(counts, {"001": 50, "002": 50, "003": 50, "004": 50, "005": 5})
        self.assertTrue(all(source.exists() for source in sources))
        with self.assertRaises(ImportCancelled):
            import_mp4(items[0], self.root, self.library_id, cancelled=lambda: True)
        self.assertTrue(sources[0].exists())
        reported = []
        destination = import_mp4(items[0], self.root, self.library_id,
                                 progress=lambda copied, size: reported.append((copied, size))).destination
        self.assertEqual(reported[-1], (items[0].size, items[0].size))
        self.assertFalse(sources[0].exists())
        self.assertTrue(destination.exists())
        self.assertEqual(self.sync().added, 2)
        imported = self.connection.execute("SELECT status FROM videos WHERE file_name = '000.mp4'").fetchone()
        self.assertEqual(imported["status"], "unprocessed")

    def test_unique_category_is_exclusive_and_moves_into_two_month_folder(self) -> None:
        self.sync()
        video_id = self.first_video()["id"]
        self.connection.execute("UPDATE videos SET added_at = '2026-03-15T00:00:00+00:00' WHERE id = ?", (video_id,))
        self.connection.commit()
        first = create_unique_category(self.connection, "分類1")
        second = create_unique_category(self.connection, "分類2")
        general = create_category(self.connection, "分類A")
        set_video_relations(self.connection, video_id, category_ids=[general])
        first_path = assign_unique_category(self.connection, self.library_id, self.root, video_id, first)
        self.assertIn("唯一分類/分類1/2026-03~04/001", first_path.as_posix())
        self.assertFalse(self.video_path.exists())
        self.assertEqual(self.connection.execute("SELECT unique_category_id FROM videos WHERE id = ?", (video_id,)).fetchone()[0], first)
        self.assertEqual(len(list_videos(self.connection, self.library_id, {"unique_category_id": first})), 1)
        self.assertEqual(len(list_videos(self.connection, self.library_id, {"unique_category_id": second})), 0)
        second_path = assign_unique_category(self.connection, self.library_id, self.root, video_id, second)
        self.assertFalse(first_path.exists())
        self.assertTrue(second_path.exists())
        self.assertEqual(self.connection.execute("SELECT unique_category_id FROM videos WHERE id = ?", (video_id,)).fetchone()[0], second)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM video_categories WHERE video_id = ?", (video_id,)).fetchone()[0], 1)
        update_video(self.connection, video_id, status="done")
        self.assertEqual(delete_unique_category(self.connection, self.library_id, self.root, second), 1)
        self.assertEqual(self.connection.execute("SELECT status FROM videos WHERE id = ?", (video_id,)).fetchone()[0], "done")
        delete_category(self.connection, general)
        self.assertEqual(self.connection.execute("SELECT status FROM videos WHERE id = ?", (video_id,)).fetchone()[0], "unprocessed")

    def test_unique_assignment_journal_recovers_category_and_path_together(self) -> None:
        self.sync()
        video_id = self.first_video()["id"]
        category = create_unique_category(self.connection, "精選")
        self.connection.execute(
            """CREATE TRIGGER fail_unique BEFORE UPDATE OF unique_category_id ON videos
               BEGIN SELECT RAISE(ABORT, 'simulated DB failure'); END"""
        )
        with self.assertRaises(FileOperationError):
            assign_unique_category(self.connection, self.library_id, self.root, video_id, category)
        self.connection.execute("DROP TRIGGER fail_unique")
        self.assertFalse(self.video_path.exists())
        messages = recover_file_operations(self.connection, self.library_id, self.root)
        self.assertEqual(len(messages), 1)
        row = self.connection.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        self.assertEqual(row["unique_category_id"], category)
        self.assertTrue((self.root / row["relative_path"]).exists())

    def test_deleting_populated_category_returns_only_orphans_to_triage(self) -> None:
        second_path = self.root / "nested" / "B.mp4"
        second_path.write_bytes(b"second disposable fixture")
        self.sync()
        rows = {row["file_name"]: row for row in self.connection.execute("SELECT * FROM videos")}
        primary = create_category(self.connection, "旅行")
        child = create_category(self.connection, "日本", primary)
        other = create_category(self.connection, "精選")
        group = create_tag_group(self.connection, "內容")
        tag = create_tag(self.connection, group, "保留")
        set_video_relations(self.connection, rows["A.mp4"]["id"], category_ids=[primary], tag_ids=[tag])
        set_video_relations(self.connection, rows["B.mp4"]["id"], category_ids=[child, other])
        update_video(self.connection, rows["A.mp4"]["id"], status="done", notes="A note")
        update_video(self.connection, rows["B.mp4"]["id"], status="done")
        self.assertEqual(category_delete_impact(self.connection, primary).returned_to_unprocessed, 1)
        result = delete_category(self.connection, primary)
        self.assertEqual((result.categories, result.affected_videos, result.returned_to_unprocessed), (2, 2, 1))
        a = self.connection.execute("SELECT * FROM videos WHERE id = ?", (rows["A.mp4"]["id"],)).fetchone()
        b = self.connection.execute("SELECT * FROM videos WHERE id = ?", (rows["B.mp4"]["id"],)).fetchone()
        self.assertEqual((a["status"], a["notes"]), ("unprocessed", "A note"))
        self.assertEqual(b["status"], "done")
        self.assertEqual(self.connection.execute("SELECT category_id FROM video_categories WHERE video_id = ?", (b["id"],)).fetchone()[0], other)
        self.assertEqual(self.connection.execute("SELECT tag_id FROM video_tags WHERE video_id = ?", (a["id"],)).fetchone()[0], tag)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0], 1)
        self.assertTrue(self.video_path.exists())
        self.assertTrue(second_path.exists())

    def test_cleanup_requires_fresh_verified_scan_and_only_removes_db(self) -> None:
        self.sync()
        video_id = self.first_video()["id"]
        self.video_path.unlink()
        self.sync()
        marker = self.root / ".videomanager" / "library.json"
        marker.write_text("broken", encoding="utf-8")
        with self.assertRaises(ScanError):
            cleanup_missing_records(self.connection, self.root, self.library_id, self.paths.cache, immediate=True)
        self.assertEqual(self.first_video()["id"], video_id)
        # Restore the marker only in this disposable fixture.
        marker.write_text(self._marker_json, encoding="utf-8")
        result = cleanup_missing_records(self.connection, self.root, self.library_id, self.paths.cache, immediate=True)
        self.assertEqual(result.deleted_records, 1)
        self.assertIsNone(self.first_video())

    def test_cleanup_keeps_existing_video_outside_current_scan_folders(self) -> None:
        self.sync()
        video_id = self.first_video()["id"]
        result = cleanup_missing_records(
            self.connection, self.root, self.library_id, self.paths.cache,
            immediate=True, scan_folders=(),
        )
        self.assertEqual(result.deleted_records, 0)
        self.assertEqual(self.first_video()["id"], video_id)
        self.assertTrue(self.video_path.is_file())

    def test_manual_scan_reads_only_chosen_folders_and_managed_imports(self) -> None:
        (self.root / "other").mkdir()
        (self.root / "other" / "hidden.mp4").write_bytes(b"hidden")
        (self.root / "匯入" / "2026-10").mkdir(parents=True)
        (self.root / "匯入" / "2026-10" / "managed.mp4").write_bytes(b"managed")
        self.assertEqual({row.file_name for row in scan_library(
            self.root, self.library_id, included_folders=(),
        )}, {"managed.mp4"})
        self.assertEqual({row.file_name for row in scan_library(
            self.root, self.library_id, included_folders=("nested",),
        )}, {"A.mp4", "managed.mp4"})

    def test_group_import_plan_and_shared_group_operations(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        sources = [outside / "Part 1.mp4", outside / "Part 2.mp4"]
        for source in sources:
            source.write_bytes(b"fixture")
        planned = plan_group_import(self.root, self.library_id, sources, month="2026-10")
        self.assertEqual(planned[0].destination.parent, planned[1].destination.parent)
        self.assertEqual(planned[0].destination.parent.name, "Part 1")
        self.assertTrue(all(source.exists() for source in sources))
        bucket = self.root / "匯入" / "2026-10" / "001"
        bucket.mkdir(parents=True)
        for number in range(100):
            (bucket / f"existing-{number:03}.mp4").write_bytes(b"fixture")
        next_bucket = plan_group_import(self.root, self.library_id, sources, month="2026-10")
        self.assertEqual(next_bucket[0].destination.parent.parent.name, "002")

        second = self.root / "nested" / "Part 2.mp4"
        second.write_bytes(b"fixture")
        self.sync()
        ids = [self.connection.execute("SELECT id FROM videos WHERE file_name = ?", (name,)).fetchone()[0]
               for name in ("A.mp4", "Part 2.mp4")]
        group_id = create_group_in_folder(self.connection, self.library_id, self.root, "A", ids)
        members = group_members(self.connection, group_id)
        self.assertEqual([member["id"] for member in members], ids)
        self.assertTrue(all((self.root / member["relative_path"]).exists() for member in members))
        self.assertEqual(expand_group_ids(self.connection, ids[:1]), ids)
        tag_group = create_tag_group(self.connection, "Topics")
        tag = create_tag(self.connection, tag_group, "Shared")
        category = create_category(self.connection, "Shared category")
        set_video_relations(self.connection, ids[0], tag_ids=[tag], category_ids=[category])
        self.assertTrue(all(self.connection.execute(
            "SELECT 1 FROM video_tags WHERE video_id = ? AND tag_id = ?", (identifier, tag),
        ).fetchone() for identifier in ids))
        unique = create_unique_category(self.connection, "Exclusive")
        assign_unique_category(self.connection, self.library_id, self.root, ids[0], unique)
        relocated = group_members(self.connection, group_id)
        self.assertTrue(all(member["unique_category_id"] == unique for member in relocated))
        self.assertEqual(len({str((self.root / member["relative_path"]).parent) for member in relocated}), 1)
        self.assertTrue(all((self.root / member["relative_path"]).exists() for member in relocated))


if __name__ == "__main__":
    unittest.main()
