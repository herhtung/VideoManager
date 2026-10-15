"""Read and create a small UUID marker without touching video files."""

from __future__ import annotations

import json
import os
import ctypes
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4


MARKER_RELATIVE_PATH = Path(".videomanager") / "library.json"


class MarkerError(ValueError):
    pass


class LibraryAmbiguousError(MarkerError):
    pass


@dataclass(frozen=True)
class LibraryMarker:
    library_id: str
    schema_version: int
    created_at: str

    def __post_init__(self) -> None:
        UUID(self.library_id)
        if self.schema_version != 1:
            raise MarkerError("Unsupported library marker version")
        timestamp = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise MarkerError("Marker timestamp must include a timezone")


def marker_path(root: Path) -> Path:
    return root / MARKER_RELATIVE_PATH


def read_marker(root: Path) -> LibraryMarker | None:
    path = marker_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        return LibraryMarker(**data)
    except (OSError, ValueError, TypeError) as exc:
        raise MarkerError(f"Invalid library marker at {path}: {exc}") from exc


def create_marker(root: Path) -> LibraryMarker:
    """Create only the marker. Existing markers are never replaced."""
    if not root.is_dir():
        raise MarkerError(f"Library root is not a directory: {root}")
    marker = LibraryMarker(str(uuid4()), 1, datetime.now(timezone.utc).isoformat())
    path = marker_path(root)
    path.parent.mkdir(exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(asdict(marker), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise MarkerError(f"Library marker already exists: {path}") from exc
    return marker


def locate_library(library_id: str, candidate_roots: list[Path]) -> Path | None:
    """Check only explicit candidate roots; never recurse through drives."""
    UUID(library_id)
    matches: list[Path] = []
    seen: set[str] = set()
    for root in candidate_roots:
        key = os.path.normcase(str(root.resolve(strict=False)))
        if key in seen:
            continue
        seen.add(key)
        try:
            marker = read_marker(root)
        except (OSError, MarkerError):
            continue
        if marker and marker.library_id == library_id:
            matches.append(root)
    if len(matches) > 1:
        raise LibraryAmbiguousError("Several library roots have the same ID; choose one manually")
    return matches[0] if matches else None


def drive_letter_candidates(last_root: Path, letters: list[str] | None = None) -> list[Path]:
    """Probe the same relative folder on mounted drives; never walk entire disks."""
    if os.name != "nt" or not last_root.drive:
        return [last_root]
    if letters is None:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        letters = [chr(ord("A") + index) for index in range(26) if mask & (1 << index)]
    suffix = last_root.relative_to(last_root.anchor)
    return [last_root] + [Path(f"{letter.upper()}:\\") / suffix for letter in letters]
