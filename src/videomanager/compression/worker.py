"""FFmpeg analysis and encoding on a dedicated worker thread, preserving originals."""

from __future__ import annotations

import json
import hashlib
import os
import queue
import re
import shutil
import subprocess
import threading
import psutil
import time
from pathlib import Path
from uuid import uuid4

from videomanager.compression.capability_probe import Capabilities
from videomanager.compression.estimator import estimate_size
from videomanager.compression.profiles import Profile, audio_options, target_video_options, video_options
from videomanager.compression.quality import measure_sample
from videomanager.compression.repository import get_job, update_job
from videomanager.compression.sampler import sample_windows
from videomanager.db.database import normalize_relative_path, utc_now
from videomanager.services.library_locator import read_marker


class CompressionError(RuntimeError):
    pass


class CompressionCancelled(CompressionError):
    pass


class CompressionInterrupted(CompressionError):
    """The app closed; verified chunks may be resumed next time."""
    pass


def _cancel_exception(cancelled) -> CompressionError:
    preserve = getattr(cancelled, "preserve_checkpoint_requested", None)
    return (CompressionInterrupted("程式關閉；已完成的壓縮片段可從歷史紀錄接續")
            if preserve and preserve() else CompressionCancelled("使用者取消"))


def _encoder_available(cap: Capabilities, encoder: str) -> bool:
    return (encoder in cap.encoders
            and (not encoder.endswith("_nvenc") or cap.nvenc_usable)
            and (not encoder.endswith("_qsv") or cap.qsv_usable))


def _probe(ffprobe: str, path: Path) -> dict:
    result = subprocess.run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-show_chapters", "-of", "json", str(path)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise CompressionError(f"FFprobe 無法讀取 {path.name}: {result.stderr[-500:]}")
    try:
        data = json.loads(result.stdout)
        video = next(stream for stream in data["streams"] if stream.get("codec_type") == "video")
        audio = next((stream for stream in data["streams"] if stream.get("codec_type") == "audio"), None)
        duration = float(data["format"].get("duration") or video.get("duration"))
        if duration <= 0:
            raise ValueError("缺少片長")
        return {"duration": duration, "video": video, "audio": audio,
                "streams": data["streams"], "format": data["format"], "chapters": data.get("chapters", [])}
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        raise CompressionError(f"無法取得有效影片資訊：{exc}") from exc


