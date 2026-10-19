"""Same-volume, journaled application trash for managed MP4 files."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from uuid import UUID

from videomanager.db.database import utc_now
from videomanager.services.file_operations import FileOperationError, _inside, _verified_root


def _paths(root: Path, row: sqlite3.Row) -> tuple[Path, Path]:
    original = _inside(root, row["original_rel_path"])[0]
    trashed = _inside(root, row["trash_rel_path"])[0]
    if not row["trash_rel_path"].startswith(".videomanager/trash/"):
        raise FileOperationError("回收區路徑不正確")
    return original, trashed


def list_trashed(connection: sqlite3.Connection, library_id: str | None) -> list[sqlite3.Row]:
    if not library_id:
        return []
    return connection.execute(
        """SELECT v.id, v.display_title, v.file_size, v.duration_ms, t.original_rel_path,
                  t.trash_rel_path, t.state, t.created_at
           FROM trash_entries t JOIN videos v ON v.id=t.video_id
           WHERE v.library_id=? ORDER BY t.created_at DESC""", (library_id,),
    ).fetchall()


def trash_video(connection: sqlite3.Connection, library_id: str, root: Path, video_id: str) -> Path:
    root = _verified_root(root, library_id)
    row = connection.execute(
        """SELECT * FROM videos WHERE id=? AND library_id=?
           AND missing_since IS NULL AND deleted_at IS NULL""", (video_id, library_id),
    ).fetchone()
    if row is None:
        raise FileOperationError("影片目前不可刪除")
    source, relative, _ = _inside(root, row["relative_path"])
    if not source.is_file() or source.is_symlink():
        raise FileOperationError("原片已不在索引位置")
    stat = source.stat()
    if stat.st_size != row["file_size"] or stat.st_mtime_ns != row["mtime_ns"]:
        raise FileOperationError("影片檔案已變動，請先重新掃描")
    trash_rel = f".videomanager/trash/{video_id}.mp4"
    destination = _inside(root, trash_rel)[0]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileOperationError("回收區已有相同影片，停止搬移")
    now = utc_now()
    with connection:
        connection.execute(
            """INSERT INTO trash_entries
               (video_id,original_rel_path,trash_rel_path,state,created_at,updated_at)
               VALUES (?,?,?,'moving',?,?)""", (video_id, relative, trash_rel, now, now),
        )
    try:
        source.rename(destination)
        with connection:
            connection.execute("UPDATE videos SET deleted_at=? WHERE id=?", (utc_now(), video_id))
            connection.execute(
                "UPDATE trash_entries SET state='active',updated_at=? WHERE video_id=?",
                (utc_now(), video_id),
            )
    except Exception as exc:
        if destination.is_file() and not source.exists():
            try:
                destination.rename(source)
            except OSError:
                pass  # The moving journal lets startup finish or roll back safely.
        if source.is_file() and not destination.exists():
            with connection:
                connection.execute("DELETE FROM trash_entries WHERE video_id=?", (video_id,))
        raise FileOperationError(f"移到程式回收桶失敗：{exc}") from exc
    return destination


def restore_video(connection: sqlite3.Connection, library_id: str, root: Path, video_id: str) -> Path:
    root = _verified_root(root, library_id)
    row = connection.execute(
        """SELECT t.*,v.library_id FROM trash_entries t JOIN videos v ON v.id=t.video_id
           WHERE t.video_id=? AND v.library_id=? AND t.state='active'""",
        (video_id, library_id),
    ).fetchone()
    if row is None:
        raise FileOperationError("找不到可還原的影片")
    original, trashed = _paths(root, row)
    if not trashed.is_file() or original.exists():
        raise FileOperationError("回收桶檔案不存在，或原位置已有檔案")
    original.parent.mkdir(parents=True, exist_ok=True)
    with connection:
        connection.execute("UPDATE trash_entries SET state='restoring',updated_at=? WHERE video_id=?",
                           (utc_now(), video_id))
    try:
        trashed.rename(original)
        stat = original.stat()
        with connection:
            connection.execute(
                "UPDATE videos SET deleted_at=NULL,missing_since=NULL,file_size=?,mtime_ns=? WHERE id=?",
                (stat.st_size, stat.st_mtime_ns, video_id),
            )
            connection.execute("DELETE FROM trash_entries WHERE video_id=?", (video_id,))
    except Exception as exc:
        if original.is_file() and not trashed.exists():
            try:
                original.rename(trashed)
            except OSError:
                pass
        if trashed.is_file() and not original.exists():
            with connection:
                connection.execute("UPDATE trash_entries SET state='active',updated_at=? WHERE video_id=?",
                                   (utc_now(), video_id))
        raise FileOperationError(f"還原影片失敗：{exc}") from exc
    return original


def empty_trash(connection: sqlite3.Connection, library_id: str, root: Path,
                cache_root: Path) -> int:
    """Permanently remove trashed files and all cascading metadata after UI confirmation."""
    root = _verified_root(root, library_id)
    rows = list_trashed(connection, library_id)
    removed = 0
    for row in rows:
        if row["state"] not in {"active", "purging"}:
            continue
        _, trashed = _paths(root, row)
        with connection:
            connection.execute("UPDATE trash_entries SET state='purging',updated_at=? WHERE video_id=?",
                               (utc_now(), row["id"]))
        _remove_private_artifacts(connection, root, row["id"])
        if trashed.exists():
            if not trashed.is_file() or trashed.is_symlink():
                raise FileOperationError(f"回收桶項目不是一般檔案：{trashed}")
            trashed.unlink()
        with connection:
            connection.execute("DELETE FROM videos WHERE id=? AND library_id=?", (row["id"], library_id))
        cache = cache_root / row["id"]
        if cache.is_dir() and not cache.is_symlink():
            shutil.rmtree(cache, ignore_errors=True)
        removed += 1
    return removed


def _remove_private_artifacts(connection: sqlite3.Connection, root: Path, video_id: str) -> None:
    """Remove this video's private samples, copies and preserved originals."""
    paths: set[str] = set()
    for row in connection.execute(
        "SELECT rel_path FROM video_variants WHERE source_video_id=?", (video_id,)
    ):
        paths.add(row[0])
    for row in connection.execute(
        "SELECT output_rel_path FROM compression_jobs WHERE video_id=?", (video_id,)
    ):
        if row[0]:
            paths.add(row[0])
    for row in connection.execute(
        """SELECT r.preserved_rel_path FROM compression_replacements r
           JOIN compression_jobs j ON j.id=r.job_id WHERE j.video_id=?""", (video_id,)
    ):
        paths.add(row[0])
    for row in connection.execute(
        "SELECT output_rel_path FROM video_edits WHERE video_id=?", (video_id,)
    ):
        if row[0]:
            paths.add(row[0])
    for row in connection.execute(
        """SELECT r.preserved_rel_path FROM edit_replacements r
           JOIN video_edits e ON e.id=r.edit_id WHERE e.video_id=?""", (video_id,)
    ):
        paths.add(row[0])
    for relative in paths:
        if not relative.startswith(".videomanager/"):
            continue
        target = _inside(root, relative)[0]
        if target.is_symlink():
            raise FileOperationError("回收桶關聯檔案是連結，停止清空")
        if target.is_file():
            target.unlink()
    job_ids = [row[0] for row in connection.execute(
        "SELECT id FROM compression_jobs WHERE video_id=?", (video_id,)
    )]
    job_ids += [row[0] for row in connection.execute(
        "SELECT id FROM video_edits WHERE video_id=?", (video_id,)
    )]
    for identifier in job_ids:
        UUID(identifier)
        directory = root / ".videomanager" / "compression-staging" / identifier
        if directory.is_dir() and not directory.is_symlink() and directory.resolve().is_relative_to(root):
            shutil.rmtree(directory)


