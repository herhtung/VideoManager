"""Shared text-focus detection and event-driven input-method synchronization."""
from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QLineEdit, QPlainTextEdit, QTextEdit, QWidget,
)


def text_input_widget(widget):
    while isinstance(widget, QWidget):
        if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox)):
            return widget
        widget = widget.parentWidget()
    return None


def text_input_has_focus():
    return text_input_widget(QApplication.focusWidget()) is not None


class InputMethodSupport(QObject):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(0)
        self.timer.timeout.connect(self.refresh)
        app.focusChanged.connect(self.schedule)
        app.applicationStateChanged.connect(self.schedule)
        app.installEventFilter(self)

    def schedule(self, *_args):
        self.timer.start()

    def refresh(self):
        # Defer until native video/tool-window activation has settled. Updating
        # ImEnabled reattaches Qt's Windows IME context without changing language
        # or resetting an in-progress composition.
        widget = self.app.focusWidget()
        if (self.app.applicationState() == Qt.ApplicationActive
                and text_input_widget(widget) is not None
                and widget.isEnabled() and widget.inputMethodQuery(Qt.ImEnabled)):
            self.app.inputMethod().update(Qt.ImEnabled | Qt.ImQueryInput)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.WindowActivate:
            self.schedule()
        elif event.type() == QEvent.MouseButtonPress and text_input_widget(watched) is not None:
            self.schedule()
        return False


def install_text_input_support(app):
    support = getattr(app, "_text_input_support", None)
    if support is None:
        support = app._text_input_support = InputMethodSupport(app)
    return support
