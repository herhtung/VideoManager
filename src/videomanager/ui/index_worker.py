"""Background scan and one-at-a-time media indexing."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from videomanager.config import AppPaths
from videomanager.db.database import open_database
from videomanager.services.cleanup_service import cleanup_missing_records
from videomanager.services.file_operations import recover_file_operations
from videomanager.services.index_service import process_pending_media, requeue_first_frame_failures
from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import synchronize


class IndexWorker(QThread):
    progress = Signal(str)
    progress_count = Signal(int, int)
    synced = Signal(object)
    outcome = Signal(str)
    failed = Signal(str)

    def __init__(self, paths: AppPaths, root: Path, library_id: str, frame_count: int, parent=None, *,
                 cleanup_mode: bool = False, preview_policy: str = "auto",
                 scan_folders: tuple[str, ...] = ()) -> None:
        super().__init__(parent)
        self.paths = paths
        self.root = root
        self.library_id = library_id
        self.frame_count = frame_count
        self.cleanup_mode = cleanup_mode
        self.preview_policy = preview_policy
        self.scan_folders = scan_folders
        self._paused = False

    def set_paused(self, value: bool) -> None:
        self._paused = value

    def run(self) -> None:
        connection = None
        try:
            connection = open_database(self.paths.database, self.paths.backups)
            recovered = recover_file_operations(connection, self.library_id, self.root)
            if recovered:
                self.progress.emit("；".join(recovered))
            if self.cleanup_mode:
                result = cleanup_missing_records(
                    connection, self.root, self.library_id, self.paths.cache,
                    immediate=True, cancelled=self.isInterruptionRequested,
                    scan_folders=self.scan_folders,
                )
                self.synced.emit(asdict(result.scan))
                self.outcome.emit(
                    f"待清理資料庫記錄：移除 {result.deleted_records} 筆；快取錯誤 {len(result.cache_errors)} 筆。"
                )
                return
            scanned = scan_library(
                self.root, self.library_id,
                included_folders=self.scan_folders,
                cancelled=self.isInterruptionRequested,
                paused=lambda: self._paused,
                progress=lambda count: self.progress.emit(f"掃描：找到 {count} 部 MP4"),
            )
            if self.isInterruptionRequested():
                return
            result = synchronize(connection, self.library_id, self.root, scanned)
            requeue_first_frame_failures(connection)
            self.synced.emit(asdict(result))
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            aged = connection.execute(
                "SELECT 1 FROM videos WHERE library_id = ? AND missing_since <= ? LIMIT 1",
                (self.library_id, cutoff),
            ).fetchone()
            if aged and not self.isInterruptionRequested():
                cleanup = cleanup_missing_records(
                    connection, self.root, self.library_id, self.paths.cache,
                    immediate=False, cancelled=self.isInterruptionRequested,
                    scan_folders=self.scan_folders,
                )
                self.synced.emit(asdict(cleanup.scan))
                self.progress.emit(f"逾期待清理記錄：移除 {cleanup.deleted_records} 筆。")
            if self.preview_policy == "paused" or (self.preview_policy == "auto" and result.total > 100):
                self.outcome.emit(
                    f"清單已建立：{result.total} 部。大量影格預覽已暫緩；可按「開始預覽」在背景繼續。"
                )
                return
            pending = connection.execute(
                "SELECT COUNT(*) FROM thumbnail_jobs WHERE state IN ('pending', 'running')"
            ).fetchone()[0]
            self.progress_count.emit(0, pending)
            done, failed = process_pending_media(
                connection, self.root, self.paths.cache,
                frame_count=self.frame_count,
                cancelled=self.isInterruptionRequested,
                paused=lambda: self._paused,
                progress=self._report_media_progress,
            )
            self.outcome.emit(f"索引完成：{done} 部成功，{failed} 部影格失敗。")
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if connection is not None:
                connection.close()

    def _report_media_progress(self, current: int, total: int, name: str) -> None:
        self.progress_count.emit(current, total)
        self.progress.emit(f"影格快取 {current}/{total}：{name}")
