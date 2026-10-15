"""Disposable T16 check: 500 library rows and 5,000 cached WebP files."""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from videomanager.config import AppPaths
from videomanager.db import repository as repo
from videomanager.db.database import open_database
from videomanager.services.library_service import open_library
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize
from videomanager.services.thumbnail_service import cache_signature
from videomanager.ui.browser_view import BrowserView, VideoModel


def main() -> int:
    app = QApplication([])
    with tempfile.TemporaryDirectory(prefix="vm-stress-", dir=Path(__file__).resolve().parent) as scratch:
        base = Path(scratch)
        root = base / "library"
        root.mkdir()
        for index in range(500):
            (root / f"測試片 {index:04d}.mp4").write_bytes(b"fixture only")
        paths = AppPaths(base / "data")
        library_id = open_library(paths, root, create=True).marker.library_id
        connection = open_database(paths.database, paths.backups)
        try:
            result = synchronize(connection, library_id, root, scan_library(root, library_id))
            assert result.total == 500, result
            frame = QImage(16, 9, QImage.Format_RGB32)
            frame.fill(QColor("#3063a8"))
            seed = base / "seed.webp"
            assert frame.save(str(seed), "WEBP")
            content = seed.read_bytes()
            for row in connection.execute("SELECT id, file_size, mtime_ns FROM videos"):
                folder = paths.cache / row["id"]
                folder.mkdir(parents=True, exist_ok=True)
                for number in range(10):
                    (folder / f"{number:02d}.webp").write_bytes(content)
                connection.execute(
                    "UPDATE thumbnail_jobs SET state = 'done', cache_signature = ? WHERE video_id = ?",
                    (cache_signature(row["file_size"], row["mtime_ns"], 10), row["id"]),
                )
            connection.commit()
            rows = repo.list_videos(connection, library_id, {}, sort="name")
            model = VideoModel(paths.cache)
            model.set_rows(rows)
            browser = BrowserView(model)
            browser.resize(1000, 650)
            browser.show()
            for index in range(500):
                browser.scrollTo(model.index(index))
                for number in range(10):
                    model.show_frame(index, number)
                    assert model.data(model.index(index), Qt.DecorationRole) is not None
                if index % 50 == 0:
                    app.processEvents()
            assert len(model.pixmaps) <= 120, len(model.pixmaps)
            matches = repo.list_videos(connection, library_id, {"text": "測試片 0499"})
            assert len(matches) == 1
            repo.update_video(connection, matches[0]["id"], rating=4)
            assert repo.video_by_id(connection, matches[0]["id"])["rating"] == 4
            browser.close()
            print("T16 passed: 500 rows, 5,000 WebP files, search/edit/scroll, RAM image cache <= 120")
        finally:
            connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
