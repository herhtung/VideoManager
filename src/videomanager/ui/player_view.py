"""MP4 player with the same metadata editor as the browser."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QPlainTextEdit, QPushButton, QScrollArea, QSlider, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

from videomanager.ui.details_panel import DetailsPanel
from videomanager.ui.vlc_backend import VlcPlayer, find_vlc_directory
from videomanager.ui.mpv_backend import MpvPlayer, find_mpv_directory


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
    compression_requested = Signal(str)
    cut_requested = Signal(str)

    def __init__(self, connection: sqlite3.Connection, video_id: str, source: Path, parent=None,
                 *, segments: list[tuple[str, Path]] | None = None,
                 embedded: bool = False, read_only: bool = False,
                 default_backend: str = "qt") -> None:
        super().__init__(parent)
        self.embedded = embedded
        self.read_only = read_only
        self.setWindowFlags(Qt.Widget if embedded else
                            Qt.Window | Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint)
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
        self._qt_player = self.player
        self._qt_audio = self.audio
        self._backend = "qt"
        self._vlc_directory = find_vlc_directory()
        self._mpv_directory = find_mpv_directory()
        self._resume_at_ms: int | None = None
        self._pause_after_resume = False
        self._pause_timer = QTimer(self)
        self._pause_timer.setSingleShot(True)
        self._pause_timer.timeout.connect(lambda: self.player.pause())
        self.video = QVideoWidget()
        self.player.setVideoOutput(self.video)
        self.error_label = QLabel("正在載入影片…")
        self.error_label.setWordWrap(True)
        self.seek = ClickSeekSlider(Qt.Horizontal)
        self.seek.setObjectName("playerSeek")
        self.seek.setFixedHeight(44)
        self.seek.setRange(0, 0)
        self.seek.sliderMoved.connect(lambda position: self.player.setPosition(position))
        self.seek.clicked_position.connect(lambda position: self.player.setPosition(position))
        self.time = QLabel("00:00 / 00:00")
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
        self.compress_button = QPushButton("壓縮")
        self.compress_button.clicked.connect(
            lambda: self.compression_requested.emit(self.segments[self.segment_index][0]))
        self.cut_button = QPushButton("剪輯")
        self.cut_button.clicked.connect(
            lambda: self.cut_requested.emit(self.segments[self.segment_index][0]))
        self.fullscreen_button = QPushButton("全螢幕播放")
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        external = QPushButton("以 Windows 預設播放器開啟")
        external.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.source))))
        self.backend_button = QPushButton("播放器：Qt")
        self.backend_button.setToolTip("只切換這次播放；預設播放器請在設定中修改。")
        backend_menu = QMenu(self.backend_button)
        backend_menu.addAction("Qt（內建）", lambda: self.switch_backend("qt"))
        if self._vlc_directory is not None:
            backend_menu.addAction("VLC", lambda: self.switch_backend("vlc"))
        if self._mpv_directory is not None:
            backend_menu.addAction("libmpv", lambda: self.switch_backend("mpv"))
        self.backend_button.setMenu(backend_menu)
        self.backend_button.setVisible(self._vlc_directory is not None or self._mpv_directory is not None)
        close = QPushButton("返回")
        close.clicked.connect(self.close)
        for button in (self.play_button, back, forward, self.previous_part, self.next_part,
                       mute, self.info_button, self.compress_button, self.cut_button,
                       self.fullscreen_button, external, self.backend_button, close):
            button.setObjectName("playerControl")
        self.exit_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self.exit_shortcut.activated.connect(self.exit_fullscreen)

        controls = QHBoxLayout()
        for widget in (self.previous_part, back, self.play_button, forward,
                       self.next_part, self.part_label, self.time, mute, self.volume,
                       self.volume_label, self.backend_button, external):
            controls.addWidget(widget)
        self.video_column = QVBoxLayout()
        self.video_column.setContentsMargins(4, 2, 4, 4)
        self.video_column.setSpacing(3)
        title_bar = QHBoxLayout()
        title_bar.setContentsMargins(0, 0, 0, 0)
        self.title_label = QLabel(source.name)
        self.title_label.setObjectName("playerTitle")
        self.title_label.setToolTip(str(source))
        title_bar.addWidget(self.title_label, 1)
        for widget in (self.info_button, self.compress_button, self.cut_button,
                       self.fullscreen_button, close):
            title_bar.addWidget(widget)
        if read_only:
            self.info_button.hide()
            self.compress_button.hide()
            self.cut_button.hide()
        self.video_column.addLayout(title_bar)
        self.video_column.addWidget(self.video, 1)
        self.video_column.addWidget(self.error_label)
        self.video_column.addWidget(self.seek)
        self.video_column.addLayout(controls)
        main = QHBoxLayout(self) if embedded else QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        self.content = QWidget(self)
        self.content.setLayout(self.video_column)
        main.addWidget(self.content, 1)
        self.details_overlay = QScrollArea(self if embedded else self.content)
        # QVideoWidget uses a native video surface on Windows. A child widget can
        # report visible while still being painted behind it, so use an owned tool
        # window positioned over the video instead.
        self.details_overlay.setWindowFlags(Qt.Widget if embedded else Qt.Tool | Qt.FramelessWindowHint)
        self.details_overlay.setWidgetResizable(True)
        self.details_overlay.setObjectName("playerDetailsOverlay")
        if embedded:
            self.details_overlay.setFixedWidth(360)
            main.addWidget(self.details_overlay)
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
        # The native video surface receives key events itself on Windows, so
        # PlayerView.keyPressEvent alone does not reliably see the space bar.
        space_owners = (self,) if embedded else (self, self.details_overlay)
        self._space_shortcuts: list[QShortcut] = []
        for owner in space_owners:
            shortcut = QShortcut(QKeySequence(Qt.Key_Space), owner)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self._toggle_playback_shortcut)
            self._shortcuts.append(shortcut)
            self._space_shortcuts.append(shortcut)
        QApplication.instance().focusChanged.connect(self._update_space_shortcuts)
        self._update_space_shortcuts()
        self._connect_player_signals()
        self._update_segment_buttons()
        if default_backend in {"vlc", "mpv"}:
            self.switch_backend(default_backend, play=True)
            if self._backend == "qt":
                self._start_source(source)
        else:
            self._start_source(source)

    def _start_source(self, path: Path) -> None:
        self.error_label.setText("正在載入影片…")
        self.error_label.show()
        self.time.setText("00:00 / 00:00")
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def _connect_player_signals(self) -> None:
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.errorOccurred.connect(self._on_error)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        if self._backend == "qt":
            self.video.videoSink().videoFrameChanged.connect(self._on_video_frame)
        else:
            self.player.firstFrameReady.connect(self._on_vlc_ready)

    def _disconnect_player_signals(self) -> None:
        self.player.positionChanged.disconnect(self._on_position)
        self.player.durationChanged.disconnect(self._on_duration)
        self.player.errorOccurred.disconnect(self._on_error)
        self.player.playbackStateChanged.disconnect(self._on_state)
        self.player.mediaStatusChanged.disconnect(self._on_media_status)
        if self._backend == "qt":
            self.video.videoSink().videoFrameChanged.disconnect(self._on_video_frame)
        else:
            self.player.firstFrameReady.disconnect(self._on_vlc_ready)

    def _on_position(self, position: int) -> None:
        if not self.seek.isSliderDown():
            self.seek.setValue(position)
        self._update_time()

    def _on_duration(self, duration: int) -> None:
        self.seek.setMaximum(duration)
        self._update_time()
        if duration > 0 and self._resume_at_ms is not None:
            target = min(self._resume_at_ms, duration)
            self._resume_at_ms = None
            self.player.setPosition(target)
            if self._pause_after_resume:
                self._pause_timer.start(200)
                self._pause_after_resume = False

    def _on_vlc_ready(self) -> None:
        self.error_label.clear()
        self.error_label.hide()

    def switch_backend(self, target: str, *, resume_position_ms: int | None = None,
                       play: bool | None = None) -> None:
        """Switch this playback session without writing a preference."""
        if target == self._backend or target not in {"qt", "vlc", "mpv"}:
            return
        if target == "vlc" and self._vlc_directory is None:
            return
        if target == "mpv" and self._mpv_directory is None:
            return
        position = self.player.position() if resume_position_ms is None else resume_position_ms
        was_playing = (self.player.playbackState() == QMediaPlayer.PlayingState
                       if play is None else play)
        previous_player = self.player
        previous_audio = self.audio
        previous_video = self.video
        previous_backend = self._backend
        self._pause_timer.stop()
        if target in {"vlc", "mpv"}:
            surface = QWidget(self.content)
            surface.setAttribute(Qt.WA_NativeWindow, True)
            surface.setStyleSheet("background: black;")
            try:
                replacement = (VlcPlayer(surface, self._vlc_directory, self) if target == "vlc"
                               else MpvPlayer(surface, self._mpv_directory, self))
            except (OSError, RuntimeError) as exc:
                surface.deleteLater()
                self.error_label.setText(f"{target} 無法啟動：{exc}。目前仍使用 {previous_backend} 播放。")
                self.error_label.show()
                return
            new_audio = replacement.audio
        else:
            surface = QVideoWidget(self.content)
            replacement = self._qt_player
            new_audio = self._qt_audio
        self._disconnect_player_signals()
        previous_player.stop()
        previous_player.setSource(QUrl())
        previous_player.setVideoOutput(None)
        previous_video.removeEventFilter(self)
        self.video_column.replaceWidget(previous_video, surface)
        previous_video.hide()
        previous_video.deleteLater()
        self.video = surface
        self.video.installEventFilter(self)
        self.player = replacement
        self.audio = new_audio
        self._backend = target
        if target == "qt":
            self.player.setVideoOutput(surface)
        self._connect_player_signals()
        self.audio.setVolume(self.volume.value() / 100)
        self.audio.setMuted(previous_audio.isMuted())
        self.backend_button.setText("播放器：" + {"qt": "Qt", "vlc": "VLC", "mpv": "libmpv"}[target])
        self._resume_at_ms = position
        self._pause_after_resume = not was_playing
        self._start_source(self.source)
        if previous_backend in {"vlc", "mpv"}:
            previous_player.close()
        QTimer.singleShot(0, self._sync_details_overlay)

    @staticmethod
    def _format_time(milliseconds: int) -> str:
        seconds = max(0, milliseconds // 1000)
        hours, rest = divmod(seconds, 3600)
        minutes, seconds = divmod(rest, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"

    def _update_time(self) -> None:
        self.time.setText(f"{self._format_time(self.player.position())} / "
                          f"{self._format_time(self.player.duration())}")

    def _on_video_frame(self, frame) -> None:
        if self._backend == "qt" and frame.isValid():
            self.error_label.clear()
            self.error_label.hide()

    def _ensure_details(self) -> DetailsPanel:
        if self.details is None:
            details = DetailsPanel(self.connection)
            details.set_video(self.segments[self.segment_index][0])
            details.saved.connect(self.metadata_saved.emit)
            self.details_overlay.setWidget(details)
            self.details = details
        return self.details

    def _place_details_overlay(self) -> None:
        if not hasattr(self, "details_overlay") or self.embedded:
            return
        width = min(440, max(330, self.video.width() // 3))
        width = min(width, self.video.width())
        top_left = self.video.mapToGlobal(QPoint(self.video.width() - width, 0))
        self.details_overlay.setGeometry(top_left.x(), top_left.y(), width, self.video.height())

    def _sync_details_overlay(self) -> None:
        if not self._details_open or not self.isVisible() or self.isMinimized():
            self.details_overlay.hide()
            return
        if self.embedded:
            self.details_overlay.show()
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
            self.error_label.show()
            return
        if self.details is not None and self.details.is_dirty() and not self.details.save_current():
            return
        self._pause_timer.stop()
        self._resume_at_ms = None
        self._pause_after_resume = False
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
            self.error_label.show()

    def toggle_maximized(self) -> None:
        target = self.window() if self.embedded else self
        if target.isFullScreen():
            self.exit_fullscreen()
            if not target.isMaximized():
                target.showMaximized()
        elif target.isMaximized():
            target.showNormal()
        else:
            target.showMaximized()
        self._update_window_buttons()

    def toggle_fullscreen(self) -> None:
        target = self.window() if self.embedded else self
        if target.isFullScreen():
            self.exit_fullscreen()
        else:
            self._was_maximized_before_fullscreen = target.isMaximized()
            self._details_open = False
            self.details_overlay.hide()
            self.info_button.setText("顯示資訊")
            target.showFullScreen()
            self._update_window_buttons()

    def exit_fullscreen(self) -> None:
        target = self.window() if self.embedded else self
        if not target.isFullScreen():
            return
        if self._was_maximized_before_fullscreen:
            target.showMaximized()
        else:
            target.showNormal()
        self._update_window_buttons()

    def _update_window_buttons(self) -> None:
        if hasattr(self, "fullscreen_button"):
            target = self.window() if self.embedded else self
            self.fullscreen_button.setText("退出全螢幕" if target.isFullScreen() else "全螢幕播放")

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
            backend = {"qt": "Qt", "vlc": "VLC", "mpv": "libmpv"}[self._backend]
            self.error_label.setText(f"{backend} 無法播放：{message}。可切換播放器或用 Windows 預設播放器。")
            self.error_label.show()

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    @staticmethod
    def _focus_accepts_space() -> bool:
        widget = QApplication.focusWidget()
        while widget is not None:
            if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox)):
                return True
            widget = widget.parentWidget()
        return False

    def _update_space_shortcuts(self, *_args) -> None:
        enabled = not self._focus_accepts_space()
        for shortcut in self._space_shortcuts:
            shortcut.setEnabled(enabled)

    def _toggle_playback_shortcut(self) -> None:
        if self._focus_accepts_space():
            return
        self.toggle_play()

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
            self._toggle_playback_shortcut()
        elif event.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            self._playback_key(event.key())
        elif event.key() == Qt.Key_Escape and (self.window() if self.embedded else self).isFullScreen():
            self.exit_fullscreen()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        if self.details is not None and self.details.is_dirty() and not self.details.save_current():
            event.ignore()
            return
        return_from_fullscreen = self.embedded and self.window().isFullScreen()
        self.exit_fullscreen()
        if return_from_fullscreen:
            self.window().showMaximized()
        QApplication.instance().focusChanged.disconnect(self._update_space_shortcuts)
        self.details_overlay.close()
        self._pause_timer.stop()
        self.player.stop()
        self.player.setSource(QUrl())
        self.player.setVideoOutput(None)
        if self._backend in {"vlc", "mpv"}:
            self.player.close()
        self.playback_closed.emit()
        super().closeEvent(event)
