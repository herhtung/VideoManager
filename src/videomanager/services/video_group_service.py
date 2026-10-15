"""Link MP4 segments while keeping each source file and card independent."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from videomanager.db.database import utc_now
from videomanager.services.file_operations import _verified_root, move_video
from videomanager.services.import_service import MAX_FILES_PER_FOLDER, choose_group_bucket


class VideoGroupError(ValueError):
    pass


def group_for_video(connection: sqlite3.Connection, video_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT g.*, m.position,
                  (SELECT COUNT(*) FROM video_group_members WHERE group_id = g.id) AS part_count
           FROM video_group_members m JOIN video_groups g ON g.id = m.group_id
           WHERE m.video_id = ?""",
        (video_id,),
    ).fetchone()


def list_groups(connection: sqlite3.Connection, library_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT g.*, COUNT(m.video_id) AS part_count
           FROM video_groups g JOIN video_group_members m ON m.group_id = g.id
           WHERE g.library_id = ? GROUP BY g.id ORDER BY g.name COLLATE NOCASE""",
        (library_id,),
    ).fetchall()


def group_members(connection: sqlite3.Connection, group_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT v.*, m.position FROM video_group_members m
           JOIN videos v ON v.id = m.video_id
           WHERE m.group_id = ? ORDER BY m.position""",
        (group_id,),
    ).fetchall()


def expand_group_ids(connection: sqlite3.Connection, video_ids: Iterable[str]) -> list[str]:
    chosen = list(dict.fromkeys(video_ids))
    if not chosen:
        return []
    groups = [row[0] for row in connection.execute(
        f"SELECT DISTINCT group_id FROM video_group_members WHERE video_id IN ({','.join('?' for _ in chosen)})",
        chosen,
    )]
    if groups:
        chosen.extend(row[0] for row in connection.execute(
            f"SELECT video_id FROM video_group_members WHERE group_id IN ({','.join('?' for _ in groups)}) ORDER BY group_id, position",
            groups,
        ))
    return list(dict.fromkeys(chosen))


def _validated_members(connection: sqlite3.Connection, library_id: str, video_ids: list[str],
                       group_id: str | None = None) -> list[sqlite3.Row]:
    if len(video_ids) < 2 or len(set(video_ids)) != len(video_ids):
        raise VideoGroupError("影片組至少需要兩個不同的 MP4 分段。")
    rows = connection.execute(
        f"""SELECT v.id, v.missing_since, v.deleted_at, m.group_id
            FROM videos v LEFT JOIN video_group_members m ON m.video_id = v.id
            WHERE v.library_id = ? AND v.id IN ({','.join('?' for _ in video_ids)})""",
        (library_id, *video_ids),
    ).fetchall()
    if len(rows) != len(video_ids) or any(row["missing_since"] or row["deleted_at"] for row in rows):
        raise VideoGroupError("只能連結目前影片庫中可用的影片。")
    if any(row["group_id"] not in (None, group_id) for row in rows):
        raise VideoGroupError("所選影片已有其他影片組，請先從原組移除。")
    return rows


def _share_relations(connection: sqlite3.Connection, video_ids: list[str]) -> None:
    placeholders = ",".join("?" for _ in video_ids)
    tags = [row[0] for row in connection.execute(
        f"SELECT DISTINCT tag_id FROM video_tags WHERE video_id IN ({placeholders})", video_ids,
    )]
    connection.executemany(
        "INSERT OR IGNORE INTO video_tags(video_id, tag_id) VALUES (?, ?)",
        ((video_id, tag_id) for video_id in video_ids for tag_id in tags),
    )


def create_video_group(connection: sqlite3.Connection, library_id: str, name: str,
                       ordered_video_ids: Iterable[str], *, folder_rel_path: str | None = None) -> str:
    name = name.strip()
    if not name:
        raise VideoGroupError("請輸入影片組名稱。")
    ids = list(ordered_video_ids)
    _validated_members(connection, library_id, ids)
    identifier = str(uuid4())
    with connection:
        connection.execute(
            "INSERT INTO video_groups(id, library_id, name, created_at, folder_rel_path) VALUES (?, ?, ?, ?, ?)",
            (identifier, library_id, name, utc_now(), folder_rel_path),
        )
        connection.executemany(
            "INSERT INTO video_group_members(video_id, group_id, position) VALUES (?, ?, ?)",
            ((video_id, identifier, position) for position, video_id in enumerate(ids, 1)),
        )
        _share_relations(connection, ids)
    return identifier


