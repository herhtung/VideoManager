"""The ordinary player must remain usable without a VLC installation."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from videomanager.config import AppPaths, Settings, load_settings, save_settings
from videomanager.ui.player_view import PlayerView
from videomanager.ui.settings_dialog import SettingsDialog


class OptionalVlcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_missing_vlc_leaves_qt_as_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-optional-vlc-") as directory:
            source = Path(directory) / "unavailable.mp4"
            connection = sqlite3.connect(":memory:")
            with patch("videomanager.ui.player_view.find_vlc_directory", return_value=None), \
                    patch("videomanager.ui.player_view.find_mpv_directory", return_value=None):
                player = PlayerView(connection, "test-video", source)
            try:
                self.assertEqual(player._backend, "qt")
                self.assertFalse(player.backend_button.isVisible())
                player.switch_backend("vlc")
                player.switch_backend("mpv")
                self.assertEqual(player._backend, "qt")
                self.assertEqual(player.player, player._qt_player)
            finally:
                player.close()
                connection.close()

    def test_only_settings_backend_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-vlc-preference-") as directory:
            paths = AppPaths(Path(directory))
            save_settings(paths.settings, Settings(vlc_video_ids=("old-video",)))
            with patch("videomanager.ui.settings_dialog.find_vlc_directory", return_value=Path(directory)), \
                    patch("videomanager.ui.settings_dialog.find_mpv_directory", return_value=None):
                dialog = SettingsDialog(load_settings(paths.settings))
            try:
                dialog.playback_backend.setCurrentIndex(dialog.playback_backend.findData("vlc"))
                save_settings(paths.settings, dialog.updated(load_settings(paths.settings)))
                self.assertEqual(load_settings(paths.settings).playback_backend, "vlc")
                self.assertEqual(load_settings(paths.settings).vlc_video_ids, ())
            finally:
                dialog.close()

    def test_unavailable_default_falls_back_to_qt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-optional-vlc-") as directory:
            source = Path(directory) / "unavailable.mp4"
            connection = sqlite3.connect(":memory:")
            with patch("videomanager.ui.player_view.find_vlc_directory", return_value=None), \
                    patch("videomanager.ui.player_view.find_mpv_directory", return_value=None):
                player = PlayerView(connection, "test-video", source, default_backend="mpv")
            try:
                self.assertEqual(player._backend, "qt")
            finally:
                player.close()
                connection.close()

    def test_space_toggles_playback_from_video_without_stealing_editor_spaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vm-space-key-") as directory:
            connection = sqlite3.connect(":memory:")
            source = Path(directory) / "unavailable.mp4"
            with patch("videomanager.ui.player_view.find_vlc_directory", return_value=None), \
                    patch("videomanager.ui.player_view.find_mpv_directory", return_value=None):
                player = PlayerView(connection, "test-video", source)
            try:
                player.show()
                self.app.processEvents()
                player.video.setFocus()
                self.app.processEvents()
                with patch.object(player, "toggle_play") as toggle:
                    QTest.keyClick(player.video, Qt.Key_Space)
                    self.app.processEvents()
                    toggle.assert_called_once_with()

                    editor = QLineEdit(player)
                    editor.show()
                    editor.setFocus()
                    self.app.processEvents()
                    QTest.keyClicks(editor, "a b")
                    self.app.processEvents()
                    self.assertEqual(editor.text(), "a b")
                    toggle.assert_called_once_with()
            finally:
                player.close()
                connection.close()


if __name__ == "__main__":
    unittest.main()
