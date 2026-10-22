"""Relation drag targets, using the same drag gesture as tag buttons."""
from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QIcon, QPalette
from PySide6.QtWidgets import QStyle, QStyleOptionButton, QStylePainter
from videomanager.ui.tag_picker import _DragButton

ENTITY_DRAG_MIME = "application/x-videomanager-entity-id"
RELATION_TYPE_DRAG_MIME = "application/x-videomanager-relation-type-id"


class RelationButton(_DragButton):
    entity_dropped = Signal(str, str, bool)
    type_dropped = Signal(str, str, bool)

    def __init__(self, label, identifier, parent=None, *, category=False):
        super().__init__(label, identifier, RELATION_TYPE_DRAG_MIME if category else ENTITY_DRAG_MIME, parent)
        self.category = category
        self.setAcceptDrops(True)
        self.setToolTip(label)

    def _label_rect(self, option):
        rect = self.style().subElementRect(QStyle.SE_PushButtonContents, option, self)
        if not self.icon().isNull():
            rect.setLeft(self._photo_rect(option).right() + 7)
        return rect

    def _photo_rect(self, option):
        content = self.style().subElementRect(QStyle.SE_PushButtonContents, option, self)
        size = self.iconSize().scaled(content.size(), Qt.KeepAspectRatio)
        if size.width() > self.iconSize().width():
            size = self.iconSize()
        return QRect(content.left(), content.top() + (content.height() - size.height()) // 2,
                     size.width(), size.height())

    def elided_text(self):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        return self.fontMetrics().elidedText(self.text(), Qt.ElideRight, max(0, self._label_rect(option).width()))

    def paintEvent(self, event):
        if self.category:
            super().paintEvent(event)
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        label_rect = self._label_rect(option)
        icon = self.icon()
        option.text = ""
        option.icon = QIcon()
        painter = QStylePainter(self)
        painter.drawControl(QStyle.CE_PushButton, option)
        painter.setClipRect(self.style().subElementRect(QStyle.SE_PushButtonContents, option, self))
        if not icon.isNull():
            image_rect = self._photo_rect(option)
            icon.paint(painter, image_rect, Qt.AlignCenter,
                       QIcon.Normal if self.isEnabled() else QIcon.Disabled)
        painter.setPen(option.palette.color(QPalette.Active if self.isEnabled() else QPalette.Disabled,
                                             QPalette.ButtonText))
        painter.drawText(label_rect, Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine, self.elided_text())

    def _accepts(self, mime):
        return mime.hasFormat(ENTITY_DRAG_MIME) or (self.category and mime.hasFormat(RELATION_TYPE_DRAG_MIME))

    def dragEnterEvent(self, event):
        if self._accepts(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        if not self._accepts(event.mimeData()):
            event.ignore()
            return
        is_type = self.category and event.mimeData().hasFormat(RELATION_TYPE_DRAG_MIME)
        mime_type = RELATION_TYPE_DRAG_MIME if is_type else ENTITY_DRAG_MIME
        try:
            source = bytes(event.mimeData().data(mime_type)).decode("utf-8")
        except UnicodeDecodeError:
            event.ignore()
            return
        after = (event.position().y() >= self.height() / 2 if self.category
                 else event.position().x() >= self.width() / 2)
        (self.type_dropped if is_type else self.entity_dropped).emit(source, self.identifier, after)
        event.acceptProposedAction()
