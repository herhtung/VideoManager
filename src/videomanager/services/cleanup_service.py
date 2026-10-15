"""Delete stale database records only after a fresh, verified complete scan."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from videomanager.services.scanner import scan_library
from videomanager.services.sync_service import SyncResult, synchronize


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
) -> CleanupResult:
    """Never removes an MP4. `immediate` requires separate UI confirmation."""
    scanned = scan_library(root, library_id, included_folders=scan_folders, cancelled=cancelled)
    if cancelled():
        raise RuntimeError("Cleanup cancelled before synchronization")
    sync = synchronize(connection, library_id, root, scanned)
    present_keys = {item.path_key for item in scanned}
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    candidates = connection.execute(
        """SELECT id, path_key, missing_since FROM videos WHERE library_id = ?
           AND missing_since IS NOT NULL AND deleted_at IS NULL""",
        (library_id,),
    ).fetchall()
    eligible = []
    for row in candidates:
        if row["path_key"] in present_keys:
            continue
        missing_time = datetime.fromisoformat(row["missing_since"])
        if immediate or missing_time <= cutoff:
            eligible.append(row["id"])
    if cancelled():
        raise RuntimeError("Cleanup cancelled before deletion")
    with connection:
        connection.executemany("DELETE FROM videos WHERE id = ?", ((identifier,) for identifier in eligible))
    errors: list[str] = []
    for identifier in eligible:
        cache_dir = cache_root / identifier
        if cache_dir.is_dir() and not cache_dir.is_symlink():
            try:
                shutil.rmtree(cache_dir)
            except OSError as exc:
                errors.append(f"{identifier}: {exc}")
    return CleanupResult(sync, len(eligible), tuple(errors))
