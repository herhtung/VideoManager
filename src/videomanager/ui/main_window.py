"""Three-pane library browser, triage, and protected file actions."""

from __future__ import annotations

from collections import OrderedDict
import json
import shutil
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, QItemSelectionModel, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar, QProgressDialog, QPushButton,
    QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

from videomanager.config import AppPaths, load_settings, save_settings
from videomanager.compression.repository import interrupt_stale_jobs
from videomanager.compression import repository as compression_jobs
from videomanager.compression.safe_replace import recover_replacements
from videomanager.compression.simple_editor import interrupt_stale_edits, recover_edit_replacements
from videomanager.db import repository as repo
from videomanager.db.database import open_database
from videomanager.services.backup_service import daily_backup, export_backup, restore_backup
from videomanager.services.file_operations import create_library_folder, move_video
from videomanager.services.trash_service import recover_trash, trash_video
from videomanager.services.existing_name_cleanup import apply_existing_name_changes, scan_existing_names
from videomanager.services.index_service import retry_thumbnail
from videomanager.services.import_service import MAX_FILES_PER_FOLDER, ImportItem, plan_group_import
from videomanager.services.library_locator import read_marker
from videomanager.services.library_service import open_library, reopen_last_library, reset_library_root
from videomanager.services.thumbnail_service import PREVIEW_COUNTS
from videomanager.services.video_group_service import (add_group_members, create_group_in_folder, create_video_group,
    dissolve_group, expand_group_ids, group_for_video, list_groups, plan_group_folder,
    remove_group_member, rename_group)
from videomanager.services.sync_service import register_imported_paths
from videomanager.ui.browser_view import BrowserView, VideoModel
from videomanager.ui.compression_center import CompressionCenter
from videomanager.ui.details_panel import DetailsPanel
from videomanager.ui.existing_name_dialog import ExistingNameDialog
from videomanager.ui.filter_dialog import FilterDialog
from videomanager.ui.group_order_dialog import GroupOrderDialog
from videomanager.ui.group_members_dialog import GroupMembersDialog
from videomanager.ui.index_worker import IndexWorker
from videomanager.ui.import_worker import ImportPlanWorker, ImportWorker
from videomanager.ui.import_name_rules import ImportNameRulesDialog
from videomanager.ui.metadata_dialog import MetadataDialog
from videomanager.ui.notifications import NotificationDialog, NotificationHistory, Toast, notify_information
from videomanager.ui.player_view import PlayerView
from videomanager.ui.settings_dialog import SettingsDialog
from videomanager.ui.stats_dialog import StatsDialog
from videomanager.ui.trash_page import TrashPage


def compact_browser_rows(rows) -> list[dict]:
    """Keep individual videos visible and show each video group as one folder card."""
    singles: list[dict] = []
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for source in rows:
        row = dict(source)
        if row.get("group_id"):
            groups.setdefault(row["group_id"], []).append(row)
        else:
            singles.append(row)
    covers: list[dict] = []
    for members in groups.values():
        members.sort(key=lambda row: (row.get("group_position") or 0, row["id"]))
        cover = dict(members[0])
        cover["_group_cover"] = True
        cover["_group_members"] = members
        cover["compressed_count"] = sum(bool(member.get("is_compressed")) for member in members)
        covers.append(cover)
    return singles + covers


class ClickableProgressBar(QProgressBar):
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()


