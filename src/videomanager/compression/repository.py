"""Persistent compression jobs. The UI and worker use separate SQLite connections."""

from __future__ import annotations

import json
import sqlite3
import shutil
import psutil
from pathlib import Path
from uuid import UUID, uuid4

from videomanager.db.database import normalize_relative_path, utc_now
from videomanager.services.library_locator import read_marker
from videomanager.compression.profiles import Profile


ACTIVE = ("analyzing", "encoding", "validating")


def queue_video(connection: sqlite3.Connection, root: Path, library_id: str,
                video_id: str, profile: Profile) -> str:
    marker = read_marker(root)
    if marker is None or marker.library_id != library_id:
        raise ValueError("影片庫識別不符")
    row = connection.execute(
        """SELECT relative_path, file_size, mtime_ns FROM videos
           WHERE id = ? AND library_id = ? AND missing_since IS NULL AND deleted_at IS NULL""",
        (video_id, library_id),
    ).fetchone()
    if row is None:
        raise ValueError("找不到可用影片")
    source = root / row["relative_path"]
    if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(root.resolve()):
        raise ValueError("來源影片不可用")
    stat = source.stat()
    if stat.st_size != row["file_size"] or stat.st_mtime_ns != row["mtime_ns"]:
        raise ValueError("影片已變動，請先重新掃描")
    existing = connection.execute(
        """SELECT id FROM compression_jobs WHERE video_id=? AND library_id=?
           AND status IN ('queued','analyzing','analyzed','encoding','validating') LIMIT 1""",
        (video_id, library_id),
    ).fetchone()
    if existing is not None:
        raise ValueError("這部影片已有未完成的試壓縮或壓縮工作，請從左側清單繼續操作")
    identifier = str(uuid4())
    with connection:
        connection.execute(
            """INSERT INTO compression_jobs
               (id, video_id, library_id, source_rel_path, profile_snapshot_json, queue_order, status,
                source_size, source_mtime_ns, created_at)
               VALUES (?, ?, ?, ?, ?, (SELECT COALESCE(MAX(queue_order),0)+1 FROM compression_jobs),
                       'queued', ?, ?, ?)""",
            (identifier, video_id, library_id, row["relative_path"],
             json.dumps(profile.snapshot(), ensure_ascii=False), stat.st_size,
             stat.st_mtime_ns, utc_now()),
        )
    return identifier


def list_jobs(connection: sqlite3.Connection, library_id: str | None, history_limit: int = 50):
    if not library_id:
        return []
    return connection.execute(
        """SELECT j.*, v.display_title, r.state AS replacement_state,
                  r.preserved_rel_path
           FROM compression_jobs j
           JOIN videos v ON v.id = j.video_id
           LEFT JOIN compression_replacements r ON r.job_id = j.id
           WHERE j.library_id = ? AND (
             j.status IN ('queued','analyzing','analyzed','encoding','validating') OR
             j.id IN (SELECT id FROM compression_jobs WHERE library_id = ?
                      AND status IN ('completed','failed','cancelled','replaced')
                      ORDER BY COALESCE(finished_at,created_at) DESC,created_at DESC LIMIT ?)
           ) ORDER BY j.queue_order DESC, j.created_at DESC""", (library_id, library_id, history_limit),
    ).fetchall()


