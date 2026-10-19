"""Local application paths and atomic, versioned settings."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True)
class AppPaths:
    base: Path

    @classmethod
    def default(cls) -> "AppPaths":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise RuntimeError("LOCALAPPDATA is unavailable; pass an explicit data directory")
        return cls(Path(local) / "VideoManager")

    @property
    def database(self) -> Path:
        return self.base / "data" / "library.db"

    @property
    def cache(self) -> Path:
        return self.base / "cache" / "thumbnails"

    @property
    def logs(self) -> Path:
        return self.base / "logs"

    @property
    def backups(self) -> Path:
        return self.base / "backups"

    @property
    def settings(self) -> Path:
        return self.base / "settings.json"

    def ensure(self) -> None:
        for directory in (self.database.parent, self.cache, self.logs, self.backups):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Settings:
    schema_version: int = 1
    library_id: str | None = None
    last_root_path: str | None = None
    preview_mode: str = "standard"
    preview_policy: str = "auto"
    hover_delay_ms: int = 400
    related_hover_delay_ms: int = 2000
    frame_interval_ms: int = 500
    thumbnail_width: int = 180
    scan_folders: tuple[str, ...] = ()
    import_remove_phrases: tuple[str, ...] = ()
    playback_backend: str = "qt"
    # Kept only so settings written by 0.2.10 can still be loaded.
    vlc_video_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Unsupported settings version")
        if (self.library_id is None) != (self.last_root_path is None):
            raise ValueError("Library ID and path must both be set or both be absent")
        if self.library_id is not None:
            UUID(self.library_id)
        if self.preview_mode not in {"standard", "detailed", "dense"}:
            raise ValueError("Unsupported preview mode")
        if self.preview_policy not in {"auto", "run", "paused"}:
            raise ValueError("Unsupported preview policy")
        if not 100 <= self.hover_delay_ms <= 2000:
            raise ValueError("Hover delay must be 100–2000 ms")
        if not 1000 <= self.related_hover_delay_ms <= 10000:
            raise ValueError("Related video delay must be 1–10 seconds")
        if not 100 <= self.frame_interval_ms <= 3000:
            raise ValueError("Frame interval must be 100–3000 ms")
        if self.thumbnail_width not in {140, 180, 240}:
            raise ValueError("Unsupported thumbnail width")
        if not isinstance(self.scan_folders, (list, tuple)):
            raise ValueError("Scan folders must be a list")
        normalized: list[str] = []
        for relative in self.scan_folders:
            if not isinstance(relative, str):
                raise ValueError("Invalid scan folder")
            parts = relative.replace("\\", "/").split("/")
            if not relative or any(part in ("", ".", "..") for part in parts) or ":" in relative:
                raise ValueError("Scan folders must be relative paths within the library")
            normalized.append("/".join(parts))
        object.__setattr__(self, "scan_folders", tuple(dict.fromkeys(normalized)))
        if not isinstance(self.import_remove_phrases, (list, tuple)):
            raise ValueError("匯入檔名清理字句必須是清單")
        phrases: list[str] = []
        for phrase in self.import_remove_phrases:
            if not isinstance(phrase, str) or "\n" in phrase or "\r" in phrase:
                raise ValueError("匯入檔名清理字句格式不正確")
            value = phrase.strip()
            if value and value not in phrases:
                phrases.append(value)
        object.__setattr__(self, "import_remove_phrases", tuple(phrases))
        if self.playback_backend not in {"qt", "vlc", "mpv"}:
            raise ValueError("播放器預設值不正確")
        if not isinstance(self.vlc_video_ids, (list, tuple)) or any(
            not isinstance(video_id, str) or not video_id for video_id in self.vlc_video_ids
        ):
            raise ValueError("播放器偏好格式不正確")
        object.__setattr__(self, "vlc_video_ids", tuple(dict.fromkeys(self.vlc_video_ids)))


def load_settings(path: Path) -> Settings:
    if not path.exists():
        return Settings()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid settings file")
    return Settings(**data)


def save_settings(path: Path, settings: Settings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(asdict(settings), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def media_tool(name: str) -> str:
    """Prefer bundled FFmpeg tools in an onedir build, then the local PATH."""
    executable = name + (".exe" if os.name == "nt" else "")
    roots = [Path(sys.executable).parent]
    if hasattr(sys, "_MEIPASS"):
        roots.insert(0, Path(sys._MEIPASS))
    for root in roots:
        for candidate in (root / "bin" / executable, root / executable):
            if candidate.is_file():
                return str(candidate)
    return shutil.which(executable) or executable


def brand_icon_path() -> Path:
    roots = [Path(__file__).resolve().parents[2]]
    if hasattr(sys, "_MEIPASS"):
        roots.insert(0, Path(sys._MEIPASS))
    for root in roots:
        candidate = root / "assets" / "videomanager.png"
        if candidate.is_file():
            return candidate
    return roots[-1] / "assets" / "videomanager.png"
