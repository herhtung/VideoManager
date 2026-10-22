"""Move and zoom a relation photo before saving its landscape crop."""
from PySide6.QtCore import QPointF, QRectF, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFileDialog,
                               QHBoxLayout, QLabel, QMessageBox, QPushButton, QSlider, QVBoxLayout, QWidget)


def photo_size(width):
    """Use the same 16:9 frame for cropping and every relation photo."""
    return QSize(width, round(width * 9 / 16))


def read_photo(path):
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and (size.width() > 2048 or size.height() > 2048):
        reader.setScaledSize(size.scaled(QSize(2048, 2048), Qt.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        raise ValueError(reader.errorString() or "無法讀取圖片")
    return image


def clipboard_photo():
    clipboard = QApplication.clipboard()
    image = clipboard.image()
    if not image.isNull():
        return (image.scaled(2048, 2048, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                if image.width() > 2048 or image.height() > 2048 else image)
    mime = clipboard.mimeData()
    for url in mime.urls() if mime is not None else ():
        if url.isLocalFile():
            try:
                return read_photo(url.toLocalFile())
            except ValueError:
                continue
    raise ValueError("剪貼簿沒有可用圖片。請先複製圖片或截圖。")


class CropCanvas(QWidget):
    zoomChanged = Signal(int)

    def __init__(self, image, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 260)
        self.setCursor(Qt.OpenHandCursor)
        self._drag_position = None
        self.set_image(image)

    def set_image(self, image):
        self.image = image.copy()
        self.zoom = 100
        self.center = QPointF(image.width() / 2, image.height() / 2)
        self.zoomChanged.emit(100)
        self.update()

    def frame(self):
        width = max(1.0, min(self.width() - 32.0, (self.height() - 32.0) * 16 / 9))
        height = width * 9 / 16
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def scale(self):
        return max(self.frame().width() / self.image.width(), self.frame().height() / self.image.height()) * self.zoom / 100

    def source_rect(self):
        half_width = self.frame().width() / self.scale() / 2
        half_height = self.frame().height() / self.scale() / 2
        x = max(half_width, min(self.image.width() - half_width, self.center.x()))
        y = max(half_height, min(self.image.height() - half_height, self.center.y()))
        self.center = QPointF(x, y)
        return QRectF(x - half_width, y - half_height, half_width * 2, half_height * 2)

    def set_zoom(self, value):
        self.zoom = max(100, min(400, int(value)))
        self.source_rect()
        self.zoomChanged.emit(self.zoom)
        self.update()

    def cropped_image(self, size=320):
        output = QImage(photo_size(size), QImage.Format_ARGB32_Premultiplied)
        output.fill(Qt.transparent)
        painter = QPainter(output)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawImage(QRectF(output.rect()), self.image, self.source_rect())
        painter.end()
        return output

    def paintEvent(self, event):
        frame, source = self.frame(), self.source_rect()
        scale = self.scale()
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#081118"))
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        target = QRectF(frame.left() - source.left() * scale, frame.top() - source.top() * scale,
                        self.image.width() * scale, self.image.height() * scale)
        painter.drawImage(target, self.image)
        mask = QPainterPath()
        mask.addRect(QRectF(self.rect()))
        mask.addRect(frame)
        painter.fillPath(mask, QColor(0, 0, 0, 155))
        painter.setPen(QPen(QColor("#a7eee2"), 2))
        painter.drawRect(frame)
        painter.setPen(QPen(QColor(255, 255, 255, 85), 1))
        for part in (1, 2):
            x, y = frame.left() + frame.width() * part / 3, frame.top() + frame.height() * part / 3
            painter.drawLine(QPointF(x, frame.top()), QPointF(x, frame.bottom()))
            painter.drawLine(QPointF(frame.left(), y), QPointF(frame.right(), y))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_position = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_position is not None:
            delta = event.position() - self._drag_position
            self.center -= delta / self.scale()
            self._drag_position = event.position()
            self.source_rect()
            self.update()
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_position = None
        self.setCursor(Qt.OpenHandCursor)

    def wheelEvent(self, event):
        self.set_zoom(self.zoom + (10 if event.angleDelta().y() > 0 else -10))
        event.accept()


class ImageCropDialog(QDialog):
    def __init__(self, image, parent=None):
        super().__init__(parent)
        self.setWindowTitle("調整關聯照片")
        self.resize(640, 510)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("拖曳調整位置，滾輪或滑桿縮放；16:9 框內內容與外側按鈕照片相同。"))
        tools = QHBoxLayout()
        for title, callback in (("選擇圖片…", self.choose_file), ("貼上圖片（Ctrl+V）", self.paste_image),
                                ("重設位置", self.reset_crop)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            tools.addWidget(button)
        layout.addLayout(tools)
        self.canvas = CropCanvas(image)
        layout.addWidget(self.canvas, 1)
        self.zoom = QSlider(Qt.Horizontal)
        self.zoom.setRange(100, 400)
        self.zoom.setValue(100)
        self.zoom.valueChanged.connect(self.canvas.set_zoom)
        self.canvas.zoomChanged.connect(self._zoom_changed)
        self.zoom_label = QLabel("100%")
        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel("縮放"))
        zoom_row.addWidget(self.zoom, 1)
        zoom_row.addWidget(self.zoom_label)
        layout.addLayout(zoom_row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("儲存照片")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.paste_shortcut = QShortcut(QKeySequence.Paste, self)
        self.paste_shortcut.activated.connect(self.paste_image)

    def _zoom_changed(self, value):
        with QSignalBlocker(self.zoom):
            self.zoom.setValue(value)
        self.zoom_label.setText(f"{value}%")

    def reset_crop(self):
        self.canvas.set_image(self.canvas.image)

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "選擇關聯照片", "", "圖片 (*.png *.jpg *.jpeg *.webp *.bmp)")
        if path:
            try:
                self.canvas.set_image(read_photo(path))
            except ValueError as error:
                QMessageBox.warning(self, "圖片讀取失敗", str(error))

    def paste_image(self):
        try:
            self.canvas.set_image(clipboard_photo())
        except ValueError as error:
            QMessageBox.information(self, "無法貼上照片", str(error))
