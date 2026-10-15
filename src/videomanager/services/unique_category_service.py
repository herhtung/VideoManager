"""Exclusive category assignments that move original files within the library."""

from __future__ import annotations

import sqlite3
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from videomanager.services.file_operations import FileOperationError, _verified_root, move_video
from videomanager.services.import_service import MAX_FILES_PER_FOLDER, choose_group_bucket
from videomanager.services.video_group_service import group_for_video, group_members


def _destination(root: Path, base: Path, file_name: str) -> Path:
    if not base.resolve(strict=False).is_relative_to(root):
        raise FileOperationError("Category folder leaves the library")
    number = 1
    while True:
        folder = base / f"{number:03d}"
        if folder.is_symlink() or base.is_symlink() or base.parent.is_symlink():
            raise FileOperationError("Category folder contains a symlink")
        if folder.exists() and not folder.is_dir():
            raise FileOperationError(f"Folder path is occupied: {folder}")
        names = {path.name.casefold() for path in folder.iterdir() if path.is_file() and path.suffix.lower() == ".mp4"} if folder.exists() else set()
        if len(names) < MAX_FILES_PER_FOLDER:
            break
        number += 1
    source_name = Path(file_name)
    candidate = source_name.name
    serial = 2
    while candidate.casefold() in names:
        candidate = f"{source_name.stem} ({serial}){source_name.suffix}"
        serial += 1
    return folder / candidate