def recover_trash(connection: sqlite3.Connection, library_id: str, root: Path,
                  cache_root: Path) -> list[str]:
    """Finish journaled moves and deletes after an interrupted application exit."""
    root = _verified_root(root, library_id)
    messages: list[str] = []
    for row in list_trashed(connection, library_id):
        original, trashed = _paths(root, row)
        state = row["state"]
        if state == "moving":
            if trashed.is_file() and not original.exists():
                with connection:
                    connection.execute("UPDATE videos SET deleted_at=? WHERE id=?", (utc_now(), row["id"]))
                    connection.execute("UPDATE trash_entries SET state='active',updated_at=? WHERE video_id=?",
                                       (utc_now(), row["id"]))
                messages.append(f"已完成回收桶搬移：{row['display_title']}")
            elif original.is_file() and not trashed.exists():
                with connection:
                    connection.execute("DELETE FROM trash_entries WHERE video_id=?", (row["id"],))
        elif state == "restoring":
            if original.is_file() and not trashed.exists():
                stat = original.stat()
                with connection:
                    connection.execute(
                        "UPDATE videos SET deleted_at=NULL,missing_since=NULL,file_size=?,mtime_ns=? WHERE id=?",
                        (stat.st_size, stat.st_mtime_ns, row["id"]),
                    )
                    connection.execute("DELETE FROM trash_entries WHERE video_id=?", (row["id"],))
                messages.append(f"已完成回收桶還原：{row['display_title']}")
            elif trashed.is_file() and not original.exists():
                with connection:
                    connection.execute("UPDATE trash_entries SET state='active',updated_at=? WHERE video_id=?",
                                       (utc_now(), row["id"]))
        elif state == "purging" and not trashed.exists():
            with connection:
                connection.execute("DELETE FROM videos WHERE id=? AND library_id=?", (row["id"], library_id))
            cache = cache_root / row["id"]
            if cache.is_dir() and not cache.is_symlink():
                shutil.rmtree(cache, ignore_errors=True)
            messages.append(f"已完成永久清除：{row['display_title']}")
    return messages
