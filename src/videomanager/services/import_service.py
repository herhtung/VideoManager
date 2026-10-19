"""Plan and move outside MP4 files into bounded, app-managed folders."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable
from uuid import uuid4

from videomanager.services.file_operations import FileOperationError, _verified_root


MAX_FILES_PER_FOLDER = 50


def cleaned_title(stem: str, remove_phrases: Iterable[str] = ()) -> str:
    """Remove configured literal phrases from a video or group display name."""
    for phrase in sorted((phrase for phrase in remove_phrases if phrase), key=len, reverse=True):
        stem = re.sub(re.escape(phrase), "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s+", " ", stem).strip(" ._-") or "未命名影片"
    if stem.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
        r"(?:COM|LPT)[1-9]", stem.split(".", 1)[0], re.IGNORECASE
    ):
        stem = f"影片-{stem}"
    return stem


def cleaned_import_name(source: Path, remove_phrases: Iterable[str] = ()) -> str:
    """Remove configured literal phrases from the stem, preserving the MP4 extension."""
    return cleaned_title(source.stem, remove_phrases) + source.suffix


def choose_group_bucket(root: Path, base: Path) -> Path:
    """Keep at most 50 immediate videos or group folders in each numbered bucket."""
    number = 1
    while True:
        folder = base / f"{number:03d}"
        for parent in (folder, *folder.parents):
            if parent == root.parent:
                break
            if parent.is_symlink():
                raise FileOperationError(f"影片組目的地含符號連結：{parent}")
        if folder.exists() and not folder.is_dir():
            raise FileOperationError(f"影片組目的地被檔案佔用：{folder}")
        count = sum(1 for entry in folder.iterdir() if entry.is_dir() or
                    (entry.is_file() and entry.suffix.lower() == ".mp4")) if folder.exists() else 0
        if count < MAX_FILES_PER_FOLDER:
            return folder
        number += 1


class ImportCancelled(FileOperationError):
    pass


@dataclass(frozen=True)
class ImportItem:
    source: Path
    destination: Path
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class ImportResult:
    destination: Path
    source_retained: bool = False


def plan_import(
    root: Path, library_id: str, sources: Iterable[Path], *, month: str | None = None,
    remove_phrases: Iterable[str] = (),
) -> list[ImportItem]:
    """Allocate at most 50 MP4s per numbered folder, without changing files."""
    root = _verified_root(root, library_id)
    remove_phrases = tuple(remove_phrases)
    month = month or datetime.now().strftime("%Y-%m")
    if len(month) != 7 or month[4] != "-" or not (month[:4] + month[5:]).isdigit():
        raise FileOperationError("Invalid import month")
    base = root / "匯入" / month
    seen_sources: set[str] = set()
    reserved: set[str] = set()
    items: list[ImportItem] = []
    folder_no = 1
    for raw in sources:
        source = Path(raw)
        if source.is_symlink() or not source.is_file() or source.suffix.lower() != ".mp4":
            raise FileOperationError(f"來源不是可匯入的 MP4：{source}")
        source = source.resolve(strict=True)
        if source.is_relative_to(root):
            raise FileOperationError(f"來源已在影片庫內，請使用庫內移動：{source}")
        source_key = os.path.normcase(str(source)).casefold()
        if source_key in seen_sources:
            raise FileOperationError(f"重複選取來源：{source}")
        seen_sources.add(source_key)
        stat = source.stat()
        while True:
            folder = base / f"{folder_no:03d}"
            if folder.is_symlink() or any(parent.is_symlink() for parent in (folder.parent, folder.parent.parent)):
                raise FileOperationError(f"匯入資料夾含符號連結：{folder}")
            if folder.exists() and not folder.is_dir():
                raise FileOperationError(f"匯入資料夾路徑已被檔案佔用：{folder}")
            existing = {entry.name.casefold() for entry in folder.iterdir() if entry.is_file() and entry.suffix.lower() == ".mp4"} if folder.exists() else set()
            occupied = sum(1 for entry in folder.iterdir() if entry.is_dir() or
                           (entry.is_file() and entry.suffix.lower() == ".mp4")) if folder.exists() else 0
            planned = {Path(key).name.casefold() for key in reserved if Path(key).parent == folder}
            if occupied + len(planned) < MAX_FILES_PER_FOLDER:
                break
            folder_no += 1
        candidate = cleaned_import_name(source, remove_phrases)
        clean_stem = Path(candidate).stem
        serial = 2
        while candidate.casefold() in existing or candidate.casefold() in planned:
            candidate = f"{clean_stem} ({serial}){source.suffix}"
            serial += 1
        destination = folder / candidate
        if not destination.resolve(strict=False).is_relative_to(root):
            raise FileOperationError("Destination leaves the library")
        items.append(ImportItem(source, destination, stat.st_size, stat.st_mtime_ns))
        reserved.add(str(destination))
    return items


def plan_group_import(root: Path, library_id: str, sources: Iterable[Path], *,
                      month: str | None = None,
                      remove_phrases: Iterable[str] = ()) -> list[ImportItem]:
    """Plan one physical folder for ordered segments, named for the first MP4."""
    root = _verified_root(root, library_id)
    paths = list(sources)
    if len(paths) < 2:
        raise FileOperationError("影片組至少需要兩個 MP4 分段")
    month = month or datetime.now().strftime("%Y-%m")
    # Reuse the regular planner's source, date and duplicate validation.
    checked = plan_import(root, library_id, paths, month=month, remove_phrases=remove_phrases)
    base = choose_group_bucket(root, root / "匯入" / month)
    stem = checked[0].destination.stem
    folder = base / stem
    serial = 2
    while folder.exists():
        folder = base / f"{stem} ({serial})"
        serial += 1
    used: set[str] = set()
    result: list[ImportItem] = []
    for position, item in enumerate(checked):
        name = item.destination.name
        number = 2
        while name.casefold() in used:
            name = f"{item.destination.stem} ({number}){item.source.suffix}"
            number += 1
        used.add(name.casefold())
        leaf = folder if len(checked) <= MAX_FILES_PER_FOLDER else folder / f"{position // MAX_FILES_PER_FOLDER + 1:03d}"
        result.append(ImportItem(item.source, leaf / name, item.size, item.mtime_ns))
    return result


def import_mp4(item: ImportItem, root: Path, library_id: str, *, cancelled: Callable[[], bool] = lambda: False,
               progress: Callable[[int, int], None] = lambda done, total: None) -> ImportResult:
    """Move on one volume; otherwise copy safely, then recycle the source."""
    root = _verified_root(root, library_id)
    source, destination = item.source, item.destination
    if cancelled():
        raise ImportCancelled("已取消匯入")
    if source.is_symlink() or not source.is_file() or source.suffix.lower() != ".mp4":
        raise FileOperationError(f"來源不可用：{source}")
    stat = source.stat()
    if (stat.st_size, stat.st_mtime_ns) != (item.size, item.mtime_ns):
        raise FileOperationError(f"來源自確認後已變更：{source}")
    if source.resolve(strict=True).is_relative_to(root):
        raise FileOperationError("來源已在影片庫內")
    if not destination.resolve(strict=False).is_relative_to(root) or destination.suffix.lower() != ".mp4":
        raise FileOperationError("目的地不在影片庫內")
    for parent in (destination.parent, *destination.parent.parents):
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise FileOperationError("目的地含符號連結")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileOperationError(f"目的地已有同名檔，未覆蓋：{destination}")
    if source.stat().st_dev == destination.parent.stat().st_dev:
        if cancelled():
            raise ImportCancelled("已取消匯入")
        try:
            os.rename(source, destination)
        except OSError as exc:
            raise FileOperationError(f"搬移失敗：{exc}") from exc
        progress(item.size, item.size)
        return ImportResult(destination)

    temporary = destination.parent / f".{uuid4().hex}.importing"
    try:
        copied = 0
        with source.open("rb") as reader, temporary.open("xb") as writer:
            while True:
                if cancelled():
                    raise ImportCancelled("已取消匯入")
                block = reader.read(4 * 1024 * 1024)
                if not block:
                    break
                writer.write(block)
                copied += len(block)
                progress(copied, item.size)
            writer.flush()
            os.fsync(writer.fileno())
        current = source.stat()
        if (current.st_size, current.st_mtime_ns) != (item.size, item.mtime_ns):
            raise FileOperationError(f"複製期間來源已變更：{source}")
        if temporary.stat().st_size != item.size:
            raise FileOperationError(f"複製大小不符：{source}")
        shutil.copystat(source, temporary)
        if destination.exists():
            raise FileOperationError(f"目的地已有同名檔，未覆蓋：{destination}")
        os.rename(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    # The completed library copy is safe. If Recycle Bin is unavailable, keep
    # the original and report that a manual cleanup remains.
    try:
        from send2trash.win import send2trash
        if send2trash.__module__ != "send2trash.win.modern":
            return ImportResult(destination, source_retained=True)
        latest = source.stat()
        if (latest.st_dev, latest.st_ino, latest.st_size, latest.st_mtime_ns) != (
            stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
        ):
            return ImportResult(destination, source_retained=True)
        send2trash(str(source))
        return ImportResult(destination, source_retained=source.exists())
    except Exception:
        return ImportResult(destination, source_retained=True)
