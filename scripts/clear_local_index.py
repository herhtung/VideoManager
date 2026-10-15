"""Clear only VideoManager's local index and preview images, preserving MP4s."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-library-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA is unavailable")
    base = (Path(local) / "VideoManager").resolve(strict=True)
    if base.name != "VideoManager" or base.parent != Path(local).resolve(strict=True):
        raise RuntimeError("Unexpected application data directory")
    settings_path = base / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    if settings.get("library_id") != args.expected_library_id:
        raise RuntimeError("Library ID changed; index was not cleared")
    database_dir = (base / "data").resolve(strict=True)
    backup_dir = (base / "backups").resolve(strict=True)
    cache = (base / "cache" / "thumbnails").resolve(strict=True)
    if (database_dir, backup_dir, cache.parent) != (base / "data", base / "backups", base / "cache"):
        raise RuntimeError("Application data contains redirected directories")
    if cache != base / "cache" / "thumbnails":
        raise RuntimeError("Preview cache path changed")
    databases = [path for path in database_dir.iterdir()
                 if path.is_file() and path.name in {"library.db", "library.db-wal", "library.db-shm"}]
    backups = [path for path in backup_dir.iterdir()
               if path.is_file() and path.name.startswith("library-") and path.suffix == ".db"]
    images = sum(1 for path in cache.rglob("*") if path.is_file())
    print(f"Target: {base}")
    print(f"Database files: {len(databases)}; backups: {len(backups)}; cached files: {images}")
    print("Settings and all original MP4 files will be preserved.")
    if not args.apply:
        print("Dry run only.")
        return 0
    for path in (*databases, *backups):
        if path.is_symlink() or path.resolve(strict=True).parent not in {database_dir, backup_dir}:
            raise RuntimeError(f"Unexpected index file path: {path}")
    for path in (*databases, *backups):
        path.unlink()
    shutil.rmtree(cache)
    cache.mkdir(parents=True)
    settings["scan_folders"] = []
    temporary = settings_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, settings_path)
    print("Cleared local index, database backups, and preview cache.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
