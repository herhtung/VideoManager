"""FFprobe metadata extraction with bounded execution time."""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

from videomanager.config import media_tool


class ProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    duration_ms: int | None
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None


def probe_media(path: Path, ffprobe: str | None = None, timeout: float = 30) -> MediaInfo:
    ffprobe = ffprobe or media_tool("ffprobe")
    try:
        completed = subprocess.run(
            [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        payload = json.loads(completed.stdout)
        streams = payload.get("streams", [])
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        if video is None:
            raise ProbeError("No video stream")
        raw_duration = payload.get("format", {}).get("duration") or video.get("duration")
        duration = None
        if raw_duration is not None:
            seconds = float(raw_duration)
            if math.isfinite(seconds) and seconds >= 0:
                duration = round(seconds * 1000)
        return MediaInfo(
            duration,
            int(video["width"]) if video.get("width") else None,
            int(video["height"]) if video.get("height") else None,
            video.get("codec_name"),
            audio.get("codec_name") if audio else None,
        )
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise ProbeError(f"FFprobe failed for {path.name}: {exc}") from exc
