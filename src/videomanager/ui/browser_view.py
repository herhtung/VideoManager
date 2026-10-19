"""On-demand thumbnail grid/list with a bounded in-memory image cache."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QEvent, QModelIndex, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QListView, QStyle, QStyledItemDelegate

from videomanager.services.thumbnail_service import PREVIEW_COUNTS, cache_signature


class VideoModel(QAbstractListModel):
    def __init__(self, cache_root: Path, preview_mode: str = "standard", parent=None) -> None:
        super().__init__(parent)
        self.cache_root = cache_root
        self.frame_count = PREVIEW_COUNTS[preview_mode]
        self.rows: list[dict] = []
        self.hover_row = -1
        self.hover_frame = 0
        self.pixmaps: OrderedDict[str, QPixmap] = OrderedDict()

    def set_rows(self, rows) -> None:
        self.beginResetModel()
        self.rows = [dict(row) for row in rows]
        self.hover_row = -1
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row = self.rows[index.row()]
        is_group_cover = row.get("_group_cover", False)
        if role == Qt.UserRole:
            return row["id"]
        if role == Qt.ToolTipRole:
            if is_group_cover:
                compressed = row.get("compressed_count", 0)
                status = f"\n已壓縮 {compressed}/{row['group_size']} 段" if compressed else ""
                return (f"影片組：{row['group_name']}（{row['group_size']} 段）{status}"
                        "\n點「播放」接續播放；點「開啟分段」查看各段。")
            group = (f"\n影片組：{row['group_name']}（第 {row['group_position']}/{row['group_size']} 段）"
                     if row.get("group_id") else "")
            compressed = "\n已壓縮" if row.get("is_compressed") else ""
            return f"{row['display_title']}\n{row.get('relative_path', '')}{group}{compressed}"
        if role == Qt.DisplayRole:
            if is_group_cover:
                return f"{row['group_name']}\n{row['group_size']} 段"
            duration = row["duration_ms"]
            duration_text = "--:--" if duration is None else f"{duration // 60000:02d}:{(duration // 1000) % 60:02d}"
            size_text = f"{row['file_size'] / 1024**3:.2f} GB" if row["file_size"] >= 1024**3 else f"{row['file_size'] / 1024**2:.1f} MB"
            offline = " · 離線" if row["missing_since"] else ""
            part = f" · 第 {row['group_position']}/{row['group_size']} 段" if row.get("group_id") else ""
            return f"{row['display_title']}\n{duration_text} · {size_text}{part}{offline}"
        if role == Qt.DecorationRole:
            fresh = (
                row["thumbnail_state"] == "done" and
                row["thumbnail_signature"] == cache_signature(row["file_size"], row["mtime_ns"], self.frame_count)
            )
            # During a 10→30/60 rebuild the old cache remains useful as a
            # static cover. It is swapped only after all new frames succeed.
            frame = self.hover_frame if fresh and index.row() == self.hover_row else 0
            path = self.cache_root / row["id"] / f"{frame:02d}.webp"
            pixmap = self._pixmap(path)
            return pixmap
        return None

    def group_cover_pixmaps(self, row: dict) -> list[QPixmap | None]:
        """Show several segment covers without generating a separate group cache."""
        result: list[QPixmap | None] = []
        for member in row.get("_group_members", [])[:3]:
            frame = self.hover_frame if self.hover_row >= 0 and member["id"] == row["id"] else 0
            pixmap = self._pixmap(self.cache_root / member["id"] / f"{frame:02d}.webp")
            result.append(pixmap or self._pixmap(self.cache_root / member["id"] / "00.webp"))
        return result

    def _pixmap(self, path: Path) -> QPixmap | None:
        key = str(path)
        if key in self.pixmaps:
            self.pixmaps.move_to_end(key)
            return self.pixmaps[key]
        if not path.is_file():
            return None
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return None
        self.pixmaps[key] = pixmap
        if len(self.pixmaps) > 120:
            self.pixmaps.popitem(last=False)
        return pixmap

    def set_preview_mode(self, mode: str) -> None:
        self.frame_count = PREVIEW_COUNTS[mode]
        if self.rows:
            self.dataChanged.emit(self.index(0), self.index(len(self.rows) - 1), [Qt.DecorationRole])

    def refresh_preview_jobs(self, connection) -> None:
        jobs = {
            row["video_id"]: row for row in connection.execute(
                "SELECT video_id, state, cache_signature FROM thumbnail_jobs"
            )
        }
        for number, video in enumerate(self.rows):
            changed = False
            for member in video.get("_group_members", [video]):
                job = jobs.get(member["id"])
                if job is None or (member["thumbnail_state"], member["thumbnail_signature"]) == (job["state"], job["cache_signature"]):
                    continue
                if job["state"] == "done" and job["cache_signature"] != member["thumbnail_signature"]:
                    for key in list(self.pixmaps):
                        if Path(key).parent.name == member["id"]:
                            del self.pixmaps[key]
                member["thumbnail_state"] = job["state"]
                member["thumbnail_signature"] = job["cache_signature"]
                changed = True
            if changed:
                index = self.index(number)
                self.dataChanged.emit(index, index, [Qt.DecorationRole])

    def show_frame(self, row: int, frame: int) -> None:
        previous = self.hover_row
        self.hover_row = row
        self.hover_frame = frame
        for changed in {previous, row}:
            if 0 <= changed < len(self.rows):
                index = self.index(changed)
                self.dataChanged.emit(index, index, [Qt.DecorationRole])


class VideoDelegate(QStyledItemDelegate):
    """Paint a frame and its label into one stable item rectangle."""

    def __init__(self, view: "BrowserView") -> None:
        super().__init__(view)
        self.view = view

    def sizeHint(self, option, index) -> QSize:
        width = self.view._thumbnail_width
        height = round(width * 104 / 180)
        if self.view.viewMode() == QListView.IconMode:
            return QSize(width + 25, height + 62)
        return QSize(max(width + 230, self.view.viewport().width() - 22), height + 18)

    @staticmethod
    def group_action_rects(item_rect: QRect, title_y: int, list_mode: bool,
                           thumbnail_width: int) -> tuple[QRect, QRect]:
        if list_mode:
            x = item_rect.x() + thumbnail_width + 22
            return QRect(x, title_y + 26, 88, 28), QRect(x + 96, title_y + 26, 106, 28)
        x = item_rect.x() + 10
        width = max(70, (item_rect.width() - 26) // 2)
        return QRect(x, title_y + 21, width, 23), QRect(x + width + 6, title_y + 21, width, 23)

    @staticmethod
    def _draw_cover_image(painter: QPainter, pixmap: QPixmap | None, target: QRect) -> None:
        painter.fillRect(target, QColor("#15232d"))
        if not isinstance(pixmap, QPixmap) or pixmap.isNull():
            return
        source = pixmap.rect()
        target_ratio = target.width() / max(1, target.height())
        source_ratio = source.width() / max(1, source.height())
        if source_ratio > target_ratio:
            crop_width = round(source.height() * target_ratio)
            source = QRect(source.x() + (source.width() - crop_width) // 2, source.y(), crop_width, source.height())
        else:
            crop_height = round(source.width() / target_ratio)
            source = QRect(source.x(), source.y() + (source.height() - crop_height) // 2, source.width(), crop_height)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawPixmap(target, pixmap, source)

    @staticmethod
    def _draw_compressed_badge(painter: QPainter, text_x: int, title_y: int,
                               text_width: int, label: str) -> int:
        """Place a quiet badge on the right of the card title and return its text width."""
        badge_width = 92 if "/" in label else 62
        badge = QRect(text_x + text_width - badge_width, title_y + 1, badge_width, 19)
        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#286660"))
        painter.drawRoundedRect(badge, 5, 5)
        painter.setPen(QColor("#f0fffc"))
        painter.drawText(badge, Qt.AlignCenter, label)
        painter.restore()
        return max(0, text_width - badge_width - 6)

    def _paint_group_cover(self, painter: QPainter, option, index: QModelIndex,
                           image_rect: QRect, title_y: int, text_x: int, text_width: int) -> None:
        row = self.view.model().rows[index.row()]
        pixmaps = self.view.model().group_cover_pixmaps(row)
        count = len(pixmaps)
        if count == 2:
            split = image_rect.width() // 2
            cells = [QRect(image_rect.x(), image_rect.y(), split - 1, image_rect.height()),
                     QRect(image_rect.x() + split + 1, image_rect.y(), image_rect.width() - split - 1, image_rect.height())]
        elif count >= 3:
            split = round(image_rect.width() * 0.64)
            half_height = image_rect.height() // 2
            cells = [QRect(image_rect.x(), image_rect.y(), split - 1, image_rect.height()),
                     QRect(image_rect.x() + split + 1, image_rect.y(), image_rect.width() - split - 1, half_height - 1),
                     QRect(image_rect.x() + split + 1, image_rect.y() + half_height + 1,
                           image_rect.width() - split - 1, image_rect.height() - half_height - 1)]
        else:
            cells = [image_rect]
        for cell, pixmap in zip(cells, pixmaps):
            self._draw_cover_image(painter, pixmap, cell)
        badge = QRect(image_rect.right() - 82, image_rect.top() + 6, 76, 23)
        painter.fillRect(badge, QColor("#162c36"))
        painter.setPen(QColor("#d4f7f3"))
        painter.drawText(badge, Qt.AlignCenter, f"影片組 {row['group_size']} 段")
        compressed = row.get("compressed_count", 0)
        if compressed:
            label = "已壓縮" if compressed == row["group_size"] else f"已壓縮 {compressed}/{row['group_size']}"
            text_width = self._draw_compressed_badge(painter, text_x, title_y, text_width, label)
        title = option.fontMetrics.elidedText(row["group_name"], Qt.ElideRight, text_width)
        painter.setPen(option.palette.highlightedText().color() if option.state & QStyle.State_Selected else option.palette.text().color())
        painter.drawText(QRect(text_x, title_y, text_width, 20), Qt.AlignLeft | Qt.AlignVCenter, title)
        play_rect, open_rect = self.group_action_rects(option.rect, title_y,
                                                        self.view.viewMode() == QListView.ListMode,
                                                        self.view._thumbnail_width)
        for rect, label in ((play_rect, "▶ 播放"), (open_rect, "開啟分段")):
            painter.setPen(QColor("#76d7d0"))
            painter.setBrush(QColor("#203842"))
            painter.drawRoundedRect(rect, 5, 5)
            painter.drawText(rect, Qt.AlignCenter, label)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        painter.save()
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        background = option.palette.highlight().color() if selected else (
            option.palette.alternateBase().color() if hovered else option.palette.base().color()
        )
        painter.fillRect(option.rect, background)
        width = self.view._thumbnail_width
        height = round(width * 104 / 180)
        if self.view.viewMode() == QListView.IconMode:
            image_rect = QRect(option.rect.x() + (option.rect.width() - width) // 2,
                               option.rect.y() + 7, width, height)
            text_x = image_rect.x()
            text_width = width
            title_y = image_rect.bottom() + 7
        else:
            image_rect = QRect(option.rect.x() + 8,
                               option.rect.y() + (option.rect.height() - height) // 2,
                               width, height)
            text_x = image_rect.right() + 14
            text_width = max(0, option.rect.right() - text_x - 8)
            title_y = option.rect.y() + 15
        painter.fillRect(image_rect, option.palette.window().color())
        if self.view.model().rows[index.row()].get("_group_cover"):
            self._paint_group_cover(painter, option, index, image_rect, title_y, text_x, text_width)
            painter.restore()
            return
        pixmap = index.data(Qt.DecorationRole)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            scaled = pixmap.size().scaled(image_rect.size(), Qt.KeepAspectRatio)
            target = QRect(
                image_rect.x() + (image_rect.width() - scaled.width()) // 2,
                image_rect.y() + (image_rect.height() - scaled.height()) // 2,
                scaled.width(), scaled.height(),
            )
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.drawPixmap(target, pixmap, pixmap.rect())
        group_position = self.view.model().rows[index.row()].get("group_position")
        group_size = self.view.model().rows[index.row()].get("group_size")
        if group_position and group_size:
            badge = QRect(image_rect.right() - 49, image_rect.top() + 6, 44, 23)
            painter.setPen(Qt.NoPen)
            painter.setBrush(option.palette.highlight())
            painter.drawRoundedRect(badge, 6, 6)
            painter.setPen(option.palette.highlightedText().color())
            painter.drawText(badge, Qt.AlignCenter, f"{group_position}/{group_size}")
        title, _, details = (index.data(Qt.DisplayRole) or "").partition("\n")
        if self.view.model().rows[index.row()].get("is_compressed"):
            text_width = self._draw_compressed_badge(painter, text_x, title_y, text_width, "已壓縮")
        painter.setPen(option.palette.highlightedText().color() if selected else option.palette.text().color())
        painter.drawText(QRect(text_x, title_y, text_width, 22), Qt.AlignLeft | Qt.AlignVCenter,
                         option.fontMetrics.elidedText(title, Qt.ElideRight, text_width))
        painter.setPen(option.palette.highlightedText().color() if selected else option.palette.mid().color())
        painter.drawText(QRect(text_x, title_y + 21, text_width, 20), Qt.AlignLeft | Qt.AlignVCenter,
                         option.fontMetrics.elidedText(details, Qt.ElideRight, text_width))
        painter.restore()


class BrowserView(QListView):
    activated_video = Signal(str)
    group_play_requested = Signal(str)
    group_open_requested = Signal(str)
    files_dropped = Signal(object)
    context_menu_requested = Signal(object)
    blank_clicked = Signal()
    related_hover_requested = Signal(str, object)
    related_hover_ended = Signal()

    def __init__(self, model: VideoModel, hover_delay_ms: int = 400, frame_interval_ms: int = 500,
                 related_hover_delay_ms: int = 2000, parent=None) -> None:
        super().__init__(parent)
        self.setModel(model)
        self._thumbnail_width = 180
        self.setItemDelegate(VideoDelegate(self))
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setSelectionMode(QListView.ExtendedSelection)
        self.setViewMode(QListView.IconMode)
        self.setResizeMode(QListView.Fixed)
        self.setMovement(QListView.Free)
        self.setDragEnabled(False)
        self.setUniformItemSizes(True)
        self.setSpacing(10)
        self.setIconSize(QSize(180, 104))
        self.setGridSize(QSize(205, 166))
        self._folder_title: str | None = None
        self._headers: list[tuple[str, int]] = []
        model.modelReset.connect(self._schedule_reflow)
        self._hover_candidate = -1
        self._frame = 0
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(hover_delay_ms)
        self._delay.timeout.connect(self._start_hover)
        self._related_delay = QTimer(self)
        self._related_delay.setSingleShot(True)
        self._related_delay.setInterval(related_hover_delay_ms)
        self._related_delay.timeout.connect(self._show_related_hover)
        self._related_visible = False
        self._cycle = QTimer(self)
        self._cycle.setInterval(frame_interval_ms)
        self._cycle.timeout.connect(self._next_frame)
        self.entered.connect(self._on_entered)
        self.doubleClicked.connect(self._activate_index)

    def _activate_index(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        row = self.model().rows[index.row()]
        if row.get("_group_cover"):
            self.group_play_requested.emit(row["group_id"])
        else:
            self.activated_video.emit(row["id"])

    def set_folder_title(self, title: str | None) -> None:
        self._folder_title = title
        self._schedule_reflow()

    def _schedule_reflow(self) -> None:
        if self.viewMode() == QListView.IconMode:
            QTimer.singleShot(0, self._reflow_cards)

    def _reflow_cards(self) -> None:
        if self.viewMode() != QListView.IconMode or not self.model():
            return
        self.doItemsLayout()
        rows = self.model().rows
        self._headers = []
        if not rows:
            self.viewport().update()
            return
        cell = self._grid_size()
        gap = 10
        columns = max(1, (max(1, self.viewport().width()) - 16 + gap) // (cell.width() + gap))
        sections: list[tuple[str, list[int]]] = []
        if self._folder_title:
            sections.append((self._folder_title, list(range(len(rows)))))
        else:
            singles = [i for i, row in enumerate(rows) if not row.get("_group_cover")]
            groups = [i for i, row in enumerate(rows) if row.get("_group_cover")]
            if singles:
                sections.append(("單部影片", singles))
            if groups:
                sections.append(("影片組 · 點開查看分段", groups))
        y = 8
        for title, indices in sections:
            self._headers.append((title, y))
            y += 34
            for position, number in enumerate(indices):
                column = position % columns
                row_number = position // columns
                self.setPositionForIndex(QPoint(8 + column * (cell.width() + gap),
                                                y + row_number * (cell.height() + gap)), self.model().index(number))
            y += ((len(indices) + columns - 1) // columns) * (cell.height() + gap) + 10
        self.viewport().update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.viewMode() != QListView.IconMode:
            return
        painter = QPainter(self.viewport())
        scroll = self.verticalScrollBar().value()
        painter.setPen(QColor("#8fc7c4"))
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        for title, content_y in self._headers:
            y = content_y - scroll
            if -30 <= y <= self.viewport().height():
                painter.drawText(QRect(10, y, self.viewport().width() - 20, 26),
                                 Qt.AlignLeft | Qt.AlignVCenter, title)
                painter.fillRect(QRect(10, y + 28, max(0, self.viewport().width() - 20), 1), QColor("#314853"))
        painter.end()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            position = event.position().toPoint()
            index = self.indexAt(position)
            if index.isValid():
                row = self.model().rows[index.row()]
                if row.get("_group_cover"):
                    rect = self.visualRect(index)
                    if self.viewMode() == QListView.IconMode:
                        title_y = rect.y() + 7 + round(self._thumbnail_width * 104 / 180) + 6
                    else:
                        title_y = rect.y() + 15
                    play_rect, open_rect = self.itemDelegate().group_action_rects(
                        rect, title_y, self.viewMode() == QListView.ListMode, self._thumbnail_width,
                    )
                    if play_rect.contains(position):
                        self.group_play_requested.emit(row["group_id"])
                        event.accept()
                        return
                    if open_rect.contains(position):
                        self.group_open_requested.emit(row["group_id"])
                        event.accept()
                        return
        super().mouseReleaseEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and not self.indexAt(event.position().toPoint()).isValid():
            self.clearSelection()
            self.selectionModel().clearCurrentIndex()
            self.blank_clicked.emit()
        super().mousePressEvent(event)

    def viewportEvent(self, event) -> bool:
        if event.type() == QEvent.ToolTip:
            index = self.indexAt(event.pos())
            if index.isValid():
                event.accept()
                return True
        if event.type() == QEvent.ContextMenu:
            self.context_menu_requested.emit(event.pos())
            event.accept()
            return True
        if event.type() == QEvent.Leave:
            self.stop_hover()
        elif event.type() == QEvent.MouseMove:
            index = self.indexAt(event.position().toPoint())
            if not index.isValid():
                if self._hover_candidate >= 0:
                    self.stop_hover()
            elif index.row() != self._hover_candidate:
                self._on_entered(index)
        return super().viewportEvent(event)

    @staticmethod
    def _local_drop_paths(event) -> list[Path]:
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        return [Path(url.toLocalFile()) for url in urls] if urls and all(url.isLocalFile() for url in urls) else []

    def dragEnterEvent(self, event) -> None:
        if self._local_drop_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._local_drop_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        paths = self._local_drop_paths(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.files_dropped.emit(paths)

    def _on_entered(self, index: QModelIndex) -> None:
        if index.row() == self._hover_candidate:
            return
        self.stop_hover()
        self._hover_candidate = index.row()
        self._delay.start()
        self._related_delay.start()

    def _show_related_hover(self) -> None:
        if self._hover_candidate < 0 or self._hover_candidate >= len(self.model().rows):
            return
        row = self.model().rows[self._hover_candidate]
        rect = self.visualRect(self.model().index(self._hover_candidate))
        self._related_visible = True
        self.related_hover_requested.emit(row["id"], self.viewport().mapToGlobal(rect.bottomLeft()))

    def _start_hover(self) -> None:
        if self._hover_candidate < 0:
            return
        self._frame = 0
        self.model().show_frame(self._hover_candidate, self._frame)
        self._cycle.start()

    def _next_frame(self) -> None:
        if self._hover_candidate >= 0:
            self._frame = (self._frame + 1) % self.model().frame_count
            self.model().show_frame(self._hover_candidate, self._frame)

    def stop_hover(self) -> None:
        self._delay.stop()
        self._related_delay.stop()
        self._cycle.stop()
        self._hover_candidate = -1
        self.model().show_frame(-1, 0)
        if self._related_visible:
            self._related_visible = False
            self.related_hover_ended.emit()

    def set_related_delay(self, milliseconds: int) -> None:
        self._related_delay.setInterval(milliseconds)

    def leaveEvent(self, event) -> None:
        self.stop_hover()
        super().leaveEvent(event)

    def set_list_mode(self, enabled: bool) -> None:
        self.stop_hover()
        self.setViewMode(QListView.ListMode if enabled else QListView.IconMode)
        self.setFlow(QListView.TopToBottom if enabled else QListView.LeftToRight)
        self.setWrapping(not enabled)
        self.setMovement(QListView.Static if enabled else QListView.Free)
        self.setResizeMode(QListView.Adjust if enabled else QListView.Fixed)
        self.setUniformItemSizes(not enabled)
        self.setSpacing(5 if enabled else 10)
        self.setGridSize(QSize() if enabled else self._grid_size())
        self.doItemsLayout()
        self._schedule_reflow()
        self.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.viewMode() == QListView.ListMode:
            QTimer.singleShot(0, self.doItemsLayout)
        else:
            self._schedule_reflow()

    def set_timing(self, hover_delay_ms: int, frame_interval_ms: int) -> None:
        self._delay.setInterval(hover_delay_ms)
        self._cycle.setInterval(frame_interval_ms)

    def set_thumbnail_width(self, width: int) -> None:
        self._thumbnail_width = width
        self.setIconSize(QSize(width, round(width * 104 / 180)))
        if self.viewMode() == QListView.IconMode:
            self.setGridSize(self._grid_size())
            self._schedule_reflow()

    def _grid_size(self) -> QSize:
        return QSize(self._thumbnail_width + 25, round(self._thumbnail_width * 104 / 180) + 62)
