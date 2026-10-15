"""P0 library open flow. No scan or original-file operations occur here."""

from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import closing
from pathlib import Path

from videomanager.config import AppPaths, Settings, load_settings, save_settings
from videomanager.db.database import DatabaseError, backup_database, open_database, register_library, utc_now
from videomanager.services.library_locator import (
    LibraryMarker,
    MarkerError,
    create_marker,
    drive_letter_candidates,
    locate_library,
    read_marker,
)


@dataclass(frozen=True)
class OpenedLibrary:
    root: Path
    marker: LibraryMarker


def open_library(paths: AppPaths, root: Path, *, create: bool = False) -> OpenedLibrary:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise MarkerError("Library root must be a directory")
    marker = read_marker(root)
    if marker is None and not create:
        raise MarkerError("No .videomanager/library.json found")
    paths.ensure()
    connection = open_database(paths.database, paths.backups)
    try:
        if marker is None:
            if connection.execute("SELECT COUNT(*) FROM libraries").fetchone()[0]:
                raise DatabaseError("This database is already bound to another library")
            marker = create_marker(root)
        register_library(connection, marker, root)
    finally:
        connection.close()
    previous = load_settings(paths.settings)
    save_settings(paths.settings, Settings(
        library_id=marker.library_id,
        last_root_path=str(root),
        preview_mode=previous.preview_mode,
        preview_policy=previous.preview_policy,
        hover_delay_ms=previous.hover_delay_ms,
        frame_interval_ms=previous.frame_interval_ms,
        thumbnail_width=previous.thumbnail_width,
        scan_folders=previous.scan_folders,
        import_remove_phrases=previous.import_remove_phrases,
    ))
    return OpenedLibrary(root, marker)


def reopen_last_library(paths: AppPaths) -> OpenedLibrary | None:
    settings = load_settings(paths.settings)
    if settings.library_id is None or settings.last_root_path is None:
        return None
    root = locate_library(
        settings.library_id,
        drive_letter_candidates(Path(settings.last_root_path)),
    )
    if root is None:
        return None
    return open_library(paths, root)


def reset_library_root(paths: AppPaths, old_root: Path, new_root: Path) -> tuple[OpenedLibrary, Path]:
    """Start a fresh managed library while preserving the old index in a backup.

    The source tree and its marker are never changed. Importing source videos is
    a separate, explicit operation after the new root is selected.
    """
    old_root = old_root.resolve(strict=True)
    new_root = new_root.resolve(strict=True)
    if not old_root.is_dir() or not new_root.is_dir():
        raise MarkerError("新舊影片庫根目錄都必須是資料夾。")
    if old_root == new_root or old_root in new_root.parents or new_root in old_root.parents:
        raise MarkerError("新根目錄不能是原根目錄，也不能位於原根目錄內或包含原根目錄。")
    if any(new_root.iterdir()):
        raise MarkerError("新根目錄必須是空資料夾。")
    previous = load_settings(paths.settings)
    old_marker = read_marker(old_root)
    if old_marker is None or old_marker.library_id != previous.library_id:
        raise MarkerError("原影片庫識別檔與目前設定不符。")
    with closing(open_database(paths.database, paths.backups)) as connection:
        rows = connection.execute("SELECT id FROM libraries").fetchall()
        if len(rows) != 1 or rows[0][0] != old_marker.library_id:
            raise DatabaseError("資料庫與原影片庫識別不符。")
        unfinished = connection.execute(
            "SELECT COUNT(*) FROM file_operations WHERE state IN ('pending', 'fs_done', 'needs_attention')"
        ).fetchone()[0]
        if unfinished:
            raise DatabaseError("尚有未完成的檔案操作，請先完成或修復後再更換根目錄。")
        backup = backup_database(connection, paths.backups)
        marker = create_marker(new_root)
        try:
            with connection:
                connection.execute("DELETE FROM libraries WHERE id = ?", (old_marker.library_id,))
                connection.execute(
                    """INSERT INTO libraries
                       (id, name, last_root_path, marker_version, last_verified_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (marker.library_id, new_root.name or str(new_root), str(new_root),
                     marker.schema_version, utc_now()),
                )
        except Exception:
            marker_file = new_root / ".videomanager" / "library.json"
            marker_file.unlink(missing_ok=True)
            marker_file.parent.rmdir()
            raise
    save_settings(paths.settings, replace(previous, library_id=marker.library_id,
                                          last_root_path=str(new_root), scan_folders=()))
    return OpenedLibrary(new_root, marker), backup
