"""Queries and metadata edits; original MP4 files are never touched here."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import uuid4

from videomanager.db.database import utc_now


SORT_COLUMNS = {
    "name": "v.file_name COLLATE NOCASE",
    "added": "v.added_at",
    "modified": "v.mtime_ns",
    "duration": "v.duration_ms",
    "size": "v.file_size",
    "rating": "v.rating",
}


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_videos(
    connection: sqlite3.Connection,
    library_id: str,
    filters: dict | None = None,
    *,
    sort: str = "added",
    descending: bool = True,
    limit: int = 5000,
    offset: int = 0,
) -> list[sqlite3.Row]:
    filters = filters or {}
    if sort not in SORT_COLUMNS:
        raise ValueError("Unsupported sort key")
    if not 1 <= limit <= 5000 or offset < 0:
        raise ValueError("Invalid pagination")
    conditions = ["v.library_id = ?", "v.deleted_at IS NULL"]
    params: list[object] = [library_id]
    if filters.get("missing_only", False):
        conditions.append("v.missing_since IS NOT NULL")
    elif not filters.get("include_missing", False):
        conditions.append("v.missing_since IS NULL")
    if text := filters.get("text"):
        conditions.append("(v.display_title LIKE ? ESCAPE '\\' OR v.file_name LIKE ? ESCAPE '\\' OR v.notes LIKE ? ESCAPE '\\')")
        pattern = "%" + _escape_like(str(text)) + "%"
        params.extend([pattern] * 3)
    if status := filters.get("status"):
        statuses = [status] if isinstance(status, str) else list(status)
        if any(item not in {"unprocessed", "in_progress", "done"} for item in statuses):
            raise ValueError("Invalid status filter")
        if statuses:
            conditions.append(f"v.status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
    if "favorite" in filters:
        conditions.append("v.is_favorite = ?")
        params.append(int(bool(filters["favorite"])))
    if "unique_category_id" in filters:
        if filters["unique_category_id"] is None:
            conditions.append("v.unique_category_id IS NULL")
        else:
            conditions.append("v.unique_category_id = ?")
            params.append(filters["unique_category_id"])
    if filters.get("video_group_id"):
        conditions.append("gm.group_id = ?")
        params.append(filters["video_group_id"])
    ranges = {
        "duration_min_ms": ("v.duration_ms", ">="),
        "duration_max_ms": ("v.duration_ms", "<="),
        "size_min": ("v.file_size", ">="),
        "size_max": ("v.file_size", "<="),
        "added_from": ("v.added_at", ">="),
        "added_to": ("v.added_at", "<="),
        "modified_from_ns": ("v.mtime_ns", ">="),
        "modified_to_ns": ("v.mtime_ns", "<="),
        "rating_min": ("v.rating", ">="),
        "rating_max": ("v.rating", "<="),
        "width_min": ("v.width", ">="),
        "height_min": ("v.height", ">="),
    }
    for key, (column, operator) in ranges.items():
        if filters.get(key) is not None:
            conditions.append(f"{column} {operator} ?")
            params.append(filters[key])
    for tag_id in filters.get("tag_all", []):
        conditions.append("EXISTS (SELECT 1 FROM video_tags vt WHERE vt.video_id = v.id AND vt.tag_id = ?)")
        params.append(tag_id)
    if any_ids := filters.get("tag_any"):
        conditions.append(f"EXISTS (SELECT 1 FROM video_tags vt WHERE vt.video_id = v.id AND vt.tag_id IN ({','.join('?' for _ in any_ids)}))")
        params.extend(any_ids)
    if not_ids := filters.get("tag_not"):
        conditions.append(f"NOT EXISTS (SELECT 1 FROM video_tags vt WHERE vt.video_id = v.id AND vt.tag_id IN ({','.join('?' for _ in not_ids)}))")
        params.extend(not_ids)
    if category_ids := filters.get("category_ids"):
        ids = set(category_ids)
        if filters.get("include_descendants"):
            frontier = list(ids)
            while frontier:
                children = connection.execute(
                    f"SELECT id FROM categories WHERE parent_id IN ({','.join('?' for _ in frontier)})",
                    frontier,
                ).fetchall()
                frontier = [row[0] for row in children if row[0] not in ids]
                ids.update(frontier)
        conditions.append(f"EXISTS (SELECT 1 FROM video_categories vc WHERE vc.video_id = v.id AND vc.category_id IN ({','.join('?' for _ in ids)}))")
        params.extend(sorted(ids))
    order = SORT_COLUMNS[sort] + (" DESC" if descending else " ASC") + ", v.id ASC"
    sql = (
        """SELECT v.*, j.state AS thumbnail_state, j.cache_signature AS thumbnail_signature,
                  gm.group_id, gm.position AS group_position, g.name AS group_name,
                  (SELECT COUNT(*) FROM video_group_members WHERE group_id = gm.group_id) AS group_size
           FROM videos v LEFT JOIN thumbnail_jobs j ON j.video_id = v.id
           LEFT JOIN video_group_members gm ON gm.video_id = v.id
           LEFT JOIN video_groups g ON g.id = gm.group_id WHERE """
        + " AND ".join(conditions) + " ORDER BY " + order + " LIMIT ? OFFSET ?"
    )
    return connection.execute(sql, (*params, limit, offset)).fetchall()


def video_by_id(connection: sqlite3.Connection, video_id: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()


def group_member_ids(connection: sqlite3.Connection, group_id: str) -> list[str]:
    return [row[0] for row in connection.execute(
        "SELECT video_id FROM video_group_members WHERE group_id = ? ORDER BY position", (group_id,),
    )]


def save_video_details(
    connection: sqlite3.Connection, video_id: str, *, title: str, notes: str,
    rating: int, is_favorite: bool, recorded_at: str | None, status: str,
    tag_ids: Iterable[str], group_cover: bool = False,
) -> None:
    """Save a part's title and the metadata shared by its whole video group atomically."""
    title = title.strip()
    if not title:
        raise ValueError("名稱不可空白。")
    if not 0 <= rating <= 5 or status not in {"unprocessed", "in_progress", "done"}:
        raise ValueError("影片資料無效。")
    member = connection.execute(
        "SELECT group_id FROM video_group_members WHERE video_id = ?", (video_id,),
    ).fetchone()
    group_id = member[0] if member else None
    if group_cover and not group_id:
        raise ValueError("找不到影片組。")
    members = group_member_ids(connection, group_id) if group_id else [video_id]
    if not members:
        raise ValueError("找不到影片。")
    tags = set(tag_ids)
    with connection:
        if group_cover:
            connection.execute("UPDATE video_groups SET name = ? WHERE id = ?", (title, group_id))
        else:
            connection.execute("UPDATE videos SET display_title = ? WHERE id = ?", (title, video_id))
        connection.executemany(
            """UPDATE videos SET notes = ?, rating = ?, is_favorite = ?,
               recorded_at = ?, status = ? WHERE id = ?""",
            ((notes, rating, int(is_favorite), recorded_at, status, member_id) for member_id in members),
        )
        connection.executemany("DELETE FROM video_tags WHERE video_id = ?", ((member_id,) for member_id in members))
        connection.executemany(
            "INSERT INTO video_tags(video_id, tag_id) VALUES (?, ?)",
            ((member_id, tag_id) for member_id in members for tag_id in tags),
        )