def _packet_bytes(ffprobe: str, path: Path, stream: str) -> int:
    result = subprocess.run([ffprobe, "-v", "error", "-select_streams", stream,
                             "-show_entries", "packet=size", "-of", "csv=p=0", str(path)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise CompressionError("樣本串流位元組量測失敗")
    return sum(int(line.strip().split(",")[0]) for line in result.stdout.splitlines()
               if line.strip().split(",")[0].isdigit())


def _source_for_job(job, root: Path) -> Path:
    marker = read_marker(root)
    if marker is None or marker.library_id != job["library_id"]:
        raise CompressionError("影片庫離線或識別碼不符")
    relative, _ = normalize_relative_path(job["source_rel_path"])
    source = root / relative
    if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(root.resolve()):
        raise CompressionError("來源影片不存在或路徑已變動")
    stat = source.stat()
    if stat.st_size != job["source_size"] or stat.st_mtime_ns != job["source_mtime_ns"]:
        raise CompressionError("來源影片已變動，請重新掃描並建立新工作")
    return source


def _space_guard(path: Path, need: int) -> None:
    free = shutil.disk_usage(path).free
    if free < need:
        raise CompressionError(f"目標磁碟空間不足；尚有 {free / 2**30:.1f} GB")


def _private_dir(root: Path, *parts: str) -> Path:
    """Do not follow a substituted staging/output directory outside the library."""
    current = root
    resolved_root = root.resolve(strict=True)
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise CompressionError("壓縮工作目錄是符號連結，拒絕使用")
        current.mkdir(exist_ok=True)
        if not current.resolve(strict=True).is_relative_to(resolved_root):
            raise CompressionError("壓縮工作目錄超出影片庫")
    return current


def _run_ffmpeg(command: list[str], duration: float, cancelled, progress,
                *, space_path: Path, minimum_free: int = 256 * 2**20) -> float:
    started = time.monotonic()
    messages: queue.Queue[str | None] = queue.Queue()
    stderr: list[str] = []
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, creationflags=creationflags) as process:
        process_started = getattr(cancelled, "process_started", None)
        if process_started is not None:
            process_started()
        process_control = psutil.Process(process.pid)
        suspended = False
        def read_stdout() -> None:
            assert process.stdout
            for raw in process.stdout:
                messages.put(raw.decode("utf-8", errors="replace"))
            messages.put(None)

        def read_stderr() -> None:
            assert process.stderr
            for raw in process.stderr:
                stderr.append(raw.decode("utf-8", errors="replace"))
                if len(stderr) > 100:
                    del stderr[:50]

        out_thread = threading.Thread(target=read_stdout, daemon=True)
        err_thread = threading.Thread(target=read_stderr, daemon=True)
        out_thread.start()
        err_thread.start()
        last_space = 0.0
        last_heartbeat = started
        last_percent = 0.0
        try:
            while process.poll() is None or not messages.empty():
                if cancelled():
                    raise _cancel_exception(cancelled)
                pause_requested = getattr(cancelled, "pause_requested", None)
                should_pause = bool(pause_requested and pause_requested())
                if should_pause != suspended and process.poll() is None:
                    try:
                        process_control.suspend() if should_pause else process_control.resume()
                        suspended = should_pause
                    except psutil.Error as exc:
                        raise CompressionError(f"無法{'暫停' if should_pause else '恢復'} FFmpeg：{exc}") from exc
                if suspended:
                    time.sleep(0.1)
                    continue
                now = time.monotonic()
                if now - last_space > 3:
                    _space_guard(space_path, minimum_free)
                    last_space = now
                try:
                    line = messages.get(timeout=0.2)
                except queue.Empty:
                    if now - last_heartbeat >= 3:
                        progress(last_percent)
                        last_heartbeat = now
                    continue
                if line is None:
                    continue
                key, _, value = line.strip().partition("=")
                if key == "out_time_us":
                    try:
                        elapsed_us = int(value)
                    except ValueError:
                        # FFmpeg can report N/A before an encoder produces timestamps.
                        continue
                    last_percent = min(99.0, max(0.0, elapsed_us / (duration * 1_000_000) * 100))
                    progress(last_percent)
                    last_heartbeat = now
            out_thread.join(timeout=2)
            err_thread.join(timeout=2)
            if process.returncode:
                raise CompressionError("FFmpeg 失敗：" + "".join(stderr)[-1000:])
            progress(100.0)
            return time.monotonic() - started
        except Exception:
            if suspended and process.poll() is None:
                try:
                    process_control.resume()
                except psutil.Error:
                    pass
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            raise


def _ffmpeg_command(cap: Capabilities, source: Path, target: Path, profile: Profile,
                    info: dict, *, start: float | None = None, length: float | None = None,
                    target_bitrate: int | None = None) -> list[str]:
    args = [cap.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats",
            "-progress", "pipe:1"]
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(source)]
    if length is not None:
        args += ["-t", f"{length:.3f}"]
    args += ["-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0", "-map_chapters", "0"]
    if target_bitrate is None:
        args += video_options(profile, info["video"].get("width"), info["video"].get("height"))
    else:
        args += target_video_options(profile, info["video"].get("height"),
                                     target_bitrate, profile.encoder)
    args += audio_options(profile, info["audio"].get("codec_name") if info["audio"] else None)
    args += ["-movflags", "+faststart", "-f", "mp4", "-n", str(target)]
    return args


def _target_bitrate(source_size: int, info: dict, profile: Profile) -> int:
    """Reserve audio and container space before assigning the video bit budget."""
    total = source_size * 8 / info["duration"] * (1 - profile.target_reduction_pct / 100) * 0.96
    audio = info["audio"]
    if audio is None:
        audio_rate = 0
    elif profile.audio.startswith("aac"):
        audio_rate = int(profile.audio[3:]) * 1000
    else:
        try:
            audio_rate = int(audio.get("bit_rate") or 0)
        except (TypeError, ValueError):
            audio_rate = 0
        if not audio_rate:
            audio_rate = 256_000 if profile.audio in {"copy", "auto"} else 192_000
    bitrate = int(total - audio_rate - 4_000)
    if bitrate < 10_000:
        raise CompressionError("選定的縮小比例留給畫面的位元率不足；可選較低比例或降低音訊位元率")
    return bitrate


