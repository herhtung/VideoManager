"""Preview density and hover timing settings."""

from __future__ import annotations

from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QLabel, QPushButton, QScrollArea,
                               QDoubleSpinBox, QSpinBox, QVBoxLayout, QWidget)

from videomanager.config import Settings
from videomanager.ui.import_name_rules import ImportNameRulesEditor
from videomanager.ui.vlc_backend import find_vlc_directory
from videomanager.ui.mpv_backend import find_mpv_directory


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("設定")
        screen = parent.screen() if parent is not None else QApplication.primaryScreen()
        area = screen.availableGeometry()
        self.resize(min(720, area.width() - 32), min(640, area.height() - 64))
        self.choose_library_requested = False
        self.reset_library_requested = False
        self.scan_existing_requested = False
        layout = QVBoxLayout(self)
        content = QWidget()
        form = QFormLayout(content)
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
        self.related_hover = QDoubleSpinBox()
        self.related_hover.setRange(1, 10)
        self.related_hover.setSingleStep(0.5)
        self.related_hover.setSuffix(" 秒")
        self.related_hover.setValue(settings.related_hover_delay_ms / 1000)
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
        form.addRow("相似影片浮欄延遲", self.related_hover)
        form.addRow("每張停留", self.interval)
        playback_note = QLabel("此處決定每次開啟影片時的預設播放器。影片內切換只對當次播放有效。")
        playback_note.setWordWrap(True)
        form.addRow("播放器", playback_note)
        self.playback_backend = QComboBox()
        for label, key, available in (("Qt（內建）", "qt", True),
                                      ("VLC（需要已安裝 VLC）", "vlc", find_vlc_directory() is not None),
                                      ("libmpv（需要可用的 mpv 函式庫）", "mpv", find_mpv_directory() is not None)):
            self.playback_backend.addItem(label, key)
            self.playback_backend.model().item(self.playback_backend.count() - 1).setEnabled(available)
        self.playback_backend.setCurrentIndex(self.playback_backend.findData(settings.playback_backend))
        form.addRow("預設播放器", self.playback_backend)
        self.remove_phrases = ImportNameRulesEditor(settings.import_remove_phrases)
        form.addRow("匯入檔名清理", self.remove_phrases)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
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
            related_hover_delay_ms=round(self.related_hover.value() * 1000),
            frame_interval_ms=self.interval.value(),
            thumbnail_width=self.size.currentData(),
            scan_folders=previous.scan_folders,
            import_remove_phrases=self.remove_phrases.phrases(),
            playback_backend=self.playback_backend.currentData(),
            vlc_video_ids=(),
        )
