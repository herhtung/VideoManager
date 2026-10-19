"""Optional aligned sample quality metrics; never label them as fidelity percentages."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from videomanager.compression.capability_probe import Capabilities


def measure_sample(capabilities: Capabilities, source: Path, sample: Path,
                   start: float, duration: float, width: int, height: int,
                   *, hdr_or_rotation: bool = False) -> tuple[str, float | None, str]:
    if hdr_or_rotation or not (capabilities.vmaf or capabilities.ssim):
        return "none", None, "特殊色彩或角度／量測濾鏡不可用，暫無可靠分數"
    # Normalize wall-clock time, size, and pixel format on both paths before comparison.
    base = (f"settb=AVTB,setpts=PTS-STARTPTS,fps=30,"
            f"scale={width}:{height}:flags=bicubic,format=yuv420p")
    metric = "vmaf" if capabilities.vmaf else "ssim"
    filter_name = "libvmaf=n_threads=2" if metric == "vmaf" else "ssim"
    graph = f"[0:v]{base}[ref];[1:v]{base}[dist];[ref][dist]{filter_name}"
    command = [capabilities.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info",
               "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
               "-i", str(sample), "-filter_complex", graph, "-an", "-f", "null", "-"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=max(30, int(duration * 10)),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            return "none", None, "無法可靠對齊原片與樣本；請直接比較畫面"
        pattern = r"VMAF score:\s*([0-9.]+)" if metric == "vmaf" else r"All:\s*([0-9.]+)"
        matches = re.findall(pattern, result.stderr)
        if not matches:
            return "none", None, "品質濾鏡沒有回傳可靠分數"
        score = float(matches[-1])
        if not (0 <= score <= (100 if metric == "vmaf" else 1)):
            return "none", None, "品質分數無效"
        return metric, score, "取樣量測；VMAF 與 SSIM 不是相同尺度"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return "none", None, "品質量測失敗；請直接比較畫面"
