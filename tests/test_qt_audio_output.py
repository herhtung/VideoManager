import os
import unittest
from array import array
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QByteArray, QObject, Signal
from PySide6.QtMultimedia import QAudioBuffer, QAudioFormat, QMediaPlayer
from PySide6.QtWidgets import QApplication
from videomanager.ui.qt_audio_output import QtAudioOutput, amplify_pcm


class _Player(QObject):
    playbackStateChanged = Signal(object)
    sourceChanged = Signal(object)
    positionChanged = Signal(int)

    def setAudioBufferOutput(self, capture):
        self.capture = capture

    def playbackState(self):
        return QMediaPlayer.PlayingState


class _Sink:
    instances = []

    def __init__(self, _device, fmt, _parent):
        self.fmt, self.data, self.stopped = fmt, bytearray(), False
        self.instances.append(self)

    def format(self):
        return self.fmt

    def setBufferSize(self, size):
        self.size = size

    def start(self):
        return self

    def write(self, data):
        self.data.extend(data)
        return len(data)

    def bytesFree(self):
        return self.size

    def stop(self):
        self.stopped = True

    def deleteLater(self):
        pass


class AudioGainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_pcm_gain_clips_without_wrapping(self):
        for fmt, code, values, expected in (
            (QAudioFormat.Int16, "h", [1000, -2000, 30000, -30000], [2000, -4000, 32767, -32768]),
            (QAudioFormat.UInt8, "B", [128, 160, 0, 255], [128, 192, 0, 255]),
            (QAudioFormat.Int32, "i", [100, -100, 2**30, -2**30], [200, -200, 2**31-1, -2**31]),
        ):
            output = array(code)
            output.frombytes(amplify_pcm(array(code, values).tobytes(), fmt, 2.0))
            self.assertEqual(list(output), expected)

    def test_qt_boost_mute_pause_and_seek_flush_audio(self):
        _Sink.instances.clear()
        player = _Player()
        audio = QtAudioOutput(player)
        fmt = QAudioFormat()
        fmt.setSampleRate(48000)
        fmt.setChannelCount(2)
        fmt.setSampleFormat(QAudioFormat.Float)
        data = array("f", [0.25, -0.25, 0.75, -0.75]).tobytes()
        buffer = QAudioBuffer(QByteArray(data), fmt)
        with patch("videomanager.ui.qt_audio_output.QAudioSink", _Sink):
            audio.setVolume(1.0)
            audio._receive(buffer)
            self.assertFalse(_Sink.instances)
            audio.setVolume(2.0)
            audio._receive(buffer)
            values = array("f")
            values.frombytes(_Sink.instances[-1].data)
            self.assertEqual(list(values), [0.5, -0.5, 1.0, -1.0])
            self.assertFalse(audio.isMuted())
            audio.setMuted(True)
            self.assertTrue(_Sink.instances[-1].stopped)
            audio._receive(buffer)
            self.assertEqual(len(_Sink.instances), 1)
            audio.setMuted(False)
            audio._receive(buffer)
            player.playbackStateChanged.emit(QMediaPlayer.PausedState)
            self.assertTrue(_Sink.instances[-1].stopped)
            audio._receive(buffer)
            player.positionChanged.emit(10000)
            self.assertTrue(_Sink.instances[-1].stopped)
            audio._receive(buffer)
            audio.setVolume(0.0)
            self.assertTrue(_Sink.instances[-1].stopped)
        audio.deleteLater()
