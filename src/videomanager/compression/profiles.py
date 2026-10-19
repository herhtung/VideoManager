"""Encoder profiles and validated FFmpeg option construction."""

from __future__ import annotations

from dataclasses import asdict, dataclass


PRESETS = (
    ("極致保真", 18, "slow"), ("高品質", 20, "medium"),
    ("一般（平衡）", 23, "medium"), ("高壓縮", 27, "medium"),
    ("極限壓縮", 30, "medium"),
)
ENCODERS = {"libx265", "libkvazaar", "hevc_nvenc", "hevc_qsv",
            "libx264", "libopenh264", "h264_nvenc", "h264_qsv"}
SPEEDS = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower"}


@dataclass(frozen=True)
class Profile:
    name: str = "一般（平衡）"
    encoder: str = "libx265"
    quality: int = 23
    speed: str = "medium"
    resolution: str = "source"
    fps: str = "source"
    audio: str = "auto"
    target_reduction_pct: int = 10
    target_passes: int = 2

    def validate(self) -> "Profile":
        if self.encoder not in ENCODERS:
            raise ValueError("不支援的編碼器")
        if not 0 <= self.quality <= 51:
            raise ValueError("品質值需介於 0 到 51")
        if self.speed not in SPEEDS:
            raise ValueError("不支援的編碼速度")
        if self.resolution not in {"source", "1080p", "720p"}:
            raise ValueError("不支援的解析度")
        if self.fps not in {"source", "30"}:
            raise ValueError("不支援的影格率")
        if self.audio not in {"auto", "copy", "aac128", "aac192", "aac256"}:
            raise ValueError("不支援的音訊模式")
        if not 5 <= self.target_reduction_pct <= 60:
            raise ValueError("目標縮小比例需介於 5% 到 60%")
        if self.target_passes not in {1, 2}:
            raise ValueError("目標位元率只能選單遍或雙遍")
        return self

    def snapshot(self) -> dict:
        return asdict(self.validate())


def builtin(index: int, encoder: str = "libx265") -> Profile:
    name, cpu_quality, speed = PRESETS[index]
    # NVENC CQ is a distinct control; these are independent starting points.
    nvenc_values = (16, 19, 23, 28, 32)
    value = nvenc_values[index] if encoder.endswith(("_nvenc", "_qsv")) else cpu_quality
    return Profile(name, encoder, value,
                   speed if not encoder.endswith(("_nvenc", "_qsv")) else "medium").validate()


def video_options(profile: Profile, source_width: int | None, source_height: int | None) -> list[str]:
    profile.validate()
    args = ["-c:v", profile.encoder]
    if profile.encoder.endswith("_nvenc"):
        args += ["-rc", "vbr", "-cq", str(profile.quality), "-b:v", "0",
                 "-preset", "p5" if profile.speed in {"medium", "slow", "slower"} else "p3"]
    elif profile.encoder.endswith("_qsv"):
        args += ["-global_quality", str(profile.quality), "-preset", profile.speed]
    elif profile.encoder == "libkvazaar":
        args += ["-kvazaar-params", f"qp={profile.quality},preset={profile.speed}"]
    elif profile.encoder == "libopenh264":
        args += ["-b:v", "2000000"]
    else:
        args += ["-crf", str(profile.quality), "-preset", profile.speed]
    args += video_geometry_options(profile, source_height)
    return args


def video_geometry_options(profile: Profile, source_height: int | None) -> list[str]:
    args: list[str] = []
    if profile.resolution != "source":
        limit = 1080 if profile.resolution == "1080p" else 720
        if source_height and source_height > limit:
            args += ["-vf", f"scale=-2:{limit}"]
    if profile.fps == "30":
        args += ["-r", "30"]
    return args


def target_video_options(profile: Profile, source_height: int | None,
                         bitrate: int, encoder: str) -> list[str]:
    if encoder not in ENCODERS or bitrate <= 0:
        raise ValueError("目標位元率需要可用的編碼器和正數位元率")
    if encoder.endswith("_nvenc"):
        preset = "p5" if profile.speed in {"medium", "slow", "slower"} else "p3"
        return ["-c:v", encoder, "-rc", "vbr", "-b:v", str(bitrate),
                "-maxrate", str(round(bitrate * 1.30)), "-bufsize", str(bitrate * 2),
                "-preset", preset, *video_geometry_options(profile, source_height)]
    if encoder.endswith("_qsv"):
        return ["-c:v", encoder, "-b:v", str(bitrate),
                "-maxrate", str(round(bitrate * 1.30)), "-bufsize", str(bitrate * 2),
                "-preset", profile.speed, *video_geometry_options(profile, source_height)]
    if encoder == "libkvazaar":
        return ["-c:v", encoder, "-b:v", str(bitrate),
                "-kvazaar-params", f"preset={profile.speed}",
                *video_geometry_options(profile, source_height)]
    if encoder == "libopenh264":
        return ["-c:v", encoder, "-b:v", str(bitrate), "-allow_skip_frames", "1",
                *video_geometry_options(profile, source_height)]
    return ["-c:v", encoder, "-b:v", str(bitrate), "-preset", profile.speed,
            *video_geometry_options(profile, source_height)]


def audio_options(profile: Profile, codec: str | None) -> list[str]:
    if profile.audio == "copy" or (profile.audio == "auto" and codec in {"aac", "alac", "mp3", "ac3", "eac3"}):
        return ["-c:a", "copy"]
    rate = {"aac128": "128k", "aac192": "192k", "aac256": "256k"}.get(profile.audio, "192k")
    return ["-c:a", "aac", "-b:a", rate]
