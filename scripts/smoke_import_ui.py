"""Check folder discovery and destination planning in the background worker."""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTimer

from videomanager.services.library_locator import create_marker
from videomanager.ui.import_worker import ImportPlanWorker


def main() -> int:
    app = QCoreApplication([])
    with tempfile.TemporaryDirectory(prefix="vm-import-ui-", dir=Path(__file__).resolve().parent) as scratch:
        base = Path(scratch)
        root = base / "library"
        root.mkdir()
        marker = create_marker(root)
        outside = base / "outside"
        nested = outside / "nested"
        nested.mkdir(parents=True)
        for index in range(205):
            (nested / f"{index:03d}.mp4").write_bytes(b"fixture")
        planned = []
        errors = []
        worker = ImportPlanWorker(outside, root, marker.library_id)
        worker.planned.connect(planned.append)
        worker.failed.connect(errors.append)
        worker.finished.connect(app.quit)
        QTimer.singleShot(30000, app.quit)
        worker.start()
        app.exec()
        assert worker.wait(5000)
        assert not errors, errors
        assert len(planned) == 1 and len(planned[0]) == 205
        counts = {}
        for item in planned[0]:
            bucket = item.destination.parent.name
            counts[bucket] = counts.get(bucket, 0) + 1
        assert counts == {"001": 50, "002": 50, "003": 50, "004": 50, "005": 5}
        assert all(item.source.exists() for item in planned[0])
    print("Import planning worker passed: 205 MP4s, background discovery, 50 per folder")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
