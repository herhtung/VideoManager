import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QStyleOptionButton, QStyle
from videomanager.ui.relation_button import RelationButton
from videomanager.ui.image_crop_dialog import photo_size


class RelationButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_narrow_photo_button_elides_name_without_changing_text_or_photo_size(self):
        name = "An exceptionally long relation name with a portrait"
        button = RelationButton(name, "entity")
        portrait = QPixmap(photo_size(320))
        portrait.fill()
        button.setIcon(QIcon(portrait))
        button.setIconSize(photo_size(96))
        button.ensurePolished()
        button.setFixedSize(170, 76)
        button.show()
        self.app.processEvents()
        try:
            self.assertTrue(button.elided_text().endswith("…"))
            self.assertNotIn("\n", button.elided_text())
            self.assertEqual(button.text(), name)
            self.assertEqual(button.toolTip(), name)
            self.assertEqual(button.iconSize(), photo_size(96))
            self.assertFalse(button.grab().isNull())
            button.setFixedWidth(button.fontMetrics().horizontalAdvance(name) + 160)
            self.assertEqual(button.elided_text(), name)
        finally:
            button.close()
            button.deleteLater()

    def test_short_name_is_unchanged(self):
        button = RelationButton("ABC", "entity")
        button.resize(180, 40)
        self.assertEqual(button.elided_text(), "ABC")
        button.deleteLater()

    def test_short_button_fits_entire_photo_instead_of_clipping_it(self):
        button = RelationButton("A long relation name", "entity")
        photo = QPixmap(photo_size(320))
        photo.fill()
        button.setIcon(QIcon(photo))
        button.setIconSize(photo_size(96))
        button.setStyleSheet("padding: 3px 7px; border: 1px solid black;")
        button.setFixedSize(180, 36)
        button.ensurePolished()
        option = QStyleOptionButton()
        button.initStyleOption(option)
        content = button.style().subElementRect(QStyle.SE_PushButtonContents, option, button)
        image = button._photo_rect(option)
        self.assertTrue(content.contains(image))
        self.assertAlmostEqual(image.width() / image.height(), 16 / 9, delta=0.05)
        self.assertGreater(button._label_rect(option).left(), image.right())
        self.assertFalse(button.grab().isNull())
        button.deleteLater()
