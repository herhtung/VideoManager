"""Offline compression center with analysis, queue, results and persistent history."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QLineEdit, QMessageBox, QPushButton, QProgressBar, QSpinBox,
    QScrollArea, QStackedWidget, QTabWidget, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from videomanager.ui.notifications import notify_information
from videomanager.compression.capability_probe import Capabilities, probe_capabilities
from videomanager.compression.profiles import PRESETS, Profile, builtin
from videomanager.compression import repository as jobs
from videomanager.compression.safe_replace import replace_original, restore_original
from videomanager.compression import simple_editor as edits
from videomanager.compression.worker import _target_encoder, fail_job, run_analysis, run_encode
from videomanager.config import AppPaths
from videomanager.db.database import open_database
from videomanager.ui.video_picker import configure_video_grid, placeholder_icon, video_item


STATUS = {"queued": "等待試壓縮", "analyzing": "試壓縮中", "analyzed": "待確認排程", "encoding": "壓縮中",
          "validating": "驗證中", "completed": "待確認替換", "cancelled": "已取消", "failed": "失敗",
          "replaced": "已替換"}


def _size(value: int | None) -> str:
    if value is None:
        return "尚未分析"
    return f"{value / 2**30:.2f} GB" if value >= 2**30 else f"{value / 2**20:.1f} MB"


def _bitrate(value: int | str | None) -> str:
    try:
        rate = int(value or 0)
    except (TypeError, ValueError):
        rate = 0
    if rate <= 0:
        return "未提供"
    return f"{rate / 1_000_000:.2f} Mbps" if rate >= 1_000_000 else f"{rate / 1000:.0f} kbps"


def _low_bitrate_warning(row) -> str:
    """Advisory only; complexity and codec make a fixed quality cutoff impossible."""
    details = json.loads(row["sample_quality_details_json"] or "{}")
    try:
        width, height = int(details["source_width"]), int(details["source_height"])
        numerator, denominator = (float(part) for part in details["source_fps"].split("/"))
        fps = numerator / denominator
        source_rate = int(details.get("source_video_bitrate") or 0)
        target_rate = int(details.get("target_video_bitrate") or 0)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return ""
    pixels_per_second = width * height * fps
    if pixels_per_second <= 0:
        return ""
    findings = []
    if source_rate and source_rate / pixels_per_second < .03:
        findings.append("原片視訊位元率相對解析度與影格率偏低")
    if target_rate and target_rate / pixels_per_second < .02:
        findings.append("目標視訊位元率偏低，畫質可能明顯下降")
    return "；".join(findings)


def _encoder_name(value: str) -> str:
    return {"libx265": "H.265 CPU", "libkvazaar": "H.265 CPU",
            "libopenh264": "H.264 CPU", "hevc_nvenc": "H.265 NVIDIA GPU",
            "hevc_qsv": "H.265 Intel GPU", "libx264": "H.264 CPU",
            "h264_nvenc": "H.264 NVIDIA GPU", "h264_qsv": "H.264 Intel GPU"}.get(value, value)


def _planned_method(row, profile: dict, cap: Capabilities | None = None) -> tuple[bool | None, str]:
    """Match the encoder's CRF-first decision; actual output can still fall back."""
    if row["estimated_size_mid"] is None:
        return None, "先按「試壓縮」，才會決定正式輸出方式"
    if (profile["encoder"].endswith("_qsv") and profile.get("target_passes", 2) == 1):
        return True, (f"直接用 {_encoder_name(profile['encoder'])} · 快速單遍目標位元率；"
                      "Intel 硬體編碼省去 CPU 雙遍與全片品質模式")
    maximum = int(row["source_size"] * (1 - profile.get("target_reduction_pct", 10) / 100))
    target_encoder, passes = (_target_encoder(cap, Profile(**profile)) if cap else
                              (profile["encoder"], profile.get("target_passes", 2)))
    if profile["encoder"] != "libopenh264" and row["estimated_size_mid"] <= maximum:
        quality = ("CQ" if profile["encoder"].endswith(("_nvenc", "_qsv")) else
                   "QP" if profile["encoder"] == "libkvazaar" else "CRF")
        fallback = "快速單遍目標位元率" if passes == 1 else "CPU 雙遍目標位元率"
        return False, (f"先用 {_encoder_name(profile['encoder'])} · {quality} {profile['quality']}；"
                       f"若成品超過目標容量，再改用{fallback}")
    encoder = (target_encoder if cap else profile["encoder"] if passes == 1 else profile["encoder"].replace(
        "hevc_nvenc", "libx265").replace("h264_nvenc", "libx264")
        .replace("hevc_qsv", "libx265").replace("h264_qsv", "libx264"))
    strategy = "快速單遍" if passes == 1 else "雙遍"
    return True, f"直接用 {_encoder_name(encoder)} · {strategy}目標位元率（略過 CRF/CQ 全片輸出）"


def _compression_advice(row, details: dict, planned_target: bool | None,
                        profile: dict) -> tuple[str, str, str]:
    if planned_target is None:
        return "先做試壓縮", "目前還沒有取樣結果，無法判斷這部片適合哪個方式。", "#98afb8"
    if details.get("target_bitrate_note"):
        return "不建議執行目前設定", details["target_bitrate_note"], "#f4b5a5"
    warning = _low_bitrate_warning(row)
    if warning:
        return "不建議直接壓縮", warning + "；仍可自行選擇產生副本。", "#f4b5a5"
    try:
        source_rate = int(details.get("source_video_bitrate") or 0)
        target_rate = int(details.get("target_video_bitrate") or 0)
    except (TypeError, ValueError):
        source_rate = target_rate = 0
    if planned_target and source_rate and target_rate / source_rate < .72:
        strong_reduction = profile.get("target_reduction_pct", 10) >= 45
        return ("建議先選較小的縮小比例",
                f"視訊位元率預計從 {_bitrate(source_rate)} 降到 {_bitrate(target_rate)}；" +
                ("接近 60% 可能明顯降低畫質，先看目標位元率試壓縮並播放副本。"
                 if strong_reduction else "可先從 10% 試起，檢查副本再決定是否提高。"), "#f3d29a")
    if planned_target:
        return "可以先試一份副本", "目標位元率模式會控制容量；畫質須以完成的副本確認。", "#8ed8c5"
    return "可以先試一份副本", "先採用品質模式；若成品容量超標，會自動改用目標位元率。", "#8ed8c5"


def _duration_hm(seconds: float | int | None) -> str:
    if not seconds:
        return "未知"
    minutes = round(float(seconds) / 60)
    if minutes < 1:
        return "不足 1 分"
    return f"{minutes // 60} 小時 {minutes % 60} 分" if minutes >= 60 else f"{minutes} 分"


def _recorded_elapsed(row) -> str:
    if not row["started_at"]:
        return ""
    try:
        started = datetime.fromisoformat(row["started_at"])
        finished = datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else datetime.now(timezone.utc)
        return _duration_hm(max(0, (finished - started).total_seconds()))
    except (TypeError, ValueError):
        return ""


def _analysis_html(row, profile: dict, show_details: bool,
                   cap: Capabilities | None = None) -> str:
    pct = profile.get("target_reduction_pct", 10)
    display_passes = _target_encoder(cap, Profile(**profile))[1] if cap else profile.get("target_passes", 2)
    if (profile["encoder"].endswith("_qsv") and profile.get("target_passes", 2) == 1) or profile["encoder"] == "libopenh264":
        selected = f"{_encoder_name(profile['encoder'])} · 目標縮小 {pct}% · 快速單遍"
    else:
        quality_label = ("CQ" if profile["encoder"].endswith("_nvenc") else
                         "QP" if profile["encoder"] == "libkvazaar" else "CRF")
        selected = (f"{escape(profile['name'])} · {_encoder_name(profile['encoder'])} · "
                    f"{quality_label} {profile['quality']} · "
                    f"目標縮小 {pct}% · {'快速單遍' if display_passes == 1 else '較準雙遍'}")
    planned_target, method = _planned_method(row, profile, cap)
    details = json.loads(row["sample_quality_details_json"] or "{}")
    advice, explanation, color = _compression_advice(row, details, planned_target, profile)
    html = ["<html><body style='color:#eaf0f2;font-family:Microsoft JhengHei UI;'>",
            f"<p style='color:#98afb8;'>你選的設定<br><b style='color:#eaf0f2;'>{selected}</b></p>",
            f"<h2 style='color:{color};margin-bottom:3px;'>{escape(advice)}</h2>",
            f"<p style='margin-top:0;'>{escape(explanation)}</p>",
            "<h3 style='color:#69d8cb;'>正式輸出會怎麼做</h3>",
            f"<p><b>{escape(method)}</b></p>"]
    if planned_target is None:
        html += [f"<p>原片容量：{_size(row['source_size'])}</p>", "</body></html>"]
        return "".join(html)
    target_bytes = details.get("target_size_bytes") or int(row["source_size"] * (1 - pct / 100))
    duration = details.get("source_duration")
    source_total = details.get("source_container_bitrate")
    if not source_total and duration:
        source_total = round(row["source_size"] * 8 / duration)
    target_total = round(target_bytes * 8 / duration) if duration else None
    source_video = _bitrate(details.get("source_video_bitrate"))
    if details.get("source_video_bitrate_approximate"):
        source_video += "（約）"
    if planned_target:
        after_video = _bitrate(details.get("target_video_bitrate"))
        after_note = ("單遍" if display_passes == 1 else "雙遍") + "編碼設定值；成品另驗證"
    else:
        samples = details.get("samples", [])
        sampled_seconds = sum(item.get("duration", 0) for item in samples)
        sampled_bytes = sum(item.get("video_bytes", 0) for item in samples)
        after_video = _bitrate(round(sampled_bytes * 8 / sampled_seconds)) if sampled_seconds else "尚無估算"
        after_note = "取樣估算；超標會改目標位元率輸出"
    html += ["<h3 style='color:#69d8cb;'>容量與位元率</h3>",
             "<table width='100%' cellspacing='0' cellpadding='9'>",
             "<tr bgcolor='#22333d'><th align='left'>項目</th><th align='left'>原片</th>"
             "<th align='left'>這次預計</th></tr>",
             f"<tr bgcolor='#1b2932'><td>容量</td><td>{_size(row['source_size'])}</td>"
             f"<td>最多 {_size(target_bytes)}</td></tr>",
             f"<tr bgcolor='#22333d'><td>視訊位元率</td><td>{escape(source_video)}</td>"
             f"<td>{escape(after_video)}<br><small>{escape(after_note)}</small></td></tr>",
             f"<tr bgcolor='#1b2932'><td>整體位元率</td><td>{_bitrate(source_total)}</td>"
             f"<td>容量上限換算 {_bitrate(target_total)}</td></tr>",
             "</table>"]
    if planned_target and duration:
        samples = details.get("samples", [])
        sampled_seconds = sum(float(item.get("duration") or 0) for item in samples)
        encode_seconds = sum(float(item.get("encode_elapsed_seconds") or 0) for item in samples)
        if sampled_seconds > 0 and encode_seconds > 0:
            estimate_seconds = duration * encode_seconds / sampled_seconds * display_passes
            level = "#f3d29a" if estimate_seconds <= 7200 else "#f4b5a5"
            html += [f"<p style='color:{level};'>依試壓縮片段的編碼速度推估，正式輸出約需 "
                     f"{_duration_hm(estimate_seconds)}。這是粗估，長片與不同場景可能較久；"
                     "工作不會因超過兩小時而自動停止。</p>"]
        if display_passes == 2:
            one_x = _duration_hm(duration * 2)
            html += [f"<p style='color:#f3d29a;'>這部影片長 {_duration_hm(duration)}。雙遍若每遍都以原速處理，"
                     f"約需 {one_x}；實際時間依電腦速度而定。</p>"]
        else:
            html += [f"<p style='color:#98afb8;'>這部影片長 {_duration_hm(duration)}。快速單遍通常較快，"
                     "容量估算較不準；超過目標會再試一次，仍未達標便不建立副本。</p>"]
    if details.get("actual_method"):
        actual_method = {"two_pass_target": "雙遍目標位元率",
                         "one_pass_target": "快速單遍目標位元率",
                         "quality": "CRF/CQ 品質模式"}.get(details["actual_method"], "未知")
        html += [f"<p><b>已完成：實際使用 {_encoder_name(details.get('actual_encoder', profile['encoder']))}"
                 f" · {actual_method}</b></p>"]
    if row["status"] == "cancelled":
        html += ["<p style='color:#f3d29a;'>工作已取消；原片未受影響。</p>"]
    elif row["error_message"]:
        html += [f"<p style='color:#f4b5a5;'>工作未完成：{escape(row['error_message'])}</p>"]
    if show_details:
        score = row["sample_quality_score"]
        metric = row["sample_quality_metric"]
        score_text = (f"{metric.upper()} {score:.3f}" if score is not None and metric != "none"
                      else "沒有可靠分數")
        sampled_target = details.get("sample_mode") == "target"
        html += [f"<h3 style='color:#98afb8;'>"
                 f"{'目標位元率' if sampled_target else 'CRF/CQ'} 試壓縮細節</h3>",
                 f"<p>取樣預估容量：{_size(row['estimated_size_min'])}～{_size(row['estimated_size_max'])}；"
                 f"取樣畫質：{escape(score_text)}；可信度：{escape(details.get('confidence', '未知'))}。</p>",
                 ("<p>取樣畫質來自目標位元率的局部片段；全片畫質仍需播放副本確認。</p>"
                  if sampled_target else
                  "<p>取樣畫質只適用於 CRF/CQ 樣本，不能代表目標位元率成品。</p>"),
                 f"<p>{escape(details.get('note', ''))}</p>"]
        for index, item in enumerate(details.get("samples", []), 1):
            measured = (f"{item['metric'].upper()} {item['score']:.3f}"
                        if item.get("score") is not None else "分數不可用")
            html += [f"<p>第 {index} 段：{item['start']:.1f}～"
                     f"{item['start'] + item['duration']:.1f} 秒 · {escape(measured)}</p>"]
    html += ["</body></html>"]
    return "".join(html)


