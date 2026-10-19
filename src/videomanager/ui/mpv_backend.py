"""Optional libmpv playback embedded in the existing Qt player surface."""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QWidget


def find_mpv_directory() -> Path | None:
    if os.name != "nt":
        return None
    candidates: list[Path] = []
    if getattr(sys, "_MEIPASS", None):
        candidates.append(Path(sys._MEIPASS) / "mpv")
    if os.environ.get("VIDEOMANAGER_MPV_DIR"):
        candidates.append(Path(os.environ["VIDEOMANAGER_MPV_DIR"]))
    for variable in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        if os.environ.get(variable):
            base = Path(os.environ[variable])
            candidates.extend((base / "mpv", base / "Programs" / "mpv"))
    executable = shutil.which("mpv.exe")
    if executable:
        candidates.append(Path(executable).parent)
    for directory in candidates:
        if any((directory / name).is_file() for name in ("mpv-2.dll", "mpv-1.dll", "libmpv-2.dll")):
            return directory
    return None


def _bind(library, name: str, result, *arguments):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = list(arguments)
    return function


class _MpvEvent(ctypes.Structure):
    _fields_ = [("event_id", ctypes.c_int), ("error", ctypes.c_int),
                ("reply_userdata", ctypes.c_uint64), ("data", ctypes.c_void_p)]


class _MpvEndFile(ctypes.Structure):
    _fields_ = [("reason", ctypes.c_int), ("error", ctypes.c_int)]


class MpvAudioOutput:
    def __init__(self, backend: "MpvPlayer") -> None:
        self.backend = backend
        self._volume = 0.7
        self._muted = False

    def setVolume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))
        self.apply()

    def setMuted(self, muted: bool) -> None:
        self._muted = bool(muted)
        self.apply()

    def isMuted(self) -> bool:
        return self._muted

    def apply(self) -> None:
        if self.backend._handle:
            self.backend._set_double(b"volume", self._volume * 100)
            self.backend._set_flag(b"mute", self._muted)


