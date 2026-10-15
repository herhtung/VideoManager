"""Launch the Qt window briefly without touching a real library."""

from __future__ import annotations

import tempfile
import os
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from videomanager.config import AppPaths
from videomanager.ui.main_window import MainWindow
from videomanager.ui.theme import apply_theme


def main() -> int:
    app = QApplication([])
    apply_theme(app)
    with tempfile.TemporaryDirectory(prefix="vm-ui-", dir=Path(__file__).resolve().parent) as scratch:
        window = MainWindow(AppPaths(Path(scratch) / "data"))
        assert window.empty_title.text() == "尚未設定影片庫位置"
        assert window.empty_button.text() == "設定影片庫根目錄…"
        window.show()
        capture = os.environ.get("VM_UI_CAPTURE")
        if capture:
            QTimer.singleShot(200, lambda: window.grab().save(capture))
        QTimer.singleShot(500, app.quit)
        result = app.exec()
        window.close()
        return result


if __name__ == "__main__":
    raise SystemExit(main())
