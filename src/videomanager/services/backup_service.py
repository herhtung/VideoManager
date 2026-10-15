"""Daily and manual database snapshots through SQLite's backup API."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from videomanager.config import AppPaths, load_settings
from videomanager.db.database import backup_database, open_database


def daily_backup(paths: AppPaths, *, keep: int = 7) -> Path | None:
    if not paths.database.exists():
        return None
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    existing = list(paths.backups.glob(f"library-{today}*.db"))
    if existing:
        return existing[-1]
    with closing(open_database(paths.database, paths.backups)) as connection:
        saved = backup_database(connection, paths.backups)
    snapshots = sorted(paths.backups.glob("library-*.db"), key=lambda item: item.stat().st_mtime, reverse=True)
    for old in snapshots[keep:]:
        old.unlink()
    return saved


def export_backup(paths: AppPaths, destination: Path) -> Path:
    if destination.exists():
        raise FileExistsError("Backup destination already exists")
    with closing(open_database(paths.database, paths.backups)) as connection:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(destination)) as target:
            connection.backup(target)
    return destination


def restore_backup(paths: AppPaths, source: Path) -> Path | None:
    """Restore a validated snapshot. The caller must close all live DB connections."""
    source = source.resolve(strict=True)
    if source == paths.database.resolve(strict=False):
        raise ValueError("Cannot restore the active database onto itself")
    settings = load_settings(paths.settings)
    with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as snapshot:
        if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Backup failed SQLite integrity check")
        tables = {row[0] for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"libraries", "videos", "schema_migrations"}.issubset(tables):
            raise ValueError("Not a VideoManager backup")
        library_ids = [row[0] for row in snapshot.execute("SELECT id FROM libraries")]
        if settings.library_id and library_ids and library_ids != [settings.library_id]:
            raise ValueError("Backup belongs to a different library ID")
        previous = None
        if paths.database.exists():
            with closing(open_database(paths.database, paths.backups)) as current:
                previous = backup_database(current, paths.backups)
        paths.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(paths.database)) as destination:
            snapshot.backup(destination)
    with closing(open_database(paths.database, paths.backups)) as verified:
        if verified.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Restored database failed integrity check")
    return previous
