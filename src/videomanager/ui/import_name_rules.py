"""One-row-at-a-time editor for phrases removed from new import filenames."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)


class PhraseRow(QFrame):
    def __init__(self, editor: "ImportNameRulesEditor", phrase: str) -> None:
        super().__init__(editor)
        self.editor = editor
        self.phrase = phrase
        self._editing = False
        self.setObjectName("importPhraseRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 7, 5)
        layout.setSpacing(7)
        self.label = QLabel(phrase)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.label.setWordWrap(True)
        self.input = QLineEdit(phrase)
        self.input.hide()
        self.input.returnPressed.connect(self._edit_or_save)
        self.edit_button = QPushButton("修改")
        self.edit_button.clicked.connect(self._edit_or_save)
        self.delete_button = QPushButton("刪除")
        self.delete_button.clicked.connect(self._delete_or_cancel)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.input, 1)
        layout.addWidget(self.edit_button)
        layout.addWidget(self.delete_button)

    def _set_editing(self, editing: bool) -> None:
        self._editing = editing
        self.label.setVisible(not editing)
        self.input.setVisible(editing)
        self.edit_button.setText("確認修改" if editing else "修改")
        self.delete_button.setText("取消" if editing else "刪除")
        if editing:
            self.input.setText(self.phrase)
            self.input.setFocus()
            self.input.selectAll()

    def _edit_or_save(self) -> None:
        if not self._editing:
            self._set_editing(True)
            return
        value = self.editor.valid_phrase(self.input.text(), exclude=self)
        if value is None:
            return
        self.phrase = value
        self.label.setText(value)
        self._set_editing(False)

    def _delete_or_cancel(self) -> None:
        if self._editing:
            self._set_editing(False)
            return
        if QMessageBox.question(
            self, "確認刪除字句", f"刪除「{self.phrase}」這條清理規則？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) == QMessageBox.Yes:
            self.editor.remove_row(self)


class ImportNameRulesEditor(QWidget):
    def __init__(self, phrases: tuple[str, ...], parent=None) -> None:
        super().__init__(parent)
        self.rows: list[PhraseRow] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumHeight(145)
        self.scroll.setMaximumHeight(220)
        self.container = QWidget()
        self.row_layout = QVBoxLayout(self.container)
        self.row_layout.setContentsMargins(5, 5, 5, 5)
        self.row_layout.setSpacing(5)
        self.row_layout.addStretch()
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll)
        add_line = QHBoxLayout()
        self.new_phrase = QLineEdit()
        self.new_phrase.setPlaceholderText("輸入要從新匯入檔名移除的字句")
        self.new_phrase.returnPressed.connect(self.add_phrase)
        self.add_button = QPushButton("確認加入")
        self.add_button.clicked.connect(self.add_phrase)
        add_line.addWidget(self.new_phrase, 1)
        add_line.addWidget(self.add_button)
        layout.addLayout(add_line)
        hint = QLabel("只影響之後搬入的影片；搬入確認畫面會顯示清理後檔名。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #96adb6;")
        layout.addWidget(hint)
        self.setStyleSheet("""
            QFrame#importPhraseRow { background: #20313a; border: 1px solid #354b54;
                border-radius: 6px; }
            QFrame#importPhraseRow QPushButton { min-height: 23px; padding: 3px 8px; }
        """)
        for phrase in phrases:
            self._append_row(phrase)

    def _append_row(self, phrase: str) -> None:
        row = PhraseRow(self, phrase)
        self.rows.append(row)
        self.row_layout.insertWidget(self.row_layout.count() - 1, row)

    def valid_phrase(self, text: str, *, exclude: PhraseRow | None = None) -> str | None:
        value = text.strip()
        if not value:
            QMessageBox.information(self, "請輸入字句", "請輸入要從檔名移除的字句。")
            return None
        if "\n" in value or "\r" in value:
            QMessageBox.warning(self, "字句格式錯誤", "每條規則只能是一行字句。")
            return None
        if any(row is not exclude and row.phrase.casefold() == value.casefold() for row in self.rows):
            QMessageBox.information(self, "字句已存在", "這條清理規則已經加入。")
            return None
        return value

    def add_phrase(self) -> None:
        value = self.valid_phrase(self.new_phrase.text())
        if value is None:
            return
        self._append_row(value)
        self.new_phrase.clear()
        self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum())

    def remove_row(self, row: PhraseRow) -> None:
        self.rows.remove(row)
        self.row_layout.removeWidget(row)
        row.deleteLater()

    def phrases(self) -> tuple[str, ...]:
        return tuple(row.phrase for row in self.rows)


class ImportNameRulesDialog(QDialog):
    def __init__(self, phrases: tuple[str, ...], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("匯入檔名清理")
        self.resize(640, 430)
        self.scan_existing_requested = False
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("匯入檔名清理"))
        self.editor = ImportNameRulesEditor(phrases)
        layout.addWidget(self.editor, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("儲存設定")
        scan_button = buttons.addButton("儲存並掃描現有影片…", QDialogButtonBox.ActionRole)
        scan_button.clicked.connect(self._scan_existing)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def phrases(self) -> tuple[str, ...]:
        return self.editor.phrases()

    def _scan_existing(self) -> None:
        self.scan_existing_requested = True
        self.accept()
