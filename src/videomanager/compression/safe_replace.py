"""Journaled, same-volume replacement with a recoverable original copy."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path
from uuid import uuid4

from videomanager.compression.repository import get_job
from videomanager.compression.worker import CompressionError, _probe, _source_for_job, _space_guard, _validate_output
from videomanager.compression.profiles import Profile
from videomanager.config import load_settings
from videomanager.db.database import normalize_relative_path, utc_now
from videomanager.services.library_locator import read_marker
from videomanager.services.thumbnail_service import cache_signature, generate_thumbnails
from videomanager.services.thumbnail_service import PREVIEW_COUNTS


def _inside(root: Path, relative: str) -> Path:
    normalized, _ = normalize_relative_path(relative)
    target = root / normalized
    if target.is_symlink() or not target.resolve(strict=False).is_relative_to(root.resolve()):
        raise CompressionError("影片路徑超出管理資料夾")
    return target


def _refresh_thumbnail(connection: sqlite3.Connection, root: Path, cache_root: Path,
                       video_id: str, source: Path, duration_ms: int, ffmpeg: str,
                       settings_path: Path | None = None) -> None:
    """Rebuild the changed video's preview immediately, without rescanning the library."""
    try:
        mode = load_settings(settings_path).preview_mode if settings_path else "standard"
    except (OSError, ValueError, TypeError):
        mode = "standard"
    count = PREVIEW_COUNTS[mode]
    try:
        generated = generate_thumbnails(source, cache_root, video_id, duration_ms,
                                        count=count, ffmpeg=ffmpeg)
        if not generated:
            return
        stat = source.stat()
        with connection:
            connection.execute(
                """UPDATE thumbnail_jobs SET state='done', attempts=0, cache_signature=?,
                   updated_at=?, error_message=NULL WHERE video_id=?""",
                (cache_signature(stat.st_size, stat.st_mtime_ns, count), utc_now(), video_id),
            )
    except Exception:
        # The replacement itself succeeded; a later scan may retry the pending preview.
        return


def prune_preserved_originals(connection: sqlite3.Connection, root: Path, keep: int = 3) -> int:
    """Keep the newest three restorable originals per library."""
    marker = read_marker(root)
    if marker is None or keep < 0:
        return 0
    rows = connection.execute(
        """SELECT r.job_id, r.preserved_rel_path FROM compression_replacements r
           JOIN compression_jobs j ON j.id=r.job_id
           WHERE j.library_id=? AND r.state='complete'
           ORDER BY COALESCE(j.finished_at,j.created_at) DESC, j.created_at DESC
           LIMIT -1 OFFSET ?""",
        (marker.library_id, keep),
    ).fetchall()
    removed = 0
    for row in rows:
        path = _inside(root, row["preserved_rel_path"])
        with connection:
            connection.execute("UPDATE compression_replacements SET state='expiring', updated_at=? WHERE job_id=?",
                               (utc_now(), row["job_id"]))
        if path.is_file():
            path.unlink()
        with connection:
            connection.execute(
                "DELETE FROM video_variants WHERE library_id=? AND rel_path=?",
                (marker.library_id, row["preserved_rel_path"]),
            )
            connection.execute(
                "UPDATE compression_replacements SET state='original_expired', updated_at=? WHERE job_id=?",
                (utc_now(), row["job_id"]),
            )
        removed += 1
    return removed


def _extra_streams(info: dict) -> Counter:
    return Counter((stream.get("codec_type"), stream.get("codec_name"))
                   for stream in info["streams"] if stream.get("codec_type") not in {"video", "audio"})


def _restore_extra_streams(capabilities, root: Path, source: Path, output: Path,
                           original_info: dict, profile: Profile) -> dict:
    """Copy the original's non-AV streams into a verified compression copy."""
    _space_guard(root, output.stat().st_size + 256 * 2**20)
    temporary = output.with_name(f".{uuid4().hex}.streams.mp4")
    try:
        command = [capabilities.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                   "-i", str(output), "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
                   "-map", "1:s?", "-map", "1:d?", "-map", "1:t?", "-map_metadata", "1",
                   "-map_chapters", "1", "-c", "copy", "-movflags", "+faststart",
                   "-f", "mp4", "-n", str(temporary)]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise CompressionError("無法把原片的附加串流保留到 MP4：" + result.stderr[-500:])
        remuxed = _validate_output(capabilities, original_info, temporary, profile)
        if _extra_streams(remuxed) != _extra_streams(original_info):
            raise CompressionError("附加串流驗證不符")
        os.replace(temporary, output)
        return remuxed
    finally:
        temporary.unlink(missing_ok=True)