def _two_month_period(added_at: str) -> str:
    try:
        date = datetime.fromisoformat(added_at.replace("Z", "+00:00"))
        if date.tzinfo is not None:
            date = date.astimezone(timezone(timedelta(hours=8)))
    except ValueError:
        date = datetime.now()
    first = ((date.month - 1) // 2) * 2 + 1
    return f"{date.year:04d}-{first:02d}~{first + 1:02d}"


def assign_unique_category(
    connection: sqlite3.Connection, library_id: str, root: Path,
    video_id: str, category_id: str | None, *, expected_destination: Path | None = None,
) -> Path | None:
    """Keep one exclusive category per video and journal the physical move."""
    root = _verified_root(root, library_id)
    target = plan_unique_assignment(connection, library_id, root, video_id, category_id)
    if target is None:
        return None
    if expected_destination is not None and target != expected_destination:
        raise FileOperationError("目的地自確認後已變更，請重新確認")
    group = group_for_video(connection, video_id)
    if group is not None:
        folder, destinations = _plan_group_assignment(connection, root, group, category_id)
        folder.mkdir(parents=True, exist_ok=False)
        for member_id, destination in destinations:
            move_video(connection, library_id, root, member_id,
                       destination.relative_to(root).as_posix(), unique_category_id=category_id)
            if category_id is None and not connection.execute(
                "SELECT 1 FROM video_categories WHERE video_id = ? LIMIT 1", (member_id,),
            ).fetchone():
                with connection:
                    connection.execute("UPDATE videos SET status = 'unprocessed' WHERE id = ?", (member_id,))
        with connection:
            connection.execute("UPDATE video_groups SET folder_rel_path = ? WHERE id = ?",
                               (folder.relative_to(root).as_posix(), group["id"]))
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    relative = target.relative_to(root).as_posix()
    move_video(connection, library_id, root, video_id, relative, unique_category_id=category_id)
    if category_id is None and not connection.execute(
        "SELECT 1 FROM video_categories WHERE video_id = ? LIMIT 1", (video_id,),
    ).fetchone():
        with connection:
            connection.execute("UPDATE videos SET status = 'unprocessed' WHERE id = ?", (video_id,))
    return target


def plan_unique_assignment(
    connection: sqlite3.Connection, library_id: str, root: Path,
    video_id: str, category_id: str | None,
) -> Path | None:
    root = _verified_root(root, library_id)
    row = connection.execute(
        "SELECT * FROM videos WHERE id = ? AND library_id = ? AND missing_since IS NULL AND deleted_at IS NULL",
        (video_id, library_id),
    ).fetchone()
    if row is None:
        raise FileOperationError("Video is unavailable")
    group = group_for_video(connection, video_id)
    if group is not None:
        members = group_members(connection, group["id"])
        if all(member["unique_category_id"] == category_id for member in members):
            return None
        _, destinations = _plan_group_assignment(connection, root, group, category_id)
        return dict(destinations)[video_id]
    if row["unique_category_id"] == category_id:
        return None
    if category_id is None:
        base = root / "匯入" / datetime.now().strftime("%Y-%m")
    else:
        category = connection.execute(
            "SELECT * FROM unique_categories WHERE id = ?", (category_id,),
        ).fetchone()
        if category is None:
            raise FileOperationError("Unique category does not exist")
        base = root / "唯一分類" / category["folder_name"] / _two_month_period(row["added_at"])
    target = _destination(root, base, row["file_name"])
    return target


def _plan_group_assignment(connection: sqlite3.Connection, root: Path, group: sqlite3.Row,
                           category_id: str | None) -> tuple[Path, list[tuple[str, Path]]]:
    members = group_members(connection, group["id"])
    first = members[0]
    if category_id is None:
        base = root / "匯入" / datetime.now().strftime("%Y-%m")
    else:
        category = connection.execute("SELECT * FROM unique_categories WHERE id = ?", (category_id,)).fetchone()
        if category is None:
            raise FileOperationError("唯一分類不存在")
        base = root / "唯一分類" / category["folder_name"] / _two_month_period(first["added_at"])
    base = choose_group_bucket(root, base)
    stem = Path(group["folder_rel_path"]).name if group["folder_rel_path"] else Path(first["file_name"]).stem
    for parent in (base, *base.parents):
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise FileOperationError("影片組目的地含符號連結")
    folder = base / stem
    serial = 2
    while folder.exists():
        folder = base / f"{stem} ({serial})"
        serial += 1
    if not folder.resolve(strict=False).is_relative_to(root):
        raise FileOperationError("影片組目的地超出管理根目錄")
    used: set[str] = set()
    destinations: list[tuple[str, Path]] = []
    for member in members:
        original = Path(member["file_name"])
        name = original.name
        number = 2
        while name.casefold() in used:
            name = f"{original.stem} ({number}){original.suffix}"
            number += 1
        used.add(name.casefold())
        destinations.append((member["id"], folder / name))
    return folder, destinations


def delete_unique_category(
    connection: sqlite3.Connection, library_id: str, root: Path, category_id: str,
    *, progress: Callable[[int, int], bool] | None = None,
) -> int:
    """Move assigned videos to unassigned buckets before deleting the category."""
    root = _verified_root(root, library_id)
    category = connection.execute("SELECT folder_name FROM unique_categories WHERE id = ?", (category_id,)).fetchone()
    if category is None:
        raise ValueError("唯一分類不存在")
    ids = [row[0] for row in connection.execute(
        "SELECT id FROM videos WHERE library_id = ? AND unique_category_id = ? AND deleted_at IS NULL AND missing_since IS NULL",
        (library_id, category_id),
    )]
    for index, video_id in enumerate(ids, 1):
        assign_unique_category(connection, library_id, root, video_id, None)
        if progress is not None and not progress(index, len(ids)):
            raise FileOperationError("已停止刪除；已搬移的影片保持新位置，其餘仍屬於原唯一分類")
    with connection:
        connection.execute(
            """UPDATE videos SET unique_category_id = NULL,
               status = CASE WHEN NOT EXISTS (
                   SELECT 1 FROM video_categories vc WHERE vc.video_id = videos.id
               ) THEN 'unprocessed' ELSE status END
               WHERE library_id = ? AND unique_category_id = ? AND missing_since IS NOT NULL""",
            (library_id, category_id),
        )
        connection.execute("DELETE FROM unique_categories WHERE id = ?", (category_id,))
    category_folder = root / "唯一分類" / category["folder_name"]
    if category_folder.is_dir() and not category_folder.is_symlink():
        for current, _, _ in os.walk(category_folder, topdown=False, followlinks=False):
            folder = Path(current)
            if not folder.is_symlink():
                try:
                    folder.rmdir()  # Only empty directories; never delete a file.
                except OSError:
                    pass
    return len(ids)
