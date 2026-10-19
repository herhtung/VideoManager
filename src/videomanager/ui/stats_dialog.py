"""Read-only library statistics from the existing local index."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
    QPushButton, QStyledItemDelegate, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)


def library_stats(connection: sqlite3.Connection, library_id: str) -> dict:
    overview = connection.execute(
        """SELECT COUNT(*) AS video_count,
                  COALESCE(SUM(file_size), 0) AS total_bytes,
                  COALESCE(SUM(duration_ms), 0) AS duration_ms,
                  COALESCE(SUM(is_favorite), 0) AS favorites,
                  COALESCE(SUM(CASE WHEN status IN ('unprocessed', 'in_progress') THEN 1 ELSE 0 END), 0) AS unprocessed,
                  COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0) AS done
           FROM videos WHERE library_id = ? AND deleted_at IS NULL AND missing_since IS NULL""",
        (library_id,),
    ).fetchone()
    missing = connection.execute(
        """SELECT COUNT(*) FROM videos WHERE library_id=? AND deleted_at IS NULL
           AND missing_since IS NOT NULL""", (library_id,),
    ).fetchone()[0]
    groups = connection.execute(
        """SELECT COUNT(DISTINCT g.id) FROM video_groups g
           JOIN video_group_members m ON m.group_id = g.id
           JOIN videos v ON v.id = m.video_id
           WHERE g.library_id = ? AND v.deleted_at IS NULL AND v.missing_since IS NULL""",
        (library_id,),
    ).fetchone()[0]
    tags = connection.execute(
        """SELECT tg.id AS category_id, tg.name AS category_name, t.id AS tag_id,
                  t.name AS tag_name, COUNT(DISTINCT v.id) AS video_count
           FROM tag_groups tg JOIN tags t ON t.group_id = tg.id
           LEFT JOIN video_tags vt ON vt.tag_id = t.id
           LEFT JOIN videos v ON v.id = vt.video_id AND v.library_id = ?
                             AND v.deleted_at IS NULL AND v.missing_since IS NULL
           GROUP BY tg.id, t.id
           ORDER BY tg.sort_order, tg.name, t.sort_order, t.name""",
        (library_id,),
    ).fetchall()
    summary = dict(overview)
    summary["missing"] = missing
    return {"overview": summary, "groups": groups, "tags": [dict(row) for row in tags]}


def readable_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if unit == "B" and value < 1024:
            return f"{int(value)} B"
        if unit != "B" and value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def readable_duration(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds // 1000, 3600)
    minutes = remainder // 60
    return f"{hours:,} 小時 {minutes:02d} 分"


class TagRatioDelegate(QStyledItemDelegate):
    """A quiet horizontal line for each tag's share of the whole library."""

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        count, total = index.data(Qt.UserRole) or (0, 0)
        width = max(0, option.rect.width() - 18)
        x = option.rect.x() + 9
        y = option.rect.center().y() - 3
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#2b414a"))
        painter.drawRoundedRect(x, y, width, 6, 3, 3)
        if count and total:
            painter.setBrush(QColor("#62cfc3"))
            painter.drawRoundedRect(x, y, max(3, round(width * count / total)), 6, 3, 3)
        painter.restore()