def update_video(connection: sqlite3.Connection, video_id: str, **changes: object) -> None:
    allowed = {"display_title", "notes", "rating", "is_favorite", "recorded_at", "status"}
    if not changes or set(changes) - allowed:
        raise ValueError("Unsupported metadata field")
    if "status" in changes and changes["status"] not in {"unprocessed", "in_progress", "done"}:
        raise ValueError("Invalid organization status")
    if "rating" in changes and changes["rating"] is not None and not 0 <= int(changes["rating"]) <= 5:
        raise ValueError("Rating must be from 0 to 5")
    with connection:
        connection.execute(
            "UPDATE videos SET " + ", ".join(f"{key} = ?" for key in changes) + " WHERE id = ?",
            (*changes.values(), video_id),
        )


def list_tag_groups(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM tag_groups ORDER BY sort_order, name").fetchall()


def list_tags(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT t.*, g.name AS group_name FROM tags t JOIN tag_groups g ON g.id = t.group_id ORDER BY g.sort_order, t.sort_order, t.name").fetchall()


def list_categories(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()


def list_unique_categories(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM unique_categories ORDER BY name COLLATE NOCASE").fetchall()


def create_unique_category(connection: sqlite3.Connection, name: str) -> str:
    name = name.strip()
    stem = name.split(".", 1)[0].upper()
    if (not name or name in {".", ".."} or name[-1] in {".", " "}
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or stem in {"CON", "PRN", "AUX", "NUL"}
            or re.fullmatch(r"(?:COM|LPT)[1-9]", stem)):
        raise ValueError("名稱不可作為 Windows 資料夾名稱")
    identifier = str(uuid4())
    with connection:
        connection.execute(
            "INSERT INTO unique_categories(id, name, folder_name) VALUES (?, ?, ?)",
            (identifier, name, name),
        )
    return identifier


def create_tag_group(connection: sqlite3.Connection, name: str) -> str:
    identifier = str(uuid4())
    with connection:
        connection.execute("INSERT INTO tag_groups(id, name) VALUES (?, ?)", (identifier, name.strip()))
    return identifier


def ensure_default_tag_group(connection: sqlite3.Connection) -> str:
    row = connection.execute("SELECT id FROM tag_groups WHERE name = ?", ("一般標籤",)).fetchone()
    if row:
        return row[0]
    return create_tag_group(connection, "一般標籤")


def rename_tag_group(connection: sqlite3.Connection, group_id: str, name: str) -> None:
    with connection:
        connection.execute("UPDATE tag_groups SET name = ? WHERE id = ?", (name.strip(), group_id))


def set_tag_group_required(connection: sqlite3.Connection, group_id: str, required: bool) -> None:
    with connection:
        updated = connection.execute(
            "UPDATE tag_groups SET is_required = ? WHERE id = ?", (int(required), group_id),
        )
        if updated.rowcount != 1:
            raise ValueError("找不到標籤分類")


def delete_tag_group(connection: sqlite3.Connection, group_id: str) -> None:
    if connection.execute("SELECT 1 FROM tags WHERE group_id = ? LIMIT 1", (group_id,)).fetchone():
        raise ValueError("請先移除群組內的標籤")
    with connection:
        connection.execute("DELETE FROM tag_groups WHERE id = ?", (group_id,))


def move_sort_order(connection: sqlite3.Connection, table: str, ordered_ids: list[str]) -> None:
    if table not in {"tag_groups", "tags", "categories"}:
        raise ValueError("Unsupported ordering table")
    with connection:
        connection.executemany(
            f"UPDATE {table} SET sort_order = ? WHERE id = ?",
            ((index, identifier) for index, identifier in enumerate(ordered_ids)),
        )


def create_tag(connection: sqlite3.Connection, group_id: str, name: str, color: str | None = None) -> str:
    identifier = str(uuid4())
    with connection:
        connection.execute("INSERT INTO tags(id, group_id, name, color) VALUES (?, ?, ?, ?)", (identifier, group_id, name.strip(), color))
    return identifier


def rename_tag(connection: sqlite3.Connection, tag_id: str, name: str, color: str | None = None) -> None:
    with connection:
        connection.execute("UPDATE tags SET name = ?, color = ? WHERE id = ?", (name.strip(), color, tag_id))


def move_tag_to_group(connection: sqlite3.Connection, tag_id: str, group_id: str) -> None:
    tag = connection.execute("SELECT name, group_id FROM tags WHERE id = ?", (tag_id,)).fetchone()
    if tag is None:
        raise ValueError("找不到標籤")
    if not connection.execute("SELECT 1 FROM tag_groups WHERE id = ?", (group_id,)).fetchone():
        raise ValueError("找不到目標標籤分類")
    if tag["group_id"] == group_id:
        return
    if connection.execute(
        "SELECT 1 FROM tags WHERE group_id = ? AND name = ?", (group_id, tag["name"]),
    ).fetchone():
        raise ValueError("目標分類已有同名標籤；請先改名或合併")
    next_order = connection.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM tags WHERE group_id = ?", (group_id,),
    ).fetchone()[0]
    with connection:
        connection.execute("UPDATE tags SET group_id = ?, sort_order = ? WHERE id = ?",
                           (group_id, next_order, tag_id))


def merge_tags(connection: sqlite3.Connection, source_id: str, target_id: str) -> None:
    if source_id == target_id:
        raise ValueError("Choose two different tags")
    with connection:
        connection.execute(
            "INSERT OR IGNORE INTO video_tags(video_id, tag_id) SELECT video_id, ? FROM video_tags WHERE tag_id = ?",
            (target_id, source_id),
        )
        connection.execute("DELETE FROM tags WHERE id = ?", (source_id,))


def delete_tag(connection: sqlite3.Connection, tag_id: str) -> None:
    with connection:
        connection.execute("DELETE FROM tags WHERE id = ?", (tag_id,))


def create_category(connection: sqlite3.Connection, name: str, parent_id: str | None = None) -> str:
    identifier = str(uuid4())
    with connection:
        connection.execute("INSERT INTO categories(id, parent_id, name) VALUES (?, ?, ?)", (identifier, parent_id, name.strip()))
    return identifier


def rename_category(connection: sqlite3.Connection, category_id: str, name: str) -> None:
    with connection:
        connection.execute("UPDATE categories SET name = ? WHERE id = ?", (name.strip(), category_id))


@dataclass(frozen=True)
class CategoryDeleteImpact:
    categories: int
    affected_videos: int
    returned_to_unprocessed: int


def _category_deletion_plan(connection: sqlite3.Connection, category_id: str) -> tuple[list[str], set[str], set[str]]:
    tree = connection.execute(
        """WITH RECURSIVE descendants(id, depth) AS (
               SELECT id, 0 FROM categories WHERE id = ?
               UNION ALL
               SELECT c.id, d.depth + 1 FROM categories c
               JOIN descendants d ON c.parent_id = d.id
           ) SELECT id FROM descendants ORDER BY depth DESC""",
        (category_id,),
    ).fetchall()
    if not tree:
        raise ValueError("分類不存在")
    category_ids = [row[0] for row in tree]
    targets = set(category_ids)
    placeholders = ",".join("?" for _ in category_ids)
    affected = {row[0] for row in connection.execute(
        f"SELECT DISTINCT video_id FROM video_categories WHERE category_id IN ({placeholders})",
        category_ids,
    )}
    orphaned: set[str] = set()
    for video_id in affected:
        still_classified = connection.execute(
            f"""SELECT 1 FROM video_categories
                WHERE video_id = ? AND category_id NOT IN ({placeholders}) LIMIT 1""",
            (video_id, *category_ids),
        ).fetchone()
        has_unique = connection.execute(
            "SELECT 1 FROM videos WHERE id = ? AND unique_category_id IS NOT NULL", (video_id,),
        ).fetchone()
        if not still_classified and not has_unique:
            orphaned.add(video_id)
    return category_ids, affected, orphaned


def category_delete_impact(connection: sqlite3.Connection, category_id: str) -> CategoryDeleteImpact:
    category_ids, affected, orphaned = _category_deletion_plan(connection, category_id)
    return CategoryDeleteImpact(len(category_ids), len(affected), len(orphaned))


def delete_category(connection: sqlite3.Connection, category_id: str) -> CategoryDeleteImpact:
    """Delete a category subtree, then return newly unclassified videos to triage."""
    with connection:
        category_ids, affected, orphaned = _category_deletion_plan(connection, category_id)
        for identifier in category_ids:
            connection.execute("DELETE FROM categories WHERE id = ?", (identifier,))
        connection.executemany(
            "UPDATE videos SET status = 'unprocessed' WHERE id = ?",
            ((video_id,) for video_id in orphaned),
        )
    return CategoryDeleteImpact(len(category_ids), len(affected), len(orphaned))


def video_tag_ids(connection: sqlite3.Connection, video_id: str) -> set[str]:
    return {row[0] for row in connection.execute("SELECT tag_id FROM video_tags WHERE video_id = ?", (video_id,))}


def video_category_ids(connection: sqlite3.Connection, video_id: str) -> set[str]:
    return {row[0] for row in connection.execute("SELECT category_id FROM video_categories WHERE video_id = ?", (video_id,))}


def set_video_relations(
    connection: sqlite3.Connection,
    video_id: str,
    *,
    tag_ids: Iterable[str] | None = None,
    category_ids: Iterable[str] | None = None,
) -> None:
    group = connection.execute(
        "SELECT group_id FROM video_group_members WHERE video_id = ?", (video_id,),
    ).fetchone()
    members = [row[0] for row in connection.execute(
        "SELECT video_id FROM video_group_members WHERE group_id = ? ORDER BY position", (group[0],),
    )] if group else [video_id]
    with connection:
        if tag_ids is not None:
            connection.executemany("DELETE FROM video_tags WHERE video_id = ?", ((item,) for item in members))
            connection.executemany("INSERT INTO video_tags(video_id, tag_id) VALUES (?, ?)",
                                   ((item, tag_id) for item in members for tag_id in set(tag_ids)))
        if category_ids is not None:
            connection.executemany("DELETE FROM video_categories WHERE video_id = ?", ((item,) for item in members))
            connection.executemany("INSERT INTO video_categories(video_id, category_id) VALUES (?, ?)",
                                   ((item, cat_id) for item in members for cat_id in set(category_ids)))


def batch_add_relations(
    connection: sqlite3.Connection,
    video_ids: Iterable[str],
    *,
    tag_ids: Iterable[str] = (),
    category_ids: Iterable[str] = (),
) -> None:
    videos = set(video_ids)
    tags = set(tag_ids)
    categories = set(category_ids)
    with connection:
        connection.executemany(
            "INSERT OR IGNORE INTO video_tags(video_id, tag_id) VALUES (?, ?)",
            ((video_id, tag_id) for video_id in videos for tag_id in tags),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO video_categories(video_id, category_id) VALUES (?, ?)",
            ((video_id, category_id) for video_id in videos for category_id in categories),
        )


def save_filter(connection: sqlite3.Connection, name: str, filters: dict) -> str:
    identifier = str(uuid4())
    with connection:
        connection.execute(
            "INSERT INTO saved_filters(id, name, filter_json, created_at) VALUES (?, ?, ?, ?)",
            (identifier, name.strip(), json.dumps(filters, ensure_ascii=False), utc_now()),
        )
    return identifier


def list_saved_filters(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM saved_filters ORDER BY name").fetchall()
