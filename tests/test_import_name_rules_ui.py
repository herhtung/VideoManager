"""Filename cleanup rules can be added, changed, and removed individually."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from videomanager.config import Settings
from videomanager.ui.import_name_rules import ImportNameRulesDialog, ImportNameRulesEditor
from videomanager.ui.settings_dialog import SettingsDialog


class ImportNameRulesUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_each_phrase_can_be_added_edited_and_deleted(self) -> None:
        editor = ImportNameRulesEditor(("舊字句",))
        try:
            editor.new_phrase.setText("新字句")
            editor.add_button.click()
            self.assertEqual(editor.phrases(), ("舊字句", "新字句"))
            row = editor.rows[0]
            row.edit_button.click()
            row.input.setText("已修改")
            row.edit_button.click()
            self.assertEqual(editor.phrases(), ("已修改", "新字句"))
            with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
                row.delete_button.click()
            self.assertEqual(len(editor.rows), 2)
            with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
                row.delete_button.click()
            self.assertEqual(editor.phrases(), ("新字句",))
        finally:
            editor.close()

    def test_settings_save_current_rules(self) -> None:
        settings = Settings(import_remove_phrases=("移除我",))
        dialog = SettingsDialog(settings)
        try:
            dialog.remove_phrases.new_phrase.setText("也移除我")
            dialog.remove_phrases.add_button.click()
            self.assertEqual(dialog.updated(settings).import_remove_phrases, ("移除我", "也移除我"))
            dialog._scan_existing()
            self.assertTrue(dialog.scan_existing_requested)
        finally:
            dialog.close()

    def test_rules_dialog_can_request_scan_after_saving(self) -> None:
        dialog = ImportNameRulesDialog(("移除我",))
        try:
            dialog._scan_existing()
            self.assertTrue(dialog.scan_existing_requested)
            self.assertEqual(dialog.phrases(), ("移除我",))
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