class StatsDialog(QDialog):
    def __init__(self, connection: sqlite3.Connection, library_id: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("影片庫統計")
        self.setObjectName("statsDialog")
        self.resize(810, 700)
        self.setStyleSheet("""
            QDialog#statsDialog { background: #101820; color: #eaf0f2; }
            QFrame#statsHero { background: #14252e; border: 1px solid #36545a; border-radius: 12px; }
            QFrame#statsMetric { background: #1a2932; border: 1px solid #2d424b; border-radius: 10px; }
            QLabel#statsTitle { color: #f3fbf9; font-size: 21px; font-weight: 700; }
            QLabel#statsSection { color: #d7ebe9; font-size: 13px; font-weight: 700; }
            QLabel#statsCaption { color: #94aeb8; font-size: 11px; }
            QLabel#statsNumber { color: #f1faf8; font-size: 20px; font-weight: 700; }
            QLabel#statsMuted { color: #96adb6; }
            QTreeWidget#statsTags { background: #16232b; border: 1px solid #30454e;
                border-radius: 8px; outline: none; alternate-background-color: #1b2b34; }
            QTreeWidget#statsTags::item { min-height: 27px; padding: 2px 6px; }
            QTreeWidget#statsTags::item:selected { background: #254b4d; }
            QHeaderView::section { background: #20313a; color: #a9c0c7;
                border: 0; border-bottom: 1px solid #3a5058; padding: 7px; }
            QProgressBar#statsProgress { height: 9px; border: 0; border-radius: 4px;
                background: #2b414a; text-align: center; }
            QProgressBar#statsProgress::chunk { background: #62cfc3; border-radius: 4px; }
        """)
        data = library_stats(connection, library_id)
        overview = data["overview"]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(13)
        hero = QFrame()
        hero.setObjectName("statsHero")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(18, 14, 18, 14)
        hero_layout.setSpacing(4)
        title = QLabel("影片庫統計")
        title.setObjectName("statsTitle")
        description = QLabel("目前影片庫的數量、容量與整理進度")
        description.setObjectName("statsMuted")
        hero_layout.addWidget(title)
        hero_layout.addWidget(description)
        layout.addWidget(hero)
        cards = QGridLayout()
        cards.setSpacing(8)
        metrics = (
            ("影片總數", f"{overview['video_count']:,} 部"),
            ("影片總容量", readable_size(overview["total_bytes"])),
            ("總片長", readable_duration(overview["duration_ms"])),
            ("影片組", f"{data['groups']:,} 組"),
            ("收藏", f"{overview['favorites']:,} 部"),
            ("索引中找不到檔案", f"{overview['missing']:,} 部"),
        )
        for index, (label, value) in enumerate(metrics):
            card = QFrame()
            card.setObjectName("statsMetric")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(15, 10, 15, 11)
            card_layout.setSpacing(4)
            caption = QLabel(label)
            caption.setObjectName("statsCaption")
            if label == "索引中找不到檔案":
                caption.setToolTip("資料庫保留記錄，但掃描時原路徑沒有找到 MP4。此數量不計入可用影片總數。")
            number = QLabel(value)
            number.setObjectName("statsNumber")
            card_layout.addWidget(caption)
            card_layout.addWidget(number)
            cards.addWidget(card, index // 3, index % 3)
        layout.addLayout(cards)
        progress_heading = QHBoxLayout()
        progress_heading.addWidget(self._section_label("整理進度"))
        progress_heading.addStretch()
        progress_heading.addWidget(QLabel(
            f"已整理 {overview['done']:,} / {overview['video_count']:,} 部"
        ))
        layout.addLayout(progress_heading)
        progress = QProgressBar()
        progress.setObjectName("statsProgress")
        progress.setTextVisible(False)
        progress.setRange(0, max(1, overview["video_count"]))
        progress.setValue(overview["done"])
        layout.addWidget(progress)
        states = QLabel(
            f"待整理 {overview['unprocessed']:,}　　"
            f"已整理 {overview['done']:,}"
        )
        states.setObjectName("statsMuted")
        layout.addWidget(states)
        search_row = QHBoxLayout()
        search_row.addWidget(self._section_label("各標籤影片數"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜尋標籤或標籤分類")
        self.search.setMaximumWidth(260)
        search_row.addWidget(self.search, 1)
        layout.addLayout(search_row)
        self.tags = QTreeWidget()
        self.tags.setObjectName("statsTags")
        self.tags.setHeaderLabels(["標籤分類／標籤", "佔全部影片", "影片數"])
        self.tags.setColumnWidth(0, 330)
        self.tags.setColumnWidth(1, 185)
        self.tags.setItemDelegateForColumn(1, TagRatioDelegate(self.tags))
        self.tags.setAlternatingRowColors(True)
        self.tags.setRootIsDecorated(True)
        self.tags.setUniformRowHeights(True)
        parents: dict[str, QTreeWidgetItem] = {}
        total = overview["video_count"]
        for tag in data["tags"]:
            category_id = tag["category_id"]
            if category_id not in parents:
                parent_item = QTreeWidgetItem([tag["category_name"], "", ""])
                self.tags.addTopLevelItem(parent_item)
                parents[category_id] = parent_item
            count = tag["video_count"]
            item = QTreeWidgetItem([tag["tag_name"], "", f"{count:,} / {total:,} 部"])
            item.setData(1, Qt.UserRole, (count, total))
            item.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
            parents[category_id].addChild(item)
        self.tags.expandAll()
        self.search.textChanged.connect(self._filter_tags)
        layout.addWidget(self.tags, 1)
        footnote = QLabel("可用影片與標籤比例不含找不到檔案的記錄；同組每個 MP4 分段各算一部。")
        footnote.setObjectName("statsMuted")
        layout.addWidget(footnote)
        close = QPushButton("關閉")
        close.clicked.connect(self.accept)
        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(close)
        layout.addLayout(button_row)

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("statsSection")
        return label

    def _filter_tags(self, text: str) -> None:
        needle = text.strip().casefold()
        for number in range(self.tags.topLevelItemCount()):
            parent = self.tags.topLevelItem(number)
            category_match = needle in parent.text(0).casefold()
            visible_children = 0
            for child_number in range(parent.childCount()):
                child = parent.child(child_number)
                visible = category_match or needle in child.text(0).casefold()
                child.setHidden(not visible)
                visible_children += visible
            parent.setHidden(visible_children == 0)