def replace_original(connection: sqlite3.Connection, root: Path, cache_root: Path,
                     job_id: str, capabilities, settings_path: Path | None = None) -> Path:
    """Call only after a separate, explicit UI confirmation."""
    job = get_job(connection, job_id)
    if job is None or job["status"] != "completed" or not job["output_rel_path"]:
        raise CompressionError("壓縮副本尚未完成")
    source = _source_for_job(job, root)
    output = _inside(root, job["output_rel_path"])
    if not output.is_file():
        raise CompressionError("壓縮副本不在預期位置")
    details = json.loads(job["sample_quality_details_json"] or "{}")
    if details.get("hdr_or_rotation"):
        raise CompressionError("原片有特殊色彩或旋轉資訊；目前無法驗證壓縮副本是否完整保留，原片與副本都已保留")
    profile = Profile(**json.loads(job["profile_snapshot_json"])).validate()
    original_info = _probe(capabilities.ffprobe, source)
    output_info = _validate_output(capabilities, original_info, output, profile)
    if _extra_streams(original_info):
        try:
            output_info = _restore_extra_streams(capabilities, root, source, output, original_info, profile)
        except (CompressionError, OSError) as exc:
            raise CompressionError(f"無法安全保留原片的附加串流；原片與壓縮副本均未替換。{exc}") from exc
    marker = read_marker(root)
    if marker is None or marker.library_id != job["library_id"]:
        raise CompressionError("影片庫識別已變動")
    preserved_relative = f".videomanager/compression-originals/{job_id}.mp4"
    preserved = _inside(root, preserved_relative)
    preserved.parent.mkdir(parents=True, exist_ok=True)
    if preserved.exists():
        raise CompressionError("此工作的原片保留位置已存在，停止替換")
    with connection:
        cursor = connection.execute(
            """INSERT INTO compression_replacements
               (job_id,source_rel_path,output_rel_path,preserved_rel_path,state,updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                   source_rel_path=excluded.source_rel_path,
                   output_rel_path=excluded.output_rel_path,
                   preserved_rel_path=excluded.preserved_rel_path,
                   state='prepared', updated_at=excluded.updated_at, error_message=NULL
               WHERE compression_replacements.state='recovered'""",
            (job_id, job["source_rel_path"], job["output_rel_path"], preserved_relative,
             "prepared", utc_now()),
        )
        if cursor.rowcount != 1:
            raise CompressionError("此替換工作尚未復原，不能重試")
    try:
        source.rename(preserved)
        with connection:
            connection.execute("UPDATE compression_replacements SET state='original_preserved', updated_at=? WHERE job_id=?",
                               (utc_now(), job_id))
        output.rename(source)
        with connection:
            connection.execute("UPDATE compression_replacements SET state='new_in_place', updated_at=? WHERE job_id=?",
                               (utc_now(), job_id))
        stat = source.stat()
        with connection:
            connection.execute(
                """UPDATE videos SET file_size=?, mtime_ns=?, duration_ms=?, width=?, height=?,
                   video_codec=?, audio_codec=?, missing_since=NULL WHERE id=? AND library_id=?""",
                (stat.st_size, stat.st_mtime_ns, round(output_info["duration"] * 1000),
                 output_info["video"].get("width"), output_info["video"].get("height"),
                 output_info["video"].get("codec_name"),
                 output_info["audio"].get("codec_name") if output_info["audio"] else None,
                 job["video_id"], job["library_id"]),
            )
            connection.execute(
                """INSERT INTO thumbnail_jobs(video_id,state,attempts,cache_signature,updated_at,error_message)
                   VALUES (?,'pending',0,NULL,?,NULL)
                   ON CONFLICT(video_id) DO UPDATE SET state='pending', attempts=0,
                   cache_signature=NULL, updated_at=excluded.updated_at, error_message=NULL""",
                (job["video_id"], utc_now()),
            )
            connection.execute(
                """UPDATE video_variants SET rel_path=?, active_variant=0
                   WHERE source_video_id=? AND rel_path=?""",
                (preserved_relative, job["video_id"], job["source_rel_path"]),
            )
            connection.execute(
                "UPDATE video_variants SET rel_path=?, active_variant=1 WHERE source_video_id=? AND rel_path=?",
                (job["source_rel_path"], job["video_id"], job["output_rel_path"]),
            )
            connection.execute("UPDATE compression_jobs SET status='replaced', finished_at=? WHERE id=?",
                               (utc_now(), job_id))
            connection.execute("UPDATE compression_replacements SET state='complete', updated_at=? WHERE job_id=?",
                               (utc_now(), job_id))
        shutil.rmtree(cache_root / job["video_id"], ignore_errors=True)
        _refresh_thumbnail(connection, root, cache_root, job["video_id"], source,
                           round(output_info["duration"] * 1000), capabilities.ffmpeg,
                           settings_path)
        try:
            prune_preserved_originals(connection, root)
        except (OSError, sqlite3.Error, CompressionError, ValueError):
            pass
        return preserved
    except Exception as exc:
        # If the app remains alive, restore both paths immediately. Startup recovery
        # performs the same journal-directed action after a crash.
        try:
            if not output.exists() and source.exists() and preserved.exists():
                source.rename(output)
            if not source.exists() and preserved.exists():
                preserved.rename(source)
            with connection:
                connection.execute(
                    "UPDATE compression_replacements SET state='recovered', error_message=?, updated_at=? WHERE job_id=?",
                    (str(exc)[-500:], utc_now(), job_id),
                )
        except (OSError, sqlite3.Error):
            pass
        raise CompressionError(f"替換未完成；原片保留位置：{preserved}。{exc}") from exc


