"""Measured sample rates with an explicitly heuristic uncertainty range."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True)
class Estimate:
    minimum: int
    midpoint: int
    maximum: int
    reduction_pct: float
    confidence: str
    note: str


def estimate_size(source_size: int, duration: float, samples: list[dict]) -> Estimate:
    if source_size <= 0 or duration <= 0 or not samples:
        raise ValueError("無法估算：缺少完整取樣結果")
    rates = [(item["video_bytes"] + item["audio_bytes"] + item["overhead_bytes"])
             / item["duration"] for item in samples if item["duration"] > 0]
    if len(rates) != len(samples):
        raise ValueError("取樣時長無效")
    avg = mean(rates)
    covered = min(1.0, sum(item["duration"] for item in samples) / duration)
    dispersion = (max(rates) - min(rates)) / avg if avg else 0
    # A broad sensitivity range, not a statistical confidence interval.
    uncertainty = min(0.8, max(dispersion / 2, (1 - covered) * 0.10))
    midpoint = round(avg * duration)
    minimum = max(0, round(midpoint * (1 - uncertainty)))
    maximum = round(midpoint * (1 + uncertainty))
    confidence = "較低" if dispersion > 0.35 or covered < 0.03 else "中等"
    note = (f"實測 {len(samples)} 段，涵蓋片長約 {covered * 100:.1f}% ；"
            f"段落位元率差異約 {dispersion * 100:.1f}%。區間按樣本差異與未取樣範圍推估，非統計保證。")
    return Estimate(minimum, midpoint, maximum, (1 - midpoint / source_size) * 100,
                    confidence, note)
