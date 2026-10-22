import os
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QLineEdit, QPlainTextEdit,
    QPushButton, QTextEdit, QVBoxLayout, QWidget,
)
from videomanager.ui.player_view import PlayerView
from videomanager.ui.text_input import install_text_input_support, text_input_widget


class TextInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widgets = []
        self.support = install_text_input_support(self.app)

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.support.timer.stop()
        self.app.removeEventFilter(self.support)
        self.app.focusChanged.disconnect(self.support.schedule)
        self.app.applicationStateChanged.disconnect(self.support.schedule)
        del self.app._text_input_support
        self.support.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def window(self):
        window = QWidget()
        self.widgets.append(window)
        layout = QVBoxLayout(window)
        editors = [QLineEdit(), QTextEdit(), QPlainTextEdit(), QDoubleSpinBox(), QComboBox()]
        editors[-1].setEditable(True)
        for editor in editors:
            layout.addWidget(editor)
        button = QPushButton("Other focus")
        layout.addWidget(button)
        window.show()
        window.activateWindow()
        self.app.processEvents()
        return window, editors, button

    def test_support_is_singleton_and_non_widget_events_are_safe(self):
        self.assertIs(install_text_input_support(self.app), self.support)
        self.assertIsNone(text_input_widget(QObject()))
        self.assertIsNone(text_input_widget(QPushButton()))
        self.assertFalse(self.support.eventFilter(QObject(), QEvent(QEvent.MouseButtonPress)))

    def test_sync_after_focus_reactivation_and_click_preserves_composition(self):
        window, editors, button = self.window()
        input_method = Mock()
        with patch.object(QApplication, "applicationState", return_value=Qt.ApplicationActive), \
                patch.object(QApplication, "inputMethod", return_value=input_method):
            for editor in editors[:3]:
                with self.subTest(editor=type(editor).__name__):
                    editor.setFocus()
                    self.app.processEvents()
                    self.assertIs(text_input_widget(self.app.focusWidget()), editor)
                    input_method.reset_mock()
                    preedit = QInputMethodEvent("ㄓㄨㄥ", [])
                    QApplication.sendEvent(editor, preedit)
                    self.assertEqual(editor.text() if isinstance(editor, QLineEdit) else editor.toPlainText(), "")
                    self.support.eventFilter(window, QEvent(QEvent.WindowActivate))
                    self.app.processEvents()
                    input_method.update.assert_called_once_with(Qt.ImEnabled | Qt.ImQueryInput)
                    input_method.reset.assert_not_called()
                    input_method.commit.assert_not_called()
                    input_method.show.assert_not_called()
                    commit = QInputMethodEvent()
                    commit.setCommitString("中文")
                    QApplication.sendEvent(editor, commit)
                    self.assertEqual(editor.text() if isinstance(editor, QLineEdit) else editor.toPlainText(), "中文")
                    input_method.reset_mock()
                    QTest.mouseClick(editor.viewport() if isinstance(editor, (QTextEdit, QPlainTextEdit)) else editor,
                                     Qt.LeftButton)
                    self.app.processEvents()
                    input_method.update.assert_called()
            button.setFocus()
            self.app.processEvents()
            input_method.reset_mock()
            self.support.refresh()
            input_method.update.assert_not_called()

    def test_inactive_or_readonly_input_does_not_reenable_ime(self):
        window, editors, _button = self.window()
        editor = editors[0]
        editor.setFocus()
        self.app.processEvents()
        input_method = Mock()
        with patch.object(QApplication, "inputMethod", return_value=input_method):
            with patch.object(QApplication, "applicationState", return_value=Qt.ApplicationInactive):
                self.support.refresh()
            editor.setReadOnly(True)
            with patch.object(QApplication, "applicationState", return_value=Qt.ApplicationActive):
                self.support.refresh()
        input_method.update.assert_not_called()

    def test_player_shortcuts_leave_editor_keys_and_composition_alone(self):
        connection = sqlite3.connect(":memory:")
        with patch("videomanager.ui.player_view.find_vlc_directory", return_value=None), \
                patch("videomanager.ui.player_view.find_mpv_directory", return_value=None), \
                patch.object(PlayerView, "_start_source"):
            player = PlayerView(connection, "test", Path("absent.mp4"), embedded=True)
        self.widgets.append(player)
        layout = player.layout()
        editors = [QLineEdit(), QPlainTextEdit(), QDoubleSpinBox(), QComboBox()]
        editors[-1].setEditable(True)
        for editor in editors:
            layout.addWidget(editor)
        player.show()
        player.activateWindow()
        self.app.processEvents()
        try:
            with patch.object(player.player, "setPosition") as seek, \
                    patch.object(player, "toggle_play") as play, \
                    patch.object(player, "toggle_fullscreen") as fullscreen, \
                    patch.object(player, "exit_fullscreen") as exit_fullscreen:
                for editor in editors:
                    editor.setFocus()
                    self.app.processEvents()
                    self.assertTrue(all(not shortcut.isEnabled() for shortcut in player._shortcuts))
                    QTest.keyClick(self.app.focusWidget(), Qt.Key_Right)
                    QTest.keyClick(self.app.focusWidget(), Qt.Key_Escape)
                    player._playback_key(Qt.Key_Left)
                    player._toggle_playback_shortcut()
                    player._toggle_fullscreen_shortcut()
                seek.assert_not_called()
                play.assert_not_called()
                fullscreen.assert_not_called()
                exit_fullscreen.assert_not_called()
                line = editors[0]
                line.setFocus()
                self.app.processEvents()
                event = QInputMethodEvent("ㄓㄨㄥ", [])
                QApplication.sendEvent(line, event)
                commit = QInputMethodEvent()
                commit.setCommitString("中文")
                QApplication.sendEvent(line, commit)
                QTest.keyClicks(line, " f")
                self.assertEqual(line.text(), "中文 f")
                player.video.setFocus()
                self.app.processEvents()
                self.assertTrue(all(shortcut.isEnabled() for shortcut in player._shortcuts))
                QTest.keyClick(player.video, Qt.Key_Space)
                QTest.keyClick(player.video, Qt.Key_F)
                QTest.keyClick(player.video, Qt.Key_Right)
                play.assert_called_once()
                fullscreen.assert_called_once()
                seek.assert_called_once()
                self.assertEqual(len(player._shortcuts), 7)
        finally:
            player.close()
            self.widgets.remove(player)
            connection.close()
