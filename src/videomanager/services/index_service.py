"""Process probe and thumbnail jobs one video at a time."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from videomanager.db.database import utc_now
from videomanager.services.media_probe import ProbeError, probe_media
from videomanager.services.thumbnail_service import (
    ThumbnailCancelled, ThumbnailError,
    cache_signature, cleanup_stale_staging,
    generate_thumbnails,
)


def retry_thumbnail(connection: sqlite3.Connection, video_id: str) -> None:
    with connection:
        connection.execute(
            """UPDATE thumbnail_jobs SET state = 'pending', attempts = 0,
               error_message = NULL, updated_at = ? WHERE video_id = ?""",
            (utc_now(), video_id),
        )


def requeue_first_frame_failures(connection: sqlite3.Connection) -> int:
    """Give legacy partial-frame errors one automatic retry, without a loop."""
    with connection:
        result = connection.execute(
            """UPDATE thumbnail_jobs SET state = 'pending', updated_at = ?
               WHERE state = 'error' AND attempts = 1
                 AND error_message LIKE 'Frame % failed:%'""",
            (utc_now(),),
        )
    return result.rowcount


def process_pending_media(
    connection: sqlite3.Connection,
    root: Path,
    cache_root: Path,
    *,
    frame_count: int = 10,
    cancelled: Callable[[], bool] = lambda: False,
    paused: Callable[[], bool] = lambda: False,
    progress: Callable[[int, int, str], None] = lambda done, total, title: None,
) -> tuple[int, int]:
    cleanup_stale_staging(cache_root)
    # An interrupted process may leave a job marked running. There is only
    # one indexing worker per app instance, so resume those jobs on startup.
    with connection:
        connection.execute("UPDATE thumbnail_jobs SET state = 'pending', updated_at = ? WHERE state = 'running'", (utc_now(),))
    requeue_first_frame_failures(connection)
    rows = connection.execute(
        """SELECT v.* FROM videos v JOIN thumbnail_jobs j ON j.video_id = v.id
           WHERE v.missing_since IS NULL AND v.deleted_at IS NULL AND j.state = 'pending'
           ORDER BY CASE WHEN j.attempts > 0 THEN 0 ELSE 1 END, v.added_at"""
    ).fetchall()
    done = failed = 0
    index = 0
    while index < len(rows):
        row = rows[index]
        if cancelled():
            break
        while paused() and not cancelled():
            time.sleep(0.2)
        if cancelled():
            break
        source = root / row["relative_path"]
        try:
            stat = source.stat()
            if stat.st_size != row["file_size"] or stat.st_mtime_ns != row["mtime_ns"]:
                raise ThumbnailError("File changed since scan; rescan required")
            with connection:
                connection.execute(
                    "UPDATE thumbnail_jobs SET state = 'running', attempts = attempts + 1, updated_at = ? WHERE video_id = ?",
                    (utc_now(), row["id"]),
                )
            duration_ms = row["duration_ms"]
            if duration_ms is None:
                info = probe_media(source)
                duration_ms = info.duration_ms
                with connection:
                    connection.execute(
                        """UPDATE videos SET duration_ms = ?, width = ?, height = ?,
                           video_codec = ?, audio_codec = ? WHERE id = ?""",
                        (duration_ms, info.width, info.height, info.video_codec, info.audio_codec, row["id"]),
                    )
            generate_thumbnails(
                source, cache_root, row["id"], duration_ms, count=frame_count,
                cancelled=lambda: cancelled() or paused(),
            )
            with connection:
                connection.execute(
                    """UPDATE thumbnail_jobs SET state = 'done', cache_signature = ?,
                       updated_at = ?, error_message = NULL WHERE video_id = ?""",
                    (cache_signature(row["file_size"], row["mtime_ns"], frame_count), utc_now(), row["id"]),
                )
            done += 1
        except ThumbnailCancelled:
            with connection:
                connection.execute(
                    """UPDATE thumbnail_jobs SET state = 'pending',
                       attempts = MAX(0, attempts - 1), updated_at = ? WHERE video_id = ?""",
                    (utc_now(), row["id"]),
                )
            if cancelled():
                break
            # A player opened mid-frame. Resume this same video after playback.
            continue
        except (OSError, ProbeError, ThumbnailError) as exc:
            with connection:
                connection.execute(
                    "UPDATE thumbnail_jobs SET state = 'error', updated_at = ?, error_message = ? WHERE video_id = ?",
                    (utc_now(), str(exc), row["id"]),
                )
            failed += 1
        progress(done + failed, len(rows), row["file_name"])
        index += 1
    return done, failed
