"""Complete, read-only MP4 enumeration before any database synchronization."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from videomanager.db.database import normalize_relative_path
from videomanager.services.library_locator import MarkerError, read_marker


class ScanError(RuntimeError):
    pass


class ScanCancelled(ScanError):
    pass


@dataclass(frozen=True)
class ScannedVideo:
    relative_path: str
    path_key: str
    file_name: str
    file_size: int
    mtime_ns: int


def scan_library(
    root: Path,
    library_id: str,
    *,
    included_folders: Iterable[str] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    paused: Callable[[], bool] = lambda: False,
    progress: Callable[[int], None] = lambda count: None,
) -> list[ScannedVideo]:
    """Return a snapshot only after a successful full walk. Never follow symlinks."""
    try:
        marker = read_marker(root)
    except (OSError, MarkerError) as exc:
        raise ScanError(f"Cannot verify library marker: {exc}") from exc
    if marker is None or marker.library_id != library_id:
        raise ScanError("Library marker is missing or has a different ID")
    if not root.is_dir():
        raise ScanError("Library root is unavailable")

    found: list[ScannedVideo] = []
    keys: set[str] = set()

    def on_error(error: OSError) -> None:
        raise ScanError(f"Cannot read directory: {error}") from error

    def wait_if_paused() -> None:
        while paused() and not cancelled():
            time.sleep(0.1)
        if cancelled():
            raise ScanCancelled("Scan cancelled")

    starts = [root]
    if included_folders is not None:
        starts = []
        root_resolved = root.resolve(strict=True)
        for relative in ("匯入", "唯一分類", *included_folders):
            managed = relative in {"匯入", "唯一分類"}
            parts = Path(relative.replace("\\", "/")).parts
            if not parts or any(part in ("", ".", "..") for part in parts) or ":" in relative:
                raise ScanError(f"Invalid scan folder: {relative}")
            candidate = root.joinpath(*parts)
            if not candidate.exists():
                if managed:
                    continue
                raise ScanError(f"Selected scan folder is unavailable: {candidate}")
            if not candidate.is_dir() or any(root.joinpath(*parts[:index]).is_symlink()
                                             for index in range(1, len(parts) + 1)):
                raise ScanError(f"Selected scan folder is not a regular directory: {candidate}")
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root_resolved):
                raise ScanError(f"Selected scan folder leaves the library: {candidate}")
            starts.append(candidate)
        starts.sort(key=lambda path: len(path.parts))
        starts = [path for index, path in enumerate(starts)
                  if not any(parent == path or parent in path.parents for parent in starts[:index])]

    try:
        for start in starts:
            for directory, subdirs, filenames in os.walk(start, topdown=True, onerror=on_error, followlinks=False):
                wait_if_paused()
                subdirs[:] = [name for name in subdirs if name != ".videomanager" and not (Path(directory) / name).is_symlink()]
                for filename in filenames:
                    wait_if_paused()
                    if not filename.lower().endswith(".mp4"):
                        continue
                    absolute = Path(directory) / filename
                    if absolute.is_symlink():
                        continue
                    stat = absolute.stat()
                    if not absolute.is_file():
                        continue
                    relative, key = normalize_relative_path(str(absolute.relative_to(root)))
                    if key in keys:
                        raise ScanError(f"Case-insensitive path collision: {relative}")
                    keys.add(key)
                    found.append(ScannedVideo(relative, key, filename, stat.st_size, stat.st_mtime_ns))
                    progress(len(found))
        # A disconnected drive or replaced marker must never authorize missing marks.
        verified = read_marker(root)
        if verified is None or verified.library_id != library_id:
            raise ScanError("Library marker changed during scan")
    except OSError as exc:
        raise ScanError(f"Scan did not complete: {exc}") from exc
    return found
