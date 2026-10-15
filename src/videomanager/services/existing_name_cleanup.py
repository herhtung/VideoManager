"""Preview and apply filename cleanup to already indexed MP4 files."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from videomanager.services.file_operations import FileOperationError, _inside, _verified_root, move_video
from videomanager.services.import_service import cleaned_import_name, cleaned_title
from videomanager.services.video_group_service import VideoGroupError, rename_group


@dataclass(frozen=True)
class ExistingNameChange:
    video_id: str
    before: str
    after: str


@dataclass(frozen=True)
class ExistingGroupNameChange:
    group_id: str
    before: str
    after: str


@dataclass(frozen=True)
class ExistingNameScan:
    changes: tuple[ExistingNameChange, ...]
    skipped: tuple[tuple[str, str], ...]
    group_changes: tuple[ExistingGroupNameChange, ...] = ()


def scan_existing_names(
    connection: sqlite3.Connection, library_id: str, root: Path,
    remove_phrases: Iterable[str], *,
    cancelled: Callable[[], bool] = lambda: False,
    progress: Callable[[int, int], None] = lambda _done, _total: None,
) -> ExistingNameScan:
    """Inspect indexed videos only; this function never changes MP4s or records."""
    root = _verified_root(root, library_id)
    phrases = tuple(phrase for phrase in remove_phrases if phrase)
    if not phrases:
        return ExistingNameScan((), ())
    rows = connection.execute(
        """SELECT id, relative_path, file_name, file_size, mtime_ns FROM videos
           WHERE library_id = ? AND deleted_at IS NULL AND missing_since IS NULL
           ORDER BY added_at, relative_path""",
        (library_id,),
    ).fetchall()
    changes: list[ExistingNameChange] = []
    skipped: list[tuple[str, str]] = []
    reserved: set[str] = set()
    processed = 0
    for index, row in enumerate(rows, 1):
        progress(index - 1, len(rows))
        if cancelled():
            break
        processed = index
        before = row["relative_path"]
        if not any(phrase.casefold() in Path(row["file_name"]).stem.casefold() for phrase in phrases):
            continue
        cleaned = cleaned_import_name(Path(row["file_name"]), phrases)
        if cleaned == row["file_name"]:
            continue
        try:
            source, _, _ = _inside(root, before)
            destination_relative = (Path(before).parent / cleaned).as_posix()
            destination, after, key = _inside(root, destination_relative)
            if not source.is_file() or source.is_symlink():
                reason = "來源檔案不存在或為捷徑"
            else:
                stat = source.stat()
                if (stat.st_size, stat.st_mtime_ns) != (row["file_size"], row["mtime_ns"]):
                    reason = "檔案已變更，請先重新掃描"
                elif destination.exists() or key in reserved or connection.execute(
                    "SELECT 1 FROM videos WHERE library_id = ? AND path_key = ? AND id <> ?",
                    (library_id, key, row["id"]),
                ).fetchone():
                    reason = "目標檔名已存在，不會覆蓋"
                else:
                    reason = ""
            if reason:
                skipped.append((before, reason))
                continue
            reserved.add(key)
            changes.append(ExistingNameChange(row["id"], before, after))
        except (FileOperationError, OSError) as exc:
            skipped.append((before, str(exc)))
    progress(processed, len(rows))
    if cancelled():
        return ExistingNameScan(tuple(changes), tuple(skipped))
    groups = connection.execute(
        "SELECT id, name FROM video_groups WHERE library_id = ? ORDER BY name", (library_id,),
    ).fetchall()
    group_changes = tuple(
        ExistingGroupNameChange(group["id"], group["name"], cleaned_title(group["name"], phrases))
        for group in groups
        if any(phrase.casefold() in group["name"].casefold() for phrase in phrases)
        and cleaned_title(group["name"], phrases) != group["name"]
    )
    return ExistingNameScan(tuple(changes), tuple(skipped), group_changes)


def apply_existing_name_changes(
    connection: sqlite3.Connection, library_id: str, root: Path,
    changes: Iterable[ExistingNameChange], *,
    group_changes: Iterable[ExistingGroupNameChange] = (),
    cancelled: Callable[[], bool] = lambda: False,
    progress: Callable[[int, int], None] = lambda _done, _total: None,
) -> tuple[int, int, list[str]]:
    """Rename sequentially through the journaled move path; report partial failures."""
    items = tuple(changes)
    groups = tuple(group_changes)
    total = len(items) + len(groups)
    success = 0
    groups_renamed = 0
    failures: list[str] = []
    for index, item in enumerate(items):
        if cancelled():
            break
        try:
            move_video(connection, library_id, root, item.video_id, item.after,
                       update_default_title=True)
            success += 1
        except (FileOperationError, sqlite3.Error, OSError) as exc:
            failures.append(f"{item.before}：{exc}")
        progress(index + 1, total)
    else:
        for index, group in enumerate(groups, len(items) + 1):
            if cancelled():
                break
            try:
                current = connection.execute(
                    "SELECT name FROM video_groups WHERE id = ? AND library_id = ?",
                    (group.group_id, library_id),
                ).fetchone()
                if current is None or current["name"] != group.before:
                    raise VideoGroupError("影片組名稱在確認後已變更")
                rename_group(connection, group.group_id, group.after)
                groups_renamed += 1
            except (VideoGroupError, sqlite3.Error) as exc:
                failures.append(f"影片組「{group.before}」：{exc}")
            progress(index, total)
    return success, groups_renamed, failures
