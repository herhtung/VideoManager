"""Optional LibVLC 3 playback inside a native Qt widget on Windows.

Qt still owns the window and controls. LibVLC only decodes, synchronizes, and
draws video, avoiding Qt Multimedia's dropped frames for some MP4 streams.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QWidget


def find_vlc_directory() -> Path | None:
    if os.name != "nt":
        return None
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "vlc" if getattr(sys, "_MEIPASS", None) else None
    configured = Path(os.environ["VIDEOMANAGER_VLC_DIR"]) if os.environ.get("VIDEOMANAGER_VLC_DIR") else None
    candidates = [bundled, configured]
    for variable in ("ProgramFiles", "ProgramW6432"):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / "VideoLAN" / "VLC")
    for directory in candidates:
        if (directory is not None and (directory / "libvlc.dll").is_file()
                and (directory / "libvlccore.dll").is_file()
                and (directory / "plugins").is_dir()):
            return directory
    return None


def _bind(library, name: str, result, *arguments):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = list(arguments)
    return function


class VlcAudioOutput:
    def __init__(self, backend: "VlcPlayer") -> None:
        self.backend = backend
        self._volume = 1.0
        self._muted = False

    def setVolume(self, volume: float) -> None:
        self._volume = max(0.0, min(2.0, volume))
        self.apply()

    def setMuted(self, muted: bool) -> None:
        self._muted = bool(muted)
        self.apply()

    def isMuted(self) -> bool:
        return self._muted

    def apply(self) -> None:
        if self.backend._player:
            self.backend._audio_set_volume(self.backend._player, round(self._volume * 100))
            self.backend._audio_set_mute(self.backend._player, int(self._muted))


class VlcPlayer(QObject):
    """The small QMediaPlayer-compatible surface used by PlayerView."""

    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playbackStateChanged = Signal(object)
    mediaStatusChanged = Signal(object)
    errorOccurred = Signal(object, str)
    firstFrameReady = Signal()

    def __init__(self, video_widget: QWidget, directory: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.video_widget = video_widget
        self.directory = directory
        self._dll_directory = os.add_dll_directory(str(directory))
        os.environ["VLC_PLUGIN_PATH"] = str(directory / "plugins")
        try:
            self._lib = ctypes.WinDLL(str(directory / "libvlc.dll"))
            pointer = ctypes.c_void_p
            integer = ctypes.c_int
            self._new = _bind(self._lib, "libvlc_new", pointer, integer,
                              ctypes.POINTER(ctypes.c_char_p))
            self._release_instance = _bind(self._lib, "libvlc_release", None, pointer)
            self._new_media = _bind(self._lib, "libvlc_media_new_path", pointer, pointer, ctypes.c_char_p)
            self._release_media = _bind(self._lib, "libvlc_media_release", None, pointer)
            self._new_player = _bind(self._lib, "libvlc_media_player_new", pointer, pointer)
            self._release_player = _bind(self._lib, "libvlc_media_player_release", None, pointer)
            self._set_media = _bind(self._lib, "libvlc_media_player_set_media", None, pointer, pointer)
            self._set_hwnd = _bind(self._lib, "libvlc_media_player_set_hwnd", None, pointer, pointer)
            self._play = _bind(self._lib, "libvlc_media_player_play", integer, pointer)
            self._pause = _bind(self._lib, "libvlc_media_player_set_pause", None, pointer, integer)
            self._stop = _bind(self._lib, "libvlc_media_player_stop", None, pointer)
            self._get_time = _bind(self._lib, "libvlc_media_player_get_time", ctypes.c_longlong, pointer)
            self._set_time = _bind(self._lib, "libvlc_media_player_set_time", None,
                                   pointer, ctypes.c_longlong)
            self._get_length = _bind(self._lib, "libvlc_media_player_get_length", ctypes.c_longlong, pointer)
            self._get_state = _bind(self._lib, "libvlc_media_player_get_state", integer, pointer)
            self._audio_set_volume = _bind(self._lib, "libvlc_audio_set_volume", integer, pointer, integer)
            self._audio_set_mute = _bind(self._lib, "libvlc_audio_set_mute", None, pointer, integer)
            self._mouse_input = _bind(self._lib, "libvlc_video_set_mouse_input", None,
                                      pointer, ctypes.c_uint)
            self._key_input = _bind(self._lib, "libvlc_video_set_key_input", None,
                                    pointer, ctypes.c_uint)
            args = (ctypes.c_char_p * 3)(b"--quiet", b"--no-video-title-show", b"--no-osd")
            self._instance = self._new(len(args), args)
            if not self._instance:
                raise RuntimeError("VLC 播放核心無法啟動")
            self._player = self._new_player(self._instance)
            if not self._player:
                raise RuntimeError("VLC 播放器無法建立")
            self._set_hwnd(self._player, ctypes.c_void_p(int(video_widget.winId())))
            self._mouse_input(self._player, 0)
            self._key_input(self._player, 0)
            self.audio = VlcAudioOutput(self)
            self._source: Path | None = None
            self._last_position = -1
            self._last_duration = -1
            self._last_state = -1
            self._first_frame_reported = False
            self._timer = QTimer(self)
            self._timer.setInterval(100)
            self._timer.timeout.connect(self._poll)
            self._timer.start()
        except Exception:
            if getattr(self, "_player", None):
                self._release_player(self._player)
            if getattr(self, "_instance", None):
                self._release_instance(self._instance)
            self._dll_directory.close()
            raise

    def setSource(self, source: QUrl) -> None:
        self.stop()
        self._set_media(self._player, None)
        self._source = None
        self._last_position = -1
        self._last_duration = -1
        self._last_state = -1
        self._first_frame_reported = False
        self.positionChanged.emit(0)
        self.durationChanged.emit(0)
        if source.isEmpty():
            return
        path = Path(source.toLocalFile())
        media = self._new_media(self._instance, str(path).encode("utf-8"))
        if not media:
            self.errorOccurred.emit(QMediaPlayer.ResourceError, "VLC 無法開啟影片")
            return
        try:
            self._set_media(self._player, media)
            self._source = path
        finally:
            self._release_media(media)

    def setVideoOutput(self, widget: QWidget | None) -> None:
        if self._player:
            handle = ctypes.c_void_p(int(widget.winId())) if widget is not None else None
            self._set_hwnd(self._player, handle)

    def play(self) -> None:
        if self._source is None:
            return
        if self._play(self._player) < 0:
            self.errorOccurred.emit(QMediaPlayer.ResourceError, "VLC 無法開始播放")
            return
        self.audio.apply()

    def pause(self) -> None:
        self._pause(self._player, 1)

    def stop(self) -> None:
        if self._player:
            self._stop(self._player)

    def setPosition(self, milliseconds: int) -> None:
        if self._source is not None:
            self._set_time(self._player, max(0, int(milliseconds)))

    def position(self) -> int:
        return max(0, int(self._get_time(self._player))) if self._player else 0

    def duration(self) -> int:
        return max(0, int(self._get_length(self._player))) if self._player else 0

    def playbackState(self):
        state = self._get_state(self._player) if self._player else 5
        if state == 3:
            return QMediaPlayer.PlayingState
        if state == 4:
            return QMediaPlayer.PausedState
        return QMediaPlayer.StoppedState

    def _poll(self) -> None:
        if not self._player or self._source is None:
            return
        state = self._get_state(self._player)
        length = self.duration()
        position = self.position()
        if length != self._last_duration:
            self._last_duration = length
            self.durationChanged.emit(length)
        if position != self._last_position:
            self._last_position = position
            self.positionChanged.emit(position)
        if state != self._last_state:
            self._last_state = state
            self.playbackStateChanged.emit(self.playbackState())
            if state == 6:
                self.mediaStatusChanged.emit(QMediaPlayer.EndOfMedia)
            elif state == 7:
                self.errorOccurred.emit(QMediaPlayer.ResourceError, "VLC 無法播放這部影片")
        if state == 3 and not self._first_frame_reported:
            self._first_frame_reported = True
            self.firstFrameReady.emit()

    def close(self) -> None:
        self._timer.stop()
        self.stop()
        if self._player:
            self._release_player(self._player)
            self._player = None
        if self._instance:
            self._release_instance(self._instance)
            self._instance = None
        self._dll_directory.close()