class MpvPlayer(QObject):
    """The QMediaPlayer subset consumed by PlayerView."""

    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playbackStateChanged = Signal(object)
    mediaStatusChanged = Signal(object)
    errorOccurred = Signal(object, str)
    firstFrameReady = Signal()

    FLAG = 3
    INT64 = 4
    DOUBLE = 5

    def __init__(self, video_widget: QWidget, directory: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.video_widget = video_widget
        self._dll_directory = os.add_dll_directory(str(directory))
        self._handle = None
        try:
            dll = next(directory / name for name in ("mpv-2.dll", "mpv-1.dll", "libmpv-2.dll")
                       if (directory / name).is_file())
            self._lib = ctypes.CDLL(str(dll))
            pointer = ctypes.c_void_p
            self._create = _bind(self._lib, "mpv_create", pointer)
            self._initialize = _bind(self._lib, "mpv_initialize", ctypes.c_int, pointer)
            self._destroy = _bind(self._lib, "mpv_terminate_destroy", None, pointer)
            self._option = _bind(self._lib, "mpv_set_option", ctypes.c_int,
                                 pointer, ctypes.c_char_p, ctypes.c_int, pointer)
            self._option_string = _bind(self._lib, "mpv_set_option_string", ctypes.c_int,
                                        pointer, ctypes.c_char_p, ctypes.c_char_p)
            self._command = _bind(self._lib, "mpv_command", ctypes.c_int,
                                  pointer, ctypes.POINTER(ctypes.c_char_p))
            self._get = _bind(self._lib, "mpv_get_property", ctypes.c_int,
                              pointer, ctypes.c_char_p, ctypes.c_int, pointer)
            self._set = _bind(self._lib, "mpv_set_property", ctypes.c_int,
                              pointer, ctypes.c_char_p, ctypes.c_int, pointer)
            self._wait_event = _bind(self._lib, "mpv_wait_event", ctypes.POINTER(_MpvEvent),
                                     pointer, ctypes.c_double)
            self._handle = self._create()
            if not self._handle:
                raise RuntimeError("libmpv 無法建立播放器")
            wid = ctypes.c_int64(int(video_widget.winId()))
            if self._option(self._handle, b"wid", self.INT64, ctypes.byref(wid)) < 0:
                raise RuntimeError("libmpv 無法連接影片畫面")
            for name, value in ((b"input-default-bindings", b"no"),
                                (b"input-vo-keyboard", b"no"),
                                (b"osc", b"no"), (b"hwdec", b"auto-safe")):
                self._option_string(self._handle, name, value)
            if self._initialize(self._handle) < 0:
                raise RuntimeError("libmpv 無法啟動")
            self.audio = MpvAudioOutput(self)
            self._source: Path | None = None
            self._loaded = False
            self._last_position = -1
            self._last_duration = -1
            self._last_state = QMediaPlayer.StoppedState
            self._first_frame_reported = False
            self._end_reported = False
            self._timer = QTimer(self)
            self._timer.setInterval(120)
            self._timer.timeout.connect(self._poll)
            self._timer.start()
        except Exception:
            if self._handle:
                self._destroy(self._handle)
                self._handle = None
            self._dll_directory.close()
            raise

    def _call(self, *arguments: bytes) -> int:
        args = (ctypes.c_char_p * (len(arguments) + 1))(*arguments, None)
        return self._command(self._handle, args)

    def _get_double(self, name: bytes) -> float:
        value = ctypes.c_double()
        return value.value if self._handle and self._get(
            self._handle, name, self.DOUBLE, ctypes.byref(value)) >= 0 else 0.0

    def _get_flag(self, name: bytes) -> bool:
        value = ctypes.c_int()
        return bool(value.value) if self._handle and self._get(
            self._handle, name, self.FLAG, ctypes.byref(value)) >= 0 else False

    def _set_double(self, name: bytes, number: float) -> None:
        value = ctypes.c_double(number)
        self._set(self._handle, name, self.DOUBLE, ctypes.byref(value))

    def _set_flag(self, name: bytes, enabled: bool) -> None:
        value = ctypes.c_int(int(enabled))
        self._set(self._handle, name, self.FLAG, ctypes.byref(value))

    def setSource(self, source: QUrl) -> None:
        self.stop()
        self._source = None
        self._loaded = False
        self._last_position = -1
        self._last_duration = -1
        self._first_frame_reported = False
        self._end_reported = False
        self.positionChanged.emit(0)
        self.durationChanged.emit(0)
        if not source.isEmpty():
            self._source = Path(source.toLocalFile())

    def setVideoOutput(self, widget: QWidget | None) -> None:
        # A new MpvPlayer is created for each native surface.
        pass

    def play(self) -> None:
        if self._source is None:
            return
        if not self._loaded or self._get_flag(b"idle-active"):
            result = self._call(b"loadfile", str(self._source).encode("utf-8"), b"replace")
            if result < 0:
                self.errorOccurred.emit(QMediaPlayer.ResourceError, "libmpv 無法開啟影片")
            else:
                self._loaded = True
        else:
            self._set_flag(b"pause", False)
        self.audio.apply()

    def pause(self) -> None:
        self._set_flag(b"pause", True)

    def stop(self) -> None:
        if self._handle:
            self._call(b"stop")

    def setPosition(self, milliseconds: int) -> None:
        if self._source is not None:
            self._set_double(b"time-pos", max(0, milliseconds) / 1000)

    def position(self) -> int:
        return max(0, round(self._get_double(b"time-pos") * 1000))

    def duration(self) -> int:
        return max(0, round(self._get_double(b"duration") * 1000))

    def playbackState(self):
        if self._source is None or self._get_flag(b"idle-active"):
            return QMediaPlayer.StoppedState
        return QMediaPlayer.PausedState if self._get_flag(b"pause") else QMediaPlayer.PlayingState

    def _poll(self) -> None:
        if self._source is None:
            return
        for _ in range(32):
            event = self._wait_event(self._handle, 0.0).contents
            if event.event_id == 0:
                break
            if event.event_id == 7 and event.data:
                result = ctypes.cast(event.data, ctypes.POINTER(_MpvEndFile)).contents
                if result.reason == 0 and not self._end_reported:
                    self._end_reported = True
                    self.mediaStatusChanged.emit(QMediaPlayer.EndOfMedia)
                    return
                if result.reason == 4:
                    self.errorOccurred.emit(QMediaPlayer.ResourceError,
                                            f"libmpv 播放失敗（錯誤碼 {result.error}）")
                    return
        duration = self.duration()
        position = self.position()
        state = self.playbackState()
        if duration != self._last_duration:
            self._last_duration = duration
            self.durationChanged.emit(duration)
        if position != self._last_position:
            self._last_position = position
            self.positionChanged.emit(position)
        if state != self._last_state:
            self._last_state = state
            self.playbackStateChanged.emit(state)
        if state == QMediaPlayer.PlayingState and not self._first_frame_reported:
            self._first_frame_reported = True
            self.firstFrameReady.emit()
        if self._get_flag(b"eof-reached") and not self._end_reported:
            self._end_reported = True
            self.mediaStatusChanged.emit(QMediaPlayer.EndOfMedia)

    def close(self) -> None:
        self._timer.stop()
        if self._handle:
            self._destroy(self._handle)
            self._handle = None
        self._dll_directory.close()