def _target_encoder(cap: Capabilities, profile: Profile) -> tuple[str, int]:
    """Choose a CPU two-pass encoder when present, otherwise a usable single pass."""
    if profile.target_passes == 1:
        return profile.encoder, 1
    preferred = ("libx265", "libkvazaar") if profile.encoder.startswith(("hevc", "libx265", "libkvazaar")) else (
        "libx264", "libopenh264")
    for encoder in preferred:
        if encoder in cap.encoders:
            return encoder, 2 if encoder in {"libx265", "libx264"} else 1
    return profile.encoder, 1


def _target_command(cap: Capabilities, source: Path, target: Path, profile: Profile,
                    info: dict, bitrate: int, encoder: str, pass_number: int | None,
                    passlog: Path | None, *, start: float | None = None,
                    length: float | None = None) -> list[str]:
    args = [cap.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats",
            "-progress", "pipe:1"]
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(source)]
    if length is not None:
        args += ["-t", f"{length:.3f}"]
    args += ["-map", "0:v:0"]
    if pass_number != 1:
        args += ["-map", "0:a:0?", "-map_metadata", "0", "-map_chapters", "0"]
    args += target_video_options(profile, info["video"].get("height"), bitrate, encoder)
    if pass_number is not None:
        if passlog is None:
            raise ValueError("雙遍編碼缺少統計檔位置")
        args += ["-pass", str(pass_number), "-passlogfile", str(passlog)]
    if pass_number == 1:
        args += ["-an", "-f", "null", os.devnull]
    else:
        args += audio_options(profile, info["audio"].get("codec_name") if info["audio"] else None)
        args += ["-movflags", "+faststart", "-f", "mp4", "-n", str(target)]
    return args


def _encode_target_size(cap: Capabilities, source: Path, target: Path, staging: Path,
                        profile: Profile, info: dict, source_size: int, cancelled, progress,
                        root: Path) -> tuple[int, str, str]:
    encoder = profile.encoder if profile.target_passes == 1 else (
        profile.encoder.replace("hevc_nvenc", "libx265").replace("h264_nvenc", "libx264")
        .replace("hevc_qsv", "libx265").replace("h264_qsv", "libx264"))
    if not _encoder_available(cap, encoder):
        raise CompressionError("目標容量模式需要所選編碼器")
    bitrate = _target_bitrate(source_size, info, profile)
    maximum = int(source_size * (1 - profile.target_reduction_pct / 100))
    if profile.target_passes == 1:
        for attempt in range(2):
            if cancelled():
                raise CompressionCancelled("使用者取消")
            target.unlink(missing_ok=True)
            base, span = (0, 80) if attempt == 0 else (80, 20)
            _run_ffmpeg(_target_command(cap, source, target, profile, info, bitrate, encoder,
                                        None, None), info["duration"], cancelled,
                        lambda value, b=base, s=span: progress(b + value * s / 100),
                        space_path=root)
            if target.is_file() and target.stat().st_size <= maximum:
                return bitrate, encoder, "one_pass_target"
            if attempt == 0 and target.is_file():
                bitrate = max(10_000, int(bitrate * maximum / target.stat().st_size * .90))
        raise CompressionError("快速單遍輸出仍超過目標容量；未建立副本，原片保留。可改用雙遍模式")
    for attempt in range(2):
        if cancelled():
            raise CompressionCancelled("使用者取消")
        target.unlink(missing_ok=True)
        passlog = staging / f"target-pass-{attempt}"
        base, half = (0, 40) if attempt == 0 else (80, 10)
        _run_ffmpeg(_target_command(cap, source, target, profile, info, bitrate, encoder, 1, passlog),
                    info["duration"], cancelled,
                    lambda value, b=base, h=half: progress(b + value * h / 100),
                    space_path=root)
        _run_ffmpeg(_target_command(cap, source, target, profile, info, bitrate, encoder, 2, passlog),
                    info["duration"], cancelled,
                    lambda value, b=base, h=half: progress(b + h + value * h / 100),
                    space_path=root)
        if target.is_file() and target.stat().st_size <= maximum:
            return bitrate, encoder, "two_pass_target"
        if attempt == 0 and target.is_file():
            bitrate = max(10_000, int(bitrate * maximum / target.stat().st_size * .90))
    raise CompressionError("輸出超過目標容量；未建立壓縮副本，原片保留。可選較低的縮小比例")


