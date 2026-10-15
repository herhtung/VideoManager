"""MP4 player with the same metadata editor as the browser."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QSlider, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

from videomanager.ui.details_panel import DetailsPanel


class ClickSeekSlider(QSlider):
    """A click anywhere on the timeline seeks to that fraction of the video."""

    clicked_position = Signal(int)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.maximum() > self.minimum():
            value = self.minimum() + round(
                (self.maximum() - self.minimum()) * max(0.0, min(1.0, event.position().x() / max(1, self.width())))
            )
            super().mousePressEvent(event)
            self.setValue(value)
            self.clicked_position.emit(value)
        else:
            super().mousePressEvent(event)


class PlayerView(QDialog):
    metadata_saved = Signal()
    playback_closed = Signal()

    def __init__(self, connection: sqlite3.Connection, video_id: str, source: Path, parent=None,
                 *, segments: list[tuple[str, Path]] | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.Window | Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowTitle(source.name)
        self.resize(1100, 700)
        self._was_maximized_before_fullscreen = False
        self._details_open = False
        self.connection = connection
        self.details: DetailsPanel | None = None
        self.source = source
        self.segments = segments or [(video_id, source)]
        self.segment_index = next((i for i, (identifier, _) in enumerate(self.segments)
                                   if identifier == video_id), 0)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget()
        self.player.setVideoOutput(self.video)
        self.error_label = QLabel("正在載入影片…")
        self.error_label.setWordWrap(True)
        self.seek = ClickSeekSlider(Qt.Horizontal)
        self.seek.setObjectName("playerSeek")
        self.seek.setFixedHeight(44)
        self.seek.setRange(0, 0)
        self.seek.sliderMoved.connect(self.player.setPosition)
        self.seek.clicked_position.connect(self.player.setPosition)
        self.player.positionChanged.connect(lambda position: None if self.seek.isSliderDown() else self.seek.setValue(position))
        self.player.durationChanged.connect(self.seek.setMaximum)
        self.time = QLabel("00:00")
        self.player.positionChanged.connect(lambda ms: self.time.setText(f"{ms // 60000:02d}:{(ms // 1000) % 60:02d}"))
        self.play_button = QPushButton("暫停")
        self.play_button.clicked.connect(self.toggle_play)
        back = QPushButton("−10 秒")
        back.clicked.connect(lambda: self.player.setPosition(max(0, self.player.position() - 10000)))
        forward = QPushButton("+10 秒")
        forward.clicked.connect(lambda: self.player.setPosition(min(self.player.duration(), self.player.position() + 10000)))
        self.previous_part = QPushButton("上一段")
        self.previous_part.clicked.connect(lambda: self.switch_segment(self.segment_index - 1))
        self.next_part = QPushButton("下一段")
        self.next_part.clicked.connect(lambda: self.switch_segment(self.segment_index + 1))
        self.part_label = QLabel()
        mute = QPushButton("靜音")
        mute.clicked.connect(lambda: self.audio.setMuted(not self.audio.isMuted()))
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(70)
        self.volume.setMaximumWidth(110)
        self.volume_label = QLabel("70%")
        self.volume.valueChanged.connect(self._set_volume)
        self.audio.setVolume(0.7)
        self.info_button = QPushButton("顯示資訊")
        self.info_button.clicked.connect(self.toggle_details)
        self.fullscreen_button = QPushButton("全螢幕播放")
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        external = QPushButton("以 Windows 預設播放器開啟")
        external.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.source))))
        close = QPushButton("返回列表")
        close.clicked.connect(self.close)
        for button in (self.play_button, back, forward, self.previous_part, self.next_part,
                       mute, self.info_button, self.fullscreen_button, external, close):
            button.setObjectName("playerControl")
        self.exit_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self.exit_shortcut.activated.connect(self.exit_fullscreen)

        controls = QHBoxLayout()
        for widget in (self.previous_part, back, self.play_button, forward,
                       self.next_part, self.part_label, self.time, mute, self.volume,
                       self.volume_label, external):
            controls.addWidget(widget)
        video_column = QVBoxLayout()
        title_bar = QHBoxLayout()
        self.title_label = QLabel(source.name)
        self.title_label.setObjectName("playerTitle")
        self.title_label.setToolTip(str(source))
        title_bar.addWidget(self.title_label, 1)
        for widget in (self.info_button, self.fullscreen_button, close):
            title_bar.addWidget(widget)
        video_column.addLayout(title_bar)
        video_column.addWidget(self.video, 1)
        video_column.addWidget(self.error_label)
        video_column.addWidget(self.seek)
        video_column.addLayout(controls)
        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        self.content = QWidget(self)
        self.content.setLayout(video_column)
        main.addWidget(self.content)
        self.details_overlay = QScrollArea(self.content)
        # QVideoWidget uses a native video surface on Windows. A child widget can
        # report visible while still being painted behind it, so use an owned tool
        # window positioned over the video instead.
        self.details_overlay.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.details_overlay.setWidgetResizable(True)
        self.details_overlay.setObjectName("playerDetailsOverlay")
        self.details_overlay.setStyleSheet(
            "QScrollArea#playerDetailsOverlay { background: #14212b; border: 1px solid #3b5361; }"
        )
        self.details_overlay.hide()
        self.video.installEventFilter(self)
        self._shortcuts: list[QShortcut] = []
        for owner in (self, self.details_overlay):
            for key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
                shortcut = QShortcut(QKeySequence(key), owner)
                shortcut.setContext(Qt.WindowShortcut)
                shortcut.activated.connect(lambda pressed=key: self._playback_key(pressed))
                self._shortcuts.append(shortcut)
        self.player.errorOccurred.connect(self._on_error)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.video.videoSink().videoFrameChanged.connect(self._on_video_frame)
        self._update_segment_buttons()
        self._start_source(source)

    def _start_source(self, path: Path) -> None:
        self.error_label.setText("正在載入影片…")
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def _on_video_frame(self, frame) -> None:
        if frame.isValid() and self.error_label.text() == "正在載入影片…":
            self.error_label.clear()

    def _ensure_details(self) -> DetailsPanel:
        if self.details is None:
            details = DetailsPanel(self.connection)
            details.set_video(self.segments[self.segment_index][0])
            details.saved.connect(self.metadata_saved.emit)
            self.details_overlay.setWidget(details)
            self.details = details
        return self.details

    def _place_details_overlay(self) -> None:
        if not hasattr(self, "details_overlay"):
            return
        width = min(440, max(330, self.video.width() // 3))
        width = min(width, self.video.width())
        top_left = self.video.mapToGlobal(QPoint(self.video.width() - width, 0))
        self.details_overlay.setGeometry(top_left.x(), top_left.y(), width, self.video.height())

    def _sync_details_overlay(self) -> None:
        if not self._details_open or not self.isVisible() or self.isMinimized():
            self.details_overlay.hide()
            return
        self._place_details_overlay()
        self.details_overlay.show()
        self.details_overlay.raise_()

    def toggle_details(self) -> None:
        self._details_open = not self._details_open
        if self._details_open:
            self._ensure_details()
        self.info_button.setText("隱藏資訊" if self._details_open else "顯示資訊")
        self._sync_details_overlay()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.video and event.type() in (QEvent.Resize, QEvent.Move):
            QTimer.singleShot(0, self._sync_details_overlay)
        return super().eventFilter(watched, event)

    def _update_segment_buttons(self) -> None:
        self.previous_part.setVisible(len(self.segments) > 1)
        self.next_part.setVisible(len(self.segments) > 1)
        self.part_label.setVisible(len(self.segments) > 1)
        self.previous_part.setEnabled(self.segment_index > 0)
        self.next_part.setEnabled(self.segment_index + 1 < len(self.segments))
        self.part_label.setText(f"第 {self.segment_index + 1}/{len(self.segments)} 段")

    def switch_segment(self, index: int) -> None:
        if index < 0 or index >= len(self.segments):
            return
        identifier, path = self.segments[index]
        if not path.is_file():
            self.error_label.setText(f"找不到下一段 MP4：{path}")
            return
        if self.details is not None and self.details.is_dirty() and not self.details.save_current():
            return
        self.segment_index = index
        self.source = path
        self.setWindowTitle(path.name)
        self.title_label.setText(path.name)
        self.title_label.setToolTip(str(path))
        if self.details is not None:
            self.details.set_video(identifier)
        self.seek.setValue(0)
        self._update_segment_buttons()
        self._start_source(path)

    def _on_media_status(self, status) -> None:
        if status == QMediaPlayer.EndOfMedia and self.segment_index + 1 < len(self.segments):
            self.switch_segment(self.segment_index + 1)
        elif status == QMediaPlayer.InvalidMedia and self.error_label.text() == "正在載入影片…":
            self.error_label.setText("影片無法載入；可試用 Windows 預設播放器。")

    def toggle_maximized(self) -> None:
        if self.isFullScreen():
            self.exit_fullscreen()
            if not self.isMaximized():
                self.showMaximized()
        elif self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self._update_window_buttons()

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.exit_fullscreen()
        else:
            self._was_maximized_before_fullscreen = self.isMaximized()
            self._details_open = False
            self.details_overlay.hide()
            self.info_button.setText("顯示資訊")
            self.showFullScreen()
            self._update_window_buttons()

    def exit_fullscreen(self) -> None:
        if not self.isFullScreen():
            return
        if self._was_maximized_before_fullscreen:
            self.showMaximized()
        else:
            self.showNormal()
        self._update_window_buttons()

    def _update_window_buttons(self) -> None:
        if hasattr(self, "fullscreen_button"):
            self.fullscreen_button.setText("退出全螢幕" if self.isFullScreen() else "全螢幕播放")

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            self._update_window_buttons()
            QTimer.singleShot(0, self._sync_details_overlay)

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        QTimer.singleShot(0, self._sync_details_overlay)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._sync_details_overlay)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._sync_details_overlay)

    def _on_state(self, state) -> None:
        self.play_button.setText("暫停" if state == QMediaPlayer.PlayingState else "播放")

    def _on_error(self, error, message: str) -> None:
        if error != QMediaPlayer.NoError:
            self.error_label.setText(f"內建播放器無法播放：{message}。可改用 Windows 預設播放器。")

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _set_volume(self, value: int) -> None:
        self.audio.setVolume(value / 100)
        self.volume_label.setText(f"{value}%")

    def _playback_key(self, key: Qt.Key) -> None:
        if isinstance(QApplication.focusWidget(), (QLineEdit, QTextEdit, QSpinBox, QComboBox)):
            return
        if key == Qt.Key_Left:
            self.player.setPosition(max(0, self.player.position() - 10000))
        elif key == Qt.Key_Right:
            self.player.setPosition(min(self.player.duration(), self.player.position() + 10000))
        elif key == Qt.Key_Up:
            self.volume.setValue(min(100, self.volume.value() + 5))
            self.audio.setMuted(False)
        elif key == Qt.Key_Down:
            self.volume.setValue(max(0, self.volume.value() - 5))

    def keyPressEvent(self, event) -> None:
        if isinstance(self.focusWidget(), (QLineEdit, QTextEdit, QSpinBox)):
            super().keyPressEvent(event)
            return
        if event.key() == Qt.Key_Space:
            self.toggle_play()
        elif event.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            self._playback_key(event.key())
        elif event.key() == Qt.Key_Escape and self.isFullScreen():
            self.exit_fullscreen()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        if self.details is not None and self.details.is_dirty() and not self.details.save_current():
            event.ignore()
            return
        self.details_overlay.close()
        self.player.stop()
        self.player.setSource(QUrl())
        self.player.setVideoOutput(None)
        self.playback_closed.emit()
        super().closeEvent(event)
