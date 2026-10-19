"""Background import, keeping large cross-volume copies off the UI thread."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from videomanager.services.import_service import ImportCancelled, ImportItem, import_mp4, plan_group_import, plan_import


def natural_key(path: Path) -> list[tuple[int, object]]:
    import re
    return [(1, int(part)) if part.isdigit() else (0, part.casefold())
            for part in re.split(r"(\d+)", path.name)]


class ImportPlanWorker(QThread):
    progress = Signal(str)
    planned = Signal(object)
    failed = Signal(str)

    def __init__(self, sources: list[Path] | Path, root: Path, library_id: str, parent=None,
                 *, group: bool = False, remove_phrases: tuple[str, ...] = ()) -> None:
        super().__init__(parent)
        self.sources = sources
        self.root = root
        self.library_id = library_id
        self.group = group
        self.remove_phrases = remove_phrases

    def run(self) -> None:
        try:
            if isinstance(self.sources, Path):
                if self.sources.is_symlink() or not self.sources.is_dir():
                    raise ValueError("來源資料夾不可用或為符號連結")
                if self.sources.resolve(strict=True).is_relative_to(self.root.resolve(strict=True)):
                    raise ValueError("來源已在影片庫內，無需重新搬入")
                paths: list[Path] = []
                iterator = self.sources.iterdir() if self.group else self.sources.rglob("*")
                for path in iterator:
                    if self.isInterruptionRequested():
                        return
                    if path.suffix.lower() == ".mp4" and path.is_file():
                        paths.append(path)
                        if len(paths) % 100 == 0:
                            self.progress.emit(f"已找到 {len(paths)} 部 MP4，正在規劃位置…")
                paths.sort(key=natural_key if self.group else None)
            else:
                paths = sorted(self.sources, key=natural_key) if self.group else self.sources
            if self.isInterruptionRequested():
                return
            planner = plan_group_import if self.group else plan_import
            self.planned.emit(planner(self.root, self.library_id, paths,
                                      remove_phrases=self.remove_phrases))
        except Exception as exc:
            self.failed.emit(str(exc))


class ImportWorker(QThread):
    progress = Signal(str)
    bytes_progress = Signal(int, int, int, int)
    outcome = Signal(object)

    def __init__(self, items: list[ImportItem], root: Path, library_id: str, parent=None) -> None:
        super().__init__(parent)
        self.items = items
        self.root = root
        self.library_id = library_id

    def run(self) -> None:
        completed: list[str] = []
        retained: list[str] = []
        failed: list[str] = []
        cancelled = False
        for index, item in enumerate(self.items, 1):
            if self.isInterruptionRequested():
                cancelled = True
                break
            self.progress.emit(f"搬入影片庫 {index}/{len(self.items)}：{item.source.name}")
            try:
                result = import_mp4(
                    item, self.root, self.library_id, cancelled=self.isInterruptionRequested,
                    progress=lambda done, total, position=index: self.bytes_progress.emit(
                        position, len(self.items), done, total),
                )
                completed.append(str(result.destination))
                if result.source_retained:
                    retained.append(str(item.source))
            except ImportCancelled:
                cancelled = True
                break
            except Exception as exc:
                failed.append(f"{item.source}: {exc}")
        self.outcome.emit({"completed": completed, "retained": retained, "failed": failed, "cancelled": cancelled})
