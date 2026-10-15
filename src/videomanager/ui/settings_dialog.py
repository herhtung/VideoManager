"""Preview density and hover timing settings."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout

from videomanager.config import Settings
from videomanager.ui.import_name_rules import ImportNameRulesEditor


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.resize(720, 640)
        self.choose_library_requested = False
        self.reset_library_requested = False
        self.scan_existing_requested = False
        layout = QVBoxLayout(self)
        form = QFormLayout()
        library_path = QLabel(settings.last_root_path or "尚未設定")
        library_path.setWordWrap(True)
        form.addRow("影片庫根目錄", library_path)
        choose_root = QPushButton("設定／重新連接根目錄…")
        choose_root.clicked.connect(self._choose_library)
        form.addRow("", choose_root)
        reset_root = QPushButton("改用新的空白管理根目錄…")
        reset_root.clicked.connect(self._reset_library)
        reset_root.setEnabled(settings.library_id is not None)
        form.addRow("", reset_root)
        explanation = QLabel("匯入的影片會依加入月份存放在這個根目錄底下，每個編號資料夾最多 50 部。")
        explanation.setWordWrap(True)
        form.addRow("", explanation)
        self.mode = QComboBox()
        self.mode.addItem("標準 — 10 張，快速辨識影片內容", "standard")
        self.mode.addItem("詳細 — 30 張，查看更多場景變化", "detailed")
        self.mode.addItem("高密度 — 60 張，適合長片或變化較多的影片", "dense")
        self.mode.setCurrentIndex(self.mode.findData(settings.preview_mode))
        self.hover = QSpinBox()
        self.hover.setRange(100, 2000)
        self.hover.setSuffix(" ms")
        self.hover.setValue(settings.hover_delay_ms)
        self.interval = QSpinBox()
        self.interval.setRange(100, 3000)
        self.interval.setSuffix(" ms")
        self.interval.setValue(settings.frame_interval_ms)
        self.size = QComboBox()
        for label, width in (("小", 140), ("中", 180), ("大", 240)):
            self.size.addItem(label, width)
        self.size.setCurrentIndex(self.size.findData(settings.thumbnail_width))
        form.addRow("預覽模式", self.mode)
        form.addRow("縮圖大小", self.size)
        form.addRow("懸停等待", self.hover)
        form.addRow("每張停留", self.interval)
        self.remove_phrases = ImportNameRulesEditor(settings.import_remove_phrases)
        form.addRow("匯入檔名清理", self.remove_phrases)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        scan_existing = buttons.addButton("儲存並掃描現有影片…", QDialogButtonBox.ActionRole)
        scan_existing.clicked.connect(self._scan_existing)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _choose_library(self) -> None:
        self.choose_library_requested = True
        self.accept()

    def _reset_library(self) -> None:
        self.reset_library_requested = True
        self.accept()

    def _scan_existing(self) -> None:
        self.scan_existing_requested = True
        self.accept()

    def updated(self, previous: Settings) -> Settings:
        return Settings(
            library_id=previous.library_id,
            last_root_path=previous.last_root_path,
            preview_mode=self.mode.currentData(),
            preview_policy=previous.preview_policy,
            hover_delay_ms=self.hover.value(),
            frame_interval_ms=self.interval.value(),
            thumbnail_width=self.size.currentData(),
            scan_folders=previous.scan_folders,
            import_remove_phrases=self.remove_phrases.phrases(),
        )