def _rollback_restore_files(source: Path, output: Path, preserved: Path) -> None:
    """Put the compressed version back at the library path after an interrupted restore."""
    if source.exists() and output.exists() and not preserved.exists():
        source.rename(preserved)
    if not source.exists() and output.exists() and preserved.exists():
        output.rename(source)
    if not source.is_file() or not preserved.is_file() or output.exists():
        raise CompressionError("還原中斷後檔案狀態不明，請檢查原片保留區")


def restore_original(connection: sqlite3.Connection, root: Path, cache_root: Path,
                     job_id: str, capabilities, settings_path: Path | None = None) -> Path:
    """Restore a preserved original from history; keep the compressed copy separately."""
    row = connection.execute(
        """SELECT j.*, r.state AS replacement_state, r.preserved_rel_path
           FROM compression_jobs j JOIN compression_replacements r ON r.job_id=j.id
           WHERE j.id=?""", (job_id,),
    ).fetchone()
    if row is None or row["replacement_state"] != "complete" or row["status"] != "replaced":
        raise CompressionError("這筆紀錄沒有可還原的原片")
    marker = read_marker(root)
    if marker is None or marker.library_id != row["library_id"]:
        raise CompressionError("影片庫識別已變動")
    newer = connection.execute(
        """SELECT 1 FROM compression_replacements r JOIN compression_jobs j ON j.id=r.job_id
           WHERE j.video_id=? AND COALESCE(j.finished_at,j.created_at)>?
             AND r.state IN ('complete','original_expired') LIMIT 1""",
        (row["video_id"], row["finished_at"] or row["created_at"]),
    ).fetchone()
    if newer:
        raise CompressionError("請先還原這部影片較新的替換紀錄")
    source = _inside(root, row["source_rel_path"])
    output = _inside(root, row["output_rel_path"])
    preserved = _inside(root, row["preserved_rel_path"])
    if not source.is_file() or not preserved.is_file() or output.exists():
        raise CompressionError("來源、保留原片或壓縮副本的位置已變動")
    current = connection.execute("SELECT mtime_ns FROM videos WHERE id=?", (row["video_id"],)).fetchone()
    stat = source.stat()
    if (current is None or stat.st_size != row["actual_size"]
            or stat.st_mtime_ns != current["mtime_ns"] or preserved.stat().st_size != row["source_size"]):
        raise CompressionError("目前影片檔案已變動，為避免覆蓋新內容，停止還原")
    original_info = _probe(capabilities.ffprobe, preserved)
    with connection:
        connection.execute("UPDATE compression_replacements SET state='restore_prepared', updated_at=? WHERE job_id=?",
                           (utc_now(), job_id))
    try:
        source.rename(output)
        with connection:
            connection.execute("UPDATE compression_replacements SET state='restore_copy_saved', updated_at=? WHERE job_id=?",
                               (utc_now(), job_id))
        preserved.rename(source)
        stat = source.stat()
        with connection:
            connection.execute(
                """UPDATE videos SET file_size=?, mtime_ns=?, duration_ms=?, width=?, height=?,
                   video_codec=?, audio_codec=?, missing_since=NULL WHERE id=? AND library_id=?""",
                (stat.st_size, stat.st_mtime_ns, round(original_info["duration"] * 1000),
                 original_info["video"].get("width"), original_info["video"].get("height"),
                 original_info["video"].get("codec_name"),
                 original_info["audio"].get("codec_name") if original_info["audio"] else None,
                 row["video_id"], row["library_id"]),
            )
            connection.execute(
                """INSERT INTO thumbnail_jobs(video_id,state,attempts,cache_signature,updated_at,error_message)
                   VALUES (?,'pending',0,NULL,?,NULL)
                   ON CONFLICT(video_id) DO UPDATE SET state='pending', attempts=0,
                   cache_signature=NULL, updated_at=excluded.updated_at, error_message=NULL""",
                (row["video_id"], utc_now()),
            )
            connection.execute(
                """UPDATE video_variants SET rel_path=?, active_variant=0
                   WHERE source_video_id=? AND rel_path=?""",
                (row["output_rel_path"], row["video_id"], row["source_rel_path"]),
            )
            connection.execute(
                """UPDATE video_variants SET rel_path=?, active_variant=1
                   WHERE source_video_id=? AND rel_path=?""",
                (row["source_rel_path"], row["video_id"], row["preserved_rel_path"]),
            )
            connection.execute("UPDATE compression_replacements SET state='restored', updated_at=? WHERE job_id=?",
                               (utc_now(), job_id))
    except Exception as exc:
        try:
            _rollback_restore_files(source, output, preserved)
            with connection:
                connection.execute("UPDATE compression_replacements SET state='complete', updated_at=? WHERE job_id=?",
                                   (utc_now(), job_id))
        except (OSError, sqlite3.Error, CompressionError):
            pass
        raise CompressionError(f"還原未完成；原片仍在保留區或原路徑。{exc}") from exc
    shutil.rmtree(cache_root / row["video_id"], ignore_errors=True)
    _refresh_thumbnail(connection, root, cache_root, row["video_id"], source,
                       round(original_info["duration"] * 1000), capabilities.ffmpeg,
                       settings_path)
    return output