def _chunk_windows(duration: float) -> list[tuple[float, float]]:
    """Small independent MP4s provide a verified restart point every five minutes."""
    count = max(1, int((duration + 299.999) // 300))
    windows = [(index * 300.0, min(300.0, duration - index * 300.0))
               for index in range(count)]
    if len(windows) > 1 and windows[-1][1] < 2:
        start, _ = windows[-2]
        windows[-2:] = [(start, duration - start)]
    return windows


def _checkpoint_identity(job, info: dict) -> str:
    stable = [job["id"], job["library_id"], job["video_id"], job["source_rel_path"],
              job["source_size"], job["source_mtime_ns"], job["profile_snapshot_json"],
              round(info["duration"], 3)]
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False).encode("utf-8")).hexdigest()


def _write_checkpoint(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".writing")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _load_checkpoint(staging: Path, identity: str) -> dict | None:
    path = staging / "checkpoint.json"
    if path.is_symlink():
        raise CompressionError("續壓紀錄路徑不安全")
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CompressionError("續壓紀錄無法讀取；原片保留，請取消此工作後重新建立") from exc
    if data.get("version") != 1 or data.get("identity") != identity:
        raise CompressionError("原片或壓縮設定與續壓紀錄不符；原片保留，請取消此工作後重新建立")
    if data.get("stage") not in {"quality", "target_0", "target_1"} or not isinstance(data.get("done"), list):
        raise CompressionError("續壓紀錄格式不正確；原片保留")
    return data


def _new_checkpoint(staging: Path, identity: str, stage: str, bitrate: int | None) -> dict:
    data = {"version": 1, "identity": identity, "stage": stage,
            "bitrate": bitrate, "done": []}
    _write_checkpoint(staging / "checkpoint.json", data)
    for old in staging.glob("chunk-*.mp4"):
        if not old.name.startswith(f"chunk-{stage}-"):
            old.unlink(missing_ok=True)
    return data


def _encode_stage(cap: Capabilities, source: Path, target: Path, staging: Path,
                  profile: Profile, info: dict, checkpoint: dict, windows: list[tuple[float, float]],
                  encoder: str, passes: int, cancelled, progress, root: Path,
                  progress_start: float, progress_span: float) -> None:
    stage = checkpoint["stage"]
    checkpoint_path = staging / "checkpoint.json"
    done = set(checkpoint["done"])
    if any(not isinstance(index, int) or index < 0 or index >= len(windows) for index in done):
        raise CompressionError("續壓紀錄的片段位置不正確")
    pieces: list[Path] = []
    for index, (start, length) in enumerate(windows):
        if cancelled():
            raise _cancel_exception(cancelled)
        piece = staging / f"chunk-{stage}-{index:04d}.mp4"
        if piece.is_symlink():
            raise CompressionError("壓縮片段路徑不安全")
        pieces.append(piece)
        if index in done:
            try:
                checked = _probe(cap.ffprobe, piece)
                if abs(checked["duration"] - length) > max(1.5, length * .02) or bool(checked["audio"]) != bool(info["audio"]):
                    raise CompressionError("片段片長或音訊不符")
            except (OSError, CompressionError):
                done.remove(index)
                checkpoint["done"] = sorted(done)
                _write_checkpoint(checkpoint_path, checkpoint)
                piece.unlink(missing_ok=True)
        if index not in done:
            piece.unlink(missing_ok=True)
            if stage == "quality":
                command = _ffmpeg_command(cap, source, piece, profile, info,
                                          start=start, length=length)
                _run_ffmpeg(command, length, cancelled,
                            lambda value, i=index: progress(progress_start +
                                progress_span * (i + value / 100) / len(windows)), space_path=root)
            else:
                bitrate = int(checkpoint["bitrate"])
                attempt = int(stage[-1])
                if passes == 2:
                    passlog = staging / f"pass-{attempt}-{index}"
                    _run_ffmpeg(_target_command(cap, source, piece, profile, info, bitrate,
                                                encoder, 1, passlog, start=start, length=length),
                                length, cancelled,
                                lambda value, i=index: progress(progress_start +
                                    progress_span * (i + value / 200) / len(windows)), space_path=root)
                    _run_ffmpeg(_target_command(cap, source, piece, profile, info, bitrate,
                                                encoder, 2, passlog, start=start, length=length),
                                length, cancelled,
                                lambda value, i=index: progress(progress_start +
                                    progress_span * (i + .5 + value / 200) / len(windows)), space_path=root)
                    for log in staging.glob(f"pass-{attempt}-{index}*"):
                        log.unlink(missing_ok=True)
                else:
                    _run_ffmpeg(_target_command(cap, source, piece, profile, info, bitrate,
                                                encoder, None, None, start=start, length=length),
                                length, cancelled,
                                lambda value, i=index: progress(progress_start +
                                    progress_span * (i + value / 100) / len(windows)), space_path=root)
            checked = _probe(cap.ffprobe, piece)
            if abs(checked["duration"] - length) > max(1.5, length * .02) or bool(checked["audio"]) != bool(info["audio"]):
                raise CompressionError("壓縮片段驗證失敗；原片保留")
            done.add(index)
            checkpoint["done"] = sorted(done)
            _write_checkpoint(checkpoint_path, checkpoint)
        progress(progress_start + progress_span * (index + 1) / len(windows))
    target.unlink(missing_ok=True)
    list_file = staging / "concat.txt"
    list_file.write_text("".join(f"file '{piece.name}'\n" for piece in pieces), encoding="utf-8")
    command = [cap.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats",
               "-progress", "pipe:1", "-f", "concat", "-safe", "0", "-i", str(list_file),
               "-c", "copy", "-movflags", "+faststart", "-f", "mp4", "-n", str(target)]
    _run_ffmpeg(command, info["duration"], cancelled, lambda value: None, space_path=root)


