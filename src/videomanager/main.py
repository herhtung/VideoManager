"""VideoManager desktop entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from videomanager.config import AppPaths, brand_icon_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline Windows MP4 library manager")
    parser.add_argument("--data-dir", type=Path, help="Override local application data directory")
    args = parser.parse_args(argv)
    paths = AppPaths(args.data_dir) if args.data_dir else AppPaths.default()
    try:
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
        from videomanager.ui.main_window import MainWindow
        from videomanager.ui.theme import apply_theme
        from videomanager.ui.text_input import install_text_input_support
    except ImportError as exc:
        message = f"Desktop import failed: {exc}. Install dependencies with: pip install -e ."
        try:
            paths.logs.mkdir(parents=True, exist_ok=True)
            (paths.logs / "startup.log").write_text(message + "\n", encoding="utf-8")
        except OSError:
            pass
        print(message, file=sys.stderr)
        return 2
    app = QApplication(sys.argv[:1])
    install_text_input_support(app)
    apply_theme(app)
    app.setWindowIcon(QIcon(str(brand_icon_path())))
    window = MainWindow(paths)
    screen = app.primaryScreen()
    if screen is not None:
        area = screen.availableGeometry()
        window.resize(min(window.width(), area.width()), min(window.height(), area.height()))
        window.move(
            area.left() + (area.width() - window.width()) // 2,
            area.top() + (area.height() - window.height()) // 2,
        )
    window.showMaximized()
    return app.exec()