def plan_group_folder(connection: sqlite3.Connection, library_id: str, root: Path,
                      ordered_video_ids: Iterable[str]) -> tuple[Path, list[tuple[str, Path]]]:
    root = _verified_root(root, library_id)
    ids = list(ordered_video_ids)
    _validated_members(connection, library_id, ids)
    rows = {row["id"]: row for row in connection.execute(
        f"SELECT id, file_name, relative_path, added_at FROM videos WHERE id IN ({','.join('?' for _ in ids)})", ids,
    )}
    first = root / rows[ids[0]]["relative_path"]
    stem = first.stem
    month = datetime.fromisoformat(rows[ids[0]]["added_at"]).astimezone().strftime("%Y-%m")
    bucket = choose_group_bucket(root, root / "匯入" / month)
    folder = bucket / stem
    serial = 2
    while folder.exists():
        folder = bucket / f"{stem} ({serial})"
        serial += 1
    if not folder.resolve(strict=False).is_relative_to(root):
        raise VideoGroupError("影片組資料夾超出管理根目錄。")
    used: set[str] = set()
    destinations: list[tuple[str, Path]] = []
    for position, video_id in enumerate(ids):
        file_name = rows[video_id]["file_name"]
        candidate = file_name
        count = 2
        while candidate.casefold() in used:
            part = Path(file_name)
            candidate = f"{part.stem} ({count}){part.suffix}"
            count += 1
        used.add(candidate.casefold())
        leaf = folder if len(ids) <= MAX_FILES_PER_FOLDER else folder / f"{position // MAX_FILES_PER_FOLDER + 1:03d}"
        destinations.append((video_id, leaf / candidate))
    return folder, destinations


def create_group_in_folder(connection: sqlite3.Connection, library_id: str, root: Path,
                           name: str, ordered_video_ids: Iterable[str], *,
                           expected_folder: Path | None = None) -> str:
    """Journal each MP4 move. A partial failure leaves a repairable group."""
    root = _verified_root(root, library_id)
    ids = list(ordered_video_ids)
    folder, destinations = plan_group_folder(connection, library_id, root, ids)
    if expected_folder is not None and folder != expected_folder:
        raise VideoGroupError("影片組目的地已變更，請重新確認。")
    folder.mkdir(parents=True, exist_ok=False)
    group_id = create_video_group(
        connection, library_id, name, ids, folder_rel_path=folder.relative_to(root).as_posix(),
    )
    for video_id, destination in destinations:
        destination.parent.mkdir(parents=True, exist_ok=True)
        move_video(connection, library_id, root, video_id, destination.relative_to(root).as_posix())
    return group_id


def organize_group_folder(connection: sqlite3.Connection, library_id: str, root: Path,
                          group_id: str) -> int:
    root = _verified_root(root, library_id)
    group = connection.execute(
        "SELECT folder_rel_path FROM video_groups WHERE id = ? AND library_id = ?", (group_id, library_id),
    ).fetchone()
    if group is None or not group["folder_rel_path"]:
        raise VideoGroupError("影片組沒有指定資料夾。")
    folder = root / group["folder_rel_path"]
    if folder.is_symlink() or not folder.resolve(strict=False).is_relative_to(root):
        raise VideoGroupError("影片組資料夾不可用。")
    folder.mkdir(parents=True, exist_ok=True)
    moved = 0
    for row in group_members(connection, group_id):
        if (root / row["relative_path"]).is_relative_to(folder):
            continue
        destination = folder / row["file_name"]
        serial = 2
        while destination.exists():
            source = Path(row["file_name"])
            destination = folder / f"{source.stem} ({serial}){source.suffix}"
            serial += 1
        move_video(connection, library_id, root, row["id"], destination.relative_to(root).as_posix())
        moved += 1
    return moved


def set_group_members(connection: sqlite3.Connection, group_id: str,
                      ordered_video_ids: Iterable[str]) -> None:
    group = connection.execute("SELECT library_id FROM video_groups WHERE id = ?", (group_id,)).fetchone()
    if group is None:
        raise VideoGroupError("找不到影片組。")
    ids = list(ordered_video_ids)
    _validated_members(connection, group["library_id"], ids, group_id)
    with connection:
        connection.execute("DELETE FROM video_group_members WHERE group_id = ?", (group_id,))
        connection.executemany(
            "INSERT INTO video_group_members(video_id, group_id, position) VALUES (?, ?, ?)",
            ((video_id, group_id, position) for position, video_id in enumerate(ids, 1)),
        )
        _share_relations(connection, ids)


def rename_group(connection: sqlite3.Connection, group_id: str, name: str) -> None:
    name = name.strip()
    if not name:
        raise VideoGroupError("影片組名稱不可空白。")
    with connection:
        cursor = connection.execute("UPDATE video_groups SET name = ? WHERE id = ?", (name, group_id))
        if not cursor.rowcount:
            raise VideoGroupError("找不到影片組。")


def dissolve_group(connection: sqlite3.Connection, group_id: str) -> None:
    with connection:
        connection.execute("DELETE FROM video_groups WHERE id = ?", (group_id,))