class MainWindow(QMainWindow):
    def __init__(self, paths: AppPaths) -> None:
        super().__init__()
        self.paths = paths
        self.settings = load_settings(paths.settings)
        self.notification_history = NotificationHistory(paths.base / "notifications.json")
        self._toast: Toast | None = None
        self.root: Path | None = None
        self.library_id = self.settings.library_id
        self.worker: IndexWorker | None = None
        self.import_worker: ImportWorker | None = None
        self.import_plan_worker: ImportPlanWorker | None = None
        self._pending_import: list[Path] | Path | None = None
        self._pending_import_group = False
        self._pending_recycle_ids: list[str] | None = None
        self._pending_group_ids: list[str] | None = None
        self._import_is_group = False
        self._import_group_mode = False
        self._group_import_items: list[ImportItem] | None = None
        self._planned_items: list[ImportItem] | None = None
        self._planning_error: str | None = None
        self._import_result: dict | None = None
        self._return_nav_key = "all"
        self._group_return_filters: tuple[str, dict] | None = None
        self.player: PlayerView | None = None
        self.compression_center: CompressionCenter | None = None
        self.edit_center: CompressionCenter | None = None
        self._player_return_widget: QWidget | None = None
        self._player_chrome_visibility: tuple[bool, bool] | None = None
        self._screen_signal_handle = None
        self._compact_toolbar: bool | None = None
        self.rebuild_preview_after_worker = False
        self._restart_previews_after_worker = False
        self._large_library = False
        self._last_preview_refresh = 0.0
        self._last_browser_nav_key: str | None = None
        self._browser_refresh_serial = 0
        self._refreshing_browser = False
        self.advanced_filters: dict = {}
        self._closing_after_worker = False
        startup_error = ""
        try:
            opened = reopen_last_library(paths)
            if opened:
                self.root = opened.root
                self.library_id = opened.marker.library_id
                self.settings = load_settings(paths.settings)
        except Exception as exc:
            startup_error = str(exc)
        self.connection = open_database(paths.database, paths.backups)
        interrupt_stale_jobs(self.connection)
        interrupt_stale_edits(self.connection)
        if self.root is not None:
            try:
                recover_replacements(self.connection, self.root, paths.cache)
                recover_edit_replacements(self.connection, self.root, paths.cache)
                recover_trash(self.connection, self.library_id, self.root, paths.cache)
            except Exception as exc:
                startup_error = f"壓縮替換復原檢查失敗：{exc}"
        if self.library_id:
            self._large_library = self.connection.execute(
                "SELECT COUNT(*) FROM videos WHERE library_id = ? AND deleted_at IS NULL",
                (self.library_id,),
            ).fetchone()[0] > 100
        if self.library_id:
            try:
                daily_backup(paths)
            except Exception as exc:
                startup_error = f"備份失敗：{exc}"
        self.setWindowTitle("VideoManager")
        self.resize(1380, 820)
        self._build_ui()
        self._toast = Toast(self)
        self.refresh_navigation()
        self.refresh_videos()
        if startup_error:
            self.statusBar().showMessage(startup_error)
        elif self.library_id is None:
            self.statusBar().showMessage("尚未選擇影片庫。請按中央的「選擇影片庫」開始。")
        elif self.root is None:
            self.statusBar().showMessage("影片庫離線。可瀏覽索引；播放與檔案操作暫停。")
        else:
            self.statusBar().showMessage(f"已連接：{self.root}")
            QTimer.singleShot(0, self.start_scan)

    @staticmethod
    def _action(menu, label, callback) -> None:
        menu.addAction(label).triggered.connect(lambda *_: callback())

    def _background_busy(self) -> bool:
        return any(worker is not None and worker.isRunning() for worker in (
            self.worker, self.import_plan_worker, self.import_worker,
        ))

    def _build_ui(self) -> None:
        library = self.menuBar().addMenu("影片庫")
        for label, callback in (
            ("選擇影片庫…", self.choose_library),
            ("重新設定管理根目錄…", self.reset_library_root),
            ("依目前模式搬入 MP4…", self.import_files), ("依目前模式搬入資料夾…", self.import_folder),
            ("建立影片組：選多個 MP4…", self.import_group_files),
            ("建立影片組：選資料夾…", self.import_group_folder),
            ("重新掃描", self.start_scan),
            ("繼續／暫停縮圖載入", self.toggle_previews),
            ("取消背景工作", self.cancel_scan), ("建立資料夾…", self.create_folder),
            ("確認清理待清理記錄…", self.cleanup_records),
            ("匯出資料庫備份…", self.export_database),
            ("還原資料庫備份…", self.restore_database),
        ):
            self._action(library, label, callback)
        self._action(library, "匯入檔名清理…", self.show_import_name_rules)
        self._action(library, "剪輯中心", self.open_edit_center)
        self._action(library, "壓縮中心", self.open_compression_center)
        edit = self.menuBar().addMenu("編輯")
        for label, callback in (
            ("管理標籤…", self.manage_metadata), ("批次加／移除標籤…", self.batch_relations),
            ("改名或移動影片…", self.move_selected), ("重新產生所選影格", self.retry_selected),
            ("將所選影片建立為一組…", self.group_selected),
            ("將所選影片退回待整理", self.return_selected_to_unprocessed),
            ("將所選 MP4 移至程式回收桶…", self.recycle_selected),
        ):
            self._action(edit, label, callback)
        view = self.menuBar().addMenu("檢視")
        for label, callback in (
            ("影片庫統計…", self.show_statistics),
            ("通知中心…", self.show_notification_center),
            ("進階篩選…", self.show_filters), ("儲存目前篩選…", self.save_smart_filter),
            ("清除篩選", self.clear_filters), ("清單／網格切換", self.toggle_list),
            ("快取占用空間", self.show_cache_size), ("清除影格快取…", self.clear_cache),
        ):
            self._action(view, label, callback)
        settings_menu = self.menuBar().addMenu("設定")
        self._action(settings_menu, "影片庫位置、預覽模式與速度…", self.show_settings)
        self._action(settings_menu, "匯入檔名清理…", self.show_import_name_rules)

        shell = QWidget()
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        splitter = QSplitter(Qt.Horizontal)
        nav_panel = QWidget()
        nav_layout = QVBoxLayout(nav_panel)
        nav_layout.setContentsMargins(8, 8, 8, 7)
        nav_layout.setSpacing(4)
        nav_title = QLabel("瀏覽影片庫")
        nav_title.setObjectName("sectionLabel")
        nav_layout.addWidget(nav_title)
        self.navigation = QListWidget()
        self.navigation.currentItemChanged.connect(self._navigation_changed)
        nav_layout.addWidget(self.navigation, 1)
        tools_label = QLabel("常用整理")
        tools_label.setObjectName("sectionLabel")
        nav_layout.addWidget(tools_label)
        for label, callback in (
            ("剪輯中心", self.open_edit_center),
            ("壓縮中心", self.open_compression_center),
            ("管理標籤", self.manage_metadata),
            ("批次加／移除標籤", self.batch_relations),
            ("將選取影片建立為一組", self.group_selected),
            ("更換管理根目錄", self.reset_library_root),
            ("設定", self.show_settings),
        ):
            button = QPushButton(label)
            button.setObjectName("navAction")
            button.clicked.connect(callback)
            nav_layout.addWidget(button)
        nav_panel.setMinimumWidth(150)
        nav_panel.setMaximumWidth(235)
        splitter.addWidget(nav_panel)
        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(16, 14, 16, 12)
        center_layout.setSpacing(10)
        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜尋顯示名稱、檔名、備註")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self.refresh_videos)
        self.search.textChanged.connect(lambda text: self.refresh_videos() if not text else None)
        search_button = QPushButton("搜尋")
        search_button.setToolTip("依輸入文字搜尋影片")
        search_button.clicked.connect(self.refresh_videos)
        self.sort = QComboBox()
        for label, key in (("加入日期", "added"), ("檔名", "name"), ("修改日期", "modified"),
                           ("片長", "duration"), ("大小", "size"), ("評分", "rating")):
            self.sort.addItem(label, key)
        self.sort.currentIndexChanged.connect(lambda *_: self.refresh_videos())
        self.filter_button = QPushButton("篩選")
        self.filter_button.clicked.connect(self.show_filters)
        self.clear_filter_button = QPushButton("清除條件")
        self.clear_filter_button.setToolTip("清除搜尋文字與進階篩選，保留目前瀏覽位置")
        self.clear_filter_button.clicked.connect(self.clear_filters)
        statistics_button = QPushButton("統計")
        statistics_button.clicked.connect(self.show_statistics)
        self.statistics_button = statistics_button
        self.view_button = QPushButton("切換成清單")
        self.view_button.clicked.connect(self.toggle_list)
        self.view_more_button = QPushButton("更多")
        view_more_menu = QMenu(self.view_more_button)
        view_more_menu.addAction("統計", self.show_statistics)
        view_more_menu.addAction("切換清單／網格", self.toggle_list)
        self.view_more_button.setMenu(view_more_menu)
        self.view_more_button.hide()
        scan_button = QPushButton("掃描")
        scan_button.clicked.connect(self.start_scan)
        self.scan_button = scan_button
        self.preview_button = QPushButton()
        self.preview_button.clicked.connect(self.toggle_previews)
        self._update_preview_button()
        self.actions_more_button = QPushButton("更多")
        actions_more_menu = QMenu(self.actions_more_button)
        actions_more_menu.addAction("重新掃描", self.start_scan)
        actions_more_menu.addAction("繼續／暫停縮圖載入", self.toggle_previews)
        actions_more_menu.addAction("壓縮所選", self.compress_selected)
        self.actions_more_button.setMenu(actions_more_menu)
        self.actions_more_button.hide()
        self.import_modes = QButtonGroup(self)
        self.import_modes.setExclusive(True)
        self.separate_mode_button = QPushButton("分開")
        self.group_mode_button = QPushButton("同組")
        self.separate_mode_button.setToolTip("多個 MP4 各自成片；拖入資料夾會分開搬入其中的影片。")
        self.group_mode_button.setToolTip("多個 MP4 成為同一組；搬入前會確認分段順序。")
        for button in (self.separate_mode_button, self.group_mode_button):
            button.setObjectName("importMode")
            button.setCheckable(True)
            self.import_modes.addButton(button)
        self.separate_mode_button.setChecked(True)
        self.separate_mode_button.clicked.connect(lambda: self._set_import_mode(False))
        self.group_mode_button.clicked.connect(lambda: self._set_import_mode(True))
        self.import_button = QPushButton()
        self.import_button.setObjectName("primaryAction")
        self.import_button.clicked.connect(self.import_files)
        self.import_folder_button = QPushButton()
        self.import_folder_button.clicked.connect(self.import_folder)
        self._set_import_mode(False)
        toolbar.addWidget(self.search, 1)
        for widget in (search_button, self.sort, self.filter_button, self.clear_filter_button,
                       statistics_button, self.view_button, self.view_more_button):
            toolbar.addWidget(widget)
        center_layout.addLayout(toolbar)
        actions = QHBoxLayout()
        for widget in (self.separate_mode_button, self.group_mode_button,
                       self.import_button, self.import_folder_button, scan_button, self.preview_button,
                       self.actions_more_button):
            actions.addWidget(widget)
        actions.addStretch()
        self.recycle_button = QPushButton("刪除選取")
        self.recycle_button.setObjectName("destructiveAction")
        self.recycle_button.setToolTip("可按 Ctrl／Shift 多選；確認後將 MP4 移至影片庫磁碟上的程式回收桶。")
        self.recycle_button.setEnabled(False)
        self.recycle_button.clicked.connect(self.recycle_selected)
        self.compress_button = QPushButton("壓縮所選")
        self.compress_button.setToolTip("直接以目前選取的影片開啟壓縮設定；原片會保留")
        self.compress_button.setEnabled(False)
        self.compress_button.clicked.connect(self.compress_selected)
        actions.addWidget(self.compress_button)
        actions.addWidget(self.recycle_button)
        center_layout.addLayout(actions)
        self.summary = QLabel()
        self.summary.setObjectName("sectionLabel")
        center_layout.addWidget(self.summary)
        self.group_breadcrumb = QWidget()
        breadcrumb_layout = QHBoxLayout(self.group_breadcrumb)
        breadcrumb_layout.setContentsMargins(0, 0, 0, 0)
        self.group_back = QPushButton("← 返回影片清單")
        self.group_back.clicked.connect(self.close_group_folder)
        self.group_breadcrumb_title = QLabel()
        self.group_breadcrumb_title.setObjectName("sectionLabel")
        self.group_rename_button = QPushButton("修改組名")
        self.group_rename_button.setToolTip("修改影片組在清單中的名稱；不移動 MP4 或資料夾。")
        self.group_rename_button.clicked.connect(lambda: self.rename_video_group())
        self.group_add_button = QPushButton("加入分段")
        self.group_add_button.clicked.connect(self.add_to_current_group)
        self.group_dissolve_button = QPushButton("解散影片組")
        self.group_dissolve_button.setToolTip("解除分組；保留每一部影片、標籤及檔案。")
        self.group_dissolve_button.clicked.connect(lambda: self.dissolve_video_group())
        self.group_delete_button = QPushButton("刪除整組影片")
        self.group_delete_button.setObjectName("destructiveAction")
        self.group_delete_button.setToolTip("確認後將整組 MP4 移入程式回收桶，日後仍可還原。")
        self.group_delete_button.clicked.connect(lambda: self.recycle_current_group())
        breadcrumb_layout.addWidget(self.group_back)
        breadcrumb_layout.addWidget(self.group_breadcrumb_title, 1)
        breadcrumb_layout.addWidget(self.group_add_button)
        breadcrumb_layout.addWidget(self.group_rename_button)
        breadcrumb_layout.addWidget(self.group_dissolve_button)
        breadcrumb_layout.addWidget(self.group_delete_button)
        self.group_breadcrumb.hide()
        center_layout.addWidget(self.group_breadcrumb)
        self.empty_state = QWidget()
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.addStretch()
        self.empty_title = QLabel()
        title_font = self.empty_title.font()
        title_font.setPointSize(18)
        title_font.setBold(True)
        self.empty_title.setFont(title_font)
        self.empty_title.setAlignment(Qt.AlignCenter)
        self.empty_message = QLabel()
        self.empty_message.setWordWrap(True)
        self.empty_message.setAlignment(Qt.AlignCenter)
        self.empty_button = QPushButton()
        self.empty_button.setMinimumWidth(180)
        self.empty_button.clicked.connect(self._empty_action)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_message)
        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(self.empty_button)
        button_row.addStretch()
        empty_layout.addLayout(button_row)
        empty_layout.addStretch()
        center_layout.addWidget(self.empty_state, 1)
        self.model = VideoModel(self.paths.cache, self.settings.preview_mode)
        self.browser = BrowserView(self.model, self.settings.hover_delay_ms, self.settings.frame_interval_ms)
        self.browser.set_thumbnail_width(self.settings.thumbnail_width)
        self.browser.selectionModel().selectionChanged.connect(lambda *_: self.show_selected_details())
        self.browser.selectionModel().selectionChanged.connect(lambda *_: self._update_recycle_button())
        self.browser.activated_video.connect(self.open_player)
        self.browser.group_play_requested.connect(self.play_group_from_start)
        self.browser.group_open_requested.connect(self.open_group_folder)
        self.browser.files_dropped.connect(self._handle_dropped_paths)
        self.browser.context_menu_requested.connect(self.show_video_context_menu)
        self.browser.blank_clicked.connect(self.show_selected_details)
        center_layout.addWidget(self.browser, 1)
        splitter.addWidget(center)
        self.details = DetailsPanel(self.connection)
        self.details.set_library(self.root, self.library_id)
        self.details.setMinimumWidth(220)
        self.details.set_video(None)
        self.details.saved.connect(self.after_metadata_saved)
        self.details.completed.connect(self.advance_after_complete)
        self.details.skipped.connect(self.select_next)
        splitter.addWidget(self.details)
        splitter.setSizes([190, 880, 310])
        self.main_stack = QStackedWidget()
        self.browser_page = splitter
        self.main_stack.addWidget(splitter)
        self.trash_page = TrashPage(self.connection, self.root, self.library_id, self.paths.cache)
        self.trash_page.back_requested.connect(self.show_library_page)
        self.trash_page.play_requested.connect(self.open_media_preview)
        self.trash_page.library_changed.connect(self._compression_library_changed)
        self.main_stack.addWidget(self.trash_page)
        self.library_task_strip = QWidget()
        task_layout = QHBoxLayout(self.library_task_strip)
        task_layout.setContentsMargins(12, 3, 12, 3)
        task_layout.setSpacing(8)
        self.library_task_label = QLabel("搬入與縮圖：目前沒有工作")
        self.library_task_label.setMinimumWidth(0)
        self.library_task_progress = QProgressBar()
        self.library_task_progress.setMinimumWidth(90)
        self.library_task_progress.setMaximumWidth(220)
        self.library_task_progress.setRange(0, 100)
        self.library_task_progress.setValue(0)
        self.library_task_progress.setFormat("待命")
        task_layout.addWidget(self.library_task_label, 1)
        task_layout.addWidget(self.library_task_progress)
        shell_layout.addWidget(self.library_task_strip)
        shell_layout.addWidget(self.main_stack, 1)
        self.setCentralWidget(shell)
        self.setAcceptDrops(True)
        self.index_progress = QProgressBar(self)
        self.index_progress.setMinimumWidth(230)
        self.index_progress.setTextVisible(True)
        self.index_progress.hide()
        self.statusBar().addPermanentWidget(self.index_progress)
        self.compression_progress = ClickableProgressBar(self)
        self.compression_progress.setMinimumWidth(230)
        self.compression_progress.setFormat("壓縮 %p% · 點擊查看")
        self.compression_progress.setCursor(Qt.PointingHandCursor)
        self.compression_progress.setToolTip("點擊開啟壓縮中心，查看目前工作與完整資訊")
        self.compression_progress.clicked.connect(self.open_compression_center)
        self.compression_progress.hide()
        self.statusBar().addPermanentWidget(self.compression_progress)
        self.notification_button = QPushButton("通知")
        self.notification_button.setObjectName("statusAction")
        self.notification_button.clicked.connect(self.show_notification_center)
        self.statusBar().addPermanentWidget(self.notification_button)

    def notify(self, title: str, message: str) -> None:
        text = f"{title}：{message}" if title else message
        try:
            self.notification_history.add(text)
        except OSError:
            pass
        if self._toast is not None and self.isVisible() and not self.isMinimized():
            self._toast.announce(text)
        self.statusBar().showMessage(text, 5000)

    def show_notification_center(self) -> None:
        NotificationDialog(self.notification_history, self).exec()

    def _set_compact_toolbar(self, compact: bool) -> None:
        if self._compact_toolbar == compact:
            return
        self._compact_toolbar = compact
        self.statistics_button.setVisible(not compact)
        self.view_button.setVisible(not compact)
        self.view_more_button.setVisible(compact)
        self.scan_button.setVisible(not compact)
        self.preview_button.setVisible(not compact)
        self.compress_button.setVisible(not compact)
        self.actions_more_button.setVisible(compact)

    def _fit_to_current_screen(self) -> None:
        handle = self.windowHandle()
        screen = handle.screen() if handle is not None else QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        previous_toolbar = self._compact_toolbar
        self._set_compact_toolbar(available.width() < 1280)
        if previous_toolbar != self._compact_toolbar:
            QTimer.singleShot(0, self._fit_to_current_screen)
            return
        if self.isMaximized() or self.isFullScreen():
            return
        frame = self.frameGeometry()
        extra_width = max(0, frame.width() - self.width())
        extra_height = max(0, frame.height() - self.height())
        self.resize(min(self.width(), available.width() - extra_width),
                    min(self.height(), available.height() - extra_height))
        frame = self.frameGeometry()
        x = max(available.left(), min(frame.left(), available.right() - frame.width() + 1))
        y = max(available.top(), min(frame.top(), available.bottom() - frame.height() + 1))
        if (frame.left(), frame.top()) != (x, y):
            self.move(self.pos().x() + x - frame.left(), self.pos().y() + y - frame.top())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None and handle is not self._screen_signal_handle:
            handle.screenChanged.connect(lambda *_: QTimer.singleShot(0, self._fit_to_current_screen))
            self._screen_signal_handle = handle
        QTimer.singleShot(0, self._fit_to_current_screen)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            QTimer.singleShot(0, self._fit_to_current_screen)

    def add_scan_folder(self) -> None:
        if self.root is None or self.library_id is None:
            notify_information(self, "先設定影片庫", "請先設定管理根目錄。")
            return
        if self._background_busy():
            notify_information(self, "請稍候", "請先暫停目前的背景工作。")
            return
        selected = QFileDialog.getExistingDirectory(self, "選擇要讀取的子資料夾", str(self.root))
        if not selected:
            return
        folder = Path(selected).resolve(strict=True)
        root = self.root.resolve(strict=True)
        if folder == root:
            notify_information(self, "請選子資料夾", "管理根目錄不會自動全盤掃描；請選擇要讀取的子資料夾。")
            return
        if not folder.is_relative_to(root) or folder.is_symlink():
            QMessageBox.warning(self, "資料夾不在影片庫內", "請選擇管理根目錄底下的子資料夾。")
            return
        relative = folder.relative_to(root).as_posix()
        if relative in self.settings.scan_folders:
            notify_information(self, "已加入", "這個資料夾已在讀取清單中。")
            return
        if QMessageBox.question(
            self, "確認讀取範圍", f"只讀取這個資料夾及其子資料夾內的 MP4：\n{folder}\n\n加入讀取清單？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        ) != QMessageBox.Yes:
            return
        self.settings = replace(self.settings, scan_folders=(*self.settings.scan_folders, relative))
        save_settings(self.paths.settings, self.settings)
        self.start_scan()

    def manage_scan_folders(self) -> None:
        if not self.settings.scan_folders:
            notify_information(self, "尚無手動讀取範圍", "請按「加入讀取資料夾」選擇要建立索引的位置。")
            return
        if self._background_busy():
            notify_information(self, "請稍候", "請先暫停目前的背景工作。")
            return
        labels = list(self.settings.scan_folders)
        selected, ok = QInputDialog.getItem(self, "管理讀取資料夾", "選擇要停止讀取的資料夾", labels, 0, False)
        if not ok:
            return
        if QMessageBox.question(
            self, "停止讀取資料夾", f"停止掃描：{self.root / selected if self.root else selected}\n"
            "影片原檔不會刪除。這個範圍的索引會標為待清理，稍後可由「確認清理待清理記錄」移除。繼續？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self.settings = replace(self.settings, scan_folders=tuple(path for path in labels if path != selected))
        save_settings(self.paths.settings, self.settings)
        self.start_scan()

    def _nav_key(self) -> str:
        item = self.navigation.currentItem()
        return item.data(Qt.UserRole) if item else "all"

    def _navigation_changed(self, current, previous) -> None:
        current_key = current.data(Qt.UserRole) if current else "all"
        previous_key = previous.data(Qt.UserRole) if previous else "all"
        if current_key == "trash":
            self.navigation.blockSignals(True)
            if previous is not None:
                self.navigation.setCurrentItem(previous)
            self.navigation.blockSignals(False)
            self.show_trash_page()
            return
        entering_group = current_key.startswith("group:")
        leaving_group = previous_key.startswith("group:") and not entering_group
        if entering_group:
            if not previous_key.startswith("group:"):
                self._return_nav_key = previous_key
                self._group_return_filters = (self.search.text(), dict(self.advanced_filters))
            # A folder must show every part, even when its cover was found through
            # a search that matched only one filename.
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
            self.advanced_filters = {}
        elif leaving_group and self._group_return_filters is not None:
            search_text, advanced_filters = self._group_return_filters
            self.search.blockSignals(True)
            self.search.setText(search_text)
            self.search.blockSignals(False)
            self.advanced_filters = advanced_filters
            self._group_return_filters = None
        self.refresh_videos()

    def _select_nav_key(self, key: str) -> bool:
        for number in range(self.navigation.count()):
            item = self.navigation.item(number)
            if item.data(Qt.UserRole) == key:
                self.navigation.setCurrentItem(item)
                return True
        return False

    def open_group_folder(self, group_id: str) -> None:
        self._return_nav_key = self._nav_key()
        self._select_nav_key(f"group:{group_id}")

    def close_group_folder(self) -> None:
        if not self._select_nav_key(self._return_nav_key):
            self._select_nav_key("all")

    def rename_video_group(self, group_id: str | None = None) -> None:
        if group_id is None:
            key = self._nav_key()
            group_id = key.split(":", 1)[1] if key.startswith("group:") else None
        if not group_id or self.library_id is None:
            return
        group = self.connection.execute(
            "SELECT name FROM video_groups WHERE id = ? AND library_id = ?",
            (group_id, self.library_id),
        ).fetchone()
        if group is None:
            QMessageBox.warning(self, "找不到影片組", "請重新整理影片清單。")
            return
        name, accepted = QInputDialog.getText(self, "修改影片組名稱", "影片組名稱", text=group["name"])
        if not accepted:
            return
        try:
            rename_group(self.connection, group_id, name)
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "修改組名失敗", str(exc))
            return
        self.refresh_navigation()
        self.refresh_videos()
        self.statusBar().showMessage(f"影片組已更名為「{name.strip()}」。", 5000)

    def dissolve_video_group(self, group_id: str | None = None) -> None:
        if group_id is None:
            key = self._nav_key()
            group_id = key.split(":", 1)[1] if key.startswith("group:") else None
        if not group_id or self.library_id is None:
            return
        group = self.connection.execute(
            "SELECT name FROM video_groups WHERE id = ? AND library_id = ?",
            (group_id, self.library_id),
        ).fetchone()
        if group is None:
            QMessageBox.warning(self, "找不到影片組", "請重新整理影片清單。")
            return
        if QMessageBox.question(
            self, "解散影片組", f"確定解散「{group['name']}」？\n\n每一部影片、標籤與檔案都會保留，只解除分組。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            dissolve_group(self.connection, group_id)
        except sqlite3.Error as exc:
            QMessageBox.warning(self, "解散失敗", str(exc))
            return
        if self._nav_key() == f"group:{group_id}":
            self._select_nav_key(self._return_nav_key) or self._select_nav_key("all")
        self.refresh_navigation()
        self.refresh_videos()
        self.statusBar().showMessage("已解散影片組；影片與檔案均保留。", 5000)

    def recycle_current_group(self) -> None:
        key = self._nav_key()
        if key.startswith("group:"):
            self.recycle_group(key.split(":", 1)[1])

    def recycle_group(self, group_id: str) -> None:
        if self.library_id is None:
            return
        ids = self._group_video_ids(group_id)
        if not ids:
            notify_information(self, "沒有可刪除的影片", "這組目前沒有可移入回收桶的影片。")
            return
        self._recycle_videos(ids)

    def _group_video_ids(self, group_id: str) -> list[str]:
        if self.library_id is None:
            return []
        return [row[0] for row in self.connection.execute(
            """SELECT v.id FROM video_group_members m JOIN videos v ON v.id=m.video_id
               WHERE m.group_id=? AND v.library_id=? AND v.deleted_at IS NULL
               ORDER BY m.position""", (group_id, self.library_id),
        )]

    def refresh_navigation(self) -> None:
        selected = self._nav_key()
        self.navigation.blockSignals(True)
        self.navigation.clear()
        counts = {}
        if self.library_id:
            rows = self.connection.execute(
                """SELECT status, COUNT(*) FROM videos WHERE library_id = ? AND
                   missing_since IS NULL AND deleted_at IS NULL GROUP BY status""",
                (self.library_id,),
            )
            counts = {row[0]: row[1] for row in rows}
        trash_count = self.connection.execute(
            """SELECT COUNT(*) FROM trash_entries t JOIN videos v ON v.id=t.video_id
               WHERE v.library_id=?""", (self.library_id,),
        ).fetchone()[0] if self.library_id else 0
        items = [
            ("unprocessed", f"待整理 ({counts.get('unprocessed', 0)})"),
            ("in_progress", f"整理中 ({counts.get('in_progress', 0)})"),
            ("all", "全部影片"), ("done", f"已整理 ({counts.get('done', 0)})"),
            ("favorite", "收藏"), ("missing", "待清理"),
            ("trash", f"程式回收桶 ({trash_count})"),
        ]
        items += [(f"tag:{row['id']}", f"標籤 › {row['group_name']} › {row['name']}")
                  for row in repo.list_tags(self.connection)]
        items += [(f"group:{row['id']}", f"影片組 › {row['name']} ({row['part_count']})")
                  for row in list_groups(self.connection, self.library_id)] if self.library_id else []
        items += [(f"saved:{row['id']}", f"已存篩選 › {row['name']}") for row in repo.list_saved_filters(self.connection)]
        for key, label in items:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, key)
            self.navigation.addItem(item)
            if key == selected:
                self.navigation.setCurrentItem(item)
        if self.navigation.currentItem() is None and self.navigation.count():
            self.navigation.setCurrentRow(2)
        self.navigation.blockSignals(False)

    def _effective_filters(self) -> dict:
        result = dict(self.advanced_filters)
        key = self._nav_key()
        if key in {"unprocessed", "in_progress", "done"}:
            result["status"] = key
        elif key == "favorite":
            result["favorite"] = True
        elif key == "missing":
            result["missing_only"] = True
        elif key.startswith("tag:"):
            result["tag_all"] = [key.split(":", 1)[1]]
        elif key.startswith("group:"):
            result["video_group_id"] = key.split(":", 1)[1]
        elif key.startswith("saved:"):
            row = self.connection.execute("SELECT filter_json FROM saved_filters WHERE id = ?", (key.split(":", 1)[1],)).fetchone()
            if row:
                result.update(json.loads(row[0]))
        if self.search.text().strip():
            result["text"] = self.search.text().strip()
        return result

    def refresh_videos(self) -> None:
        if not hasattr(self, "model"):
            return
        selected = self.selected_video_id()
        current = self.browser.currentIndex()
        selected_group = (self.model.rows[current.row()].get("group_id")
                          if current.isValid() and current.row() < len(self.model.rows) else None)
        if selected_group is None:
            selected_group = self.details.group_id
        nav_key = self._nav_key()
        same_view = nav_key == self._last_browser_nav_key
        scroll_value = self.browser.verticalScrollBar().value() if same_view else 0
        try:
            rows = repo.list_videos(
                self.connection, self.library_id, self._effective_filters(),
                sort=self.sort.currentData() or "added",
            ) if self.library_id else []
        except (ValueError, sqlite3.Error) as exc:
            self.statusBar().showMessage(f"篩選失敗：{exc}")
            rows = []
        folder_view = self._nav_key().startswith("group:")
        if folder_view:
            visible_rows = sorted((dict(row) for row in rows), key=lambda row: (row.get("group_position") or 0, row["id"]))
            group_id = self._nav_key().split(":", 1)[1]
            group = self.connection.execute(
                "SELECT name FROM video_groups WHERE id = ? AND library_id = ?", (group_id, self.library_id),
            ).fetchone()
            folder_name = group["name"] if group else "影片組"
            self.group_breadcrumb_title.setText(folder_name)
            self.group_breadcrumb.show()
            self.browser.set_folder_title(f"{folder_name} · 分段")
            summary = f"{len(visible_rows)} 段影片"
        else:
            visible_rows = compact_browser_rows(rows)
            group_count = sum(bool(row.get("_group_cover")) for row in visible_rows)
            self.group_breadcrumb.hide()
            self.browser.set_folder_title(None)
            summary = f"{len(rows)} 部影片 · {group_count} 組" if group_count else f"顯示 {len(rows)} 部影片"
        self._refreshing_browser = True
        self.model.set_rows(visible_rows)
        self._last_browser_nav_key = nav_key
        self.summary.setText(summary + (" · 影片庫離線" if self.root is None else ""))
        self._update_empty_state(len(rows))
        for index, row in enumerate(self.model.rows):
            if row["id"] == selected or (row.get("_group_cover") and row.get("group_id") == selected_group):
                self.browser.setCurrentIndex(self.model.index(index))
                break
        self._refreshing_browser = False
        self.show_selected_details()
        self._browser_refresh_serial += 1
        serial = self._browser_refresh_serial
        QTimer.singleShot(0, lambda value=scroll_value, token=serial: self.browser.verticalScrollBar().setValue(value)
                          if token == self._browser_refresh_serial else None)
        self._update_recycle_button()
        self._update_filter_controls()

    def _update_filter_controls(self) -> None:
        count = len(self.advanced_filters)
        self.filter_button.setText(f"篩選（{count}）" if count else "篩選")
        self.clear_filter_button.setEnabled(bool(count or self.search.text()))

    def _update_empty_state(self, visible_count: int) -> None:
        empty = visible_count == 0
        self.empty_state.setVisible(empty)
        self.browser.setVisible(not empty)
        if not empty:
            return
        if self.library_id is None:
            title, message, button, action = (
                "尚未設定影片庫位置", "選擇最外層管理資料夾；搬入的 MP4 會依加入月份放進匯入資料夾。",
                "設定影片庫根目錄…", "choose",
            )
        elif self.root is None:
            title, message, button, action = (
                "影片庫未連線", "請重新選擇原本的影片庫。標籤和備註仍保留在本機。",
                "重新連接影片庫…", "choose",
            )
        elif self.worker and self.worker.isRunning():
            title, message, button, action = (
                "正在掃描影片庫", "找到影片後會先顯示清單，影格預覽會在背景陸續完成。",
                "取消背景工作", "cancel",
            )
        elif (self.import_worker and self.import_worker.isRunning()) or (
            self.import_plan_worker and self.import_plan_worker.isRunning()
        ):
            title, message, button, action = (
                "正在準備或搬入 MP4", "程式正在尋找影片並分配資料夾。", "取消背景工作", "cancel",
            )
        elif self.connection.execute(
            "SELECT 1 FROM videos WHERE library_id = ? AND deleted_at IS NULL LIMIT 1", (self.library_id,),
        ).fetchone():
            title, message, button, action = (
                "目前沒有符合條件的影片", "可查看全部影片，或調整搜尋與篩選條件。",
                "查看全部影片", "all",
            )
        elif not self.settings.scan_folders:
            title, message, button, action = (
                "還沒有選擇讀取範圍", "管理根目錄只決定搬入位置。請手動選擇要讀取的子資料夾，或直接搬入 MP4。",
                "加入讀取資料夾…", "scan_folder",
            )
        else:
            title, message, button, action = (
                "尚未找到 MP4", "可從其他位置搬入 MP4；程式會自動分配資料夾，並加入待整理。",
                "搬入 MP4…", "import",
            )
        self.empty_title.setText(title)
        self.empty_message.setText(message)
        self.empty_button.setText(button)
        self._empty_action_kind = action

    def _empty_action(self) -> None:
        if self._empty_action_kind == "choose":
            self.choose_library()
        elif self._empty_action_kind == "cancel":
            self.cancel_scan()
        elif self._empty_action_kind == "all":
            self.advanced_filters = {}
            self.search.clear()
            self.navigation.setCurrentRow(2)
            self.refresh_videos()
        elif self._empty_action_kind == "import":
            self.import_files()
        elif self._empty_action_kind == "scan_folder":
            self.add_scan_folder()
        else:
            self.start_scan()

    def selected_video_id(self) -> str | None:
        if not hasattr(self, "browser"):
            return None
        index = self.browser.currentIndex()
        return index.data(Qt.UserRole) if index.isValid() and not self.model.rows[index.row()].get("_group_cover") else None

    def _selected_video_ids(self) -> list[str]:
        selected: list[str] = []
        for index in self.browser.selectedIndexes():
            if not index.isValid():
                continue
            row = self.model.rows[index.row()]
            if row.get("_group_cover"):
                selected.extend(self._group_video_ids(row["group_id"]))
            else:
                selected.append(index.data(Qt.UserRole))
        return list(dict.fromkeys(selected))

    def _update_recycle_button(self) -> None:
        count = len(self._selected_video_ids())
        indexes = self.browser.selectedIndexes()
        only_group = (len(indexes) == 1 and indexes[0].isValid()
                      and self.model.rows[indexes[0].row()].get("_group_cover"))
        self.recycle_button.setText(
            f"刪除整組（{count} 段）" if only_group else
            f"刪除選取（{count}）" if count > 1 else "刪除選取"
        )
        self.recycle_button.setEnabled(
            bool(count) and self.root is not None and self.library_id is not None
            and self._pending_recycle_ids is None
        )
        self.compress_button.setEnabled(bool(self.browser.selectedIndexes()) and self.root is not None)

    def show_selected_details(self) -> None:
        if self._refreshing_browser:
            return
        indexes = self.browser.selectedIndexes()
        batch_ids = [index.data(Qt.UserRole) for index in indexes if index.isValid()
                     and not self.model.rows[index.row()].get("_group_cover")
                     and not self.model.rows[index.row()].get("group_id")]
        if len(batch_ids) > 1 and len(batch_ids) == len(indexes) and all(
            not repo.video_tag_ids(self.connection, video_id) for video_id in batch_ids
        ):
            self.details.set_batch(batch_ids)
            return
        video_id = self.selected_video_id()
        current = self.browser.currentIndex()
        group_id = (self.model.rows[current.row()].get("group_id")
                    if current.isValid() and self.model.rows[current.row()].get("_group_cover") else None)
        if group_id:
            self.details.set_group(group_id)
        else:
            self.details.set_video(video_id)

    def show_video_context_menu(self, position) -> None:
        index = self.browser.indexAt(position)
        if not index.isValid():
            return
        selection = self.browser.selectionModel()
        if selection.isSelected(index):
            selection.setCurrentIndex(index, QItemSelectionModel.NoUpdate)
        else:
            selection.setCurrentIndex(index, QItemSelectionModel.ClearAndSelect)
        self.show_selected_details()
        row = self.model.rows[index.row()]
        if row.get("_group_cover"):
            menu = QMenu(self)
            menu.addAction("播放整組").triggered.connect(lambda: self.play_group_from_start(row["group_id"]))
            menu.addAction("開啟分段").triggered.connect(lambda: self.open_group_folder(row["group_id"]))
            menu.addAction("壓縮整組分段…").triggered.connect(
                lambda: self.open_compression_center([part["id"] for part in row["_group_members"]]))
            menu.addAction("剪輯分段…").triggered.connect(
                lambda: self.open_compression_center([part["id"] for part in row["_group_members"]], focus_cut=True))
            menu.addAction("加入分段…").triggered.connect(lambda: self.add_to_group(row["group_id"]))
            menu.addAction("修改影片組名稱…").triggered.connect(lambda: self.rename_video_group(row["group_id"]))
            menu.addAction("解散影片組…").triggered.connect(lambda: self.dissolve_video_group(row["group_id"]))
            delete_group = menu.addAction("刪除整組影片…")
            delete_group.setEnabled(self.root is not None)
            delete_group.triggered.connect(lambda: self.recycle_group(row["group_id"]))
            menu.addAction("整組退回待整理").triggered.connect(self.return_selected_to_unprocessed)
            missing = [member["id"] for member in row["_group_members"]
                       if member["thumbnail_state"] == "error"
                       or not (self.paths.cache / member["id"] / "00.webp").is_file()]
            retry = menu.addAction("重試缺少的影格")
            retry.setEnabled(bool(missing) and self.root is not None)
            retry.triggered.connect(lambda _=False, ids=missing: self._retry_thumbnails(ids))
            menu.setAttribute(Qt.WA_DeleteOnClose)
            menu.popup(self.browser.viewport().mapToGlobal(position))
            return
        ids = self._selected_video_ids()
        if not ids:
            return
        menu = self._build_video_context_menu(ids)
        menu.setAttribute(Qt.WA_DeleteOnClose)
        menu.popup(self.browser.viewport().mapToGlobal(position))

    def _build_video_context_menu(self, ids: list[str]) -> QMenu:
        menu = QMenu(self)
        if len(ids) == 1:
            play = menu.addAction("播放影片")
            play.setEnabled(self.root is not None)
            play.triggered.connect(lambda: self.open_player(ids[0]))
        tags = menu.addAction("加／移除標籤…")
        tags.triggered.connect(self.batch_relations)
        menu.addAction("壓縮這部影片…" if len(ids) == 1 else f"加入壓縮佇列（{len(ids)} 部）…").triggered.connect(
            lambda: self.open_compression_center(ids))
        if len(ids) == 1:
            menu.addAction("剪掉一段…").triggered.connect(
                lambda: self.open_compression_center(ids, focus_cut=True))
        if len(ids) > 1:
            group = menu.addAction("建立為同一組…")
            group.setEnabled(self.root is not None and not self._background_busy())
            group.triggered.connect(self.group_selected)
        statuses = {row["id"]: row["status"] for row in self.model.rows}
        reset = menu.addAction("退回待整理" if len(ids) == 1 else f"將 {len(ids)} 部退回待整理")
        reset.setEnabled(any(statuses.get(video_id) != "unprocessed" for video_id in ids))
        reset.triggered.connect(self.return_selected_to_unprocessed)
        if len(ids) == 1:
            member = group_for_video(self.connection, ids[0])
            if member:
                menu.addAction("從影片組移出").triggered.connect(
                    lambda _=False, group_id=member["id"], video_id=ids[0]:
                    self.remove_from_group(group_id, video_id))
        if len(ids) == 1:
            retry = menu.addAction("重新產生影格")
            retry.setEnabled(self.root is not None)
            retry.triggered.connect(self.retry_selected)
        menu.addSeparator()
        remove = menu.addAction("刪除選取…" if len(ids) > 1 else "刪除影片…")
        remove.setEnabled(self.root is not None)
        remove.triggered.connect(self.recycle_selected)
        return menu

    def add_to_current_group(self) -> None:
        key = self._nav_key()
        if key.startswith("group:"):
            self.add_to_group(key.split(":", 1)[1])

    def add_to_group(self, group_id: str) -> None:
        if not self.library_id:
            return
        rows = self.connection.execute(
            """SELECT v.id, v.display_title, v.relative_path FROM videos v
               LEFT JOIN video_group_members m ON m.video_id = v.id
               WHERE v.library_id = ? AND v.deleted_at IS NULL AND v.missing_since IS NULL
                 AND m.video_id IS NULL ORDER BY v.display_title COLLATE NOCASE""",
            (self.library_id,),
        ).fetchall()
        if not rows:
            notify_information(self, "沒有可加入的影片", "目前沒有未分組的 MP4。")
            return
        dialog = GroupMembersDialog(rows, self.paths.cache, self)
        if dialog.exec() != QDialog.Accepted or not dialog.selected_ids():
            return
        try:
            count = add_group_members(self.connection, group_id, dialog.selected_ids())
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "加入失敗", str(exc))
            return
        self.refresh_navigation()
        self.refresh_videos()
        self.statusBar().showMessage(f"已加入 {count} 段；原始 MP4 沒有搬動。", 5000)

    def remove_from_group(self, group_id: str, video_id: str) -> None:
        if QMessageBox.question(
            self, "移出影片組", "只解除同組關係，保留影片、標籤及原本檔案位置。確定移出？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            dissolved = remove_group_member(self.connection, group_id, video_id)
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "移出失敗", str(exc))
            return
        if dissolved and self._nav_key() == f"group:{group_id}":
            self._return_nav_key = "all"
        self.refresh_navigation()
        self.refresh_videos()
        self.statusBar().showMessage("已移出分段。" + ("影片組不足兩段，已解除分組。" if dissolved else ""), 5000)

    def after_metadata_saved(self) -> None:
        self.refresh_navigation()
        self.refresh_videos()

    def compress_selected(self) -> None:
        ids = expand_group_ids(self.connection, [index.data(Qt.UserRole)
                                                 for index in self.browser.selectedIndexes()])
        if not ids:
            return
        self.open_compression_center(ids)

    def open_compression_center(self, video_ids: list[str] | None = None, *, focus_cut: bool = False) -> None:
        if focus_cut:
            self.open_edit_center(video_ids)
            return
        if self.player is not None and self.main_stack.currentWidget() is self.player:
            self.player.close()
            if self.player is not None:
                return
        if self.compression_center is None:
            center = CompressionCenter(self.paths, self.connection, self.root, self.library_id,
                                       video_ids or [], self, embedded=True)
            self.compression_center = center
            center.library_changed.connect(self._compression_library_changed)
            center.job_progress.connect(self._compression_progress_changed)
            center.job_notice.connect(lambda message: self.notify("壓縮中心", message))
            center.replacement_active.connect(lambda active: self.worker.set_paused(active) if self.worker else None)
            center.play_path_requested.connect(self.open_media_preview)
            center.back_requested.connect(self.show_library_page)
            self.main_stack.addWidget(center)
        elif video_ids:
            self.compression_center.focus_videos(video_ids)
        self.compression_center.show_workflow()
        self.main_stack.setCurrentWidget(self.compression_center)

    def open_edit_center(self, video_ids: list[str] | None = None) -> None:
        if self.player is not None and self.main_stack.currentWidget() is self.player:
            self.player.close()
            if self.player is not None:
                return
        if self.edit_center is None:
            center = CompressionCenter(self.paths, self.connection, self.root, self.library_id,
                                       video_ids or [], self, embedded=True, edit_only=True)
            self.edit_center = center
            center.library_changed.connect(self._compression_library_changed)
            center.job_progress.connect(self._compression_progress_changed)
            center.job_notice.connect(lambda message: self.notify("剪輯中心", message))
            center.replacement_active.connect(lambda active: self.worker.set_paused(active) if self.worker else None)
            center.play_path_requested.connect(self.open_media_preview)
            center.back_requested.connect(self.show_library_page)
            self.main_stack.addWidget(center)
        elif video_ids:
            self.edit_center.focus_cut(video_ids[0])
        self.main_stack.setCurrentWidget(self.edit_center)

    def show_library_page(self) -> None:
        self.main_stack.setCurrentWidget(self.browser_page)
        self.refresh_videos()

    def show_trash_page(self) -> None:
        self.trash_page.refresh()
        self.main_stack.setCurrentWidget(self.trash_page)

    def _release_tool_pages(self) -> bool:
        for center in (self.compression_center, self.edit_center):
            if center is not None and (center.has_active_work() or center.cap_thread.isRunning()):
                notify_information(self, "工作進行中", "請等壓縮或剪輯工作結束後再切換影片庫。")
                return False
        for attribute in ("compression_center", "edit_center"):
            center = getattr(self, attribute)
            if center is not None:
                center.close()
                self.main_stack.removeWidget(center)
                center.deleteLater()
                setattr(self, attribute, None)
        self.show_library_page()
        return True

    def _compression_library_changed(self) -> None:
        self.model.pixmaps.clear()
        self.after_metadata_saved()
        self.model.refresh_preview_jobs(self.connection)
        self.browser.viewport().update()
        self.trash_page.refresh()

    def _compression_center_finished(self, *_args) -> None:
        self.compression_center = None
        self.compression_progress.hide()
        if self._closing_after_worker:
            QTimer.singleShot(0, self.close)

    def _compression_progress_changed(self, value: int, message: str) -> None:
        if value < 0:
            self.compression_progress.hide()
            return
        self.compression_progress.setValue(value)
        self.compression_progress.setToolTip(message)
        self.compression_progress.show()

    def return_selected_to_unprocessed(self) -> None:
        ids = expand_group_ids(self.connection, [index.data(Qt.UserRole)
                                                for index in self.browser.selectedIndexes()])
        if not ids or self.library_id is None:
            notify_information(self, "請先選取影片", "可按 Ctrl 或 Shift 多選要退回待整理的影片。")
            return
        rows = [repo.video_by_id(self.connection, video_id) for video_id in ids]
        eligible = [row["id"] for row in rows if row is not None and
                    row["library_id"] == self.library_id and row["missing_since"] is None and
                    row["deleted_at"] is None and row["status"] != "unprocessed"]
        if not eligible:
            notify_information(self, "無須退回", "選取的影片已在待整理，或目前不可用。")
            return
        try:
            with self.connection:
                self.connection.executemany(
                    "UPDATE videos SET status = 'unprocessed' WHERE id = ? AND library_id = ?",
                    ((video_id, self.library_id) for video_id in eligible),
                )
        except sqlite3.Error as exc:
            QMessageBox.critical(self, "退回失敗", str(exc))
            return
        self.after_metadata_saved()
        self.statusBar().showMessage(f"已將 {len(eligible)} 部影片退回待整理；原有標籤與資料保留。", 5000)

    def select_next(self) -> None:
        index = self.browser.currentIndex().row()
        if self.model.rowCount():
            self.browser.setCurrentIndex(self.model.index(min(index + 1, self.model.rowCount() - 1)))

    def advance_after_complete(self) -> None:
        index = self.browser.currentIndex().row()
        self.refresh_navigation()
        self.refresh_videos()
        if self.model.rowCount():
            self.browser.setCurrentIndex(self.model.index(min(max(index, 0), self.model.rowCount() - 1)))

    def choose_library(self) -> None:
        if (self.worker and self.worker.isRunning()) or (self.import_worker and self.import_worker.isRunning()) or (
            self.import_plan_worker and self.import_plan_worker.isRunning()
        ):
            self.statusBar().showMessage("請先取消背景工作，再切換影片庫。")
            return
        selected = QFileDialog.getExistingDirectory(self, "設定影片庫最外層根目錄")
        if not selected:
            return
        root = Path(selected)
        if not self._release_tool_pages():
            return
        try:
            marker = read_marker(root)
            if self.library_id is not None and marker is None:
                notify_information(
                    self, "請選擇原影片庫",
                    "目前已有影片庫。重新連接時，請選擇包含 .videomanager 識別檔的原根目錄。"
                    "若要更換磁碟位置，請先搬動整個影片庫根目錄（包含識別檔），再選擇新位置。",
                )
                return
            create = marker is None
            if create and QMessageBox.question(
                self, "建立影片庫識別檔",
                "此資料夾沒有識別檔。要建立 .videomanager/library.json 嗎？\n只建立識別檔，不修改 MP4。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return
            opened = open_library(self.paths, root, create=create)
            self.root = opened.root
            self.library_id = opened.marker.library_id
            self.settings = load_settings(self.paths.settings)
            self._large_library = self.connection.execute(
                "SELECT COUNT(*) FROM videos WHERE library_id = ? AND deleted_at IS NULL",
                (self.library_id,),
            ).fetchone()[0] > 100
            self._update_preview_button()
            self.details.set_library(self.root, self.library_id)
            self.trash_page.set_library(self.root, self.library_id)
            self.refresh_navigation()
            self.refresh_videos()
            self.start_scan()
        except Exception as exc:
            QMessageBox.critical(self, "無法開啟影片庫", str(exc))

    def reset_library_root(self) -> None:
        if self.root is None or self.library_id is None:
            notify_information(self, "無法更換根目錄", "請先重新連接目前的影片庫。")
            return
        if self._background_busy() or self.player is not None:
            notify_information(self, "請稍候", "請先結束背景工作與播放器，再更換根目錄。")
            return
        selected = QFileDialog.getExistingDirectory(self, "選擇新的空白管理根目錄", str(self.root.parent))
        if not selected:
            return
        new_root = Path(selected)
        count = self.connection.execute(
            "SELECT COUNT(*) FROM videos WHERE library_id = ? AND deleted_at IS NULL",
            (self.library_id,),
        ).fetchone()[0]
        if QMessageBox.question(
            self, "確認更換管理根目錄",
            f"原根目錄：{self.root}\n新根目錄：{new_root}\n\n"
            f"目前 {count} 部影片的索引會先備份，再從使用中的清單移除。"
            "原始 MP4 全部留在原位置；之後可自行分批選擇搬入新根目錄。"
            "\n新根目錄必須是空資料夾。確定繼續？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        if not self._release_tool_pages():
            return
        try:
            opened, backup = reset_library_root(self.paths, self.root, new_root)
            self.root = opened.root
            self.library_id = opened.marker.library_id
            self.settings = load_settings(self.paths.settings)
            self._large_library = False
            self.details.set_library(self.root, self.library_id)
            self.trash_page.set_library(self.root, self.library_id)
            self.details.set_video(None)
            self.refresh_navigation()
            self.refresh_videos()
            self._update_preview_button()
            self.start_scan()
            notify_information(
                self, "新影片庫已設定",
                f"新根目錄：{self.root}\n舊索引備份：{backup}\n"
                "原始影片仍在舊位置，可用「搬入 MP4」分批加入。",
            )
        except Exception as exc:
            QMessageBox.critical(self, "更換根目錄失敗", str(exc))

    def _update_preview_button(self) -> None:
        if hasattr(self, "preview_button"):
            paused = self.settings.preview_policy == "paused" or (
                self.settings.preview_policy == "auto" and self._large_library
            )
            self.preview_button.setText("繼續縮圖載入" if paused else "暫停縮圖載入")
            self.preview_button.setToolTip("控制背景影格產生；已完成的縮圖仍可觀看")
            self.preview_button.setEnabled(self.root is not None)

    def _set_library_task(self, message: str, counts: tuple[int, int] | None,
                          *, idle: bool = False) -> None:
        self.library_task_label.setText(message)
        if counts is None:
            self.library_task_progress.setRange(0, 0)
            self.library_task_progress.setFormat("處理中")
        else:
            current, total = counts
            self.library_task_progress.setRange(0, max(1, total))
            self.library_task_progress.setValue(current)
            self.library_task_progress.setFormat("待命" if idle else "%p%")

    def _on_library_task_message(self, message: str) -> None:
        self.statusBar().showMessage(message)
        if message.startswith(("掃描", "已找到", "影格快取", "正在")):
            self.library_task_label.setText(message)

    def _on_import_bytes(self, index: int, total: int, copied: int, size: int) -> None:
        # The total bar advances continuously during cross-volume copies.
        scaled = (index - 1) * 1000 + round(1000 * copied / max(1, size))
        self._set_library_task(
            f"正在搬入影片：{index}/{total} 部 · 本部 {copied / 2**20:.0f}/{size / 2**20:.0f} MB",
            (scaled, max(1, total * 1000)),
        )

    def toggle_previews(self) -> None:
        if self.root is None or self.library_id is None:
            return
        paused = self.settings.preview_policy == "paused" or (
            self.settings.preview_policy == "auto" and self._large_library
        )
        self.settings = replace(self.settings, preview_policy="run" if paused else "paused")
        save_settings(self.paths.settings, self.settings)
        self._update_preview_button()
        if paused:
            self.statusBar().showMessage("正在繼續背景縮圖載入…")
            if self.worker and self.worker.isRunning():
                self._restart_previews_after_worker = True
                self.worker.requestInterruption()
            else:
                self.start_scan()
        elif self.worker and self.worker.isRunning():
            self._restart_previews_after_worker = False
            self.worker.requestInterruption()
            self.statusBar().showMessage("正在暫停縮圖載入；目前影格完成後停止…")
        else:
            self.statusBar().showMessage("縮圖載入已暫停；已完成的縮圖與影片清單仍可使用。")

    def start_scan(self) -> None:
        if self.root is None or self.library_id is None:
            self.statusBar().showMessage("影片庫離線，請先選擇正確資料夾。")
            return
        if (self.worker and self.worker.isRunning()) or (self.import_worker and self.import_worker.isRunning()) or (
            self.import_plan_worker and self.import_plan_worker.isRunning()
        ):
            return
        self.worker = IndexWorker(
            self.paths, self.root, self.library_id,
            PREVIEW_COUNTS[self.settings.preview_mode], self,
            preview_policy=self.settings.preview_policy,
            scan_folders=self.settings.scan_folders,
        )
        self.worker.set_paused(self.player is not None)
        self.worker.progress.connect(self._on_library_task_message)
        self.worker.progress_count.connect(self._on_index_progress)
        self.worker.synced.connect(self._on_synced)
        self.worker.outcome.connect(self._on_index_outcome)
        self.worker.failed.connect(lambda message: self.statusBar().showMessage(f"掃描或索引失敗：{message}"))
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()
        self._set_library_task("正在掃描影片庫…", None)
        self.index_progress.setFormat("掃描中…")
        self.index_progress.setRange(0, 0)
        self.index_progress.show()
        self._update_empty_state(self.model.rowCount())

    def _on_index_progress(self, current: int, total: int) -> None:
        self._set_library_task(f"正在產生縮圖：{current}/{total} 部", (current, max(total, 1)))
        self.index_progress.setRange(0, max(total, 1))
        self.index_progress.setValue(current)
        self.index_progress.setFormat(f"{current}/{total} 部縮圖載入")
        now = time.monotonic()
        if current and (now - self._last_preview_refresh >= 1 or current == total):
            self.model.refresh_preview_jobs(self.connection)
            self._last_preview_refresh = now

    def _on_synced(self, result: dict) -> None:
        self._large_library = result["total"] > 100
        self._update_preview_button()
        self.refresh_navigation()
        self.refresh_videos()
        self.statusBar().showMessage(
            f"掃描完成：{result['total']} 部；新增 {result['added']}、變更 {result['changed']}、待清理 {result['pending_cleanup']}。"
        )

    def _on_index_outcome(self, message: str) -> None:
        self.refresh_videos()
        self.statusBar().showMessage(message)

    def _on_worker_finished(self) -> None:
        self.worker = None
        self.index_progress.hide()
        self._set_library_task("搬入與縮圖：目前沒有工作", (0, 100), idle=True)
        self.model.refresh_preview_jobs(self.connection)
        self._update_empty_state(self.model.rowCount())
        if self._closing_after_worker:
            QTimer.singleShot(0, self.close)
        elif self._pending_recycle_ids is not None:
            ids = self._pending_recycle_ids
            self._pending_recycle_ids = None
            self._update_recycle_button()
            QTimer.singleShot(0, lambda: self._confirm_recycle(ids, resume_scan=True))
        elif self._pending_group_ids is not None:
            ids = self._pending_group_ids
            self._pending_group_ids = None
            QTimer.singleShot(0, lambda: self._confirm_group(ids, resume_scan=True))
        elif self._pending_import is not None:
            sources = self._pending_import
            group = self._pending_import_group
            self._pending_import = None
            self._pending_import_group = False
            QTimer.singleShot(0, lambda: self._begin_import(sources, group=group))
        elif self.rebuild_preview_after_worker:
            self.rebuild_preview_after_worker = False
            self._mark_all_thumbnails_pending()
            self.start_scan()
        elif self._restart_previews_after_worker:
            self._restart_previews_after_worker = False
            self.start_scan()

    def cancel_scan(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            self.statusBar().showMessage("正在取消背景工作…")
        if self.import_worker and self.import_worker.isRunning():
            self.import_worker.requestInterruption()
            self.statusBar().showMessage("正在取消搬入；目前檔案完成後停止…")
        if self.import_plan_worker and self.import_plan_worker.isRunning():
            self.import_plan_worker.requestInterruption()
            self.statusBar().showMessage("正在取消匯入規劃…")

    def import_files(self) -> None:
        if self.root is None or self.library_id is None:
            notify_information(self, "影片庫未連線", "請先選擇影片庫，再搬入 MP4。")
            return
        if (self.import_worker and self.import_worker.isRunning()) or (
            self.import_plan_worker and self.import_plan_worker.isRunning()
        ):
            notify_information(self, "正在搬入", "請先等目前的搬入工作完成。")
            return
        selected, _ = QFileDialog.getOpenFileNames(
            self, "選擇同組 MP4 分段" if self._import_group_mode else "選擇要分開搬入的 MP4",
            "", "MP4 影片 (*.mp4 *.MP4)",
        )
        if selected:
            if self._import_group_mode and len(selected) < 2:
                notify_information(self, "需要多個分段", "同組搬入至少需要兩個 MP4；可切回「分開搬入」匯入單一影片。")
                return
            self._begin_import([Path(path) for path in selected], group=self._import_group_mode)

    def import_folder(self) -> None:
        if self.root is None or self.library_id is None:
            notify_information(self, "影片庫未連線", "請先選擇影片庫，再搬入 MP4。")
            return
        if (self.import_worker and self.import_worker.isRunning()) or (
            self.import_plan_worker and self.import_plan_worker.isRunning()
        ):
            notify_information(self, "正在搬入", "請先等目前的搬入工作完成。")
            return
        selected = QFileDialog.getExistingDirectory(
            self, "選擇同組 MP4 所在的資料夾" if self._import_group_mode else "選擇包含 MP4 的來源資料夾",
        )
        if not selected:
            return
        self._begin_import(Path(selected), group=self._import_group_mode)

    def _set_import_mode(self, group: bool) -> None:
        self._import_group_mode = group
        self.group_mode_button.setChecked(group)
        self.separate_mode_button.setChecked(not group)
        self.import_button.setText("選分段 MP4" if group else "搬入 MP4")
        self.import_folder_button.setText("選資料夾" if group else "搬入資料夾")

    def import_group_files(self) -> None:
        self._set_import_mode(True)
        self.import_files()

    def import_group_folder(self) -> None:
        self._set_import_mode(True)
        self.import_folder()

    def dragEnterEvent(self, event) -> None:
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if urls and all(url.isLocalFile() for url in urls):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls()]
        self._handle_dropped_paths(paths)
        event.acceptProposedAction()

    def _handle_dropped_paths(self, paths: list[Path]) -> None:
        if len(paths) == 1 and paths[0].is_dir():
            self._begin_import(paths[0], group=self._import_group_mode)
        elif paths and all(path.suffix.lower() == ".mp4" and path.is_file() for path in paths):
            if self._import_group_mode and len(paths) < 2:
                notify_information(self, "需要多個分段", "同組搬入至少需要兩個 MP4；可切回「分開搬入」匯入單一影片。")
                return
            self._begin_import(paths, group=self._import_group_mode)
        else:
            notify_information(self, "拖入 MP4", "請拖入一個資料夾，或一個以上的 MP4 檔案。")
            return

    def _begin_import(self, sources: list[Path] | Path, *, group: bool = False) -> None:
        if self.root is None or self.library_id is None:
            notify_information(self, "影片庫未連線", "請先連接影片庫，再搬入 MP4。")
            return
        if self._pending_group_ids is not None or self._pending_recycle_ids is not None:
            notify_information(self, "請稍候", "請等目前的影片操作完成。")
            return
        if (self.import_worker and self.import_worker.isRunning()) or (
            self.import_plan_worker and self.import_plan_worker.isRunning()
        ):
            notify_information(self, "正在搬入", "請先等目前的搬入工作完成。")
            return
        if self.worker and self.worker.isRunning():
            self._pending_import = sources
            self._pending_import_group = group
            self.worker.requestInterruption()
            self.statusBar().showMessage("正在暫停索引，接著會顯示搬入確認…")
            return
        self._planned_items = None
        self._import_is_group = group
        self._group_import_items = None
        self._planning_error = None
        self.import_plan_worker = ImportPlanWorker(
            sources, self.root, self.library_id, self, group=group,
            remove_phrases=self.settings.import_remove_phrases,
        )
        self.import_plan_worker.progress.connect(self._on_library_task_message)
        self.import_plan_worker.planned.connect(self._on_import_planned)
        self.import_plan_worker.failed.connect(self._on_import_plan_failed)
        self.import_plan_worker.finished.connect(self._on_import_plan_finished)
        self.import_plan_worker.start()
        self._set_library_task("正在搜尋 MP4 並規劃搬入…", None)
        self.statusBar().showMessage("正在搜尋 MP4 並規劃目的地…")
        self._update_empty_state(self.model.rowCount())

    def _on_import_planned(self, items: list[ImportItem]) -> None:
        self._planned_items = items

    def _on_import_plan_failed(self, message: str) -> None:
        self._planning_error = message

    def _on_import_plan_finished(self) -> None:
        self.import_plan_worker = None
        self._set_library_task("搬入與縮圖：目前沒有工作", (0, 100), idle=True)
        self._update_empty_state(self.model.rowCount())
        if self._closing_after_worker:
            QTimer.singleShot(0, self.close)
            return
        if self._planning_error:
            QMessageBox.critical(self, "無法規劃搬入", self._planning_error)
            return
        if self._planned_items is None:
            self.statusBar().showMessage("已取消匯入規劃。")
            return
        items = self._planned_items
        self._planned_items = None
        if not items:
            notify_information(self, "沒有 MP4", "所選位置沒有可搬入的 MP4。")
            return
        if self._import_is_group:
            dialog = GroupOrderDialog([item.source for item in items], self)
            if dialog.exec() != QDialog.Accepted:
                return
            try:
                items = plan_group_import(
                    self.root, self.library_id, dialog.ordered_paths(),
                    remove_phrases=self.settings.import_remove_phrases,
                )
            except Exception as exc:
                QMessageBox.critical(self, "影片組規劃失敗", str(exc))
                return
        self._confirm_import(items)

    def _confirm_import(self, items: list[ImportItem]) -> None:
        preview = "\n".join(f"{item.source}  →  {item.destination}" for item in items[:5])
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Question)
        message.setWindowTitle("確認搬入影片組" if self._import_is_group else "確認搬入 MP4")
        message.setText(
            f"將 {len(items)} 段 MP4 搬入同一個影片組資料夾：\n{items[0].destination.parent}"
            if self._import_is_group else
            f"將 {len(items)} 部 MP4 搬入影片庫。\n每個自動資料夾最多 50 部；完成後加入「待整理」。"
        )
        message.setInformativeText(f"前 {min(5, len(items))} 部的來源與目的地：\n{preview}\n\n跨磁碟搬入會先複製完整檔案，再將原檔移至 Windows 資源回收筒。")
        message.setDetailedText("\n".join(f"{item.source}  →  {item.destination}" for item in items))
        message.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        message.setDefaultButton(QMessageBox.No)
        if message.exec() != QMessageBox.Yes:
            return
        self._group_import_items = items if self._import_is_group else None
        self.import_worker = ImportWorker(items, self.root, self.library_id, self)
        self.import_worker.progress.connect(self._on_library_task_message)
        self.import_worker.bytes_progress.connect(self._on_import_bytes)
        self.import_worker.outcome.connect(self._on_import_outcome)
        self.import_worker.finished.connect(self._on_import_finished)
        self.import_worker.start()
        self._set_library_task(f"正在搬入影片：0/{len(items)} 部", (0, len(items)))
        self._update_empty_state(self.model.rowCount())

    def _on_import_outcome(self, result: dict) -> None:
        self._import_result = result

    def _on_import_finished(self) -> None:
        self.import_worker = None
        self._set_library_task("搬入完成，正在更新影片庫…", None)
        result = self._import_result or {"completed": [], "retained": [], "failed": [], "cancelled": False}
        self._import_result = None
        summary = f"已搬入 {len(result['completed'])} 部；失敗 {len(result['failed'])} 部。"
        if result["cancelled"]:
            summary += " 已取消剩餘工作。"
        if result["retained"]:
            summary += f"\n{len(result['retained'])} 部的來源原檔仍保留，請檢查後自行處理。"
        if result["failed"]:
            summary += "\n\n失敗項目：\n" + "\n".join(result["failed"][:10])
        if self._closing_after_worker:
            QTimer.singleShot(0, self.close)
            return
        if self._group_import_items and len(result["completed"]) >= 2 and self.root and self.library_id:
            try:
                ordered = register_imported_paths(
                    self.connection, self.library_id, self.root,
                    [Path(destination) for destination in result["completed"]],
                )
                folder = self._group_import_items[0].destination.parent
                if len(self._group_import_items) > MAX_FILES_PER_FOLDER:
                    folder = folder.parent
                create_video_group(
                    self.connection, self.library_id, self._group_import_items[0].destination.stem,
                    ordered, folder_rel_path=folder.relative_to(self.root).as_posix(),
                )
                summary += f"\n已建立影片組，共 {len(ordered)} 段。"
                self.refresh_navigation()
                self.refresh_videos()
            except Exception as exc:
                summary += f"\n影片已搬入，但建立影片組失敗：{exc}。可選取分段後使用「將所選影片建立為一組」。"
        self._group_import_items = None
        if result["completed"]:
            self.start_scan()
        else:
            self._set_library_task("搬入與縮圖：目前沒有工作", (0, 100), idle=True)
        self.statusBar().showMessage(summary.replace("\n", " "))
        notify_information(self, "搬入結果", summary)

    def cleanup_records(self) -> None:
        if self.root is None or self.library_id is None or self._background_busy():
            notify_information(self, "無法清理", "請確認影片庫已連線，且背景工作已完成。")
            return
        if QMessageBox.question(
            self, "確認清理資料庫記錄",
            "將先完整重新掃描，然後移除仍找不到的影片資料庫記錄與 C 槽快取。\n"
            "此操作不刪除原始 MP4；若影片曾在檔案總管被移走，標籤和備註將無法自動復原。繼續？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self.worker = IndexWorker(
            self.paths, self.root, self.library_id,
            PREVIEW_COUNTS[self.settings.preview_mode], self,
            cleanup_mode=True, preview_policy=self.settings.preview_policy,
            scan_folders=self.settings.scan_folders,
        )
        self.worker.progress.connect(self._on_library_task_message)
        self.worker.progress_count.connect(self._on_index_progress)
        self.worker.synced.connect(self._on_synced)
        self.worker.outcome.connect(self._on_index_outcome)
        self.worker.failed.connect(lambda message: self.statusBar().showMessage(f"清理失敗：{message}"))
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()
        self.index_progress.setFormat("掃描中…")
        self.index_progress.setRange(0, 0)
        self.index_progress.show()

    def play_group_from_start(self, group_id: str) -> None:
        first = self.connection.execute(
            """SELECT v.id FROM video_group_members m JOIN videos v ON v.id = m.video_id
               WHERE m.group_id = ? AND v.deleted_at IS NULL AND v.missing_since IS NULL
               ORDER BY m.position LIMIT 1""",
            (group_id,),
        ).fetchone()
        if first is None:
            notify_information(self, "影片組不可用", "這組目前沒有可播放的分段。")
            return
        self.open_player(first[0])

    def open_player(self, video_id: str) -> None:
        if self.root is None or self.library_id is None:
            notify_information(self, "影片庫離線", "重新連接影片庫後才能播放 MP4。")
            return
        marker = read_marker(self.root)
        if marker is None or marker.library_id != self.library_id:
            QMessageBox.warning(self, "影片庫識別不符", "請重新選擇正確的影片庫。")
            return
        row = repo.video_by_id(self.connection, video_id)
        if row is None or row["missing_since"]:
            notify_information(self, "影片不可用", "此影片目前離線或待清理。")
            return
        source = self.root / row["relative_path"]
        if not source.is_file():
            QMessageBox.warning(self, "找不到影片", "原始 MP4 已不在索引路徑。請重新掃描。")
            return
        if self.worker:
            self.worker.set_paused(True)
        group = group_for_video(self.connection, video_id)
        segments = [(member["id"], self.root / member["relative_path"])
                    for member in self.connection.execute(
                        """SELECT v.id, v.relative_path FROM video_group_members m
                           JOIN videos v ON v.id = m.video_id WHERE m.group_id = ? ORDER BY m.position""",
                        (group["id"],),
                    )] if group else None
        self._show_player(video_id, source, segments=segments)

    def open_media_preview(self, video_id: str, path: str) -> None:
        source = Path(path)
        if not source.is_file():
            QMessageBox.warning(self, "找不到影片", "檔案已不在原位置。")
            return
        self._show_player(video_id, source, read_only=True)

    def _show_player(self, video_id: str, source: Path, *,
                     segments: list[tuple[str, Path]] | None = None,
                     read_only: bool = False) -> None:
        if self.player is not None:
            self.player.close()
            if self.player is not None:
                return
        self._player_return_widget = self.main_stack.currentWidget()
        self.browser.stop_hover()
        player = PlayerView(self.connection, video_id, source, self,
                            segments=segments, embedded=True, read_only=read_only,
                            default_backend=self.settings.playback_backend)
        self.player = player
        player.compression_requested.connect(lambda identifier: self.open_compression_center([identifier]))
        player.cut_requested.connect(lambda identifier: self.open_edit_center([identifier]))
        player.metadata_saved.connect(self.after_metadata_saved)
        player.playback_closed.connect(self._player_closed)
        self.main_stack.addWidget(player)
        self.main_stack.setCurrentWidget(player)
        self._player_chrome_visibility = (
            self.menuBar().isVisible(), self.statusBar().isVisible(),
        )
        self.menuBar().hide()
        self.statusBar().hide()

    def _player_closed(self) -> None:
        if self.worker:
            self.worker.set_paused(False)
        if self.player is not None:
            self.main_stack.removeWidget(self.player)
        self.player = None
        target = self._player_return_widget
        self._player_return_widget = None
        if target is not None and self.main_stack.indexOf(target) >= 0:
            self.main_stack.setCurrentWidget(target)
        else:
            self.show_library_page()
        if self._player_chrome_visibility is not None:
            menu_visible, status_visible = self._player_chrome_visibility
            self.menuBar().setVisible(menu_visible)
            self.statusBar().setVisible(status_visible)
            self._player_chrome_visibility = None
        self.show_selected_details()

    def manage_metadata(self) -> None:
        MetadataDialog(self.connection, self, root=self.root, library_id=self.library_id).exec()
        self.refresh_navigation()
        self.show_selected_details()
        self.refresh_videos()

    def show_filters(self) -> None:
        dialog = FilterDialog(self.connection, self.advanced_filters, self)
        if dialog.exec():
            try:
                self.advanced_filters = dialog.filters()
                self.refresh_videos()
            except ValueError as exc:
                QMessageBox.warning(self, "篩選值錯誤", str(exc))

    def show_statistics(self) -> None:
        if self.library_id is None:
            notify_information(self, "尚未設定影片庫", "請先設定影片庫位置。")
            return
        StatsDialog(self.connection, self.library_id, self).exec()

    def clear_filters(self) -> None:
        self.advanced_filters = {}
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.refresh_videos()

    def save_smart_filter(self) -> None:
        name, ok = QInputDialog.getText(self, "儲存目前篩選", "篩選名稱")
        if ok and name.strip():
            try:
                repo.save_filter(self.connection, name, self._effective_filters())
                self.refresh_navigation()
            except sqlite3.Error as exc:
                QMessageBox.critical(self, "儲存失敗", str(exc))

    def toggle_list(self) -> None:
        list_mode = self.browser.viewMode() != QListView.ListMode
        self.browser.set_list_mode(list_mode)
        self.view_button.setText("切換成網格" if list_mode else "切換成清單")
        self.statusBar().showMessage("目前為清單檢視" if list_mode else "目前為網格檢視", 3000)

    def show_settings(self) -> None:
        if (self.import_worker and self.import_worker.isRunning()) or (
            self.import_plan_worker and self.import_plan_worker.isRunning()
        ):
            notify_information(self, "請稍候", "請等待搬入工作完成後再變更設定。")
            return
        dialog = SettingsDialog(self.settings, self)
        if not dialog.exec():
            return
        updated = dialog.updated(self.settings)
        mode_changed = updated.preview_mode != self.settings.preview_mode
        save_settings(self.paths.settings, updated)
        self.settings = updated
        self.model.set_preview_mode(updated.preview_mode)
        self.browser.set_timing(updated.hover_delay_ms, updated.frame_interval_ms)
        self.browser.set_thumbnail_width(updated.thumbnail_width)
        if dialog.scan_existing_requested:
            self.scan_existing_video_names()
        if mode_changed and not dialog.reset_library_requested:
            if self.worker and self.worker.isRunning():
                self.rebuild_preview_after_worker = True
                self.worker.requestInterruption()
            else:
                self._mark_all_thumbnails_pending()
                self.start_scan()
        if dialog.choose_library_requested:
            self.choose_library()
        if dialog.reset_library_requested:
            self.reset_library_root()

    def show_import_name_rules(self) -> None:
        if (self.import_worker and self.import_worker.isRunning()) or (
            self.import_plan_worker and self.import_plan_worker.isRunning()
        ):
            notify_information(self, "請稍候", "請等待目前的搬入工作完成後再修改檔名清理規則。")
            return
        dialog = ImportNameRulesDialog(self.settings.import_remove_phrases, self)
        if dialog.exec() != QDialog.Accepted:
            return
        updated = replace(self.settings, import_remove_phrases=dialog.phrases())
        save_settings(self.paths.settings, updated)
        self.settings = updated
        self.statusBar().showMessage(f"已儲存 {len(updated.import_remove_phrases)} 條匯入檔名清理規則。", 5000)
        if dialog.scan_existing_requested:
            self.scan_existing_video_names()

    def scan_existing_video_names(self) -> None:
        if self.root is None or self.library_id is None:
            notify_information(self, "影片庫未連線", "請先連接影片庫，再掃描現有 MP4。")
            return
        if self._background_busy() or self.player is not None:
            notify_information(self, "請稍候", "請先完成或取消背景工作，並關閉播放器。")
            return
        if not self.settings.import_remove_phrases:
            notify_information(self, "尚無清理規則", "請先加入要從檔名移除的字句。")
            return
        total = self.connection.execute(
            """SELECT COUNT(*) FROM videos WHERE library_id = ?
               AND deleted_at IS NULL AND missing_since IS NULL""",
            (self.library_id,),
        ).fetchone()[0]
        scan_progress = QProgressDialog("正在檢查已建檔的 MP4…", "停止", 0, total, self)
        scan_progress.setWindowModality(Qt.WindowModal)
        scan_progress.setMinimumDuration(0)
        scan_progress.show()

        def update_scan(done: int, _total: int) -> None:
            scan_progress.setValue(done)
            QApplication.processEvents()

        try:
            scan = scan_existing_names(
                self.connection, self.library_id, self.root, self.settings.import_remove_phrases,
                cancelled=scan_progress.wasCanceled, progress=update_scan,
            )
        except Exception as exc:
            scan_progress.close()
            QMessageBox.critical(self, "掃描失敗", str(exc))
            return
        stopped = scan_progress.wasCanceled()
        scan_progress.close()
        if stopped:
            self.statusBar().showMessage("已停止檔名掃描，沒有改動影片。", 5000)
            return
        if not scan.changes and not scan.group_changes and not scan.skipped:
            notify_information(self, "掃描完成", "現有影片和影片組名稱都不包含設定的清理字句。")
            return
        if ExistingNameDialog(scan, self).exec() != QDialog.Accepted:
            return
        progress = QProgressDialog("正在改名現有影片…", "停止", 0,
                                   len(scan.changes) + len(scan.group_changes), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        def update_progress(done: int, _total: int) -> None:
            progress.setValue(done)
            QApplication.processEvents()

        success, groups_renamed, failures = apply_existing_name_changes(
            self.connection, self.library_id, self.root, scan.changes,
            group_changes=scan.group_changes,
            cancelled=progress.wasCanceled, progress=update_progress,
        )
        stopped = progress.wasCanceled()
        progress.close()
        self.refresh_navigation()
        self.refresh_videos()
        self.details.set_video(self.selected_video_id())
        summary = (f"已改名 {success} 部 MP4、{groups_renamed} 個影片組；"
                   f"掃描時略過 {len(scan.skipped)} 部；執行失敗 {len(failures)} 項。")
        if stopped:
            summary += " 已停止剩餘工作。"
        if failures:
            summary += "\n\n失敗項目：\n" + "\n".join(failures[:8])
        self.statusBar().showMessage(summary.replace("\n", " "), 10000)
        notify_information(self, "現有影片改名結果", summary)

    def _mark_all_thumbnails_pending(self) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE thumbnail_jobs SET state = 'pending', attempts = 0,
                   cache_signature = NULL, error_message = NULL"""
            )
        self.refresh_videos()

    def retry_selected(self) -> None:
        video_id = self.selected_video_id()
        if video_id:
            self._retry_thumbnails([video_id])

    def _retry_thumbnails(self, video_ids: list[str]) -> None:
        for video_id in video_ids:
            retry_thumbnail(self.connection, video_id)
        if self.worker and self.worker.isRunning():
            self._restart_previews_after_worker = True
        else:
            self.start_scan()
        if self.settings.preview_policy == "paused" or (
            self.settings.preview_policy == "auto" and self._large_library
        ):
            self.statusBar().showMessage(
                f"已排入 {len(video_ids)} 部影格重試；按「開始預覽」執行。", 7000
            )

    def show_cache_size(self) -> None:
        total = sum(path.stat().st_size for path in self.paths.cache.rglob("*.webp") if path.is_file())
        notify_information(self, "快取占用空間", f"影格快取約 {total / 1024**3:.2f} GB。")

    def clear_cache(self) -> None:
        if self._background_busy():
            notify_information(self, "請稍候", "請先取消背景工作，再清除快取。")
            return
        if QMessageBox.question(self, "清除快取", "刪除 C 槽影格快取並在下次索引時重建？不會刪除 MP4。") != QMessageBox.Yes:
            return
        if self.paths.cache.is_dir():
            shutil.rmtree(self.paths.cache)
        self.paths.cache.mkdir(parents=True, exist_ok=True)
        self._mark_all_thumbnails_pending()
        self.start_scan()

    def export_database(self) -> None:
        selected, _ = QFileDialog.getSaveFileName(self, "匯出資料庫備份", "videomanager-backup.db", "SQLite (*.db)")
        if selected:
            try:
                export_backup(self.paths, Path(selected))
                notify_information(self, "備份完成", selected)
            except Exception as exc:
                QMessageBox.critical(self, "備份失敗", str(exc))

    def restore_database(self) -> None:
        if self._background_busy() or self.player is not None:
            notify_information(self, "請稍候", "請先結束背景工作與播放器，再還原備份。")
            return
        selected, _ = QFileDialog.getOpenFileName(self, "選擇資料庫備份", "", "SQLite (*.db)")
        if not selected:
            return
        if QMessageBox.question(
            self, "確認還原",
            "目前資料庫會先備份到 C 槽，再用所選備份還原。確認繼續？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self.connection.close()
        try:
            previous = restore_backup(self.paths, Path(selected))
            notify_information(self, "還原完成", f"還原完成。舊資料庫備份：{previous}")
        except Exception as exc:
            QMessageBox.critical(self, "還原失敗", str(exc))
        finally:
            self.connection = open_database(self.paths.database, self.paths.backups)
            self.details.connection = self.connection
            self.details.set_library(self.root, self.library_id)
            self.refresh_navigation()
            self.refresh_videos()

    def batch_relations(self) -> None:
        ids = expand_group_ids(self.connection, [index.data(Qt.UserRole) for index in self.browser.selectedIndexes()])
        if not ids:
            notify_information(self, "請先選取影片", "可按 Ctrl 或 Shift 多選。")
            return
        rows = repo.list_tags(self.connection)
        labels = [f"{row['group_name']} › {row['name']}" for row in rows]
        if not labels:
            notify_information(self, "尚無可用標籤", "請先新增標籤。")
            return
        label, ok = QInputDialog.getItem(self, "批次標記", "選擇", labels, 0, False)
        if not ok:
            return
        operation, ok = QInputDialog.getItem(self, "批次標記", "操作", ["加入", "移除"], 0, False)
        if not ok:
            return
        selected_id = rows[labels.index(label)]["id"]
        try:
            with self.connection:
                if operation == "加入":
                    self.connection.executemany(
                        "INSERT OR IGNORE INTO video_tags(video_id, tag_id) VALUES (?, ?)",
                        ((video_id, selected_id) for video_id in ids),
                    )
                else:
                    self.connection.executemany(
                        "DELETE FROM video_tags WHERE video_id = ? AND tag_id = ?",
                        ((video_id, selected_id) for video_id in ids),
                    )
            self.show_selected_details()
        except sqlite3.Error as exc:
            QMessageBox.critical(self, "批次標記失敗", str(exc))

    def group_selected(self) -> None:
        ids = self._selected_video_ids()
        if len(ids) < 2:
            notify_information(self, "選取分段", "請按 Ctrl 或 Shift 選取至少兩部 MP4。")
            return
        if self.root is None or self.library_id is None:
            notify_information(self, "影片庫未連線", "請先連接影片庫，再建立影片組。")
            return
        if (self.import_worker and self.import_worker.isRunning()) or (
            self.import_plan_worker and self.import_plan_worker.isRunning()
        ) or self._pending_import is not None or self._pending_recycle_ids is not None:
            notify_information(self, "請稍候", "請等目前的影片操作完成。")
            return
        if self.worker and self.worker.isRunning():
            self._pending_group_ids = ids
            self.worker.requestInterruption()
            self.statusBar().showMessage("正在暫停背景縮圖載入，接著確認影片組順序…")
            return
        self._confirm_group(ids)

    def _confirm_group(self, ids: list[str], *, resume_scan: bool = False) -> None:
        try:
            if self.root is None or self.library_id is None:
                return
            rows = [repo.video_by_id(self.connection, video_id) for video_id in ids]
            if any(row is None or row["missing_since"] is not None or row["deleted_at"] is not None for row in rows):
                notify_information(self, "影片不可用", "所選分段已不在影片庫，請重新選取。")
                return
            paths = [self.root / row["relative_path"] for row in rows]
            dialog = GroupOrderDialog(paths, self)
            if dialog.exec() != QDialog.Accepted:
                return
            by_path = {str(path): video_id for path, video_id in zip(paths, ids)}
            ordered = [by_path[str(path)] for path in dialog.ordered_paths()]
            folder, destinations = plan_group_folder(self.connection, self.library_id, self.root, ordered)
            preview = "\n".join(f"{self.root / repo.video_by_id(self.connection, video_id)['relative_path']} → {target}"
                                for video_id, target in destinations)
            if QMessageBox.question(
                self, "確認建立影片組", f"將 {len(ordered)} 段搬進資料夾：\n{folder}\n\n{preview}\n\n確認搬移？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return
            create_group_in_folder(
                self.connection, self.library_id, self.root, paths[ids.index(ordered[0])].stem,
                ordered, expected_folder=folder,
            )
            self.refresh_navigation()
            self.refresh_videos()
            self.statusBar().showMessage(f"已建立影片組，共 {len(ordered)} 段。", 5000)
        except Exception as exc:
            QMessageBox.critical(self, "建立影片組失敗", f"{exc}\n若已搬動部分檔案，可重新掃描修復。")
        finally:
            if resume_scan and not self._closing_after_worker and self.root is not None:
                QTimer.singleShot(0, self.start_scan)

    def move_selected(self) -> None:
        video_id = self.selected_video_id()
        if not video_id or self.root is None or self.library_id is None or self._background_busy():
            notify_information(self, "不可操作", "請選擇已連線的影片。")
            return
        row = repo.video_by_id(self.connection, video_id)
        destination, ok = QInputDialog.getText(self, "改名或移動影片", "新相對路徑（限影片庫內）", text=row["relative_path"])
        if not ok or not destination.strip():
            return
        group = group_for_video(self.connection, video_id)
        if group and Path(destination.strip()).parent.as_posix() != Path(row["relative_path"]).parent.as_posix():
            notify_information(self, "影片組位置", "同組分段需留在共同資料夾。可在這裡改檔名。")
            return
        if QMessageBox.question(
            self, "確認檔案操作",
            f"來源：{self.root / row['relative_path']}\n目的：{self.root / destination}\n\n確定移動原始 MP4？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            move_video(self.connection, self.library_id, self.root, video_id, destination.strip())
            self.refresh_videos()
        except Exception as exc:
            QMessageBox.critical(self, "檔案操作失敗", str(exc))

    def create_folder(self) -> None:
        if self.root is None or self.library_id is None or self._background_busy():
            notify_information(self, "影片庫離線", "重新連接後才能建立資料夾。")
            return
        relative, ok = QInputDialog.getText(self, "建立資料夾", "影片庫內的相對路徑")
        if ok and relative.strip():
            if QMessageBox.question(self, "確認建立", f"建立：{self.root / relative}") != QMessageBox.Yes:
                return
            try:
                create_library_folder(self.root, self.library_id, relative.strip())
            except Exception as exc:
                QMessageBox.critical(self, "建立失敗", str(exc))

    def recycle_selected(self) -> None:
        self._recycle_videos(self._selected_video_ids())

    def _recycle_videos(self, ids: list[str]) -> None:
        if not ids or self.root is None or self.library_id is None:
            notify_information(self, "不可操作", "請先選取已連線的影片。")
            return
        if (self.import_worker and self.import_worker.isRunning()) or (
            self.import_plan_worker and self.import_plan_worker.isRunning()
        ) or self._pending_import is not None:
            notify_information(self, "正在搬入", "請等目前的搬入工作完成後再刪除。")
            return
        if self.worker and self.worker.isRunning():
            self._pending_recycle_ids = ids
            self._update_recycle_button()
            self.worker.requestInterruption()
            self.statusBar().showMessage("正在暫停背景索引，接著確認刪除選取影片…")
            return
        self._confirm_recycle(ids)

    def _recycle_block_reason(self, video_id: str) -> str | None:
        for center in (self.compression_center, self.edit_center):
            if center is None:
                continue
            for worker in (center.analysis_worker, center.worker, center.replacement_worker):
                if worker is None:
                    continue
                row = compression_jobs.get_job(self.connection, worker.job_id)
                if row is not None and row["video_id"] == video_id:
                    return "壓縮或試壓縮正在處理"
            if center.cut_worker is not None:
                row = self.connection.execute(
                    "SELECT video_id FROM video_edits WHERE id=?", (center.cut_worker.edit_id,)
                ).fetchone()
                if row is not None and row["video_id"] == video_id:
                    return "剪輯正在處理"
        if self.connection.execute(
            "SELECT 1 FROM compression_jobs WHERE video_id=? AND status IN ('analyzing','encoding','validating') LIMIT 1",
            (video_id,),
        ).fetchone():
            return "壓縮或試壓縮正在處理"
        if self.connection.execute(
            "SELECT 1 FROM video_edits WHERE video_id=? AND status IN ('running','validating') LIMIT 1",
            (video_id,),
        ).fetchone():
            return "剪輯正在處理"
        return None

    def _cancel_waiting_video_work(self, video_id: str) -> None:
        waiting = self.connection.execute(
            "SELECT id FROM compression_jobs WHERE video_id=? AND status IN ('queued','analyzed')",
            (video_id,),
        ).fetchall()
        for row in waiting:
            compression_jobs.cancel_waiting_job(self.connection, self.root, row["id"])
        with self.connection:
            self.connection.execute(
                "UPDATE video_edits SET status='cancelled' WHERE video_id=? AND status='queued'",
                (video_id,),
            )
        if self.compression_center is not None:
            self.compression_center.refresh_jobs()
        if self.edit_center is not None:
            self.edit_center.refresh_edits()

    def _confirm_recycle(self, ids: list[str], *, resume_scan: bool = False) -> None:
        try:
            if self.root is None or self.library_id is None:
                return
            allowed: list[tuple[str, sqlite3.Row]] = []
            restricted: list[str] = []
            for video_id in ids:
                row = repo.video_by_id(self.connection, video_id)
                name = row["display_title"] if row is not None else video_id
                reason = self._recycle_block_reason(video_id)
                if row is None or row["missing_since"] is not None or row["deleted_at"] is not None:
                    reason = "影片已不在可操作的影片庫"
                if reason:
                    restricted.append(f"{name}：{reason}")
                else:
                    allowed.append((video_id, row))
            if restricted:
                QMessageBox.warning(self, "部分影片無法刪除",
                                    "以下影片仍在處理或不可用，已略過：\n" + "\n".join(restricted[:20]))
            if not allowed:
                return
            paths = [self.root / row["relative_path"] for _, row in allowed]
            allowed_ids = [video_id for video_id, _ in allowed]
            grouped = sum(bool(group_for_video(self.connection, video_id)) for video_id in allowed_ids)
            group_ids = {group["id"] for video_id in allowed_ids
                         if (group := group_for_video(self.connection, video_id)) is not None}
            whole_group = False
            if len(group_ids) == 1:
                group_id = next(iter(group_ids))
                active_group_ids = {member[0] for member in self.connection.execute(
                    """SELECT v.id FROM video_group_members m JOIN videos v ON v.id=m.video_id
                       WHERE m.group_id=? AND v.deleted_at IS NULL""", (group_id,),
                )}
                whole_group = active_group_ids == set(allowed_ids)
            preview = "\n".join(str(path) for path in paths[:5])
            if len(paths) > 5:
                preview += f"\n…另有 {len(paths) - 5} 部（展開詳細資訊可查看全部）"
            message = QMessageBox(self)
            message.setIcon(QMessageBox.Question)
            message.setWindowTitle("確認刪除整組影片" if whole_group else "確認刪除選取影片")
            message.setText(f"將{'整組' if whole_group else '選取的'} {len(allowed_ids)} 部 MP4 移至程式回收桶？")
            message.setInformativeText(
                f"{preview}\n\n影片會保留在影片庫同一磁碟，可在程式回收桶播放或還原。"
                + ("還原所有分段後，影片組也會重新出現。" if whole_group else
                   f"其中 {grouped} 段屬於影片組，未選取的分段不會移除。" if grouped else "")
            )
            message.setDetailedText("\n".join(str(path) for path in paths))
            message.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            message.setDefaultButton(QMessageBox.No)
            if message.exec() != QMessageBox.Yes:
                return
            completed = 0
            failed: list[str] = []
            for video_id, path in zip(allowed_ids, paths):
                try:
                    reason = self._recycle_block_reason(video_id)
                    if reason:
                        raise RuntimeError(reason)
                    self._cancel_waiting_video_work(video_id)
                    trash_video(self.connection, self.library_id, self.root, video_id)
                    completed += 1
                except Exception as exc:
                    failed.append(f"{path}: {exc}")
            self.refresh_navigation()
            self.refresh_videos()
            self.trash_page.refresh()
            self.statusBar().showMessage(f"已移至程式回收桶 {completed} 部；失敗 {len(failed)} 部。")
            if failed:
                QMessageBox.warning(self, "部分影片未刪除", "\n".join(failed[:10]))
        finally:
            if resume_scan and not self._closing_after_worker and self.root is not None:
                QTimer.singleShot(0, self.start_scan)

    def closeEvent(self, event) -> None:
        for attribute in ("compression_center", "edit_center"):
            center = getattr(self, attribute)
            if center is None:
                continue
            if center.has_active_work():
                if not self._closing_after_worker:
                    answer = QMessageBox.question(
                        self, "壓縮工作進行中",
                        "關閉程式會停下目前的工作。正式壓縮已完成的片段會保留，"
                        "下次可在壓縮中心的歷史紀錄接續；正在處理的片段會重做。"
                        "試壓縮與剪輯會取消。原片不受影響。要繼續關閉嗎？",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                    )
                    if answer != QMessageBox.Yes:
                        event.ignore()
                        return
                    self._closing_after_worker = True
                if not center._closing:
                    center.stop_for_app_exit()
                event.ignore()
                self.statusBar().showMessage("正在安全結束影片工作…")
                QTimer.singleShot(300, self.close)
                return
            if center.cap_thread.isRunning():
                self._closing_after_worker = True
                event.ignore()
                self.statusBar().showMessage("正在關閉影片工具…")
                QTimer.singleShot(300, self.close)
                return
            center.close()
            self.main_stack.removeWidget(center)
            setattr(self, attribute, None)
        if self.import_plan_worker and self.import_plan_worker.isRunning():
            self._closing_after_worker = True
            self.import_plan_worker.requestInterruption()
            event.ignore()
            self.statusBar().showMessage("正在結束匯入規劃…")
            return
        if self.import_worker and self.import_worker.isRunning():
            self._closing_after_worker = True
            self.import_worker.requestInterruption()
            event.ignore()
            self.statusBar().showMessage("正在結束搬入工作…")
            return
        if self.worker and self.worker.isRunning():
            self._closing_after_worker = True
            self.worker.requestInterruption()
            event.ignore()
            self.statusBar().showMessage("正在結束背景工作…")
            return
        if self.player is not None and not self.player.close():
            event.ignore()
            return
        self.connection.close()
        super().closeEvent(event)
