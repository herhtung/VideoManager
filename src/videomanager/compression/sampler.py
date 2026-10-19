"""Bounded, nonoverlapping representative time windows."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SampleWindow:
    start: float
    duration: float


def sample_windows(duration: float, count: int = 4) -> list[SampleWindow]:
    if duration <= 0 or count not in {2, 4, 8}:
        raise ValueError("片長或取樣數無效")
    # Sample the latter half where these videos often differ most. Keep every
    # window fully inside 40%-90%, not merely its midpoint.
    region_start = duration * 0.40
    usable = duration * 0.50
    count = min(count, max(1, int(usable)))
    length = min(20.0, usable / count * 0.8)
    if length <= 0:
        raise ValueError("影片太短，無法試壓縮")
    windows: list[SampleWindow] = []
    for index in range(count):
        center = region_start + usable * (index + 0.5) / count
        start = center - length / 2
        windows.append(SampleWindow(round(start, 3), round(length, 3)))
    return windows
