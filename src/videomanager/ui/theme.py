"""A restrained dark theme shared by the library and player windows."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft JhengHei UI", 10))
    palette = QPalette()
    colors = {
        QPalette.Window: "#101820",
        QPalette.WindowText: "#eaf0f2",
        QPalette.Base: "#172129",
        QPalette.AlternateBase: "#1e2a33",
        QPalette.Text: "#eaf0f2",
        QPalette.Button: "#22333d",
        QPalette.ButtonText: "#eaf0f2",
        QPalette.Highlight: "#286e69",
        QPalette.HighlightedText: "#ffffff",
        QPalette.PlaceholderText: "#869ba5",
        QPalette.Mid: "#a2b1ba",
        QPalette.Dark: "#0b1116",
        QPalette.Light: "#35454e",
    }
    for role, value in colors.items():
        palette.setColor(role, QColor(value))
    app.setPalette(palette)
    app.setStyleSheet("""
        QMainWindow, QDialog { background: #101820; color: #eaf0f2; }
        QMenuBar { background: #101820; color: #c4d4da; border-bottom: 1px solid #26353e; padding: 3px 8px; }
        QMenuBar::item { padding: 5px 10px; border-radius: 5px; }
        QMenuBar::item:selected, QMenu::item:selected { background: #294b50; }
        QMenu { background: #1b2831; color: #eaf0f2; border: 1px solid #334751; padding: 5px; }
        QMenu::item { padding: 6px 22px; }
        QPushButton { background: #22333d; color: #eaf0f2; border: 1px solid #3a515a;
                      border-radius: 7px; padding: 6px 11px; min-height: 27px; }
        QPushButton:hover { background: #2d4650; border-color: #5d928f; }
        QPushButton:pressed { background: #1c4547; }
        QPushButton:disabled { color: #70838b; background: #1b252b; border-color: #2c3940; }
        QPushButton#primaryAction { background: #69d8cb; color: #112329; border-color: #69d8cb; font-weight: 700; }
        QPushButton#primaryAction:hover { background: #89e5d9; }
        QPushButton#importMode:checked { background: #286e69; color: white;
            border-color: #79d9ca; font-weight: 700; }
        QPushButton#destructiveAction { background: #38252b; color: #ffd5d5;
            border-color: #995b64; }
        QPushButton#destructiveAction:hover { background: #593039; border-color: #e3838e; }
        QPushButton#playerControl { padding: 3px 8px; min-height: 22px; border-radius: 5px; }
        QSlider#playerSeek::groove:horizontal { height: 12px; background: #344c55; border-radius: 6px; }
        QSlider#playerSeek::sub-page:horizontal { background: #69d8cb; border-radius: 6px; }
        QSlider#playerSeek::handle:horizontal { width: 22px; margin: -6px 0;
            background: #e5fcf7; border: 2px solid #50bfb2; border-radius: 11px; }
        QSlider#playerSeek::handle:horizontal:hover { background: #ffffff; }
        QLineEdit, QComboBox, QSpinBox, QDateEdit, QTextEdit, QPlainTextEdit {
            background: #1b2932; color: #eaf0f2; border: 1px solid #3b5059;
            border-radius: 6px; padding: 5px 8px; selection-background-color: #286e69; }
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus {
            border-color: #69d8cb; }
        QListWidget, QListView, QTreeWidget { background: #172129; color: #eaf0f2;
            border: 1px solid #2b3a43; border-radius: 7px; outline: none; }
        QListWidget::item { padding: 6px 7px; border-radius: 5px; }
        QListWidget::item:selected { background: #286e69; color: white; }
        QListWidget::item:hover:!selected { background: #263841; }
        QSplitter::handle { background: #24323a; }
        QStatusBar { background: #101820; color: #a2b1ba; border-top: 1px solid #26353e; }
        QProgressBar { background: #1b2932; color: #eaf0f2; border: 1px solid #3b5059;
                       border-radius: 5px; text-align: center; }
        QProgressBar::chunk { background: #5fcfbe; border-radius: 4px; }
        QWidget#appHeader { background: #16232b; border-bottom: 1px solid #31424c; }
        QLabel#appTitle { color: #f2faf9; font-size: 17px; font-weight: 700; }
        QLabel#appSubtitle, QLabel#rootLabel, QLabel#sectionLabel { color: #98afb8; }
        QLabel#sectionLabel { font-weight: 700; letter-spacing: 1px; }
        QLabel#playerTitle { color: #f2faf9; font-size: 13px; font-weight: 700; }
        QToolTip { background: #263b43; color: #f2faf9; border: 1px solid #5c8f8e; padding: 5px; }
    """)
