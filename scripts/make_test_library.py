"""Create a NEW, disposable two-video library using FFmpeg.

The target must not exist. This script never operates on an existing library.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Path to a new, nonexistent test directory")
    args = parser.parse_args()
    output = args.output.resolve(strict=False)
    if output.exists():
        parser.error(f"Refusing to use an existing path: {output}")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        parser.error("FFmpeg is required to generate valid test MP4 files")
    output.mkdir(parents=True)
    nested = output / "子資料夾 (測試)"
    nested.mkdir()
    for target, color in ((output / "影片 A.mp4", "blue"), (nested / "影片 B.mp4", "red")):
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=24",
                "-t", "1", "-c:v", "mpeg4", "-q:v", "5", str(target),
            ],
            check=True,
        )
    print(f"Created disposable test library at {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