def _result_html(row, profile: dict) -> str:
    details = json.loads(row["sample_quality_details_json"] or "{}")
    method = {"two_pass_target": "雙遍目標位元率",
              "one_pass_target": "快速單遍目標位元率",
              "quality": "CRF/CQ 品質模式"}.get(details.get("actual_method"), "舊版工作未記錄")
    encoder = _encoder_name(details.get("actual_encoder") or profile["encoder"])
    actual_video = _bitrate(details.get("actual_video_bitrate"))
    if details.get("actual_video_bitrate_approximate"):
        actual_video += "（約）"
    source_total = details.get("source_container_bitrate")
    if not source_total and details.get("source_duration"):
        source_total = round(row["source_size"] * 8 / details["source_duration"])
    return ("<html><body style='color:#eaf0f2;font-family:Microsoft JhengHei UI;'>"
            f"<h2 style='color:#69d8cb;'>實際採用：{escape(encoder)} · {method}</h2>"
            "<table width='100%' cellspacing='0' cellpadding='9'>"
            "<tr bgcolor='#22333d'><th align='left'>項目</th><th align='left'>原片</th>"
            "<th align='left'>壓縮副本</th></tr>"
            f"<tr bgcolor='#1b2932'><td>容量</td><td>{_size(row['source_size'])}</td>"
            f"<td>{_size(row['actual_size'])}</td></tr>"
            f"<tr bgcolor='#22333d'><td>視訊位元率</td><td>{_bitrate(details.get('source_video_bitrate'))}</td>"
            f"<td>{escape(actual_video)}</td></tr>"
            f"<tr bgcolor='#1b2932'><td>整體位元率</td><td>{_bitrate(source_total)}</td>"
            f"<td>{_bitrate(details.get('actual_total_bitrate'))}</td></tr></table>"
            f"<p>實際縮小 {row['actual_reduction_pct']:.1f}% · 節省 "
            f"{_size(row['source_size'] - row['actual_size'])}</p>"
            "<p>請播放副本比較畫質；原片仍保留，只有再次確認才會替換。</p>"
            f"<p style='color:#98afb8;'>{escape(row['source_rel_path'] if row['status'] == 'replaced' else row['output_rel_path'] or '')}</p>"
            "</body></html>")


class CapabilityThread(QThread):
    ready = Signal(object)

    def run(self) -> None:
        self.ready.emit(probe_capabilities())


class _WorkerControl:
    def __init__(self, worker: "CompressionThread") -> None:
        self.worker = worker

    def __call__(self) -> bool:
        return self.worker.isInterruptionRequested()

    def pause_requested(self) -> bool:
        return self.worker._paused.is_set()

    def process_started(self) -> None:
        self.worker.process_started.emit()

    def preserve_checkpoint_requested(self) -> bool:
        return self.worker._preserve_checkpoint_on_exit


class CompressionThread(QThread):
    progress_changed = Signal(float)
    process_started = Signal()
    result = Signal(str)

    def __init__(self, paths: AppPaths, root: Path, job_id: str, mode: str,
                 capabilities: Capabilities, sample_count: int, parent=None) -> None:
        super().__init__(parent)
        self.paths, self.root, self.job_id = paths, root, job_id
        self.mode, self.capabilities, self.sample_count = mode, capabilities, sample_count
        self._paused = threading.Event()
        self._preserve_checkpoint_on_exit = False

    def set_paused(self, paused: bool) -> None:
        self._paused.set() if paused else self._paused.clear()

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def interrupt_for_exit(self) -> None:
        self._preserve_checkpoint_on_exit = True
        self.requestInterruption()

    def run(self) -> None:
        connection = open_database(self.paths.database, self.paths.backups)
        control = _WorkerControl(self)
        try:
            if self.mode == "analyze":
                run_analysis(connection, self.job_id, self.root, self.capabilities,
                             self.sample_count, control,
                             self.progress_changed.emit)
            elif self.mode == "encode":
                run_encode(connection, self.job_id, self.root, self.capabilities,
                           control, self.progress_changed.emit)
            elif self.mode == "restore":
                restore_original(connection, self.root, self.paths.cache, self.job_id,
                                 self.capabilities, self.paths.settings)
            else:
                replace_original(connection, self.root, self.paths.cache, self.job_id,
                                 self.capabilities, self.paths.settings)
            self.result.emit("")
        except Exception as exc:
            if self.mode in {"analyze", "encode"}:
                try:
                    fail_job(connection, self.job_id, exc)
                except sqlite3.Error:
                    pass
            self.result.emit(str(exc))
        finally:
            connection.close()


class CutThread(QThread):
    progress_changed = Signal(float)
    result = Signal(str)

    def __init__(self, paths: AppPaths, root: Path, edit_id: str, mode: str,
                 capabilities: Capabilities, parent=None) -> None:
        super().__init__(parent)
        self.paths, self.root, self.edit_id = paths, root, edit_id
        self.mode, self.capabilities = mode, capabilities

    def run(self) -> None:
        connection = open_database(self.paths.database, self.paths.backups)
        try:
            if self.mode == "cut":
                edits.run_edit(connection, self.edit_id, self.root, self.capabilities,
                               self.isInterruptionRequested, self.progress_changed.emit)
            else:
                edits.replace_edit(connection, self.root, self.paths.cache, self.edit_id,
                                   self.capabilities)
            self.result.emit("")
        except Exception as exc:
            if self.mode == "cut":
                try:
                    edits.fail_edit(connection, self.edit_id, exc)
                except sqlite3.Error:
                    pass
            self.result.emit(str(exc))
        finally:
            connection.close()


