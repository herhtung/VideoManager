"""Cut one unwanted interval into a new MP4; optionally replace with recovery."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path
from uuid import uuid4

from videomanager.compression.worker import (
    CompressionCancelled, CompressionError, _private_dir, _probe, _run_ffmpeg,
    _source_for_job, _space_guard,
)
from videomanager.db.database import normalize_relative_path, utc_now
from videomanager.services.library_locator import read_marker


def parse_time(value: str) -> float:
    pieces = value.strip().split(":")
    if not 1 <= len(pieces) <= 3:
        raise ValueError("時間格式請用秒數、分:秒或時:分:秒")
    try:
        numbers = [float(piece) for piece in pieces]
    except ValueError as exc:
        raise ValueError("時間格式不正確") from exc
    if any(number < 0 for number in numbers) or any(number >= 60 for number in numbers[1:]):
        raise ValueError("時間不可為負數，分與秒需小於 60")
    total = 0.0
    for number in numbers:
        total = total * 60 + number
    return total


def create_edit(connection: sqlite3.Connection, root: Path, library_id: str,
                video_id: str, start: float, end: float) -> str:
    marker = read_marker(root)
    if marker is None or marker.library_id != library_id:
        raise CompressionError("影片庫識別不符")
    row = connection.execute(
        """SELECT relative_path,file_size,mtime_ns FROM videos WHERE id=? AND library_id=?
           AND deleted_at IS NULL AND missing_since IS NULL""", (video_id, library_id),
    ).fetchone()
    if row is None:
        raise CompressionError("找不到可用影片")
    relative, _ = normalize_relative_path(row["relative_path"])
    source = root / relative
    if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(root.resolve()):
        raise CompressionError("來源影片不可用")
    stat = source.stat()
    if stat.st_size != row["file_size"] or stat.st_mtime_ns != row["mtime_ns"]:
        raise CompressionError("來源影片已變動，請先重新掃描")
    if start < 0 or end <= start or end - start < 0.1:
        raise ValueError("排除片段的起點與終點無效")
    identifier = str(uuid4())
    with connection:
        connection.execute(
            """INSERT INTO video_edits
               (id,video_id,library_id,source_rel_path,source_size,source_mtime_ns,
                cut_start,cut_end,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,'queued',?)""",
            (identifier, video_id, library_id, relative, stat.st_size, stat.st_mtime_ns,
             start, end, utc_now()),
        )
    return identifier


def get_edit(connection: sqlite3.Connection, edit_id: str):
    return connection.execute("SELECT * FROM video_edits WHERE id=?", (edit_id,)).fetchone()


def list_edits(connection: sqlite3.Connection, library_id: str | None):
    if not library_id:
        return []
    return connection.execute(
        """SELECT e.*,v.display_title FROM video_edits e JOIN videos v ON v.id=e.video_id
           WHERE e.library_id=? ORDER BY e.created_at DESC""", (library_id,),
    ).fetchall()


def _filter(start: float, end: float, duration: float, has_audio: bool) -> tuple[str, list[str]]:
    keep = [(a, b) for a, b in ((0.0, start), (end, duration)) if b - a >= 0.05]
    if not keep:
        raise ValueError("不能排除整部影片")
    parts = []
    for index, (a, b) in enumerate(keep):
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{index}]")
        if has_audio:
            parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{index}]")
    if len(keep) == 1:
        video, audio = "[v0]", "[a0]"
    else:
        streams = "".join(f"[v{i}]{f'[a{i}]' if has_audio else ''}" for i in range(len(keep)))
        parts.append(f"{streams}concat=n={len(keep)}:v=1:a={1 if has_audio else 0}"
                     + ("[v][a]" if has_audio else "[v]"))
        video, audio = "[v]", "[a]"
    return ";".join(parts), ["-map", video] + (["-map", audio] if has_audio else [])


def _edit_path(root: Path, relative: str) -> Path:
    normalized, _ = normalize_relative_path(relative)
    path = root / normalized
    if path.is_symlink() or not path.resolve(strict=False).is_relative_to(root.resolve()):
        raise CompressionError("路徑超出影片庫")
    return path


def run_edit(connection: sqlite3.Connection, edit_id: str, root: Path, capabilities,
             cancelled=lambda: False, progress=lambda value: None) -> Path:
    edit = get_edit(connection, edit_id)
    if edit is None or edit["status"] != "queued":
        raise CompressionError("剪輯工作狀態無效")
    source = _source_for_job(edit, root)
    if not {"libx264", "libopenh264"}.intersection(capabilities.encoders):
        raise CompressionError("剪輯需要 H.264 CPU 編碼器，但本機 FFmpeg 不提供")
    info = _probe(capabilities.ffprobe, source)
    start, end, duration = edit["cut_start"], edit["cut_end"], info["duration"]
    if start < 0 or end > duration + 0.05 or end - start >= duration - 0.5:
        raise CompressionError("排除範圍超出片長，或剩餘影片太短")
    end = min(end, duration)
    if info["video"].get("color_transfer") in {"smpte2084", "arib-std-b67"}:
        raise CompressionError("HDR 影片目前不支援剪輯，避免錯誤變換色彩")
    staging = _private_dir(root, ".videomanager", "compression-staging", edit_id)
    target = staging / "cut.partial"
    target.unlink(missing_ok=True)
    output_dir = _private_dir(root, ".videomanager", "edited")
    output = output_dir / f"{source.stem}_cut.mp4"
    serial = 2
    while output.exists():
        output = output_dir / f"{source.stem}_cut_{serial}.mp4"
        serial += 1
    _space_guard(root, edit["source_size"] + 512 * 2**20)
    graph, maps = _filter(start, end, duration, info["audio"] is not None)
    encoder = "libx264" if "libx264" in capabilities.encoders else "libopenh264"
    if encoder == "libx264":
        video_args = ["-c:v", encoder, "-preset", "medium", "-crf", "20"]
    else:
        try:
            source_rate = int(info["video"].get("bit_rate") or 0)
        except (TypeError, ValueError):
            source_rate = 0
        video_args = ["-c:v", encoder, "-b:v", str(max(2_000_000, round(source_rate * 1.2)))]
    command = [capabilities.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
               "-nostats", "-progress", "pipe:1", "-i", str(source),
               "-filter_complex", graph, *maps, "-map_metadata", "0",
               *video_args]
    if info["audio"]:
        command += ["-c:a", "aac", "-b:a", "192k"]
    command += ["-movflags", "+faststart", "-f", "mp4", "-n", str(target)]
    with connection:
        connection.execute("UPDATE video_edits SET status='running',progress=0,original_duration=? WHERE id=?",
                           (duration, edit_id))
    output_created = False
    try:
        _run_ffmpeg(command, duration - (end - start), cancelled, progress, space_path=root)
        if cancelled():
            raise CompressionCancelled("使用者取消")
        with connection:
            connection.execute("UPDATE video_edits SET status='validating',progress=99 WHERE id=?", (edit_id,))
        produced = _probe(capabilities.ffprobe, target)
        expected = duration - (end - start)
        if abs(produced["duration"] - expected) > max(1.5, expected * 0.02):
            raise CompressionError("剪輯結果片長不符")
        if bool(produced["audio"]) != bool(info["audio"]):
            raise CompressionError("剪輯結果音訊串流不符")
        for seek in (0.1, max(0.1, produced["duration"] / 2)):
            check = subprocess.run(
                [capabilities.ffmpeg, "-v", "error", "-ss", f"{seek:.2f}", "-i", str(target),
                 "-frames:v", "1", "-f", "null", "-"], capture_output=True, timeout=40,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if check.returncode:
                raise CompressionError("剪輯結果影格解碼失敗")
        _source_for_job(edit, root)
        if output.exists():
            raise CompressionError("目的檔名已被其他工作使用")
        target.rename(output)
        output_created = True
        with connection:
            connection.execute(
                """INSERT INTO video_variants
                   (id,source_video_id,library_id,rel_path,codec,size_bytes,created_at,active_variant,original_preserved)
                   VALUES (?,?,?,?,?,?,?,0,1)""",
                (str(uuid4()), edit["video_id"], edit["library_id"], output.relative_to(root).as_posix(),
                 produced["video"].get("codec_name"), output.stat().st_size, utc_now()),
            )
            connection.execute(
                """UPDATE video_edits SET status='completed',output_rel_path=?,output_duration=?,
                   output_size=?,progress=100,finished_at=? WHERE id=?""",
                (output.relative_to(root).as_posix(), produced["duration"],
                 output.stat().st_size, utc_now(), edit_id),
            )
        return output
    except Exception:
        if output_created:
            output.unlink(missing_ok=True)
        raise
    finally:
        target.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)


def fail_edit(connection: sqlite3.Connection, edit_id: str, exc: Exception) -> None:
    with connection:
        connection.execute(
            "UPDATE video_edits SET status=?,error_message=?,finished_at=? WHERE id=?",
            ("cancelled" if isinstance(exc, CompressionCancelled) else "failed", str(exc)[-1000:],
             utc_now(), edit_id),
        )


def interrupt_stale_edits(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute(
            """UPDATE video_edits SET status='failed',error_message='程式上次關閉時剪輯尚未完成；原片保留。',
               finished_at=? WHERE status IN ('running','validating')""", (utc_now(),),
        )


def replace_edit(connection: sqlite3.Connection, root: Path, cache_root: Path,
                 edit_id: str, capabilities) -> Path:
    edit = get_edit(connection, edit_id)
    if edit is None or edit["status"] != "completed" or not edit["output_rel_path"]:
        raise CompressionError("剪輯副本尚未完成")
    source = _source_for_job(edit, root)
    output = _edit_path(root, edit["output_rel_path"])
    if not output.is_file():
        raise CompressionError("剪輯副本不存在")
    info = _probe(capabilities.ffprobe, output)
    if abs(info["duration"] - edit["output_duration"]) > 1.5:
        raise CompressionError("剪輯副本已變動")
    marker = read_marker(root)
    if marker is None or marker.library_id != edit["library_id"]:
        raise CompressionError("影片庫識別已變動")
    preserved_rel = f".videomanager/edit-originals/{edit_id}.mp4"
    preserved = _edit_path(root, preserved_rel)
    preserved.parent.mkdir(parents=True, exist_ok=True)
    if preserved.exists():
        raise CompressionError("原片保留位置已存在")
    with connection:
        cursor = connection.execute(
            """INSERT INTO edit_replacements
               (edit_id,source_rel_path,output_rel_path,preserved_rel_path,state,updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(edit_id) DO UPDATE SET state='prepared',updated_at=excluded.updated_at,
                   error_message=NULL WHERE edit_replacements.state='recovered'""",
            (edit_id, edit["source_rel_path"], edit["output_rel_path"], preserved_rel,
             "prepared", utc_now()),
        )
        if cursor.rowcount != 1:
            raise CompressionError("先前剪輯替換尚未復原")
    try:
        source.rename(preserved)
        with connection:
            connection.execute("UPDATE edit_replacements SET state='original_preserved',updated_at=? WHERE edit_id=?",
                               (utc_now(), edit_id))
        output.rename(source)
        with connection:
            connection.execute("UPDATE edit_replacements SET state='new_in_place',updated_at=? WHERE edit_id=?",
                               (utc_now(), edit_id))
        stat = source.stat()
        with connection:
            connection.execute(
                """UPDATE videos SET file_size=?,mtime_ns=?,duration_ms=?,width=?,height=?,
                   video_codec=?,audio_codec=?,missing_since=NULL WHERE id=? AND library_id=?""",
                (stat.st_size, stat.st_mtime_ns, round(info["duration"] * 1000),
                 info["video"].get("width"), info["video"].get("height"),
                 info["video"].get("codec_name"),
                 info["audio"].get("codec_name") if info["audio"] else None,
                 edit["video_id"], edit["library_id"]),
            )
            connection.execute(
                """INSERT INTO thumbnail_jobs(video_id,state,attempts,cache_signature,updated_at,error_message)
                   VALUES (?,'pending',0,NULL,?,NULL)
                   ON CONFLICT(video_id) DO UPDATE SET state='pending',attempts=0,
                   cache_signature=NULL,updated_at=excluded.updated_at,error_message=NULL""",
                (edit["video_id"], utc_now()),
            )
            connection.execute(
                "UPDATE video_variants SET rel_path=?,active_variant=1 WHERE source_video_id=? AND rel_path=?",
                (edit["source_rel_path"], edit["video_id"], edit["output_rel_path"]),
            )
            connection.execute("UPDATE video_edits SET status='replaced',finished_at=? WHERE id=?",
                               (utc_now(), edit_id))
            connection.execute("UPDATE edit_replacements SET state='complete',updated_at=? WHERE edit_id=?",
                               (utc_now(), edit_id))
        shutil.rmtree(cache_root / edit["video_id"], ignore_errors=True)
        return preserved
    except Exception as exc:
        try:
            if preserved.exists() and source.exists() and not output.exists():
                source.rename(output)
            if preserved.exists() and not source.exists():
                preserved.rename(source)
            with connection:
                connection.execute("UPDATE edit_replacements SET state='recovered',error_message=?,updated_at=? WHERE edit_id=?",
                                   (str(exc)[-500:], utc_now(), edit_id))
        except (OSError, sqlite3.Error):
            pass
        raise CompressionError(f"剪輯替換失敗，原片保留位置：{preserved}。{exc}") from exc


def recover_edit_replacements(connection: sqlite3.Connection, root: Path, cache_root: Path) -> list[str]:
    marker = read_marker(root)
    if marker is None:
        return ["影片庫離線，剪輯替換待復原"]
    messages = []
    rows = connection.execute(
        """SELECT r.*,e.library_id,e.video_id,e.status AS edit_status
           FROM edit_replacements r JOIN video_edits e ON e.id=r.edit_id
           WHERE r.state NOT IN ('complete','recovered')"""
    ).fetchall()
    for row in rows:
        if row["library_id"] != marker.library_id:
            continue
        try:
            source = _edit_path(root, row["source_rel_path"])
            output = _edit_path(root, row["output_rel_path"])
            preserved = _edit_path(root, row["preserved_rel_path"])
            if row["edit_status"] == "replaced":
                shutil.rmtree(cache_root / row["video_id"], ignore_errors=True)
                state = "complete"
            else:
                if preserved.exists() and source.exists() and output.exists():
                    raise CompressionError("來源、副本、原片皆存在，狀態不明")
                if preserved.exists() and source.exists() and not output.exists():
                    source.rename(output)
                if preserved.exists() and not source.exists():
                    preserved.rename(source)
                if not source.is_file():
                    raise CompressionError("原片不存在")
                state = "recovered"
            with connection:
                connection.execute("UPDATE edit_replacements SET state=?,updated_at=? WHERE edit_id=?",
                                   (state, utc_now(), row["edit_id"]))
            messages.append(f"剪輯替換 {row['edit_id']} 已復原")
        except (OSError, CompressionError, sqlite3.Error) as exc:
            messages.append(f"剪輯替換 {row['edit_id']} 需人工檢查：{exc}")
    return messages