def _resumable_encode(cap: Capabilities, source: Path, target: Path, staging: Path,
                      profile: Profile, info: dict, job, cancelled, progress, root: Path
                      ) -> tuple[int | None, str, str]:
    identity = _checkpoint_identity(job, info)
    checkpoint = _load_checkpoint(staging, identity)
    maximum = int(job["source_size"] * (1 - profile.target_reduction_pct / 100))
    try_quality = (not profile.encoder.endswith("_qsv") and
                   profile.encoder != "libopenh264" and
                   (job["estimated_size_mid"] is None or job["estimated_size_mid"] <= maximum))
    windows = _chunk_windows(info["duration"])
    if checkpoint is None:
        checkpoint = _new_checkpoint(staging, identity, "quality" if try_quality else "target_0",
                                     None if try_quality else _target_bitrate(job["source_size"], info, profile))
    if checkpoint["stage"] == "quality":
        _encode_stage(cap, source, target, staging, profile, info, checkpoint, windows,
                      profile.encoder, 1, cancelled, progress, root, 0, 48)
        if target.stat().st_size <= maximum:
            return None, profile.encoder, "quality"
        target.unlink(missing_ok=True)
        checkpoint = _new_checkpoint(staging, identity, "target_0",
                                     _target_bitrate(job["source_size"], info, profile))
    encoder, passes = _target_encoder(cap, profile)
    if not _encoder_available(cap, encoder):
        raise CompressionError("目標容量模式需要所選編碼器")
    for attempt in range(int(checkpoint["stage"][-1]), 2):
        stage = f"target_{attempt}"
        if checkpoint["stage"] != stage:
            raise CompressionError("續壓紀錄的編碼階段不正確")
        base = (48 if try_quality else 0) + (0 if attempt == 0 else (38 if try_quality else 77))
        span = (38 if try_quality else 77) if attempt == 0 else (10 if try_quality else 19)
        _encode_stage(cap, source, target, staging, profile, info, checkpoint, windows,
                      encoder, passes, cancelled, progress, root, base, span)
        if target.stat().st_size <= maximum:
            return int(checkpoint["bitrate"]), encoder, (
                "one_pass_target" if passes == 1 else "two_pass_target")
        if attempt == 0:
            adjusted = max(10_000, int(int(checkpoint["bitrate"]) * maximum /
                                    target.stat().st_size * .90))
            target.unlink(missing_ok=True)
            checkpoint = _new_checkpoint(staging, identity, "target_1", adjusted)
    raise CompressionError("輸出超過目標容量；原片保留。可選較低的縮小比例")


