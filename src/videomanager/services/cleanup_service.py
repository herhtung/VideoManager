"""Delete stale database records only after a fresh, verified complete scan."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import SyncResult, synchronize
from videomanager.db.database import backup_database


@dataclass(frozen=True)
class CleanupResult:
    scan: SyncResult
    deleted_records: int
    cache_errors: tuple[str, ...]


def cleanup_missing_records(
    connection: sqlite3.Connection,
    root: Path,
    library_id: str,
    cache_root: Path,
    *,
    immediate: bool = False,
    cancelled=lambda: False,
    scan_folders: tuple[str, ...] | None = None,
    backup_dir: Path | None = None,
) -> CleanupResult:
    """Never removes an MP4. `immediate` requires separate UI confirmation."""
    scanned = scan_library(root, library_id, included_folders=scan_folders, cancelled=cancelled)
    if cancelled():
        raise RuntimeError("Cleanup cancelled before synchronization")
    sync = synchronize(connection, library_id, root, scanned)
    deleted, errors = purge_confirmed_missing(
        connection, root, library_id, cache_root,
        {item.path_key for item in scanned}, immediate=immediate,
        cancelled=cancelled, backup_dir=backup_dir,
    )
    remaining = connection.execute(
        "SELECT COUNT(*) FROM videos WHERE library_id=? AND deleted_at IS NULL AND missing_since IS NOT NULL",
        (library_id,),
    ).fetchone()[0]
    return CleanupResult(replace(sync, pending_cleanup=remaining), deleted, errors)


def purge_confirmed_missing(
    connection: sqlite3.Connection,
    root: Path,
    library_id: str,
    cache_root: Path,
    present_keys: set[str],
    *,
    immediate: bool,
    cancelled=lambda: False,
    backup_dir: Path | None = None,
) -> tuple[int, tuple[str, ...]]:
    """Remove only verified absent index rows, never a file or an active job."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    candidates = connection.execute(
        """SELECT id, relative_path, path_key, missing_since FROM videos WHERE library_id = ?
           AND missing_since IS NOT NULL AND deleted_at IS NULL""",
        (library_id,),
    ).fetchall()
    eligible = []
    for row in candidates:
        if row["path_key"] in present_keys:
            continue
        # A selected scan folder may have been removed from the scan scope while
        # its video is still on disk. Never erase that video's metadata.
        if (root / row["relative_path"]).exists():
            continue
        busy = connection.execute(
            """SELECT 1 FROM compression_jobs WHERE video_id=? AND status IN
               ('queued','analyzing','analyzed','encoding','validating','completed')
               UNION SELECT 1 FROM video_edits WHERE video_id=? AND status IN
               ('queued','running','validating','completed')
               UNION SELECT 1 FROM file_operations WHERE video_id=? AND state IN
               ('pending','fs_done','needs_attention') LIMIT 1""",
            (row["id"], row["id"], row["id"]),
        ).fetchone()
        if busy:
            continue
        missing_time = datetime.fromisoformat(row["missing_since"])
        if immediate or missing_time <= cutoff:
            eligible.append(row["id"])
    if cancelled():
        raise RuntimeError("Cleanup cancelled before deletion")
    if eligible and backup_dir is not None:
        backup_database(connection, backup_dir)
    with connection:
        connection.executemany("DELETE FROM videos WHERE id = ?", ((identifier,) for identifier in eligible))
        connection.execute(
            """DELETE FROM video_groups WHERE library_id=? AND NOT EXISTS
               (SELECT 1 FROM video_group_members WHERE group_id=video_groups.id)""",
            (library_id,),
        )
    errors: list[str] = []
    for identifier in eligible:
        cache_dir = cache_root / identifier
        if cache_dir.is_dir() and not cache_dir.is_symlink():
            try:
                shutil.rmtree(cache_dir)
            except OSError as exc:
                errors.append(f"{identifier}: {exc}")
    return len(eligible), tuple(errors)
