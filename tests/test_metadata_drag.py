"""Dragging tag categories and tags updates their stored placement."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication

from videomanager.db import repository as repo
from videomanager.db.database import open_database
from videomanager.ui.metadata_dialog import MetadataDialog
from videomanager.ui.tag_picker import CATEGORY_DRAG_MIME, TAG_DRAG_MIME


class MetadataDragTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def drop(button, mime_type: str, identifier: str, *, lower_half: bool = False,
             right_half: bool = False) -> None:
        mime = QMimeData()
        mime.setData(mime_type, identifier.encode("utf-8"))
        point = QPoint(button.width() * (3 if right_half else 1) // 4,
                       button.height() * (3 if lower_half else 1) // 4)
        enter = QDragEnterEvent(point, Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
        drop = QDropEvent(QPointF(point), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(button, enter)
        assert enter.isAccepted()
        QApplication.sendEvent(button, drop)
        assert drop.isAccepted()

    def test_drag_category_reorders_and_tag_drop_moves_category(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-metadata-drag-") as directory:
            base = Path(directory)
            connection = open_database(base / "data.db", base / "backups")
            try:
                first = repo.create_tag_group(connection, "甲")
                second = repo.create_tag_group(connection, "乙")
                third = repo.create_tag_group(connection, "丙")
                tag = repo.create_tag(connection, first, "標籤")
                other = repo.create_tag(connection, second, "另一個標籤")
                repo.move_sort_order(connection, "tag_groups", [first, second, third])
                dialog = MetadataDialog(connection)
                dialog.show()
                self.app.processEvents()
                self.assertGreaterEqual(dialog.width(), 1260)
                self.assertGreaterEqual(dialog.height(), 820)
                self.drop(dialog.categories._buttons[third], CATEGORY_DRAG_MIME, first, lower_half=True)
                self.app.processEvents()
                self.assertEqual([row["id"] for row in repo.list_tag_groups(connection)], [second, third, first])
                self.assertEqual(dialog.categories.selected_id(), first)
                self.drop(dialog.categories._buttons[second], TAG_DRAG_MIME, tag)
                self.app.processEvents()
                self.assertEqual(connection.execute("SELECT group_id FROM tags WHERE id = ?", (tag,)).fetchone()[0],
                                 second)
                self.assertEqual(dialog.categories.selected_id(), second)
                self.assertEqual(dialog._selected_tag_id, tag)

                self.drop(dialog.tags._headers[second], CATEGORY_DRAG_MIME, first,
                          lower_half=True)
                self.app.processEvents()
                self.assertEqual([row["id"] for row in repo.list_tag_groups(connection)],
                                 [second, first, third])
                self.assertEqual(dialog.categories.selected_id(), first)

                self.drop(dialog.tags._buttons[other][0], TAG_DRAG_MIME, tag)
                self.app.processEvents()
                second_tags = [row["id"] for row in repo.list_tags(connection)
                               if row["group_id"] == second]
                self.assertEqual(second_tags, [tag, other])
                self.assertEqual(dialog._selected_tag_id, tag)

                self.drop(dialog.tags._headers[third], TAG_DRAG_MIME, tag)
                self.app.processEvents()
                self.assertEqual(connection.execute("SELECT group_id FROM tags WHERE id = ?", (tag,)).fetchone()[0],
                                 third)
                self.drop(dialog.tags._buttons[other][0], TAG_DRAG_MIME, tag, right_half=True)
                self.app.processEvents()
                second_tags = [row["id"] for row in repo.list_tags(connection)
                               if row["group_id"] == second]
                self.assertEqual(second_tags, [other, tag])
                dialog.close()
                dialog.deleteLater()
                self.app.processEvents()
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
