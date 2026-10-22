"""Qt audio gain above 100%, without changing the ordinary playback path."""
from array import array

from PySide6.QtCore import QTimer, Signal
from PySide6.QtMultimedia import QAudioBufferOutput, QAudioFormat, QAudioOutput, QAudioSink, QMediaPlayer


def amplify_pcm(data: bytes, sample_format, gain: float) -> bytes:
    formats = {QAudioFormat.UInt8: ("B", -128, 127, 128),
               QAudioFormat.Int16: ("h", -32768, 32767, 0),
               QAudioFormat.Int32: ("i", -2147483648, 2147483647, 0),
               QAudioFormat.Float: ("f", -1.0, 1.0, 0)}
    code, low, high, offset = formats[sample_format]
    samples = array(code)
    samples.frombytes(data)
    for index, sample in enumerate(samples):
        value = max(low, min(high, (sample - offset) * gain))
        samples[index] = value if code == "f" else round(value) + offset
    return samples.tobytes()


class QtAudioOutput(QAudioOutput):
    boost_failed = Signal()

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self._player = player
        self._gain = 1.0
        self._muted = False
        self._sink = self._output = None
        self._pending = bytearray()
        self._last_position = 0
        self._capture = QAudioBufferOutput(self.device().preferredFormat(), self)
        self._capture.audioBufferReceived.connect(self._receive)
        player.setAudioBufferOutput(self._capture)
        player.playbackStateChanged.connect(self._state_changed)
        player.sourceChanged.connect(self._reset)
        player.positionChanged.connect(self._position_changed)
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(10)
        self._flush_timer.timeout.connect(self._flush)

    def setVolume(self, volume):
        gain = max(0.0, min(2.0, volume))
        if gain != self._gain:
            self._reset()
        self._gain = gain
        super().setVolume(min(1.0, gain))
        super().setMuted(self._muted or gain > 1.0)

    def setMuted(self, muted):
        self._muted = bool(muted)
        if muted:
            self._reset()
        super().setMuted(self._muted or self._gain > 1.0)

    def isMuted(self):
        return self._muted

    def _state_changed(self, state):
        if state != QMediaPlayer.PlayingState:
            self._reset()

    def _position_changed(self, position):
        if abs(position - self._last_position) > 1000:
            self._reset()
        self._last_position = position

    def _reset(self, *_args):
        self._flush_timer.stop()
        self._pending.clear()
        if self._sink is not None:
            self._sink.stop()
            self._sink.deleteLater()
        self._sink = self._output = None

    def _receive(self, buffer):
        if (self._gain <= 1.0 or self._muted or not buffer.isValid()
                or self._player.playbackState() != QMediaPlayer.PlayingState):
            return
        fmt = buffer.format()
        if self._sink is None or self._sink.format() != fmt:
            self._reset()
            self._sink = QAudioSink(self.device(), fmt, self)
            self._sink.setBufferSize(max(4096, fmt.bytesForDuration(60000)))
            self._output = self._sink.start()
        if self._output is None:
            self.setVolume(1.0)
            self.boost_failed.emit()
            return
        data = bytes(buffer.constData())[:buffer.byteCount()]
        self._pending.extend(amplify_pcm(data, fmt.sampleFormat(), self._gain))
        limit = fmt.bytesForDuration(120000)
        if limit and len(self._pending) > limit:
            discard = len(self._pending) - limit
            frame = max(1, fmt.bytesPerFrame())
            discard = (discard + frame - 1) // frame * frame
            del self._pending[:discard]
        self._flush()
        if self._pending:
            self._flush_timer.start()

    def _flush(self):
        if self._output is not None and self._pending:
            count = min(len(self._pending), self._sink.bytesFree())
            count -= count % max(1, self._sink.format().bytesPerFrame())
            if count:
                written = self._output.write(bytes(self._pending[:count]))
                if written > 0:
                    del self._pending[:written]
                elif written < 0:
                    self.setVolume(1.0)
                    self.boost_failed.emit()
                    return
        if not self._pending:
            self._flush_timer.stop()
