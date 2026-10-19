from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from videomanager.config import AppPaths
from videomanager.db import repository as repo
from videomanager.db.database import _migrations, open_database
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.ui.details_panel import DetailsPanel
from videomanager.ui.filter_dialog import FilterDialog
from videomanager.ui.metadata_dialog import MetadataDialog


class RelationTypeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def database(self, base):
        paths = AppPaths(base / "data")
        root = base / "library"
        root.mkdir()
        for name in ("source", "first", "second", "third"):
            (root / f"{name}.mp4").write_bytes(name.encode())
        library_id = open_library(paths, root, create=True).marker.library_id
        connection = open_database(paths.database, paths.backups)
        synchronize(connection, library_id, root, scan_library(root, library_id))
        ids = {row["file_name"].split(".")[0]: row["id"] for row in connection.execute("SELECT id,file_name FROM videos")}
        return paths, root, library_id, connection, ids

    def link(self, connection, video, *entities):
        with connection:
            connection.executemany("INSERT INTO video_entities VALUES (?,?)", ((video, entity) for entity in entities))

    def test_v11_upgrade_preserves_links_ids_images_files_and_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("videomanager.db.database._migrations", return_value=_migrations()[:11]):
                paths, root, library, connection, ids = self.database(Path(directory))
                image = b"\x89PNG\r\n\x1a\nlegacy-image"
                with connection:
                    connection.execute("INSERT INTO entities VALUES ('old-person','person','舊人物',?)", (image,))
                    connection.execute("INSERT INTO entities VALUES ('old-studio','studio','舊片商',NULL)")
                self.link(connection, ids["source"], "old-person", "old-studio")
                self.link(connection, ids["first"], "old-person")
                before = [tuple(row) for row in connection.execute("SELECT * FROM videos ORDER BY id")]
                connection.close()
            settings = paths.settings.read_bytes()
            connection = open_database(paths.database, paths.backups)
            try:
                self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM videos ORDER BY id")], before)
                self.assertEqual(repo.video_entity_ids(connection, ids["source"]), {"old-person", "old-studio"})
                self.assertEqual(connection.execute("SELECT thumbnail_png FROM entities WHERE id='old-person'").fetchone()[0], image)
                self.assertEqual([row["id"] for row in repo.list_relation_types(connection)], ["person", "studio"])
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual((root / "source.mp4").read_bytes(), b"source")
                self.assertEqual(paths.settings.read_bytes(), settings)
                backups = list(paths.backups.glob("*.db"))
                self.assertEqual(len(backups), 1)
                with closing(sqlite3.connect(backups[0])) as backup:
                    self.assertEqual(backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 11)
                    self.assertEqual(backup.execute("SELECT COUNT(*) FROM video_entities").fetchone()[0], 3)
            finally:
                connection.close()

    def test_custom_types_priority_limit_and_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, root, library, connection, ids = self.database(Path(directory))
            try:
                third_type = repo.create_relation_type(connection, "系列")
                with self.assertRaises(ValueError):
                    repo.create_relation_type(connection, "第四類")
                with self.assertRaises(sqlite3.IntegrityError), connection:
                    connection.execute("INSERT INTO relation_types VALUES ('fourth','第四類',2)")
                person = repo.create_entity(connection, "person", "人物甲")
                studios = [repo.create_entity(connection, "studio", f"片商 {i}") for i in range(2)]
                series = repo.create_entity(connection, third_type, "系列甲")
                self.link(connection, ids["source"], person, *studios, series)
                self.link(connection, ids["first"], person)
                self.link(connection, ids["second"], *studios)
                self.link(connection, ids["third"], series)
                self.assertEqual([row["id"] for row in repo.related_videos(connection, ids["source"])],
                                 [ids["first"], ids["second"], ids["third"]])
                repo.rename_relation_type(connection, "person", "角色")
                repo.move_relation_type(connection, third_type, -1)
                repo.move_relation_type(connection, third_type, -1)
                expected = [ids["third"], ids["first"], ids["second"]]
                self.assertEqual([row["id"] for row in repo.related_videos(connection, ids["source"])], expected)
                self.assertEqual([row["id"] for row in repo.list_videos(connection, library, {"related_to": ids["source"]})], expected)
                self.assertEqual({row["id"] for row in repo.list_videos(connection, library, {"entity_ids": [series]})},
                                 {ids["source"], ids["third"]})
                with self.assertRaises(ValueError):
                    repo.delete_relation_type(connection, third_type)
                self.assertEqual(repo.video_entity_ids(connection, ids["source"]), {person, *studios, series})
            finally:
                connection.close()

    def test_third_type_editor_equal_height_and_compact_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, root, library, connection, ids = self.database(Path(directory))
            panel = dialog = manager = None
            try:
                kind = repo.create_relation_type(connection, "系列")
                entity = repo.create_entity(connection, kind, "系列甲")
                for index in range(25):
                    repo.create_entity(connection, "person", f"人物{index}")
                repo.create_entity(connection, "person", "長名稱" * 15)
                panel = DetailsPanel(connection)
                panel.set_library(root, library)
                panel.set_video(ids["source"])
                panel.resize(650, 900)
                panel.show()
                self.app.processEvents()
                heights = [panel.relations_layout.itemAt(index).widget().height() for index in range(3)]
                self.assertLessEqual(max(heights) - min(heights), 1)
                widths = [button.width() for button in panel.entity_pickers["person"]._buttons.values()]
                self.assertLessEqual(max(widths) - min(widths), 1)
                panel.entity_pickers[kind].set_selected({entity})
                self.assertTrue(panel.save_current())
                self.assertEqual(repo.video_entity_ids(connection, ids["source"], kind), {entity})
                dialog = FilterDialog(connection, {"entity_ids": [entity]})
                self.assertEqual(dialog.filters()["entity_ids"], [entity])
                self.assertLessEqual(dialog.duration_min.width(), 100)
                dialog.clear_all()
                self.assertNotIn("entity_ids", dialog.filters())
                manager = MetadataDialog(connection)
                self.assertEqual(len(manager.entity_lists), 3)
                self.assertFalse(manager.add_relation_type_button.isEnabled())
                manager.change_relation_type(kind, "up")
                self.assertEqual(list(manager.entity_lists), ["person", kind, "studio"])
                second = repo.create_entity(connection, kind, "第二項")
                manager.refresh_entities(second)
                manager.move_entity_item(kind, -1)
                self.assertEqual([row["id"] for row in repo.list_entities(connection, kind)], [second, entity])
                self.assertEqual(repo.video_entity_ids(connection, ids["source"], kind), {entity})
            finally:
                for widget in (panel, dialog, manager):
                    if widget is not None:
                        widget.close()
                connection.close()


if __name__ == "__main__":
    unittest.main()