def recover_replacements(connection: sqlite3.Connection, root: Path, cache_root: Path) -> list[str]:
    """Safely roll unfinished swaps back before the normal library scan."""
    marker = read_marker(root)
    if marker is None:
        return ["影片庫離線，替換復原須等重新連接"]
    results = []
    rows = connection.execute(
        """SELECT r.*, j.library_id, j.video_id, j.status AS job_status
           FROM compression_replacements r JOIN compression_jobs j ON j.id = r.job_id
           WHERE r.state NOT IN ('complete','recovered','restored','original_expired')"""
    ).fetchall()
    for row in rows:
        if row["library_id"] != marker.library_id:
            continue
        try:
            source = _inside(root, row["source_rel_path"])
            output = _inside(root, row["output_rel_path"])
            preserved = _inside(root, row["preserved_rel_path"])
            if row["state"] == "expiring":
                if preserved.is_file():
                    preserved.unlink()
                with connection:
                    connection.execute(
                        "DELETE FROM video_variants WHERE library_id=? AND rel_path=?",
                        (row["library_id"], row["preserved_rel_path"]),
                    )
                    connection.execute(
                        "UPDATE compression_replacements SET state='original_expired', updated_at=? WHERE job_id=?",
                        (utc_now(), row["job_id"]),
                    )
                continue
            if row["state"].startswith("restore_"):
                _rollback_restore_files(source, output, preserved)
                with connection:
                    connection.execute("UPDATE compression_replacements SET state='complete', updated_at=? WHERE job_id=?",
                                       (utc_now(), row["job_id"]),
                    )
                results.append(f"已復原未完成的原片還原工作 {row['job_id']}")
                continue
            if row["job_status"] == "replaced":
                shutil.rmtree(cache_root / row["video_id"], ignore_errors=True)
                with connection:
                    connection.execute("UPDATE compression_replacements SET state='complete',updated_at=? WHERE job_id=?",
                                       (utc_now(), row["job_id"]))
                continue
            if preserved.exists() and source.exists() and output.exists():
                raise CompressionError("來源、副本與保留原片同時存在，狀態不明，需人工檢查")
            if preserved.exists() and source.exists() and not output.exists():
                source.rename(output)
            if preserved.exists() and not source.exists():
                preserved.rename(source)
            if not source.is_file():
                raise CompressionError("來源仍不存在，需人工檢查保留區")
            with connection:
                connection.execute("UPDATE compression_replacements SET state='recovered',updated_at=? WHERE job_id=?",
                                   (utc_now(), row["job_id"]))
            results.append(f"已復原替換工作 {row['job_id']}")
        except (OSError, CompressionError, sqlite3.Error) as exc:
            results.append(f"替換工作 {row['job_id']} 需人工檢查：{exc}")
    return results
