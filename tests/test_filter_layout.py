import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from videomanager.db import repository as repo
from videomanager.db.database import open_database
from videomanager.ui.filter_dialog import FilterDialog


class FilterLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_tabs_preserve_combined_filters_and_give_relations_full_height(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            connection = open_database(base / "data.db", base / "backups")
            group = repo.create_tag_group(connection, "分類")
            tag = repo.create_tag(connection, group, "標籤")
            entity = repo.create_entity(connection, "person", "人物甲")
            modified = round(datetime.fromisoformat(FilterDialog._utc_date("2026-10-20")).timestamp() * 1e9)
            current = {"tag_all": [tag], "entity_ids": [entity], "duration_min_ms": 120000,
                       "size_max": 2 * 1024**3, "rating_min": 3, "favorite": False,
                       "modified_from_ns": modified}
            dialog = FilterDialog(connection, current)
            try:
                dialog.show()
                self.app.processEvents()
                self.assertEqual(dialog.tabs.count(), 3)
                self.assertGreater(dialog.width(), 0)
                self.assertGreater(dialog.height(), 0)
                self.assertEqual(dialog.filters(), current)
                self.assertIn("（1）", dialog.tabs.tabText(0))
                self.assertIn("（1）", dialog.tabs.tabText(1))
                dialog.tabs.setCurrentIndex(1)
                self.app.processEvents()
                self.assertGreater(dialog.person_filter.scroll.height(), 200)
                self.assertFalse(dialog.person_filter.add_button.isVisible())
                self.assertIsNone(dialog.person_filter._buttons[entity].mime_type)
                self.assertLessEqual(dialog.duration_min.width(), 100)
                dialog.clear_all()
                self.assertEqual(dialog.filters(), {})
                self.assertEqual([dialog.tabs.tabText(i) for i in range(3)], ["標籤", "關聯", "其他條件"])
            finally:
                dialog.close()
                dialog.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                connection.close()
