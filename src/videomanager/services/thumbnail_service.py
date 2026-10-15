"""Generate 10, 30, or 60 WebP frames at 5% through 90%."""

from __future__ import annotations

import shutil
import subprocess
import re
import time
from pathlib import Path
from uuid import uuid4

from videomanager.config import media_tool


class ThumbnailError(RuntimeError):
    pass


class ThumbnailCancelled(ThumbnailError):
    pass


PREVIEW_COUNTS = {"standard": 10, "detailed": 30, "dense": 60}


def cleanup_stale_staging(cache_root: Path, *, older_than_seconds: int = 3600) -> int:
    """Remove only old, direct-child staging directories from interrupted jobs."""
    if not cache_root.is_dir():
        return 0
    root = cache_root.resolve(strict=True)
    removed = 0
    for path in cache_root.iterdir():
        if not re.fullmatch(r"\.[0-9a-f-]{36}-[0-9a-f]{32}\.(tmp|old)", path.name):
            continue
        try:
            if path.is_symlink() or not path.is_dir():
                continue
            resolved = path.resolve(strict=True)
            if resolved.parent != root or time.time() - path.stat().st_mtime < older_than_seconds:
                continue
            shutil.rmtree(resolved)
            removed += 1
        except OSError:
            continue
    return removed


def frame_fractions(count: int = 10) -> tuple[float, ...]:
    if count not in PREVIEW_COUNTS.values():
        raise ValueError("Preview count must be 10, 30, or 60")
    return tuple(0.05 + index * (0.90 - 0.05) / (count - 1) for index in range(count))


def cache_signature(file_size: int, mtime_ns: int, count: int = 10) -> str:
    return f"{file_size}:{mtime_ns}:frames={count}:5-90-v1"


def frame_paths(cache_root: Path, video_id: str, count: int = 10) -> list[Path]:
    return [cache_root / video_id / f"{index:02d}.webp" for index in range(count)]


def _run_frame_command(command: list[str], cancelled, *, timeout: float = 45) -> tuple[int, str]:
    """Let playback stop an active decoder instead of waiting for its timeout."""
    with subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as process:
        deadline = time.monotonic() + timeout
        while True:
            if cancelled():
                process.kill()
                process.communicate()
                raise ThumbnailCancelled("Thumbnail job cancelled")
            try:
                _, stderr = process.communicate(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
                return process.returncode, stderr.decode("utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    process.kill()
                    process.communicate()
                    raise subprocess.TimeoutExpired(command, timeout)


def generate_thumbnails(
    source: Path,
    cache_root: Path,
    video_id: str,
    duration_ms: int | None,
    *,
    count: int = 10,
    ffmpeg: str | None = None,
    cancelled=lambda: False,
) -> list[Path]:
    if duration_ms is None or duration_ms <= 0:
        raise ThumbnailError("No usable duration; cannot select representative frames")
    frame_fractions(count)
    ffmpeg = ffmpeg or media_tool("ffmpeg")
    cache_root.mkdir(parents=True, exist_ok=True)
    staging = cache_root / f".{video_id}-{uuid4().hex}.tmp"
    staging.mkdir()
    target = cache_root / video_id
    previous = cache_root / f".{video_id}-{uuid4().hex}.old"
    try:
        duration = duration_ms / 1000
        successful: list[int] = []
        failed: dict[int, str] = {}
        for index, fraction in enumerate(frame_fractions(count)):
            if cancelled():
                raise ThumbnailCancelled("Thumbnail job cancelled")
            target_time = min(duration * fraction, max(0, duration - 0.05))
            output = staging / f"{index:02d}.webp"
            errors: list[str] = []
            for offset in (0, -0.25, 0.25):
                output.unlink(missing_ok=True)
                seek = max(0, min(target_time + offset, max(0, duration - 0.05)))
                try:
                    returncode, stderr = _run_frame_command(
                        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{seek:.3f}",
                         "-i", str(source), "-frames:v", "1", "-vf",
                         "scale=w=320:h=180:force_original_aspect_ratio=decrease",
                         "-c:v", "libwebp", "-quality", "80", "-y", str(output)],
                        cancelled,
                    )
                except ThumbnailCancelled:
                    raise
                except (OSError, subprocess.TimeoutExpired) as exc:
                    errors.append(str(exc))
                    continue
                if returncode == 0 and output.is_file() and output.stat().st_size > 0:
                    successful.append(index)
                    break
                errors.append(stderr.strip()[-400:])
            else:
                output.unlink(missing_ok=True)
                failed[index] = f"Frame {index + 1} failed: {'; '.join(errors)}"
        if cancelled():
            raise ThumbnailCancelled("Thumbnail job cancelled")
        if not successful:
            raise ThumbnailError(next(iter(failed.values())))
        for index in failed:
            nearest = min(successful, key=lambda good: (abs(good - index), good))
            shutil.copyfile(staging / f"{nearest:02d}.webp", staging / f"{index:02d}.webp")
        if target.exists():
            target.rename(previous)
        try:
            staging.rename(target)
        except Exception:
            if previous.exists():
                previous.rename(target)
            raise
        if previous.exists():
            shutil.rmtree(previous)
        return frame_paths(cache_root, video_id, count)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
