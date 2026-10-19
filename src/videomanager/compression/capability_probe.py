"""Discover actual bundled FFmpeg capabilities, including executable NVENC."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from videomanager.config import media_tool


@dataclass(frozen=True)
class Capabilities:
    ffmpeg: str
    ffprobe: str
    version: str
    encoders: frozenset[str]
    vmaf: bool
    ssim: bool
    nvenc_usable: bool
    message: str = ""
    qsv_usable: bool = False


def _output(args: list[str], timeout: int = 15) -> str:
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-600:])
    return result.stdout


def probe_capabilities(ffmpeg: str | None = None, ffprobe: str | None = None,
                       *, test_nvenc: bool = True) -> Capabilities:
    ffmpeg = ffmpeg or media_tool("ffmpeg")
    ffprobe = ffprobe or media_tool("ffprobe")
    if not Path(ffmpeg).is_file() or not Path(ffprobe).is_file():
        return Capabilities(ffmpeg, ffprobe, "", frozenset(), False, False, False, "找不到 FFmpeg 或 FFprobe")
    try:
        version = _output([ffmpeg, "-version"]).splitlines()[0]
        _output([ffprobe, "-version"])
        encoders_text = _output([ffmpeg, "-hide_banner", "-encoders"])
        filters_text = _output([ffmpeg, "-hide_banner", "-filters"])
        encoders = frozenset(name for name in ("libx265", "libkvazaar", "libx264", "libopenh264",
                                              "hevc_nvenc", "h264_nvenc", "hevc_qsv", "h264_qsv")
                             if any(line.split()[1:2] == [name] for line in encoders_text.splitlines()))
        filters = {line.split()[1] for line in filters_text.splitlines() if len(line.split()) > 1}
        nvenc = False
        nvenc_reason = ""
        if test_nvenc and "hevc_nvenc" in encoders:
            try:
                _output([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                         "color=size=64x64:rate=1", "-frames:v", "1", "-c:v", "hevc_nvenc",
                         "-f", "null", "-"], 20)
                nvenc = True
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                error = str(exc)
                if "Driver does not support the required nvenc API version" in error:
                    nvenc_reason = "NVIDIA 驅動不支援隨附 FFmpeg 所需的 NVENC 版本；可更新驅動後重試。"
                else:
                    nvenc_reason = "NVENC 實測失敗：" + (error.splitlines()[0] if error else "未知原因")
        qsv = False
        if "hevc_qsv" in encoders:
            try:
                _output([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                         "color=size=128x72:rate=1", "-frames:v", "1", "-c:v", "hevc_qsv",
                         "-f", "null", "-"], 20)
                qsv = True
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                pass
        return Capabilities(ffmpeg, ffprobe, version, encoders, "libvmaf" in filters,
                            "ssim" in filters, nvenc, nvenc_reason, qsv)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, IndexError) as exc:
        return Capabilities(ffmpeg, ffprobe, "", frozenset(), False, False, False, str(exc))