def _validate_output(cap: Capabilities, source_info: dict, target: Path, profile: Profile) -> dict:
    if not target.is_file() or target.stat().st_size <= 0:
        raise CompressionError("輸出檔案不存在或為空")
    info = _probe(cap.ffprobe, target)
    if "mp4" not in str(info["format"].get("format_name", "")) and "mov" not in str(info["format"].get("format_name", "")):
        raise CompressionError("輸出不是 MP4")
    tolerance = max(3.0, source_info["duration"] * 0.02)
    if abs(info["duration"] - source_info["duration"]) > tolerance:
        raise CompressionError("輸出片長與來源差異過大")
    if bool(info["audio"]) != bool(source_info["audio"]):
        raise CompressionError("輸出音訊串流與來源不符")
    if profile.resolution == "source" and (
        info["video"].get("width") != source_info["video"].get("width") or
        info["video"].get("height") != source_info["video"].get("height")
    ):
        raise CompressionError("輸出解析度與來源不符")
    if profile.resolution in {"1080p", "720p"}:
        limit = 1080 if profile.resolution == "1080p" else 720
        if int(info["video"].get("height") or 0) > limit:
            raise CompressionError("輸出解析度未符合設定")
    if profile.fps == "30":
        raw_rate = info["video"].get("avg_frame_rate") or "0/1"
        try:
            numerator, denominator = (float(part) for part in raw_rate.split("/"))
            if denominator == 0 or abs(numerator / denominator - 30) > 0.5:
                raise CompressionError("輸出 FPS 未符合設定")
        except (ValueError, ZeroDivisionError) as exc:
            raise CompressionError("無法驗證輸出 FPS") from exc
    for seek in (0.1, max(0.1, info["duration"] * 0.5), max(0.1, info["duration"] - 1)):
        result = subprocess.run([cap.ffmpeg, "-v", "error", "-ss", f"{seek:.2f}", "-i", str(target),
                                 "-frames:v", "1", "-f", "null", "-"], capture_output=True,
                                timeout=40, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise CompressionError("輸出影格抽樣解碼失敗")
    return info


def run_analysis(connection, job_id: str, root: Path, cap: Capabilities, count: int,
                 cancelled=lambda: False, progress=lambda value: None) -> None:
    job = get_job(connection, job_id)
    if job is None or job["status"] not in {"queued", "analyzed", "failed"}:
        raise CompressionError("工作狀態不可分析")
    source = _source_for_job(job, root)
    profile = Profile(**json.loads(job["profile_snapshot_json"])).validate()
    if not _encoder_available(cap, profile.encoder):
        raise CompressionError("所選編碼器不可用；請改選可用模式")
    info = _probe(cap.ffprobe, source)
    sample_target_bitrate = None
    if (profile.encoder.endswith("_qsv") and profile.target_passes == 1) or profile.encoder == "libopenh264":
        try:
            sample_target_bitrate = _target_bitrate(job["source_size"], info, profile)
        except CompressionError:
            pass
    windows = sample_windows(info["duration"], count)
    staging = _private_dir(root, ".videomanager", "compression-staging", job_id)
    _space_guard(root, min(job["source_size"], 2 * 2**30) + 256 * 2**20)
    update_job(connection, job_id, status="analyzing", started_at=utc_now(), progress=0,
               error_code=None, error_message=None)
    stats: list[dict] = []
    unusual = any(stream.get("codec_type") not in {"video", "audio"} for stream in info["streams"])
    hdr = bool(info["video"].get("color_transfer") in {"smpte2084", "arib-std-b67"} or
               info["video"].get("side_data_list"))
    try:
        with connection:
            connection.execute("DELETE FROM compression_samples WHERE job_id = ?", (job_id,))
        for index, window in enumerate(windows):
            if cancelled():
                raise CompressionCancelled("使用者取消")
            target = staging / f"sample-{index + 1}.mp4"
            target.unlink(missing_ok=True)
            elapsed = _run_ffmpeg(_ffmpeg_command(cap, source, target, profile, info,
                                        start=window.start, length=window.duration,
                                        target_bitrate=sample_target_bitrate),
                        window.duration, cancelled,
                        lambda value, i=index: progress((i + value / 100) / len(windows) * 100),
                        space_path=root)
            sample_info = _probe(cap.ffprobe, target)
            video_bytes = _packet_bytes(cap.ffprobe, target, "v:0")
            audio_bytes = _packet_bytes(cap.ffprobe, target, "a:0") if sample_info["audio"] else 0
            actual_duration = min(window.duration, sample_info["duration"])
            width = int(sample_info["video"].get("width") or 0)
            height = int(sample_info["video"].get("height") or 0)
            metric, score, explanation = measure_sample(
                cap, source, target, window.start, actual_duration, width, height,
                hdr_or_rotation=hdr or width <= 0 or height <= 0)
            item = {"start": window.start, "duration": actual_duration,
                    "encode_elapsed_seconds": elapsed,
                    "video_bytes": video_bytes, "audio_bytes": audio_bytes,
                    "overhead_bytes": max(0, target.stat().st_size - video_bytes - audio_bytes),
                    "metric": metric, "score": score, "quality_note": explanation,
                    "sample_rel_path": target.relative_to(root).as_posix()}
            stats.append(item)
            with connection:
                connection.execute(
                    """INSERT INTO compression_samples
                       (id,job_id,start_seconds,duration_seconds,encoded_video_bytes,
                        quality_metric,quality_score,sample_stats_json)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (str(uuid4()), job_id, window.start, actual_duration, video_bytes,
                     metric, score, json.dumps(item, ensure_ascii=False)),
                )
        estimate = estimate_size(job["source_size"], info["duration"], stats)
        valid_scores = [item["score"] for item in stats if item["score"] is not None]
        metric = stats[0]["metric"] if valid_scores and all(
            item["metric"] == stats[0]["metric"] and item["score"] is not None for item in stats
        ) else "none"
        try:
            target_video_bitrate = _target_bitrate(job["source_size"], info, profile)
            target_bitrate_note = ""
        except CompressionError as exc:
            target_video_bitrate = None
            target_bitrate_note = str(exc)
        source_video_bitrate = info["video"].get("bit_rate")
        video_bitrate_approximate = not bool(source_video_bitrate)
        if video_bitrate_approximate:
            try:
                audio_rate = int(info["audio"].get("bit_rate") or 0) if info["audio"] else 0
            except (TypeError, ValueError):
                audio_rate = 0
            source_video_bitrate = max(0, round(job["source_size"] * 8 / info["duration"]) - audio_rate)
        details = {"samples": stats, "confidence": estimate.confidence, "note": estimate.note,
                   "sample_mode": "target" if sample_target_bitrate is not None else "quality",
                   "extra_streams": unusual, "hdr_or_rotation": hdr,
                   "source_duration": info["duration"],
                   "source_width": info["video"].get("width"),
                   "source_height": info["video"].get("height"),
                   "source_fps": info["video"].get("avg_frame_rate"),
                   "source_video_codec": info["video"].get("codec_name"),
                   "source_video_bitrate": source_video_bitrate,
                   "source_video_bitrate_approximate": video_bitrate_approximate,
                   "source_audio_bitrate": info["audio"].get("bit_rate") if info["audio"] else None,
                   "source_container_bitrate": info["format"].get("bit_rate"),
                   "target_size_bytes": int(job["source_size"] * (1 - profile.target_reduction_pct / 100)),
                   "target_video_bitrate": target_video_bitrate,
                   "target_bitrate_note": target_bitrate_note,
                   "quality_note": "取樣量測，非整部影片實測或保真百分比"}
        update_job(connection, job_id, status="analyzed", progress=100,
                   estimated_size_min=estimate.minimum, estimated_size_mid=estimate.midpoint,
                   estimated_size_max=estimate.maximum, estimated_reduction_pct=estimate.reduction_pct,
                   sample_quality_metric=metric,
                   sample_quality_score=min(valid_scores) if metric != "none" else None,
                   sample_quality_details_json=json.dumps(details, ensure_ascii=False),
                   finished_at=utc_now())
    except Exception:
        with connection:
            connection.execute("DELETE FROM compression_samples WHERE job_id = ?", (job_id,))
        shutil.rmtree(staging, ignore_errors=True)
        raise


def run_encode(connection, job_id: str, root: Path, cap: Capabilities,
               cancelled=lambda: False, progress=lambda value: None) -> None:
    job = get_job(connection, job_id)
    if job is None or job["status"] != "analyzed":
        raise CompressionError("請先完成試壓縮分析")
    source = _source_for_job(job, root)
    profile = Profile(**json.loads(job["profile_snapshot_json"])).validate()
    if not _encoder_available(cap, profile.encoder):
        raise CompressionError("所選編碼器不可用")
    info = _probe(cap.ffprobe, source)
    staging = _private_dir(root, ".videomanager", "compression-staging", job_id)
    target = staging / "full.partial"
    output_dir = _private_dir(root, ".videomanager", "compressed")
    output = output_dir / f"{source.stem}_compressed.mp4"
    serial = 2
    while output.exists():
        output = output_dir / f"{source.stem}_compressed_{serial}.mp4"
        serial += 1
    need = max(int(job["estimated_size_max"] or 0), job["source_size"]) + 512 * 2**20
    _space_guard(root, need)
    update_job(connection, job_id, status="encoding", started_at=utc_now(), progress=0,
               error_code=None, error_message=None)
    output_created = False
    discard_staging = False
    try:
        maximum = int(job["source_size"] * (1 - profile.target_reduction_pct / 100))
        used_target_bitrate, actual_encoder, actual_method = _resumable_encode(
            cap, source, target, staging, profile, info, job, cancelled, progress, root)
        if cancelled():
            raise _cancel_exception(cancelled)
        update_job(connection, job_id, status="validating", progress=99)
        progress(99.0)
        output_info = _validate_output(cap, info, target, profile)
        if target.stat().st_size > maximum:
            raise CompressionError("輸出未達所選縮小比例；原片保留")
        _source_for_job(job, root)
        marker = read_marker(root)
        if marker is None or marker.library_id != job["library_id"] or output.exists():
            raise CompressionError("影片庫或目的路徑已變動")
        target.rename(output)
        output_created = True
        actual = output.stat().st_size
        reduction = (1 - actual / job["source_size"]) * 100
        details = json.loads(job["sample_quality_details_json"] or "{}")
        try:
            actual_video_rate = int(output_info["video"].get("bit_rate") or 0)
        except (TypeError, ValueError):
            actual_video_rate = 0
        try:
            actual_audio_rate = int(output_info["audio"].get("bit_rate") or 0) if output_info["audio"] else 0
        except (TypeError, ValueError):
            actual_audio_rate = 0
        approximate_video_rate = actual_video_rate <= 0
        if approximate_video_rate:
            actual_video_rate = max(0, round(actual * 8 / output_info["duration"]) - actual_audio_rate)
        details.update({
            "actual_method": actual_method,
            "actual_encoder": actual_encoder,
            "requested_video_bitrate": used_target_bitrate,
            "actual_video_bitrate": actual_video_rate,
            "actual_video_bitrate_approximate": approximate_video_rate,
            "actual_audio_bitrate": actual_audio_rate,
            "actual_total_bitrate": round(actual * 8 / output_info["duration"]),
        })
        with connection:
            connection.execute(
                """INSERT INTO video_variants
                   (id,source_video_id,library_id,rel_path,codec,size_bytes,created_at,active_variant,original_preserved)
                   VALUES (?,?,?,?,?,?,?,0,1)""",
                (str(uuid4()), job["video_id"], job["library_id"], output.relative_to(root).as_posix(),
                 output_info["video"].get("codec_name"), actual, utc_now()),
            )
            connection.execute(
                """UPDATE compression_jobs SET status='completed', output_rel_path=?, actual_size=?,
                   actual_reduction_pct=?, sample_quality_details_json=?, progress=100,
                   finished_at=? WHERE id=?""",
                (output.relative_to(root).as_posix(), actual, reduction,
                 json.dumps(details, ensure_ascii=False), utc_now(), job_id),
            )
        discard_staging = True
    except CompressionCancelled:
        discard_staging = True
        raise
    except Exception:
        if output_created:
            output.unlink(missing_ok=True)
        raise
    finally:
        target.unlink(missing_ok=True)
        if discard_staging:
            shutil.rmtree(staging, ignore_errors=True)
        with connection:
            connection.execute("DELETE FROM compression_samples WHERE job_id = ?", (job_id,))


def fail_job(connection, job_id: str, exc: Exception) -> None:
    update_job(connection, job_id, status="cancelled" if isinstance(exc, CompressionCancelled) else "failed",
               error_code=type(exc).__name__, error_message=str(exc)[-1200:], finished_at=utc_now())
