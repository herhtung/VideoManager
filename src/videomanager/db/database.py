"""Versioned SQLite database with a backup before every upgrade."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from uuid import uuid4

from videomanager.services.library_locator import LibraryMarker


class DatabaseError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrations() -> list[tuple[int, str]]:
    directory = resources.files("videomanager.db").joinpath("migrations")
    migrations: list[tuple[int, str]] = []
    for file in directory.iterdir():
        if file.name.endswith(".sql"):
            version = int(file.name.split("_", 1)[0])
            migrations.append((version, file.read_text(encoding="utf-8")))
    migrations.sort()
    if [version for version, _ in migrations] != list(range(1, len(migrations) + 1)):
        raise DatabaseError("Migration versions must start at 1 and be consecutive")
    return migrations


def backup_database(connection: sqlite3.Connection, backup_dir: Path) -> Path:
    """Use SQLite's backup API so WAL data is included."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    name = f"library-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}.db"
    path = backup_dir / name
    try:
        with closing(sqlite3.connect(path)) as destination:
            connection.backup(destination)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def open_database(path: Path, backup_dir: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists() and path.stat().st_size > 0
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not row[0].startswith("sqlite_")
        }
        if tables and "schema_migrations" not in tables:
            raise DatabaseError("Existing database has no migration history; refusing to modify it")
        connection.execute("PRAGMA journal_mode = WAL")
        current = 0
        if "schema_migrations" in tables:
            current = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
        migrations = _migrations()
        if current > len(migrations):
            raise DatabaseError("Database schema is newer than this application")
        if current < len(migrations) and existed:
            backup_database(connection, backup_dir)
        for version, sql in migrations:
            if version <= current:
                continue
            script = (
                "BEGIN IMMEDIATE;\n"
                + sql
                + "\nINSERT INTO schema_migrations(version, applied_at) VALUES ("
                + str(version)
                + ", '"
                + utc_now()
                + "');\nCOMMIT;"
            )
            try:
                connection.executescript(script)
            except sqlite3.Error as exc:
                connection.rollback()
                raise DatabaseError(f"Migration {version} failed: {exc}") from exc
        return connection
    except Exception:
        connection.close()
        raise


def register_library(
    connection: sqlite3.Connection, marker: LibraryMarker, root: Path
) -> None:
    """Bind this V1 database to exactly one marker UUID."""
    existing = connection.execute("SELECT id FROM libraries").fetchall()
    if existing and any(row[0] != marker.library_id for row in existing):
        raise DatabaseError("Database belongs to another library ID")
    with connection:
        connection.execute(
            """INSERT INTO libraries
               (id, name, last_root_path, marker_version, last_verified_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   last_root_path = excluded.last_root_path,
                   marker_version = excluded.marker_version,
                   last_verified_at = excluded.last_verified_at""",
            (marker.library_id, root.name or str(root), str(root), marker.schema_version, utc_now()),
        )


def normalize_relative_path(path: str) -> tuple[str, str]:
    """Return a portable path and Windows case-insensitive uniqueness key."""
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    if not normalized or any(part in ("", ".", "..") for part in parts) or ":" in normalized:
        raise ValueError("Expected a relative path within the library")
    return normalized, normalized.casefold()
