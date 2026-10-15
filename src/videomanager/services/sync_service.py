"""Synchronize a complete scan without deleting library metadata."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from videomanager.db.database import normalize_relative_path, utc_now
from videomanager.services.library_locator import read_marker
from videomanager.services.scanner import ScannedVideo


@dataclass(frozen=True)
class SyncResult:
    total: int
    added: int
    existing: int
    changed: int
    restored: int
    newly_missing: int
    pending_cleanup: int


def register_imported_paths(connection: sqlite3.Connection, library_id: str, root: Path,
                            destinations: list[Path]) -> list[str]:
    """Add newly imported files without walking or marking the rest of the library missing."""
    root = root.resolve(strict=True)
    marker = read_marker(root)
    if marker is None or marker.library_id != library_id:
        raise ValueError("影片庫識別不符")
    prepared = []
    for destination in destinations:
        if destination.is_symlink() or not destination.is_file() or destination.suffix.lower() != ".mp4":
            raise ValueError(f"匯入完成的 MP4 不存在：{destination}")
        resolved = destination.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError("匯入檔案超出影片庫")
        relative, key = normalize_relative_path(str(resolved.relative_to(root)))
        stat = resolved.stat()
        prepared.append((relative, key, resolved.name, stat.st_size, stat.st_mtime_ns))
    now = utc_now()
    ids: list[str] = []
    with connection:
        for relative, key, name, size, mtime_ns in prepared:
            previous = connection.execute(
                "SELECT id, file_size, mtime_ns FROM videos WHERE library_id = ? AND path_key = ? AND deleted_at IS NULL",
                (library_id, key),
            ).fetchone()
            if previous is None:
                video_id = str(uuid4())
                connection.execute(
                    """INSERT INTO videos(id, library_id, relative_path, path_key, file_name,
                       display_title, file_size, mtime_ns, added_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (video_id, library_id, relative, key, name, Path(name).stem, size, mtime_ns, now),
                )
                connection.execute(
                    "INSERT INTO thumbnail_jobs(video_id, state, attempts, updated_at) VALUES (?, 'pending', 0, ?)",
                    (video_id, now),
                )
            else:
                video_id = previous["id"]
                changed = previous["file_size"] != size or previous["mtime_ns"] != mtime_ns
                connection.execute(
                    """UPDATE videos SET relative_path = ?, file_name = ?, file_size = ?, mtime_ns = ?,
                       missing_since = NULL WHERE id = ?""",
                    (relative, name, size, mtime_ns, video_id),
                )
                if changed:
                    connection.execute(
                        """INSERT INTO thumbnail_jobs(video_id, state, attempts, updated_at)
                           VALUES (?, 'pending', 0, ?)
                           ON CONFLICT(video_id) DO UPDATE SET state = 'pending', attempts = 0,
                           cache_signature = NULL, updated_at = excluded.updated_at, error_message = NULL""",
                        (video_id, now),
                    )
            ids.append(video_id)
    return ids


def synchronize(
    connection: sqlite3.Connection,
    library_id: str,
    root: Path,
    scanned: list[ScannedVideo],
) -> SyncResult:
    """Call only after scan_library returns successfully."""
    job_id = str(uuid4())
    started = utc_now()
    existing_rows = connection.execute(
        "SELECT id, path_key, relative_path, file_size, mtime_ns, missing_since FROM videos WHERE library_id = ? AND deleted_at IS NULL",
        (library_id,),
    ).fetchall()
    existing = {row["path_key"]: row for row in existing_rows}
    seen: set[str] = set()
    added = changed = restored = newly_missing = 0
    with connection:
        connection.execute(
            "INSERT INTO scan_jobs(id, library_id, started_at, state) VALUES (?, ?, ?, 'running')",
            (job_id, library_id, started),
        )
        for item in scanned:
            if item.path_key in seen:
                raise ValueError(f"Duplicate path key in scan: {item.path_key}")
            seen.add(item.path_key)
            previous = existing.get(item.path_key)
            if previous is None:
                video_id = str(uuid4())
                connection.execute(
                    """INSERT INTO videos(id, library_id, relative_path, path_key, file_name,
                       display_title, file_size, mtime_ns, added_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (video_id, library_id, item.relative_path, item.path_key, item.file_name,
                     Path(item.file_name).stem, item.file_size, item.mtime_ns, started),
                )
                connection.execute(
                    """INSERT INTO thumbnail_jobs(video_id, state, attempts, updated_at)
                       VALUES (?, 'pending', 0, ?)""",
                    (video_id, started),
                )
                added += 1
            else:
                was_changed = previous["file_size"] != item.file_size or previous["mtime_ns"] != item.mtime_ns
                if was_changed:
                    changed += 1
                if previous["missing_since"] is not None:
                    restored += 1
                connection.execute(
                    """UPDATE videos SET relative_path = ?, file_name = ?, file_size = ?,
                       mtime_ns = ?, missing_since = NULL,
                       duration_ms = CASE WHEN ? THEN NULL ELSE duration_ms END,
                       width = CASE WHEN ? THEN NULL ELSE width END,
                       height = CASE WHEN ? THEN NULL ELSE height END,
                       video_codec = CASE WHEN ? THEN NULL ELSE video_codec END,
                       audio_codec = CASE WHEN ? THEN NULL ELSE audio_codec END
                       WHERE id = ?""",
                    (item.relative_path, item.file_name, item.file_size, item.mtime_ns,
                     *([int(was_changed)] * 5), previous["id"]),
                )
                if was_changed:
                    connection.execute(
                        """INSERT INTO thumbnail_jobs(video_id, state, attempts, cache_signature, updated_at, error_message)
                           VALUES (?, 'pending', 0, NULL, ?, NULL)
                           ON CONFLICT(video_id) DO UPDATE SET state = 'pending', attempts = 0,
                           cache_signature = NULL, updated_at = excluded.updated_at, error_message = NULL""",
                        (previous["id"], started),
                    )
        for key, previous in existing.items():
            if key not in seen and previous["missing_since"] is None:
                connection.execute("UPDATE videos SET missing_since = ? WHERE id = ?", (started, previous["id"]))
                newly_missing += 1
        pending = connection.execute(
            "SELECT COUNT(*) FROM videos WHERE library_id = ? AND missing_since IS NOT NULL AND deleted_at IS NULL",
            (library_id,),
        ).fetchone()[0]
        result = SyncResult(len(scanned), added, len(scanned) - added, changed, restored, newly_missing, pending)
        connection.execute(
            "UPDATE scan_jobs SET finished_at = ?, state = 'done', result_json = ? WHERE id = ?",
            (utc_now(), json.dumps(asdict(result)), job_id),
        )
    return result
