"""Journaled, root-bound MP4 moves and renames. No overwrite or raw delete."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from uuid import uuid4

from videomanager.db.database import normalize_relative_path, utc_now
from videomanager.services.library_locator import read_marker


class FileOperationError(RuntimeError):
    pass


_UNCHANGED = object()


def _verified_root(root: Path, library_id: str) -> Path:
    marker = read_marker(root)
    if marker is None or marker.library_id != library_id:
        raise FileOperationError("Library marker is missing or does not match")
    return root.resolve(strict=True)


def _inside(root: Path, relative: str) -> tuple[Path, str, str]:
    try:
        normalized, key = normalize_relative_path(relative)
    except ValueError as exc:
        raise FileOperationError(str(exc)) from exc
    if not normalized.lower().endswith(".mp4"):
        raise FileOperationError("Only MP4 files may be moved")
    absolute = root / normalized
    for candidate in (absolute, *absolute.parents):
        if candidate == root.parent:
            break
        if candidate.is_symlink():
            raise FileOperationError("Symlink paths are not managed")
    if not absolute.resolve(strict=False).is_relative_to(root):
        raise FileOperationError("Path leaves the verified library root")
    return absolute, normalized, key


def move_video(
    connection: sqlite3.Connection,
    library_id: str,
    root: Path,
    video_id: str,
    destination_relative: str,
    *,
    unique_category_id: str | None | object = _UNCHANGED,
    update_default_title: bool = False,
) -> str:
    """The caller must show source/destination and obtain user confirmation first."""
    root = _verified_root(root, library_id)
    row = connection.execute(
        "SELECT * FROM videos WHERE id = ? AND library_id = ? AND missing_since IS NULL AND deleted_at IS NULL",
        (video_id, library_id),
    ).fetchone()
    if row is None:
        raise FileOperationError("Video is unavailable")
    source, source_relative, _ = _inside(root, row["relative_path"])
    destination, dest_relative, dest_key = _inside(root, destination_relative)
    if source_relative == dest_relative:
        raise FileOperationError("Source and destination are identical")
    if not source.is_file() or source.is_symlink():
        raise FileOperationError("Source file is missing or is a symlink")
    stat = source.stat()
    if stat.st_size != row["file_size"] or stat.st_mtime_ns != row["mtime_ns"]:
        raise FileOperationError("Source changed since last scan; scan again")
    if not destination.parent.is_dir():
        raise FileOperationError("Destination folder does not exist")
    if destination.exists():
        raise FileOperationError("Destination already exists; no file was overwritten")
    conflict = connection.execute(
        "SELECT id FROM videos WHERE library_id = ? AND path_key = ? AND id <> ?",
        (library_id, dest_key, video_id),
    ).fetchone()
    if conflict:
        raise FileOperationError("Destination path already belongs to another database record")
    set_unique_category = unique_category_id is not _UNCHANGED
    if set_unique_category and unique_category_id is not None and not connection.execute(
        "SELECT 1 FROM unique_categories WHERE id = ?", (unique_category_id,),
    ).fetchone():
        raise FileOperationError("Unique category does not exist")
    effective_category_id = row["unique_category_id"] if unique_category_id is _UNCHANGED else unique_category_id
    if effective_category_id is not None:
        category = connection.execute(
            "SELECT folder_name FROM unique_categories WHERE id = ?", (effective_category_id,),
        ).fetchone()
        if category is None:
            raise FileOperationError("Unique category does not exist")
        prefix = f"唯一分類/{category['folder_name']}/".casefold()
        if not dest_relative.casefold().startswith(prefix):
            raise FileOperationError("已指定唯一分類的影片必須留在該分類的資料夾內")
    elif set_unique_category and not dest_relative.casefold().startswith("匯入/".casefold()):
        raise FileOperationError("取消唯一分類時須搬回匯入資料夾")
    operation_id = str(uuid4())
    with connection:
        connection.execute(
            """INSERT INTO file_operations(id, video_id, type, src_rel_path, dst_rel_path,
               state, started_at, set_unique_category, target_unique_category_id)
               VALUES (?, ?, 'move', ?, ?, 'pending', ?, ?, ?)""",
            (operation_id, video_id, source_relative, dest_relative, utc_now(),
             int(set_unique_category), None if unique_category_id is _UNCHANGED else unique_category_id),
        )
    try:
        os.rename(source, destination)
    except OSError as exc:
        with connection:
            connection.execute(
                "UPDATE file_operations SET state = 'failed', finished_at = ?, error = ? WHERE id = ?",
                (utc_now(), str(exc), operation_id),
            )
        raise FileOperationError(f"File move failed: {exc}") from exc
    try:
        with connection:
            connection.execute("UPDATE file_operations SET state = 'fs_done' WHERE id = ?", (operation_id,))
        with connection:
            connection.execute(
                """UPDATE videos SET relative_path = ?, path_key = ?, file_name = ?,
                   display_title = CASE WHEN ? AND display_title = ? THEN ? ELSE display_title END,
                   unique_category_id = CASE WHEN ? THEN ? ELSE unique_category_id END WHERE id = ?""",
                (dest_relative, dest_key, destination.name, int(update_default_title),
                 Path(row["file_name"]).stem, destination.stem, int(set_unique_category),
                 None if unique_category_id is _UNCHANGED else unique_category_id, video_id),
            )
            connection.execute(
                "UPDATE file_operations SET state = 'done', finished_at = ? WHERE id = ?",
                (utc_now(), operation_id),
            )
    except sqlite3.Error as exc:
        raise FileOperationError(
            f"File moved but database update failed. Operation {operation_id} requires recovery: {exc}"
        ) from exc
    return operation_id


def recover_file_operations(connection: sqlite3.Connection, library_id: str, root: Path) -> list[str]:
    """Reconcile journal entries against files after a verified reconnect."""
    root = _verified_root(root, library_id)
    messages: list[str] = []
    rows = connection.execute(
        """SELECT fo.*, v.relative_path FROM file_operations fo
           JOIN videos v ON v.id = fo.video_id
           WHERE v.library_id = ? AND fo.state IN ('pending', 'fs_done') ORDER BY fo.started_at""",
        (library_id,),
    ).fetchall()
    for row in rows:
        source, _, _ = _inside(root, row["src_rel_path"])
        if row["type"] == "recycle":
            with connection:
                if source.exists():
                    connection.execute(
                        "UPDATE file_operations SET state = 'failed', finished_at = ?, error = 'Recycle did not occur' WHERE id = ?",
                        (utc_now(), row["id"]),
                    )
                    messages.append(f"Recycle did not occur: {row['src_rel_path']}")
                else:
                    connection.execute("UPDATE videos SET missing_since = COALESCE(missing_since, ?) WHERE id = ?", (utc_now(), row["video_id"]))
                    connection.execute(
                        "UPDATE file_operations SET state = 'done', finished_at = ? WHERE id = ?",
                        (utc_now(), row["id"]),
                    )
                    messages.append(f"Recovered recycle record: {row['src_rel_path']}")
            continue
        if row["type"] != "move":
            messages.append(f"Unknown operation needs manual review: {row['id']}")
            continue
        destination, dest_relative, dest_key = _inside(root, row["dst_rel_path"])
        if destination.is_file() and not source.exists():
            with connection:
                connection.execute(
                    """UPDATE videos SET relative_path = ?, path_key = ?, file_name = ?,
                       unique_category_id = CASE WHEN ? THEN ? ELSE unique_category_id END WHERE id = ?""",
                    (dest_relative, dest_key, destination.name, row["set_unique_category"],
                     row["target_unique_category_id"], row["video_id"]),
                )
                connection.execute(
                    "UPDATE file_operations SET state = 'done', finished_at = ?, error = NULL WHERE id = ?",
                    (utc_now(), row["id"]),
                )
            messages.append(f"Recovered move: {dest_relative}")
        elif source.is_file() and not destination.exists():
            with connection:
                connection.execute(
                    "UPDATE file_operations SET state = 'failed', finished_at = ?, error = 'Move did not occur' WHERE id = ?",
                    (utc_now(), row["id"]),
                )
            messages.append(f"Move did not occur: {row['src_rel_path']}")
        else:
            with connection:
                connection.execute(
                    "UPDATE file_operations SET state = 'needs_attention', error = 'Ambiguous file state' WHERE id = ?",
                    (row["id"],),
                )
            messages.append(f"Manual review required: {row['src_rel_path']}")
    return messages


def create_library_folder(root: Path, library_id: str, relative: str) -> Path:
    root = _verified_root(root, library_id)
    normalized, _ = normalize_relative_path(relative)
    destination = (root / normalized).resolve(strict=False)
    if not destination.is_relative_to(root):
        raise FileOperationError("Folder path leaves the verified root")
    if destination.exists():
        raise FileOperationError("Destination already exists")
    if not destination.parent.is_dir():
        raise FileOperationError("Parent folder does not exist")
    destination.mkdir()
    return destination


def recycle_video(
    connection: sqlite3.Connection,
    library_id: str,
    root: Path,
    video_id: str,
) -> str:
    """Send one verified MP4 to the Windows Recycle Bin; never fall back to unlink."""
    if os.name != "nt":
        raise FileOperationError("Recycle Bin action is Windows-only")
    try:
        from send2trash.win import send2trash
    except ImportError as exc:
        raise FileOperationError("Windows Recycle Bin support is unavailable") from exc
    if send2trash.__module__ != "send2trash.win.modern":
        raise FileOperationError("Modern Windows Recycle Bin backend is unavailable; no file was removed")
    root = _verified_root(root, library_id)
    row = connection.execute(
        "SELECT * FROM videos WHERE id = ? AND library_id = ? AND missing_since IS NULL AND deleted_at IS NULL",
        (video_id, library_id),
    ).fetchone()
    if row is None:
        raise FileOperationError("Video is unavailable")
    source, source_relative, _ = _inside(root, row["relative_path"])
    if not source.is_file() or source.is_symlink():
        raise FileOperationError("Source is unavailable or a symlink")
    stat = source.stat()
    if stat.st_size != row["file_size"] or stat.st_mtime_ns != row["mtime_ns"]:
        raise FileOperationError("Source changed since last scan; scan again")
    operation_id = str(uuid4())
    with connection:
        connection.execute(
            """INSERT INTO file_operations(id, video_id, type, src_rel_path,
               state, started_at) VALUES (?, ?, 'recycle', ?, 'pending', ?)""",
            (operation_id, video_id, source_relative, utc_now()),
        )
    try:
        send2trash(str(source))
        if source.exists():
            raise FileOperationError("Recycle Bin operation did not remove the source")
    except Exception as exc:
        with connection:
            connection.execute(
                "UPDATE file_operations SET state = ?, finished_at = ?, error = ? WHERE id = ?",
                ("pending" if not source.exists() else "failed", utc_now(), str(exc), operation_id),
            )
        raise FileOperationError(f"Recycle Bin operation failed: {exc}") from exc
    try:
        with connection:
            connection.execute("UPDATE videos SET missing_since = ? WHERE id = ?", (utc_now(), video_id))
            connection.execute(
                "UPDATE file_operations SET state = 'done', finished_at = ? WHERE id = ?",
                (utc_now(), operation_id),
            )
    except sqlite3.Error as exc:
        raise FileOperationError(
            f"File was recycled but database update failed. Operation {operation_id} requires recovery: {exc}"
        ) from exc
    return operation_id