def cancel_waiting_job(connection: sqlite3.Connection, root: Path | None, job_id: str) -> bool:
    """Discard this job's compression copy and samples, then mark it cancelled."""
    with connection:
        # Hold a write lock while checking ownership and deleting files, so another
        # worker cannot begin a journaled replacement between these operations.
        changed = connection.execute(
            """UPDATE compression_jobs SET encode_queued=encode_queued
               WHERE id=? AND status IN ('queued','analyzed','completed')
               AND NOT EXISTS (SELECT 1 FROM compression_replacements
                               WHERE job_id=compression_jobs.id AND state NOT IN ('recovered','restored'))""",
            (job_id,),
        ).rowcount
        if not changed:
            return False
        row = get_job(connection, job_id)
        marker = read_marker(root) if root is not None else None
        online = root is not None and marker is not None and marker.library_id == row["library_id"]
        output = None
        if row["output_rel_path"]:
            if not online:
                raise ValueError("請先連接正確的影片庫，再取消並清除壓縮副本。")
            relative, _ = normalize_relative_path(row["output_rel_path"])
            output = root / relative
            source = root / row["source_rel_path"]
            if (not source.is_file() or source.stat().st_size != row["source_size"]
                    or source.stat().st_mtime_ns != row["source_mtime_ns"]):
                raise ValueError("原片不存在或已變動，暫不刪除可能需要保留的壓縮副本。")
            compressed = root / ".videomanager" / "compressed"
            if (relative != row["output_rel_path"] or len(Path(relative).parts) != 3
                    or Path(relative).parts[:2] != (".videomanager", "compressed")
                    or any(path.is_symlink() or path.is_junction()
                           for path in (compressed.parent, compressed, output))
                    or output.resolve().parent != compressed.resolve()
                    or not output.resolve().is_relative_to(root.resolve())
                    or output.resolve() == source.resolve()):
                raise ValueError("壓縮副本位置不安全，已停止取消以保護原片。")
            if (connection.execute(
                    "SELECT 1 FROM compression_jobs WHERE library_id=? AND id<>? "
                    "AND output_rel_path=? COLLATE NOCASE LIMIT 1",
                    (row["library_id"], job_id, relative)).fetchone()
                    or connection.execute(
                        "SELECT 1 FROM videos WHERE library_id=? AND relative_path=? COLLATE NOCASE LIMIT 1",
                        (row["library_id"], relative)).fetchone()
                    or connection.execute(
                        "SELECT 1 FROM video_variants WHERE library_id=? AND rel_path=? COLLATE NOCASE "
                        "AND (source_video_id<>? OR original_preserved<>1) LIMIT 1",
                        (row["library_id"], relative, row["video_id"])).fetchone()):
                raise ValueError("這份副本仍被其他影片或工作使用，已停止清除。")
        staging = None
        if online:
            UUID(job_id)
            private = root / ".videomanager"
            base = private / "compression-staging"
            staging = base / job_id
            if (any(path.is_symlink() or path.is_junction() for path in (private, base, staging))
                    or not staging.resolve().is_relative_to(root.resolve())):
                raise ValueError("工作暫存位置不安全，已停止清除。")
        # File errors leave the job retryable; never report success while an open
        # player or a permission error prevented deletion of the compression copy.
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
        if output is not None:
            output.unlink(missing_ok=True)
            connection.execute(
                "DELETE FROM video_variants WHERE library_id=? AND source_video_id=? AND rel_path=? COLLATE NOCASE",
                (row["library_id"], row["video_id"], row["output_rel_path"]))
        connection.execute("DELETE FROM compression_samples WHERE job_id = ?", (job_id,))
        connection.execute(
            """UPDATE compression_jobs SET status='cancelled', progress=0, encode_queued=0,
               output_rel_path=NULL, actual_size=NULL, actual_reduction_pct=NULL,
               error_code=NULL, error_message=NULL, sample_quality_details_json=NULL,
               finished_at=? WHERE id=?""", (utc_now(), job_id))
    return True


def prune_terminal_jobs(connection: sqlite3.Connection, library_id: str | None,
                        root: Path | None, keep: int = 50) -> int:
    """Remove old non-replacement work and its samples; retain recovery journals."""
    if not library_id or root is None or keep < 0:
        return 0
    marker = read_marker(root)
    if marker is None or marker.library_id != library_id:
        return 0
    private = root / ".videomanager"
    base = private / "compression-staging"
    if private.is_symlink() or base.is_symlink():
        return 0
    rows = connection.execute(
        """SELECT j.id FROM compression_jobs j WHERE j.library_id=?
           AND j.status IN ('completed','failed','cancelled')
           AND NOT EXISTS (SELECT 1 FROM compression_replacements r WHERE r.job_id=j.id)
           ORDER BY COALESCE(j.finished_at,j.created_at) DESC,j.created_at DESC
           LIMIT -1 OFFSET ?""", (library_id, keep),
    ).fetchall()
    if not rows:
        return 0
    ids = [row["id"] for row in rows]
    with connection:
        connection.executemany("DELETE FROM compression_jobs WHERE id=?", ((identifier,) for identifier in ids))
    for identifier in ids:
        try:
            UUID(identifier)
        except ValueError:
            continue
        target = base / identifier
        if target.is_dir() and not target.is_symlink() and target.resolve().is_relative_to(root.resolve()):
            shutil.rmtree(target, ignore_errors=True)
    return len(ids)


def get_job(connection: sqlite3.Connection, job_id: str):
    return connection.execute("SELECT * FROM compression_jobs WHERE id = ?", (job_id,)).fetchone()


def schedule_encode(connection: sqlite3.Connection, job_id: str) -> bool:
    """Record the user's approval so the next available encoder can start it."""
    with connection:
        changed = connection.execute(
            "UPDATE compression_jobs SET encode_queued=1 WHERE id=? AND status='analyzed'",
            (job_id,),
        ).rowcount
    return bool(changed)


def next_scheduled_encode(connection: sqlite3.Connection, library_id: str | None,
                          exclude_video_id: str | None = None):
    if not library_id:
        return None
    return connection.execute(
        """SELECT id FROM compression_jobs WHERE library_id=? AND status='analyzed'
           AND encode_queued=1 AND (? IS NULL OR video_id<>?)
           ORDER BY queue_order DESC LIMIT 1""",
        (library_id, exclude_video_id, exclude_video_id),
    ).fetchone()


