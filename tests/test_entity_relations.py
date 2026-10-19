from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMessageBox, QTabWidget

from videomanager.config import AppPaths, Settings, load_settings, save_settings
from videomanager.db import repository as repo
from videomanager.db.database import open_database
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.ui.details_panel import DetailsPanel
from videomanager.ui.metadata_dialog import MetadataDialog


class EntityRelationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_entities_persist_rename_filter_and_many_to_many(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-entities-") as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            (root / "one.mp4").write_bytes(b"test")
            (root / "two.mp4").write_bytes(b"test")
            paths = AppPaths(base / "data")
            marker = open_library(paths, root, create=True).marker
            connection = open_database(paths.database, paths.backups)
            try:
                synchronize(connection, marker.library_id, root, scan_library(root, marker.library_id))
                ids = [row[0] for row in connection.execute("SELECT id FROM videos ORDER BY file_name")]
                group = repo.create_tag_group(connection, "類型")
                tag_a = repo.create_tag(connection, group, "標籤 A")
                tag_b = repo.create_tag(connection, group, "標籤 B")
                repo.set_video_relations(connection, ids[0], tag_ids={tag_a})
                repo.set_video_relations(connection, ids[1], tag_ids={tag_b})
                person = repo.create_entity(connection, "person", "未知人物 #001")
                studio = repo.create_entity(connection, "studio", "片商甲")
                for video_id in ids:
                    with connection:
                        connection.execute("INSERT INTO video_entities(video_id,entity_id) VALUES (?,?)", (video_id, person))
                with connection:
                    connection.execute("INSERT INTO video_entities(video_id,entity_id) VALUES (?,?)", (ids[0], studio))
                repo.rename_entity(connection, person, "演員甲")
                self.assertEqual(connection.execute("SELECT name FROM entities WHERE id=?", (person,)).fetchone()[0], "演員甲")
                self.assertEqual(repo.video_entity_ids(connection, ids[1], "person"), {person})
                panel = DetailsPanel(connection)
                panel.set_video(ids[0])
                panel.people._buttons[person].click()
                self.assertEqual(panel.people.selected_ids(), set())
                panel.people._buttons[person].click()
                self.assertEqual(panel.people.selected_ids(), {person})
                panel.people.set_selected({person})
                panel.studios.set_selected({studio})
                self.assertTrue(panel.save_current())
                self.assertIn(f'href="person:{person}"', panel.entity_summary.text())
                self.assertIn("片商甲", panel.entity_summary.text())
                self.assertEqual(repo.video_tag_ids(connection, ids[0]), {tag_a})
                panel.set_batch(ids)
                panel.people.set_selected({person})
                panel.studios.set_selected({studio})
                self.assertTrue(panel.save_current())
                self.assertEqual(repo.video_tag_ids(connection, ids[1]), {tag_b})
                self.assertEqual(repo.video_entity_ids(connection, ids[1]), {person, studio})
                self.assertEqual({row["id"] for row in repo.list_videos(connection, marker.library_id,
                                 {"person_ids": [person], "studio_ids": [studio]})}, set(ids))
                recommendations = repo.related_videos(connection, ids[0])
                self.assertEqual([row["id"] for row in recommendations], [ids[1]])
                self.assertIn("演員甲", recommendations[0]["shared_names"])
                self.assertEqual([row["id"] for row in repo.list_videos(connection, marker.library_id,
                                 {"person_ids": [person], "studio_ids": [studio], "tag_all": [tag_a]})], [ids[0]])
                panel.batch_operation.setCurrentIndex(1)
                panel.people.set_selected({person})
                panel.studios.set_selected(set())
                self.assertTrue(panel.save_current())
                self.assertEqual(repo.video_entity_ids(connection, ids[0]), {studio})
                panel.close()
                connection.close()
                connection = open_database(paths.database, paths.backups)
                self.assertEqual(repo.video_entity_ids(connection, ids[1]), {studio})
                manager = MetadataDialog(connection)
                self.assertEqual(manager.windowTitle(), "管理標籤／關聯")
                self.assertEqual(manager.findChild(QTabWidget).count(), 2)
                manager.entity_type.setCurrentIndex(1)
                manager.refresh_entities(studio)
                manager.entity_name.setText("片商乙")
                manager.rename_entity()
                self.assertEqual(connection.execute("SELECT name FROM entities WHERE id=?", (studio,)).fetchone()[0], "片商乙")
                with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
                    manager.delete_entity()
                self.assertIsNone(connection.execute("SELECT id FROM entities WHERE id=?", (studio,)).fetchone())
                self.assertEqual(repo.video_entity_ids(connection, ids[1]), set())
                manager.close()
            finally:
                connection.close()

    def test_related_hover_delay_is_saved_and_old_settings_keep_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-related-settings-") as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"schema_version":1}', encoding="utf-8")
            self.assertEqual(load_settings(path).related_hover_delay_ms, 2000)
            save_settings(path, Settings(related_hover_delay_ms=3500))
            self.assertEqual(load_settings(path).related_hover_delay_ms, 3500)


if __name__ == "__main__":
    unittest.main()