class CompressionCenter(QDialog):
    library_changed = Signal()
    job_progress = Signal(int, str)
    job_notice = Signal(str)
    replacement_active = Signal(bool)
    back_requested = Signal()
    play_path_requested = Signal(str, str)

    def __init__(self, paths: AppPaths, connection: sqlite3.Connection,
                 root: Path | None, library_id: str | None, initial_ids: list[str] | None = None,
                 parent=None, *, embedded: bool = False, edit_only: bool = False) -> None:
        super().__init__(parent)
        self.embedded = embedded
        self.edit_only = edit_only
        if embedded:
            self.setWindowFlags(Qt.Widget)
        self.setWindowTitle("剪輯中心" if edit_only else "影片壓縮中心")
        self.resize(1220, 850)
        self.paths, self.connection, self.root, self.library_id = paths, connection, root, library_id
        self.capabilities: Capabilities | None = None
        self.worker: CompressionThread | None = None
        self.analysis_worker: CompressionThread | None = None
        self.replacement_worker: CompressionThread | None = None
        self._batch_replace_queue: list[str] = []
        self._batch_replace_total = 0
        self._batch_replace_success = 0
        self._batch_replace_errors: dict[str, str] = {}
        self._batch_replacing_id: str | None = None
        self._batch_replace_timer = QTimer(self)
        self._batch_replace_timer.setInterval(1000)
        self._batch_replace_timer.timeout.connect(self._pump_batch_replacements)
        self.cut_worker: CutThread | None = None
        self._analysis_waiting: list[str] = []
        self._batch_accepted_bitrate_warning = False
        self._closing = False
        self._initial_ids = set(initial_ids or [])
        self._job_started_at: dict[str, float] = {}
        self._progress_by_job: dict[str, float] = {}
        self._last_progress_change: dict[str, float] = {}
        self._loading_profile = False
        self._loaded_profile_job_id: str | None = None
        self._analysis_paused = False
        self._encode_paused = False
        self._profile_save_timer = QTimer(self)
        self._profile_save_timer.setSingleShot(True)
        self._profile_save_timer.timeout.connect(
            lambda: self._save_current_profile(self._loaded_profile_job_id)
        )
        self._build_ui()
        self._load_videos()
        if not self.edit_only:
            self.refresh_jobs()
        self.cap_thread = CapabilityThread(self)
        self.cap_thread.ready.connect(self._capabilities_ready)
        self.cap_thread.finished.connect(lambda: self.close() if self._closing and
                                         self.worker is None and self.analysis_worker is None and
                                         self.replacement_worker is None and
                                         self.cut_worker is None else None)
        self.cap_thread.start()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        heading_row = QHBoxLayout()
        heading = QLabel("剪輯中心" if self.edit_only else "影片壓縮中心")
        heading.setObjectName("sectionLabel")
        heading_row.addWidget(heading, 1)
        if not self.edit_only:
            self.confirm_all_replace_button = QPushButton("確認全部待替換…")
            self.confirm_all_replace_button.clicked.connect(self.confirm_all_replace)
            heading_row.addWidget(self.confirm_all_replace_button)
            self.history_button = QPushButton("歷史紀錄")
            self.history_button.clicked.connect(self.show_history)
            heading_row.addWidget(self.history_button)
        back = QPushButton("返回影片庫" if self.embedded else "關閉")
        back.clicked.connect(self.back_requested.emit if self.embedded else self.close)
        heading_row.addWidget(back)
        layout.addLayout(heading_row)
        if not self.edit_only:
            layout.addWidget(QLabel("選影片 → 試壓縮 → 排程壓縮 → 檢查副本 → 確認替換"))
        self.cap_label = QLabel("正在偵測本機 FFmpeg 與編碼器…")
        self.cap_label.setWordWrap(True)
        layout.addWidget(self.cap_label)
        if self.edit_only:
            self.tabs = QTabWidget()
            layout.addWidget(self.tabs, 1)
            self._cut_tab()
            return
        self.workspace = QStackedWidget()
        workflow = QWidget()
        workflow_layout = QVBoxLayout(workflow)
        content = QHBoxLayout()
        left = QVBoxLayout()
        self.active_heading = QLabel("影片工作 · 0 部")
        self.active_heading.setObjectName("sectionLabel")
        left.addWidget(self.active_heading)
        add_video = QPushButton("加入影片…")
        add_video.clicked.connect(self.open_video_picker)
        left.addWidget(add_video)
        self.jobs_list = QListWidget()
        self.jobs_list.setMinimumWidth(230)
        self.jobs_list.setMaximumWidth(350)
        self.jobs_list.setIconSize(QSize(110, 62))
        self.jobs_list.setWordWrap(True)
        self.jobs_list.setStyleSheet("QListWidget::item { padding: 7px; border-bottom: 1px solid #31434b; }"
                                     "QListWidget::item:selected { background: #236e70; color: white; }")
        self.jobs_list.currentItemChanged.connect(lambda *_: self._on_active_selected())
        left.addWidget(self.jobs_list, 1)
        content.addLayout(left)
        right = QVBoxLayout()
        self.job_context = QLabel("請從左側選取要處理的影片")
        self.job_context.setObjectName("sectionLabel")
        self.job_context.setWordWrap(True)
        right.addWidget(self.job_context)
        self.tabs = QTabWidget()
        right.addWidget(self.tabs, 1)
        content.addLayout(right, 1)
        workflow_layout.addLayout(content, 1)
        self.workspace.addWidget(workflow)
        layout.addWidget(self.workspace, 1)
        self._settings_tab()
        self._analysis_tab()
        self._queue_tab()
        self._history_tab()
        self.tabs.currentChanged.connect(self._tab_changed)

    def _settings_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("設定只適用於左側選取的影片；修改後可再次試壓縮。"))
        self.video_picker = QDialog(self)
        self.video_picker.setWindowTitle("加入影片至壓縮中心")
        self.video_picker.resize(860, 700)
        picker_layout = QVBoxLayout(self.video_picker)
        picker_layout.addWidget(QLabel("選取要加入左側工作列表的影片"))
        selector = QHBoxLayout()
        self.video_search = QLineEdit()
        self.video_search.setPlaceholderText("搜尋顯示名稱或檔名…")
        self.video_search.textChanged.connect(self._filter_videos)
        self.only_selected = QCheckBox("只顯示這次選取")
        self.only_selected.setChecked(bool(self._initial_ids))
        self.only_selected.toggled.connect(self._filter_videos)
        selector.addWidget(self.video_search, 1)
        selector.addWidget(self.only_selected)
        picker_layout.addLayout(selector)
        self.selection_count = QLabel("尚未選取影片")
        picker_layout.addWidget(self.selection_count)
        self.videos = QListWidget()
        configure_video_grid(self.videos)
        self.videos.setMinimumHeight(180)
        self.videos.itemSelectionChanged.connect(self._selection_changed)
        picker_layout.addWidget(self.videos, 1)
        form = QFormLayout()
        self.mode = QComboBox()
        for name, _, _ in PRESETS:
            self.mode.addItem(name)
        self.mode.setCurrentIndex(2)
        self.encoder = QComboBox()
        for label, key in (("H.265 Intel GPU · 快速", "hevc_qsv"),
                           ("H.265 CPU · 省容量", "libx265"),
                           ("H.265 CPU · 相容版", "libkvazaar"),
                           ("H.265 NVIDIA GPU · 快速", "hevc_nvenc"),
                           ("H.264 CPU · 高相容", "libx264"),
                           ("H.264 CPU · 相容版", "libopenh264"),
                           ("H.264 Intel GPU", "h264_qsv"),
                           ("H.264 NVIDIA GPU", "h264_nvenc")):
            self.encoder.addItem(label, key)
        self.quality = QSpinBox()
        self.quality.setRange(0, 51)
        self.quality.setValue(23)
        self.quality_label = QLabel("CRF 品質值")
        self.speed = QComboBox()
        for name in ("ultrafast", "veryfast", "fast", "medium", "slow", "slower"):
            self.speed.addItem(name)
        self.speed.setCurrentText("medium")
        self.resolution = QComboBox()
        for label, value in (("維持原始", "source"), ("最高 1080p", "1080p"), ("最高 720p", "720p")):
            self.resolution.addItem(label, value)
        self.fps = QComboBox()
        self.fps.addItem("維持原始", "source")
        self.fps.addItem("30 FPS（可能影響流暢度）", "30")
        self.audio = QComboBox()
        for label, value in (("自動：相容音訊直接複製", "auto"), ("直接複製", "copy"),
                             ("AAC 128 kbps", "aac128"), ("AAC 192 kbps", "aac192"),
                             ("AAC 256 kbps", "aac256")):
            self.audio.addItem(label, value)
        self.samples = QComboBox()
        for label, value in (("快速 · 2 段", 2), ("標準 · 4 段", 4), ("精準 · 8 段", 8)):
            self.samples.addItem(label, value)
        self.samples.setCurrentIndex(1)
        self.target_reduction = QSpinBox()
        self.target_reduction.setRange(5, 60)
        self.target_reduction.setSuffix(" %")
        self.target_reduction.setValue(10)
        self.target_passes = QComboBox()
        self.target_passes.addItem("雙遍 · 容量較準確", 2)
        self.target_passes.addItem("快速單遍 · 可能需要重試", 1)
        self.mode.currentIndexChanged.connect(self._preset_changed)
        self.encoder.currentIndexChanged.connect(self._encoder_changed)
        form.addRow("品質模式", self.mode)
        form.addRow("編碼器", self.encoder)
        form.addRow(self.quality_label, self.quality)
        form.addRow("編碼速度", self.speed)
        form.addRow("解析度", self.resolution)
        form.addRow("影格率", self.fps)
        form.addRow("音訊", self.audio)
        form.addRow("目標縮小比例", self.target_reduction)
        form.addRow("容量控制方式", self.target_passes)
        form.addRow("取樣", self.samples)
        layout.addLayout(form)
        note = QLabel("Intel GPU 快速單遍直接用目標位元率，避免重編整部影片。"
                      "其他模式先用所選品質模式；若達不到目標容量，再用選定的容量控制方式。"
                      "雙遍會用 CPU；快速單遍可用所選的 GPU 編碼器（需驅動支援）。"
                      "接近 60% 可能明顯影響畫質；先看目標位元率試壓縮，再檢查完成的副本。")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(QLabel("副本保存在原磁碟的 .videomanager/compressed；分析暫存亦在同一磁碟。"))
        self.queue_button = QPushButton("加入處理清單")
        self.queue_button.clicked.connect(self.queue_selection)
        self.queue_button.setEnabled(False)
        picker_layout.addWidget(self.queue_button)
        self.tabs.addTab(page, "設定")
        for field in (self.mode, self.encoder, self.quality, self.speed, self.resolution,
                      self.fps, self.audio, self.target_reduction, self.target_passes):
            signal = field.valueChanged if isinstance(field, QSpinBox) else field.currentIndexChanged
            signal.connect(self._profile_edited)

    def open_video_picker(self) -> None:
        self.video_picker.exec()

    def _analysis_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.analysis_text = QTextBrowser()
        self.analysis_text.setStyleSheet(
            "QTextBrowser { background: #172129; color: #eaf0f2; border: 1px solid #2b3a43;"
            " border-radius: 8px; padding: 12px; }"
        )
        self.analysis_text.setText("尚未分析。選取左側影片後按「試壓縮」。")
        layout.addWidget(self.analysis_text, 1)
        self.analysis_details = QCheckBox("顯示取樣與技術細節")
        self.analysis_details.toggled.connect(self._show_job)
        layout.addWidget(self.analysis_details)
        self.schedule_from_analysis = QPushButton("確認試壓縮結果，排隊壓縮")
        self.schedule_from_analysis.clicked.connect(self.schedule_selected)
        self.schedule_from_analysis.setEnabled(False)
        layout.addWidget(self.schedule_from_analysis)
        row = QHBoxLayout()
        original = QPushButton("開啟原片")
        original.clicked.connect(lambda: self._open_path("source"))
        sample = QPushButton("開啟取樣片段")
        sample.clicked.connect(lambda: self._open_path("sample"))
        row.addWidget(original)
        row.addWidget(sample)
        row.addStretch()
        layout.addLayout(row)
        self.tabs.addTab(page, "試壓縮分析")

    def _queue_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.next_step = QLabel("加入影片後，這裡會顯示下一步。")
        self.next_step.setWordWrap(True)
        self.next_step.setStyleSheet("QLabel { background: #203841; border: 1px solid #376169;"
                                     " border-radius: 9px; padding: 16px; font-size: 16px; }")
        layout.addWidget(self.next_step)
        self.method_status = QLabel("尚未選取影片")
        self.method_status.setWordWrap(True)
        layout.addWidget(self.method_status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        self.progress_detail = QLabel("尚無執行中的工作")
        layout.addWidget(self.progress_detail)
        self.pause_current_button = QPushButton("暫停這部影片")
        self.pause_current_button.setEnabled(False)
        self.pause_current_button.clicked.connect(self.toggle_selected_pause)
        layout.addWidget(self.pause_current_button)
        row = QGridLayout()
        self.queue_actions = {}
        for index, (label, callback) in enumerate((("試壓縮", lambda: self._start_selected("analyze")),
                                ("確認排程壓縮", self.schedule_selected),
                                ("分析全部待執行", lambda: self._start_batch("analyze")),
                                ("壓縮全部已分析", lambda: self._start_batch("encode")),
                                ("暫停試壓縮排隊", self._toggle_analysis_queue),
                                ("暫停正式壓縮排隊", self._toggle_encode_queue),
                                ("取消這部工作", self.cancel_selected))):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button, index // 3, index % 3)
            self.queue_actions[label] = button
            if label == "暫停試壓縮排隊":
                self.pause_analysis_button = button
            elif label == "暫停正式壓縮排隊":
                self.pause_encode_button = button
        layout.addLayout(row)
        order = QHBoxLayout()
        up = QPushButton("上移")
        down = QPushButton("下移")
        up.clicked.connect(lambda: self._move_job(-1))
        down.clicked.connect(lambda: self._move_job(1))
        order.addWidget(up)
        order.addWidget(down)
        order.addStretch()
        layout.addLayout(order)
        self._results_tab(layout)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, "目前狀態")

    def _results_tab(self, parent_layout: QVBoxLayout) -> None:
        self.result_panel = QWidget()
        layout = QVBoxLayout(self.result_panel)
        layout.addWidget(QLabel("壓縮結果"))
        self.result_text = QTextBrowser()
        self.result_text.setStyleSheet(
            "QTextBrowser { background: #172129; color: #eaf0f2; border: 1px solid #2b3a43;"
            " border-radius: 8px; padding: 12px; }"
        )
        self.result_text.setText("尚無壓縮結果。")
        self.result_text.setMinimumHeight(150)
        layout.addWidget(self.result_text)
        self.show_variants = QCheckBox("顯示這部影片的壓縮副本")
        self.show_variants.toggled.connect(self._show_job)
        layout.addWidget(self.show_variants)
        self.variants_list = QListWidget()
        self.variants_list.hide()
        layout.addWidget(self.variants_list, 1)
        row = QHBoxLayout()
        open_copy = QPushButton("開啟壓縮副本")
        open_copy.clicked.connect(lambda: self._open_path("output"))
        replace = QPushButton("確認後替換原片…")
        replace.clicked.connect(self.confirm_replace)
        self.replace_button = replace
        replace.setEnabled(False)
        row.addWidget(open_copy)
        row.addWidget(replace)
        row.addStretch()
        layout.addLayout(row)
        parent_layout.addWidget(self.result_panel)
        self.result_panel.hide()

    def _history_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        back = QPushButton("← 返回處理中的影片")
        back.clicked.connect(self.show_workflow)
        layout.addWidget(back)
        self.history = QTableWidget(0, 5)
        self.history.setHorizontalHeaderLabels(["時間", "影片", "狀態", "編碼器", "原片保留／訊息"])
        self.history.horizontalHeader().setStretchLastSection(True)
        self.history.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history.setSelectionBehavior(QTableWidget.SelectRows)
        self.history.setSelectionMode(QTableWidget.SingleSelection)
        self.history.itemSelectionChanged.connect(self._history_changed)
        layout.addWidget(self.history)
        self.history_detail = QLabel("選取紀錄可查看原片保留狀態。")
        self.history_detail.setWordWrap(True)
        layout.addWidget(self.history_detail)
        self.restore_button = QPushButton("還原保留的原片…")
        self.restore_button.setEnabled(False)
        self.restore_button.clicked.connect(self.confirm_restore)
        layout.addWidget(self.restore_button)
        self.retry_history_button = QPushButton("重新加入處理清單")
        self.retry_history_button.setEnabled(False)
        self.retry_history_button.clicked.connect(self.retry_history_selected)
        layout.addWidget(self.retry_history_button)
        self.workspace.addWidget(page)

    def show_history(self) -> None:
        self.workspace.setCurrentIndex(1)
        self.history_button.setText("處理中的影片")
        self.history_button.clicked.disconnect()
        self.history_button.clicked.connect(self.show_workflow)
        self._history_changed()

    def show_workflow(self) -> None:
        self.workspace.setCurrentIndex(0)
        self.history_button.setText("歷史紀錄")
        self.history_button.clicked.disconnect()
        self.history_button.clicked.connect(self.show_history)
        self._show_job()

    def _cut_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        description = QLabel("排除一段不要的畫面，輸出新的 MP4 副本。時間可輸入秒數或 時:分:秒。"
                             "先播放檢查副本，確認後才可取代原片；原片會保留。")
        description.setWordWrap(True)
        layout.addWidget(description)
        form = QFormLayout()
        self.cut_videos = QComboBox()
        self.cut_videos.setMinimumWidth(450)
        self.cut_start = QLineEdit()
        self.cut_start.setPlaceholderText("例如 00:01:15")
        self.cut_end = QLineEdit()
        self.cut_end.setPlaceholderText("例如 00:02:30")
        form.addRow("來源影片", self.cut_videos)
        form.addRow("排除起點", self.cut_start)
        form.addRow("排除終點", self.cut_end)
        layout.addLayout(form)
        self.cut_button = QPushButton("產生剪輯副本")
        self.cut_button.setEnabled(False)
        self.cut_button.clicked.connect(self.create_cut)
        layout.addWidget(self.cut_button)
        self.cut_progress = QProgressBar()
        self.cut_progress.setRange(0, 100)
        layout.addWidget(self.cut_progress)
        self.cut_progress_detail = QLabel("尚無執行中的剪輯")
        layout.addWidget(self.cut_progress_detail)
        layout.addWidget(QLabel("剪輯紀錄 · 選擇已完成的項目可播放或取代"))
        self.edits_list = QListWidget()
        layout.addWidget(self.edits_list, 1)
        row = QHBoxLayout()
        for label, callback in (("開啟原片", lambda: self._open_edit_path("source")),
                                ("播放剪輯副本", lambda: self._open_edit_path("output")),
                                ("確認後取代原片…", self.confirm_cut_replace),
                                ("取消目前剪輯", self.cancel_cut)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        self.tabs.addTab(page, "簡易剪輯")
        self.refresh_edits()

    def _load_videos(self) -> None:
        if not self.library_id:
            return
        rows = self.connection.execute(
            """SELECT id, display_title, relative_path FROM videos WHERE library_id = ?
               AND deleted_at IS NULL AND missing_since IS NULL ORDER BY display_title COLLATE NOCASE""",
            (self.library_id,),
        ).fetchall()
        fallback = placeholder_icon()
        if not self.edit_only:
            self.videos.blockSignals(True)
        for row in rows:
            if not self.edit_only:
                item = video_item(row, self.paths.cache, fallback)
                item.setData(Qt.UserRole, row["id"])
                item.setData(Qt.UserRole + 1, f"{row['display_title']} {row['relative_path']}".casefold())
                self.videos.addItem(item)
                if row["id"] in self._initial_ids:
                    item.setSelected(True)
            if self.edit_only:
                self.cut_videos.addItem(row["display_title"], row["id"])
                if row["id"] in self._initial_ids:
                    self.cut_videos.setCurrentIndex(self.cut_videos.count() - 1)
        if not self.edit_only:
            self.videos.blockSignals(False)
            self._selection_changed()

    def _filter_videos(self) -> None:
        needle = self.video_search.text().casefold().strip()
        selected_only = self.only_selected.isChecked()
        for index in range(self.videos.count()):
            item = self.videos.item(index)
            item.setHidden((needle not in item.data(Qt.UserRole + 1)) or
                           (selected_only and not item.isSelected()))

    def _selection_changed(self) -> None:
        count = len(self.videos.selectedItems())
        self.selection_count.setText(f"這次選了 {count} 部影片" if count else "尚未選取影片")
        if count == 0 and self.only_selected.isChecked():
            self.only_selected.setChecked(False)
        else:
            self._filter_videos()

    def focus_videos(self, video_ids: list[str]) -> None:
        self.show_workflow()
        requested = set(video_ids)
        self.video_search.clear()
        self.videos.blockSignals(True)
        for index in range(self.videos.count()):
            item = self.videos.item(index)
            item.setSelected(item.data(Qt.UserRole) in requested)
        self.videos.blockSignals(False)
        self.only_selected.setChecked(bool(requested))
        self._selection_changed()
        if requested:
            first = next((self.videos.item(index) for index in range(self.videos.count())
                          if self.videos.item(index).data(Qt.UserRole) in requested), None)
            if first:
                self.videos.scrollToItem(first)
            if self.capabilities is not None:
                self._attach_videos_to_jobs(video_ids)
            else:
                self._initial_ids.update(requested)
        self.tabs.setCurrentIndex(0)

    def _attach_videos_to_jobs(self, video_ids: list[str]) -> None:
        if self.root is None or self.library_id is None:
            return
        last_job = None
        for video_id in video_ids:
            existing = self.connection.execute(
                """SELECT id FROM compression_jobs WHERE video_id=? AND library_id=?
                   AND status IN ('queued','analyzing','analyzed','encoding','validating','completed')
                   ORDER BY created_at DESC LIMIT 1""", (video_id, self.library_id),
            ).fetchone()
            if existing is not None:
                last_job = existing["id"]
            else:
                try:
                    last_job = jobs.queue_video(self.connection, self.root, self.library_id,
                                                video_id, self._profile())
                except (OSError, ValueError, sqlite3.Error) as exc:
                    self.job_notice.emit(str(exc))
        if last_job:
            self.refresh_jobs(last_job)

    def _capabilities_ready(self, cap: Capabilities) -> None:
        self.capabilities = cap
        names = "、".join(sorted(cap.encoders)) or "無"
        self.cap_label.setText("Intel GPU：" + ("可用" if cap.qsv_usable else "不可用") +
                               "  ·  NVIDIA GPU：" + ("可用" if cap.nvenc_usable else "不可用") +
                               "  ·  畫質取樣：" + ("可用" if cap.vmaf or cap.ssim else "不可用"))
        self.cap_label.setToolTip(f"{cap.version or cap.message}\n編碼器：{names}\n{cap.message}")
        if self.edit_only:
            self.cut_button.setEnabled(self.root is not None and self.library_id is not None
                                       and bool({"libx264", "libopenh264"}.intersection(cap.encoders)))
            return
        for index in range(self.encoder.count()):
            name = self.encoder.itemData(index)
            enabled = (name in cap.encoders
                       and (not name.endswith("_nvenc") or cap.nvenc_usable)
                       and (not name.endswith("_qsv") or cap.qsv_usable))
            self.encoder.model().item(index).setEnabled(enabled)
        # Capability probing runs after the list and a video's profile have
        # loaded. Choosing a hardware default here must not edit that video's
        # saved profile or reset an already analyzed job to "queued".
        blockers = [QSignalBlocker(control) for control in (self.encoder, self.target_passes, self.speed)]
        try:
            if cap.qsv_usable and "hevc_qsv" in cap.encoders:
                self.encoder.setCurrentIndex(self.encoder.findData("hevc_qsv"))
                self.target_passes.setCurrentIndex(self.target_passes.findData(1))
                self.speed.setCurrentText("fast")
            else:
                fallback = next((index for index in range(self.encoder.count())
                                 if self.encoder.model().item(index).isEnabled()), -1)
                if fallback >= 0:
                    self.encoder.setCurrentIndex(fallback)
        finally:
            del blockers
        if self._loaded_profile_job_id is not None:
            self._loaded_profile_job_id = None
            self._load_selected_profile()
            selected_id = self._selected_id()
            selected_job = jobs.get_job(self.connection, selected_id) if selected_id else None
            chosen = self.encoder.currentIndex()
            if (selected_job is not None and selected_job["status"] in {"queued", "analyzed"}
                    and chosen >= 0 and not self.encoder.model().item(chosen).isEnabled()):
                fallback = next((index for index in range(self.encoder.count())
                                 if self.encoder.model().item(index).isEnabled()), -1)
                if fallback >= 0:
                    self.encoder.setCurrentIndex(fallback)
                    self._save_current_profile(selected_id)
        self.queue_button.setEnabled(self.root is not None and self.library_id is not None and bool(cap.encoders))
        if self._initial_ids:
            initial = list(self._initial_ids)
            self._initial_ids.clear()
            self._attach_videos_to_jobs(initial)
        self._pump_analysis()
        self._pump_encode()
        self._update_replace_all_button()

    def _load_selected_profile(self) -> None:
        identifier = self._selected_id()
        if identifier == self._loaded_profile_job_id:
            return
        self._save_current_profile(self._loaded_profile_job_id)
        self._loaded_profile_job_id = identifier
        row = jobs.get_job(self.connection, identifier) if identifier else None
        editable = bool(row and row["status"] in {"queued", "analyzed"})
        controls = (self.mode, self.encoder, self.quality, self.speed, self.resolution,
                    self.fps, self.audio, self.target_reduction, self.target_passes)
        if row is not None:
            profile = Profile(**json.loads(row["profile_snapshot_json"]))
            self._loading_profile = True
            blockers = [QSignalBlocker(control) for control in controls]
            try:
                mode_index = next((i for i, preset in enumerate(PRESETS) if preset[0] == profile.name), 2)
                self.mode.setCurrentIndex(mode_index)
                self.encoder.setCurrentIndex(self.encoder.findData(profile.encoder))
                self.quality.setValue(profile.quality)
                self.speed.setCurrentText(profile.speed)
                self.resolution.setCurrentIndex(self.resolution.findData(profile.resolution))
                self.fps.setCurrentIndex(self.fps.findData(profile.fps))
                self.audio.setCurrentIndex(self.audio.findData(profile.audio))
                self.target_reduction.setValue(profile.target_reduction_pct)
                self.target_passes.setCurrentIndex(self.target_passes.findData(profile.target_passes))
            finally:
                del blockers
                self._loading_profile = False
            is_qsv = profile.encoder.endswith("_qsv")
            self.quality_label.setText("畫質由目標容量決定" if is_qsv or profile.encoder == "libopenh264" else
                                       "CQ 品質值" if profile.encoder.endswith("_nvenc") else
                                       "QP 品質值" if profile.encoder == "libkvazaar" else "CRF 品質值")
        for control in controls:
            control.setEnabled(editable)
        if row is not None:
            self.target_passes.setEnabled(editable and profile.encoder not in {"libkvazaar", "libopenh264"})
            self.quality.setEnabled(editable and profile.encoder != "libopenh264" and not profile.encoder.endswith("_qsv"))
        if row is not None and row["profile_snapshot_json"]:
            self.mode.setEnabled(editable and not profile.encoder.endswith("_qsv"))
            self.quality.setEnabled(editable and not profile.encoder.endswith("_qsv"))

    def _profile_edited(self, *_args) -> None:
        if not self._loading_profile and self._loaded_profile_job_id:
            self._profile_save_timer.start(0)

    def _save_current_profile(self, identifier: str | None) -> None:
        if self._loading_profile or not identifier or identifier != self._loaded_profile_job_id:
            return
        row = jobs.get_job(self.connection, identifier)
        if row is None or row["status"] not in {"queued", "analyzed"}:
            return
        try:
            changed = row["profile_snapshot_json"] != json.dumps(self._profile().snapshot(), ensure_ascii=False)
            if changed and jobs.update_waiting_profile(self.connection, identifier, self._profile()):
                self._show_job()
                item = self.jobs_list.currentItem()
                if item and item.data(Qt.UserRole) == identifier:
                    item.setText(f"等待試壓縮\n{Path(row['source_rel_path']).name}")
        except ValueError:
            return

    def _preset_changed(self) -> None:
        profile = builtin(self.mode.currentIndex(), self.encoder.currentData() or "libx265")
        self.quality.setValue(profile.quality)
        self.speed.setCurrentText(profile.speed)

    def _encoder_changed(self) -> None:
        chosen = str(self.encoder.currentData())
        is_qsv = chosen.endswith("_qsv")
        self.quality_label.setText("畫質由目標容量決定" if is_qsv or chosen == "libopenh264" else
                                   "CQ 品質值" if chosen.endswith("_nvenc") else
                                   "QP 品質值" if chosen == "libkvazaar" else "CRF 品質值")
        self._preset_changed()
        self.mode.setEnabled(not is_qsv)
        self.quality.setEnabled(not is_qsv and chosen != "libopenh264")
        self.target_passes.setEnabled(chosen not in {"libkvazaar", "libopenh264"})
        if is_qsv or chosen in {"libkvazaar", "libopenh264"}:
            self.target_passes.setCurrentIndex(self.target_passes.findData(1))

    def _profile(self) -> Profile:
        encoder = self.encoder.currentData()
        passes = 1 if encoder in {"libkvazaar", "libopenh264"} else self.target_passes.currentData()
        return Profile(PRESETS[self.mode.currentIndex()][0], encoder,
                       self.quality.value(), self.speed.currentText(), self.resolution.currentData(),
                       self.fps.currentData(), self.audio.currentData(),
                       self.target_reduction.value(), passes).validate()

    def queue_selection(self) -> None:
        if self.root is None or self.library_id is None or self.capabilities is None:
            return
        self._save_current_profile(self._loaded_profile_job_id)
        ids = [item.data(Qt.UserRole) for item in self.videos.selectedItems()]
        if not ids:
            notify_information(self, "請選影片", "在上方清單選擇一部或多部 MP4。")
            return
        profile = self._profile()
        if profile.encoder not in self.capabilities.encoders or (
            profile.encoder.endswith("_nvenc") and not self.capabilities.nvenc_usable
        ) or (
            profile.encoder.endswith("_qsv") and not self.capabilities.qsv_usable
        ):
            QMessageBox.warning(self, "編碼器不可用", "請改選可用的編碼器。")
            return
        errors = []
        added = []
        for video_id in ids:
            try:
                existing = self.connection.execute(
                    """SELECT id FROM compression_jobs WHERE video_id=? AND library_id=?
                       AND status IN ('queued','analyzing','analyzed','encoding','validating','completed')
                       ORDER BY created_at DESC LIMIT 1""", (video_id, self.library_id),
                ).fetchone()
                added.append(existing["id"] if existing is not None else
                             jobs.queue_video(self.connection, self.root, self.library_id, video_id, profile))
            except (OSError, ValueError, sqlite3.Error) as exc:
                errors.append(str(exc))
        self.refresh_jobs(added[-1] if added else None)
        self.tabs.setCurrentIndex(2)
        if added:
            self.video_picker.accept()
        if errors:
            QMessageBox.warning(self, "部分影片未加入", "\n".join(errors[:5]))

    def _update_replace_all_button(self) -> None:
        done = self._batch_replace_total - len(self._batch_replace_queue) - bool(self._batch_replacing_id)
        self.confirm_all_replace_button.setText(
            f"批次替換 {done}/{self._batch_replace_total}" if self._batch_replace_total else "確認全部待替換…")
        self.confirm_all_replace_button.setEnabled(
            self.root is not None and self.capabilities is not None and not self._closing
            and self.replacement_worker is None and self.cut_worker is None
            and not self._batch_replace_total and bool(self._replacement_candidates()))

    def refresh_jobs(self, selected: str | None = None) -> None:
        self._update_replace_all_button()
        selected = selected or self._selected_id()
        history_selected = self._selected_history_id()
        jobs.prune_terminal_jobs(self.connection, self.library_id, self.root)
        rows = jobs.list_jobs(self.connection, self.library_id)
        self.jobs_list.blockSignals(True)
        self.jobs_list.clear()
        active_rows = [row for row in rows if row["status"] in
                       {"queued", "analyzing", "analyzed", "encoding", "validating",
                        "completed"} and not (
                            self.replacement_worker is not None
                            and self.replacement_worker.mode == "replace"
                            and self.replacement_worker.job_id == row["id"])]
        self.active_heading.setText(f"影片工作 · {len(active_rows)} 部")
        for row in active_rows:
            state = ("等待正式壓縮" if row["status"] == "analyzed" and row["encode_queued"]
                     else STATUS.get(row["status"], row["status"]))
            show_progress = row["status"] in {"analyzing", "encoding", "validating"}
            percent = min(99, self._progress_by_job.get(row["id"], row["progress"]))
            suffix = f" · {percent:.0f}%" if show_progress else ""
            thumb = self.paths.cache / row["video_id"] / "00.webp"
            icon = QIcon(str(thumb)) if thumb.is_file() else placeholder_icon()
            item = QListWidgetItem(icon, f"{state}{suffix}\n{row['display_title']}")
            item.setSizeHint(QSize(310, 83))
            item.setToolTip(f"{row['display_title']}\n{row['source_rel_path']}")
            item.setData(Qt.UserRole, row["id"])
            self.jobs_list.addItem(item)
            if row["id"] == selected:
                self.jobs_list.setCurrentItem(item)
        if self.jobs_list.currentItem() is None and self.jobs_list.count():
            self.jobs_list.setCurrentRow(0)
        self.jobs_list.blockSignals(False)
        history_rows = sorted((row for row in rows if row["status"] in
                               {"failed", "cancelled", "replaced"}),
                              key=lambda row: row["finished_at"] or row["created_at"], reverse=True)[:50]
        self.history.blockSignals(True)
        self.history.setRowCount(len(history_rows))
        for index, row in enumerate(history_rows):
            profile = json.loads(row["profile_snapshot_json"])
            retention = ("原片可還原（最近 3 部）" if row["replacement_state"] == "complete"
                         else "原片已還原" if row["replacement_state"] == "restored"
                         else "原片保留期限已過" if row["replacement_state"] == "original_expired"
                         else row["error_message"] or "")
            state = ("已還原原片" if row["replacement_state"] == "restored"
                     else STATUS.get(row["status"], row["status"]))
            for column, value in enumerate((row["created_at"][:16].replace("T", " "),
                                            row["display_title"], state,
                                            _encoder_name(profile["encoder"]), retention)):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(Qt.UserRole, row["id"])
                    thumb = self.paths.cache / row["video_id"] / "00.webp"
                    if thumb.is_file():
                        cell.setIcon(QIcon(str(thumb)))
                self.history.setItem(index, column, cell)
            if row["id"] == history_selected:
                self.history.selectRow(index)
        self.history.blockSignals(False)
        self._load_selected_profile()
        self._show_job()
        self._history_changed()

    def _selected_id(self) -> str | None:
        item = self.jobs_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _analysis_uses_video(self, video_id: str) -> bool:
        if self.analysis_worker is None:
            return False
        row = jobs.get_job(self.connection, self.analysis_worker.job_id)
        return bool(row and row["video_id"] == video_id)

    def _worker_uses_video(self, worker: CompressionThread | None, video_id: str) -> bool:
        row = jobs.get_job(self.connection, worker.job_id) if worker else None
        return bool(row and row["video_id"] == video_id)

    def _on_active_selected(self) -> None:
        self._load_selected_profile()
        if self.workspace.currentIndex() == 1 and self._selected_id():
            self.show_workflow()
            row = jobs.get_job(self.connection, self._selected_id())
            self.tabs.setCurrentIndex(2 if row and row["status"] == "completed" else 1)
        self._show_job()

    def _tab_changed(self, index: int) -> None:
        self._show_job()

    def _selected_history_id(self) -> str | None:
        row = self.history.currentRow()
        item = self.history.item(row, 0) if row >= 0 else None
        return item.data(Qt.UserRole) if item else None

    def _select_history(self, job_id: str) -> None:
        for row in range(self.history.rowCount()):
            item = self.history.item(row, 0)
            if item and item.data(Qt.UserRole) == job_id:
                self.history.selectRow(row)
                self.history.scrollToItem(item)
                break

    def _history_changed(self) -> None:
        identifier = self._selected_history_id()
        row = self.connection.execute(
            """SELECT j.status, j.source_rel_path, r.state, r.preserved_rel_path
               FROM compression_jobs j LEFT JOIN compression_replacements r ON r.job_id=j.id
               WHERE j.id=?""", (identifier,),
        ).fetchone() if identifier else None
        if row is None:
            self.history_detail.setText("選取紀錄可查看原片保留狀態。")
            self.restore_button.setEnabled(False)
            self.retry_history_button.setEnabled(False)
            if self.workspace.currentIndex() == 1:
                self.job_context.setText("歷史紀錄 · 請選取一筆工作")
            return
        if self.workspace.currentIndex() == 1:
            self.job_context.setText(f"歷史紀錄 · {Path(row['source_rel_path']).name}")
        self.retry_history_button.setEnabled(row["status"] in {"failed", "cancelled"})
        self.retry_history_button.setText(
            "接續上次壓縮" if row["status"] == "failed" and
            jobs.has_resume_checkpoint(self.root, identifier) else "重新加入處理清單")
        if row["state"] == "complete":
            preserved = self.root / row["preserved_rel_path"] if self.root else None
            available = bool(preserved and preserved.is_file())
            self.history_detail.setText(
                "保留原片可還原到影片庫原位置；目前的壓縮版會移回副本區。"
                if available else "保留原片目前無法讀取，請檢查影片庫。"
            )
            video = jobs.get_job(self.connection, identifier)
            self.restore_button.setEnabled(
                available and self.replacement_worker is None and self.cut_worker is None
                and bool(video) and not self._analysis_uses_video(video["video_id"])
                and not self._worker_uses_video(self.worker, video["video_id"])
            )
        elif row["state"] == "restored":
            self.history_detail.setText("原片已還原；壓縮版仍在副本區。")
            self.restore_button.setEnabled(False)
        elif row["state"] == "original_expired":
            self.history_detail.setText("只保留最近 3 部被替換的原片；這筆較舊的原片已清除。")
            self.restore_button.setEnabled(False)
        else:
            self.history_detail.setText("這筆工作沒有可還原的原片。")
            self.restore_button.setEnabled(False)

    def _show_job(self) -> None:
        identifier = self._selected_id()
        row = jobs.get_job(self.connection, identifier) if identifier else None
        if row is None:
            self.analysis_text.setText("尚未分析")
            self.result_text.setText("尚無壓縮結果")
            self.result_panel.hide()
            self.progress.setValue(0)
            self.progress.hide()
            self.progress_detail.setText("尚無執行中的工作")
            self.job_context.setText("請從左側選取要處理的影片")
            self.schedule_from_analysis.setEnabled(False)
            self.replace_button.setEnabled(False)
            self.next_step.setText("從「設定」選取影片並加入處理清單。")
            self.method_status.setText("尚未選取影片")
            self.pause_current_button.setEnabled(False)
            for label in ("試壓縮", "確認排程壓縮", "取消這部工作"):
                self.queue_actions[label].setEnabled(False)
            return
        editable = row["status"] in {"queued", "analyzed"}
        for control in (self.mode, self.encoder, self.quality, self.speed,
                        self.resolution, self.fps, self.audio,
                        self.target_reduction, self.target_passes):
            control.setEnabled(editable)
        is_qsv = str(self.encoder.currentData()).endswith("_qsv")
        self.mode.setEnabled(editable and not is_qsv)
        self.quality.setEnabled(editable and not is_qsv)
        selected_worker = next((worker for worker in (self.analysis_worker, self.worker,
                                                     self.replacement_worker)
                                if worker is not None and worker.job_id == identifier), None)
        state = ("正在替換原片" if selected_worker is not None and selected_worker is self.replacement_worker
                 else "準備試壓縮" if selected_worker is not None and selected_worker is self.analysis_worker and row["status"] == "queued"
                 else "準備正式壓縮" if selected_worker is not None and selected_worker is self.worker and row["status"] == "analyzed"
                 else "等待正式壓縮" if row["status"] == "analyzed" and row["encode_queued"]
                 else STATUS.get(row["status"], row["status"]))
        self.job_context.setText(f"{Path(row['source_rel_path']).name} · {state}")
        next_step = {
            "queued": "下一步：按「試壓縮」，先看容量與畫質估算。",
            "analyzing": "正在試壓縮；可選取其他影片準備設定。",
            "analyzed": ("已確認排程，會在目前壓縮結束後自動開始。" if row["encode_queued"]
                         else "下一步：查看「試壓縮分析」，確認後按「排隊壓縮」。"),
            "encoding": "正在壓縮；可對下一部影片做試壓縮並排程。",
            "validating": "正在驗證副本，完成後可播放比較。",
            "completed": "副本已完成。請在下方播放並比較壓縮結果，再確認是否替換原片。",
            "failed": f"工作失敗：{row['error_message'] or '請查看錯誤資訊'}",
            "cancelled": "這部影片的工作已取消；原片仍保留。",
        }.get(row["status"], "請查看目前工作狀態。")
        if selected_worker is not None and selected_worker is self.replacement_worker:
            next_step = "正在替換原片並更新縮圖；完成後可從歷史紀錄還原。"
        self.next_step.setText(next_step)
        profile = json.loads(row["profile_snapshot_json"])
        details = json.loads(row["sample_quality_details_json"] or "{}")
        if details.get("actual_encoder") and row["status"] in {"completed", "replaced"}:
            self.method_status.setText(
                f"本片實際採用：{_encoder_name(details['actual_encoder'])}"
            )
        elif row["status"] in {"analyzed", "encoding", "validating"}:
            _, planned_method = _planned_method(row, profile, self.capabilities)
            self.method_status.setText(f"本片預計方式：{planned_method}")
        else:
            self.method_status.setText(f"本片已選編碼器：{_encoder_name(profile['encoder'])}")
        active_progress = row["status"] in {"analyzing", "encoding", "validating"}
        self.progress.setVisible(active_progress)
        self.progress.setValue(min(99, round(self._progress_by_job.get(identifier, row["progress"]))))
        if selected_worker is not None and selected_worker is self.replacement_worker:
            self.progress_detail.setText("正在替換原片並更新縮圖")
        elif row["status"] == "validating":
            self.progress_detail.setText("轉檔已結束，正在驗證檔案與影格；完成後才會建立副本")
        elif selected_worker and selected_worker.is_paused():
            self.progress_detail.setText("已暫停；按「繼續這部影片」從原處繼續")
        elif identifier in self._job_started_at:
            elapsed = time.monotonic() - self._job_started_at[identifier]
            self.progress_detail.setText(f"已執行 {_duration_hm(elapsed)}")
        elif selected_worker:
            self.progress_detail.setText("正在準備檔案與 FFmpeg；尚未開始計算壓縮時間")
        elif row["status"] == "analyzed" and row["encode_queued"]:
            rank = self.connection.execute(
                """SELECT COUNT(*) FROM compression_jobs WHERE library_id=? AND status='analyzed'
                   AND encode_queued=1 AND queue_order>=?""",
                (self.library_id, row["queue_order"]),
            ).fetchone()[0]
            self.progress_detail.setText(f"正式壓縮排隊第 {rank} 位；尚未開始計算執行時間")
        elif row["status"] == "queued":
            if identifier in self._analysis_waiting:
                self.progress_detail.setText(
                    f"試壓縮排隊第 {self._analysis_waiting.index(identifier) + 1} 位；尚未開始計算執行時間"
                )
            else:
                self.progress_detail.setText("尚未加入試壓縮排隊；按「試壓縮」開始")
        else:
            elapsed = _recorded_elapsed(row)
            self.progress_detail.setText(f"{state} · 執行 {elapsed}" if elapsed else state)
        self.queue_actions["試壓縮"].setEnabled(
            row["status"] in {"queued", "analyzed"} and not row["encode_queued"]
            and not self._analysis_uses_video(row["video_id"]) and self.cut_worker is None
        )
        self.queue_actions["確認排程壓縮"].setEnabled(
            row["status"] == "analyzed" and not row["encode_queued"]
        )
        self.queue_actions["取消這部工作"].setEnabled(
            row["status"] in {"queued", "analyzing", "analyzed", "encoding", "validating"}
        )
        self.pause_current_button.setEnabled(selected_worker in (self.analysis_worker, self.worker)
                                             and selected_worker is not None)
        self.pause_current_button.setText("繼續這部影片" if selected_worker and selected_worker.is_paused()
                                          else "暫停這部影片")

        self.schedule_from_analysis.setEnabled(row["status"] == "analyzed" and
                                               not row["encode_queued"])
        self.replace_button.setEnabled(not self._batch_replace_total and row["status"] == "completed" and
                                       self.replacement_worker is None and self.cut_worker is None and
                                       not self._analysis_uses_video(row["video_id"]) and
                                       not self._worker_uses_video(self.worker, row["video_id"]))
        self.analysis_text.setHtml(_analysis_html(row, profile, self.analysis_details.isChecked(),
                                                  self.capabilities))
        self.result_panel.setVisible(row["status"] == "completed" and row["actual_size"] is not None)
        if row["actual_size"] is None:
            self.result_text.setText("尚無壓縮結果")
        else:
            self.result_text.setHtml(_result_html(row, profile))
        self.variants_list.setVisible(self.show_variants.isChecked())
        self.variants_list.clear()
        if self.show_variants.isChecked():
            for variant in self.connection.execute(
                "SELECT rel_path,size_bytes,active_variant FROM video_variants WHERE source_video_id=? ORDER BY created_at DESC",
                (row["video_id"],),
            ):
                self.variants_list.addItem(
                    f"{'目前使用 · ' if variant['active_variant'] else ''}{variant['rel_path']} · {_size(variant['size_bytes'])}"
                )

    def toggle_selected_pause(self) -> None:
        identifier = self._selected_id()
        worker = next((current for current in (self.analysis_worker, self.worker)
                       if current is not None and current.job_id == identifier), None)
        if worker is None:
            return
        worker.set_paused(not worker.is_paused())
        self._show_job()

    def _start_selected(self, mode: str) -> None:
        identifier = self._selected_id()
        if identifier and mode == "analyze":
            self._save_current_profile(identifier)
            if identifier not in self._analysis_waiting and not (
                self.analysis_worker and self.analysis_worker.job_id == identifier
            ):
                self._analysis_waiting.append(identifier)
            self._pump_analysis()
            self._show_job()

    def _start_job(self, identifier: str, mode: str) -> None:
        if (self.root is None or self.capabilities is None
                or (mode == "analyze" and self.analysis_worker is not None)
                or (mode != "analyze" and self.worker is not None)):
            return
        row = jobs.get_job(self.connection, identifier)
        if row is None or (mode == "analyze" and row["status"] not in {"queued", "analyzed"}) or (
            mode == "encode" and row["status"] != "analyzed"
        ):
            notify_information(self, "工作狀態", "此工作目前無法執行所選動作。")
            return
        other_worker = self.worker if mode == "analyze" else self.analysis_worker
        other_job = jobs.get_job(self.connection, other_worker.job_id) if other_worker else None
        if ((other_job is not None and other_job["video_id"] == row["video_id"])
                or self._worker_uses_video(self.replacement_worker, row["video_id"])):
            return
        thread = CompressionThread(self.paths, self.root, identifier, mode,
                                   self.capabilities, self.samples.currentData(), self)
        if mode == "analyze":
            self.analysis_worker = thread
        else:
            self.worker = thread
        thread.process_started.connect(lambda current=thread: self._mark_worker_started(current))
        thread.progress_changed.connect(lambda value, current=thread: self._progress_changed(current, value))
        thread.result.connect(lambda error, current=thread: self._worker_result(current, error))
        thread.finished.connect(lambda current=thread: self._worker_finished(current))
        thread.start()
        if mode != "analyze" or self.worker is None:
            self.job_progress.emit(0, f"{'試壓縮分析' if mode == 'analyze' else '壓縮'}："
                                      f"{Path(row['source_rel_path']).name}")
        if self._selected_id() == identifier:
            self.tabs.setCurrentIndex(2)
        self._show_job()

    def _mark_worker_started(self, thread: CompressionThread) -> None:
        if thread.job_id not in self._job_started_at:
            self._job_started_at[thread.job_id] = time.monotonic()
        if self._selected_id() == thread.job_id:
            self._show_job()

    def _progress_changed(self, thread: CompressionThread, value: float) -> None:
        now = time.monotonic()
        percent = min(99, max(0, round(value)))
        previous = self._progress_by_job.get(thread.job_id)
        if previous is None or value > previous + 0.01:
            self._last_progress_change[thread.job_id] = now
        self._progress_by_job[thread.job_id] = value
        elapsed = max(0.0, now - self._job_started_at.get(thread.job_id, now))
        phase = ("取樣完成，正在計算結果" if thread.mode == "analyze" and percent >= 99
                 else "正在試壓縮取樣" if thread.mode == "analyze"
                 else "轉檔結束，正在驗證副本" if percent >= 99
                 else "正在轉檔（容量不足時可能重試）")
        message = f"{phase} · {percent}% · 已執行 {_duration_hm(elapsed)}"
        if now - self._last_progress_change.get(thread.job_id, now) >= 20:
            message += " · FFmpeg 仍在執行，暫無新進度回報"
        if self._selected_id() == thread.job_id:
            self.progress.setValue(percent)
            self.progress_detail.setText(message)
        for index in range(self.jobs_list.count()):
            item = self.jobs_list.item(index)
            if item.data(Qt.UserRole) == thread.job_id:
                title = jobs.get_job(self.connection, thread.job_id)
                state = "試壓縮中" if thread.mode == "analyze" else "壓縮中"
                item.setText(f"{state} · {percent}%\n{Path(title['source_rel_path']).name}")
                break
        if thread is self.worker or self.worker is None:
            self.job_progress.emit(percent, message)

    def _worker_result(self, thread: CompressionThread, error: str) -> None:
        if error:
            if thread.job_id == self._batch_replacing_id:
                self._batch_replace_errors[thread.job_id] = error
            self.job_notice.emit(f"壓縮工作未完成：{error[:200]}")

    def _worker_finished(self, worker: CompressionThread) -> None:
        was_selected = self._selected_id() == worker.job_id
        self._job_started_at.pop(worker.job_id, None)
        self._progress_by_job.pop(worker.job_id, None)
        self._last_progress_change.pop(worker.job_id, None)
        if worker is self.analysis_worker:
            self.analysis_worker = None
        elif worker is self.worker:
            self.worker = None
        elif worker is self.replacement_worker:
            self.replacement_worker = None
        worker.deleteLater()
        self.refresh_jobs()
        self.library_changed.emit()
        row = jobs.get_job(self.connection, worker.job_id)
        if worker.mode in {"replace", "restore"}:
            self.replacement_active.emit(False)
            replacement = self.connection.execute(
                "SELECT state FROM compression_replacements WHERE job_id=?", (worker.job_id,)
            ).fetchone()
            if worker.job_id == self._batch_replacing_id:
                if row and replacement and replacement[0] == "complete":
                    self._batch_replace_success += 1
                else:
                    self._batch_replace_errors.setdefault(worker.job_id, "替換未完成，原片與副本仍保留。")
                self._batch_replacing_id = None
            if row and replacement and replacement[0] == ("restored" if worker.mode == "restore" else "complete"):
                if worker.mode == "restore":
                    self.show_history()
                    self._select_history(worker.job_id)
                if worker.mode == "replace":
                    self.job_notice.emit("原片已替換；保留原片可在歷史紀錄還原。")
                else:
                    self.job_notice.emit("已還原原片，壓縮副本仍保留。")
        elif row and was_selected:
            if row["status"] == "analyzed":
                self.tabs.setCurrentIndex(1)
            elif row["status"] == "completed":
                self.tabs.setCurrentIndex(2)
        if self._closing:
            if not self.worker and not self.analysis_worker and not self.replacement_worker and not self.cut_worker:
                self.job_progress.emit(-1, "")
                self.close()
            return
        else:
            self._pump_analysis()
            self._pump_encode()
            if self.worker is None and self.analysis_worker is not None:
                analysis = self.analysis_worker
                row = jobs.get_job(self.connection, analysis.job_id)
                if row:
                    self.job_progress.emit(
                        round(self._progress_by_job.get(analysis.job_id, 0)),
                        f"試壓縮分析：{Path(row['source_rel_path']).name}",
                    )
            elif self.worker is None and self.analysis_worker is None:
                self.job_progress.emit(-1, "")

    def _start_batch(self, mode: str) -> None:
        self._batch_accepted_bitrate_warning = False
        if mode == "encode":
            rows = self.connection.execute(
                "SELECT * FROM compression_jobs WHERE library_id=? AND status='analyzed'",
                (self.library_id,),
            ).fetchall()
            risky = sum(bool(_low_bitrate_warning(row)) for row in rows)
            if risky and QMessageBox.question(
                self, "批次位元率提醒",
                f"有 {risky} 部影片的原片或目標位元率偏低，建議先逐部檢查。"
                "\n\n仍要全部產生壓縮副本嗎？原片會保留。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return
            with self.connection:
                self.connection.execute(
                    "UPDATE compression_jobs SET encode_queued=1 WHERE library_id=? AND status='analyzed'",
                    (self.library_id,),
                )
            self.refresh_jobs()
            self._pump_encode()
            return
        rows = self.connection.execute(
            """SELECT id FROM compression_jobs WHERE library_id=? AND status='queued'
               ORDER BY queue_order DESC""", (self.library_id,),
        ).fetchall()
        for row in rows:
            if row["id"] not in self._analysis_waiting:
                self._analysis_waiting.append(row["id"])
        self._pump_analysis()

    def _pump_analysis(self) -> None:
        if (self.analysis_worker is not None or self._analysis_paused
                or self.capabilities is None or self.root is None):
            return
        for _ in range(len(self._analysis_waiting)):
            identifier = self._analysis_waiting.pop(0)
            row = jobs.get_job(self.connection, identifier)
            if row is not None and row["status"] in {"queued", "analyzed"} and not row["encode_queued"]:
                encoding_job = jobs.get_job(self.connection, self.worker.job_id) if self.worker else None
                if ((encoding_job is not None and encoding_job["video_id"] == row["video_id"])
                        or self._worker_uses_video(self.replacement_worker, row["video_id"])):
                    self._analysis_waiting.append(identifier)
                    continue
                self._start_job(identifier, "analyze")
                return

    def schedule_selected(self) -> None:
        identifier = self._selected_id()
        row = jobs.get_job(self.connection, identifier) if identifier else None
        if row is None or row["status"] != "analyzed" or row["encode_queued"]:
            return
        warning = _low_bitrate_warning(row)
        if warning and QMessageBox.question(
            self, "位元率偏低", f"{warning}。\n\n仍要排隊產生壓縮副本嗎？原片會保留。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        if jobs.schedule_encode(self.connection, identifier):
            self.refresh_jobs(identifier)
            self.tabs.setCurrentIndex(2)
            self._pump_encode()

    def _pump_encode(self) -> None:
        if self.worker is not None or self._encode_paused or self.capabilities is None or self.root is None:
            return
        analyzing_job = jobs.get_job(self.connection, self.analysis_worker.job_id) if self.analysis_worker else None
        row = jobs.next_scheduled_encode(self.connection, self.library_id,
                                         analyzing_job["video_id"] if analyzing_job else None)
        if row:
            self._start_job(row["id"], "encode")

    def _toggle_analysis_queue(self) -> None:
        self._analysis_paused = not self._analysis_paused
        self.pause_analysis_button.setText("繼續試壓縮排隊" if self._analysis_paused else "暫停試壓縮排隊")
        if not self._analysis_paused:
            self._pump_analysis()

    def _toggle_encode_queue(self) -> None:
        self._encode_paused = not self._encode_paused
        self.pause_encode_button.setText("繼續正式壓縮排隊" if self._encode_paused else "暫停正式壓縮排隊")
        if not self._encode_paused:
            self._pump_encode()

    def cancel_selected(self) -> None:
        identifier = self._selected_id()
        if not identifier:
            return
        if identifier in self._analysis_waiting:
            self._analysis_waiting.remove(identifier)
        if self.analysis_worker and self.analysis_worker.job_id == identifier:
            self.analysis_worker.requestInterruption()
        elif self.worker and self.worker.job_id == identifier:
            self.worker.requestInterruption()
        elif jobs.get_job(self.connection, identifier)["status"] in {"queued", "analyzed"}:
            try:
                jobs.cancel_waiting_job(self.connection, self.root, identifier)
                self.refresh_jobs()
            except OSError as exc:
                QMessageBox.warning(self, "清理試壓縮片段失敗", str(exc))

    def retry_selected(self) -> None:
        identifier = self._selected_id()
        if identifier:
            jobs.retry_job(self.connection, identifier)
            self.refresh_jobs()

    def retry_history_selected(self) -> None:
        identifier = self._selected_history_id()
        row = jobs.get_job(self.connection, identifier) if identifier else None
        if row is None or row["status"] not in {"failed", "cancelled"}:
            return
        try:
            resumed = jobs.resume_job(self.connection, self.root, identifier)
        except RuntimeError as exc:
            QMessageBox.warning(self, "暫時無法接續", str(exc))
            return
        if not resumed:
            jobs.retry_job(self.connection, identifier)
        self.refresh_jobs(identifier)
        self.show_workflow()
        self.tabs.setCurrentIndex(2)
        if resumed:
            self._pump_encode()

    def _move_job(self, direction: int) -> None:
        item = self.jobs_list.currentItem()
        if not item:
            return
        index = self.jobs_list.row(item)
        target = index + direction
        if target < 0 or target >= self.jobs_list.count():
            return
        first_id = item.data(Qt.UserRole)
        other_id = self.jobs_list.item(target).data(Qt.UserRole)
        first = jobs.get_job(self.connection, first_id)
        other = jobs.get_job(self.connection, other_id)
        if first["status"] not in {"queued", "analyzed"} or other["status"] not in {"queued", "analyzed"}:
            return
        with self.connection:
            self.connection.execute("UPDATE compression_jobs SET queue_order=? WHERE id=?",
                                    (other["queue_order"], first_id))
            self.connection.execute("UPDATE compression_jobs SET queue_order=? WHERE id=?",
                                    (first["queue_order"], other_id))
        self.refresh_jobs()

    def _open_path(self, which: str) -> None:
        identifier = self._selected_id()
        row = jobs.get_job(self.connection, identifier) if identifier else None
        if row is None or self.root is None:
            return
        relative = None
        if which == "source":
            relative = row["source_rel_path"]
        elif which == "output":
            relative = row["source_rel_path"] if row["status"] == "replaced" else row["output_rel_path"]
        elif which == "sample":
            samples = self.connection.execute(
                "SELECT sample_stats_json FROM compression_samples WHERE job_id=? ORDER BY start_seconds LIMIT 1",
                (identifier,),
            ).fetchone()
            if samples:
                relative = json.loads(samples[0])["sample_rel_path"]
        if relative and (self.root / relative).is_file():
            self.play_path_requested.emit(row["video_id"], str(self.root / relative))
        elif which == "sample":
            notify_information(self, "取樣片段已清除", "試壓縮取樣檔已不在磁碟上；原片及正式壓縮副本不受影響。")
        else:
            notify_information(self, "檔案不可用", "原片或壓縮副本已不在預期位置；請檢查影片庫磁碟。")

    def _replacement_candidates(self) -> list[str]:
        rows = self.connection.execute(
            """SELECT j.id,j.video_id FROM compression_jobs j JOIN videos v ON v.id=j.video_id
               WHERE j.library_id=? AND j.status='completed' AND v.deleted_at IS NULL
               ORDER BY COALESCE(j.finished_at,j.created_at) DESC,j.created_at DESC,j.id DESC""",
            (self.library_id,),
        ).fetchall()
        seen = set()
        result = []
        for row in rows:
            if row["video_id"] not in seen:
                result.append(row["id"])
                seen.add(row["video_id"])
        return result

    def confirm_all_replace(self) -> None:
        if (self._closing or self._batch_replace_total or self.replacement_worker or self.cut_worker
                or self.root is None or self.capabilities is None):
            return
        identifiers = self._replacement_candidates()
        if not identifiers:
            return
        message = (f"一次確認 {len(identifiers)} 部待替換影片，每部採用最新完成的副本。\n\n"
                   "請先確認已播放比較。確認後會在背景逐部驗證與替換，不合格的項目會略過並通知。\n\n"
                   "依目前原片保留規則，僅保留最近 3 部原片供還原；之後成功替換時會清除最舊的保留原片。")
        if QMessageBox.question(self, "確認全部替換", message,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._batch_replace_queue = identifiers
        self._batch_replace_total = len(identifiers)
        self._batch_replace_success = 0
        self._batch_replace_errors = {}
        self.confirm_all_replace_button.setEnabled(False)
        self._batch_replace_timer.start()
        self._pump_batch_replacements()

    def _pump_batch_replacements(self) -> None:
        if self._closing or not self._batch_replace_total:
            self._batch_replace_timer.stop()
            return
        if self.replacement_worker or self.cut_worker:
            return
        for _ in range(len(self._batch_replace_queue)):
            identifier = self._batch_replace_queue.pop(0)
            row = jobs.get_job(self.connection, identifier)
            if row and (self._analysis_uses_video(row["video_id"])
                        or self._worker_uses_video(self.worker, row["video_id"])):
                self._batch_replace_queue.append(identifier)
                continue
            reason = ""
            if row is None or row["status"] != "completed":
                reason = "工作狀態已改變"
            else:
                try:
                    details = json.loads(row["sample_quality_details_json"] or "{}")
                    if details.get("hdr_or_rotation"):
                        reason = "含特殊色彩或旋轉資訊，無法安全替換"
                    elif not (self.root / row["source_rel_path"]).is_file() or not (
                            row["output_rel_path"] and (self.root / row["output_rel_path"]).is_file()):
                        reason = "原片或壓縮副本找不到"
                except (ValueError, OSError) as exc:
                    reason = str(exc)
            if reason:
                self._batch_replace_errors[identifier] = reason
                name = Path(row["source_rel_path"]).name if row else identifier
                self.job_notice.emit(f"批次替換略過 {name}：{reason}")
                continue
            self._batch_replacing_id = identifier
            self._start_replace(identifier)
            if self.replacement_worker is not None:
                return
            self._batch_replacing_id = None
            self._batch_replace_errors[identifier] = "無法啟動替換工作"
        if not self._batch_replace_queue:
            total, succeeded = self._batch_replace_total, self._batch_replace_success
            self._batch_replace_total = 0
            self._batch_replace_timer.stop()
            message = f"批次替換完成：成功 {succeeded}/{total} 部；未完成 {total - succeeded} 部。"
            for identifier, reason in list(self._batch_replace_errors.items())[:10]:
                row = jobs.get_job(self.connection, identifier)
                name = Path(row["source_rel_path"]).name if row else identifier
                message += f"\n{name}：{reason}"
            self.job_notice.emit(message)
            self.refresh_jobs()

    def confirm_replace(self) -> None:
        identifier = self._selected_id()
        row = jobs.get_job(self.connection, identifier) if identifier else None
        if row is None:
            return
        if row["status"] == "replaced":
            return
        if (self._batch_replace_total or row["status"] != "completed" or self.root is None or self.replacement_worker or self.cut_worker
                or self._analysis_uses_video(row["video_id"])
                or self._worker_uses_video(self.worker, row["video_id"])):
            return
        details = json.loads(row["sample_quality_details_json"] or "{}")
        if details.get("hdr_or_rotation"):
            QMessageBox.warning(self, "無法安全替換", "來源含特殊色彩或旋轉資訊；請保留壓縮副本供比較。")
            return
        if not (self.root / row["source_rel_path"]).is_file() or not (
                row["output_rel_path"] and (self.root / row["output_rel_path"]).is_file()):
            notify_information(self, "檔案不可用", "原片或壓縮副本已不在預期位置，無法替換。")
            return
        message = (f"原片：{self.root / row['source_rel_path']}（{_size(row['source_size'])}）\n"
                   f"副本：{self.root / row['output_rel_path']}（{_size(row['actual_size'])}）\n"
                   f"實際節省：{row['actual_reduction_pct']:.1f}%\n\n"
                   "確認後，原片會移至同磁碟的 .videomanager/compression-originals 保留區，"
                   "壓縮版才會放到原路徑。最多保留最近 3 部原片，可從歷史紀錄還原；"
                   "第 4 部替換成功時會清除最舊的保留原片。請先自行播放比較。")
        if details.get("extra_streams"):
            message += "\n\n原片含附加資料流；替換前會嘗試保留並驗證，失敗時兩份影片均不會被替換。"
        if QMessageBox.question(self, "確認替換原片", message,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._start_replace(identifier)

    def _start_replace(self, identifier: str) -> None:
        row = jobs.get_job(self.connection, identifier)
        if (row is None or row["status"] != "completed" or self.root is None or self.capabilities is None
                or self.replacement_worker or self.cut_worker
                or self._analysis_uses_video(row["video_id"])
                or self._worker_uses_video(self.worker, row["video_id"])):
            return
        self.replace_button.setEnabled(False)
        self.replacement_worker = CompressionThread(self.paths, self.root, identifier, "replace",
                                                    self.capabilities, 4, self)
        self.replacement_active.emit(True)
        self.replacement_worker.result.connect(
            lambda error, current=self.replacement_worker: self._worker_result(current, error))
        self.replacement_worker.finished.connect(
            lambda current=self.replacement_worker: self._worker_finished(current))
        self.replacement_worker.start()
        self.workspace.setCurrentIndex(0)
        self.refresh_jobs()
        self.job_progress.emit(0, "正在替換原片並更新縮圖")

    def confirm_restore(self) -> None:
        identifier = self._selected_history_id()
        if (not identifier or self.root is None or self.capabilities is None
                or self.replacement_worker or self.cut_worker):
            return
        row = self.connection.execute(
            """SELECT j.source_rel_path, r.preserved_rel_path FROM compression_jobs j
               JOIN compression_replacements r ON r.job_id=j.id
               WHERE j.id=? AND r.state='complete'""", (identifier,),
        ).fetchone()
        if row is None:
            return
        selected_job = jobs.get_job(self.connection, identifier)
        if selected_job and (self._analysis_uses_video(selected_job["video_id"])
                             or self._worker_uses_video(self.worker, selected_job["video_id"])):
            return
        message = (f"將保留原片放回：{self.root / row['source_rel_path']}\n"
                   "目前使用的壓縮版會移回副本區，不會刪除。確定還原？")
        if QMessageBox.question(self, "確認還原原片", message,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.replacement_worker = CompressionThread(self.paths, self.root, identifier, "restore",
                                                    self.capabilities, 4, self)
        self.replacement_active.emit(True)
        self.restore_button.setEnabled(False)
        self.replacement_worker.result.connect(
            lambda error, current=self.replacement_worker: self._worker_result(current, error))
        self.replacement_worker.finished.connect(
            lambda current=self.replacement_worker: self._worker_finished(current))
        self.replacement_worker.start()
        self.job_progress.emit(0, "正在還原原片並更新縮圖")

    def refresh_edits(self, selected: str | None = None) -> None:
        if not hasattr(self, "edits_list"):
            return
        selected = selected or (self.edits_list.currentItem().data(Qt.UserRole)
                                if self.edits_list.currentItem() else None)
        self.edits_list.clear()
        for row in edits.list_edits(self.connection, self.library_id):
            status = {"queued": "待執行", "running": "剪輯中", "validating": "驗證中",
                      "completed": "副本完成", "replaced": "已取代原片", "failed": "失敗",
                      "cancelled": "已取消"}.get(row["status"], row["status"])
            text = (f"{status} · {row['display_title']} · 排除 "
                    f"{row['cut_start']:.1f}–{row['cut_end']:.1f} 秒")
            if row["error_message"]:
                text += f" · {row['error_message']}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, row["id"])
            self.edits_list.addItem(item)
            if row["id"] == selected:
                self.edits_list.setCurrentItem(item)
        if self.edits_list.currentItem() is None and self.edits_list.count():
            self.edits_list.setCurrentRow(0)

    def _selected_edit(self):
        item = self.edits_list.currentItem()
        return edits.get_edit(self.connection, item.data(Qt.UserRole)) if item else None

    def focus_cut(self, video_id: str | None = None) -> None:
        if video_id:
            index = self.cut_videos.findData(video_id)
            if index >= 0:
                self.cut_videos.setCurrentIndex(index)
        self.tabs.setCurrentIndex(0)

    def create_cut(self) -> None:
        if (self.root is None or self.library_id is None or self.capabilities is None
                or self.worker is not None or self.cut_worker is not None
                or self.replacement_worker is not None):
            return
        try:
            video_id = self.cut_videos.currentData()
            if not video_id:
                raise ValueError("請先選擇來源影片")
            start = edits.parse_time(self.cut_start.text())
            end = edits.parse_time(self.cut_end.text())
            edit_id = edits.create_edit(self.connection, self.root, self.library_id,
                                        video_id, start, end)
        except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
            QMessageBox.warning(self, "無法開始剪輯", str(exc))
            return
        self.refresh_edits(edit_id)
        self._start_cut(edit_id, "cut")

    def _start_cut(self, edit_id: str, mode: str) -> None:
        if (self.root is None or self.capabilities is None or self.cut_worker or self.worker
                or self.replacement_worker):
            return
        self.cut_worker = CutThread(self.paths, self.root, edit_id, mode, self.capabilities, self)
        self._job_started_at = time.monotonic()
        self.cut_worker.progress_changed.connect(self._cut_progress_changed)
        self.cut_worker.result.connect(self._cut_result)
        self.cut_worker.finished.connect(self._cut_finished)
        if mode == "replace":
            self.replacement_active.emit(True)
        self.cut_worker.start()
        self.job_progress.emit(0, f"{'剪輯' if mode == 'cut' else '取代剪輯副本'}：{self.cut_videos.currentText()}")
        self.tabs.setCurrentIndex(0)

    def _cut_progress_changed(self, value: float) -> None:
        percent = round(value)
        self.cut_progress.setValue(percent)
        elapsed = max(0.0, time.monotonic() - self._job_started_at)
        eta = round(elapsed * (100 - value) / value) if 1 < value < 100 else None
        message = f"剪輯 {percent}% · 已執行 {round(elapsed)} 秒"
        if eta is not None:
            message += f" · 估計剩餘 {eta} 秒"
        self.cut_progress_detail.setText(message)
        self.job_progress.emit(percent, message)

    def _cut_result(self, error: str) -> None:
        if error:
            if self.isVisible():
                QMessageBox.warning(self, "剪輯工作未完成", error[:1200])
            else:
                self.job_notice.emit(f"剪輯工作未完成：{error[:200]}")

    def _cut_finished(self) -> None:
        worker = self.cut_worker
        self.cut_worker = None
        if worker:
            self.refresh_edits(worker.edit_id)
            worker.deleteLater()
        self.cut_progress_detail.setText("目前沒有執行中的剪輯")
        self.job_progress.emit(-1, "")
        self.library_changed.emit()
        if worker and worker.mode == "replace":
            self.replacement_active.emit(False)
        if self._closing:
            self.close()

    def cancel_cut(self) -> None:
        if self.cut_worker and self.cut_worker.mode == "cut":
            self.cut_worker.requestInterruption()

    def _open_edit_path(self, which: str) -> None:
        row = self._selected_edit()
        if row is None or self.root is None:
            return
        relative = row["source_rel_path"] if which == "source" or row["status"] == "replaced" else row["output_rel_path"]
        if relative and (self.root / relative).is_file():
            self.play_path_requested.emit(row["video_id"], str(self.root / relative))
        else:
            notify_information(self, "檔案不可用", "來源或剪輯副本已不在影片庫中。")

    def confirm_cut_replace(self) -> None:
        row = self._selected_edit()
        if (row is None or row["status"] != "completed" or self.root is None
                or self.worker or self.cut_worker or self.replacement_worker):
            notify_information(self, "尚無可取代副本", "請先完成剪輯並播放檢查副本。")
            return
        message = (f"原片：{self.root / row['source_rel_path']}（{_size(row['source_size'])}）\n"
                   f"剪輯版：{self.root / row['output_rel_path']}（{_size(row['output_size'])}）\n"
                   f"片長：{row['original_duration']:.1f} 秒 → {row['output_duration']:.1f} 秒\n\n"
                   "確定要取代原片？原片會移至同磁碟的 .videomanager/edit-originals 保留區。"
                   "請先播放剪輯副本檢查；取代後時間書籤可能需要調整。")
        if QMessageBox.question(self, "確認取代原片", message,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self._start_cut(row["id"], "replace")

    def closeEvent(self, event) -> None:
        self._profile_save_timer.stop()
        self._save_current_profile(self._loaded_profile_job_id)
        if self.has_active_work() and not self._closing:
            event.ignore()
            self.hide()
            return
        if self.cut_worker and self.cut_worker.isRunning():
            event.ignore()
            return
        if self.replacement_worker and self.replacement_worker.isRunning():
            event.ignore()
            return
        if self.worker and self.worker.isRunning():
            event.ignore()
            return
        if self.analysis_worker and self.analysis_worker.isRunning():
            event.ignore()
            return
        if self.cap_thread.isRunning():
            self._closing = True
            event.ignore()
            self.hide()
            return
        super().closeEvent(event)

    def reject(self) -> None:
        # QDialog handles Esc by calling reject(). An embedded page must navigate
        # back instead of closing itself inside the main window's stacked widget.
        if self.embedded:
            self.back_requested.emit()
        else:
            super().reject()

    def has_active_work(self) -> bool:
        return (self.worker is not None or self.analysis_worker is not None
                or self.replacement_worker is not None or self.cut_worker is not None
                or bool(self._analysis_waiting) or bool(self._batch_replace_total))

    def stop_for_app_exit(self) -> None:
        self._closing = True
        self._batch_replace_queue.clear()
        self._batch_replace_total = 0
        self._batch_replacing_id = None
        self._batch_replace_timer.stop()
        self._analysis_waiting.clear()
        if self.worker is not None:
            self.worker.interrupt_for_exit()
        if self.replacement_worker is not None:
            self.replacement_worker.requestInterruption()
        if self.analysis_worker is not None:
            self.analysis_worker.requestInterruption()
        if self.cut_worker is not None and self.cut_worker.mode == "cut":
            self.cut_worker.requestInterruption()
        self.close()