def next_queued_analysis(connection: sqlite3.Connection, library_id: str | None):
    if not library_id:
        return None
    return connection.execute(
        """SELECT id FROM compression_jobs WHERE library_id=? AND status='queued'
           ORDER BY queue_order DESC LIMIT 1""", (library_id,),
    ).fetchone()


def update_waiting_profile(connection: sqlite3.Connection, job_id: str, profile: Profile) -> bool:
    """Keep each waiting video's settings in its own job snapshot."""
    snapshot = json.dumps(profile.snapshot(), ensure_ascii=False)
    row = get_job(connection, job_id)
    if row is None or row["status"] not in {"queued", "analyzed"}:
        return False
    if row["profile_snapshot_json"] == snapshot:
        return True
    with connection:
        connection.execute(
            """UPDATE compression_jobs SET profile_snapshot_json=?, status='queued',
               encode_queued=0, progress=0, started_at=NULL, finished_at=NULL,
               estimated_size_min=NULL, estimated_size_mid=NULL, estimated_size_max=NULL,
               estimated_reduction_pct=NULL, sample_quality_metric=NULL,
               sample_quality_score=NULL, sample_quality_details_json=NULL,
               error_code=NULL, error_message=NULL WHERE id=? AND status IN ('queued','analyzed')""",
            (snapshot, job_id),
        )
    return True


def update_job(connection: sqlite3.Connection, job_id: str, **fields) -> None:
    allowed = {"status", "progress", "output_rel_path", "estimated_size_min", "estimated_size_mid",
               "estimated_size_max", "actual_size", "estimated_reduction_pct", "actual_reduction_pct",
               "sample_quality_metric", "sample_quality_score", "sample_quality_details_json",
               "error_code", "error_message", "started_at", "finished_at"}
    if not fields or set(fields) - allowed:
        raise ValueError("無效工作欄位")
    with connection:
        connection.execute("UPDATE compression_jobs SET " + ", ".join(f"{key} = ?" for key in fields)
                           + " WHERE id = ?", (*fields.values(), job_id))


def interrupt_stale_jobs(connection: sqlite3.Connection) -> None:
    """After a process restart, no old PID can still be trusted as our worker."""
    with connection:
        connection.execute(
            """UPDATE compression_jobs SET status = 'failed', error_code = 'interrupted',
               error_message = '程式上次關閉時工作尚未完成；原始影片未被取代。', finished_at = ?
               WHERE status IN ('analyzing','encoding','validating')""", (utc_now(),),
        )


def retry_job(connection: sqlite3.Connection, job_id: str) -> None:
    row = get_job(connection, job_id)
    if row is None or row["status"] not in {"failed", "cancelled"}:
        return
    with connection:
        connection.execute("DELETE FROM compression_samples WHERE job_id = ?", (job_id,))
        connection.execute(
            """UPDATE compression_jobs SET status = 'queued', progress = 0, error_code = NULL,
               error_message = NULL, started_at = NULL, finished_at = NULL,
               estimated_size_min=NULL, estimated_size_mid=NULL, estimated_size_max=NULL,
               estimated_reduction_pct=NULL, sample_quality_metric=NULL,
               sample_quality_score=NULL, sample_quality_details_json=NULL, encode_queued=0
               WHERE id = ? AND status IN ('failed','cancelled')""", (job_id,),
        )


def has_resume_checkpoint(root: Path | None, job_id: str) -> bool:
    if root is None:
        return False
    try:
        UUID(job_id)
    except ValueError:
        return False
    private = root / ".videomanager"
    base = private / "compression-staging"
    folder = base / job_id
    checkpoint = folder / "checkpoint.json"
    return (not any(path.is_symlink() for path in (private, base, folder, checkpoint))
            and checkpoint.is_file() and checkpoint.resolve().is_relative_to(root.resolve()))


def resume_job(connection: sqlite3.Connection, root: Path | None, job_id: str) -> bool:
    """Queue an interrupted encode without discarding its analysis or verified chunks."""
    row = get_job(connection, job_id)
    if row is None or row["status"] != "failed" or not has_resume_checkpoint(root, job_id):
        return False
    marker = read_marker(root) if root is not None else None
    if marker is None or marker.library_id != row["library_id"]:
        return False
    # A crash can leave a child FFmpeg alive. Never re-use its staging paths.
    staging_key = str((root / ".videomanager" / "compression-staging" / job_id).resolve()).casefold()
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (process.info["name"] or "").casefold()
            command = " ".join(process.info["cmdline"] or []).casefold()
            if "ffmpeg" in name and staging_key in command:
                raise RuntimeError("上次的 FFmpeg 程序仍在使用續壓檔案；請先結束該程序再接續")
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    with connection:
        changed = connection.execute(
            """UPDATE compression_jobs SET status='analyzed', encode_queued=1,
               progress=0, error_code=NULL, error_message=NULL, started_at=NULL,
               finished_at=NULL WHERE id=? AND status='failed'""", (job_id,),
        ).rowcount
    return bool(changed)
