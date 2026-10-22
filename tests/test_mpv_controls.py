import ctypes
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QObject, QTimer
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication
from videomanager.ui.mpv_backend import MpvAudioOutput, MpvPlayer, _MpvEvent, _MpvProperty
from videomanager.ui.vlc_backend import VlcAudioOutput


class MpvControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def player(self):
        player = MpvPlayer.__new__(MpvPlayer)
        QObject.__init__(player)
        player._handle = 1
        player._source = Path('test.mp4')
        player._values = {b'time-pos': 40.0, b'duration': 100.0, b'pause': True,
                          b'idle-active': False, b'eof-reached': False}
        player._loaded = player._file_ready = True
        player._loading = player._end_reported = player._first_frame_reported = False
        player._last_position = player._last_duration = -1
        player._last_state = QMediaPlayer.PausedState
        player._paused_at = time.monotonic() - 600
        player._recovery_position_ms = None
        player._resume_timer = QTimer(player)
        player._command = Mock(return_value=0)
        player._set = Mock(return_value=0)
        player.audio = MpvAudioOutput(player)
        self.addCleanup(player._resume_timer.stop)
        return player

    def test_long_pause_healthy_resume_does_not_reload_and_manual_seek_cancels_watchdog(self):
        player = self.player()
        player.play()
        self.assertTrue(player._resume_timer.isActive())
        player._values[b'time-pos'] = 41.0
        player._check_resume()
        player._command.assert_not_called()
        player.setPosition(50000)
        self.assertFalse(player._resume_timer.isActive())
        arguments = player._command.call_args.args[2]
        self.assertEqual([arguments[i] for i in range(3)], [b'seek', b'50.000', b'absolute+exact'])

    def test_stalled_resume_reloads_and_restores_position_after_file_loaded(self):
        player = self.player()
        player.play()
        player._check_resume()
        self.assertEqual(player._recovery_position_ms, 40000)
        loaded, none = _MpvEvent(8, 0, 0, None), _MpvEvent(0, 0, 0, None)
        player._wait_event = Mock(side_effect=[ctypes.pointer(loaded), ctypes.pointer(none)])
        player._poll()
        self.assertIsNone(player._recovery_position_ms)
        arguments = player._command.call_args.args[2]
        self.assertEqual([arguments[i] for i in range(3)], [b'seek', b'40.000', b'absolute+exact'])
        self.assertEqual(player.playbackState(), QMediaPlayer.PlayingState)
        player.pause()
        self.assertFalse(player._resume_timer.isActive())

    def test_observed_position_is_cached_without_synchronous_native_reads(self):
        player = self.player()
        value = ctypes.c_double(55.5)
        prop = _MpvProperty(b'time-pos', player.DOUBLE, ctypes.cast(ctypes.pointer(value), ctypes.c_void_p))
        event = _MpvEvent(22, 0, 0, ctypes.cast(ctypes.pointer(prop), ctypes.c_void_p))
        none = _MpvEvent(0, 0, 0, None)
        player._wait_event = Mock(side_effect=[ctypes.pointer(event), ctypes.pointer(none)])
        player._poll()
        self.assertEqual(player.position(), 55500)
        self.assertFalse(hasattr(player, '_get'))

    def test_seek_while_loading_waits_for_file_and_does_not_cancel_user_pause(self):
        player = self.player()
        player._loading = True
        player.setPosition(30000)
        player._command.assert_not_called()
        player.pause()
        loaded, none = _MpvEvent(8, 0, 0, None), _MpvEvent(0, 0, 0, None)
        player._wait_event = Mock(side_effect=[ctypes.pointer(loaded), ctypes.pointer(none)])
        player._poll()
        self.assertIsNone(player._recovery_position_ms)
        arguments = player._command.call_args.args[2]
        self.assertEqual([arguments[i] for i in range(3)], [b'seek', b'30.000', b'absolute+exact'])
        self.assertEqual(player.playbackState(), QMediaPlayer.PausedState)

    def test_native_audio_adapters_default_to_100_and_accept_200(self):
        mpv = SimpleNamespace(_handle=1, _set_double=Mock(), _set_flag=Mock())
        audio = MpvAudioOutput(mpv)
        self.assertEqual(audio._volume, 1.0)
        audio.setVolume(2.0)
        mpv._set_double.assert_called_with(b'volume', 200.0)
        audio.setVolume(-1)
        mpv._set_double.assert_called_with(b'volume', 0.0)
        vlc = SimpleNamespace(_player=1, _audio_set_volume=Mock(), _audio_set_mute=Mock())
        audio = VlcAudioOutput(vlc)
        self.assertEqual(audio._volume, 1.0)
        audio.setVolume(3.0)
        vlc._audio_set_volume.assert_called_with(1, 200)
