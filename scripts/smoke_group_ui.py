"""Exercise the multi-file import completion and grouped player on disposable files."""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtWidgets import QApplication, QMessageBox

from videomanager.config import AppPaths
from videomanager.services.import_service import import_mp4, plan_group_import
from videomanager.services.library_service import open_library
from videomanager.ui.main_window import MainWindow


def main() -> int:
    app = QApplication([])
    with tempfile.TemporaryDirectory(prefix="vm-group-", dir=Path(__file__).resolve().parent) as scratch:
        base = Path(scratch)
        root = base / "library"
        root.mkdir()
        paths = AppPaths(base / "data")
        opened = open_library(paths, root, create=True)
        source_dir = base / "outside"
        source_dir.mkdir()
        sources = [source_dir / "Part 1.mp4", source_dir / "Part 2.mp4"]
        for source in sources:
            source.write_bytes(b"disposable test file")
        items = plan_group_import(root, opened.marker.library_id, sources)
        completed = [str(import_mp4(item, root, opened.marker.library_id).destination) for item in items]
        window = MainWindow(paths)
        original = QMessageBox.information
        QMessageBox.information = staticmethod(lambda *args, **kwargs: QMessageBox.Ok)
        try:
            window._group_import_items = items
            window._import_result = {"completed": completed, "retained": [], "failed": [], "cancelled": False}
            window.start_scan = lambda: None
            window._on_import_finished()
            rows = window.connection.execute(
                "SELECT id, relative_path FROM videos ORDER BY file_name",
            ).fetchall()
            group = window.connection.execute("SELECT id, folder_rel_path FROM video_groups").fetchone()
            assert len(rows) == 2 and group is not None
            assert group["folder_rel_path"] == items[0].destination.parent.relative_to(root).as_posix()
            assert all((root / row["relative_path"]).exists() for row in rows)
            window.open_player(rows[0]["id"])
            assert window.player is not None and len(window.player.segments) == 2
            assert window.player.next_part.isEnabled()
            window.player.switch_segment(1)
            assert window.player.segment_index == 1 and window.player.previous_part.isEnabled()
            window.player.close()
            class DropEvent:
                def __init__(self, dropped):
                    self.mime = QMimeData()
                    self.mime.setUrls([QUrl.fromLocalFile(str(path)) for path in dropped])
                    self.accepted = False

                def mimeData(self):
                    return self.mime

                def acceptProposedAction(self):
                    self.accepted = True

            routed = []
            window._begin_import = lambda paths, *, group=False: routed.append((paths, group))
            window._set_import_mode(True)
            files_drop = DropEvent([Path(path) for path in completed])
            window.dropEvent(files_drop)
            folder_drop = DropEvent([items[0].destination.parent])
            window.dropEvent(folder_drop)
            assert files_drop.accepted and folder_drop.accepted
            assert len(routed) == 2 and all(group for _, group in routed)
        finally:
            window.close()
            window.connection.close()
            QMessageBox.information = original
    print("Grouped import UI passed: files and folder drops, one group folder, linked player controls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
