from __future__ import annotations

import os
import sys
import math
import json
import base64
import hashlib
import re
from datetime import datetime
import threading
import subprocess
import traceback
import tempfile
import time
import shutil
import copy
import webbrowser
from queue import Queue, Empty
from pathlib import Path
from functools import lru_cache
from typing import Optional, Callable

import cv2
import numpy as np
# Keep OpenCV from spawning a large worker pool while FFmpeg is decoding AV1.
# This favors a responsive desktop on laptops with 16 GB RAM.
try:
    cv2.setNumThreads(2)
    cv2.setUseOptimized(True)
except Exception:
    pass
from PIL import Image, ImageDraw, ImageFont, ImageTk
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser, simpledialog

from cta_detector import CTAScanCancelled, detect_cta_image_candidates, get_subscribe_ocr_status
from project_storage import (
    ensure_project_workspace, backup_project_file, sync_project_assets,
    default_output_directory,
    default_output_directory_for_project,
)

try:
    import imageio_ffmpeg
except Exception:
    imageio_ffmpeg = None

APP_TITLE = "Góc Review Phim – Video Studio"
APP_VERSION = "1.5.8.0"
PREVIEW_MIN = 10
PREVIEW_MAX = 60
MASK_DEFAULT_EXPANSION = 16


class ExportCancelled(Exception):
    """Raised when the user intentionally cancels an export."""


def resource_path(relative: str) -> Path:
    """Resolve assets for both source and PyInstaller-style builds."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def app_settings_path() -> Path:
    """Store per-user FFmpeg choice beside the source app (or executable)."""
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return base / "user_settings.json"


def load_app_settings() -> dict:
    try:
        import json
        path = app_settings_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def save_app_settings(data: dict) -> None:
    import json
    path = app_settings_path()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def configured_ffmpeg_path() -> str:
    path = str(load_app_settings().get("ffmpeg_path", "")).strip()
    return path if path and Path(path).is_file() else ""


def _hidden_subprocess_options() -> dict:
    """Prevent child FFmpeg/ffprobe processes from opening console windows on Windows."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    return {
        "creationflags": flags,
        "startupinfo": startupinfo,
    }


@lru_cache(maxsize=4)
def ffmpeg_has_encoder(ffmpeg_path: str, encoder_name: str) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=False, timeout=15,
            **_hidden_subprocess_options()
        )
        return result.returncode == 0 and encoder_name in ((result.stdout or "") + (result.stderr or ""))
    except Exception:
        return False


@lru_cache(maxsize=8)
def ffmpeg_has_decoder(ffmpeg_path: str, decoder_name: str) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-decoders"],
            capture_output=True, text=True, check=False, timeout=15,
            **_hidden_subprocess_options()
        )
        return result.returncode == 0 and decoder_name in ((result.stdout or "") + (result.stderr or ""))
    except Exception:
        return False


@lru_cache(maxsize=2)
def ffmpeg_has_hwaccel(ffmpeg_path: str, accel_name: str) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-hwaccels"],
            capture_output=True, text=True, check=False, timeout=12,
            **_hidden_subprocess_options()
        )
        lines = {line.strip().lower() for line in ((result.stdout or "") + "\n" + (result.stderr or "")).splitlines()}
        return result.returncode == 0 and accel_name.lower() in lines
    except Exception:
        return False


@lru_cache(maxsize=2)
def probe_nvenc_result(ffmpeg_path: str) -> tuple[bool, str]:
    """Test a real 720p NVENC encode, matching the command known to work on user GPUs.

    Very small 128x128 synthetic frames can fail on some NVENC driver/build
    combinations even though normal 720p/1080p encodes work. Use a 1280x720
    testsrc2 input so the probe measures the actual encoder/device path.
    """
    if not ffmpeg_has_encoder(ffmpeg_path, "h264_nvenc"):
        return False, "FFmpeg build không có h264_nvenc."
    try:
        test = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
             "-frames:v", "30", "-an", "-c:v", "h264_nvenc", "-preset", "p5",
             "-f", "null", "-"],
            capture_output=True, text=True, check=False, timeout=30,
            **_hidden_subprocess_options()
        )
        detail = (test.stderr or test.stdout or "").strip()
        if test.returncode == 0:
            return True, "NVENC đã mã hóa thử 30 frame 720p thành công."
        return False, detail[-1600:] or f"FFmpeg trả mã lỗi {test.returncode}."
    except Exception as exc:
        return False, str(exc)


@lru_cache(maxsize=2)
def probe_nvenc(ffmpeg_path: str) -> bool:
    return probe_nvenc_result(ffmpeg_path)[0]


def choose_video_encoder(ffmpeg_path: str, prefer_gpu: bool = True) -> tuple[str, str]:
    """Return an actually usable encoder; NVENC is probed once then safely falls back."""
    if prefer_gpu and probe_nvenc(ffmpeg_path):
        return "h264_nvenc", "NVIDIA NVENC"
    if ffmpeg_has_encoder(ffmpeg_path, "libx264"):
        return "libx264", "CPU x264"
    raise RuntimeError("FFmpeg hiện tại không có encoder H.264 phù hợp (h264_nvenc/libx264).")


@lru_cache(maxsize=16)
def _ffmpeg_has_option(ffmpeg_path: str, option: str) -> bool:
    """Check CLI option support so incompatible system FFmpeg builds are skipped."""
    try:
        probe = subprocess.run([ffmpeg_path, "-hide_banner", "-h", "full"],
                               capture_output=True, text=True, check=False, timeout=12,
                               **_hidden_subprocess_options())
        help_text = (probe.stdout or "") + "\n" + (probe.stderr or "")
        return option in help_text
    except Exception:
        return False


@lru_cache(maxsize=1)
def get_ffmpeg() -> str:
    """Choose the best available FFmpeg, preferring a working NVIDIA NVENC build.

    Older FFmpeg builds without -fps_mode remain usable: the rawvideo decoder
    path simply omits that output option because frames are transported in order.
    """
    from shutil import which
    candidates = []
    # An explicitly selected FFmpeg takes precedence. This lets Windows users
    # point the app at a full build with NVENC/libdav1d even when PATH points
    # to an older x264-only binary.
    configured = configured_ffmpeg_path()
    if configured:
        return configured
    system_ffmpeg = which("ffmpeg")
    if system_ffmpeg and system_ffmpeg not in candidates:
        candidates.append(system_ffmpeg)
    if imageio_ffmpeg is not None:
        try:
            bundled = imageio_ffmpeg.get_ffmpeg_exe()
            if bundled and Path(bundled).exists() and bundled not in candidates:
                candidates.append(bundled)
        except Exception:
            pass

    usable = []
    for candidate in candidates:
        has_x264 = ffmpeg_has_encoder(candidate, "libx264")
        has_nvenc = probe_nvenc(candidate)
        if has_x264 or has_nvenc:
            usable.append((candidate, has_nvenc, has_x264))

    # Do not accidentally select an x264-only FFmpeg when another candidate
    # in PATH or the Python environment can actually initialize NVENC.
    for candidate, has_nvenc, _has_x264 in usable:
        if has_nvenc:
            return candidate
    for candidate, _has_nvenc, has_x264 in usable:
        if has_x264:
            return candidate
    if candidates:
        return candidates[0]
    raise RuntimeError("Không tìm thấy FFmpeg tương thích. Hãy chạy CAI_DAT_VA_CHAY.bat để cài đủ thư viện.")


def safe_float(value: str, field_name: str = "Giá trị") -> float:
    """Parse a non-negative finite number from a GUI text field.

    Tkinter StringVar values are user-controlled text; validate them before
    passing timestamps to FFmpeg so malformed input shows a friendly error.
    """
    raw = str(value).strip().replace(",", ".")
    if not raw:
        raise ValueError(f"{field_name} không được để trống.")
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} phải là một số hợp lệ (ví dụ: 12 hoặc 12.5).") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} phải là số hữu hạn.")
    if number < 0:
        raise ValueError(f"{field_name} không được nhỏ hơn 0.")
    return number


def _parse_rate(rate: str) -> float:
    try:
        if "/" in rate:
            a, b = rate.split("/", 1)
            return float(a) / float(b)
        return float(rate)
    except Exception:
        return 0.0


def get_ffprobe() -> Optional[str]:
    """Prefer ffprobe that ships beside the selected FFmpeg executable."""
    from shutil import which
    try:
        ffmpeg_path = Path(get_ffmpeg())
        probe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
        sibling = ffmpeg_path.with_name(probe_name)
        if sibling.is_file():
            return str(sibling)
    except Exception:
        pass
    return which("ffprobe")


def read_video_info(path: str) -> dict:
    ffprobe = get_ffprobe()
    if ffprobe:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate,duration,nb_frames,codec_name",
             "-show_entries", "format=duration", "-of", "json", path],
            capture_output=True, text=True, check=False, timeout=30,
            **_hidden_subprocess_options())
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout or "{}")
            streams = data.get("streams", [])
            if streams:
                st = streams[0]
                width, height = int(st.get("width", 0)), int(st.get("height", 0))
                fps = _parse_rate(st.get("avg_frame_rate") or st.get("r_frame_rate") or "0")
                duration = float(st.get("duration") or data.get("format", {}).get("duration") or 0)
                frames = int(float(st.get("nb_frames") or 0))
                derived_frames = int(round(duration * fps)) if duration > 0 and fps > 0 else 0
                # Some containers report a stale/estimated nb_frames value.
                # Prefer duration × average frame rate when the two disagree
                # materially; this keeps the export ETA from showing hours
                # for a ~20 minute source.
                if derived_frames > 0 and (frames <= 0 or abs(frames - derived_frames) / derived_frames > 0.03):
                    frames = derived_frames
                if width > 0 and height > 0 and fps > 0:
                    return {"width": width, "height": height, "fps": fps,
                            "frames": frames, "duration": duration,
                            "codec": st.get("codec_name", "unknown")}
    if imageio_ffmpeg is None:
        raise RuntimeError("Không đọc được thông số video. Hãy kiểm tra FFmpeg và file nguồn.")
    reader = imageio_ffmpeg.read_frames(path, pix_fmt="rgb24")
    try:
        meta = reader.send(None)
    finally:
        reader.close()
    width, height = meta.get("source_size") or meta.get("size")
    fps = float(meta.get("fps") or 0)
    duration = float(meta.get("duration") or 0)
    if width <= 0 or height <= 0 or fps <= 0:
        raise RuntimeError("Không đọc được thông số video từ FFmpeg.")
    frames = int(round(duration * fps)) if duration > 0 else 0
    return {"width": int(width), "height": int(height), "fps": fps,
            "frames": frames, "duration": duration, "codec": meta.get("codec", "unknown")}


def probe_media_details(path: str) -> dict:
    """Return reliable container and stream metadata for quality control."""
    ffprobe = get_ffprobe()
    if not ffprobe:
        raise RuntimeError("Không tìm thấy ffprobe. Hãy chọn bộ FFmpeg Essentials có ffprobe.exe đi kèm.")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=duration,size,format_name,bit_rate:stream=index,codec_type,codec_name,width,height,pix_fmt,avg_frame_rate,r_frame_rate,duration,nb_frames,sample_rate,channels,channel_layout,bit_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=False, timeout=45,
        **_hidden_subprocess_options()
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-1200:]
        raise RuntimeError(detail or "FFprobe không đọc được metadata của video.")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Metadata FFprobe trả về không hợp lệ.") from exc
    streams = data.get("streams") or []
    video = next((x for x in streams if x.get("codec_type") == "video"), None)
    audio = next((x for x in streams if x.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}
    duration_values = []
    try:
        if fmt.get("duration") is not None:
            duration_values.append(float(fmt["duration"]))
    except (TypeError, ValueError):
        pass
    for stream in streams:
        try:
            if stream.get("duration") is not None:
                duration_values.append(float(stream["duration"]))
        except (TypeError, ValueError):
            pass
    duration = max(duration_values) if duration_values else 0.0
    return {"format": fmt, "streams": streams, "video": video, "audio": audio,
            "duration": duration, "format_name": str(fmt.get("format_name") or "unknown")}


def inspect_output_quality(source_path: Optional[str], output_path: str,
                           expected_start: float = 0.0,
                           expected_duration: Optional[float] = None,
                           deep_scan: bool = False) -> dict:
    """Validate an exported file without changing it; deep_scan decodes the full file."""
    report = {
        "source_path": str(source_path) if source_path else "",
        "output_path": str(output_path),
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "deep_scanned": bool(deep_scan),
        "checks": [], "status": "FAIL", "summary": "",
        "metadata": {},
    }
    def add(status: str, name: str, detail: str):
        report["checks"].append({"status": status, "name": name, "detail": str(detail)})

    path = Path(output_path)
    if not path.is_file():
        add("FAIL", "File đầu ra", "Không tìm thấy file đầu ra.")
        report["status"] = "FAIL"
        report["summary"] = "Không tìm thấy file đầu ra."
        return report
    size = path.stat().st_size
    if size < 1024:
        add("FAIL", "Dung lượng file", f"File chỉ có {size:,} byte; có thể là file rỗng hoặc xuất dở.")
    else:
        add("PASS", "Dung lượng file", f"{size / (1024 * 1024):.2f} MB; file không rỗng.")

    try:
        output = probe_media_details(str(path))
    except Exception as exc:
        add("FAIL", "Đọc cấu trúc media", f"FFprobe không đọc được: {exc}")
        report["status"] = "FAIL"
        report["summary"] = "File không có metadata media hợp lệ."
        return report

    video = output.get("video")
    audio = output.get("audio")
    report["metadata"] = {
        "duration": output.get("duration", 0.0),
        "format": output.get("format_name", "unknown"),
        "size_bytes": size,
        "video_codec": (video or {}).get("codec_name", ""),
        "width": int((video or {}).get("width") or 0),
        "height": int((video or {}).get("height") or 0),
        "fps": _parse_rate((video or {}).get("avg_frame_rate") or (video or {}).get("r_frame_rate") or "0/0"),
        "audio_codec": (audio or {}).get("codec_name", "") if audio else "",
        "audio_channels": int((audio or {}).get("channels") or 0) if audio else 0,
    }
    out_duration = float(output.get("duration") or 0.0)
    width = report["metadata"]["width"]
    height = report["metadata"]["height"]
    fps = report["metadata"]["fps"]

    if not video:
        add("FAIL", "Luồng hình ảnh", "Không tìm thấy video stream trong file đầu ra.")
    else:
        add("PASS", "Luồng hình ảnh", f"Có video stream ({report['metadata']['video_codec'] or 'codec không rõ'}).")
    if width > 0 and height > 0:
        add("PASS", "Độ phân giải", f"{width} × {height} px.")
    else:
        add("FAIL", "Độ phân giải", "Kích thước khung hình không hợp lệ.")
    if fps > 0:
        add("PASS", "Tốc độ khung hình", f"{fps:.3f} FPS.")
    else:
        add("FAIL", "Tốc độ khung hình", "Không xác định được FPS hợp lệ.")
    if out_duration > 0:
        add("PASS", "Thời lượng đọc được", f"{out_duration:.2f} giây ({out_duration / 60:.2f} phút).")
    else:
        add("FAIL", "Thời lượng đọc được", "Container không báo thời lượng hợp lệ.")

    source_details = None
    if source_path and Path(source_path).is_file():
        try:
            source_details = probe_media_details(str(source_path))
        except Exception as exc:
            add("WARN", "Đối chiếu video nguồn", f"Không đọc được nguồn để so sánh: {exc}")
    elif source_path:
        add("WARN", "Đối chiếu video nguồn", "Không tìm thấy file nguồn; bỏ qua so sánh với nguồn.")

    expected = expected_duration
    if expected is None and source_details:
        expected = max(0.0, float(source_details.get("duration") or 0.0) - max(0.0, expected_start))
    if expected is not None and float(expected) > 0 and out_duration > 0:
        expected = float(expected)
        tolerance = max(1.0, expected * 0.02)
        delta = abs(out_duration - expected)
        if delta <= tolerance:
            add("PASS", "Đối chiếu thời lượng", f"Kỳ vọng {expected:.2f}s; thực tế {out_duration:.2f}s (sai lệch {delta:.2f}s, ngưỡng {tolerance:.2f}s).")
        else:
            add("FAIL", "Đối chiếu thời lượng", f"Kỳ vọng {expected:.2f}s nhưng thực tế {out_duration:.2f}s (sai lệch {delta:.2f}s). Có thể video bị cắt thiếu hoặc xuất thừa.")
    else:
        add("WARN", "Đối chiếu thời lượng", "Không có thời lượng nguồn/kỳ vọng để so sánh; chỉ kiểm tra thời lượng file đầu ra.")

    if source_details:
        source_has_audio = source_details.get("audio") is not None
        if source_has_audio and not audio:
            add("FAIL", "Âm thanh", "Video nguồn có âm thanh nhưng file đầu ra không có audio stream.")
        elif source_has_audio and audio:
            add("PASS", "Âm thanh", f"Đã giữ audio ({audio.get('codec_name', 'codec không rõ')}, {audio.get('channels') or '?'} kênh).")
        elif not source_has_audio and not audio:
            add("PASS", "Âm thanh", "Video nguồn không có audio; đầu ra không có audio là phù hợp.")
        else:
            add("WARN", "Âm thanh", "Đầu ra có audio trong khi nguồn không báo audio stream.")
    else:
        add("PASS" if audio else "WARN", "Âm thanh", (f"Có audio stream ({audio.get('codec_name', 'codec không rõ')})." if audio else "Không có audio stream; chưa có nguồn để đối chiếu."))

    # Decode three representative frames and require actual image bytes from FFmpeg.
    if video and out_duration > 0:
        sample_times = sorted(set([min(0.05, out_duration * 0.1), out_duration * 0.5,
                                   max(0.0, out_duration - min(0.1, out_duration * 0.05))]))
        decoded = 0
        sample_errors = []
        ffmpeg = get_ffmpeg()
        for sec in sample_times:
            try:
                result = subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{sec:.3f}",
                     "-i", str(path), "-map", "0:v:0", "-frames:v", "1",
                     "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
                    capture_output=True, check=False, timeout=60,
                    **_hidden_subprocess_options())
                if result.returncode == 0 and result.stdout:
                    decoded += 1
                else:
                    err = result.stderr.decode("utf-8", errors="replace")[-300:]
                    sample_errors.append(f"{sec:.2f}s: {err or 'không lấy được khung hình'}")
            except Exception as exc:
                sample_errors.append(f"{sec:.2f}s: {exc}")
        if decoded == len(sample_times):
            add("PASS", "Đọc thử khung hình", f"Giải mã được {decoded}/{len(sample_times)} khung hình ở đầu, giữa và cuối video.")
        else:
            add("FAIL", "Đọc thử khung hình", f"Chỉ giải mã được {decoded}/{len(sample_times)} khung hình. " + " | ".join(sample_errors)[:700])
    else:
        add("FAIL", "Đọc thử khung hình", "Không thể kiểm tra khung hình vì không có video stream hoặc thời lượng hợp lệ.")

    if audio and out_duration > 0:
        audio_sample_time = max(0.0, min(out_duration * 0.5, out_duration - 0.25))
        try:
            audio_decode = subprocess.run(
                [get_ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{audio_sample_time:.3f}",
                 "-i", str(path), "-map", "0:a:0", "-t", "2", "-f", "null", "-"],
                capture_output=True, text=True, check=False, timeout=60,
                **_hidden_subprocess_options())
            if audio_decode.returncode == 0:
                add("PASS", "Đọc thử âm thanh", "FFmpeg giải mã được một đoạn audio mẫu.")
            else:
                detail = (audio_decode.stderr or "").strip()[-700:]
                add("FAIL", "Đọc thử âm thanh", detail or f"FFmpeg kết thúc với mã lỗi {audio_decode.returncode}.")
        except Exception as exc:
            add("FAIL", "Đọc thử âm thanh", str(exc))
    elif not audio:
        add("PASS", "Đọc thử âm thanh", "Không có audio stream để lấy mẫu.")

    if deep_scan and video:
        ffmpeg = get_ffmpeg()
        timeout_seconds = max(120, min(7200, int(out_duration * 5 + 60)))
        try:
            scan = subprocess.run(
                [ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-i", str(path),
                 "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"],
                capture_output=True, text=True, check=False, timeout=timeout_seconds,
                **_hidden_subprocess_options())
            detail = (scan.stderr or "").strip()[-1400:]
            if scan.returncode == 0 and not detail:
                add("PASS", "Quét giải mã toàn bộ", "FFmpeg giải mã toàn bộ video và audio mà không báo lỗi.")
            else:
                add("FAIL", "Quét giải mã toàn bộ", detail or f"FFmpeg kết thúc với mã lỗi {scan.returncode}.")
        except subprocess.TimeoutExpired:
            add("WARN", "Quét giải mã toàn bộ", f"Quá thời gian kiểm tra ({timeout_seconds}s); chưa kết luận file bị lỗi.")
        except Exception as exc:
            add("FAIL", "Quét giải mã toàn bộ", str(exc))

    failures = sum(1 for x in report["checks"] if x["status"] == "FAIL")
    warnings = sum(1 for x in report["checks"] if x["status"] == "WARN")
    report["status"] = "FAIL" if failures else ("WARN" if warnings else "PASS")
    report["summary"] = f"{sum(1 for x in report['checks'] if x['status']=='PASS')} đạt · {warnings} cảnh báo · {failures} lỗi"
    return report


def format_quality_report(report: dict) -> str:
    lines = [
        "GOC REVIEW PHIM VIDEO STUDIO - QUALITY CONTROL",
        f"Thoi gian kiem tra: {report.get('checked_at', '')}",
        f"Trang thai: {report.get('status', 'UNKNOWN')} - {report.get('summary', '')}",
        f"File dau ra: {report.get('output_path', '')}",
    ]
    if report.get("source_path"):
        lines.append(f"Video nguon: {report['source_path']}")
    meta = report.get("metadata") or {}
    if meta:
        lines.append(f"Metadata: {meta.get('width')}x{meta.get('height')} | {meta.get('fps', 0):.3f} FPS | {meta.get('duration', 0):.2f}s | {meta.get('video_codec', '')} | audio={meta.get('audio_codec') or 'none'}")
    lines.append("")
    for check in report.get("checks", []):
        lines.append(f"[{check['status']}] {check['name']}: {check['detail']}")
    return "\n".join(lines)


def read_frame(path: str, seconds: float) -> np.ndarray:
    ffmpeg = get_ffmpeg()
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, seconds):.3f}",
           "-i", path, "-map", "0:v:0", "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                            **_hidden_subprocess_options())
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError("Không đọc được khung hình: " + (detail[-500:] or "thử mốc thời gian khác"))
    data = np.frombuffer(result.stdout, dtype=np.uint8)
    frame = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError("FFmpeg đã trích xuất nhưng không giải mã được ảnh khung hình.")
    return frame


def polygon_to_mask(frame_shape, polygon: list[tuple[int, int]]) -> np.ndarray:
    h, w = frame_shape[:2]
    selection = np.zeros((h, w), np.uint8)
    pts = np.array(polygon, np.int32).reshape((-1, 1, 2))
    cv2.fillPoly(selection, [pts], 255)
    return selection


def extract_text_mask(frame: np.ndarray, polygon: list[tuple[int, int]], *, return_info: bool = False):
    """Try foreground extraction inside a user selection.

    GrabCut is useful for sparse text/logo strokes, but it is not reliable for
    composed overlays such as subscribe cards (logo + text + solid panel).  In
    those cases we fall back to the user's exact polygon instead of failing.
    The fallback deliberately keeps the selection boundary: the user can see
    the full red area and refine it with the brush/eraser before export.
    """
    h, w = frame.shape[:2]
    if len(polygon) < 3:
        raise ValueError("Hãy khoanh một vùng quanh logo, chữ hoặc banner trước.")

    selection = polygon_to_mask(frame.shape, polygon)
    x, y, bw, bh = cv2.boundingRect(np.array(polygon, np.int32))
    if bw < 6 or bh < 6 or cv2.countNonZero(selection) < 20:
        raise ValueError("Vùng khoanh quá nhỏ. Hãy khoanh rộng hơn một chút.")

    margin = max(4, int(min(bw, bh) * 0.03))
    x0, y0 = max(0, x - margin), max(0, y - margin)
    x1, y1 = min(w, x + bw + margin), min(h, y + bh + margin)
    crop = frame[y0:y1, x0:x1]
    sel = selection[y0:y1, x0:x1]
    crop_h, crop_w = crop.shape[:2]
    # GrabCut cost grows quickly with pixel count. Work on a capped crop for
    # 4K/large selections, then map the binary result back to source resolution.
    work_scale = min(1.0, 1280.0 / max(crop_w, crop_h))
    if work_scale < 1.0:
        work_size = (max(1, int(round(crop_w * work_scale))),
                     max(1, int(round(crop_h * work_scale))))
        crop_work = cv2.resize(crop, work_size, interpolation=cv2.INTER_AREA)
        sel_work = cv2.resize(sel, work_size, interpolation=cv2.INTER_NEAREST)
    else:
        crop_work, sel_work = crop, sel

    gc = np.full(sel_work.shape, cv2.GC_BGD, np.uint8)
    gc[sel_work > 0] = cv2.GC_PR_FGD
    inner = cv2.erode(sel_work, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)), iterations=1)
    gc[inner > 0] = cv2.GC_PR_FGD
    bg_model = np.zeros((1, 65), np.float64)
    fg_model = np.zeros((1, 65), np.float64)
    local = None
    try:
        cv2.grabCut(crop_work, gc, None, bg_model, fg_model, 5, cv2.GC_INIT_WITH_MASK)
        local = np.where((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(local, 8)
        cleaned = np.zeros_like(local)
        max_area = max(2000, int(local.size * 0.22))
        for i in range(1, n):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if 3 <= area <= max_area:
                cleaned[labels == i] = 255
        cleaned = cv2.bitwise_and(cleaned, sel_work)
        selected_area = max(1, cv2.countNonZero(sel_work))
        kept_area = cv2.countNonZero(cleaned)
        if kept_area >= max(8, int(selected_area * 0.008)) and kept_area <= int(selected_area * 0.88):
            if work_scale < 1.0:
                cleaned = cv2.resize(cleaned, (crop_w, crop_h), interpolation=cv2.INTER_NEAREST)
            result = np.zeros((h, w), np.uint8)
            result[y0:y1, x0:x1] = cleaned
            return (result, False) if return_info else result
    except cv2.error:
        pass

    # Robust fallback for logos/banners with backgrounds: use the region the
    # user intentionally selected. This is much safer than an error dialog or
    # silently producing an empty mask.
    fallback = selection
    return (fallback, True) if return_info else fallback


def _anchor_top_left(anchor: str, width: int, height: int, obj_w: int, obj_h: int,
                     margin: int) -> tuple[int, int]:
    positions = {
        "top_left": (margin, margin),
        "top_center": ((width - obj_w) // 2, margin),
        "top_right": (width - obj_w - margin, margin),
        "center_left": (margin, (height - obj_h) // 2),
        "center": ((width - obj_w) // 2, (height - obj_h) // 2),
        "center_right": (width - obj_w - margin, (height - obj_h) // 2),
        "bottom_left": (margin, height - obj_h - margin),
        "bottom_center": ((width - obj_w) // 2, height - obj_h - margin),
        "bottom_right": (width - obj_w - margin, height - obj_h - margin),
    }
    return positions.get(anchor, positions["top_right"])


def default_branding_config(duration: float = 0.0) -> dict:
    return {
        "logo_enabled": True, "label_enabled": True,
        "logo_anchor": "top_right", "logo_x": 0.91, "logo_y": 0.10,
        "logo_width_pct": 10.5, "logo_opacity": 100, "logo_rotation": 0,
        "label_anchor": "top_center", "label_x": 0.50, "label_y": 0.065,
        "label_scale": 100, "label_text_color": "#FFFFFF",
        "label_bg_color": "#990000", "label_opacity": 100,
        "show_from": 0.0, "show_to": max(0.0, float(duration or 0.0)),
    }


def _draw_branding_label(width: int, height: int, text: str, config: dict) -> Image.Image:
    scale = max(40, min(180, int(config.get("label_scale", 100)))) / 100.0
    badge_w = max(135, int(width * 0.115 * scale))
    badge_h = max(43, int(height * 0.061 * scale))
    image = Image.new("RGBA", (badge_w, badge_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    bg = str(config.get("label_bg_color", "#990000"))
    fg = str(config.get("label_text_color", "#FFFFFF"))
    try:
        draw.rectangle((2, 3, badge_w - 3, badge_h - 3), fill=(0, 0, 0, 65))
        draw.rectangle((0, 0, badge_w - 4, badge_h - 6), fill=bg)
    except Exception:
        draw.rectangle((2, 3, badge_w - 3, badge_h - 3), fill=(0, 0, 0, 65))
        draw.rectangle((0, 0, badge_w - 4, badge_h - 6), fill=(153, 0, 0, 245))
    font = None
    candidates = []
    if os.name == "nt":
        candidates = [r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf"]
    else:
        candidates = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    font_size = max(14, int(39 * scale))
    for candidate in candidates:
        try:
            font = ImageFont.truetype(candidate, font_size)
            break
        except Exception:
            pass
    if font is None:
        font = ImageFont.load_default()
    label = (text or "Tập 1").strip() or "Tập 1"
    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if tw > badge_w - 20 and candidates:
        try:
            font = ImageFont.truetype(candidates[0], max(12, int(font_size * (badge_w - 20) / max(1, tw))))
            bbox = draw.textbbox((0, 0), label, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            pass
    x = (badge_w - tw) / 2 - bbox[0]
    y = (badge_h - 6 - th) / 2 - bbox[1]
    try:
        draw.text((x, y), label, fill=fg, font=font)
    except Exception:
        draw.text((x, y), label, fill=(255, 255, 255, 255), font=font)
    opacity = max(0, min(100, int(config.get("label_opacity", 96)))) / 100.0
    if opacity < 1:
        image.putalpha(image.getchannel("A").point(lambda a: int(a * opacity)))
    return image


def build_branding_components(width: int, height: int, logo_path: str,
                               episode_text: str, config: Optional[dict] = None):
    cfg = {**default_branding_config(), **(config or {})}
    logo_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    label_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    margin = max(18, int(width * 0.018))
    if bool(cfg.get("logo_enabled", True)) and logo_path and Path(logo_path).exists():
        try:
            with Image.open(logo_path) as source:
                source.seek(0)
                logo = source.convert("RGBA")
            target_w = max(24, int(width * max(3.0, min(40.0, float(cfg.get("logo_width_pct", 10.5)))) / 100.0))
            target_h = max(1, int(logo.height * target_w / max(1, logo.width)))
            logo = logo.resize((target_w, target_h), Image.Resampling.LANCZOS)
            rotation = float(cfg.get("logo_rotation", 0))
            if abs(rotation) > 0.01:
                logo = logo.rotate(rotation, resample=Image.Resampling.BICUBIC, expand=True)
            opacity = max(0, min(100, int(cfg.get("logo_opacity", 90)))) / 100.0
            if opacity < 1:
                logo.putalpha(logo.getchannel("A").point(lambda a: int(a * opacity)))
            anchor = str(cfg.get("logo_anchor", "top_right"))
            if anchor == "custom":
                cx = max(0.0, min(1.0, float(cfg.get("logo_x", 0.91)))) * width
                cy = max(0.0, min(1.0, float(cfg.get("logo_y", 0.10)))) * height
                x, y = int(cx - logo.width / 2), int(cy - logo.height / 2)
            else:
                x, y = _anchor_top_left(anchor, width, height, logo.width, logo.height, margin)
            x = max(0, min(width - logo.width, x)); y = max(0, min(height - logo.height, y))
            logo_layer.alpha_composite(logo, (x, y))
        except Exception:
            pass
    if bool(cfg.get("label_enabled", True)):
        badge = _draw_branding_label(width, height, episode_text, cfg)
        anchor = str(cfg.get("label_anchor", "top_center"))
        if anchor == "custom":
            cx = max(0.0, min(1.0, float(cfg.get("label_x", 0.5)))) * width
            cy = max(0.0, min(1.0, float(cfg.get("label_y", 0.065)))) * height
            x, y = int(cx - badge.width / 2), int(cy - badge.height / 2)
        else:
            x, y = _anchor_top_left(anchor, width, height, badge.width, badge.height, margin)
        x = max(0, min(width - badge.width, x)); y = max(0, min(height - badge.height, y))
        label_layer.alpha_composite(badge, (x, y))
    def to_bgr_rgba(layer: Image.Image) -> np.ndarray:
        arr = np.asarray(layer).copy()
        arr[:, :, :3] = cv2.cvtColor(arr[:, :, :3], cv2.COLOR_RGB2BGR)
        return arr
    return to_bgr_rgba(logo_layer), to_bgr_rgba(label_layer)


def build_overlay(width: int, height: int, logo_path: str, episode_text: str,
                  logo_width_ratio: float = 0.105, branding_config: Optional[dict] = None) -> np.ndarray:
    cfg = dict(branding_config or {})
    if "logo_width_pct" not in cfg:
        cfg["logo_width_pct"] = float(logo_width_ratio) * 100.0
    logo_layer, label_layer = build_branding_components(width, height, logo_path, episode_text, cfg)
    # Keep the legacy helper contract: return one premultiplied-ready RGBA overlay in BGR channel order.
    overlay = Image.fromarray(logo_layer, "RGBA")
    overlay.alpha_composite(Image.fromarray(label_layer, "RGBA"))
    return np.asarray(overlay).copy()


def estimate_mask_expansion(mask: Optional[np.ndarray]) -> int:
    """Estimate a modest dilation from extracted glyph components.

    This is a geometry heuristic, not OCR: it uses connected-component
    heights from the user's lasso-limited mask and clamps the result to a
    conservative 4..20 px range. The user can always switch to manual mode.
    """
    if mask is None or cv2.countNonZero(mask) == 0:
        return MASK_DEFAULT_EXPANSION
    try:
        count, _, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
        heights = []
        for i in range(1, count):
            x, y, w, h, area = [int(v) for v in stats[i]]
            if area >= 3 and h >= 3 and w >= 2:
                heights.append(h)
        if not heights:
            return MASK_DEFAULT_EXPANSION
        # A small fraction of glyph height generally clears antialiased edges
        # without inflating the inpaint ROI excessively.
        glyph_height = float(np.median(heights))
        return int(max(4, min(20, round(glyph_height * 0.16))))
    except Exception:
        return MASK_DEFAULT_EXPANSION


def prepare_effects(mask: Optional[np.ndarray], expansion: int,
                    overlay_rgba: np.ndarray):
    """Precompute static mask/overlay crops once before the frame loop."""
    mask_pack = None
    if mask is not None and cv2.countNonZero(mask) > 0:
        if expansion > 0:
            k = expansion * 2 + 1
            active_mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
        else:
            active_mask = mask.copy()
        points = cv2.findNonZero(active_mask)
        if points is not None:
            x, y, w, h = cv2.boundingRect(points)
            # Fixed Telea radius 4; expanding the mask is calibrated separately.
            # An 8-second 1080p AV1 sample compared 12/16/20/24 px expansion.
            # 16 px was the smallest setting that fully cleared the white fill
            # and dark outline in this sample. 12 px left glyph remnants; 20/24
            # produced a wider inpaint patch and slower processing. Keep the
            # UI default at 16 px while allowing manual adjustment per source.
            mask_pack = (x, y, w, h, active_mask[y:y+h, x:x+w].copy(), 4)

    # The logo (top-right) and episode tag (top-center) are separate islands.
    # Treat them as independent ROIs instead of blending the entire bounding
    # rectangle between them. Precompute premultiplied color + scratch buffers
    # once; this avoids allocating float arrays on every video frame.
    overlay_packs = []
    if overlay_rgba is not None:
        alpha_full = overlay_rgba[:, :, 3]
        occupied_x = np.flatnonzero(np.any(alpha_full > 0, axis=0))
        if occupied_x.size:
            split_points = np.where(np.diff(occupied_x) > 12)[0] + 1
            x_groups = np.split(occupied_x, split_points)
            for group in x_groups:
                if group.size == 0:
                    continue
                x0, x1 = int(group[0]), int(group[-1]) + 1
                alpha_crop_full = alpha_full[:, x0:x1]
                occupied_y = np.flatnonzero(np.any(alpha_crop_full > 0, axis=1))
                if not occupied_y.size:
                    continue
                y0, y1 = int(occupied_y[0]), int(occupied_y[-1]) + 1
                rgba = overlay_rgba[y0:y1, x0:x1]
                alpha = rgba[:, :, 3:4].astype(np.float32) / 255.0
                inverse_alpha = 1.0 - alpha
                color = rgba[:, :, :3].astype(np.float32)
                premultiplied = color * alpha
                scratch = np.empty_like(premultiplied, dtype=np.float32)
                overlay_packs.append((x0, y0, x1 - x0, y1 - y0,
                                      premultiplied, inverse_alpha, scratch))
    return mask_pack, overlay_packs


def apply_prepared_effects(frame: np.ndarray, prepared) -> np.ndarray:
    """Apply fixed mask and logo directly to the writable frame buffer.

    The source frame comes from a bytearray owned by the bounded decode queue.
    Mutating it in place avoids an additional 6 MB full-frame allocation at
    1080p for every frame. Only watermark/logo ROIs allocate working arrays.
    """
    mask_pack, overlay_pack = prepared
    if mask_pack is not None:
        x, y, w, h, crop_mask, radius = mask_pack
        roi = frame[y:y+h, x:x+w]
        # cv2.inpaint returns a new ROI-sized image; assign it back into the
        # existing full-frame buffer instead of copying the whole frame.
        repaired = cv2.inpaint(roi, crop_mask, radius, cv2.INPAINT_TELEA)
        roi[:] = repaired
        del repaired
    for x, y, w, h, premultiplied, inverse_alpha, scratch in overlay_pack:
        roi = frame[y:y+h, x:x+w]
        # Reuse the preallocated scratch buffer for alpha blending. copyto
        # converts float32 to uint8 in-place without another full-size array.
        np.multiply(roi, inverse_alpha, out=scratch, casting="unsafe")
        scratch += premultiplied
        np.clip(scratch, 0.0, 255.0, out=scratch)
        np.copyto(roi, scratch, casting="unsafe")
    return frame


def apply_mask_and_overlay(frame: np.ndarray, mask: Optional[np.ndarray],
                           expansion: int, overlay_rgba: np.ndarray) -> np.ndarray:
    return apply_prepared_effects(frame, prepare_effects(mask, expansion, overlay_rgba))


def read_exact_into(stream, buffer: bytearray) -> bool:
    """Fill one reusable frame buffer, tolerating short pipe reads."""
    view = memoryview(buffer)
    offset = 0
    size = len(buffer)
    while offset < size:
        chunk_size = stream.readinto(view[offset:])
        if not chunk_size:
            return False
        offset += chunk_size
    return True


def read_exact(stream, size: int) -> Optional[bytes]:
    """Compatibility wrapper used by frame-inspection helpers/tests."""
    data = bytearray(size)
    return data if read_exact_into(stream, data) else None


def normalize_excluded_ranges(ranges, duration: Optional[float] = None,
                              merge_gap: float = 0.04) -> list[tuple[float, float]]:
    """Validate, clamp and merge source-time ranges scheduled to be omitted."""
    cleaned = []
    for item in ranges or []:
        try:
            if isinstance(item, dict):
                start, end = float(item.get("start", 0.0)), float(item.get("end", 0.0))
            else:
                start, end = float(item[0]), float(item[1])
            if not (math.isfinite(start) and math.isfinite(end)):
                continue
            start = max(0.0, start)
            end = max(0.0, end)
            if duration is not None:
                start = min(float(duration), start)
                end = min(float(duration), end)
            if end - start >= 0.05:
                cleaned.append((start, end))
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
    cleaned.sort()
    merged: list[list[float]] = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1] + max(0.0, merge_gap):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(float(a), float(b)) for a, b in merged]


def excluded_duration_in_range(ranges, start: float, end: float) -> float:
    """Return the total overlap between omitted ranges and [start, end]."""
    total = 0.0
    for cut_start, cut_end in normalize_excluded_ranges(ranges):
        total += max(0.0, min(float(end), cut_end) - max(float(start), cut_start))
    return total


def kept_time_ranges(start: float, end: float, excluded_ranges) -> list[tuple[float, float]]:
    """Return source-time spans that remain after omitting excluded intervals."""
    start, end = float(start), float(end)
    if end <= start:
        return []
    kept = []
    cursor = start
    for cut_start, cut_end in normalize_excluded_ranges(excluded_ranges, duration=end):
        cut_start = max(start, cut_start)
        cut_end = min(end, cut_end)
        if cut_end <= start or cut_start >= end:
            continue
        if cut_start > cursor + 1e-6:
            kept.append((cursor, cut_start))
        cursor = max(cursor, cut_end)
    if cursor < end - 1e-6:
        kept.append((cursor, end))
    return kept


def encode_video(input_path: str, output_path: str, start_seconds: float,
                 duration_seconds: Optional[float], mask: Optional[np.ndarray],
                 expansion: int, overlay_rgba: np.ndarray,
                 progress_cb: Callable[[float, str], None],
                 cancel_event: Optional[threading.Event] = None,
                 process_tracker: Optional[Callable[[subprocess.Popen, bool], None]] = None,
                 preview_mode: bool = False,
                 prefer_gpu: bool = True,
                 scene_masks: Optional[list[dict]] = None,
                 branding_range: Optional[tuple[float, float]] = None,
                 excluded_ranges: Optional[list[tuple[float, float]]] = None) -> None:
    """Stream one frame at a time through the mask compositor and FFmpeg encoder.

    Large files are never loaded into RAM. The raw pipe carries BGR24 directly
    (avoiding two full-frame color conversions). For preview only, FFmpeg scales
    to 720p before the pipe to reduce CPU, memory bandwidth and encode work.
    NVIDIA NVENC is probed at runtime; if the FFmpeg build/driver cannot use it,
    the function transparently falls back to libx264.
    """
    def check_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise ExportCancelled("Bạn đã dừng xuất video theo yêu cầu. File đang xuất dở đã được xóa.")

    check_cancelled()
    ffmpeg = get_ffmpeg()
    info = read_video_info(input_path)
    source_width, source_height, fps = info["width"], info["height"], info["fps"]
    if mask is not None and mask.shape[:2] != (source_height, source_width):
        raise RuntimeError("Mask không khớp kích thước video. Hãy tải lại khung hình và tạo mask mới.")

    # Preview mode renders at 720p max. Segment/full exports keep source dimensions.
    out_width, out_height = source_width, source_height
    if preview_mode and source_height > 720:
        scale = 720.0 / source_height
        out_height = 720
        out_width = max(2, int(round((source_width * scale) / 2.0) * 2))

    effective_mask = mask
    effective_overlay = overlay_rgba
    effective_expansion = int(expansion)
    if (out_width, out_height) != (source_width, source_height):
        if effective_mask is not None:
            effective_mask = cv2.resize(effective_mask, (out_width, out_height), interpolation=cv2.INTER_NEAREST)
        effective_overlay = cv2.resize(overlay_rgba, (out_width, out_height), interpolation=cv2.INTER_AREA)
        effective_expansion = max(0, int(round(expansion * (out_height / source_height))))

    expected = None if duration_seconds is None else max(1, int(round(duration_seconds * fps)))
    if duration_seconds is not None and info["frames"] > 0:
        start_frame = max(0, int(round(start_seconds * fps)))
        remaining_frames = max(0, info["frames"] - start_frame)
        expected = min(expected, remaining_frames)
    if expected == 0:
        raise RuntimeError("Thời điểm xuất nằm ngoài thời lượng video. Hãy chọn mốc bắt đầu sớm hơn.")

    source_duration = (max(0.0, min(float(duration_seconds), float(info.get("duration", 0.0)) - start_seconds))
                       if duration_seconds is not None else max(0.0, float(info.get("duration", 0.0)) - start_seconds))
    source_expected = expected if expected is not None else (
        max(1, int(info.get("frames", 0)) - int(round(start_seconds * fps))) if info.get("frames", 0) > 0
        else max(1, int(round(source_duration * fps))) if source_duration > 0 else None
    )
    source_end = start_seconds + source_duration
    active_exclusions = []
    for cut_start, cut_end in normalize_excluded_ranges(excluded_ranges, duration=source_end):
        left, right = max(start_seconds, cut_start), min(source_end, cut_end)
        if right - left >= 0.02:
            active_exclusions.append((left, right))

    # Convert source-time exclusions into frame-index ranges. Frame indices are
    # relative to the decoded segment, so scene masks and branding still use the
    # original source timestamps after a cut.
    excluded_frame_ranges = []
    if active_exclusions and source_expected is not None:
        for cut_start, cut_end in active_exclusions:
            first = max(0, int(math.ceil((cut_start - start_seconds) * fps - 1e-7)))
            last = min(source_expected, int(math.ceil((cut_end - start_seconds) * fps - 1e-7)))
            if last > first:
                excluded_frame_ranges.append((first, last))
        excluded_frame_ranges.sort()
        merged_frames = []
        for first, last in excluded_frame_ranges:
            if merged_frames and first <= merged_frames[-1][1]:
                merged_frames[-1] = (merged_frames[-1][0], max(merged_frames[-1][1], last))
            else:
                merged_frames.append((first, last))
        excluded_frame_ranges = merged_frames
        excluded_count = sum(last - first for first, last in excluded_frame_ranges)
        if expected is not None:
            expected = max(0, expected - excluded_count)
            if expected <= 0:
                raise RuntimeError("Các khoảng bỏ đã bao phủ toàn bộ đoạn xuất. Hãy giảm hoặc xóa bớt khoảng thời gian.")

    # For full export this estimate is only used for progress/ETA; decoding runs to EOF.
    progress_total = source_expected

    frame_bytes = out_width * out_height * 3
    codec_name = str(info.get("codec", "")).lower()
    transparent_overlay = np.zeros_like(effective_overlay)
    prepared = prepare_effects(effective_mask, effective_expansion, transparent_overlay)
    prepared_global_branding = prepare_effects(None, 0, effective_overlay)
    global_brand_start, global_brand_end = (branding_range or (0.0, float("inf")))
    # Scene masks override the project-wide mask only inside their own time ranges.
    # Scene-specific branding can override the project-wide branding for that interval.
    prepared_scenes = []
    no_mask_prepared = prepare_effects(None, 0, transparent_overlay)
    if scene_masks:
        for scene in sorted(scene_masks, key=lambda item: float(item.get("start", 0.0))):
            scene_mask = scene.get("mask")
            if scene_mask is not None and scene_mask.shape[:2] != (source_height, source_width):
                raise RuntimeError("Mask của một phân cảnh không khớp kích thước video.")
            if scene_mask is not None and (out_width, out_height) != (source_width, source_height):
                scene_mask = cv2.resize(scene_mask, (out_width, out_height), interpolation=cv2.INTER_NEAREST)
            scene_expansion = max(0, int(scene.get("expansion", expansion)))
            if (out_width, out_height) != (source_width, source_height):
                scene_expansion = max(0, int(round(scene_expansion * (out_height / source_height))))
            scene_brand_overlay = scene.get("branding_overlay")
            if scene_brand_overlay is not None:
                if (out_width, out_height) != (source_width, source_height):
                    scene_brand_overlay = cv2.resize(scene_brand_overlay, (out_width, out_height), interpolation=cv2.INTER_AREA)
                scene_brand_prepared = prepare_effects(None, 0, scene_brand_overlay)
                scene_cfg = scene.get("branding_config") or {}
                scene_brand_start = float(scene_cfg.get("show_from", 0.0))
                scene_brand_end = float(scene_cfg.get("show_to", float("inf")))
            else:
                scene_brand_prepared = None
                scene_brand_start, scene_brand_end = 0.0, float("inf")
            prepared_scenes.append((
                max(0.0, float(scene.get("start", 0.0))),
                max(0.0, float(scene.get("end", 0.0))),
                prepare_effects(scene_mask, scene_expansion, transparent_overlay),
                scene_brand_prepared,
                scene_brand_start,
                scene_brand_end,
            ))
    if prefer_gpu:
        progress_cb(0.0, "Đang kiểm tra NVIDIA NVENC để tăng tốc xuất video…")
    video_encoder, _encoder_label = choose_video_encoder(ffmpeg, prefer_gpu=prefer_gpu)
    active_video_encoder = video_encoder
    logical_cpus = os.cpu_count() or 8
    # When NVENC is active, leave CPU headroom for AV1 decode + inpaint.
    # With CPU x264, keep a few more cores free for the encoder.
    if active_video_encoder == "h264_nvenc":
        av1_decode_threads = max(4, min(8, logical_cpus - 2))
    else:
        av1_decode_threads = max(3, min(6, logical_cpus - 3))
    use_dav1d = bool(codec_name == "av1" and ffmpeg_has_decoder(ffmpeg, "libdav1d"))
    if codec_name == "av1":
        decoder_label = "dav1d" if use_dav1d else "FFmpeg AV1"
        decode_note = f"AV1 CPU/{decoder_label} ({av1_decode_threads} luồng)"
    elif prefer_gpu and ffmpeg_has_hwaccel(ffmpeg, "cuda") and codec_name in {"h264", "hevc"}:
        decode_note = "thử giải mã NVIDIA NVDEC"
    else:
        decode_note = "giải mã CPU"
    progress_cb(0.0, f"Đã chọn {('NVIDIA NVENC' if video_encoder == 'h264_nvenc' else 'CPU x264')} • {decode_note}…")

    def run_pass(seek_mode: str, audio_codec: str, selected_encoder: str, decoder_hwaccel: bool = False):
        if seek_mode == "fast":
            dec_cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
            if decoder_hwaccel:
                dec_cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            elif codec_name == "av1":
                # dav1d is a dedicated multithreaded AV1 decoder when available.
                dec_cmd += ["-threads", str(av1_decode_threads)]
                if use_dav1d:
                    dec_cmd += ["-c:v", "libdav1d"]
            dec_cmd += ["-ss", f"{max(0, start_seconds):.3f}", "-i", input_path]
        else:
            dec_cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
            if decoder_hwaccel:
                dec_cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            elif codec_name == "av1":
                dec_cmd += ["-threads", str(av1_decode_threads)]
                if use_dav1d:
                    dec_cmd += ["-c:v", "libdav1d"]
            dec_cmd += ["-i", input_path, "-ss", f"{max(0, start_seconds):.3f}"]
        if duration_seconds is not None:
            dec_cmd += ["-t", f"{duration_seconds:.3f}"]
        dec_cmd += ["-map", "0:v:0"]
        if decoder_hwaccel:
            vf_parts = ["hwdownload", "format=nv12"]
            if (out_width, out_height) != (source_width, source_height):
                vf_parts.append(f"scale={out_width}:{out_height}:flags=fast_bilinear")
            vf_parts.append("format=bgr24")
            dec_cmd += ["-vf", ",".join(vf_parts)]
        elif (out_width, out_height) != (source_width, source_height):
            dec_cmd += ["-vf", f"scale={out_width}:{out_height}:flags=fast_bilinear,format=bgr24"]
        else:
            dec_cmd += ["-pix_fmt", "bgr24"]
        if _ffmpeg_has_option(ffmpeg, "-fps_mode"):
            dec_cmd += ["-fps_mode", "passthrough"]
        dec_cmd += ["-f", "rawvideo", "pipe:1"]

        enc_cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{out_width}x{out_height}",
            "-r", f"{fps:.8f}", "-i", "pipe:0",
            "-ss", f"{max(0, start_seconds):.3f}", "-i", input_path,
            "-map", "0:v:0"
        ]
        if active_exclusions:
            # Audio must undergo the same time edits as video. Refuse a silent
            # desync when ffprobe is unavailable instead of exporting bad A/V.
            media_details = probe_media_details(input_path)
            has_audio = bool(media_details.get("audio"))
            if has_audio:
                timeline_end = source_end
                kept_ranges = kept_time_ranges(start_seconds, timeline_end, active_exclusions)
                if not kept_ranges:
                    raise RuntimeError("Không còn đoạn video nào sau khi áp dụng các khoảng Subscribe cần bỏ.")
                audio_parts = []
                audio_labels = []
                for part_index, (keep_start, keep_end) in enumerate(kept_ranges):
                    rel_start = max(0.0, keep_start - start_seconds)
                    rel_end = max(rel_start, keep_end - start_seconds)
                    label = f"a{part_index}"
                    audio_parts.append(
                        f"[1:a:0]atrim=start={rel_start:.6f}:end={rel_end:.6f},"
                        f"asetpts=PTS-STARTPTS[{label}]"
                    )
                    audio_labels.append(f"[{label}]")
                if len(audio_labels) == 1:
                    audio_parts.append(f"{audio_labels[0]}anull[aout]")
                else:
                    audio_parts.append(
                        "".join(audio_labels) + f"concat=n={len(audio_labels)}:v=0:a=1[aout]"
                    )
                enc_cmd += ["-filter_complex", ";".join(audio_parts), "-map", "[aout]"]
            else:
                enc_cmd += ["-map", "1:a?"]
        else:
            enc_cmd += ["-map", "1:a?"]
        if selected_encoder == "h264_nvenc":
            enc_cmd += ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                        "-cq", "18", "-b:v", "0", "-pix_fmt", "yuv420p"]
        else:
            enc_cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "17",
                        "-pix_fmt", "yuv420p"]
        enc_cmd += ["-c:a", audio_codec]
        if duration_seconds is not None:
            # Faststart is useful for previews/segments. A full large export skips
            # the extra moov relocation pass to avoid rewriting gigabytes at EOF.
            enc_cmd += ["-movflags", "+faststart"]
        if audio_codec == "aac":
            enc_cmd += ["-b:a", "192k"]
        if duration_seconds is not None and not active_exclusions:
            enc_cmd += ["-t", f"{duration_seconds:.3f}"]
        enc_cmd += [output_path]

        Path(output_path).unlink(missing_ok=True)
        dec_log = tempfile.TemporaryFile()
        enc_log = tempfile.TemporaryFile()
        decoder = None
        encoder = None
        count = 0
        write_broken = False
        start_clock = time.perf_counter()
        try:
            check_cancelled()
            decoder = subprocess.Popen(
                dec_cmd, stdout=subprocess.PIPE, stderr=dec_log,
                bufsize=1024 * 1024, **_hidden_subprocess_options()
            )
            if process_tracker:
                process_tracker(decoder, True)
            check_cancelled()
            encoder = subprocess.Popen(
                enc_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=enc_log, bufsize=1024 * 1024, **_hidden_subprocess_options()
            )
            if process_tracker:
                process_tracker(encoder, True)
            check_cancelled()
            # A tiny bounded queue lets FFmpeg decode the next frame while
            # OpenCV repairs/blends the current frame. At 1080p, two queued
            # BGR frames consume only about 12 MB; memory use cannot grow with
            # the duration or file size of the source video.
            frame_queue: Queue = Queue(maxsize=2)
            end_marker = object()
            reader_errors: list[BaseException] = []
            reader_stop_event = threading.Event()

            def reader_should_stop():
                return reader_stop_event.is_set() or bool(cancel_event and cancel_event.is_set())

            def decoder_reader():
                try:
                    while not reader_should_stop():
                        frame_buffer = bytearray(frame_bytes)
                        if not read_exact_into(decoder.stdout, frame_buffer):
                            break
                        while not reader_should_stop():
                            try:
                                frame_queue.put(frame_buffer, timeout=0.1)
                                break
                            except Exception:
                                continue
                except BaseException as exc:
                    if not reader_should_stop():
                        reader_errors.append(exc)
                finally:
                    # EOF marker is only needed on natural completion. On
                    # segment limit/cancel/error, the consumer is already
                    # leaving the loop and must never leave a daemon blocked.
                    while not reader_should_stop():
                        try:
                            frame_queue.put(end_marker, timeout=0.1)
                            break
                        except Exception:
                            continue

            reader_thread = threading.Thread(target=decoder_reader, name="GocReview-FrameReader", daemon=True)
            reader_thread.start()
            update_every = max(1, int(fps // 2))
            scene_pointer = 0
            source_frame_index = 0
            while True:
                check_cancelled()
                if expected is not None and count >= expected:
                    break
                try:
                    raw = frame_queue.get(timeout=0.2)
                except Empty:
                    if decoder.poll() is not None and not reader_thread.is_alive():
                        break
                    continue
                if raw is end_marker:
                    break
                # The bytearray belongs to this queue item. OpenCV mutates it
                # in place; a memoryview writes it to FFmpeg without .tobytes()
                # allocating another full-frame copy.
                frame = np.frombuffer(raw, dtype=np.uint8).reshape((out_height, out_width, 3))
                frame_time = start_seconds + (source_frame_index / max(0.001, fps))
                current_source_index = source_frame_index
                source_frame_index += 1
                if any(first <= current_source_index < last for first, last in excluded_frame_ranges):
                    if progress_total and source_frame_index % update_every == 0:
                        elapsed = max(0.001, time.perf_counter() - start_clock)
                        source_rate = source_frame_index / elapsed
                        pct = min(99.0, source_frame_index * 100.0 / progress_total)
                        remaining = max(0, progress_total - source_frame_index) / max(0.01, source_rate)
                        eta_seconds = int(remaining)
                        hours, rem = divmod(eta_seconds, 3600)
                        minutes, seconds = divmod(rem, 60)
                        eta_clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
                        progress_cb(pct, f"Đang bỏ khoảng đã chọn · {source_frame_index:,} khung hình nguồn • còn khoảng {eta_clock}")
                    del frame, raw
                    continue
                active_mask_prepared = prepared
                active_branding_prepared = prepared_global_branding
                active_scene_branding = False
                scene_branding_override_active = False
                if prepared_scenes:
                    while scene_pointer < len(prepared_scenes) and frame_time >= prepared_scenes[scene_pointer][1]:
                        scene_pointer += 1
                    # When scene mode is active, frames outside defined scenes use no scene mask.
                    active_mask_prepared = no_mask_prepared
                    if scene_pointer < len(prepared_scenes):
                        scene_start, scene_end, scene_pack, scene_brand_pack, scene_brand_start, scene_brand_end = prepared_scenes[scene_pointer]
                        if scene_start <= frame_time < scene_end:
                            active_mask_prepared = scene_pack
                            if scene_brand_pack is not None:
                                scene_branding_override_active = True
                                if scene_brand_start <= frame_time < scene_brand_end:
                                    active_branding_prepared = scene_brand_pack
                                    active_scene_branding = True
                processed = apply_prepared_effects(frame, active_mask_prepared)
                if ((scene_branding_override_active and active_scene_branding) or
                        (not scene_branding_override_active and global_brand_start <= frame_time < global_brand_end)):
                    processed = apply_prepared_effects(frame, active_branding_prepared)
                check_cancelled()
                try:
                    encoder.stdin.write(memoryview(processed).cast("B"))
                except (BrokenPipeError, OSError):
                    if cancel_event is not None and cancel_event.is_set():
                        raise ExportCancelled("Bạn đã dừng xuất video theo yêu cầu.")
                    write_broken = True
                    break
                count += 1
                if source_frame_index % update_every == 0:
                    elapsed = max(0.001, time.perf_counter() - start_clock)
                    render_fps = count / elapsed
                    source_rate = source_frame_index / elapsed
                    if progress_total:
                        pct = min(99.0, source_frame_index * 100.0 / progress_total)
                        remaining = max(0, progress_total - source_frame_index) / max(0.01, source_rate)
                        eta_seconds = int(remaining)
                        hours, rem = divmod(eta_seconds, 3600)
                        minutes, seconds = divmod(rem, 60)
                        eta_clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
                        eta = f" • Còn khoảng {eta_clock}"
                    else:
                        pct = 0.0
                        eta = ""
                    encoder_note = "NVIDIA NVENC" if selected_encoder == "h264_nvenc" else "CPU x264"
                    if decoder_hwaccel:
                        decoder_note = "NVDEC"
                    elif codec_name == "av1":
                        decoder_note = "AV1 dav1d CPU" if use_dav1d else "AV1 CPU"
                    else:
                        decoder_note = "CPU decode"
                    progress_cb(pct, f"{count:,} khung hình • {render_fps:.1f} FPS • {encoder_note} • {decoder_note}{eta}")
                del frame, processed, raw

            # Stop/join the producer before closing the decoder pipe. On a
            # normal EOF it has already enqueued the sentinel; on segment
            # completion/cancel, terminating FFmpeg releases a pending read.
            reached_limit = expected is not None and count >= expected
            if write_broken or reached_limit or (cancel_event and cancel_event.is_set()):
                reader_stop_event.set()
            if (write_broken or reached_limit or (cancel_event and cancel_event.is_set())) and decoder.poll() is None:
                try:
                    decoder.terminate()
                    decoder.wait(timeout=5)
                except Exception:
                    try:
                        decoder.kill()
                        decoder.wait(timeout=3)
                    except Exception:
                        pass
            reader_thread.join(timeout=3)
            if reader_thread.is_alive() and decoder.stdout:
                try:
                    decoder.stdout.close()
                except Exception:
                    pass
                reader_thread.join(timeout=2)
            if reader_errors and not (cancel_event and cancel_event.is_set()):
                raise RuntimeError(f"Lỗi đọc khung hình: {reader_errors[0]}")

            check_cancelled()
            try:
                if encoder.stdin:
                    encoder.stdin.close()
            except Exception:
                pass
            dec_code = decoder.wait()
            enc_code = encoder.wait()
            # FFmpeg is intentionally terminated once the exact requested frame
            # count has been consumed. Its non-zero signal exit is not a decode
            # failure when the segment is complete and cancellation was not asked.
            if expected is not None and count >= expected and not (cancel_event and cancel_event.is_set()):
                dec_code = 0
            dec_log.seek(0)
            enc_log.seek(0)
            dec_err = dec_log.read().decode("utf-8", errors="replace")
            enc_err = enc_log.read().decode("utf-8", errors="replace")
            check_cancelled()
        finally:
            if "reader_stop_event" in locals():
                reader_stop_event.set()
            if decoder is not None and decoder.poll() is None:
                try:
                    decoder.terminate()
                    decoder.wait(timeout=2)
                except Exception:
                    try:
                        decoder.kill()
                        decoder.wait(timeout=2)
                    except Exception:
                        pass
            if "reader_thread" in locals() and reader_thread is not None and reader_thread.is_alive():
                reader_thread.join(timeout=1)
            for proc in (decoder, encoder):
                if proc is not None and proc.poll() is None:
                    try:
                        proc.kill()
                        proc.wait(timeout=3)
                    except Exception:
                        pass
                if proc is not None and process_tracker:
                    process_tracker(proc, False)
            for proc in (decoder, encoder):
                if proc is not None:
                    for pipe_name in ("stdout", "stdin"):
                        pipe = getattr(proc, pipe_name, None)
                        if pipe is not None:
                            try:
                                pipe.close()
                            except Exception:
                                pass
            dec_log.close()
            enc_log.close()
        return count, dec_code, enc_code, dec_err, enc_err, write_broken

    # RTX 2070 supports NVDEC for H.264/HEVC, but not AV1. AV1 sources therefore
    # decode on CPU while NVENC can still accelerate H.264 output encoding.
    use_nvdec = bool(prefer_gpu and codec_name in {"h264", "hevc"} and ffmpeg_has_hwaccel(ffmpeg, "cuda"))

    def run_with_encoder(seek_mode: str, audio_codec: str):
        nonlocal use_nvdec, use_dav1d, active_video_encoder
        result = run_pass(seek_mode, audio_codec, active_video_encoder, decoder_hwaccel=use_nvdec)
        count, dec_code, enc_code, dec_err, enc_err, write_broken = result
        if use_dav1d and (dec_code != 0 or count == 0) and not (cancel_event and cancel_event.is_set()):
            progress_cb(0.0, "Decoder dav1d không đọc được đoạn này. Đang chuyển sang decoder AV1 mặc định…")
            use_dav1d = False
            result = run_pass(seek_mode, audio_codec, active_video_encoder, decoder_hwaccel=use_nvdec)
            count, dec_code, enc_code, dec_err, enc_err, write_broken = result
        if use_nvdec and (dec_code != 0 or count == 0) and not (cancel_event and cancel_event.is_set()):
            progress_cb(0.0, "NVDEC không đọc được nguồn này. Đang chuyển giải mã CPU…")
            use_nvdec = False
            result = run_pass(seek_mode, audio_codec, active_video_encoder, decoder_hwaccel=False)
            count, dec_code, enc_code, dec_err, enc_err, write_broken = result
        if active_video_encoder == "h264_nvenc" and enc_code != 0 and not (cancel_event and cancel_event.is_set()):
            progress_cb(0.0, "NVIDIA NVENC không khởi tạo được. Đang tự chuyển sang CPU x264…")
            active_video_encoder = "libx264"
            result = run_pass(seek_mode, audio_codec, active_video_encoder, decoder_hwaccel=use_nvdec)
            count, dec_code, enc_code, dec_err, enc_err, write_broken = result
            if use_nvdec and (dec_code != 0 or count == 0) and not (cancel_event and cancel_event.is_set()):
                use_nvdec = False
                result = run_pass(seek_mode, audio_codec, active_video_encoder, decoder_hwaccel=False)
        return result

    active_seek_mode = "fast"
    result = run_with_encoder(active_seek_mode, "copy")
    count, dec_code, enc_code, dec_err, enc_err, write_broken = result
    check_cancelled()

    if count == 0 and dec_code == 0:
        progress_cb(0.0, "Đang thử tua chính xác để tương thích video nguồn…")
        active_seek_mode = "accurate"
        result = run_with_encoder(active_seek_mode, "copy")
        count, dec_code, enc_code, dec_err, enc_err, write_broken = result

    check_cancelled()
    if count > 0 and enc_code != 0:
        progress_cb(0.0, "Đang chuyển âm thanh sang AAC để tương thích MP4…")
        result = run_with_encoder(active_seek_mode, "aac")
        count, dec_code, enc_code, dec_err, enc_err, write_broken = result
        check_cancelled()
        if count == 0 or enc_code != 0:
            active_seek_mode = "accurate"
            result = run_with_encoder(active_seek_mode, "aac")
            count, dec_code, enc_code, dec_err, enc_err, write_broken = result

    check_cancelled()
    if dec_code != 0:
        raise RuntimeError("FFmpeg gặp lỗi khi đọc video nguồn:\n" + (dec_err[-1200:] or f"Mã lỗi {dec_code}"))
    if enc_code != 0:
        raise RuntimeError("FFmpeg gặp lỗi khi ghi video:\n" + (enc_err[-1200:] or f"Mã lỗi {enc_code}"))
    if count <= 0:
        detail = dec_err.strip() or enc_err.strip()
        raise RuntimeError("FFmpeg không trả về khung hình nào để xuất. Kiểm tra file nguồn/thời điểm xem thử."
                           + ("\nChi tiết FFmpeg: " + detail[-900:] if detail else ""))
    if not Path(output_path).exists() or Path(output_path).stat().st_size < 100_000:
        raise RuntimeError("File đầu ra không hợp lệ hoặc quá nhỏ.")
    final_encoder_label = "NVIDIA NVENC" if active_video_encoder == "h264_nvenc" else "CPU x264"
    if use_nvdec:
        decode_label = "NVDEC"
    elif codec_name == "av1":
        decode_label = "CPU decode AV1 (dav1d)" if use_dav1d else "CPU decode AV1"
    else:
        decode_label = "CPU decode"
    size_label = f"{out_width}×{out_height}"
    progress_cb(100.0, f"Hoàn tất {count:,} khung hình • {size_label} • {final_encoder_label} • {decode_label}.")


def read_media_layout(path: str) -> dict:
    """Read concat-relevant stream metadata with ffprobe."""
    ffprobe = get_ffprobe()
    if not ffprobe:
        info = read_video_info(path)
        return {"video": {"codec_name": info.get("codec"), "width": info["width"],
                          "height": info["height"], "avg_frame_rate": str(info["fps"])},
                "audio": None, "duration": info["duration"], "info": info}
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,width,height,pix_fmt,avg_frame_rate,r_frame_rate,sample_rate,channels,channel_layout",
         "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True, check=False, timeout=30,
        **_hidden_subprocess_options()
    )
    if result.returncode != 0:
        raise RuntimeError(f"Không đọc được thông tin file {Path(path).name}: {(result.stderr or '')[-600:]}")
    import json
    data = json.loads(result.stdout or "{}")
    video = next((x for x in data.get("streams", []) if x.get("codec_type") == "video"), None)
    audio = next((x for x in data.get("streams", []) if x.get("codec_type") == "audio"), None)
    if not video:
        raise RuntimeError(f"File không có luồng video: {Path(path).name}")
    info = read_video_info(path)
    duration = float(data.get("format", {}).get("duration") or info.get("duration") or 0)
    return {"video": video, "audio": audio, "duration": duration, "info": info}


def _concat_quote(path: str) -> str:
    # concat demuxer syntax: single-quoted paths, with embedded apostrophes escaped.
    return "'" + str(Path(path).resolve()).replace("\\", "/").replace("'", "'\\''") + "'"


def _run_ffmpeg_progress(cmd: list[str], total_duration: float,
                         progress_cb: Callable[[float, str], None],
                         cancel_event: threading.Event,
                         process_tracker: Optional[Callable[[subprocess.Popen, bool], None]],
                         stage_label: str) -> tuple[int, str]:
    """Run FFmpeg hidden and report progress from -progress pipe:1."""
    err_file = tempfile.TemporaryFile()
    proc = None
    last_pct = -1.0
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=err_file,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            **_hidden_subprocess_options()
        )
        if process_tracker:
            process_tracker(proc, True)
        out_time = 0.0
        assert proc.stdout is not None
        while True:
            if cancel_event.is_set() and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            line = line.strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    out_time = max(0.0, float(line.split("=", 1)[1]) / 1_000_000.0)
                except ValueError:
                    pass
            elif line.startswith("progress="):
                pct = min(99.0, 100.0 * out_time / max(0.1, total_duration))
                if pct - last_pct >= 0.4:
                    last_pct = pct
                    elapsed_text = f"{out_time:.1f}/{total_duration:.1f}s"
                    progress_cb(pct, f"{stage_label} • {elapsed_text}")
        code = proc.wait()
        err_file.seek(0)
        error = err_file.read().decode("utf-8", errors="replace")
        if cancel_event.is_set():
            raise ExportCancelled("Đã dừng ghép video theo yêu cầu.")
        return code, error
    finally:
        if proc is not None:
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass
            if process_tracker:
                process_tracker(proc, False)
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
        err_file.close()


def merge_videos(input_paths: list[str], output_path: str,
                 progress_cb: Callable[[float, str], None],
                 cancel_event: threading.Event,
                 process_tracker: Optional[Callable[[subprocess.Popen, bool], None]] = None,
                 prefer_gpu: bool = True, overwrite: bool = False) -> str:
    """Join videos in order. Stream-copy when compatible; otherwise normalize and re-encode.

    Files are processed by FFmpeg without loading the full media into RAM.
    A successful concat-demuxer pass preserves original streams and quality.
    Incompatible sources fall back to a normalized H.264/AAC encode using
    NVENC only after a real device initialization test.
    """
    paths = [str(Path(p).resolve()) for p in input_paths]
    if len(paths) < 2:
        raise ValueError("Hãy chọn ít nhất 2 video để ghép.")
    if len(set(paths)) != len(paths):
        raise ValueError("Danh sách có video bị chọn trùng. Hãy bỏ mục trùng trước khi ghép.")
    for path in paths:
        if not Path(path).is_file():
            raise FileNotFoundError(f"Không tìm thấy video: {path}")
    final_path = Path(output_path).resolve()
    if str(final_path) in paths:
        raise ValueError("File đầu ra không được trùng với một video nguồn.")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = final_path.with_name(final_path.stem + ".__partial__" + final_path.suffix)
    part_path.unlink(missing_ok=True)
    ffmpeg = get_ffmpeg()
    layouts = [read_media_layout(p) for p in paths]
    total_duration = sum(max(0.01, float(x["duration"])) for x in layouts)
    if total_duration <= 0:
        raise RuntimeError("Không xác định được thời lượng các video cần ghép.")

    list_path = None
    try:
        # First try stream-copy only when the elementary stream layouts match.
        def video_sig(layout):
            v = layout["video"]
            return (v.get("codec_name"), v.get("width"), v.get("height"),
                    v.get("pix_fmt"), v.get("avg_frame_rate") or v.get("r_frame_rate"))
        def audio_sig(layout):
            a = layout["audio"]
            if not a:
                return None
            return (a.get("codec_name"), a.get("sample_rate"), a.get("channels"), a.get("channel_layout"))
        copy_compatible = all(video_sig(x) == video_sig(layouts[0]) and
                              audio_sig(x) == audio_sig(layouts[0]) for x in layouts[1:])

        if copy_compatible:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n",
                                             suffix=".ffconcat", delete=False) as f:
                list_path = f.name
                f.write("ffconcat version 1.0\n")
                for p in paths:
                    f.write("file " + _concat_quote(p) + "\n")
            copy_cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "concat", "-safe", "0", "-i", list_path,
                        "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                        "-fflags", "+genpts", "-movflags", "+faststart",
                        "-progress", "pipe:1", "-stats_period", "0.5", "-nostats", str(part_path)]
            progress_cb(0.0, "Đang ghép nhanh không mã hóa lại (giữ nguyên chất lượng)…")
            code, err = _run_ffmpeg_progress(copy_cmd, total_duration, progress_cb,
                                             cancel_event, process_tracker, "Ghép nhanh")
            copy_ok = code == 0 and part_path.exists() and part_path.stat().st_size > 50_000
            if copy_ok:
                try:
                    check = read_video_info(str(part_path))
                    copy_ok = check["duration"] >= total_duration * 0.94
                except Exception:
                    copy_ok = False
            if not copy_ok:
                part_path.unlink(missing_ok=True)
                progress_cb(0.0, "Các luồng không nối trực tiếp được. Đang chuyển sang ghép tương thích…")
        else:
            progress_cb(0.0, "Video khác thông số. Đang chuẩn hóa để ghép liền mạch…")

        if not part_path.exists() or part_path.stat().st_size <= 50_000:
            first = layouts[0]["info"]
            width = max(2, int(first["width"]) // 2 * 2)
            height = max(2, int(first["height"]) // 2 * 2)
            fps = float(first.get("fps") or 30.0)
            fps = min(60.0, max(1.0, fps))
            cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-filter_complex_threads", "2"]
            for p in paths:
                cmd += ["-i", p]
            filters = []
            concat_labels = []
            for index, layout in enumerate(layouts):
                dur = max(0.01, float(layout["duration"]))
                vlabel, alabel = f"v{index}", f"a{index}"
                filters.append(
                    f"[{index}:v:0]trim=duration={dur:.6f},setpts=PTS-STARTPTS,"
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                    f"fps={fps:.8f},format=yuv420p[{vlabel}]"
                )
                if layout["audio"]:
                    filters.append(
                        f"[{index}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                        f"apad,atrim=duration={dur:.6f},asetpts=PTS-STARTPTS[{alabel}]"
                    )
                else:
                    filters.append(
                        f"anullsrc=r=48000:cl=stereo,atrim=duration={dur:.6f},"
                        f"asetpts=PTS-STARTPTS[{alabel}]"
                    )
                concat_labels.extend([f"[{vlabel}]", f"[{alabel}]"])
            filters.append("".join(concat_labels) + f"concat=n={len(paths)}:v=1:a=1[vout][aout]")
            cmd += ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]"]
            encoder, label = choose_video_encoder(ffmpeg, prefer_gpu=prefer_gpu)
            if encoder == "h264_nvenc":
                cmd += ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "18", "-b:v", "0"]
            else:
                cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
            cmd += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                    "-progress", "pipe:1", "-stats_period", "0.5", "-nostats", str(part_path)]
            progress_cb(0.0, f"Đang ghép và chuẩn hóa bằng {label}…")
            code, err = _run_ffmpeg_progress(cmd, total_duration, progress_cb,
                                             cancel_event, process_tracker, "Ghép và mã hóa")
            if code != 0:
                raise RuntimeError("Không ghép được video:\n" + (err[-1800:] or f"FFmpeg mã lỗi {code}."))

        if cancel_event.is_set():
            raise ExportCancelled("Đã dừng ghép video theo yêu cầu.")
        if not part_path.exists() or part_path.stat().st_size <= 50_000:
            raise RuntimeError("File ghép đầu ra không hợp lệ hoặc quá nhỏ.")
        result_info = read_video_info(str(part_path))
        if result_info["duration"] < total_duration * 0.90:
            raise RuntimeError(f"Video ghép ngắn bất thường ({result_info['duration']:.1f}s / dự kiến {total_duration:.1f}s).")
        if final_path.exists() and not overwrite:
            raise FileExistsError(f"File đầu ra đã tồn tại: {final_path}")
        os.replace(part_path, final_path)
        progress_cb(100.0, f"Ghép xong {len(paths)} video • {result_info['width']}×{result_info['height']} • {result_info['fps']:.2f} FPS.")
        return str(final_path)
    finally:
        part_path.unlink(missing_ok=True)
        if list_path and Path(list_path).exists():
            Path(list_path).unlink(missing_ok=True)


class VideoToolApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_TITLE}  |  v{APP_VERSION}")
        self.root.geometry("1648x940")
        self.root.minsize(1280, 760)
        self.root.configure(bg="#06111B")
        # Give the image workspace more vertical room on desktop displays.
        if os.name == "nt":
            try:
                self.root.state("zoomed")
            except tk.TclError:
                pass
        icon_path = resource_path("assets/app_icon.ico")
        if icon_path.exists():
            try:
                self.root.iconbitmap(str(icon_path))
            except tk.TclError:
                pass

        self.video_path = ""
        self.output_dir = ""
        self.current_project_path = ""
        self._autosave_recovery_path = ""
        self._autosave_after_id = None
        self._autosave_periodic_id = None
        self.project_dirty = False
        self._loading_project = False
        self.scenes: list[dict] = []
        self.excluded_ranges: list[tuple[float, float]] = []
        self.selected_scene_index: Optional[int] = None
        self.scene_listbox = None
        self.project_name_var = tk.StringVar(value="Chưa có dự án")
        self.output_dir_custom = False
        self.logo_path = str(resource_path("assets/Goc_Review_Phim_Logo.png"))
        self.branding_config = default_branding_config()
        self.branding_studio_window = None
        self.info: Optional[dict] = None
        self.frame: Optional[np.ndarray] = None
        self.mask: Optional[np.ndarray] = None
        self.lasso_points: list[tuple[int, int]] = []
        self._mask_clip_cache_key = None
        self._mask_clip_cache = None
        self.canvas_points: list[tuple[float, float]] = []
        self.mask_tool_var = tk.StringVar(value="lasso")
        self.mask_brush_size_var = tk.IntVar(value=24)
        self._selection_start = None
        self._selection_current = None
        self._brush_last = None
        self._mask_redraw_after_id = None
        self.photo = None
        self.canvas_image_id = None
        self.canvas_w = 960
        # A lower requested height lets the fixed export bar remain visible on laptop screens.
        self.canvas_h = 400
        self.display_scale = 1.0
        self.display_offset = (0, 0)
        self.busy = False
        self.cancel_event: Optional[threading.Event] = None
        self.active_processes: set[subprocess.Popen] = set()
        self.process_lock = threading.Lock()
        self.merge_window = None
        self.merge_listbox = None
        self.merge_files: list[str] = []
        self.merge_name_var = tk.StringVar(value="GocReview_Ghep.mp4")
        self.merge_folder_var = tk.StringVar(value="")
        self.merge_status_var = tk.StringVar(value="Thêm ít nhất 2 video rồi sắp xếp thứ tự ghép.")
        self.merge_progress = None
        self.merge_stop_button = None
        self.merge_start_button = None
        self.batch_window = None
        self.batch_files = []
        self.batch_cancel_event = None
        self.batch_active = False
        self.batch_listbox = None
        self.batch_status_var = tk.StringVar(value="Chưa có video trong hàng đợi.")
        self.batch_progress = None
        # CTA image detector is intentionally separate from watermark/banner/logo detection.
        self.cta_window = None
        self.cta_candidates = []
        self.cta_selected_ids = set()
        self.cta_scan_running = False
        self.cta_close_after_scan = False
        self.cta_scan_cancel_event = None
        self.cta_scan_events = Queue()
        self.cta_preview_photo = None
        self.cta_preview_label = None
        self.cta_preview_caption = None
        self.cta_start_var = None
        self.cta_end_var = None
        self.cta_tree = None
        self.cta_progress = None
        self.cta_status_var = None
        self.cta_scan_button = None
        self.cta_stop_button = None
        self.cta_cut_button = None
        self.nav_buttons = []
        self.project_hub_window = None
        self.scene_strip_inner = None
        self.scene_strip_canvas = None
        self.scene_thumb_queue = Queue()
        self.scene_thumb_generation = 0
        self.scene_thumb_labels = {}
        self.scene_thumbnail_refs = {}
        self.scene_thumb_pending = 0
        self._scene_thumb_poll_id = None
        self.inspector_tab_buttons = []
        self.inspector_title_var = tk.StringVar(value="LÀM SẠCH")
        self.show_mask = tk.BooleanVar(value=True)
        self.preview_720_var = tk.BooleanVar(value=True)
        self.prefer_gpu_var = tk.BooleanVar(value=True)
        self.ffmpeg_path = configured_ffmpeg_path()
        self.ffmpeg_var = tk.StringVar(value=Path(self.ffmpeg_path).name if self.ffmpeg_path else "Tự động chọn FFmpeg")
        self.ffmpeg_status_var = tk.StringVar(value="NVENC sẽ được kiểm tra khi bắt đầu xuất")

        self.video_var = tk.StringVar(value="Chưa chọn video")
        self.output_dir_var = tk.StringVar(value="Thư mục xuất sẽ mặc định cùng thư mục video")
        self.logo_var = tk.StringVar(value=Path(self.logo_path).name if Path(self.logo_path).exists() else "Chưa chọn logo")
        # Flexible title badge: Tập / Phần / Chương / Mùa / custom free text.
        self.label_type_var = tk.StringVar(value="Tập")
        self.label_value_var = tk.StringVar(value="21")
        self.auto_open_output_var = tk.BooleanVar(value=True)
        self.frame_time_var = tk.StringVar(value="5")
        self.preview_seek_var = tk.DoubleVar(value=5.0)
        self.preview_seek_label_var = tk.StringVar(value="00:00:05")
        self._preview_seek_scale = None
        self._mask_undo_stack = []
        self._mask_redo_stack = []
        self._mask_undo_limit = 8
        self._brush_undo_pushed = False
        self.preview_start_var = tk.StringVar(value="300")
        self.preview_length_var = tk.StringVar(value="12")
        self.segment_start_var = tk.StringVar(value="0")
        self.segment_end_var = tk.StringVar(value="")
        self.segment_from_minutes_var = tk.StringVar(value="0")
        self.segment_to_minutes_var = tk.StringVar(value="0")
        self._syncing_minute_fields = False
        self.autosave_enabled_var = tk.BooleanVar(value=True)
        self.autosave_nav_var = tk.StringVar(value="Auto Save\nđang hoạt động")
        self.expansion_var = tk.IntVar(value=MASK_DEFAULT_EXPANSION)
        self.auto_mask_expansion_var = tk.BooleanVar(value=True)
        self._mask_expansion_override = False
        self._setting_auto_expansion = False
        self.status_var = tk.StringVar(value="Chọn video để bắt đầu.")
        self.meta_var = tk.StringVar(value="Chưa có thông tin video")

        self._style()
        self._build_ui()
        self._bind_project_dirty_tracking()
        self._on_mask_mode_changed()
        self._update_project_indicator()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._schedule_periodic_autosave()
        self.root.after(900, self._check_autosave_recovery)

    def _style(self):
        """Teal / midnight-blue Studio theme based on the user's approved reference."""
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # Approved palette: midnight navy, petrol blue, cyan/teal, soft berry accent.
        bg = "#06111B"
        shell = "#0A1B29"
        panel = "#102738"
        panel_alt = "#153546"
        panel_hover = "#1A4050"
        input_bg = "#0B202D"
        text = "#EAF4F5"
        muted = "#9BB6BF"
        dim = "#668793"
        cyan = "#31C1CB"
        cyan_hover = "#78D5D8"
        amber = "#E5B75D"
        danger = "#D65D68"
        danger_hover = "#E2767E"
        border = "#285365"
        track = "#174154"

        try:
            self.root.configure(bg=bg)
        except Exception:
            pass

        style.configure("TFrame", background=bg)
        style.configure("Panel.TFrame", background=panel)
        style.configure("Card.TFrame", background=panel_alt)
        style.configure("TLabel", background=bg, foreground=text, font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background=panel, foreground=text, font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=panel_alt, foreground=text, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=panel, foreground=muted, font=("Segoe UI", 9))
        style.configure("CardMuted.TLabel", background=panel_alt, foreground=muted, font=("Segoe UI", 9))
        style.configure("Brand.TLabel", background=bg, foreground=text, font=("Segoe UI", 23, "bold"))
        style.configure("Tagline.TLabel", background=bg, foreground=amber, font=("Segoe UI", 9, "bold"))
        style.configure("Version.TLabel", background=panel_alt, foreground=cyan_hover,
                        font=("Segoe UI", 9, "bold"), padding=(11, 6))
        style.configure("Section.TLabel", background=panel, foreground=cyan_hover,
                        font=("Segoe UI", 10, "bold"))
        style.configure("PanelTitle.TLabel", background=panel, foreground=text,
                        font=("Segoe UI", 12, "bold"))
        style.configure("Hint.TLabel", background=panel, foreground=muted, font=("Segoe UI", 9))
        style.configure("Status.TLabel", background=panel, foreground=cyan_hover,
                        font=("Segoe UI", 9, "bold"))
        style.configure("Warning.TLabel", background=bg, foreground=amber, font=("Segoe UI", 9))

        style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=(12, 10),
                        background=panel_hover, foreground=text, borderwidth=0,
                        focusthickness=2, focuscolor=cyan)
        style.map("TButton",
                  background=[("disabled", "#203744"), ("pressed", panel_alt), ("active", "#247E9A")],
                  foreground=[("disabled", "#66818A")])
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 12),
                        background=cyan, foreground="#031119", borderwidth=0)
        style.map("Primary.TButton",
                  background=[("disabled", "#285B62"), ("pressed", "#247E9A"), ("active", cyan_hover)],
                  foreground=[("disabled", "#8AAEB0")])
        style.configure("Secondary.TButton", font=("Segoe UI", 9, "bold"), padding=(12, 10),
                        background=panel_hover, foreground=text, borderwidth=0)
        style.map("Secondary.TButton",
                  background=[("disabled", "#203744"), ("pressed", panel_alt), ("active", "#247E9A")])
        style.configure("Danger.TButton", font=("Segoe UI", 9, "bold"), padding=(12, 10),
                        background=danger, foreground="#FFFFFF", borderwidth=0)
        style.map("Danger.TButton",
                  background=[("disabled", "#68424C"), ("pressed", "#B94F5D"), ("active", danger_hover)],
                  foreground=[("disabled", "#BCA1A5")])

        style.configure("TEntry", fieldbackground=input_bg, foreground=text,
                        insertcolor="#FFFFFF", padding=(8, 7), bordercolor=border,
                        lightcolor=border, darkcolor=border)
        style.map("TEntry", fieldbackground=[("disabled", panel_alt)],
                  foreground=[("disabled", dim)])
        style.configure("TSpinbox", fieldbackground=input_bg, foreground=text,
                        arrowsize=13, padding=(6, 6))
        style.configure("TCheckbutton", background=panel, foreground=text, font=("Segoe UI", 9))
        style.map("TCheckbutton", background=[("active", panel)],
                  foreground=[("disabled", muted)])
        style.configure("Horizontal.TScale", background=panel, troughcolor=track)
        style.configure("Horizontal.TProgressbar", troughcolor=panel_alt,
                        background=cyan, bordercolor=panel_alt, lightcolor=cyan,
                        darkcolor=cyan, thickness=10)
        style.configure("Vertical.TScrollbar", background=panel_hover,
                        troughcolor=panel, bordercolor=panel, arrowcolor=cyan_hover)
        style.configure("TCombobox", fieldbackground=input_bg, background=panel_alt,
                        foreground=text, arrowcolor=cyan_hover, padding=(7, 6),
                        bordercolor=border, lightcolor=border, darkcolor=border)
        style.map("TCombobox", fieldbackground=[("readonly", input_bg), ("disabled", panel_alt)],
                  foreground=[("readonly", text), ("disabled", muted)],
                  selectbackground=[("readonly", panel_hover)], selectforeground=[("readonly", text)])
        style.configure("Treeview", background=input_bg, fieldbackground=input_bg,
                        foreground=text, rowheight=28, borderwidth=0, font=("Segoe UI", 9))
        style.map("Treeview", background=[("selected", "#1B6577")],
                  foreground=[("selected", "#FFFFFF")])
        style.configure("Treeview.Heading", background=panel_alt, foreground=cyan_hover,
                        font=("Segoe UI", 9, "bold"), relief="flat", padding=(7, 7))
        style.map("Treeview.Heading", background=[("active", panel_hover)],
                  foreground=[("active", text)])
        style.configure("TNotebook", background=bg, borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background=panel_alt, foreground=muted,
                        padding=(12, 10), font=("Segoe UI", 9, "bold"), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", panel_hover), ("active", "#247E9A")],
                  foreground=[("selected", cyan_hover), ("active", text)])
        # A hidden notebook is driven by the lightweight workflow navigation.
        try:
            style.layout("Hidden.TNotebook", [("Notebook.client", {"sticky": "nswe"})])
            style.layout("Hidden.TNotebook.Tab", [])
        except Exception:
            pass
        style.configure("Nav.TButton", font=("Segoe UI", 10, "bold"), padding=(10, 13),
                        background=panel, foreground=muted, borderwidth=0, anchor="w")
        style.map("Nav.TButton", background=[("active", panel_hover)],
                  foreground=[("active", text)])
        style.configure("NavActive.TButton", font=("Segoe UI", 10, "bold"), padding=(10, 13),
                        background=panel_hover, foreground=cyan_hover, borderwidth=0, anchor="w")
        style.map("NavActive.TButton", background=[("active", panel_hover)],
                  foreground=[("active", cyan_hover)])
        style.configure("WorkspaceTab.TButton", font=("Segoe UI", 10, "bold"), padding=(10, 11),
                        background=panel_alt, foreground=muted, borderwidth=0)
        style.map("WorkspaceTab.TButton", background=[("active", panel_hover)],
                  foreground=[("active", text)])
        style.configure("WorkspaceActive.TButton", font=("Segoe UI", 10, "bold"), padding=(10, 11),
                        background="#1B6577", foreground="#FFFFFF", borderwidth=0)
        style.map("WorkspaceActive.TButton", background=[("active", "#247E9A")],
                  foreground=[("active", "#FFFFFF")])
        style.configure("SceneAction.TButton", font=("Segoe UI", 9, "bold"), padding=(7, 8),
                        background=panel_hover, foreground=text, borderwidth=0)
        style.map("SceneAction.TButton", background=[("active", "#247E9A"), ("pressed", panel_alt)])
        style.configure("TSeparator", background=border)

    def _build_ui(self):
        """Reference-first Studio layout: app bar, project/action bar, left navigation,
        wide preview, contextual inspector, scene filmstrip, range timeline and status.
        Heavy media operations remain in their existing worker threads.
        """
        outer = ttk.Frame(self.root, padding=(8, 7, 8, 7))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        # ── App bar: brand + autosave + settings ─────────────────────────────
        header = ttk.Frame(outer, style="Panel.TFrame", padding=(13, 5))
        header.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        header.columnconfigure(1, weight=1)
        brand = ttk.Frame(header, style="Panel.TFrame")
        brand.grid(row=0, column=0, sticky="w")
        self.brand_mark = tk.Canvas(brand, width=40, height=40, bg="#102738",
                                    highlightthickness=0, bd=0)
        self.brand_mark.pack(side="left", padx=(0, 10))
        self.brand_mark.create_rectangle(4, 8, 36, 36, fill="#155568", outline="#31C1CB", width=1)
        self.brand_mark.create_rectangle(4, 4, 36, 13, fill="#31C1CB", outline="#31C1CB")
        for x in (10, 20, 30):
            self.brand_mark.create_line(x, 5, x + 4, 12, fill="#EAF4F5", width=1)
        self.brand_mark.create_line(12, 21, 28, 21, fill="#EAF4F5", width=2)
        self.brand_mark.create_line(12, 27, 25, 27, fill="#78D5D8", width=2)
        brand_text = ttk.Frame(brand, style="Panel.TFrame")
        brand_text.pack(side="left", fill="y")
        ttk.Label(brand_text, text="Góc Review Phim Video Studio",
                  font=("Segoe UI", 16, "bold"), style="Panel.TLabel").pack(anchor="w")
        ttk.Label(brand_text, text="VIDEO STUDIO  ·  MASK  /  BRANDING  /  EXPORT",
                  style="Muted.TLabel").pack(anchor="w")

        header_right = ttk.Frame(header, style="Panel.TFrame")
        header_right.grid(row=0, column=2, sticky="e")
        self.header_save_state = tk.StringVar(value="✓  Đã lưu")
        ttk.Label(header_right, textvariable=self.header_save_state,
                  style="Status.TLabel").pack(side="left", padx=(0, 14))
        ttk.Button(header_right, text="Cài đặt", width=7,
                   command=lambda: self._select_workspace_tab(3),
                   style="Secondary.TButton").pack(side="left", padx=(0, 7))
        ttk.Label(header_right, text=f"v{APP_VERSION}", style="Version.TLabel").pack(side="left")

        # ── Project/action bar: this replaces the old oversized source toolbar ─
        project_bar = ttk.Frame(outer, style="Panel.TFrame", padding=(13, 8))
        project_bar.grid(row=1, column=0, sticky="ew", pady=(0, 7))
        project_bar.columnconfigure(1, weight=1)
        ttk.Label(project_bar, text="DỰ ÁN", font=("Segoe UI", 10, "bold"),
                  style="Panel.TLabel").grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 10))
        ttk.Label(project_bar, textvariable=self.project_name_var,
                  font=("Segoe UI", 13, "bold"), style="Panel.TLabel").grid(
            row=0, column=1, sticky="w")
        ttk.Label(project_bar, textvariable=self.video_var, style="Muted.TLabel",
                  wraplength=560).grid(row=1, column=1, sticky="w", pady=(2, 0))
        actions = ttk.Frame(project_bar, style="Panel.TFrame")
        actions.grid(row=0, column=2, rowspan=2, sticky="e")
        ttk.Button(actions, text="HOÀN TÁC", width=10, command=self.undo_mask,
                   style="Secondary.TButton").pack(side="left", padx=2)
        ttk.Button(actions, text="LÀM LẠI", width=9, command=self.redo_mask,
                   style="Secondary.TButton").pack(side="left", padx=2)
        ttk.Separator(actions, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(actions, text="LƯU  Ctrl+S", command=self.save_project,
                   style="Primary.TButton").pack(side="left", padx=2)
        ttk.Button(actions, text="LƯU THÀNH", command=lambda: self.save_project(save_as=True),
                   style="Secondary.TButton").pack(side="left", padx=2)
        ttk.Button(actions, text="XUẤT VIDEO", command=self.export_full,
                   style="Primary.TButton").pack(side="left", padx=(6, 0))

        # ── Main three-column workspace ──────────────────────────────────────
        body = ttk.Frame(outer, style="TFrame")
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=0, minsize=258)
        body.columnconfigure(1, weight=1, minsize=570)
        body.columnconfigure(2, weight=0, minsize=420)
        body.rowconfigure(0, weight=1)

        # Left rail + project snapshot.
        nav = ttk.Frame(body, style="Panel.TFrame", padding=(10, 12))
        nav.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        nav.columnconfigure(0, weight=1)
        ttk.Label(nav, text="ĐIỀU HƯỚNG", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", padx=7, pady=(3, 8))
        nav_specs = [
            ("⌂   Trang chủ", "home"),
            ("▣   Project", "project"),
            ("▰   Edit", "edit"),
            ("✧   Branding", "branding"),
            ("⇧   Export", "export"),
        ]
        for nav_index, (label, action) in enumerate(nav_specs):
            callback = {
                "home": self.open_project_hub,
                "project": self.open_project_from_nav,
                "edit": lambda: self._select_workspace_tab(0, nav_index=2),
                "branding": lambda: self._select_workspace_tab(1, nav_index=3),
                "export": lambda: self._select_workspace_tab(2, nav_index=4),
            }[action]
            button = ttk.Button(nav, text=label,
                                style="NavActive.TButton" if action == "edit" else "Nav.TButton",
                                command=callback)
            button.grid(row=nav_index + 1, column=0, sticky="ew", pady=4)
            self.nav_buttons.append(button)
        ttk.Separator(nav).grid(row=6, column=0, sticky="ew", pady=13)
        ttk.Label(nav, text="DỰ ÁN HIỆN TẠI", style="Muted.TLabel").grid(
            row=7, column=0, sticky="w", padx=7)
        project_card = ttk.Frame(nav, style="Card.TFrame", padding=(11, 11))
        project_card.grid(row=8, column=0, sticky="ew", padx=1, pady=(7, 8))
        ttk.Label(project_card, textvariable=self.project_name_var, style="Card.TLabel",
                  wraplength=190, justify="left", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(project_card, textvariable=self.video_var, style="CardMuted.TLabel",
                  wraplength=190, justify="left").pack(anchor="w", pady=(7, 0))
        ttk.Label(project_card, textvariable=self.meta_var, style="CardMuted.TLabel",
                  wraplength=190, justify="left").pack(anchor="w", pady=(7, 0))
        ttk.Label(nav, text="Ctrl + S  ·  Lưu nhanh", style="Muted.TLabel").grid(
            row=9, column=0, sticky="w", padx=7, pady=(4, 2))
        ttk.Label(nav, textvariable=self.autosave_nav_var, style="Muted.TLabel",
                  justify="left", wraplength=205).grid(row=10, column=0, sticky="w", padx=7)
        nav.grid_rowconfigure(11, weight=1)

        # Center workspace: tool tabs, large preview, real seek controls, scene cards.
        preview_panel = ttk.Frame(body, style="Panel.TFrame", padding=(9, 8))
        preview_panel.grid(row=0, column=1, sticky="nsew", padx=(0, 7))
        preview_panel.columnconfigure(0, weight=1)
        preview_panel.rowconfigure(1, weight=1, minsize=250)

        workspace_tabs = ttk.Frame(preview_panel, style="Panel.TFrame")
        workspace_tabs.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        for col in range(4):
            workspace_tabs.columnconfigure(col, weight=1)
        tab_specs = [
            ("▣  Video", self.choose_video),
            ("✣  Phân cảnh", self._focus_scene_strip),
            ("▢  Watermark", lambda: self._select_workspace_tab(0, nav_index=2)),
            ("◫  Xem so sánh", self.open_mask_comparison),
        ]
        self.workspace_tab_buttons = []
        for col, (label, command) in enumerate(tab_specs):
            b = ttk.Button(workspace_tabs, text=label, command=command,
                           style="WorkspaceActive.TButton" if col == 0 else "WorkspaceTab.TButton")
            b.grid(row=0, column=col, sticky="ew", padx=2)
            self.workspace_tab_buttons.append(b)

        self.canvas = tk.Canvas(preview_panel, width=900, height=380,
                                bg="#030A10", highlightthickness=1,
                                highlightbackground="#285365", cursor="crosshair")
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<ButtonPress-1>", self._lasso_start)
        self.canvas.bind("<B1-Motion>", self._lasso_move)
        self.canvas.bind("<ButtonRelease-1>", self._lasso_end)
        self.canvas.bind("<Configure>", lambda _e: self.refresh_canvas())

        # Preview seek bar and controls; seeking decodes only after mouse release.
        preview_controls = ttk.Frame(preview_panel, style="Panel.TFrame", padding=(5, 5))
        preview_controls.grid(row=2, column=0, sticky="ew")
        preview_controls.columnconfigure(1, weight=1)
        ttk.Label(preview_controls, text="THỜI ĐIỂM", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        self._preview_seek_scale = ttk.Scale(preview_controls, from_=0, to=1,
            variable=self.preview_seek_var, command=self._on_preview_seek_changed)
        self._preview_seek_scale.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self._preview_seek_scale.bind("<ButtonRelease-1>", self._preview_seek_released)
        ttk.Label(preview_controls, textvariable=self.preview_seek_label_var,
                  style="Status.TLabel", width=12, anchor="center").grid(row=0, column=2, sticky="e")
        control_row = ttk.Frame(preview_controls, style="Panel.TFrame")
        control_row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        control_row.columnconfigure(7, weight=1)
        ttk.Label(control_row, text="Tại (s)", style="Muted.TLabel").pack(side="left", padx=(0, 5))
        ttk.Entry(control_row, textvariable=self.frame_time_var, width=8).pack(side="left")
        ttk.Button(control_row, text="Lùi 1 giây", command=lambda: self._step_preview_sample(-1),
                   style="Secondary.TButton").pack(side="left", padx=(6, 3))
        ttk.Button(control_row, text="TẢI KHUNG", command=self.load_frame,
                   style="Secondary.TButton").pack(side="left", padx=3)
        ttk.Button(control_row, text="Tiến 1 giây", command=lambda: self._step_preview_sample(1),
                   style="Secondary.TButton").pack(side="left", padx=3)
        ttk.Button(control_row, text="CHỤP ẢNH", command=self.capture_preview_frame,
                   style="Secondary.TButton").pack(side="right", padx=3)
        ttk.Button(control_row, text="FIT", command=self.refresh_canvas,
                   style="Secondary.TButton").pack(side="right", padx=3)

        # Scene filmstrip with actual thumbnails, selected scene outlined in cyan.
        scene_strip = ttk.Frame(preview_panel, style="Card.TFrame", padding=(9, 7))
        scene_strip.grid(row=3, column=0, sticky="ew", pady=(3, 5))
        scene_strip.columnconfigure(0, weight=1)
        scene_head = ttk.Frame(scene_strip, style="Card.TFrame")
        scene_head.grid(row=0, column=0, sticky="ew")
        self.scene_count_var = tk.StringVar(value="PHÂN CẢNH (0)")
        ttk.Label(scene_head, textvariable=self.scene_count_var, style="Section.TLabel").pack(side="left")
        scene_actions_top = ttk.Frame(scene_head, style="Card.TFrame")
        scene_actions_top.pack(side="right")
        ttk.Button(scene_actions_top, text="THÊM PHÂN CẢNH", command=self.add_scene,
                   style="SceneAction.TButton").pack(side="left", padx=2)
        ttk.Button(scene_actions_top, text="TÁCH", command=self.split_selected_scene,
                   style="SceneAction.TButton").pack(side="left", padx=2)
        ttk.Button(scene_actions_top, text="XÓA", command=self.delete_selected_scene,
                   style="SceneAction.TButton").pack(side="left", padx=2)

        strip_wrap = ttk.Frame(scene_strip, style="Card.TFrame")
        strip_wrap.grid(row=1, column=0, sticky="ew", pady=(7, 0))
        strip_wrap.columnconfigure(0, weight=1)
        self.scene_strip_canvas = tk.Canvas(strip_wrap, height=112, bg="#102738",
                                           highlightthickness=0, borderwidth=0)
        self.scene_strip_scrollbar = ttk.Scrollbar(strip_wrap, orient="horizontal", command=self.scene_strip_canvas.xview)
        self.scene_strip_canvas.configure(xscrollcommand=self.scene_strip_scrollbar.set)
        self.scene_strip_canvas.grid(row=0, column=0, sticky="ew")
        self.scene_strip_scrollbar.grid(row=1, column=0, sticky="ew")
        self.scene_strip_inner = ttk.Frame(self.scene_strip_canvas, style="Card.TFrame")
        self.scene_strip_window = self.scene_strip_canvas.create_window(
            (0, 0), window=self.scene_strip_inner, anchor="nw")
        self.scene_strip_inner.bind("<Configure>", lambda _e: self._sync_scene_strip_scrollbar())
        self.scene_strip_canvas.bind("<Configure>", lambda _e: self._sync_scene_strip_scrollbar())
        self.scene_strip_canvas.bind("<MouseWheel>", lambda e: self.scene_strip_canvas.xview_scroll(
            -1 if e.delta > 0 else 1, "units"))

        # The In/Out range timeline lives in Export Center to preserve more vertical room for Preview.

        # Right inspector: Watermark / Effects / Advanced.
        inspector = ttk.Frame(body, style="Panel.TFrame", padding=(10, 9))
        inspector.grid(row=0, column=2, sticky="nsew")
        inspector.rowconfigure(2, weight=1)
        inspector.columnconfigure(0, weight=1)
        ttk.Label(inspector, textvariable=self.inspector_title_var,
                  style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w", padx=5, pady=(3, 8))
        inspector_tabs = ttk.Frame(inspector, style="Panel.TFrame")
        inspector_tabs.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        for col in range(3):
            inspector_tabs.columnconfigure(col, weight=1)
        for col, (label, index) in enumerate((("WATERMARK", 0), ("HIỆU ỨNG", 1), ("NÂNG CAO", 3))):
            b = ttk.Button(inspector_tabs, text=label,
                           command=lambda i=index: self._select_workspace_tab(i),
                           style="WorkspaceActive.TButton" if col == 0 else "WorkspaceTab.TButton")
            b.grid(row=0, column=col, sticky="ew", padx=2)
            self.inspector_tab_buttons.append(b)

        self.settings_notebook = ttk.Notebook(inspector, style="Hidden.TNotebook")
        self.settings_notebook.grid(row=2, column=0, sticky="nsew")
        self.settings_scroll_canvases = []

        def add_scroll_tab(title: str):
            tab = ttk.Frame(self.settings_notebook, style="Panel.TFrame")
            tab.columnconfigure(0, weight=1)
            tab.rowconfigure(0, weight=1)
            c = tk.Canvas(tab, bg="#102738", highlightthickness=0, borderwidth=0)
            sb = ttk.Scrollbar(tab, orient="vertical", command=c.yview)
            c.configure(yscrollcommand=sb.set)
            c.grid(row=0, column=0, sticky="nsew")
            sb.grid(row=0, column=1, sticky="ns")
            inner = ttk.Frame(c, style="Panel.TFrame", padding=(13, 12))
            window_id = c.create_window((0, 0), window=inner, anchor="nw")
            inner.bind("<Configure>", lambda _e, canvas=c: canvas.configure(scrollregion=canvas.bbox("all")))
            c.bind("<Configure>", lambda e, canvas=c, item=window_id: canvas.itemconfigure(item, width=e.width))
            c.bind("<MouseWheel>", lambda e, canvas=c: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
            self.settings_scroll_canvases.append(c)
            self.settings_notebook.add(tab, text=title)
            return inner, c

        # WATERMARK inspector.
        mask_tab, mask_scroll = add_scroll_tab("WATERMARK")
        ttk.Label(mask_tab, text="KHUNG HÌNH MẪU", style="Section.TLabel").pack(anchor="w")
        ttk.Label(mask_tab, text="Thời điểm lấy ảnh (giây)", style="Panel.TLabel").pack(anchor="w", pady=(8, 0))
        frame_row = ttk.Frame(mask_tab, style="Panel.TFrame")
        frame_row.pack(fill="x", pady=(4, 0))
        ttk.Entry(frame_row, textvariable=self.frame_time_var, width=9).pack(side="left", ipady=2)
        ttk.Button(frame_row, text="Tải ảnh", command=self.load_frame, style="Secondary.TButton").pack(side="left", padx=(7, 0))
        ttk.Separator(mask_tab).pack(fill="x", pady=10)
        ttk.Label(mask_tab, text="CÔNG CỤ CHỌN VÙNG", style="Section.TLabel").pack(anchor="w")
        select_tools = ttk.Frame(mask_tab, style="Panel.TFrame")
        select_tools.pack(fill="x", pady=(6, 3))
        select_tools.columnconfigure((0, 1), weight=1)
        ttk.Button(select_tools, text="Khoanh tự do", command=lambda: self.set_mask_tool("lasso"),
                   style="Secondary.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=3)
        ttk.Button(select_tools, text="Khung chữ nhật", command=lambda: self.set_mask_tool("rect"),
                   style="Secondary.TButton").grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=3)
        edit_tools = ttk.Frame(mask_tab, style="Panel.TFrame")
        edit_tools.pack(fill="x", pady=(2, 3))
        edit_tools.columnconfigure((0, 1), weight=1)
        ttk.Button(edit_tools, text="Bút thêm", command=lambda: self.set_mask_tool("brush"),
                   style="Secondary.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=3)
        ttk.Button(edit_tools, text="Tẩy mask", command=lambda: self.set_mask_tool("erase"),
                   style="Secondary.TButton").grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=3)
        ttk.Label(mask_tab, text="Kích thước bút", style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Scale(mask_tab, from_=4, to=100, variable=self.mask_brush_size_var).pack(fill="x", pady=(2, 4))
        ttk.Separator(mask_tab).pack(fill="x", pady=9)
        ttk.Label(mask_tab, text="TẠO / KIỂM TRA MASK", style="Section.TLabel").pack(anchor="w")
        ttk.Button(mask_tab, text="TỰ TÁCH NÉT (NẾU PHÙ HỢP)", command=self.auto_extract,
                   style="Primary.TButton").pack(fill="x", pady=(6, 4))
        ttk.Button(mask_tab, text="DÙNG TOÀN BỘ VÙNG KHOANH", command=self.use_selection_mask,
                   style="Secondary.TButton").pack(fill="x", pady=3)
        ttk.Button(mask_tab, text="XÓA VÙNG KHOANH / MASK", command=self.clear_selection,
                   style="Secondary.TButton").pack(fill="x", pady=3)
        ttk.Checkbutton(mask_tab, text="Hiện mask màu đỏ", variable=self.show_mask,
                        command=self.refresh_canvas).pack(anchor="w", pady=(5, 0))
        ttk.Checkbutton(mask_tab, text="Smart Mask – tự ước lượng PX",
                        variable=self.auto_mask_expansion_var,
                        command=self._on_mask_mode_changed).pack(anchor="w", pady=(6, 3))
        ttk.Label(mask_tab, text="Nới mép mask (PX)", style="Panel.TLabel").pack(anchor="w")
        self.expansion_scale = ttk.Scale(mask_tab, from_=0, to=30, variable=self.expansion_var,
                                         command=lambda _v: self._on_expansion_manual_changed())
        self.expansion_scale.pack(fill="x", pady=(4, 0))
        self.expansion_label = ttk.Label(mask_tab, text=f"Tự động · mặc định {MASK_DEFAULT_EXPANSION} px (có thể chỉnh tay)",
                                         style="Muted.TLabel")
        self.expansion_label.pack(anchor="e")
        ttk.Label(mask_tab, text="Banner/logo có nền: dùng toàn bộ vùng khoanh rồi tinh chỉnh bằng Bút/Tẩy.",
                  style="Muted.TLabel", wraplength=320).pack(anchor="w", pady=(5, 8))
        ttk.Separator(mask_tab).pack(fill="x", pady=8)
        ttk.Label(mask_tab, text="WATERMARK THEO PHÂN CẢNH", style="Section.TLabel").pack(anchor="w")
        ttk.Label(mask_tab, text="Chọn cảnh ở dải thumbnail phía dưới Preview. Mỗi khoảng có mask riêng.",
                  style="Muted.TLabel", wraplength=320).pack(anchor="w", pady=(4, 6))
        # Kept as a non-displayed Listbox for backwards-compatible selection logic.
        self.scene_listbox = tk.Listbox(mask_tab, height=4, exportselection=False)
        scene_actions = ttk.Frame(mask_tab, style="Panel.TFrame")
        scene_actions.pack(fill="x", pady=(3, 0))
        scene_actions.columnconfigure((0, 1), weight=1)
        ttk.Button(scene_actions, text="NẠP CẢNH", command=self.load_selected_scene,
                   style="Secondary.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(scene_actions, text="CẬP NHẬT CẢNH", command=self.update_selected_scene,
                   style="Secondary.TButton").grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ttk.Button(mask_tab, text="XÓA CẢNH ĐÃ CHỌN", command=self.delete_selected_scene,
                   style="Secondary.TButton").pack(fill="x", pady=(5, 0))
        self.controls_canvas = mask_scroll
        self.controls_window = None

        # BRANDING inspector.
        branding_tab, _branding_scroll = add_scroll_tab("BRANDING")
        ttk.Label(branding_tab, text="NHẬN DIỆN VIDEO", style="Section.TLabel").pack(anchor="w")
        ttk.Label(branding_tab, text="Logo đang dùng", style="Panel.TLabel").pack(anchor="w", pady=(8, 3))
        ttk.Label(branding_tab, textvariable=self.logo_var, style="Muted.TLabel", wraplength=320).pack(anchor="w")
        ttk.Button(branding_tab, text="CHỌN LOGO KHÁC", command=self.choose_logo,
                   style="Secondary.TButton").pack(fill="x", pady=(6, 0))
        ttk.Button(branding_tab, text="MỞ STUDIO BRANDING", command=self.open_branding_studio,
                   style="Primary.TButton").pack(fill="x", pady=(6, 0))
        ttk.Separator(branding_tab).pack(fill="x", pady=11)
        ttk.Label(branding_tab, text="NHÃN TẬP / PHẦN", style="Section.TLabel").pack(anchor="w")
        ttk.Label(branding_tab, text="Loại nhãn", style="Panel.TLabel").pack(anchor="w", pady=(7, 0))
        self.label_type_combo = ttk.Combobox(branding_tab, textvariable=self.label_type_var,
            values=("Tập", "Phần", "Chương", "Mùa", "Tùy chỉnh"), state="readonly")
        self.label_type_combo.pack(fill="x", pady=(4, 0))
        ttk.Label(branding_tab, text="Số thứ tự / nội dung", style="Panel.TLabel").pack(anchor="w", pady=(7, 0))
        ttk.Entry(branding_tab, textvariable=self.label_value_var).pack(fill="x", pady=(4, 0), ipady=3)
        ttk.Label(branding_tab, text="Có thể dùng Preset Branding để tái sử dụng giữa nhiều dự án.",
                  style="Muted.TLabel", wraplength=320).pack(anchor="w", pady=(7, 0))

        # EXPORT inspector.
        export_tab, _export_scroll = add_scroll_tab("XUẤT VIDEO")
        ttk.Label(export_tab, text="THƯ MỤC ĐẦU RA", style="Section.TLabel").pack(anchor="w")
        ttk.Label(export_tab, textvariable=self.output_dir_var, style="Muted.TLabel", wraplength=320).pack(anchor="w", pady=(5, 2))
        ttk.Button(export_tab, text="CHỌN THƯ MỤC XUẤT", command=self.choose_output_dir, style="Secondary.TButton").pack(fill="x", pady=(2, 8))
        ttk.Separator(export_tab).pack(fill="x", pady=7)
        ttk.Label(export_tab, text="XEM THỬ", style="Section.TLabel").pack(anchor="w")
        ttk.Label(export_tab, text="Bắt đầu tại (giây)", style="Panel.TLabel").pack(anchor="w", pady=(8, 0))
        ttk.Entry(export_tab, textvariable=self.preview_start_var).pack(fill="x", pady=(4, 0), ipady=3)
        ttk.Label(export_tab, text="Độ dài xem thử (10–60 giây)", style="Panel.TLabel").pack(anchor="w", pady=(7, 0))
        ttk.Spinbox(export_tab, from_=PREVIEW_MIN, to=PREVIEW_MAX,
                    textvariable=self.preview_length_var, width=10).pack(anchor="w", pady=(4, 0), ipady=2)
        ttk.Separator(export_tab).pack(fill="x", pady=10)
        ttk.Label(export_tab, text="CHỌN ĐOẠN THEO PHÚT", style="Section.TLabel").pack(anchor="w")
        minute_row = ttk.Frame(export_tab, style="Panel.TFrame")
        minute_row.pack(fill="x", pady=(7, 0))
        minute_row.columnconfigure((0, 1), weight=1)
        minute_left = ttk.Frame(minute_row, style="Panel.TFrame")
        minute_left.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        minute_right = ttk.Frame(minute_row, style="Panel.TFrame")
        minute_right.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        ttk.Label(minute_left, text="TỪ PHÚT", style="Muted.TLabel").pack(anchor="w")
        ttk.Entry(minute_left, textvariable=self.segment_from_minutes_var).pack(fill="x", pady=(4, 0), ipady=3)
        ttk.Label(minute_right, text="ĐẾN PHÚT", style="Muted.TLabel").pack(anchor="w")
        ttk.Entry(minute_right, textvariable=self.segment_to_minutes_var).pack(fill="x", pady=(4, 0), ipady=3)
        ttk.Button(export_tab, text="ÁP DỤNG MỐC PHÚT VÀO TIMELINE", command=self.apply_minute_range,
                   style="Primary.TButton").pack(fill="x", pady=(7, 0))
        ttk.Label(export_tab, text="Có thể nhập số thập phân, ví dụ 2.5 phút.",
                  style="Muted.TLabel", wraplength=320).pack(anchor="w", pady=(5, 0))
        ttk.Separator(export_tab).pack(fill="x", pady=10)
        ttk.Label(export_tab, text="VÙNG XUẤT · IN / OUT", style="Section.TLabel").pack(anchor="w")
        self.timeline_canvas = tk.Canvas(export_tab, height=58, bg="#0B202D",
                                         highlightthickness=0, cursor="hand2")
        self.timeline_canvas.pack(fill="x", pady=(6, 2))
        self.timeline_canvas.bind("<Configure>", lambda _e: self._timeline_redraw())
        self.timeline_canvas.bind("<Button-1>", self._timeline_press)
        self.timeline_canvas.bind("<B1-Motion>", self._timeline_drag)
        self.timeline_canvas.bind("<ButtonRelease-1>", self._timeline_release)
        self.timeline_info_var = tk.StringVar(value="Chọn video để bật timeline.")
        ttk.Label(export_tab, textvariable=self.timeline_info_var, style="Muted.TLabel",
                  wraplength=320).pack(anchor="w", pady=(0, 3))
        ttk.Separator(export_tab).pack(fill="x", pady=10)
        segment_row = ttk.Frame(export_tab, style="Panel.TFrame")
        segment_row.pack(fill="x", pady=(2, 0))
        segment_row.columnconfigure((0, 1), weight=1)
        for col, (label, var) in enumerate((("TỪ GIÂY", self.segment_start_var), ("ĐẾN GIÂY", self.segment_end_var))):
            holder = ttk.Frame(segment_row, style="Panel.TFrame")
            holder.grid(row=0, column=col, sticky="ew", padx=(0, 5) if col == 0 else (5, 0))
            ttk.Label(holder, text=label, style="Muted.TLabel").pack(anchor="w")
            ttk.Entry(holder, textvariable=var).pack(fill="x", pady=(4, 0), ipady=3)
        ttk.Checkbutton(export_tab, text="Preview nhẹ 720p", variable=self.preview_720_var).pack(anchor="w", pady=(9, 0))
        ttk.Checkbutton(export_tab, text="Tự mở thư mục sau khi xuất", variable=self.auto_open_output_var).pack(anchor="w", pady=(5, 0))
        ttk.Separator(export_tab).pack(fill="x", pady=10)
        ttk.Label(export_tab, text="BẮT ĐẦU XUẤT", style="Section.TLabel").pack(anchor="w")
        ttk.Button(export_tab, text="XEM THỬ", command=self.export_preview,
                   style="Secondary.TButton").pack(fill="x", pady=(6, 3))
        ttk.Button(export_tab, text="XUẤT ĐOẠN", command=self.export_segment,
                   style="Secondary.TButton").pack(fill="x", pady=3)
        ttk.Button(export_tab, text="XUẤT TOÀN BỘ", command=self.export_full,
                   style="Primary.TButton").pack(fill="x", pady=3)
        self.stop_button = ttk.Button(export_tab, text="DỪNG XUẤT", command=self.stop_export,
                                      state="disabled", style="Danger.TButton")
        self.stop_button.pack(fill="x", pady=(3, 0))
        ttk.Separator(export_tab).pack(fill="x", pady=10)
        ttk.Button(export_tab, text="MỞ BATCH PROCESSING", command=self.open_batch_dialog,
                   style="Primary.TButton").pack(fill="x", pady=(3, 0))
        ttk.Button(export_tab, text="KIỂM TRA CHẤT LƯỢNG", command=self.open_quality_check_dialog,
                   style="Secondary.TButton").pack(fill="x", pady=(5, 0))

        # ADVANCED / system settings.
        system_tab, _system_scroll = add_scroll_tab("NÂNG CAO")
        ttk.Label(system_tab, text="FFMPEG & TĂNG TỐC", style="Section.TLabel").pack(anchor="w")
        ttk.Label(system_tab, textvariable=self.ffmpeg_status_var, style="Muted.TLabel",
                  wraplength=320).pack(anchor="w", pady=(7, 0))
        ttk.Button(system_tab, text="Chọn FFmpeg…", command=self.choose_ffmpeg,
                   style="Secondary.TButton").pack(fill="x", pady=(7, 4))
        ttk.Button(system_tab, text="Kiểm tra NVIDIA NVENC", command=self.check_gpu_status,
                   style="Primary.TButton").pack(fill="x")
        ttk.Button(system_tab, text="Đặt lại FFmpeg tự động", command=self.reset_ffmpeg,
                   style="Secondary.TButton").pack(fill="x", pady=(4, 0))
        ttk.Button(system_tab, text="Mở trang tải FFmpeg", command=self.open_ffmpeg_download_page,
                   style="Secondary.TButton").pack(fill="x", pady=(4, 0))
        ttk.Separator(system_tab).pack(fill="x", pady=10)
        ttk.Label(system_tab, text="HIỆU SUẤT & LƯU DỰ ÁN", style="Section.TLabel").pack(anchor="w")
        ttk.Checkbutton(system_tab, text="Ưu tiên NVIDIA NVENC nếu khả dụng",
                        variable=self.prefer_gpu_var).pack(anchor="w", pady=(7, 0))
        ttk.Checkbutton(system_tab, text="Tự động lưu dự án (5 giây sau khi chỉnh, tối đa mỗi 1 phút)",
                        variable=self.autosave_enabled_var,
                        command=self._on_autosave_setting_changed).pack(anchor="w", pady=(6, 0))
        ttk.Label(system_tab, text="Nếu chưa chọn nơi lưu, bản khôi phục được lưu riêng trên máy.",
                  style="Muted.TLabel", wraplength=320).pack(anchor="w", pady=(3, 0))
        ttk.Separator(system_tab).pack(fill="x", pady=10)
        ttk.Label(system_tab, text="CÔNG CỤ", style="Section.TLabel").pack(anchor="w")
        ttk.Button(system_tab, text="MỞ CÔNG CỤ GHÉP VIDEO", command=self.open_merge_dialog,
                   style="Primary.TButton").pack(fill="x", pady=(7, 0))

        self.settings_notebook.bind("<<NotebookTabChanged>>", self._on_settings_tab_changed)
        self.active_controls_canvas = mask_scroll

        # Persistent progress dock and technical status.
        footer = ttk.Frame(outer, style="Panel.TFrame", padding=(10, 5))
        footer.grid(row=3, column=0, sticky="ew", pady=(5, 3))
        footer.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(footer, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        ttk.Label(footer, textvariable=self.status_var, style="Status.TLabel").grid(
            row=0, column=1, sticky="e")

        status = ttk.Frame(outer, style="Panel.TFrame", padding=(10, 5))
        status.grid(row=4, column=0, sticky="ew")
        status.columnconfigure(1, weight=1)
        self.ready_status_var = tk.StringVar(value="●  Sẵn sàng")
        ttk.Label(status, textvariable=self.ready_status_var, style="Status.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 14))
        ttk.Label(status, textvariable=self.meta_var, style="Muted.TLabel").grid(
            row=0, column=1, sticky="w")
        gpu_text = "GPU (NVENC): Ưu tiên" if self.prefer_gpu_var.get() else "GPU: Tự động"
        self.gpu_status_label = ttk.Label(status, text=gpu_text, style="Status.TLabel")
        self.gpu_status_label.grid(row=0, column=2, sticky="e", padx=(8, 0))

        self.segment_start_var.trace_add("write", lambda *_: self._timeline_redraw())
        self.segment_end_var.trace_add("write", lambda *_: self._timeline_redraw())
        self.root.bind_all("<MouseWheel>", self._scroll_controls, add="+")
        self.root.bind_all("<Control-z>", self._undo_shortcut, add="+")
        self.root.bind_all("<Control-y>", self._redo_shortcut, add="+")
        self._timeline_redraw()
        self._draw_placeholder()
        self._refresh_scene_list()
        self.root.after_idle(lambda: self._restore_inspector_page(0))
        self.root.after(150, self._poll_scene_thumbnail_queue)

    def _present_engine_window(self, win):
        """Present a Tk dialog independently of the withdrawn bridge root.

        The Qt shell is the visible main window. Toplevels marked transient to the
        hidden Tk root may remain unmapped on Windows; these dialogs must be normal
        top-level windows and be explicitly deiconified/lifted.
        """
        try:
            if win is None or not win.winfo_exists():
                return
            win.deiconify()
            win.update_idletasks()
            win.lift()
            win.focus_force()
        except tk.TclError:
            pass

    def _restore_inspector_page(self, index: int):
        """Re-map the embedded scroll-form canvas after a hidden Notebook page is selected.

        Tk's themed Notebook may unmap a Canvas window child when its page is hidden;
        explicitly restoring its window geometry avoids blank Inspector panels after
        switching tabs, especially on smaller laptop windows.
        """
        try:
            if not (0 <= index < len(self.settings_scroll_canvases)):
                return
            canvas = self.settings_scroll_canvases[index]
            canvas.update_idletasks()
            children = canvas.winfo_children()
            if not children:
                return
            inner = children[0]
            items = canvas.find_all()
            if items:
                item = items[0]
                content_h = max(canvas.winfo_height(), inner.winfo_reqheight())
                canvas.itemconfigure(item, state="normal", width=max(1, canvas.winfo_width()),
                                     height=max(1, content_h))
                canvas.configure(scrollregion=(0, 0, max(1, canvas.winfo_width()), max(1, content_h)))
            inner.update_idletasks()
            canvas.update_idletasks()
        except (tk.TclError, IndexError):
            pass

    def _sync_scene_strip_scrollbar(self):
        """Keep the scene filmstrip clean: hide horizontal scroll unless cards overflow."""
        try:
            canvas = self.scene_strip_canvas
            inner = self.scene_strip_inner
            viewport = max(1, canvas.winfo_width())
            requested = max(1, inner.winfo_reqwidth())
            content_height = max(112, inner.winfo_reqheight())
            canvas.configure(scrollregion=(0, 0, max(viewport, requested), content_height))
            canvas.itemconfigure(self.scene_strip_window, width=max(viewport, requested), height=content_height)
            if requested <= viewport + 4:
                self.scene_strip_scrollbar.grid_remove()
                canvas.yview_moveto(0)
                canvas.xview_moveto(0)
            else:
                self.scene_strip_scrollbar.grid()
        except (tk.TclError, AttributeError):
            pass

    def _focus_scene_strip(self):
        """Bring focus to the scene strip and keep the selected scene visible."""
        try:
            self.scene_strip_canvas.focus_set()
            self.scene_strip_canvas.xview_moveto(0)
            self.status_var.set("Chọn một thumbnail để nạp mask và thời gian của phân cảnh.")
        except Exception:
            pass

    def _scene_card_select(self, index: int):
        if not (0 <= index < len(self.scenes)):
            return
        self._clear_mask_history()
        self.selected_scene_index = index
        try:
            if self.scene_listbox is not None:
                self.scene_listbox.selection_clear(0, tk.END)
                self.scene_listbox.selection_set(index)
                self.scene_listbox.activate(index)
        except Exception:
            pass
        self.load_selected_scene()
        self._refresh_scene_thumbnail_selection()

    def _refresh_scene_thumbnail_selection(self):
        selected = self.selected_scene_index
        for idx, frame in getattr(self, "scene_card_frames", {}).items():
            try:
                frame.configure(highlightbackground="#31C1CB" if idx == selected else "#285365",
                                highlightthickness=2 if idx == selected else 1)
            except tk.TclError:
                pass

    def _refresh_scene_thumbnail_strip(self):
        inner = getattr(self, "scene_strip_inner", None)
        if inner is None:
            return
        self.scene_thumb_generation += 1
        generation = self.scene_thumb_generation
        self.scene_thumb_labels = {}
        self.scene_thumbnail_refs = {}
        self.scene_card_frames = {}
        for child in inner.winfo_children():
            child.destroy()
        try:
            self.scene_count_var.set(f"PHÂN CẢNH ({len(self.scenes)})")
        except Exception:
            pass
        if not self.scenes:
            ttk.Label(inner, text="Chưa có phân cảnh. Khoanh watermark, chọn In/Out rồi nhấn + THÊM PHÂN CẢNH.",
                      style="CardMuted.TLabel", padding=(10, 25)).pack(side="left", anchor="w")
            return
        snapshot = [(float(scene.get("start", 0.0)), str(scene.get("name", f"Cảnh {i+1}")),
                     self._fmt_time(scene.get("start", 0)), self._fmt_time(scene.get("end", 0)))
                    for i, scene in enumerate(self.scenes)]
        self.scene_thumb_pending = len(snapshot)
        video_path = self.video_path
        for index, (start, name, start_text, end_text) in enumerate(snapshot):
            card = tk.Frame(inner, bg="#153546", highlightbackground="#31C1CB" if index == self.selected_scene_index else "#285365",
                            highlightthickness=2 if index == self.selected_scene_index else 1,
                            bd=0, padx=5, pady=5, cursor="hand2")
            card.pack(side="left", padx=(0, 7), pady=1)
            thumb = tk.Label(card, text="Đang tải ảnh…", bg="#0B202D", fg="#9BB6BF",
                             font=("Segoe UI", 8), bd=0, highlightthickness=1,
                             highlightbackground="#285365", anchor="center")
            thumb.pack(fill="x")
            title = tk.Label(card, text=f"{index+1:02d}  {name}", bg="#153546", fg="#EAF4F5",
                             anchor="w", font=("Segoe UI", 9, "bold"))
            title.pack(fill="x", pady=(4, 1))
            times = tk.Label(card, text=f"{start_text} – {end_text}", bg="#153546", fg="#9BB6BF",
                             anchor="w", font=("Segoe UI", 8))
            times.pack(fill="x")
            for widget in (card, thumb, title, times):
                widget.bind("<Button-1>", lambda _e, i=index: self._scene_card_select(i))
            self.scene_thumb_labels[index] = thumb
            self.scene_card_frames[index] = card
        self._refresh_scene_thumbnail_selection()
        try:
            self.root.after_idle(self._sync_scene_strip_scrollbar)
        except tk.TclError:
            pass
        if not video_path:
            return

        def worker():
            for index, item in enumerate(snapshot):
                if generation != self.scene_thumb_generation:
                    return
                try:
                    frame = read_frame(video_path, item[0])
                    h, w = frame.shape[:2]
                    scale = min(134 / max(1, w), 76 / max(1, h))
                    resized = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))),
                                         interpolation=cv2.INTER_AREA)
                    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
                    self.scene_thumb_queue.put((generation, index, rgb, None))
                except Exception as exc:
                    self.scene_thumb_queue.put((generation, index, None, str(exc)))
        threading.Thread(target=worker, name="SceneThumbnailWorker", daemon=True).start()

    def _poll_scene_thumbnail_queue(self):
        try:
            while True:
                generation, index, rgb, error = self.scene_thumb_queue.get_nowait()
                if generation != self.scene_thumb_generation:
                    continue
                label = self.scene_thumb_labels.get(index)
                if label is None or not label.winfo_exists():
                    continue
                if rgb is not None:
                    photo = ImageTk.PhotoImage(Image.fromarray(rgb))
                    label.configure(image=photo, text="")
                    self.scene_thumbnail_refs[index] = photo
                else:
                    label.configure(text="Không tải được ảnh")
                self.scene_thumb_pending = max(0, self.scene_thumb_pending - 1)
                self._sync_scene_strip_scrollbar()
        except Empty:
            pass
        try:
            if self.root.winfo_exists():
                self._scene_thumb_poll_id = self.root.after(140, self._poll_scene_thumbnail_queue)
        except tk.TclError:
            self._scene_thumb_poll_id = None

    def open_mask_comparison(self):
        """Show source frame beside the current red mask overlay for inspection."""
        if self.frame is None:
            messagebox.showinfo("Chưa có khung hình", "Hãy tải khung hình video trước.", parent=self.root)
            return
        win = tk.Toplevel(self.root)
        win.title("So sánh khung gốc và vùng mask")
        win.geometry("1100x520")
        win.configure(bg="#06111B")
        wrap = ttk.Frame(win, style="Panel.TFrame", padding=10)
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure((0, 1), weight=1)
        wrap.rowconfigure(1, weight=1)
        ttk.Label(wrap, text="KHUNG GỐC", style="Section.TLabel").grid(row=0, column=0, sticky="w", padx=5, pady=(0, 6))
        ttk.Label(wrap, text="LỚP MASK ĐANG CHỌN", style="Section.TLabel").grid(row=0, column=1, sticky="w", padx=5, pady=(0, 6))
        h, w = self.frame.shape[:2]
        max_w, max_h = 520, 420
        scale = min(max_w / max(1, w), max_h / max(1, h))
        size = (max(1, int(w * scale)), max(1, int(h * scale)))
        original = cv2.cvtColor(cv2.resize(self.frame, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
        overlay = self.frame.copy()
        if self.mask is not None and self.show_mask.get():
            tint = np.zeros_like(overlay); tint[:, :, 2] = 255
            alpha = (self.mask > 0)[:, :, None].astype(np.float32) * 0.42
            overlay = np.clip(overlay.astype(np.float32) * (1-alpha) + tint.astype(np.float32)*alpha, 0, 255).astype(np.uint8)
        overlay = cv2.cvtColor(cv2.resize(overlay, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
        self.compare_photos = [ImageTk.PhotoImage(Image.fromarray(original)), ImageTk.PhotoImage(Image.fromarray(overlay))]
        for col, photo in enumerate(self.compare_photos):
            panel = tk.Label(wrap, image=photo, bg="#030A10", bd=0)
            panel.grid(row=1, column=col, sticky="nsew", padx=5, pady=4)
        ttk.Label(wrap, text="So sánh này hiển thị mask màu đỏ, chưa phải kết quả inpaint sau xuất.",
                  style="Muted.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", padx=5, pady=(4, 0))
        self._present_engine_window(win)

    def _on_preview_seek_changed(self, value):
        try:
            self.preview_seek_label_var.set(self._fmt_time(float(value)))
        except Exception:
            pass

    def _preview_seek_released(self, _event=None):
        try:
            seconds = max(0.0, float(self.preview_seek_var.get()))
            self.frame_time_var.set(f"{seconds:.3f}".rstrip("0").rstrip("."))
            self.load_frame()
        except Exception as exc:
            self.status_var.set(f"Không tua được khung hình: {exc}")

    def _step_preview_sample(self, delta: float):
        if not self.video_path or not self.info:
            self.choose_video()
            return
        try:
            current = safe_float(self.frame_time_var.get(), "Thời điểm khung hình")
            duration = float(self.info.get("duration", 0.0))
            target = min(max(0.0, current + delta), max(0.0, duration - 0.05))
            self.frame_time_var.set(f"{target:.3f}".rstrip("0").rstrip("."))
            self.load_frame()
        except Exception as exc:
            self.status_var.set(str(exc))

    def capture_preview_frame(self):
        if self.frame is None:
            messagebox.showinfo("Chưa có khung hình", "Hãy tải một khung hình trước khi chụp.", parent=self.root)
            return
        default_name = "GocReview_Frame.png"
        try:
            if self.video_path:
                default_name = f"{Path(self.video_path).stem}_{self.frame_time_var.get().replace('.', '_')}s.png"
        except Exception:
            pass
        path = filedialog.asksaveasfilename(
            title="Lưu ảnh khung hình", initialfile=default_name,
            defaultextension=".png", filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg *.jpeg")])
        if not path:
            return
        try:
            rgb = cv2.cvtColor(self.frame, cv2.COLOR_BGR2RGB)
            Image.fromarray(rgb).save(path)
            self.status_var.set(f"Đã lưu ảnh khung hình: {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Không lưu được ảnh", str(exc), parent=self.root)

    # Lightweight Undo/Redo history for the current mask-editing context only.
    # History is cleared when switching video/frame/scene to avoid applying a mask
    # snapshot to a different source image.
    def _mask_snapshot(self):
        return {
            "mask": self.mask.copy() if self.mask is not None else None,
            "lasso_points": list(self.lasso_points),
            "canvas_points": list(self.canvas_points),
        }

    def _push_mask_undo(self):
        try:
            self._mask_undo_stack.append(self._mask_snapshot())
            if len(self._mask_undo_stack) > self._mask_undo_limit:
                self._mask_undo_stack.pop(0)
            self._mask_redo_stack.clear()
        except Exception:
            pass

    def _clear_mask_history(self):
        self._mask_undo_stack.clear()
        self._mask_redo_stack.clear()
        self._brush_undo_pushed = False

    def _restore_mask_snapshot(self, snapshot):
        current = snapshot.get("mask")
        self.mask = current.copy() if current is not None else None
        self.lasso_points = list(snapshot.get("lasso_points", []))
        self.canvas_points = list(snapshot.get("canvas_points", []))
        if self.frame is not None and self.lasso_points:
            scale, (ox, oy), _size = self._canvas_geometry()
            self.canvas_points = [(ox + x * scale, oy + y * scale) for x, y in self.lasso_points]
        self.refresh_canvas()
        self._mark_project_dirty()

    def undo_mask(self):
        if not self._mask_undo_stack:
            self.status_var.set("Không còn thao tác mask để hoàn tác.")
            return
        self._mask_redo_stack.append(self._mask_snapshot())
        snapshot = self._mask_undo_stack.pop()
        self._restore_mask_snapshot(snapshot)
        self.status_var.set("Đã hoàn tác chỉnh sửa mask.")

    def redo_mask(self):
        if not self._mask_redo_stack:
            self.status_var.set("Không còn thao tác mask để làm lại.")
            return
        self._mask_undo_stack.append(self._mask_snapshot())
        snapshot = self._mask_redo_stack.pop()
        self._restore_mask_snapshot(snapshot)
        self.status_var.set("Đã làm lại chỉnh sửa mask.")

    def _undo_shortcut(self, _event=None):
        focus = self.root.focus_get()
        if focus is not None and focus.winfo_class() in {"Entry", "TEntry", "TCombobox", "Spinbox", "TSpinbox", "Text"}:
            return None
        self.undo_mask()
        return "break"

    def _redo_shortcut(self, _event=None):
        focus = self.root.focus_get()
        if focus is not None and focus.winfo_class() in {"Entry", "TEntry", "TCombobox", "Spinbox", "TSpinbox", "Text"}:
            return None
        self.redo_mask()
        return "break"

    def split_selected_scene(self):
        """Split the selected processing segment at the current preview time.

        Both halves inherit the current scene's mask and per-scene settings.
        This is a processing-range split, not a change to the source video.
        """
        index = self.selected_scene_index
        if index is None or not (0 <= index < len(self.scenes)):
            messagebox.showinfo("Chưa chọn phân cảnh", "Hãy chọn một phân cảnh trước khi tách.", parent=self.root)
            return
        try:
            point = safe_float(self.frame_time_var.get(), "Thời điểm tách")
            source = self.scenes[index]
            start, end = float(source.get("start", 0)), float(source.get("end", 0))
            if point <= start + 0.15 or point >= end - 0.15:
                raise ValueError("Điểm tách phải nằm bên trong phân cảnh, cách hai đầu ít nhất 0,15 giây.")
            left = copy.deepcopy(source)
            right = copy.deepcopy(source)
            left["mask"] = source["mask"].copy() if source.get("mask") is not None else None
            right["mask"] = source["mask"].copy() if source.get("mask") is not None else None
            left["start"], left["end"] = start, point
            right["start"], right["end"] = point, end
            base_name = str(source.get("name", f"Cảnh {index + 1}"))
            left["name"] = base_name
            right["name"] = f"{base_name} · phần 2"
            self.scenes[index:index + 1] = [left, right]
            self.selected_scene_index = index + 1
            self._clear_mask_history()
            self._refresh_scene_list()
            if self.scene_listbox:
                self.scene_listbox.selection_clear(0, tk.END)
                self.scene_listbox.selection_set(self.selected_scene_index)
                self.scene_listbox.activate(self.selected_scene_index)
            self.load_selected_scene()
            self._mark_project_dirty()
            self.status_var.set(f"Đã tách {base_name} tại {self._fmt_time(point)}. Hai phần giữ nguyên mask và Branding riêng của cảnh.")
        except Exception as exc:
            messagebox.showwarning("Không tách được phân cảnh", str(exc), parent=self.root)

    def _on_settings_tab_changed(self, _event=None):
        try:
            selected = self.settings_notebook.index(self.settings_notebook.select())
            self.active_controls_canvas = self.settings_scroll_canvases[selected]
            self.controls_canvas = self.active_controls_canvas
            if selected == 2 and getattr(self, "timeline_canvas", None) is not None:
                self.root.after_idle(self._timeline_redraw)
        except Exception:
            pass

    def _set_nav_active(self, nav_index: Optional[int]):
        for i, button in enumerate(self.nav_buttons):
            try:
                button.configure(style="NavActive.TButton" if i == nav_index else "Nav.TButton")
            except tk.TclError:
                pass

    def _select_workspace_tab(self, index: int, nav_index: Optional[int] = None):
        """Switch the contextual Inspector without rebuilding the workspace."""
        try:
            self.settings_notebook.select(index)
            self._on_settings_tab_changed()
            self.root.after_idle(lambda selected=index: self._restore_inspector_page(selected))
            labels = {0: "WATERMARK", 1: "BRANDING", 2: "EXPORT CENTER", 3: "NÂNG CAO"}
            if hasattr(self, "inspector_title_var"):
                self.inspector_title_var.set(labels.get(index, "INSPECTOR"))
            active_button = {0: 0, 1: 1, 3: 2}.get(index)
            for i, button in enumerate(getattr(self, "inspector_tab_buttons", [])):
                button.configure(style="WorkspaceActive.TButton" if i == active_button else "WorkspaceTab.TButton")
            if nav_index is not None:
                self._last_workspace_nav = nav_index
                self._set_nav_active(nav_index)
            elif index in (0, 1, 2):
                self._set_nav_active({0: 2, 1: 3, 2: 4}[index])
        except Exception:
            pass

    def open_project_hub(self):
        """Small start panel; it does not tear down/recreate the active editor."""
        if getattr(self, "project_hub_window", None) is not None:
            try:
                if self.project_hub_window.winfo_exists():
                    self.project_hub_window.lift()
                    return
            except tk.TclError:
                pass
        win = tk.Toplevel(self.root)
        self.project_hub_window = win
        win.title("Trang chủ · Góc Review Phim Video Studio")
        win.geometry("620x430")
        win.minsize(540, 380)
        win.configure(bg="#06111B")
        wrap = ttk.Frame(win, style="Panel.TFrame", padding=22)
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="GÓC REVIEW PHIM", style="Brand.TLabel").pack(anchor="w")
        ttk.Label(wrap, text="VIDEO STUDIO  ·  BẮT ĐẦU CÔNG VIỆC", style="Tagline.TLabel").pack(anchor="w", pady=(2, 18))
        current = self.project_name_var.get() or "Chưa có dự án"
        card = ttk.Frame(wrap, style="Card.TFrame", padding=14)
        card.pack(fill="x", pady=(0, 16))
        ttk.Label(card, text="DỰ ÁN ĐANG MỞ", style="Section.TLabel").pack(anchor="w")
        ttk.Label(card, text=current, style="Card.TLabel", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(5, 2))
        ttk.Label(card, text=self.video_var.get(), style="CardMuted.TLabel", wraplength=520).pack(anchor="w")
        actions = ttk.Frame(wrap, style="Panel.TFrame")
        actions.pack(fill="x")
        actions.columnconfigure((0, 1), weight=1)
        ttk.Button(actions, text="TẠO / CHỌN VIDEO MỚI", style="Primary.TButton",
                   command=lambda: (win.destroy(), self.choose_video())).grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=5)
        ttk.Button(actions, text="MỞ DỰ ÁN ĐÃ LƯU", style="Secondary.TButton",
                   command=lambda: (win.destroy(), self.open_project())).grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=5)
        ttk.Button(actions, text="BATCH PROCESSING", style="Secondary.TButton",
                   command=lambda: (win.destroy(), self.open_batch_dialog())).grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=5)
        ttk.Button(actions, text="TIẾP TỤC CHỈNH SỬA", style="Secondary.TButton",
                   command=win.destroy).grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=5)
        ttk.Label(wrap, text="Auto Save vẫn hoạt động trong nền. Ctrl + S để lưu ngay.",
                  style="Muted.TLabel").pack(anchor="w", pady=(16, 0))
        self._set_nav_active(0)
        win.protocol("WM_DELETE_WINDOW", lambda: (win.destroy(), self._set_nav_active(getattr(self, "_last_workspace_nav", 2))))
        self._present_engine_window(win)

    def open_project_from_nav(self):
        self._set_nav_active(1)
        self.open_project()

    def open_cta_detection(self):
        """Open a separate, review-first detector for Subscribe button/text overlays."""
        if not self.video_path or not Path(self.video_path).is_file():
            messagebox.showinfo("Chưa có video", "Hãy mở video nguồn trước khi quét Subscribe.", parent=self.root)
            return
        if self.busy or self.cta_scan_running:
            messagebox.showinfo("Đang xử lý", "Hãy đợi tác vụ hiện tại kết thúc trước khi quét Subscribe.", parent=self.root)
            return
        if self.cta_window is not None and self.cta_window.winfo_exists():
            self.cta_window.lift()
            return

        win = tk.Toplevel(self.root)
        self.cta_window = win
        win.title("Subscribe Detector · OCR hình ảnh / animation")
        win.geometry("1120x740")
        win.minsize(900, 650)
        win.configure(bg="#06111B")
        outer = ttk.Frame(win, padding=16, style="Panel.TFrame")
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="CTA SUBSCRIBE DETECTOR · IMAGE ONLY", style="PanelTitle.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="Nhận diện CTA Subscribe hiển thị bằng OCR tiếng Anh/Việt, nút/biểu tượng đồ họa và dấu hiệu chuyển động. Xem thumbnail, chọn đúng khoảng rồi mới cắt; không phân tích lời nói hoặc âm thanh.",
            style="Muted.TLabel", wraplength=1000).pack(anchor="w", pady=(5, 12))

        options = ttk.Frame(outer, style="Panel.TFrame")
        options.pack(fill="x", pady=(0, 8))
        ttk.Label(options, text="Mức lấy mẫu", style="Panel.TLabel").pack(side="left")
        self.cta_sample_var = tk.StringVar(value="1.0")
        sample_combo = ttk.Combobox(options, textvariable=self.cta_sample_var, state="readonly", width=30,
                                    values=("0.5", "1.0", "2.0"))
        sample_combo.pack(side="left", padx=8)
        ttk.Label(options, text="giây / ảnh · 0.5 dày / 1.0 cân bằng / 2.0 nhanh",
                  style="Muted.TLabel").pack(side="left")

        actions = ttk.Frame(outer, style="Panel.TFrame")
        actions.pack(fill="x", pady=(0, 8))
        self.cta_scan_button = ttk.Button(actions, text="▶  QUÉT SUBSCRIBE",
                                           style="Primary.TButton", command=self._start_cta_scan)
        self.cta_scan_button.pack(side="left")
        self.cta_stop_button = ttk.Button(actions, text="DỪNG QUÉT", style="Danger.TButton",
                                          state="disabled", command=self._cancel_cta_scan)
        self.cta_stop_button.pack(side="left", padx=7)
        ttk.Button(actions, text="CHỌN / BỎ CHỌN", style="Secondary.TButton",
                   command=self._toggle_cta_candidate).pack(side="left")
        self.cta_cut_button = ttk.Button(actions, text="✂  CẮT ĐOẠN ĐÃ CHỌN & XUẤT MP4",
                                         style="Primary.TButton", state="disabled",
                                         command=self._cut_selected_cta_ranges)
        self.cta_cut_button.pack(side="left", padx=7)
        ttk.Button(actions, text="ĐÓNG", style="Secondary.TButton",
                   command=self._close_cta_detection).pack(side="right")

        self.cta_status_var = tk.StringVar(value="Sẵn sàng. Quét một lượt theo mẫu thời gian; nhận diện chữ Anh/Việt và nút Subscribe hiển thị trong video.")
        ttk.Label(outer, textvariable=self.cta_status_var, style="Status.TLabel").pack(anchor="w", pady=(2, 5))
        self.cta_progress = ttk.Progressbar(outer, maximum=100, mode="determinate")
        self.cta_progress.pack(fill="x", pady=(0, 10))

        # Reserve the footer before the expanding results area. Packing the
        # expanding content first consumes the remaining parcel and clips the
        # explanatory label at the bottom on common Windows/DPI sizes.
        footer = ttk.Frame(outer, style="Panel.TFrame")
        footer.pack(fill="x", pady=(8, 0))
        ttk.Label(
            footer,
            text="Khi bấm CẮT, các khoảng đã chọn sẽ được thêm vào danh sách bỏ của dự án và xuất video mới. Video nguồn được giữ nguyên.",
            style="Muted.TLabel", wraplength=1000
        ).pack(anchor="w")

        content = ttk.Frame(outer, style="Panel.TFrame")
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=3)
        content.columnconfigure(1, weight=2)
        content.rowconfigure(0, weight=1)
        table_frame = ttk.Frame(content, style="Panel.TFrame")
        table_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        columns = ("selected", "range", "duration", "confidence", "samples", "location")
        self.cta_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=16,
                                     selectmode="browse")
        headings = (("selected", "CHỌN"), ("range", "KHOẢNG THỜI GIAN"), ("duration", "DÀI"),
                    ("confidence", "ĐIỂM GỢI Ý"), ("samples", "MẪU"), ("location", "VỊ TRÍ"))
        for col, label in headings:
            self.cta_tree.heading(col, text=label)
        self.cta_tree.column("selected", width=58, anchor="center", stretch=False)
        self.cta_tree.column("range", width=190, anchor="center", stretch=False)
        self.cta_tree.column("duration", width=70, anchor="center", stretch=False)
        self.cta_tree.column("confidence", width=80, anchor="center", stretch=False)
        self.cta_tree.column("samples", width=55, anchor="center", stretch=False)
        self.cta_tree.column("location", width=90, anchor="center", stretch=True)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.cta_tree.yview)
        self.cta_tree.configure(yscrollcommand=scroll.set)
        self.cta_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.cta_tree.bind("<<TreeviewSelect>>", self._on_cta_candidate_selected)
        self.cta_tree.bind("<Double-1>", lambda _event: self._toggle_cta_candidate())

        preview = ttk.Frame(content, style="Panel.TFrame")
        preview.grid(row=0, column=1, sticky="nsew")
        ttk.Label(preview, text="THUMBNAIL XEM LẠI", style="Panel.TLabel").pack(anchor="w")
        self.cta_preview_label = ttk.Label(preview, text="Chọn một kết quả để xem khung hình mẫu",
                                           style="Muted.TLabel", anchor="center")
        self.cta_preview_label.pack(fill="both", expand=True, pady=(8, 5))
        self.cta_preview_caption = tk.StringVar(value="Chưa chọn ứng viên.")
        ttk.Label(preview, textvariable=self.cta_preview_caption, style="Muted.TLabel",
                  wraplength=350).pack(anchor="w", pady=(3, 0))
        edit_row = ttk.Frame(preview, style="Panel.TFrame")
        edit_row.pack(fill="x", pady=(8, 0))
        ttk.Label(edit_row, text="Bắt đầu", style="Panel.TLabel").pack(side="left")
        self.cta_start_var = tk.StringVar(value="")
        ttk.Entry(edit_row, textvariable=self.cta_start_var, width=9).pack(side="left", padx=(5, 10))
        ttk.Label(edit_row, text="Kết thúc", style="Panel.TLabel").pack(side="left")
        self.cta_end_var = tk.StringVar(value="")
        ttk.Entry(edit_row, textvariable=self.cta_end_var, width=9).pack(side="left", padx=(5, 0))
        ttk.Label(preview, text="Có thể chỉnh mốc của dòng đang chọn trước khi cắt.",
                  style="Muted.TLabel", wraplength=350).pack(anchor="w", pady=(4, 0))

        win.protocol("WM_DELETE_WINDOW", self._close_cta_detection)
        self._present_engine_window(win)

    def _start_cta_scan(self):
        if self.cta_scan_running or self.busy:
            return
        if not self.video_path or not Path(self.video_path).is_file():
            messagebox.showwarning("Thiếu video", "Video nguồn không còn tồn tại.", parent=self.cta_window)
            return
        try:
            interval = float(self.cta_sample_var.get() or 0.5)
            if interval not in (0.5, 1.0, 2.0):
                interval = 0.5
        except Exception:
            interval = 0.5
        self.cta_scan_cancel_event = threading.Event()
        self.cta_scan_running = True
        self.cta_candidates = []
        self.cta_selected_ids = set()
        self.cta_progress.configure(value=0)
        self.cta_status_var.set("Đang quét chữ Subscribe bằng OCR…")
        self.cta_scan_button.configure(state="disabled")
        self.cta_stop_button.configure(state="normal")
        self.cta_cut_button.configure(state="disabled")
        for item in self.cta_tree.get_children():
            self.cta_tree.delete(item)
        events = self.cta_scan_events
        cancel = self.cta_scan_cancel_event
        video = str(self.video_path)

        def progress(done, total):
            events.put(("progress", done, total))

        def worker():
            try:
                result = detect_cta_image_candidates(video, sample_interval=interval,
                                                      cancel_event=cancel, progress_callback=progress)
                events.put(("done", result))
            except CTAScanCancelled as exc:
                events.put(("cancelled", str(exc)))
            except Exception as exc:
                events.put(("error", str(exc)))

        threading.Thread(target=worker, name="CTAImageDetector", daemon=True).start()
        self._poll_cta_scan_events()

    def _poll_cta_scan_events(self):
        if self.cta_window is None or not self.cta_window.winfo_exists():
            self.cta_scan_running = False
            return
        latest_done = False
        try:
            while True:
                event = self.cta_scan_events.get_nowait()
                if event[0] == "progress":
                    _kind, done, total = event
                    pct = 100.0 * done / max(1, total)
                    self.cta_progress.configure(value=pct)
                    self.cta_status_var.set(f"Đang quét Subscribe… {done}/{total} ảnh mẫu ({pct:.0f}%).")
                elif event[0] == "done":
                    self.cta_candidates = event[1].get("candidates", [])
                    self.cta_scan_running = False
                    self.cta_scan_button.configure(state="normal")
                    self.cta_stop_button.configure(state="disabled")
                    for index, candidate in enumerate(self.cta_candidates):
                        self.cta_tree.insert("", "end", iid=str(index), values=(
                            "□", f"{self._fmt_time(candidate['start'])} – {self._fmt_time(candidate['end'])}",
                            f"{candidate['duration']:.1f}s", f"{candidate['confidence']:.0f}%",
                            str(candidate["samples"]), candidate.get("location", "—")))
                    self.cta_status_var.set(
                        f"Hoàn tất: {len(self.cta_candidates)} ứng viên từ {event[1]['samples']} ảnh mẫu. {event[1].get('note', '')}")
                    self.cta_cut_button.configure(state="disabled")
                    if self.cta_candidates:
                        first = self.cta_tree.get_children()[0]
                        self.cta_tree.selection_set(first)
                        self._on_cta_candidate_selected()
                    else:
                        self.cta_preview_caption.set("Không tìm thấy chữ Subscribe rõ ràng trong các khung hình đã quét.")
                    latest_done = True
                    if self.cta_close_after_scan:
                        self.cta_close_after_scan = False
                        self._close_cta_detection()
                        return
                elif event[0] == "cancelled":
                    self.cta_scan_running = False
                    self.cta_scan_button.configure(state="normal")
                    self.cta_stop_button.configure(state="disabled")
                    self.cta_status_var.set(event[1])
                    latest_done = True
                    if self.cta_close_after_scan:
                        self.cta_close_after_scan = False
                        self._close_cta_detection()
                        return
                elif event[0] == "error":
                    self.cta_scan_running = False
                    self.cta_scan_button.configure(state="normal")
                    self.cta_stop_button.configure(state="disabled")
                    self.cta_status_var.set("Quét chưa thành công: " + event[1])
                    messagebox.showerror("Lỗi Subscribe Detector", event[1], parent=self.cta_window)
                    latest_done = True
                    if self.cta_close_after_scan:
                        self.cta_close_after_scan = False
                        self._close_cta_detection()
                        return
        except Empty:
            pass
        if self.cta_scan_running and not latest_done:
            self.cta_window.after(100, self._poll_cta_scan_events)

    def _cancel_cta_scan(self):
        if self.cta_scan_cancel_event is not None:
            self.cta_scan_cancel_event.set()
            self.cta_stop_button.configure(state="disabled")
            self.cta_status_var.set("Đang dừng quét…")

    def _on_cta_candidate_selected(self, _event=None):
        if self.cta_tree is None:
            return
        selected = self.cta_tree.selection()
        if not selected:
            return
        try:
            candidate = self.cta_candidates[int(selected[0])]
            if self.cta_start_var is not None:
                self.cta_start_var.set(f"{float(candidate['start']):.2f}")
            if self.cta_end_var is not None:
                self.cta_end_var.set(f"{float(candidate['end']):.2f}")
            frame = read_frame(self.video_path, float(candidate["sample_time"]))
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(rgb)
            image.thumbnail((510, 330), Image.Resampling.LANCZOS)
            draw = ImageDraw.Draw(image)
            sx = image.width / max(1.0, float(frame.shape[1]))
            sy = image.height / max(1.0, float(frame.shape[0]))
            x, y, w, h = candidate["x"], candidate["y"], candidate["w"], candidate["h"]
            draw.rectangle((x * sx, y * sy, (x + w) * sx, (y + h) * sy), outline=(40, 235, 210), width=3)
            self.cta_preview_photo = ImageTk.PhotoImage(image)
            self.cta_preview_label.configure(image=self.cta_preview_photo, text="")
            self.cta_preview_caption.set(
                f"{candidate.get('label', 'Subscribe')} · điểm gợi ý {candidate['confidence']:.0f}/100 · {candidate['samples']} ảnh mẫu · "
                f"{self._fmt_time(candidate['start'])}–{self._fmt_time(candidate['end'])}. "
                "Khung xanh chỉ là vùng đồ họa gợi ý, không phải xác nhận ngữ nghĩa.")
        except Exception as exc:
            self.cta_preview_caption.set(f"Không tạo được thumbnail: {exc}")

    def _toggle_cta_candidate(self):
        if self.cta_tree is None:
            return
        selected = self.cta_tree.selection()
        if not selected:
            return
        candidate_id = int(selected[0])
        if candidate_id in self.cta_selected_ids:
            self.cta_selected_ids.remove(candidate_id)
            label = "□"
        else:
            self.cta_selected_ids.add(candidate_id)
            label = "☑"
        values = list(self.cta_tree.item(selected[0], "values"))
        values[0] = label
        self.cta_tree.item(selected[0], values=values)
        count_selected = len(self.cta_selected_ids)
        self.cta_cut_button.configure(state="normal" if count_selected else "disabled")
        self.cta_status_var.set(f"Đã chọn {count_selected}/{len(self.cta_candidates)} khoảng Subscribe. Hãy xem lại thumbnail trước khi cắt.")

    def _cut_selected_cta_ranges(self):
        if not self.cta_selected_ids:
            messagebox.showinfo("Chưa chọn Subscribe", "Hãy chọn ít nhất một khoảng Subscribe cần bỏ.", parent=self.cta_window)
            return
        duration = float((self.info or {}).get("duration", 0.0))
        if duration <= 0 or not self.video_path:
            messagebox.showwarning("Thiếu video", "Không còn thông tin video nguồn.", parent=self.cta_window)
            return
        selected = [self.cta_candidates[index] for index in sorted(self.cta_selected_ids)
                    if 0 <= index < len(self.cta_candidates)]
        if not selected:
            return
        proposed = []
        active_row = self.cta_tree.selection() if self.cta_tree is not None else ()
        active_id = int(active_row[0]) if active_row else None
        for index in sorted(self.cta_selected_ids):
            if not (0 <= index < len(self.cta_candidates)):
                continue
            item = self.cta_candidates[index]
            start, end = float(item["start"]), float(item["end"])
            # Editing the currently focused row lets the user trim sampling margins
            # without disturbing the other selected CTA intervals.
            if index == active_id:
                try:
                    start = safe_float(self.cta_start_var.get(), "Mốc Subscribe bắt đầu")
                    end = safe_float(self.cta_end_var.get(), "Mốc Subscribe kết thúc")
                except Exception as exc:
                    messagebox.showwarning("Mốc CTA chưa hợp lệ", str(exc), parent=self.cta_window)
                    return
            start = max(0.0, start)
            end = min(duration, end)
            if end <= start:
                messagebox.showwarning("Mốc CTA chưa hợp lệ", "Mốc kết thúc phải lớn hơn mốc bắt đầu.", parent=self.cta_window)
                return
            if end - start >= 0.05:
                proposed.append((start, end))
        if not proposed:
            messagebox.showwarning("Không có khoảng hợp lệ", "Các mốc được chọn không tạo thành khoảng cắt hợp lệ.", parent=self.cta_window)
            return
        total = sum(end - start for start, end in normalize_excluded_ranges(proposed, duration=duration))
        answer = messagebox.askyesno(
            "Xác nhận cắt Subscribe",
            f"Bạn đã chọn {len(proposed)} khoảng Subscribe (khoảng {total:.2f} giây).\n\n"
            "Video Studio sẽ cắt đồng bộ hình và tiếng, sau đó xuất một file MP4 mới.\n"
            "Các khoảng bỏ khác đã có trong dự án cũng được áp dụng. Video nguồn luôn được giữ nguyên.\n\nTiếp tục?",
            parent=self.cta_window)
        if not answer:
            return
        old_ranges = list(self.excluded_ranges)
        self.excluded_ranges = normalize_excluded_ranges(old_ranges + proposed, duration=duration)
        self._mark_project_dirty()
        self.export_full()
        if not self.busy:
            self.excluded_ranges = old_ranges
            self._mark_project_dirty()
            return
        self.cta_status_var.set("Đang xuất MP4 với các khoảng Subscribe đã chọn. Có thể theo dõi tiến trình ở cửa sổ chính.")
        try:
            self.cta_window.destroy()
        except tk.TclError:
            pass
        self.cta_window = None

    def _close_cta_detection(self):
        if self.cta_scan_running:
            if not messagebox.askyesno("Đang quét Subscribe", "Dừng quét hiện tại và đóng cửa sổ sau khi dừng xong?", parent=self.cta_window):
                return
            self.cta_close_after_scan = True
            self._cancel_cta_scan()
            return
        if self.cta_window is not None:
            try:
                self.cta_window.destroy()
            except tk.TclError:
                pass
        self.cta_window = None
        self.cta_tree = None
        self.cta_preview_photo = None
        self.cta_start_var = None
        self.cta_end_var = None

    def _bind_project_dirty_tracking(self):
        """Mark a project dirty when editable controls change; loading a project is suppressed."""
        tracked_vars = (
            self.label_type_var, self.label_value_var, self.frame_time_var,
            self.preview_start_var, self.preview_length_var,
            self.segment_start_var, self.segment_end_var,
            self.segment_from_minutes_var, self.segment_to_minutes_var,
            self.expansion_var, self.auto_mask_expansion_var, self.show_mask,
            self.preview_720_var, self.prefer_gpu_var, self.auto_open_output_var,
            self.autosave_enabled_var,
        )
        for variable in tracked_vars:
            variable.trace_add("write", lambda *_args: self._mark_project_dirty())
        self.segment_start_var.trace_add("write", lambda *_: self._sync_minute_fields_from_seconds())
        self.segment_end_var.trace_add("write", lambda *_: self._sync_minute_fields_from_seconds())
        self.root.bind_all("<Control-s>", self._project_save_shortcut, add="+")
        self.root.bind_all("<Control-o>", self._project_open_shortcut, add="+")
        self.root.bind_all("<Control-Shift-S>", self._project_save_as_shortcut, add="+")

    def _mark_project_dirty(self):
        if self._loading_project or (not self.video_path and not self.current_project_path):
            return
        self.project_dirty = True
        self._update_project_indicator()
        self._schedule_debounced_autosave()

    def _update_project_indicator(self):
        if self.current_project_path:
            name = Path(self.current_project_path).stem
            self.project_name_var.set(f"{name}{'  •  CHƯA LƯU' if self.project_dirty else ''}")
        elif self._autosave_recovery_path:
            self.project_name_var.set(f"TỰ LƯU · {Path(self._autosave_recovery_path).stem[:14]}")
        else:
            self.project_name_var.set("DỰ ÁN CHƯA LƯU" if self.project_dirty else "CHƯA CÓ DỰ ÁN")
        try:
            suffix = " *" if self.project_dirty else ""
            self.root.title(f"{APP_TITLE}  |  v{APP_VERSION}{suffix}")
            if hasattr(self, "header_save_state"):
                if not self.autosave_enabled_var.get():
                    state_text = "Auto Save đang tắt"
                elif self.project_dirty:
                    state_text = "●  Có thay đổi · đang tự lưu"
                else:
                    state_text = "✓  Đã lưu"
                self.header_save_state.set(state_text)
        except Exception:
            pass

    def _autosave_directory(self) -> Path:
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        else:
            base = Path.home() / ".local" / "share"
        return base / "GocReviewPhim" / "autosave"

    def _recovery_path_for_video(self) -> Path:
        identity = str(Path(self.video_path).expanduser().resolve())
        token = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()[:16]
        stem = Path(self.video_path).stem[:48] or "du_an"
        return self._autosave_directory() / f"{stem}_{token}.autosave.grproj"

    def _schedule_debounced_autosave(self):
        if not self.autosave_enabled_var.get() or not self.video_path or self._loading_project:
            return
        try:
            if self._autosave_after_id:
                self.root.after_cancel(self._autosave_after_id)
        except Exception:
            pass
        try:
            self._autosave_after_id = self.root.after(5000, self._autosave_now)
        except tk.TclError:
            self._autosave_after_id = None

    def _schedule_periodic_autosave(self):
        try:
            if self._autosave_periodic_id:
                self.root.after_cancel(self._autosave_periodic_id)
            self._autosave_periodic_id = self.root.after(60000, self._autosave_tick)
        except tk.TclError:
            self._autosave_periodic_id = None

    def _autosave_tick(self):
        if self.autosave_enabled_var.get() and self.project_dirty:
            self._autosave_now()
        self._schedule_periodic_autosave()

    def _on_autosave_setting_changed(self):
        self.autosave_nav_var.set("Auto Save\nđang hoạt động" if self.autosave_enabled_var.get() else "Auto Save\nđang tắt")
        self._mark_project_dirty()
        if self.autosave_enabled_var.get():
            self._schedule_debounced_autosave()
        elif self._autosave_after_id:
            try:
                self.root.after_cancel(self._autosave_after_id)
            except Exception:
                pass
            self._autosave_after_id = None

    def _autosave_now(self):
        self._autosave_after_id = None
        if not self.autosave_enabled_var.get() or not self.project_dirty or not self.video_path or not self.info:
            return
        if self.busy:
            try:
                self._autosave_after_id = self.root.after(10000, self._autosave_now)
            except tk.TclError:
                pass
            return
        try:
            if self.current_project_path:
                target = Path(self.current_project_path).expanduser().resolve()
            else:
                if not self._autosave_recovery_path:
                    self._autosave_recovery_path = str(self._recovery_path_for_video())
                target = Path(self._autosave_recovery_path).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = self._build_project_data(target)
            if self.current_project_path:
                payload = self._sync_project_asset_state(payload, target)
            temp_path = target.with_name(target.name + ".tmp")
            try:
                temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temp_path, target)
            finally:
                temp_path.unlink(missing_ok=True)
            self.project_dirty = False
            self._update_project_indicator()
            self.status_var.set(f"Đã tự động lưu lúc {datetime.now().strftime('%H:%M:%S')}")
        except Exception as exc:
            # Autosave failures must not interrupt editing or open repetitive dialogs.
            self.status_var.set(f"Chưa tự động lưu được: {exc}")

    def _check_autosave_recovery(self):
        if self.video_path:
            return
        try:
            folder = self._autosave_directory()
            candidates = sorted(folder.glob("*.autosave.grproj"), key=lambda item: item.stat().st_mtime, reverse=True)
            if not candidates:
                return
            newest = candidates[0]
            modified = datetime.fromtimestamp(newest.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
            if messagebox.askyesno(
                "Tìm thấy bản tự động lưu",
                f"Tìm thấy bản khôi phục gần nhất ({modified}):\n{newest.name}\n\nBạn có muốn khôi phục phiên làm việc này không?"
            ):
                self._load_project_file(newest, recovery=True)
        except Exception:
            pass

    def _sync_minute_fields_from_seconds(self):
        if self._syncing_minute_fields:
            return
        try:
            start = max(0.0, float(self.segment_start_var.get() or 0.0)) / 60.0
            end = max(0.0, float(self.segment_end_var.get() or 0.0)) / 60.0
            self._syncing_minute_fields = True
            self.segment_from_minutes_var.set(f"{start:.3f}".rstrip("0").rstrip("."))
            self.segment_to_minutes_var.set(f"{end:.3f}".rstrip("0").rstrip("."))
        except (TypeError, ValueError):
            pass
        finally:
            self._syncing_minute_fields = False

    def apply_minute_range(self):
        try:
            if not self.info:
                raise ValueError("Hãy chọn video trước.")
            start_min = safe_float(self.segment_from_minutes_var.get(), "Mốc bắt đầu (phút)")
            end_min = safe_float(self.segment_to_minutes_var.get(), "Mốc kết thúc (phút)")
            if start_min < 0 or end_min <= start_min:
                raise ValueError("Mốc phút kết thúc phải lớn hơn mốc bắt đầu.")
            start_seconds, end_seconds = start_min * 60.0, end_min * 60.0
            if end_seconds > float(self.info.get("duration", 0.0)) + 0.05:
                raise ValueError(f"Mốc kết thúc vượt thời lượng video ({self.info['duration'] / 60.0:.2f} phút).")
            self.segment_start_var.set(f"{start_seconds:.3f}".rstrip("0").rstrip("."))
            self.segment_end_var.set(f"{end_seconds:.3f}".rstrip("0").rstrip("."))
            self._timeline_redraw()
            self.status_var.set(f"Đã áp dụng đoạn {start_min:g}–{end_min:g} phút. Nhấn XUẤT ĐOẠN để xuất video.")
        except Exception as exc:
            messagebox.showwarning("Khoảng thời gian không hợp lệ", str(exc))

    def _refresh_scene_list(self):
        listbox = getattr(self, "scene_listbox", None)
        if listbox is not None:
            try:
                listbox.delete(0, tk.END)
                for index, scene in enumerate(self.scenes):
                    start = self._fmt_time(scene.get("start", 0.0))
                    end = self._fmt_time(scene.get("end", 0.0))
                    px = int(scene.get("expansion", MASK_DEFAULT_EXPANSION))
                    listbox.insert(tk.END, f"{scene.get('name', f'Cảnh {index + 1}')}  |  {start}–{end}  |  {px}px")
            except tk.TclError:
                pass
        self._refresh_scene_thumbnail_strip()

    def _on_scene_selected(self, _event=None):
        if not self.scene_listbox:
            return
        selected = self.scene_listbox.curselection()
        self.selected_scene_index = int(selected[0]) if selected else None
        self._refresh_scene_thumbnail_selection()

    def _scene_range(self):
        if not self.info:
            raise ValueError("Hãy chọn video trước.")
        start = safe_float(self.segment_start_var.get(), "Thời điểm bắt đầu cảnh")
        end = safe_float(self.segment_end_var.get(), "Thời điểm kết thúc cảnh")
        duration = float(self.info.get("duration", 0.0))
        if start < 0 or end <= start or end > duration + 0.05:
            raise ValueError("Khoảng thời gian cảnh không hợp lệ. Hãy kiểm tra In/Out trên timeline hoặc nhập theo phút.")
        if self.mask is None or cv2.countNonZero(self.mask) == 0:
            raise ValueError("Cảnh này chưa có mask. Hãy khoanh watermark và nhấn TỰ TÁCH NÉT trước.")
        return max(0.0, start), min(duration, end)

    def _scene_candidate(self, start, end, name):
        return {
            "name": name,
            "start": float(start),
            "end": float(end),
            "expansion": int(self._effective_mask_expansion(self.mask)),
            "auto_expansion": bool(self.auto_mask_expansion_var.get()),
            "manual_override": bool(self._mask_expansion_override),
            "lasso_points": list(self.lasso_points),
            "mask": self.mask.copy(),
        }

    def _validate_scene_overlap(self, candidate, ignore_index=None):
        for index, scene in enumerate(self.scenes):
            if ignore_index is not None and index == ignore_index:
                continue
            if candidate["start"] < float(scene["end"]) and candidate["end"] > float(scene["start"]):
                raise ValueError(
                    f"Khoảng thời gian bị trùng với {scene.get('name', f'Cảnh {index + 1}')} "
                    f"({self._fmt_time(scene['start'])}–{self._fmt_time(scene['end'])}). Hãy chọn In/Out không chồng lấn."
                )

    def add_scene(self):
        try:
            start, end = self._scene_range()
            candidate = self._scene_candidate(start, end, f"Cảnh {len(self.scenes) + 1}")
            self._validate_scene_overlap(candidate)
            self.segment_from_minutes_var.set(f"{start / 60.0:.4f}".rstrip("0").rstrip("."))
            self.segment_to_minutes_var.set(f"{end / 60.0:.4f}".rstrip("0").rstrip("."))
            self.scenes.append(candidate)
            self.scenes.sort(key=lambda item: item["start"])
            self.selected_scene_index = next(i for i, item in enumerate(self.scenes) if item is candidate)
            self._refresh_scene_list()
            if self.scene_listbox:
                self.scene_listbox.selection_clear(0, tk.END)
                self.scene_listbox.selection_set(self.selected_scene_index)
                self.scene_listbox.see(self.selected_scene_index)
            self._mark_project_dirty()
            self.status_var.set(f"Đã thêm {candidate['name']} ({self._fmt_time(start)}–{self._fmt_time(end)}).")
        except Exception as exc:
            messagebox.showwarning("Không thêm được phân cảnh", str(exc))

    def load_selected_scene(self):
        index = self.selected_scene_index
        if index is None or not (0 <= index < len(self.scenes)):
            messagebox.showinfo("Chưa chọn cảnh", "Hãy chọn một phân cảnh trong danh sách trước.")
            return
        scene = self.scenes[index]
        self._clear_mask_history()
        try:
            sample_time = min(float(scene["start"]), max(0.0, float(self.info["duration"]) - 0.05))
            frame = read_frame(self.video_path, sample_time)
            self._loading_project = True
            try:
                self.frame = frame
                self.frame_time_var.set(f"{sample_time:.3f}".rstrip("0").rstrip("."))
                self.preview_seek_var.set(sample_time)
                self.preview_seek_label_var.set(self._fmt_time(sample_time))
                self.segment_start_var.set(f"{scene['start']:.3f}".rstrip("0").rstrip("."))
                self.segment_end_var.set(f"{scene['end']:.3f}".rstrip("0").rstrip("."))
                self.segment_from_minutes_var.set(f"{float(scene['start']) / 60.0:.4f}".rstrip("0").rstrip("."))
                self.segment_to_minutes_var.set(f"{float(scene['end']) / 60.0:.4f}".rstrip("0").rstrip("."))
                self.mask = scene["mask"].copy()
                self.lasso_points = list(scene.get("lasso_points", []))
                self.expansion_var.set(int(scene.get("expansion", MASK_DEFAULT_EXPANSION)))
                self.auto_mask_expansion_var.set(bool(scene.get("auto_expansion", True)))
                self._mask_expansion_override = bool(scene.get("manual_override", False))
                self.canvas_points = []
            finally:
                self._loading_project = False
            self._on_mask_mode_changed(reset_override=False)
            self.root.update_idletasks()
            if self.lasso_points:
                scale, (ox, oy), _size = self._canvas_geometry()
                self.canvas_points = [(ox + x * scale, oy + y * scale) for x, y in self.lasso_points]
            self.refresh_canvas()
            self.status_var.set(f"Đã nạp {scene.get('name', f'Cảnh {index + 1}')}. Chỉnh mask rồi nhấn Cập nhật.")
        except Exception as exc:
            messagebox.showerror("Không nạp được phân cảnh", str(exc))

    def update_selected_scene(self):
        index = self.selected_scene_index
        if index is None or not (0 <= index < len(self.scenes)):
            messagebox.showinfo("Chưa chọn cảnh", "Hãy chọn phân cảnh cần cập nhật.")
            return
        try:
            start, end = self._scene_range()
            candidate = self._scene_candidate(start, end, self.scenes[index].get("name", f"Cảnh {index + 1}"))
            self._validate_scene_overlap(candidate, ignore_index=index)
            self.scenes[index] = candidate
            self.segment_from_minutes_var.set(f"{start / 60.0:.4f}".rstrip("0").rstrip("."))
            self.segment_to_minutes_var.set(f"{end / 60.0:.4f}".rstrip("0").rstrip("."))
            self.scenes.sort(key=lambda item: item["start"])
            self.selected_scene_index = next((i for i, sc in enumerate(self.scenes) if sc["name"] == candidate["name"] and sc["start"] == candidate["start"]), None)
            self._refresh_scene_list()
            if self.scene_listbox and self.selected_scene_index is not None:
                self.scene_listbox.selection_set(self.selected_scene_index)
            self._mark_project_dirty()
            self.status_var.set(f"Đã cập nhật {candidate['name']}.")
        except Exception as exc:
            messagebox.showwarning("Không cập nhật được cảnh", str(exc))

    def delete_selected_scene(self):
        index = self.selected_scene_index
        if index is None or not (0 <= index < len(self.scenes)):
            messagebox.showinfo("Chưa chọn cảnh", "Hãy chọn phân cảnh cần xóa.")
            return
        name = self.scenes[index].get("name", f"Cảnh {index + 1}")
        if not messagebox.askyesno("Xóa phân cảnh", f"Xóa {name}?"):
            return
        del self.scenes[index]
        self.selected_scene_index = None
        self._refresh_scene_list()
        self._mark_project_dirty()
        self.status_var.set(f"Đã xóa {name}.")

    def _project_save_shortcut(self, _event=None):
        self.save_project()
        return "break"

    def _project_open_shortcut(self, _event=None):
        self.open_project()
        return "break"

    def _project_save_as_shortcut(self, _event=None):
        self.save_project(save_as=True)
        return "break"

    def _confirm_unsaved_project(self) -> bool:
        if not self.project_dirty:
            return True
        answer = messagebox.askyesnocancel(
            "Dự án chưa được lưu",
            "Các thay đổi trong dự án chưa được lưu.\n\nBạn có muốn lưu trước khi tiếp tục không?"
        )
        if answer is None:
            return False
        if answer:
            return self.save_project()
        return True

    @staticmethod
    def _project_path_record(path: str, project_dir: Path) -> Optional[dict]:
        if not path:
            return None
        try:
            absolute = Path(path).expanduser().resolve()
            try:
                relative = os.path.relpath(str(absolute), str(project_dir.resolve()))
            except (ValueError, OSError):
                relative = None
            return {"absolute": str(absolute), "relative": relative}
        except Exception:
            return {"absolute": str(path), "relative": None}

    @staticmethod
    def _resolve_project_path(record, project_dir: Path, require_exists: bool = True) -> str:
        if not record:
            return ""
        if isinstance(record, str):
            candidates = [Path(record).expanduser()]
        elif isinstance(record, dict):
            candidates = []
            absolute = record.get("absolute")
            relative = record.get("relative")
            # Prefer the project-relative location so a project and its media can move together.
            if relative:
                candidate = Path(str(relative))
                candidates.append(candidate if candidate.is_absolute() else project_dir / candidate)
            if absolute:
                candidates.append(Path(str(absolute)).expanduser())
        else:
            return ""
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                if resolved.exists() or not require_exists:
                    return str(resolved)
            except Exception:
                continue
        return ""

    @staticmethod
    def _encode_project_mask(mask: Optional[np.ndarray]) -> Optional[str]:
        if mask is None:
            return None
        ok, encoded = cv2.imencode(
            ".png", np.asarray(mask, dtype=np.uint8),
            [cv2.IMWRITE_PNG_COMPRESSION, 6]
        )
        if not ok:
            raise ValueError("Không thể mã hóa mask để lưu dự án.")
        return base64.b64encode(encoded.tobytes()).decode("ascii")

    @staticmethod
    def _decode_project_mask(encoded: Optional[str]) -> Optional[np.ndarray]:
        if not encoded:
            return None
        raw = base64.b64decode(encoded, validate=True)
        mask = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if mask is None:
            raise ValueError("Dữ liệu mask trong file dự án không hợp lệ.")
        return np.where(mask > 0, 255, 0).astype(np.uint8)

    def _sync_project_asset_state(self, payload: dict, target_path: Path) -> dict:
        """Copy linked assets into the project and keep live references portable."""
        payload = sync_project_assets(payload, target_path)
        media = payload.get("media", {}) if isinstance(payload, dict) else {}
        if isinstance(media, dict) and media.get("logo"):
            resolved = self._resolve_project_path(media.get("logo"), target_path.parent, True)
            if resolved:
                self.logo_path = resolved
                try:
                    self.logo_var.set(Path(resolved).name)
                except Exception:
                    pass
        raw_scenes = payload.get("scenes", []) if isinstance(payload, dict) else []
        for scene, raw_scene in zip(self.scenes, raw_scenes):
            if isinstance(raw_scene, dict) and raw_scene.get("branding_logo"):
                resolved = self._resolve_project_path(raw_scene.get("branding_logo"), target_path.parent, True)
                if resolved:
                    scene["branding_logo_path"] = resolved
        return payload

    def _build_project_data(self, target_path: Path) -> dict:
        project_dir = target_path.resolve().parent
        frame_time = safe_float(self.frame_time_var.get(), "Thời điểm khung hình")
        mask_data = self._encode_project_mask(self.mask)
        return {
            "format": "goc-review-phim-video-studio-project",
            "schema_version": 4,
            "app_version": APP_VERSION,
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "media": {
                "video": self._project_path_record(self.video_path, project_dir),
                "logo": self._project_path_record(self.logo_path, project_dir),
                "output_dir": self._project_path_record(self.output_dir, project_dir),
                "output_dir_custom": bool(self.output_dir_custom),
                "video_width": int(self.info.get("width", 0)) if self.info else 0,
                "video_height": int(self.info.get("height", 0)) if self.info else 0,
            },
            "settings": {
                "label_type": self.label_type_var.get(),
                "label_value": self.label_value_var.get(),
                "frame_time": frame_time,
                "preview_start": self.preview_start_var.get(),
                "preview_length": self.preview_length_var.get(),
                "segment_start": self.segment_start_var.get(),
                "segment_end": self.segment_end_var.get(),
                "mask_expansion": int(self.expansion_var.get()),
                "auto_mask_expansion": bool(self.auto_mask_expansion_var.get()),
                "mask_manual_override": bool(self._mask_expansion_override),
                "show_mask": bool(self.show_mask.get()),
                "preview_720": bool(self.preview_720_var.get()),
                "prefer_gpu": bool(self.prefer_gpu_var.get()),
                "auto_open_output": bool(self.auto_open_output_var.get()),
                "autosave_enabled": bool(self.autosave_enabled_var.get()),
                "segment_from_minutes": self.segment_from_minutes_var.get(),
                "segment_to_minutes": self.segment_to_minutes_var.get(),
                "branding": dict(self.branding_config),
            },
            "selection": {
                "lasso_points": [[int(x), int(y)] for x, y in self.lasso_points],
                "mask_png_base64": mask_data,
                "mask_width": int(self.mask.shape[1]) if self.mask is not None else 0,
                "mask_height": int(self.mask.shape[0]) if self.mask is not None else 0,
                "show_mask": bool(self.show_mask.get()),
            },
            "excluded_ranges": [
                {"start": float(start), "end": float(end)}
                for start, end in normalize_excluded_ranges(self.excluded_ranges,
                    duration=float(self.info.get("duration", 0.0)) if self.info else None)
            ],
            "scenes": [
                {
                    "name": str(scene.get("name", f"Cảnh {index + 1}")),
                    "start": float(scene.get("start", 0.0)),
                    "end": float(scene.get("end", 0.0)),
                    "expansion": int(scene.get("expansion", MASK_DEFAULT_EXPANSION)),
                    "auto_expansion": bool(scene.get("auto_expansion", True)),
                    "manual_override": bool(scene.get("manual_override", False)),
                    "lasso_points": [[int(x), int(y)] for x, y in scene.get("lasso_points", [])],
                    "mask_png_base64": self._encode_project_mask(scene.get("mask")),
                    "branding": dict(scene.get("branding")) if isinstance(scene.get("branding"), dict) else None,
                    "branding_logo": self._project_path_record(scene.get("branding_logo_path", ""), project_dir),
                }
                for index, scene in enumerate(self.scenes)
            ],
        }

    def save_project(self, save_as: bool = False) -> bool:
        """Save the complete editing state to a portable JSON-based .grproj file."""
        if self.busy:
            messagebox.showwarning("Đang xử lý", "Hãy đợi tác vụ hiện tại kết thúc trước khi lưu dự án.")
            return False
        if not self.video_path or not self.info:
            messagebox.showwarning("Chưa có video", "Hãy chọn video nguồn trước khi lưu dự án.")
            return False

        target = Path(self.current_project_path) if self.current_project_path and not save_as else None
        if target is None:
            initial_dir = str(Path(self.video_path).resolve().parent)
            initial_name = f"{Path(self.video_path).stem}.grproj"
            selected = filedialog.asksaveasfilename(
                title="Lưu dự án Video Studio",
                initialdir=initial_dir,
                initialfile=initial_name,
                defaultextension=".grproj",
                filetypes=[("Góc Review Phim Project", "*.grproj"), ("JSON", "*.json")]
            )
            if not selected:
                return False
            target = Path(selected).expanduser().resolve()
            if not target.suffix:
                target = target.with_suffix(".grproj")

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            ensure_project_workspace(target)
            payload = self._build_project_data(target)
            payload = self._sync_project_asset_state(payload, target)
            temp_path = target.with_name(target.name + ".tmp")
            try:
                temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temp_path, target)
            finally:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass
            self.current_project_path = str(target)
            backup_warning = ""
            # A manual save records the newly committed state as a recovery point.
            # Auto Save bypasses this function, so it never fills the rolling history.
            if not target.name.endswith(".autosave.grproj"):
                try:
                    backup_project_file(target, keep=8)
                except Exception as backup_exc:
                    # The main project is already committed; a backup failure must
                    # not be reported as if the primary save itself had failed.
                    backup_warning = f" · chưa tạo được bản sao lưu: {backup_exc}"
            if self._autosave_recovery_path:
                try:
                    Path(self._autosave_recovery_path).unlink(missing_ok=True)
                except Exception:
                    pass
                self._autosave_recovery_path = ""
            self.project_dirty = False
            self._update_project_indicator()
            self.status_var.set(f"Đã lưu dự án: {target.name}{backup_warning}")
            if save_as:
                messagebox.showinfo("Đã lưu bản dự án mới", f"Đã tạo bản dự án mới:\n{target}")
            return True
        except Exception as exc:
            messagebox.showerror("Không lưu được dự án", str(exc))
            return False

    def open_project(self):
        if self.busy:
            messagebox.showwarning("Đang xử lý", "Hãy đợi tác vụ hiện tại kết thúc trước khi mở dự án khác.")
            return
        if not self._confirm_unsaved_project():
            return
        selected = filedialog.askopenfilename(
            title="Mở dự án Video Studio",
            filetypes=[("Góc Review Phim Project", "*.grproj"), ("JSON", "*.json"), ("Tất cả tệp", "*.*")]
        )
        if selected:
            self._load_project_file(Path(selected).expanduser().resolve())

    def _load_project_file(self, project_path: Path, recovery: bool = False) -> bool:
        """Validate and restore a project without clearing its saved mask during frame loading."""
        warnings = []
        logo_fallback = False
        try:
            payload = json.loads(project_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("format") != "goc-review-phim-video-studio-project":
                raise ValueError("Đây không phải file dự án Góc Review Phim Video Studio.")
            schema_version = int(payload.get("schema_version", 0))
            if schema_version > 4:
                raise ValueError(
                    f"Dự án dùng định dạng mới hơn (schema {schema_version}). "
                    "Hãy cập nhật Video Studio lên phiên bản mới hơn."
                )
            if schema_version < 1:
                raise ValueError("File dự án thiếu phiên bản định dạng hoặc không được hỗ trợ.")

            media = payload.get("media") or {}
            settings = payload.get("settings") or {}
            selection = payload.get("selection") or {}
            if not isinstance(media, dict) or not isinstance(settings, dict) or not isinstance(selection, dict):
                raise ValueError("Cấu trúc dữ liệu dự án không hợp lệ.")

            video_path = self._resolve_project_path(media.get("video"), project_path.parent, True)
            if not video_path:
                relinked = filedialog.askopenfilename(
                    title="Không tìm thấy video nguồn – chọn lại video",
                    filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi"), ("Tất cả tệp", "*.*")]
                )
                if not relinked:
                    return False
                video_path = str(Path(relinked).resolve())
            info = read_video_info(video_path)
            duration = float(info.get("duration", 0.0))
            frame_time = float(settings.get("frame_time", 0.0))
            frame_time = max(0.0, min(frame_time, max(0.0, duration - 0.05)))
            frame = read_frame(video_path, frame_time)

            logo_path = self._resolve_project_path(media.get("logo"), project_path.parent, True)
            if not logo_path:
                # Keep opening resilient when a project was moved without its custom
                # logo. The built-in asset becomes the new saved reference, so the
                # same notice will not repeat on subsequent opens.
                logo_path = str(resource_path("assets/Goc_Review_Phim_Logo.png"))
                logo_fallback = True

            output_custom = bool(media.get("output_dir_custom", False))
            output_dir = self._resolve_project_path(media.get("output_dir"), project_path.parent, True)
            if not output_dir:
                output_dir = str(default_output_directory_for_project(project_path, video_path))
                Path(output_dir).mkdir(parents=True, exist_ok=True)
                if media.get("output_dir"):
                    warnings.append("Không tìm thấy thư mục xuất đã lưu; đã chuyển về thư mục video xuất mặc định.")

            old_width = int(media.get("video_width", info.get("width", 0)) or info.get("width", 0))
            old_height = int(media.get("video_height", info.get("height", 0)) or info.get("height", 0))
            frame_h, frame_w = frame.shape[:2]

            lasso_points = []
            raw_points = selection.get("lasso_points") or []
            if isinstance(raw_points, list):
                for point in raw_points[:100000]:
                    if not isinstance(point, (list, tuple)) or len(point) != 2:
                        continue
                    try:
                        x, y = int(float(point[0])), int(float(point[1]))
                    except (TypeError, ValueError):
                        continue
                    if old_width > 0 and old_height > 0:
                        x = round(x * frame_w / old_width)
                        y = round(y * frame_h / old_height)
                    lasso_points.append((max(0, min(frame_w - 1, x)), max(0, min(frame_h - 1, y))))

            mask = self._decode_project_mask(selection.get("mask_png_base64"))
            if mask is not None and mask.shape[:2] != (frame_h, frame_w):
                mask = cv2.resize(mask, (frame_w, frame_h), interpolation=cv2.INTER_NEAREST)
                warnings.append("Kích thước video đã thay đổi; mask được co giãn theo khung hình mới.")

            # Validate and normalize all user-editable values before mutating the workspace.
            label_types = ("Tập", "Phần", "Chương", "Mùa", "Tùy chỉnh")
            label_type = settings.get("label_type", "Tập")
            if label_type not in label_types:
                label_type = "Tùy chỉnh"
            label_value = str(settings.get("label_value", "21"))
            def bounded_number(key, default, low, high, as_int=False):
                try:
                    value = float(settings.get(key, default))
                    value = max(low, min(high, value))
                    return str(int(round(value))) if as_int else str(value)
                except (TypeError, ValueError):
                    return str(default)

            preview_start = bounded_number("preview_start", 0, 0, max(0, duration))
            preview_length = bounded_number("preview_length", 12, PREVIEW_MIN, PREVIEW_MAX, True)
            segment_start = bounded_number("segment_start", 0, 0, max(0, duration))
            segment_end_num = float(bounded_number("segment_end", duration, 0, max(0, duration)))
            segment_start_num = float(segment_start)
            if segment_end_num <= segment_start_num:
                segment_end_num = duration
            segment_end = f"{segment_end_num:.3f}".rstrip("0").rstrip(".")
            expansion = int(float(bounded_number("mask_expansion", MASK_DEFAULT_EXPANSION, 0, 30, True)))
            minute_start_default = float(segment_start) / 60.0
            minute_end_default = float(segment_end) / 60.0
            minute_start = bounded_number("segment_from_minutes", minute_start_default, 0, duration / 60.0)
            minute_end = bounded_number("segment_to_minutes", minute_end_default, 0, duration / 60.0)
            autosave_enabled = bool(settings.get("autosave_enabled", True))
            raw_branding = settings.get("branding") if isinstance(settings.get("branding"), dict) else {}
            loaded_branding = {**default_branding_config(duration), **raw_branding}
            for key in ("logo_opacity", "label_opacity"):
                loaded_branding[key] = max(0, min(100, int(float(loaded_branding.get(key, 90)))))
            loaded_branding["logo_width_pct"] = max(3.0, min(40.0, float(loaded_branding.get("logo_width_pct", 12.0))))
            loaded_branding["logo_rotation"] = max(-180.0, min(180.0, float(loaded_branding.get("logo_rotation", 0))))
            loaded_branding["show_from"] = max(0.0, min(duration, float(loaded_branding.get("show_from", 0.0))))
            loaded_branding["show_to"] = max(loaded_branding["show_from"], min(duration, float(loaded_branding.get("show_to", duration))))

            parsed_scenes = []
            raw_scenes = payload.get("scenes", []) if schema_version >= 2 else []
            if raw_scenes is not None and not isinstance(raw_scenes, list):
                raise ValueError("Danh sách phân cảnh trong dự án không hợp lệ.")
            for index, raw_scene in enumerate(raw_scenes or []):
                if not isinstance(raw_scene, dict):
                    continue
                try:
                    scene_start = max(0.0, min(duration, float(raw_scene.get("start", 0.0))))
                    scene_end = max(0.0, min(duration, float(raw_scene.get("end", 0.0))))
                    scene_mask = self._decode_project_mask(raw_scene.get("mask_png_base64"))
                    if scene_mask is None or scene_end <= scene_start:
                        continue
                    if scene_mask.shape[:2] != (frame_h, frame_w):
                        scene_mask = cv2.resize(scene_mask, (frame_w, frame_h), interpolation=cv2.INTER_NEAREST)
                        warnings.append(f"Mask của Cảnh {index + 1} đã được co giãn theo kích thước video.")
                    scene_points = []
                    for point in (raw_scene.get("lasso_points") or [])[:100000]:
                        if isinstance(point, (list, tuple)) and len(point) == 2:
                            px = int(float(point[0])); py = int(float(point[1]))
                            if old_width > 0 and old_height > 0:
                                px = round(px * frame_w / old_width)
                                py = round(py * frame_h / old_height)
                            scene_points.append((max(0, min(frame_w - 1, px)), max(0, min(frame_h - 1, py))))
                    parsed_scenes.append({
                        "name": str(raw_scene.get("name", f"Cảnh {index + 1}")),
                        "start": scene_start,
                        "end": scene_end,
                        "expansion": int(max(0, min(30, int(raw_scene.get("expansion", MASK_DEFAULT_EXPANSION))))),
                        "auto_expansion": bool(raw_scene.get("auto_expansion", True)),
                        "manual_override": bool(raw_scene.get("manual_override", False)),
                        "lasso_points": scene_points,
                        "mask": scene_mask,
                        "branding": ({**loaded_branding, **raw_scene.get("branding", {})}
                                     if isinstance(raw_scene.get("branding"), dict) else None),
                        "branding_logo_path": self._resolve_project_path(raw_scene.get("branding_logo"), project_path.parent, True),
                    })
                except Exception as scene_exc:
                    warnings.append(f"Bỏ qua phân cảnh {index + 1}: {scene_exc}")
            raw_excluded = payload.get("excluded_ranges", []) if schema_version >= 4 else []
            if raw_excluded is not None and not isinstance(raw_excluded, list):
                raise ValueError("Danh sách khoảng cần bỏ trong dự án không hợp lệ.")
            parsed_excluded = normalize_excluded_ranges(raw_excluded or [], duration=duration)
            if len(parsed_excluded) != len(raw_excluded or []):
                warnings.append("Một số khoảng cần bỏ không hợp lệ hoặc nằm ngoài video nên đã được bỏ qua/điều chỉnh.")

            parsed_scenes.sort(key=lambda item: item["start"])
            for previous, current in zip(parsed_scenes, parsed_scenes[1:]):
                if current["start"] < previous["end"]:
                    raise ValueError("Các phân cảnh trong dự án bị chồng lấn thời gian. Hãy sửa file dự án hoặc bỏ phân cảnh lỗi.")

            # Commit only after the video, frame, and project payload have been read successfully.
            self._loading_project = True
            try:
                self.video_path = video_path
                self.info = info
                self.frame = frame
                self.mask = mask
                self.lasso_points = lasso_points
                self.canvas_points = []
                self.scenes = parsed_scenes
                self.excluded_ranges = parsed_excluded
                self.selected_scene_index = None
                self.output_dir = output_dir
                self.output_dir_custom = output_custom
                self.logo_path = logo_path
                self.branding_config = loaded_branding

                self.video_var.set(Path(video_path).name)
                self.output_dir_var.set(output_dir)
                self.logo_var.set(Path(logo_path).name)
                self.meta_var.set(
                    f"{info['width']}×{info['height']}  •  {info['fps']:.3f} FPS  •  "
                    f"{self._fmt_time(info['duration'])}  •  {info.get('codec', 'video')}"
                )
                self.label_type_var.set(label_type)
                self.label_value_var.set(label_value)
                self.frame_time_var.set(f"{frame_time:.3f}".rstrip("0").rstrip("."))
                self.preview_start_var.set(preview_start)
                self.preview_length_var.set(preview_length)
                self.segment_start_var.set(segment_start)
                self.segment_end_var.set(segment_end)
                self.segment_from_minutes_var.set(minute_start)
                self.segment_to_minutes_var.set(minute_end)
                self.expansion_var.set(expansion)
                self.auto_mask_expansion_var.set(bool(settings.get("auto_mask_expansion", True)))
                self._mask_expansion_override = bool(settings.get("mask_manual_override", False))
                self.show_mask.set(bool(settings.get("show_mask", selection.get("show_mask", True))))
                self.preview_720_var.set(bool(settings.get("preview_720", True)))
                self.prefer_gpu_var.set(bool(settings.get("prefer_gpu", True)))
                self.auto_open_output_var.set(bool(settings.get("auto_open_output", True)))
                self.autosave_enabled_var.set(autosave_enabled)
                self.timeline_info_var.set("Dự án đã khôi phục. Kiểm tra mask trước khi xuất.")
            finally:
                self._loading_project = False

            self._on_mask_mode_changed(reset_override=False)
            if recovery:
                self.current_project_path = ""
                self._autosave_recovery_path = str(project_path.resolve())
            else:
                self.current_project_path = str(project_path.resolve())
                self._autosave_recovery_path = ""
            self.project_dirty = False
            self._refresh_scene_list()
            self.root.update_idletasks()
            if self.frame is not None and self.lasso_points:
                scale, (ox, oy), _size = self._canvas_geometry()
                self.canvas_points = [(ox + x * scale, oy + y * scale) for x, y in self.lasso_points]
            self._timeline_redraw()
            self.refresh_canvas()
            status_text = f"Đã mở dự án: {project_path.name}"
            if logo_fallback:
                status_text += " · dùng logo mặc định (logo riêng không còn tại đường dẫn đã lưu)"
            self.status_var.set(status_text)
            self._update_project_indicator()
            if warnings:
                messagebox.showwarning("Dự án được mở với lưu ý", "\n".join(warnings))
            return True
        except Exception as exc:
            messagebox.showerror("Không mở được dự án", str(exc))
            return False

    def _timeline_duration(self) -> float:
        try:
            return max(0.0, float(self.info.get("duration", 0.0))) if self.info else 0.0
        except Exception:
            return 0.0

    def _timeline_redraw(self):
        canvas = getattr(self, "timeline_canvas", None)
        if canvas is None:
            return
        try:
            w = max(220, canvas.winfo_width())
            h = max(58, canvas.winfo_height())
            canvas.delete("all")
            duration = self._timeline_duration()
            pad = 27
            left, right = pad, w - pad
            y = 27
            canvas.create_line(left, y, right, y, fill="#174154", width=8, capstyle="round")
            if duration <= 0 or right <= left:
                canvas.create_text(w / 2, h / 2, text="Chọn video để bật timeline", fill="#668793",
                                   font=("Segoe UI", 10, "bold"))
                self.timeline_info_var.set("Timeline sẽ hiển thị sau khi nạp video.")
                return
            try:
                start = max(0.0, min(duration, float(self.segment_start_var.get() or 0)))
            except Exception:
                start = 0.0
            try:
                end = max(0.0, min(duration, float(self.segment_end_var.get() or duration)))
            except Exception:
                end = duration
            if end < start:
                start, end = end, start
            x1 = left + (right - left) * start / duration
            x2 = left + (right - left) * end / duration
            canvas.create_line(x1, y, x2, y, fill="#31C1CB", width=9, capstyle="round")
            for fraction in (0, .25, .5, .75, 1):
                x = left + (right - left) * fraction
                canvas.create_line(x, 39, x, 45, fill="#668793", width=1)
                sec = duration * fraction
                canvas.create_text(x, 55, text=self._fmt_time(sec), fill="#9BB6BF",
                                   font=("Segoe UI", 8), anchor="center")
            for x, color, tag in ((x1, "#78D5D8", "in_handle"), (x2, "#FFFFFF", "out_handle")):
                canvas.create_line(x, 12, x, 42, fill=color, width=3, tags=(tag,))
                canvas.create_oval(x-6, 19, x+6, 31, fill=color, outline="#0B202D", width=1,
                                   tags=(tag,))
            self.timeline_info_var.set(
                f"Đang chọn  {self._fmt_time(start)}  →  {self._fmt_time(end)}   ·   {max(0, end-start):.1f} giây / {self._fmt_time(duration)}"
            )
        except Exception:
            pass

    def _timeline_x_to_seconds(self, x: float) -> float:
        duration = self._timeline_duration()
        w = max(220, self.timeline_canvas.winfo_width())
        left, right = 27.0, float(w - 27)
        if duration <= 0 or right <= left:
            return 0.0
        return max(0.0, min(duration, (x-left) / (right-left) * duration))

    def _timeline_press(self, event):
        if not self.info or self.busy:
            return
        try:
            start = float(self.segment_start_var.get() or 0)
        except Exception:
            start = 0.0
        try:
            end = float(self.segment_end_var.get() or self._timeline_duration())
        except Exception:
            end = self._timeline_duration()
        x_start = 27 + (self.timeline_canvas.winfo_width()-54) * start / max(0.001, self._timeline_duration())
        x_end = 27 + (self.timeline_canvas.winfo_width()-54) * end / max(0.001, self._timeline_duration())
        self._timeline_drag_handle = "start" if abs(event.x-x_start) <= abs(event.x-x_end) else "end"
        self._timeline_drag(event)

    def _timeline_drag(self, event):
        if not getattr(self, "_timeline_drag_handle", None) or self.busy:
            return
        value = self._timeline_x_to_seconds(event.x)
        if self._timeline_drag_handle == "start":
            try:
                end = float(self.segment_end_var.get() or self._timeline_duration())
            except Exception:
                end = self._timeline_duration()
            value = min(value, max(0.0, end - 0.1))
            self.segment_start_var.set(f"{value:.2f}")
        else:
            try:
                start = float(self.segment_start_var.get() or 0)
            except Exception:
                start = 0.0
            value = max(value, start + 0.1)
            self.segment_end_var.set(f"{min(value, self._timeline_duration()):.2f}")
        self._timeline_redraw()

    def _timeline_release(self, _event=None):
        self._timeline_drag_handle = None

    def _on_close(self):
        if self.busy:
            answer = messagebox.askyesno(
                "Đang xuất video",
                "Tool đang xuất video. Bạn có muốn dừng tác vụ và đóng ứng dụng không?"
            )
            if not answer:
                return
            self.stop_export()
            self.root.after(400, self._close_when_idle)
            return
        if self.autosave_enabled_var.get() and self.project_dirty:
            self._autosave_now()
        if not self._confirm_unsaved_project():
            return
        for after_id in (self._autosave_after_id, self._autosave_periodic_id):
            if after_id:
                try:
                    self.root.after_cancel(after_id)
                except Exception:
                    pass
        self.root.destroy()

    def _close_when_idle(self):
        if self.busy:
            self.root.after(250, self._close_when_idle)
        else:
            self._on_close()

    def _scroll_controls(self, event):
        """Scroll only the currently active inspector tab."""
        canvas = getattr(self, "active_controls_canvas", getattr(self, "controls_canvas", None))
        if canvas is not None:
            delta = -1 if getattr(event, "delta", 0) > 0 else 1
            units = max(1, abs(int(getattr(event, "delta", 120) / 120)))
            try:
                canvas.yview_scroll(delta * units, "units")
            except Exception:
                pass

    def _draw_placeholder(self):
        try:
            cw = max(1, self.canvas.winfo_width())
            ch = max(1, self.canvas.winfo_height())
        except Exception:
            cw, ch = self.canvas_w, self.canvas_h
        self.canvas.delete("all")
        self.canvas.create_text(cw / 2, ch / 2 - 12,
                                text="Chọn video MP4 để hiển thị khung hình",
                                width=max(180, cw - 36), justify="center",
                                fill="#9BB6BF", font=("Segoe UI", 14, "bold"))
        self.canvas.create_text(cw / 2, ch / 2 + 22,
                                text="Sau đó khoanh sơ bộ watermark bằng chuột",
                                width=max(180, cw - 36), justify="center",
                                fill="#668793", font=("Segoe UI", 10))

    def choose_ffmpeg(self):
        path = filedialog.askopenfilename(
            title="Chọn ffmpeg.exe (nên chọn bản có NVENC)",
            filetypes=[("FFmpeg executable", "ffmpeg.exe"), ("All files", "*.*")]
        )
        if not path:
            return
        path = str(Path(path).resolve())
        try:
            version = subprocess.run([path, "-version"], capture_output=True, text=True,
                                     timeout=10, check=False, **_hidden_subprocess_options())
            if version.returncode != 0:
                raise RuntimeError((version.stderr or version.stdout or "Không chạy được FFmpeg.")[-600:])
            if not (ffmpeg_has_encoder(path, "h264_nvenc") or ffmpeg_has_encoder(path, "libx264")):
                raise RuntimeError("FFmpeg này không có h264_nvenc hoặc libx264. Hãy chọn ffmpeg.exe từ bản full build.")
        except Exception as exc:
            messagebox.showerror("FFmpeg không hợp lệ", str(exc))
            return
        try:
            settings = load_app_settings()
            settings["ffmpeg_path"] = path
            save_app_settings(settings)
            get_ffmpeg.cache_clear()
            ffmpeg_has_encoder.cache_clear()
            ffmpeg_has_decoder.cache_clear()
            ffmpeg_has_hwaccel.cache_clear()
            probe_nvenc.cache_clear()
            probe_nvenc_result.cache_clear()
            _ffmpeg_has_option.cache_clear()
            selected = get_ffmpeg()
            nvenc_ok = probe_nvenc(selected)
            self.ffmpeg_path = path
            self.ffmpeg_var.set(Path(path).name)
            self.ffmpeg_status_var.set(
                f"{Path(path).name} · " + ("NVIDIA NVENC sẵn sàng" if nvenc_ok else "NVENC chưa khởi tạo được")
            )
            if nvenc_ok:
                messagebox.showinfo("Đã chọn FFmpeg", "FFmpeg đã được lưu và kiểm tra NVENC thành công. Lần xuất tiếp theo sẽ ưu tiên NVIDIA NVENC.")
            else:
                messagebox.showwarning(
                    "FFmpeg đã được lưu",
                    "FFmpeg chạy được nhưng NVENC chưa khởi tạo thành công. Tool vẫn có thể xuất bằng CPU x264.\n\n"
                    "Hãy dùng nút Kiểm tra GPU để xem chi tiết."
                )
        except Exception as exc:
            messagebox.showerror("Không lưu được FFmpeg", str(exc))

    def open_ffmpeg_download_page(self):
        webbrowser.open("https://www.gyan.dev/ffmpeg/builds/")

    def reset_ffmpeg(self):
        try:
            settings = load_app_settings()
            settings.pop("ffmpeg_path", None)
            save_app_settings(settings)
            self.ffmpeg_path = ""
            get_ffmpeg.cache_clear()
            ffmpeg_has_encoder.cache_clear()
            ffmpeg_has_decoder.cache_clear()
            ffmpeg_has_hwaccel.cache_clear()
            probe_nvenc.cache_clear()
            probe_nvenc_result.cache_clear()
            _ffmpeg_has_option.cache_clear()
            selected = get_ffmpeg()
            nvenc_ok = probe_nvenc(selected)
            self.ffmpeg_var.set("Tự động chọn FFmpeg")
            self.ffmpeg_status_var.set("NVENC sẵn sàng" if nvenc_ok else "Tự động · hiện dùng CPU x264")
        except Exception as exc:
            messagebox.showerror("Không đặt lại được FFmpeg", str(exc))

    def check_gpu_status(self):
        def worker():
            try:
                ffmpeg = get_ffmpeg()
                has_nvenc = ffmpeg_has_encoder(ffmpeg, "h264_nvenc")
                nvenc_ok, nvenc_detail = probe_nvenc_result(ffmpeg)
                has_dav1d = ffmpeg_has_decoder(ffmpeg, "libdav1d")
                message = (
                    f"FFmpeg đang dùng:\n{ffmpeg}\n\n"
                    f"h264_nvenc có trong build: {'Có' if has_nvenc else 'Không'}\n"
                    f"NVENC khởi tạo được: {'Có' if nvenc_ok else 'Không'}\n"
                    f"AV1 decoder libdav1d: {'Có' if has_dav1d else 'Không'}\n\n"
                    f"Chi tiết test NVENC 720p/30: {nvenc_detail}\n\n"
                    + ("Kết luận: có thể mã hóa bằng NVIDIA NVENC." if nvenc_ok else
                       "Kết luận: hiện tại tool sẽ dùng CPU x264. Hãy cập nhật driver/kiểm tra quyền GPU hoặc chọn FFmpeg khác nếu cần.")
                )
                self.root.after(0, lambda m=message, ok=nvenc_ok: self._show_gpu_status(m, ok))
            except Exception as exc:
                self.root.after(0, lambda e=str(exc): messagebox.showerror("Kiểm tra GPU", e))
        threading.Thread(target=worker, daemon=True).start()
        self.ffmpeg_status_var.set("Đang kiểm tra FFmpeg / GPU…")

    def _show_gpu_status(self, message: str, nvenc_ok: bool):
        selected_name = Path(get_ffmpeg()).name
        self.ffmpeg_status_var.set(
            f"{selected_name} · " + ("NVIDIA NVENC sẵn sàng" if nvenc_ok else "Hiện dùng CPU x264")
        )
        messagebox.showinfo("Chẩn đoán FFmpeg / GPU", message)

    def load_video_path(self, path: str) -> bool:
        """Load a video from an explicit path without opening a file dialog.

        This is also used by the Qt bridge so Unicode Windows paths travel as
        JSON data, not through a Tk file-dialog monkey patch.
        """
        if self.busy:
            return False
        if self.video_path and not self._confirm_unsaved_project():
            return False
        path = str(Path(path).expanduser())
        if not Path(path).is_file():
            raise FileNotFoundError(f"Không tìm thấy video nguồn: {path}")

        info = read_video_info(path)
        self.video_path = path
        self.info = info
        # Preserve the user's chosen logo styling when switching source videos,
        # but reset the visibility range to the new video's duration.
        self.branding_config = {**default_branding_config(info.get("duration", 0.0)), **self.branding_config}
        self.branding_config["show_from"] = 0.0
        self.branding_config["show_to"] = float(info.get("duration", 0.0))
        self.current_project_path = ""
        self._autosave_recovery_path = ""
        self.scenes = []
        self.excluded_ranges = []
        self._refresh_scene_list()
        self.selected_scene_index = None
        if not self.output_dir_custom or not self.output_dir:
            output_folder = default_output_directory(path)
            output_folder.mkdir(parents=True, exist_ok=True)
            self.output_dir = str(output_folder)
            self.output_dir_var.set(self.output_dir)
        self.video_var.set(Path(path).name)
        self.meta_var.set(f"{info['width']}×{info['height']}  •  {info['fps']:.3f} FPS  •  {self._fmt_time(info['duration'])}  •  {info.get('codec', 'video')}")
        self.preview_start_var.set("0")
        self.segment_start_var.set("0")
        self.segment_end_var.set(f"{info['duration']:.3f}".rstrip("0").rstrip("."))
        self.segment_from_minutes_var.set("0")
        self.segment_to_minutes_var.set(f"{info['duration'] / 60.0:.3f}".rstrip("0").rstrip("."))
        self._timeline_redraw()
        self.frame_time_var.set("0")
        self.mask = None
        self.lasso_points = []
        self.canvas_points = []
        self.load_frame()
        self._mark_project_dirty()
        self.status_var.set("Đã nạp video. Hãy tải khung hình có watermark rồi khoanh vùng.")
        return True

    def choose_video(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(
            title="Chọn video",
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            self.load_video_path(path)
        except Exception as exc:
            messagebox.showerror("Không mở được video", str(exc))

    def choose_logo(self):
        # Build the picker from the formats supported by the installed Pillow build.
        # This includes common raster formats such as PNG/JPEG/WEBP/BMP/TIFF/GIF/ICO.
        try:
            Image.init()
            supported = sorted(
                ext for ext, fmt in Image.registered_extensions().items()
                if fmt in Image.OPEN
            )
        except Exception:
            supported = [".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".ico"]
        patterns = " ".join(f"*{ext}" for ext in supported)
        path = filedialog.askopenfilename(
            title="Chọn logo (các định dạng ảnh được hỗ trợ)",
            filetypes=[("Tất cả định dạng ảnh hỗ trợ", patterns),
                       ("PNG", "*.png"), ("JPEG", "*.jpg *.jpeg *.jfif"),
                       ("WEBP", "*.webp"), ("BMP", "*.bmp *.dib"),
                       ("TIFF", "*.tif *.tiff"), ("GIF (lấy khung đầu)", "*.gif"),
                       ("ICO", "*.ico"), ("Tất cả tệp", "*.*")]
        )
        if not path:
            return
        try:
            with Image.open(path) as image:
                # Animated GIF/multi-page images use their first frame as the logo.
                try:
                    image.seek(0)
                except Exception:
                    pass
                image.convert("RGBA").load()
        except Exception as exc:
            messagebox.showerror(
                "Không đọc được ảnh logo",
                "Định dạng ảnh này chưa được Pillow hỗ trợ hoặc file bị lỗi.\n"
                "Hãy dùng PNG, JPEG, WEBP, BMP, TIFF, GIF hoặc ICO.\n\n" + str(exc)
            )
            return
        self.logo_path = path
        self.logo_var.set(Path(path).name)
        self._mark_project_dirty()
        self.status_var.set("Đã chọn logo mới.")
        self.refresh_canvas()

    def choose_output_dir(self):
        initial = self.output_dir or (str(Path(self.video_path).resolve().parent) if self.video_path else str(Path.home()))
        path = filedialog.askdirectory(title="Chọn thư mục lưu video đã xuất", initialdir=initial, mustexist=True)
        if path:
            self.output_dir = str(Path(path).resolve())
            self.output_dir_custom = True
            self.output_dir_var.set(self.output_dir)
            self._mark_project_dirty()
            self.status_var.set(f"Thư mục xuất đã chọn: {self.output_dir}")

    def open_output_folder(self, output_path: Optional[str] = None):
        """Open the output folder in Windows Explorer (or the platform file manager)."""
        target = Path(output_path).resolve().parent if output_path else (
            Path(self.output_dir).resolve() if self.output_dir else
            (Path(self.video_path).resolve().parent if self.video_path else Path.home())
        )
        if not target.exists():
            messagebox.showwarning("Chưa có thư mục", f"Thư mục chưa tồn tại:\n{target}")
            return
        try:
            if os.name == "nt":
                os.startfile(str(target))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except Exception as exc:
            messagebox.showerror("Không mở được thư mục", f"{target}\n\n{exc}")

    def _open_folder_after_success(self, output: str):
        if self.auto_open_output_var.get():
            self.open_output_folder(output)

    def show_full_frame(self):
        """Open a separate fit-to-window viewer so the whole source frame is visible."""
        if self.frame is None:
            messagebox.showinfo("Chưa có khung hình", "Hãy chọn video và tải khung hình trước.")
            return
        win = tk.Toplevel(self.root)
        win.title("Xem toàn khung hình – Góc Review Phim")
        win.geometry("1280x820")
        win.minsize(800, 520)
        win.configure(bg="#06111B")

        header = ttk.Frame(win, style="Panel.TFrame", padding=(14, 10))
        header.pack(fill="x", padx=12, pady=(12, 6))
        h, w = self.frame.shape[:2]
        ttk.Label(header, text=f"TOÀN KHUNG HÌNH  •  {w} × {h}  •  TỶ LỆ {w / max(1, h):.3f}:1",
                  style="PanelTitle.TLabel").pack(side="left")
        ttk.Button(header, text="ĐÓNG", command=win.destroy, style="Secondary.TButton").pack(side="right")

        canvas = tk.Canvas(win, bg="#030A10", highlightthickness=1,
                           highlightbackground="#285365")
        canvas.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        display = self.frame.copy()
        if self.mask is not None and self.show_mask.get():
            expanded = self.mask
            pad = self._effective_mask_expansion()
            if pad > 0:
                k = pad * 2 + 1
                expanded = cv2.dilate(expanded, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
            tint = np.zeros_like(display)
            tint[:, :, 2] = 255
            alpha = (expanded > 0)[:, :, None].astype(np.float32) * 0.38
            display = np.clip(display.astype(np.float32) * (1 - alpha) + tint.astype(np.float32) * alpha,
                              0, 255).astype(np.uint8)
        rgb = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
        source_image = Image.fromarray(rgb)
        photo_holder = {"photo": None}

        def redraw(_event=None):
            cw, ch = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
            scale = min(cw / source_image.width, ch / source_image.height)
            dw = max(1, int(source_image.width * scale))
            dh = max(1, int(source_image.height * scale))
            shown = source_image.resize((dw, dh), Image.Resampling.LANCZOS)
            photo_holder["photo"] = ImageTk.PhotoImage(shown)
            canvas.delete("all")
            canvas.create_image((cw - dw) / 2, (ch - dh) / 2,
                                anchor="nw", image=photo_holder["photo"])
            canvas.create_text(12, 12, anchor="nw", fill="#EAF4F5",
                               text="Đang hiển thị TOÀN BỘ khung hình – ảnh được thu vừa cửa sổ, không crop.",
                               font=("Segoe UI", 9, "bold"))

        canvas.bind("<Configure>", redraw)
        win.bind("<Escape>", lambda _e: win.destroy())
        win.after(80, redraw)
        self._present_engine_window(win)

    def open_merge_dialog(self):
        """Open a separate, simple dialog for joining multiple video files."""
        if self.busy:
            messagebox.showinfo("Đang xử lý", "Hãy chờ tác vụ hiện tại hoàn tất hoặc dừng trước khi ghép video.")
            return
        if self.merge_window is not None and self.merge_window.winfo_exists():
            self.merge_window.lift()
            self.merge_window.focus_force()
            return
        win = tk.Toplevel(self.root)
        self.merge_window = win
        win.title("Ghép nhiều video – Góc Review Phim")
        win.geometry("760x610")
        win.minsize(650, 520)
        win.configure(bg="#06111B")
        win.grab_set()
        win.protocol("WM_DELETE_WINDOW", self._close_merge_dialog)

        shell = ttk.Frame(win, style="Panel.TFrame", padding=16)
        shell.pack(fill="both", expand=True, padx=12, pady=12)
        ttk.Label(shell, text="GHÉP NHIỀU VIDEO", style="PanelTitle.TLabel").pack(anchor="w")
        ttk.Label(shell,
                  text="Thêm ít nhất 2 clip, sắp xếp theo thứ tự mong muốn. Nếu thông số khác nhau, tool sẽ tự chuẩn hóa và mã hóa lại.",
                  style="Hint.TLabel", wraplength=700).pack(anchor="w", pady=(5, 12))

        list_area = ttk.Frame(shell, style="Card.TFrame", padding=8)
        list_area.pack(fill="both", expand=True)
        self.merge_listbox = tk.Listbox(list_area, selectmode=tk.EXTENDED, height=12,
                                        bg="#0B202D", fg="#EAF4F5",
                                        selectbackground="#247E9A", selectforeground="#FFFFFF",
                                        relief="flat", highlightthickness=1,
                                        highlightbackground="#285365", font=("Segoe UI", 9))
        list_scroll = ttk.Scrollbar(list_area, orient="vertical", command=self.merge_listbox.yview)
        self.merge_listbox.configure(yscrollcommand=list_scroll.set)
        self.merge_listbox.pack(side="left", fill="both", expand=True)
        list_scroll.pack(side="right", fill="y")

        row = ttk.Frame(shell, style="Panel.TFrame")
        row.pack(fill="x", pady=(9, 12))
        ttk.Button(row, text="Thêm video", command=self._merge_add_files,
                   style="Primary.TButton").pack(side="left")
        ttk.Button(row, text="Bỏ chọn", command=self._merge_remove_selected,
                   style="Secondary.TButton").pack(side="left", padx=(7, 0))
        ttk.Button(row, text="Lên", command=lambda: self._merge_move(-1),
                   style="Secondary.TButton").pack(side="left", padx=(7, 0))
        ttk.Button(row, text="Xuống", command=lambda: self._merge_move(1),
                   style="Secondary.TButton").pack(side="left", padx=(7, 0))
        ttk.Button(row, text="Xóa danh sách", command=self._merge_clear,
                   style="Secondary.TButton").pack(side="right")

        ttk.Label(shell, text="Tên file đầu ra", style="Panel.TLabel").pack(anchor="w")
        name_entry = ttk.Entry(shell, textvariable=self.merge_name_var)
        name_entry.pack(fill="x", pady=(4, 8), ipady=3)
        ttk.Label(shell, text="Thư mục lưu", style="Panel.TLabel").pack(anchor="w")
        folder_row = ttk.Frame(shell, style="Panel.TFrame")
        folder_row.pack(fill="x", pady=(4, 10))
        folder_entry = ttk.Entry(folder_row, textvariable=self.merge_folder_var)
        folder_entry.pack(side="left", fill="x", expand=True, ipady=3)
        ttk.Button(folder_row, text="Chọn thư mục…", command=self._merge_choose_folder,
                   style="Secondary.TButton").pack(side="left", padx=(8, 0))

        progress = ttk.Progressbar(shell, mode="determinate", maximum=100)
        progress.pack(fill="x", pady=(2, 5))
        self.merge_progress = progress
        status = ttk.Label(shell, textvariable=self.merge_status_var, style="Status.TLabel", wraplength=700)
        status.pack(anchor="w", pady=(0, 10))
        actions = ttk.Frame(shell, style="Panel.TFrame")
        actions.pack(fill="x")
        self.merge_start_button = ttk.Button(actions, text="GHÉP VIDEO", command=self._merge_start,
                                             style="Primary.TButton")
        self.merge_start_button.pack(side="left", fill="x", expand=True)
        self.merge_stop_button = ttk.Button(actions, text="DỪNG GHÉP", command=self.stop_export,
                                            style="Danger.TButton", state="disabled")
        self.merge_stop_button.pack(side="left", padx=(10, 0))

        initial_folder = self.output_dir or (str(Path(self.video_path).resolve().parent) if self.video_path else str(Path.home()))
        self.merge_folder_var.set(initial_folder)
        self.merge_files = []
        self._present_engine_window(win)

    def _merge_refresh_list(self):
        if not self.merge_listbox or not self.merge_listbox.winfo_exists():
            return
        self.merge_listbox.delete(0, tk.END)
        for i, path in enumerate(self.merge_files, 1):
            self.merge_listbox.insert(tk.END, f"{i:02d}. {Path(path).name}")

    def _merge_add_files(self):
        if self.busy:
            return
        paths = filedialog.askopenfilenames(
            parent=self.merge_window, title="Chọn các video cần ghép (theo thứ tự)",
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"), ("Tất cả tệp", "*.*")]
        )
        if not paths:
            return
        for p in paths:
            p = str(Path(p).resolve())
            if p not in self.merge_files:
                self.merge_files.append(p)
        self._merge_refresh_list()
        self.merge_status_var.set(f"Đã chọn {len(self.merge_files)} video. Dùng nút Lên/Xuống để đổi thứ tự.")

    def _merge_remove_selected(self):
        if self.busy or not self.merge_listbox:
            return
        selected = list(self.merge_listbox.curselection())
        for i in reversed(selected):
            del self.merge_files[i]
        self._merge_refresh_list()

    def _merge_clear(self):
        if self.busy:
            return
        self.merge_files.clear()
        self._merge_refresh_list()
        self.merge_status_var.set("Danh sách đã được xóa.")

    def _merge_move(self, direction: int):
        if not self.merge_listbox or self.busy:
            return
        selected = set(self.merge_listbox.curselection())
        if not selected:
            return
        moved = set(selected)
        if direction < 0:
            for i in sorted(selected):
                if i > 0 and (i - 1) not in moved:
                    self.merge_files[i - 1], self.merge_files[i] = self.merge_files[i], self.merge_files[i - 1]
                    moved.remove(i)
                    moved.add(i - 1)
        else:
            for i in sorted(selected, reverse=True):
                if i < len(self.merge_files) - 1 and (i + 1) not in moved:
                    self.merge_files[i + 1], self.merge_files[i] = self.merge_files[i], self.merge_files[i + 1]
                    moved.remove(i)
                    moved.add(i + 1)
        self._merge_refresh_list()
        for i in sorted(moved):
            self.merge_listbox.selection_set(i)

    def _merge_choose_folder(self):
        initial = self.merge_folder_var.get().strip() or str(Path.home())
        folder = filedialog.askdirectory(parent=self.merge_window, title="Chọn thư mục lưu video ghép",
                                         initialdir=initial, mustexist=True)
        if folder:
            self.merge_folder_var.set(str(Path(folder).resolve()))

    def _merge_start(self):
        if self.busy:
            return
        if len(self.merge_files) < 2:
            messagebox.showwarning("Thiếu video", "Hãy thêm ít nhất 2 video để ghép.", parent=self.merge_window)
            return
        folder = Path(self.merge_folder_var.get().strip()).expanduser()
        if not folder.is_dir():
            messagebox.showwarning("Thư mục không hợp lệ", "Hãy chọn một thư mục lưu video tồn tại.", parent=self.merge_window)
            return
        name = self.merge_name_var.get().strip()
        if not name:
            messagebox.showwarning("Thiếu tên file", "Hãy nhập tên file đầu ra.", parent=self.merge_window)
            return
        if not name.lower().endswith(".mp4"):
            name += ".mp4"
        if any(ch in name for ch in '<>:\"/|?*'):
            messagebox.showwarning("Tên file không hợp lệ", "Tên file chứa ký tự Windows không hỗ trợ.", parent=self.merge_window)
            return
        output = (folder / name).resolve()
        if str(output) in self.merge_files:
            messagebox.showwarning("Trùng file", "File đầu ra trùng với một video nguồn. Hãy đổi tên hoặc thư mục.", parent=self.merge_window)
            return
        overwrite_confirmed = False
        if output.exists():
            overwrite_confirmed = messagebox.askyesno("File đã tồn tại", f"Ghi đè file này?\n{output}", parent=self.merge_window)
            if not overwrite_confirmed:
                return
        try:
            total_input = sum(Path(p).stat().st_size for p in self.merge_files)
            free = shutil.disk_usage(folder).free
            if free < max(512 * 1024**2, int(total_input * 1.25)):
                messagebox.showwarning("Thiếu dung lượng", "Ổ đĩa có thể không đủ chỗ cho video ghép. Hãy chọn ổ còn trống nhiều hơn.", parent=self.merge_window)
                return
        except Exception:
            pass

        self.busy = True
        self.cancel_event = threading.Event()
        cancel_event = self.cancel_event
        input_snapshot = list(self.merge_files)
        prefer_gpu_snapshot = bool(self.prefer_gpu_var.get())
        self.stop_button.configure(state="normal")
        self.merge_start_button.configure(state="disabled")
        self.merge_stop_button.configure(state="normal")
        self.merge_progress.configure(value=0)
        self.merge_status_var.set("Đang chuẩn bị ghép video…")
        self._merge_processes_lock = self.process_lock
        with self.process_lock:
            self.active_processes.clear()

        def tracker(proc, add):
            if add:
                with self.process_lock:
                    self.active_processes.add(proc)
                    should_stop = cancel_event.is_set()
                if should_stop and proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            else:
                with self.process_lock:
                    self.active_processes.discard(proc)

        def report(pct, text):
            try:
                self.root.after(0, lambda p=pct, t=text: self._merge_progress_update(p, t))
            except tk.TclError:
                pass

        def worker():
            try:
                result = merge_videos(input_snapshot, str(output), report,
                                      cancel_event, tracker, prefer_gpu=prefer_gpu_snapshot,
                                      overwrite=overwrite_confirmed)
                if cancel_event.is_set():
                    raise ExportCancelled("Đã dừng ghép video.")
                self.root.after(0, lambda p=result: self._merge_finished(p))
            except ExportCancelled as exc:
                try:
                    self.root.after(0, lambda d=str(exc): self._merge_cancelled(d))
                except tk.TclError:
                    pass
            except Exception as exc:
                try:
                    self.root.after(0, lambda d=str(exc): self._merge_failed(d))
                except tk.TclError:
                    pass
        threading.Thread(target=worker, daemon=True).start()

    def _merge_progress_update(self, pct: float, text: str):
        if self.merge_progress and self.merge_progress.winfo_exists():
            self.merge_progress.configure(value=max(0, min(100, pct)))
        self.merge_status_var.set(text)

    def _merge_reset(self):
        self.busy = False
        self.cancel_event = None
        with self.process_lock:
            self.active_processes.clear()
        self.stop_button.configure(state="disabled")
        if self.merge_start_button and self.merge_start_button.winfo_exists():
            self.merge_start_button.configure(state="normal")
        if self.merge_stop_button and self.merge_stop_button.winfo_exists():
            self.merge_stop_button.configure(state="disabled")

    def _merge_finished(self, output: str):
        self._merge_reset()
        if self.merge_progress and self.merge_progress.winfo_exists():
            self.merge_progress.configure(value=100)
        self.merge_status_var.set(f"Đã ghép xong: {Path(output).name}")
        self._open_folder_after_success(output)
        folder_note = "\n\nĐã mở thư mục chứa video." if self.auto_open_output_var.get() else ""
        messagebox.showinfo("Ghép video hoàn tất", f"Đã ghép thành công:\n{output}{folder_note}", parent=self.merge_window)

    def _merge_cancelled(self, detail: str):
        self._merge_reset()
        self.merge_status_var.set("Đã dừng ghép. File tạm đã được xóa.")
        messagebox.showinfo("Đã dừng ghép", detail, parent=self.merge_window)

    def _merge_failed(self, detail: str):
        self._merge_reset()
        self.merge_status_var.set("Ghép video chưa thành công.")
        messagebox.showerror("Lỗi ghép video", detail, parent=self.merge_window)

    def _close_merge_dialog(self):
        if self.busy:
            messagebox.showinfo("Đang ghép video", "Hãy nhấn DỪNG GHÉP và đợi tác vụ kết thúc trước khi đóng cửa sổ.", parent=self.merge_window)
            return
        if self.merge_window is not None:
            self.merge_window.grab_release()
            self.merge_window.destroy()
        self.merge_window = None
        self.merge_listbox = None

    def _make_output_path(self, suffix: str) -> str:
        if not self.video_path:
            raise ValueError("Bạn hãy chọn video trước.")
        folder = Path(self.output_dir).expanduser() if self.output_dir else Path(self.video_path).resolve().parent
        folder.mkdir(parents=True, exist_ok=True)
        stem = Path(self.video_path).stem
        candidate = folder / f"{stem}_{suffix}.mp4"
        index = 2
        while candidate.exists():
            candidate = folder / f"{stem}_{suffix}_{index}.mp4"
            index += 1
        if candidate.resolve() == Path(self.video_path).resolve():
            raise ValueError("Đường dẫn đầu ra trùng với video gốc. Hãy chọn thư mục khác.")
        return str(candidate)

    def load_frame(self):
        if not self.video_path:
            messagebox.showinfo("Chưa có video", "Bạn hãy chọn video trước.")
            return
        try:
            seconds = safe_float(self.frame_time_var.get(), "Thời điểm khung hình")
            if self.info and self.info["duration"] > 0:
                seconds = min(seconds, max(0, self.info["duration"] - 0.05))
            self.frame = read_frame(self.video_path, seconds)
            self.frame_time_var.set(f"{seconds:.3f}".rstrip("0").rstrip("."))
            self.preview_seek_var.set(seconds)
            self.preview_seek_label_var.set(self._fmt_time(seconds))
            if self.info and self._preview_seek_scale is not None:
                try:
                    self._preview_seek_scale.configure(to=max(1.0, float(self.info.get("duration", 1.0))))
                except tk.TclError:
                    pass
            self.mask = None
            self.lasso_points = []
            self.canvas_points = []
            self._clear_mask_history()
            self.refresh_canvas()
            self._mark_project_dirty()
            self.status_var.set(f"Đã tải khung hình tại {self._fmt_time(seconds)}. Vẽ vòng bao quanh watermark.")
        except Exception as exc:
            messagebox.showerror("Không tải được khung hình", str(exc))

    def _canvas_geometry(self):
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        if self.frame is None:
            return 1.0, (0, 0), (cw, ch)
        h, w = self.frame.shape[:2]
        scale = min(cw / w, ch / h)
        dw, dh = max(1, int(w * scale)), max(1, int(h * scale))
        ox, oy = (cw - dw) / 2, (ch - dh) / 2
        return scale, (ox, oy), (dw, dh)

    def refresh_canvas(self):
        if self.frame is None:
            self._draw_placeholder()
            return
        scale, (ox, oy), (dw, dh) = self._canvas_geometry()
        self.display_scale, self.display_offset = scale, (ox, oy)
        preview = self.frame.copy()
        if self.mask is not None and self.show_mask.get():
            expansion = self._effective_mask_expansion()
            show = self.mask
            if expansion > 0:
                k = expansion * 2 + 1
                show = cv2.dilate(show, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
            tint = np.zeros_like(preview)
            tint[:, :, 2] = 255
            a = (show > 0)[:, :, None].astype(np.float32) * 0.38
            preview = np.clip(preview.astype(np.float32) * (1 - a) + tint.astype(np.float32) * a, 0, 255).astype(np.uint8)
        rgb = cv2.cvtColor(preview, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb).resize((dw, dh), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(pil)
        self.canvas.delete("all")
        self.canvas.create_image(ox, oy, anchor="nw", image=self.photo)
        # Draw the rough lasso to show the user's original selection.
        if self.canvas_points:
            flat = []
            for px, py in self.canvas_points:
                flat.extend([px, py])
            if len(flat) >= 4:
                flat.extend([self.canvas_points[0][0], self.canvas_points[0][1]])
                self.canvas.create_line(*flat, fill="#31C1CB", width=2, smooth=True)
        if self.auto_mask_expansion_var.get() and not self._mask_expansion_override:
            estimate = self._effective_mask_expansion()
            self.expansion_label.configure(text=f"Tự động đề xuất · {estimate} px (có thể chỉnh tay)")
        else:
            self.expansion_label.configure(text=f"Chỉnh tay · {self._effective_mask_expansion()} px")

    def _event_to_video(self, event) -> tuple[int, int]:
        scale, (ox, oy), (dw, dh) = self._canvas_geometry()
        x = int((event.x - ox) / scale)
        y = int((event.y - oy) / scale)
        h, w = self.frame.shape[:2]
        return max(0, min(w - 1, x)), max(0, min(h - 1, y))

    def _event_inside_image(self, event) -> bool:
        scale, (ox, oy), (dw, dh) = self._canvas_geometry()
        return ox <= event.x <= ox + dw and oy <= event.y <= oy + dh

    def set_mask_tool(self, mode: str):
        if mode not in {"lasso", "rect", "brush", "erase"}:
            return
        self.mask_tool_var.set(mode)
        cursor = "crosshair" if mode in {"lasso", "rect"} else "tcross"
        try:
            self.canvas.configure(cursor=cursor)
        except tk.TclError:
            pass
        names = {"lasso": "Khoanh tự do", "rect": "Khung chữ nhật", "brush": "Bút thêm mask", "erase": "Tẩy mask"}
        if mode in {"brush", "erase"} and self.mask is None:
            self.status_var.set("Hãy tạo mask trước, sau đó dùng Bút thêm hoặc Tẩy để tinh chỉnh.")
        else:
            self.status_var.set(f"Đã chọn công cụ: {names[mode]}.")

    def _schedule_mask_preview_refresh(self):
        # Throttle brush preview redraws to roughly 30 FPS so mouse events do not
        # repeatedly resize/reblend the full frame faster than the UI can paint.
        if self._mask_redraw_after_id is None:
            self._mask_redraw_after_id = self.root.after(32, self._run_mask_preview_refresh)

    def _run_mask_preview_refresh(self):
        self._mask_redraw_after_id = None
        self.refresh_canvas()

    def _mask_clip_region(self):
        """Return a cached polygon clip mask limited to the selected bounding box."""
        if self.frame is None or len(self.lasso_points) < 3:
            return None
        h, w = self.frame.shape[:2]
        points = tuple((int(x), int(y)) for x, y in self.lasso_points)
        key = ((h, w), points)
        if key == self._mask_clip_cache_key:
            return self._mask_clip_cache

        polygon = np.asarray(points, dtype=np.int32)
        x, y, bw, bh = cv2.boundingRect(polygon)
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(w, x + bw), min(h, y + bh)
        if x1 <= x0 or y1 <= y0:
            self._mask_clip_cache_key = key
            self._mask_clip_cache = None
            return None
        local = polygon - np.asarray([x0, y0], dtype=np.int32)
        allowed = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
        cv2.fillPoly(allowed, [local.reshape((-1, 1, 2))], 255)
        self._mask_clip_cache_key = key
        self._mask_clip_cache = (x0, y0, x1, y1, allowed)
        return self._mask_clip_cache

    def _paint_mask_at(self, x: int, y: int, erase: bool = False):
        if self.frame is None or self.mask is None:
            return
        radius = max(2, int(round(float(self.mask_brush_size_var.get()) / 2.0)))
        value = 0 if erase else 255
        # Keep the mask binary (0/255). Anti-aliased gray edge pixels can survive
        # an Eraser stroke because any non-zero pixel is still treated as mask.
        if self._brush_last is None:
            cv2.circle(self.mask, (x, y), radius, value, -1, lineType=cv2.LINE_8)
        else:
            cv2.line(self.mask, self._brush_last, (x, y), value, thickness=radius * 2,
                     lineType=cv2.LINE_8)
            cv2.circle(self.mask, (x, y), radius, value, -1, lineType=cv2.LINE_8)
        self._brush_last = (x, y)
        clip = self._mask_clip_region()
        if clip is not None:
            x0, y0, x1, y1, allowed = clip
            roi = self.mask[y0:y1, x0:x1]
            cv2.bitwise_and(roi, allowed, dst=roi)
        self._schedule_mask_preview_refresh()

    def _lasso_start(self, event):
        if self.frame is None or not self._event_inside_image(event) or self.busy:
            return
        x, y = self._event_to_video(event)
        mode = self.mask_tool_var.get()
        if mode in {"brush", "erase"}:
            if self.mask is None:
                self.status_var.set("Chưa có mask. Khoanh vùng và tạo mask trước khi dùng bút.")
                return
            self._brush_last = None
            if not self._brush_undo_pushed:
                self._push_mask_undo()
                self._brush_undo_pushed = True
            self._paint_mask_at(x, y, erase=(mode == "erase"))
            return
        self._push_mask_undo()
        self._selection_start = (x, y)
        self._selection_current = (x, y)
        self.lasso_points = [(x, y)]
        self.canvas_points = [(event.x, event.y)]
        self.mask = None
        self.canvas.delete("all")
        self.refresh_canvas()

    def _lasso_move(self, event):
        if self.frame is None or self.busy or not self._event_inside_image(event):
            return
        mode = self.mask_tool_var.get()
        x, y = self._event_to_video(event)
        if mode in {"brush", "erase"}:
            if self._brush_last is not None:
                self._paint_mask_at(x, y, erase=(mode == "erase"))
            return
        if self._selection_start is None:
            return
        if mode == "rect":
            self._selection_current = (x, y)
            self.canvas.delete("selection-preview")
            scale, (ox, oy), _size = self._canvas_geometry()
            sx, sy = self._selection_start
            self.canvas.create_rectangle(ox + sx * scale, oy + sy * scale,
                                          ox + x * scale, oy + y * scale,
                                          outline="#31C1CB", width=2, dash=(5, 3),
                                          tags="selection-preview")
        else:
            if not self.lasso_points or (x, y) != self.lasso_points[-1]:
                self.lasso_points.append((x, y))
                self.canvas_points.append((event.x, event.y))
                if len(self.canvas_points) >= 2:
                    a, b = self.canvas_points[-2], self.canvas_points[-1]
                    self.canvas.create_line(a[0], a[1], b[0], b[1], fill="#31C1CB", width=2, smooth=True)

    def _lasso_end(self, event):
        mode = self.mask_tool_var.get()
        if mode in {"brush", "erase"}:
            self._brush_last = None
            self._brush_undo_pushed = False
            if self.frame is not None and self.mask is not None:
                self._mark_project_dirty()
                self.status_var.set("Đã cập nhật mask. Kiểm tra vùng màu đỏ trước khi xuất.")
            return
        if self._selection_start is None:
            return
        if mode == "rect":
            if event is not None and self._event_inside_image(event):
                self._selection_current = self._event_to_video(event)
            sx, sy = self._selection_start
            ex, ey = self._selection_current or self._selection_start
            x0, x1 = sorted((sx, ex))
            y0, y1 = sorted((sy, ey))
            if x1 - x0 < 5 or y1 - y0 < 5:
                self.status_var.set("Vùng chữ nhật quá nhỏ. Hãy khoanh lại rộng hơn.")
                self._selection_start = self._selection_current = None
                return
            self.lasso_points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            scale, (ox, oy), _size = self._canvas_geometry()
            self.canvas_points = [(ox + x * scale, oy + y * scale) for x, y in self.lasso_points]
            self.refresh_canvas()
        self._selection_start = self._selection_current = None
        if self.lasso_points:
            self.refresh_canvas()
            self._mark_project_dirty()
            self.status_var.set(f"Đã khoanh vùng ({len(self.lasso_points)} điểm). Chọn Tự tách nét hoặc Dùng toàn bộ vùng khoanh.")

    def _effective_mask_expansion(self, mask=None) -> int:
        active_mask = self.mask if mask is None else mask
        if self.auto_mask_expansion_var.get() and not self._mask_expansion_override:
            return estimate_mask_expansion(active_mask)
        return max(0, min(30, int(round(float(self.expansion_var.get())))))

    def _on_expansion_manual_changed(self):
        # Auto remains a suggestion; moving the slider is an explicit user override.
        if self._setting_auto_expansion or self._loading_project:
            return
        self._mask_expansion_override = True
        self.refresh_canvas()
        if hasattr(self, "expansion_label"):
            self.expansion_label.configure(text=f"Chỉnh tay · {self._effective_mask_expansion()} px")

    def _on_mask_mode_changed(self, reset_override: bool = True):
        automatic = bool(self.auto_mask_expansion_var.get())
        if automatic and reset_override:
            self._mask_expansion_override = False
            suggestion = estimate_mask_expansion(self.mask)
            try:
                self._setting_auto_expansion = True
                self.expansion_var.set(suggestion)
            except Exception:
                pass
            finally:
                self._setting_auto_expansion = False
        elif not automatic and reset_override:
            self._mask_expansion_override = True
        try:
            self.expansion_scale.configure(state="normal")
        except Exception:
            pass
        self.refresh_canvas()

    def use_selection_mask(self):
        if self.frame is None or len(self.lasso_points) < 3:
            messagebox.showinfo("Chưa khoanh vùng", "Hãy khoanh tự do hoặc dùng khung chữ nhật quanh logo/banner trước.", parent=self.root)
            return
        self._push_mask_undo()
        self.mask = polygon_to_mask(self.frame.shape, self.lasso_points)
        self.show_mask.set(True)
        self.refresh_canvas()
        self._mark_project_dirty()
        self.status_var.set(f"Đã dùng toàn bộ vùng khoanh làm mask ({cv2.countNonZero(self.mask):,} pixel). Dùng Bút/Tẩy để tinh chỉnh nếu cần.")

    def auto_extract(self):
        if self.frame is None or len(self.lasso_points) < 3:
            messagebox.showinfo("Chưa khoanh vùng", "Bạn hãy khoanh vùng logo, chữ hoặc banner trước.", parent=self.root)
            return
        try:
            self._push_mask_undo()
            self.mask, used_fallback = extract_text_mask(self.frame, self.lasso_points, return_info=True)
            self.show_mask.set(True)
            self.refresh_canvas()
            count = cv2.countNonZero(self.mask)
            self._mark_project_dirty()
            if used_fallback:
                self.status_var.set(f"Không tách được nét riêng biệt; đã chuyển sang dùng toàn bộ vùng khoanh làm mask ({count:,} pixel). Kiểm tra màu đỏ và tinh chỉnh bằng Bút/Tẩy.")
            else:
                self.status_var.set(f"Đã tách mask gồm {count:,} pixel. Độ nới hiện tại: {self._effective_mask_expansion()} px. Hãy kiểm tra màu đỏ trước khi xuất.")
        except Exception as exc:
            # The only remaining errors are invalid selections or programming/runtime
            # issues. Never replace a meaningful validation error with a silent mask.
            messagebox.showwarning("Chưa tạo được mask", str(exc), parent=self.root)

    def clear_selection(self):
        self._push_mask_undo()
        self.lasso_points = []
        self.canvas_points = []
        self.mask = None
        self.refresh_canvas()
        self._mark_project_dirty()
        self.status_var.set("Đã xóa vùng khoanh và mask.")

    def _branding_presets_dir(self) -> Path:
        if os.name == "nt":
            base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        folder = base / "GocReviewPhim" / "branding_presets"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _branding_preset_files(self) -> list[Path]:
        try:
            return sorted(self._branding_presets_dir().glob("*.json"), key=lambda p: p.stem.casefold())
        except Exception:
            return []

    def open_branding_studio(self):
        if self.branding_studio_window is not None:
            try:
                self.branding_studio_window.lift()
                return
            except Exception:
                self.branding_studio_window = None
        if self.frame is None:
            messagebox.showinfo("Chưa có khung hình", "Hãy chọn video và tải một khung hình trước khi mở Studio Branding.")
            return

        win = tk.Toplevel(self.root)
        self.branding_studio_window = win
        win.title("Studio Branding · Logo & Nhãn")
        win.geometry("1240x790")
        win.minsize(1050, 680)
        win.configure(bg="#06111B")
        win.grab_set()
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_branding_studio(win))

        current_scene = (self.scenes[self.selected_scene_index]
                         if self.selected_scene_index is not None and 0 <= self.selected_scene_index < len(self.scenes)
                         else None)
        scene_scope_var = tk.BooleanVar(value=bool(current_scene and isinstance(current_scene.get("branding"), dict)))
        cfg = dict(current_scene.get("branding") or self.branding_config) if current_scene else dict(self.branding_config)
        cfg = {**default_branding_config(self._timeline_duration()), **cfg}
        logo_path_var = tk.StringVar(value=self.logo_path)
        logo_width_var = tk.DoubleVar(value=float(cfg.get("logo_width_pct", 10.5)))
        logo_opacity_var = tk.IntVar(value=int(cfg.get("logo_opacity", 100)))
        logo_rotation_var = tk.IntVar(value=int(cfg.get("logo_rotation", 0)))
        label_scale_var = tk.IntVar(value=int(cfg.get("label_scale", 100)))
        label_opacity_var = tk.IntVar(value=int(cfg.get("label_opacity", 100)))
        logo_enabled_var = tk.BooleanVar(value=bool(cfg.get("logo_enabled", True)))
        label_enabled_var = tk.BooleanVar(value=bool(cfg.get("label_enabled", True)))
        logo_anchor_var = tk.StringVar(value=str(cfg.get("logo_anchor", "top_right")))
        label_anchor_var = tk.StringVar(value=str(cfg.get("label_anchor", "top_center")))
        from_min_var = tk.StringVar(value=f"{float(cfg.get('show_from', 0.0))/60.0:.3f}".rstrip("0").rstrip("."))
        to_min_var = tk.StringVar(value=f"{float(cfg.get('show_to', self._timeline_duration()))/60.0:.3f}".rstrip("0").rstrip("."))
        preset_var = tk.StringVar(value="")
        text_color_var = tk.StringVar(value=str(cfg.get("label_text_color", "#FFFFFF")))
        bg_color_var = tk.StringVar(value=str(cfg.get("label_bg_color", "#990000")))
        label_text_preview_var = tk.StringVar(value=self._branding_text())
        dragging = {"active": False, "scale": 1.0, "offset": (0.0, 0.0), "bbox": None}

        outer = ttk.Frame(win, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(header, text="STUDIO BRANDING", font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Label(header, text="Kéo logo trực tiếp trên Preview · thay đổi sẽ áp dụng vào bản xuất",
                  foreground="#6A8794").pack(side="left", padx=18)

        body = ttk.Frame(outer)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        preview_panel = ttk.Frame(body, padding=10)
        preview_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        preview_panel.columnconfigure(0, weight=1)
        preview_panel.rowconfigure(1, weight=1)
        ttk.Label(preview_panel, text="PREVIEW BRANDING", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 8))
        preview_canvas = tk.Canvas(preview_panel, bg="#02080D", highlightthickness=1,
                                   highlightbackground="#285365", cursor="fleur")
        preview_canvas.grid(row=1, column=0, sticky="nsew")
        ttk.Label(preview_panel, text="Mẹo: kéo trực tiếp logo để đặt vị trí tùy chỉnh. Chọn ô vị trí để căn nhanh.",
                  foreground="#7895A0", wraplength=680).grid(row=2, column=0, sticky="w", pady=(8, 0))

        controls_shell = ttk.Frame(body, width=365)
        controls_shell.grid(row=0, column=1, sticky="ns")
        controls_shell.grid_propagate(False)
        controls_shell.rowconfigure(0, weight=1)
        controls_shell.columnconfigure(0, weight=1)
        controls_scroll = tk.Canvas(controls_shell, width=340, bg="#06111B", highlightthickness=0)
        controls_scroll.grid(row=0, column=0, sticky="nsew")
        controls_bar = ttk.Scrollbar(controls_shell, orient="vertical", command=controls_scroll.yview)
        controls_bar.grid(row=0, column=1, sticky="ns")
        controls_scroll.configure(yscrollcommand=controls_bar.set)
        controls = ttk.Frame(controls_scroll, padding=(12, 8))
        controls_window_id = controls_scroll.create_window((0, 0), window=controls, anchor="nw")
        controls.columnconfigure(0, weight=1)
        controls.bind("<Configure>", lambda _e: controls_scroll.configure(scrollregion=controls_scroll.bbox("all")))
        controls_scroll.bind("<Configure>", lambda e: controls_scroll.itemconfigure(controls_window_id, width=e.width))
        ttk.Label(controls, text="LOGO", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(controls, textvariable=logo_path_var, wraplength=300, foreground="#7895A0").grid(row=1, column=0, sticky="w", pady=(3, 4))

        def choose_local_logo():
            path = filedialog.askopenfilename(parent=win, title="Chọn logo",
                filetypes=[("Ảnh logo", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.gif *.ico"), ("Tất cả tệp", "*.*")])
            if not path:
                return
            try:
                with Image.open(path) as im:
                    im.seek(0); im.convert("RGBA").load()
                logo_path_var.set(path)
                redraw_preview()
            except Exception as exc:
                messagebox.showerror("Không đọc được logo", str(exc), parent=win)
        ttk.Button(controls, text="Chọn ảnh logo…", command=choose_local_logo).grid(row=2, column=0, sticky="ew")
        ttk.Checkbutton(controls, text="Hiển thị logo", variable=logo_enabled_var,
                        command=lambda: redraw_preview()).grid(row=3, column=0, sticky="w", pady=(5, 2))

        def add_scale(row, title, variable, low, high, command=None):
            ttk.Label(controls, text=title).grid(row=row, column=0, sticky="w", pady=(7, 0))
            scale = ttk.Scale(controls, from_=low, to=high, variable=variable,
                              command=(lambda _v: redraw_preview()) if command is None else command)
            scale.grid(row=row + 1, column=0, sticky="ew", pady=(1, 0))
            return scale
        add_scale(4, "Kích thước logo (%)", logo_width_var, 3, 40)
        add_scale(6, "Độ trong suốt logo (%)", logo_opacity_var, 0, 100)
        add_scale(8, "Xoay logo (độ)", logo_rotation_var, -180, 180)

        ttk.Label(controls, text="VỊ TRÍ LOGO (9 VỊ TRÍ)", font=("Segoe UI", 9, "bold")).grid(row=10, column=0, sticky="w", pady=(10, 4))
        anchors = [
            ("↖", "top_left"), ("↑", "top_center"), ("↗", "top_right"),
            ("←", "center_left"), ("◎", "center"), ("→", "center_right"),
            ("↙", "bottom_left"), ("↓", "bottom_center"), ("↘", "bottom_right"),
        ]
        pos_frame = ttk.Frame(controls)
        pos_frame.grid(row=11, column=0, sticky="ew")
        for col in range(3):
            pos_frame.columnconfigure(col, weight=1)
        def set_logo_anchor(anchor):
            cfg["logo_anchor"] = anchor
            logo_anchor_var.set(anchor)
            redraw_preview()
        for i, (symbol, anchor) in enumerate(anchors):
            ttk.Button(pos_frame, text=symbol, width=5,
                       command=lambda a=anchor: set_logo_anchor(a)).grid(row=i//3, column=i%3, sticky="ew", padx=2, pady=2)

        ttk.Separator(controls).grid(row=12, column=0, sticky="ew", pady=9)
        ttk.Label(controls, text="NHÃN TẬP / PHẦN", font=("Segoe UI", 9, "bold")).grid(row=13, column=0, sticky="w")
        ttk.Checkbutton(controls, text="Hiển thị nhãn", variable=label_enabled_var,
                        command=lambda: redraw_preview()).grid(row=14, column=0, sticky="w", pady=(3, 0))
        ttk.Label(controls, text="Nội dung nhãn (lấy từ tab NHÃN)", foreground="#7895A0").grid(row=15, column=0, sticky="w")
        ttk.Label(controls, textvariable=label_text_preview_var).grid(row=16, column=0, sticky="w", pady=(1, 2))
        add_scale(17, "Kích thước nhãn (%)", label_scale_var, 40, 180)
        add_scale(19, "Độ trong suốt nhãn (%)", label_opacity_var, 0, 100)
        ttk.Label(controls, text="Vị trí nhãn").grid(row=21, column=0, sticky="w", pady=(5, 0))
        anchor_labels = {"top_left":"Trên trái", "top_center":"Trên giữa", "top_right":"Trên phải",
                         "center_left":"Giữa trái", "center":"Chính giữa", "center_right":"Giữa phải",
                         "bottom_left":"Dưới trái", "bottom_center":"Dưới giữa", "bottom_right":"Dưới phải"}
        label_combo = ttk.Combobox(controls, state="readonly", values=list(anchor_labels.values()), width=20)
        label_combo.set(anchor_labels.get(label_anchor_var.get(), "Trên giữa"))
        label_combo.grid(row=22, column=0, sticky="ew", pady=(2, 0))
        def label_anchor_changed(_event=None):
            reverse = {v:k for k,v in anchor_labels.items()}
            cfg["label_anchor"] = reverse.get(label_combo.get(), "top_center")
            label_anchor_var.set(cfg["label_anchor"])
            redraw_preview()
        label_combo.bind("<<ComboboxSelected>>", label_anchor_changed)
        color_row = ttk.Frame(controls)
        color_row.grid(row=23, column=0, sticky="ew", pady=(5, 0))
        def choose_color(target_var, title):
            chosen = colorchooser.askcolor(color=target_var.get(), title=title, parent=win)
            if chosen and chosen[1]:
                target_var.set(chosen[1].upper())
                redraw_preview()
        ttk.Button(color_row, text="Màu chữ", command=lambda: choose_color(text_color_var, "Chọn màu chữ")).pack(side="left", expand=True, fill="x", padx=(0, 3))
        ttk.Button(color_row, text="Màu nền", command=lambda: choose_color(bg_color_var, "Chọn màu nền")).pack(side="left", expand=True, fill="x", padx=(3, 0))

        ttk.Separator(controls).grid(row=24, column=0, sticky="ew", pady=8)
        ttk.Label(controls, text="THỜI GIAN HIỂN THỊ", font=("Segoe UI", 9, "bold")).grid(row=25, column=0, sticky="w")
        time_row = ttk.Frame(controls)
        time_row.grid(row=26, column=0, sticky="ew", pady=(4, 0))
        time_row.columnconfigure((0,1), weight=1)
        ttk.Label(time_row, text="Từ phút").grid(row=0, column=0, sticky="w")
        ttk.Label(time_row, text="Đến phút").grid(row=0, column=1, sticky="w")
        ttk.Entry(time_row, textvariable=from_min_var).grid(row=1, column=0, sticky="ew", padx=(0,4))
        ttk.Entry(time_row, textvariable=to_min_var).grid(row=1, column=1, sticky="ew", padx=(4,0))
        ttk.Label(controls, text="Áp dụng cho cả logo và nhãn trong khoảng này.", foreground="#7895A0", wraplength=300).grid(row=27, column=0, sticky="w", pady=(3, 0))

        ttk.Separator(controls).grid(row=28, column=0, sticky="ew", pady=8)
        ttk.Label(controls, text="PRESET BRANDING", font=("Segoe UI", 9, "bold")).grid(row=29, column=0, sticky="w")
        preset_files = self._branding_preset_files()
        preset_combo = ttk.Combobox(controls, textvariable=preset_var, state="readonly",
                                    values=[x.stem for x in preset_files])
        preset_combo.grid(row=30, column=0, sticky="ew", pady=(4, 4))
        preset_btns = ttk.Frame(controls)
        preset_btns.grid(row=31, column=0, sticky="ew")
        preset_btns.columnconfigure((0,1,2), weight=1)

        def config_from_vars():
            result = dict(cfg)
            result.update({
                "logo_enabled": bool(logo_enabled_var.get()), "label_enabled": bool(label_enabled_var.get()),
                "logo_anchor": logo_anchor_var.get(), "logo_width_pct": float(logo_width_var.get()),
                "logo_opacity": int(float(logo_opacity_var.get())), "logo_rotation": int(float(logo_rotation_var.get())),
                "label_scale": int(float(label_scale_var.get())), "label_opacity": int(float(label_opacity_var.get())),
                "label_anchor": label_anchor_var.get(), "label_text_color": text_color_var.get(),
                "label_bg_color": bg_color_var.get(),
                "show_from": max(0.0, float(from_min_var.get().replace(",", ".")) * 60.0),
                "show_to": max(0.0, float(to_min_var.get().replace(",", ".")) * 60.0),
            })
            return result

        def set_config(new_cfg, new_logo=None):
            nonlocal cfg
            cfg = {**default_branding_config(self._timeline_duration()), **new_cfg}
            logo_width_var.set(float(cfg.get("logo_width_pct", 10.5)))
            logo_opacity_var.set(int(cfg.get("logo_opacity", 100)))
            logo_rotation_var.set(int(cfg.get("logo_rotation", 0)))
            label_scale_var.set(int(cfg.get("label_scale", 100)))
            label_opacity_var.set(int(cfg.get("label_opacity", 100)))
            logo_enabled_var.set(bool(cfg.get("logo_enabled", True)))
            label_enabled_var.set(bool(cfg.get("label_enabled", True)))
            logo_anchor_var.set(str(cfg.get("logo_anchor", "top_right")))
            label_anchor_var.set(str(cfg.get("label_anchor", "top_center")))
            label_combo.set(anchor_labels.get(label_anchor_var.get(), "Trên giữa"))
            from_min_var.set(f"{float(cfg.get('show_from',0))/60:.3f}".rstrip("0").rstrip("."))
            to_min_var.set(f"{float(cfg.get('show_to',self._timeline_duration()))/60:.3f}".rstrip("0").rstrip("."))
            text_color_var.set(str(cfg.get("label_text_color", "#FFFFFF")))
            bg_color_var.set(str(cfg.get("label_bg_color", "#990000")))
            if new_logo:
                logo_path_var.set(new_logo)
            redraw_preview()

        def refresh_presets():
            files = self._branding_preset_files()
            preset_combo.configure(values=[x.stem for x in files])

        def save_preset():
            name = simpledialog.askstring("Lưu Preset Branding", "Đặt tên cho preset:", parent=win)
            if not name:
                return
            safe = re.sub(r"[^\w\- ]+", "", name, flags=re.UNICODE).strip().replace(" ", "_")[:60]
            if not safe:
                messagebox.showwarning("Tên preset không hợp lệ", "Hãy nhập tên preset khác.", parent=win)
                return
            folder = self._branding_presets_dir()
            try:
                current_cfg = config_from_vars()
            except Exception as exc:
                messagebox.showwarning("Thiết lập thời gian chưa hợp lệ", f"Hãy kiểm tra mốc Từ phút / Đến phút.\n\n{exc}", parent=win)
                return
            payload = {"format": "goc-review-phim-branding-preset", "version": 1,
                       "branding": current_cfg, "logo_file": ""}
            source_logo = Path(logo_path_var.get()) if logo_path_var.get() else None
            if source_logo and source_logo.is_file():
                logo_dir = folder / "assets"
                logo_dir.mkdir(parents=True, exist_ok=True)
                target_logo = logo_dir / f"{safe}{source_logo.suffix.lower()}"
                try:
                    shutil.copy2(source_logo, target_logo)
                    payload["logo_file"] = str(target_logo)
                except Exception as exc:
                    messagebox.showerror("Không sao chép được logo", str(exc), parent=win)
                    return
            target = folder / f"{safe}.json"
            try:
                target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                preset_var.set(safe)
                refresh_presets()
                messagebox.showinfo("Đã lưu preset", f"Đã lưu preset: {safe}", parent=win)
            except Exception as exc:
                messagebox.showerror("Không lưu được preset", str(exc), parent=win)

        def load_preset():
            name = preset_var.get().strip()
            if not name:
                return
            target = self._branding_presets_dir() / f"{name}.json"
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
                if payload.get("format") != "goc-review-phim-branding-preset":
                    raise ValueError("File preset không đúng định dạng.")
                new_logo = str(payload.get("logo_file", ""))
                if not new_logo or not Path(new_logo).is_file():
                    new_logo = logo_path_var.get()
                set_config(payload.get("branding", {}), new_logo)
            except Exception as exc:
                messagebox.showerror("Không nạp được preset", str(exc), parent=win)

        def delete_preset():
            name = preset_var.get().strip()
            if not name:
                return
            target = self._branding_presets_dir() / f"{name}.json"
            if messagebox.askyesno("Xóa preset", f"Xóa preset {name}?", parent=win):
                target.unlink(missing_ok=True)
                refresh_presets()
                preset_var.set("")

        ttk.Button(preset_btns, text="Lưu", command=save_preset).grid(row=0, column=0, sticky="ew", padx=2)
        ttk.Button(preset_btns, text="Nạp", command=load_preset).grid(row=0, column=1, sticky="ew", padx=2)
        ttk.Button(preset_btns, text="Xóa", command=delete_preset).grid(row=0, column=2, sticky="ew", padx=2)

        # Scene scope is optional and only available when a scene is selected.
        if current_scene is not None:
            ttk.Checkbutton(controls, text=f"Chỉ áp dụng cho {current_scene.get('name','cảnh đang chọn')}",
                            variable=scene_scope_var).grid(row=32, column=0, sticky="w", pady=(7, 0))
        else:
            ttk.Label(controls, text="Có thể lưu preset để dùng lại ở các dự án khác.", foreground="#7895A0").grid(row=32, column=0, sticky="w", pady=(7, 0))

        def compose_preview():
            source = self.frame
            h, w = source.shape[:2]
            local_cfg = config_from_vars()
            logo_layer, label_layer = build_branding_components(w, h, logo_path_var.get(), self._branding_text(), local_cfg)
            base = Image.fromarray(cv2.cvtColor(source, cv2.COLOR_BGR2RGB)).convert("RGBA")
            try:
                preview_time = float(self.frame_time_var.get() or 0)
            except Exception:
                preview_time = 0.0
            is_visible = float(local_cfg.get("show_from", 0.0)) <= preview_time < float(local_cfg.get("show_to", float("inf")))
            if is_visible:
                logo_rgb = logo_layer.copy(); logo_rgb[:, :, :3] = cv2.cvtColor(logo_rgb[:, :, :3], cv2.COLOR_BGR2RGB)
                label_rgb = label_layer.copy(); label_rgb[:, :, :3] = cv2.cvtColor(label_rgb[:, :, :3], cv2.COLOR_BGR2RGB)
                base.alpha_composite(Image.fromarray(logo_rgb, "RGBA"))
                base.alpha_composite(Image.fromarray(label_rgb, "RGBA"))
            else:
                logo_layer[:, :, 3] = 0
            return base.convert("RGB"), logo_layer

        preview_photo = {"image": None}
        def redraw_preview(*_args):
            try:
                if not win.winfo_exists():
                    return
                image, logo_layer = compose_preview()
                cw = max(100, preview_canvas.winfo_width())
                ch = max(100, preview_canvas.winfo_height())
                scale = min(cw / image.width, ch / image.height)
                dw, dh = max(1, int(image.width * scale)), max(1, int(image.height * scale))
                ox, oy = (cw - dw) / 2, (ch - dh) / 2
                image = image.resize((dw, dh), Image.Resampling.LANCZOS)
                preview_photo["image"] = ImageTk.PhotoImage(image)
                preview_canvas.delete("all")
                preview_canvas.create_image(ox, oy, anchor="nw", image=preview_photo["image"])
                alpha = logo_layer[:, :, 3]
                ys, xs = np.where(alpha > 4)
                bbox = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1) if xs.size else None
                dragging.update({"scale": scale, "offset": (ox, oy), "bbox": bbox})
                if bbox:
                    x0, y0, x1, y1 = bbox
                    preview_canvas.create_rectangle(ox+x0*scale, oy+y0*scale, ox+x1*scale, oy+y1*scale,
                                                    outline="#31C1CB", dash=(4, 3), width=1)
            except Exception:
                pass

        def on_logo_press(event):
            bbox = dragging.get("bbox")
            if not bbox or not logo_enabled_var.get():
                return
            scale = dragging["scale"]; ox, oy = dragging["offset"]
            x = (event.x - ox) / max(0.0001, scale)
            y = (event.y - oy) / max(0.0001, scale)
            x0,y0,x1,y1 = bbox
            dragging["active"] = x0 <= x <= x1 and y0 <= y <= y1
        def on_logo_drag(event):
            if not dragging.get("active"):
                return
            scale = dragging["scale"]; ox, oy = dragging["offset"]
            frame_h, frame_w = self.frame.shape[:2]
            cx = max(0, min(frame_w, (event.x - ox) / max(0.0001, scale)))
            cy = max(0, min(frame_h, (event.y - oy) / max(0.0001, scale)))
            cfg["logo_anchor"] = "custom"
            cfg["logo_x"] = cx / frame_w
            cfg["logo_y"] = cy / frame_h
            logo_anchor_var.set("custom")
            redraw_preview()
        def on_logo_release(_event=None):
            dragging["active"] = False
        preview_canvas.bind("<ButtonPress-1>", on_logo_press)
        preview_canvas.bind("<B1-Motion>", on_logo_drag)
        preview_canvas.bind("<ButtonRelease-1>", on_logo_release)
        preview_canvas.bind("<Configure>", redraw_preview)

        for variable in (logo_width_var, logo_opacity_var, logo_rotation_var, label_scale_var,
                         label_opacity_var, logo_enabled_var, label_enabled_var,
                         from_min_var, to_min_var, text_color_var, bg_color_var):
            variable.trace_add("write", lambda *_: redraw_preview())

        bottom = ttk.Frame(outer)
        bottom.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(bottom, text="Preset được lưu riêng trên máy; logo được sao chép vào thư mục preset để không mất đường dẫn.",
                  foreground="#7895A0").pack(side="left")
        def apply_and_close():
            try:
                new_cfg = config_from_vars()
                if new_cfg["show_to"] < new_cfg["show_from"]:
                    raise ValueError("Mốc 'Đến phút' phải lớn hơn hoặc bằng 'Từ phút'.")
                duration = self._timeline_duration()
                if duration and new_cfg["show_to"] > duration + 0.05:
                    raise ValueError(f"Mốc kết thúc vượt thời lượng video ({self._fmt_time(duration)}).")
                new_logo = logo_path_var.get()
                if new_cfg.get("logo_enabled") and (not new_logo or not Path(new_logo).is_file()):
                    raise ValueError("Logo đang bật nhưng không tìm thấy file ảnh.")
                if current_scene is not None and scene_scope_var.get():
                    current_scene["branding"] = new_cfg
                    current_scene["branding_logo_path"] = new_logo
                    self.status_var.set(f"Đã áp dụng Branding riêng cho {current_scene.get('name','phân cảnh')}.")
                else:
                    self.logo_path = new_logo
                    self.logo_var.set(Path(new_logo).name if new_logo else "Chưa chọn logo")
                    self.branding_config = new_cfg
                    self.status_var.set("Đã áp dụng cấu hình Studio Branding cho dự án.")
                self._mark_project_dirty()
                self.refresh_canvas()
                self._close_branding_studio(win)
            except Exception as exc:
                messagebox.showwarning("Không áp dụng được Branding", str(exc), parent=win)
        ttk.Button(bottom, text="HỦY", command=lambda: self._close_branding_studio(win)).pack(side="right", padx=(6, 0))
        ttk.Button(bottom, text="ÁP DỤNG", command=apply_and_close, style="Primary.TButton").pack(side="right")
        win.after(150, redraw_preview)
        self._present_engine_window(win)

    def _close_branding_studio(self, win):
        try:
            win.grab_release()
            win.destroy()
        except Exception:
            pass
        if self.branding_studio_window is win:
            self.branding_studio_window = None

    def _branding_text(self) -> str:
        """Return the exact badge string selected by the user."""
        label_type = (self.label_type_var.get() or "Tập").strip()
        value = (self.label_value_var.get() or "").strip()
        if label_type == "Tùy chỉnh":
            return value or "Review"
        return f"{label_type} {value or '1'}"

    def _render_preview_frame(self):
        if self.frame is None:
            return
        overlay = build_overlay(self.frame.shape[1], self.frame.shape[0], self.logo_path,
                                self._branding_text(), branding_config=self.branding_config)
        mask = self.mask
        frame = apply_mask_and_overlay(self.frame.copy(), mask, self._effective_mask_expansion(mask), overlay)
        self.mask = None
        self.canvas_points = []
        self.lasso_points = []
        self.show_mask.set(False)
        self.frame = frame
        self.refresh_canvas()
        self.status_var.set("Đang hiển thị khung hình mẫu sau khi xử lý. Tải lại khung hình để chỉnh mask.")

    def _validate_export(self, preview: bool):
        if not self.video_path or not self.info:
            raise ValueError("Bạn chưa chọn video.")
        start = safe_float(self.preview_start_var.get(), "Thời điểm bắt đầu") if preview else 0.0
        duration = None
        if preview:
            try:
                duration = int(self.preview_length_var.get())
            except Exception as exc:
                raise ValueError("Độ dài xem thử phải là số nguyên từ 10 đến 60.") from exc
            if not (PREVIEW_MIN <= duration <= PREVIEW_MAX):
                raise ValueError("Độ dài xem thử chỉ được từ 10 đến 60 giây.")
            if start >= self.info["duration"]:
                raise ValueError("Thời điểm bắt đầu vượt quá thời lượng video.")
            remaining = self.info["duration"] - start
            if remaining < PREVIEW_MIN:
                raise ValueError("Video chỉ còn dưới 10 giây tại vị trí đã chọn. Hãy chọn thời điểm sớm hơn.")
            duration = min(duration, remaining)
        if self.mask is None and self.lasso_points:
            raise ValueError("Bạn đã khoanh vùng nhưng chưa nhấn 'Tự tách nét'.")
        return start, duration

    def export_preview(self):
        if self.cta_scan_running:
            messagebox.showinfo("CTA đang quét", "Hãy đợi hoặc dừng CTA Detector trước khi xuất video.", parent=self.root)
            return
        try:
            start, duration = self._validate_export(True)
        except Exception as exc:
            messagebox.showwarning("Kiểm tra lại", str(exc))
            return
        try:
            output = self._make_output_path("XEM_THU")
        except Exception as exc:
            messagebox.showerror("Không dùng được thư mục xuất", str(exc))
            return
        self._start_export(output, start, duration, preview_mode=True)

    def export_segment(self):
        if self.cta_scan_running:
            messagebox.showinfo("CTA đang quét", "Hãy đợi hoặc dừng CTA Detector trước khi xuất video.", parent=self.root)
            return
        try:
            if not self.video_path or not self.info:
                raise ValueError("Bạn chưa chọn video.")
            start = safe_float(self.segment_start_var.get(), "Thời điểm bắt đầu")
            end = safe_float(self.segment_end_var.get(), "Thời điểm kết thúc")
            if end <= start:
                raise ValueError("Thời điểm kết thúc phải lớn hơn thời điểm bắt đầu.")
            if end > self.info["duration"] + 0.05:
                raise ValueError(f"Thời điểm kết thúc vượt quá thời lượng video ({self.info['duration']:.2f} giây).")
            if self.mask is None and self.lasso_points:
                raise ValueError("Bạn đã khoanh vùng nhưng chưa nhấn 'Tự tách nét'.")
            duration = end - start
        except Exception as exc:
            messagebox.showwarning("Kiểm tra đoạn video", str(exc))
            return
        suffix = f"DOAN_{int(round(start))}s_{int(round(end))}s"
        try:
            output = self._make_output_path(suffix)
        except Exception as exc:
            messagebox.showerror("Không dùng được thư mục xuất", str(exc))
            return
        self._start_export(output, start, duration)

    def export_full(self):
        if self.cta_scan_running:
            messagebox.showinfo("CTA đang quét", "Hãy đợi hoặc dừng CTA Detector trước khi xuất video.", parent=self.root)
            return
        try:
            if not self.video_path or not self.info:
                raise ValueError("Bạn chưa chọn video.")
            if self.mask is None and self.lasso_points:
                raise ValueError("Bạn đã khoanh vùng nhưng chưa nhấn 'Tự tách nét'.")
        except Exception as exc:
            messagebox.showwarning("Kiểm tra lại", str(exc))
            return
        try:
            output = self._make_output_path("GocReview")
        except Exception as exc:
            messagebox.showerror("Không dùng được thư mục xuất", str(exc))
            return
        self._start_export(output, 0.0, None)

    def _start_export(self, output: str, start: float, duration: Optional[float], preview_mode: bool = False):
        if self.busy:
            return
        if self.excluded_ranges and self.info:
            range_end = start + duration if duration is not None else float(self.info.get("duration", 0.0))
            excluded_total = excluded_duration_in_range(self.excluded_ranges, start, range_end)
            if excluded_total > 0.02:
                active_count = sum(1 for a, b in normalize_excluded_ranges(self.excluded_ranges)
                                   if min(range_end, b) > max(start, a))
                if not messagebox.askyesno(
                        "Xác nhận loại bỏ khoảng thời gian",
                        f"Lần xuất này sẽ bỏ {active_count} khoảng, tổng cộng khoảng {excluded_total:.2f} giây.\n\nHình và tiếng được cắt đồng bộ. Video nguồn không bị thay đổi. Tiếp tục xuất?"):
                    return
        # A full export can be larger than its source; warn before filling the output drive.
        if duration is None and self.video_path and Path(self.video_path).exists():
            try:
                output_drive = Path(output).resolve().parent
                free_bytes = shutil.disk_usage(output_drive).free
                source_bytes = Path(self.video_path).stat().st_size
                recommended = max(1024 ** 3, source_bytes * 2)
                if free_bytes < recommended:
                    free_gb = free_bytes / (1024 ** 3)
                    need_gb = recommended / (1024 ** 3)
                    proceed = messagebox.askyesno(
                        "Kiểm tra dung lượng ổ đĩa",
                        f"Ổ đĩa còn khoảng {free_gb:.1f} GB.\n"
                        f"Nên có khoảng {need_gb:.1f} GB trống cho một lần xuất video lớn.\n\n"
                        "Bạn vẫn muốn tiếp tục xuất?"
                    )
                    if not proceed:
                        return
            except Exception:
                pass
        if (self.branding_config.get("logo_enabled", True)
                and (not self.logo_path or not Path(self.logo_path).exists())):
            messagebox.showwarning("Thiếu logo", "Không tìm thấy logo. Hãy chọn một file ảnh được hỗ trợ hoặc tắt Logo trong Studio Branding.")
            return
        overlay = build_overlay(self.info["width"], self.info["height"], self.logo_path,
                                self._branding_text(), branding_config=self.branding_config)
        scene_snapshot = [
            {
                "name": scene.get("name", "Cảnh"),
                "start": float(scene["start"]),
                "end": float(scene["end"]),
                "expansion": int(scene.get("expansion", MASK_DEFAULT_EXPANSION)),
                "mask": scene["mask"].copy(),
                "branding_config": dict(scene.get("branding") or {}),
                "branding_overlay": (build_overlay(self.info["width"], self.info["height"],
                                                    scene.get("branding_logo_path") or self.logo_path,
                                                    self._branding_text(), branding_config=scene["branding"])
                                     if isinstance(scene.get("branding"), dict) else None),
            }
            for scene in self.scenes if scene.get("mask") is not None
        ]
        # In scene mode each defined interval has its own mask; outside those intervals
        # do not leak the currently selected scene's mask into unrelated footage.
        mask_copy = None if scene_snapshot else (self.mask.copy() if self.mask is not None else None)
        expansion = self._effective_mask_expansion(mask_copy)
        self.busy = True
        self.cancel_event = threading.Event()
        with self.process_lock:
            self.active_processes.clear()
        self.progress.configure(value=0)
        self.status_var.set("Đang chuẩn bị xuất video… Nhấn DỪNG nếu muốn hủy.")
        self._set_controls_state(False)
        self.stop_button.configure(state="normal")

        cancel_event = self.cancel_event

        def track_process(proc: subprocess.Popen, add: bool):
            if add:
                with self.process_lock:
                    self.active_processes.add(proc)
                    should_terminate = cancel_event.is_set()
                if should_terminate and proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            else:
                with self.process_lock:
                    self.active_processes.discard(proc)

        def worker():
            try:
                def report(pct, text):
                    try:
                        self.root.after(0, lambda p=pct, t=text: self._progress_update(p, t))
                    except tk.TclError:
                        pass
                source_snapshot = str(self.video_path)
                base_end = start + duration if duration is not None else float(self.info.get("duration", 0.0))
                expected_duration = max(0.0, base_end - start - excluded_duration_in_range(self.excluded_ranges, start, base_end))
                encode_video(self.video_path, output, start, duration, mask_copy, expansion,
                             overlay, report, cancel_event=cancel_event,
                             process_tracker=track_process,
                             preview_mode=(preview_mode and self.preview_720_var.get()),
                             prefer_gpu=self.prefer_gpu_var.get(),
                             scene_masks=scene_snapshot,
                             branding_range=(float(self.branding_config.get("show_from", 0.0)),
                                             float(self.branding_config.get("show_to", self.info.get("duration", 0.0)))),
                             excluded_ranges=list(self.excluded_ranges))
                if cancel_event.is_set():
                    raise ExportCancelled()
                self.root.after(0, lambda: self._progress_update(99, "Đang kiểm tra chất lượng video vừa xuất…"))
                quality_report = inspect_output_quality(source_snapshot, output, start, expected_duration)
                if cancel_event.is_set():
                    raise ExportCancelled()
                self.root.after(0, lambda q=quality_report: self._export_done(output, q))
            except ExportCancelled as exc:
                Path(output).unlink(missing_ok=True)
                detail = str(exc)
                try:
                    self.root.after(0, lambda d=detail: self._export_cancelled(d))
                except tk.TclError:
                    pass
            except Exception as exc:
                detail = str(exc)
                if cancel_event.is_set():
                    Path(output).unlink(missing_ok=True)
                    try:
                        self.root.after(0, lambda: self._export_cancelled(
                            "Đã hủy xuất video. File xuất dở đã được xóa."))
                    except tk.TclError:
                        pass
                else:
                    try:
                        self.root.after(0, lambda d=detail: self._export_error(d))
                    except tk.TclError:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def open_quality_check_dialog(self):
        output = filedialog.askopenfilename(
            parent=self.root, title="Chọn video đầu ra cần kiểm tra",
            filetypes=[("Video", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v"), ("Tất cả", "*.*")])
        if not output:
            return
        source = ""
        compare = messagebox.askyesno(
            "Đối chiếu với video nguồn?",
            "Chọn video nguồn để so sánh thời lượng và kiểm tra giữ âm thanh.\n\nBạn có muốn chọn video nguồn không?",
            parent=self.root)
        if compare:
            source = filedialog.askopenfilename(
                parent=self.root, title="Chọn video nguồn tương ứng",
                filetypes=[("Video", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v"), ("Tất cả", "*.*")]) or ""
        report = inspect_output_quality(source or None, output)
        self._show_quality_report(report)

    def _show_quality_report(self, report: dict):
        win = tk.Toplevel(self.root)
        win.title("Quality Control · Kiểm tra chất lượng đầu ra")
        win.geometry("900x590")
        win.minsize(720, 440)
        win.configure(bg="#06111B")
        outer = ttk.Frame(win, padding=16, style="Panel.TFrame")
        outer.pack(fill="both", expand=True)
        status = report.get("status", "UNKNOWN")
        color = "#63D7B0" if status == "PASS" else ("#E8BE70" if status == "WARN" else "#E97878")
        ttk.Label(outer, text="QUALITY CONTROL", style="Section.TLabel").pack(anchor="w")
        status_var = tk.StringVar(value=f"{status}  ·  {report.get('summary', '')}")
        ttk.Label(outer, textvariable=status_var, style="Status.TLabel").pack(anchor="w", pady=(5, 3))
        ttk.Label(outer, text=Path(report.get("output_path", "")).name,
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 10))
        columns = ("status", "check", "detail")
        table = ttk.Treeview(outer, columns=columns, show="headings", height=15)
        table.heading("status", text="KẾT QUẢ")
        table.heading("check", text="HẠNG MỤC")
        table.heading("detail", text="CHI TIẾT")
        table.column("status", width=90, minwidth=80, stretch=False, anchor="center")
        table.column("check", width=190, minwidth=140, stretch=False)
        table.column("detail", width=540, minwidth=260, stretch=True)
        table.tag_configure("PASS", foreground="#79D9B9")
        table.tag_configure("WARN", foreground="#E8BE70")
        table.tag_configure("FAIL", foreground="#F08080")
        table.pack(fill="both", expand=True)
        for item in table.get_children():
            table.delete(item)
        def populate(current):
            for item in table.get_children():
                table.delete(item)
            for check in current.get("checks", []):
                table.insert("", "end", values=(check["status"], check["name"], check["detail"]), tags=(check["status"],))
            state_var_text = f"{current.get('status', 'UNKNOWN')}  ·  {current.get('summary', '')}"
            status_var.set(state_var_text)
        populate(report)
        progress_var = tk.StringVar(value="Kiểm tra nhanh hoàn tất. Có thể chạy quét giải mã toàn bộ để kiểm tra kỹ hơn.")
        ttk.Label(outer, textvariable=progress_var, style="Muted.TLabel", wraplength=850).pack(anchor="w", pady=(8, 6))
        buttons = ttk.Frame(outer, style="Panel.TFrame")
        buttons.pack(fill="x")
        def save_report():
            target = filedialog.asksaveasfilename(
                parent=win, title="Lưu báo cáo chất lượng", defaultextension=".txt",
                initialfile=f"Quality_Report_{Path(report.get('output_path','video')).stem}.txt",
                filetypes=[("Text", "*.txt")])
            if target:
                Path(target).write_text(format_quality_report(report), encoding="utf-8")
                progress_var.set(f"Đã lưu báo cáo: {target}")
        def run_deep_scan():
            deep_button.configure(state="disabled")
            progress_var.set("Đang giải mã toàn bộ video và audio. Thời gian phụ thuộc độ dài video…")
            def worker():
                updated = inspect_output_quality(report.get("source_path") or None,
                    report.get("output_path", ""), deep_scan=True)
                def done():
                    nonlocal report
                    report = updated
                    populate(report)
                    deep_button.configure(state="normal")
                    progress_var.set("Quét sâu đã hoàn tất. Kiểm tra kết quả 'Quét giải mã toàn bộ'.")
                try:
                    win.after(0, done)
                except tk.TclError:
                    pass
            threading.Thread(target=worker, daemon=True).start()
        deep_button = ttk.Button(buttons, text="QUÉT GIẢI MÃ TOÀN BỘ", command=run_deep_scan, style="Primary.TButton")
        deep_button.pack(side="left")
        ttk.Button(buttons, text="LƯU BÁO CÁO", command=save_report, style="Secondary.TButton").pack(side="left", padx=8)
        ttk.Button(buttons, text="ĐÓNG", command=win.destroy, style="Secondary.TButton").pack(side="right")
        self._present_engine_window(win)

    def open_batch_dialog(self):
        if self.batch_window is not None and self.batch_window.winfo_exists():
            self.batch_window.lift(); self.batch_window.focus_force(); return
        win = tk.Toplevel(self.root); self.batch_window = win
        win.title("Batch Processing · Xử lý hàng loạt")
        win.geometry("760x650"); win.configure(bg="#06111B");
        outer = ttk.Frame(win, padding=16, style="Panel.TFrame"); outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="BATCH PROCESSING", style="Section.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Thêm nhiều video, áp dụng cấu hình mask/branding hiện tại và xuất lần lượt.",
                  style="Muted.TLabel", wraplength=700).pack(anchor="w", pady=(5, 12))
        list_frame=ttk.Frame(outer, style="Panel.TFrame"); list_frame.pack(fill="both", expand=True)
        self.batch_listbox=tk.Listbox(list_frame, bg="#0B1B29", fg="#DCEAF2", selectbackground="#167F83",
                                      relief="flat", height=14, font=("Segoe UI",10), activestyle="none")
        sb=ttk.Scrollbar(list_frame, orient="vertical", command=self.batch_listbox.yview)
        self.batch_listbox.configure(yscrollcommand=sb.set); self.batch_listbox.pack(side="left",fill="both",expand=True); sb.pack(side="right",fill="y")
        controls=ttk.Frame(outer, style="Panel.TFrame"); controls.pack(fill="x", pady=9)
        ttk.Button(controls,text="＋ Thêm video",command=self._batch_add_files,style="Primary.TButton").pack(side="left",padx=(0,5))
        ttk.Button(controls,text="Xóa",command=self._batch_remove_selected,style="Secondary.TButton").pack(side="left",padx=5)
        ttk.Button(controls,text="Lên",command=lambda:self._batch_move(-1),style="Secondary.TButton").pack(side="left",padx=5)
        ttk.Button(controls,text="Xuống",command=lambda:self._batch_move(1),style="Secondary.TButton").pack(side="left",padx=5)
        folderrow=ttk.Frame(outer,style="Panel.TFrame"); folderrow.pack(fill="x",pady=(5,8))
        self.batch_folder_var=tk.StringVar(value=self.output_dir or str(Path(self.video_path).parent if self.video_path else Path.home()))
        ttk.Label(folderrow,text="Thư mục xuất:",style="Panel.TLabel").pack(side="left")
        ttk.Entry(folderrow,textvariable=self.batch_folder_var).pack(side="left",fill="x",expand=True,padx=8)
        ttk.Button(folderrow,text="Chọn…",command=self._batch_choose_folder,style="Secondary.TButton").pack(side="right")
        ttk.Label(outer,text="Cấu hình dùng chung: mask hiện tại, Smart Mask PX, logo/nhãn và Branding. Video khác độ phân giải sẽ được cảnh báo/bỏ qua nếu mask không tương thích.",
                  style="Muted.TLabel",wraplength=700).pack(anchor="w",pady=(0,8))
        self.batch_progress=ttk.Progressbar(outer,maximum=100,mode="determinate"); self.batch_progress.pack(fill="x")
        ttk.Label(outer,textvariable=self.batch_status_var,style="Status.TLabel").pack(anchor="w",pady=7)
        bottom=ttk.Frame(outer,style="Panel.TFrame"); bottom.pack(fill="x",pady=(5,0))
        self.batch_start_button=ttk.Button(bottom,text="▶ BẮT ĐẦU XỬ LÝ",command=self._batch_start,style="Primary.TButton"); self.batch_start_button.pack(side="left",fill="x",expand=True,padx=(0,6))
        self.batch_stop_button=ttk.Button(bottom,text="■ DỪNG HÀNG ĐỢI",command=self._batch_stop,state="disabled",style="Danger.TButton"); self.batch_stop_button.pack(side="left",fill="x",expand=True,padx=(6,0))
        self._batch_refresh_list()
        win.protocol("WM_DELETE_WINDOW", lambda: self._batch_close())
        self._present_engine_window(win)

    def _batch_add_files(self):
        files=filedialog.askopenfilenames(parent=self.batch_window,title="Chọn video cần xử lý",
            filetypes=[("Video","*.mp4 *.mkv *.mov *.avi *.webm *.m4v"),("Tất cả","*.*")])
        for f in files:
            if f not in self.batch_files: self.batch_files.append(f)
        self._batch_refresh_list()

    def _batch_refresh_list(self):
        if self.batch_listbox is None: return
        self.batch_listbox.delete(0,"end")
        for i,f in enumerate(self.batch_files,1): self.batch_listbox.insert("end",f"{i:02d}. {Path(f).name}")

    def _batch_remove_selected(self):
        sel=self.batch_listbox.curselection()
        if sel:
            del self.batch_files[sel[0]]; self._batch_refresh_list()

    def _batch_move(self,delta):
        sel=self.batch_listbox.curselection()
        if not sel: return
        i=sel[0]; j=i+delta
        if 0<=j<len(self.batch_files):
            self.batch_files[i],self.batch_files[j]=self.batch_files[j],self.batch_files[i]
            self._batch_refresh_list(); self.batch_listbox.selection_set(j)

    def _batch_choose_folder(self):
        folder=filedialog.askdirectory(parent=self.batch_window,title="Chọn thư mục lưu kết quả")
        if folder: self.batch_folder_var.set(folder)

    def _batch_start(self):
        if self.busy:
            messagebox.showwarning("Đang có tác vụ","Hãy đợi tác vụ xuất hiện tại kết thúc trước.",parent=self.batch_window); return
        if not self.batch_files:
            messagebox.showwarning("Hàng đợi trống","Hãy thêm ít nhất một video.",parent=self.batch_window); return
        folder=Path(self.batch_folder_var.get()).expanduser()
        try: folder.mkdir(parents=True,exist_ok=True)
        except Exception as e:
            messagebox.showerror("Thư mục không hợp lệ",str(e),parent=self.batch_window); return
        if self.mask is None and not any(sc.get("mask") is not None for sc in self.scenes):
            messagebox.showwarning("Chưa có mask","Hãy tạo mask hoặc thiết lập mask theo phân cảnh trước khi chạy Batch.",parent=self.batch_window); return
        files=list(self.batch_files); mask=self.mask.copy() if self.mask is not None else None
        expansion=self._effective_mask_expansion(mask)
        scenes=[{"name":sc.get("name","Cảnh"),"start":float(sc["start"]),"end":float(sc["end"]),"expansion":int(sc.get("expansion",MASK_DEFAULT_EXPANSION)),"mask":sc["mask"].copy(),"branding_config":dict(sc.get("branding") or {}),"branding_logo_path":sc.get("branding_logo_path")} for sc in self.scenes if sc.get("mask") is not None]
        logo_path=self.logo_path; branding=dict(self.branding_config); label=self._branding_text(); prefer_gpu=bool(self.prefer_gpu_var.get())
        self.batch_cancel_event=threading.Event(); cancel=self.batch_cancel_event
        self.batch_active = True
        self.batch_start_button.configure(state="disabled"); self.batch_stop_button.configure(state="normal"); self.busy=True
        self.batch_progress.configure(value=0); self.batch_status_var.set(f"Đang chuẩn bị {len(files)} video…")
        def worker():
            success=[]; failed=[]; quality_records=[]; total=len(files)
            for idx,src in enumerate(files):
                if cancel.is_set(): break
                try:
                    info=read_video_info(src); w,h=info["width"],info["height"]
                    if mask is not None and mask.shape[:2]!=(h,w):
                        raise RuntimeError(f"Mask hiện tại có kích thước {mask.shape[1]}×{mask.shape[0]}, video là {w}×{h}. Hãy tạo mask tương ứng.")
                    for sc in scenes:
                        if sc["mask"].shape[:2]!=(h,w): raise RuntimeError(f"Mask phân cảnh '{sc['name']}' không khớp độ phân giải {w}×{h}.")
                    if branding.get("logo_enabled",True) and (not logo_path or not Path(logo_path).exists()): raise RuntimeError("Không tìm thấy file logo đang chọn.")
                    overlay=build_overlay(w,h,logo_path,label,branding_config=branding)
                    scene_snap=[]
                    for sc in scenes:
                        sc_logo=sc.get("branding_logo_path") or logo_path
                        scene_snap.append({**sc,"branding_overlay":(build_overlay(w,h,sc_logo,label,branding_config=sc["branding_config"]) if sc.get("branding_config") else None)})
                    stem=Path(src).stem; dest=folder/f"{stem}_GocReview.mp4"; n=2
                    while dest.exists(): dest=folder/f"{stem}_GocReview_{n}.mp4"; n+=1
                    def report(pct,msg,ii=idx):
                        overall=((ii+max(0,min(100,float(pct)))/100)/total)*100
                        try:self.root.after(0,lambda v=overall,t=f"Video {ii+1}/{total}: {msg}":(self.batch_progress.configure(value=v),self.batch_status_var.set(t)))
                        except tk.TclError:pass
                    encode_video(str(src),str(dest),0.0,None,mask,expansion,overlay,report,cancel_event=cancel,
                                 prefer_gpu=prefer_gpu,scene_masks=scene_snap or None,
                                 branding_range=(float(branding.get("show_from",0.0)),float(branding.get("show_to",info.get("duration",0.0)))))
                    if cancel.is_set():
                        raise ExportCancelled()
                    qreport = inspect_output_quality(str(src), str(dest), 0.0, float(info.get("duration", 0.0)))
                    quality_records.append(qreport)
                    if qreport["status"] == "FAIL":
                        failed.append((src, f"QC thất bại; file được giữ để kiểm tra: {dest}. {qreport['summary']}"))
                    else:
                        success.append(str(dest))
                except ExportCancelled: break
                except Exception as exc: failed.append((src,str(exc)))
            report_path = folder / f"GocReview_Batch_Quality_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            try:
                report_text = "\n\n".join(format_quality_report(q) for q in quality_records)
                report_text += "\n\nTỔNG KẾT BATCH\n" + f"Thành công: {len(success)}\nLỗi hoặc QC không đạt: {len(failed)}\nChưa chạy: {total-len(success)-len(failed)}\n"
                if failed:
                    report_text += "\nDANH SÁCH LỖI\n" + "\n".join(f"{Path(src).name}: {err}" for src,err in failed)
                report_path.write_text(report_text, encoding="utf-8")
            except Exception as report_exc:
                report_path = None
                report_write_error = str(report_exc)
            def finish():
                self.busy=False; self.batch_active = False
                self.batch_start_button.configure(state="normal"); self.batch_stop_button.configure(state="disabled")
                if cancel.is_set(): status=f"Đã dừng. Thành công {len(success)}, lỗi {len(failed)}, còn lại chưa chạy {total-len(success)-len(failed)}."
                else: status=f"Hoàn tất hàng đợi: {len(success)} thành công, {len(failed)} lỗi."
                self.batch_status_var.set(status); self.progress.configure(value=0); self.status_var.set(status)
                summary="\n".join([f"✓ {Path(x).name}" for x in success]+[f"✗ {Path(x).name}: {e}" for x,e in failed])
                if len(summary)>4500: summary=summary[:4500]+"\n…"
                report_note = f"\n\nBáo cáo chất lượng: {report_path}" if report_path else "\n\nKhông lưu được báo cáo chất lượng: " + report_write_error
                if self.batch_window and self.batch_window.winfo_exists(): messagebox.showinfo("Kết quả Batch Processing",status+("\n\n"+summary if summary else "")+report_note,parent=self.batch_window)
            try:self.root.after(0,finish)
            except tk.TclError:pass
        threading.Thread(target=worker,daemon=True).start()

    def _batch_stop(self):
        if self.batch_cancel_event: self.batch_cancel_event.set(); self.batch_status_var.set("Đang dừng sau video hiện tại…")

    def _batch_close(self):
        if self.batch_cancel_event and not self.batch_cancel_event.is_set() and self.busy:
            messagebox.showinfo("Batch đang chạy","Hãy dừng hàng đợi và đợi video hiện tại kết thúc trước khi đóng cửa sổ.",parent=self.batch_window); return
        self.batch_window.destroy(); self.batch_window=None

    def stop_export(self):
        if not self.busy or self.cancel_event is None:
            return
        self.cancel_event.set()
        self.stop_button.configure(state="disabled")
        self.status_var.set("Đang dừng tác vụ và xóa file xuất dở…")
        with self.process_lock:
            running = list(self.active_processes)
        for proc in running:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    def _export_cancelled(self, detail: str):
        self.busy = False
        self.cancel_event = None
        with self.process_lock:
            self.active_processes.clear()
        self._set_controls_state(True)
        self.stop_button.configure(state="disabled")
        self.progress.configure(value=0)
        self.status_var.set("Đã dừng xuất video. File chưa hoàn chỉnh đã được xóa.")
        messagebox.showinfo("Đã hủy xuất", detail or "Đã dừng xuất video theo yêu cầu.")

    def _set_controls_state(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        selectable = (ttk.Button, ttk.Entry, ttk.Spinbox, ttk.Scale, ttk.Checkbutton, ttk.Combobox)
        def visit(widget):
            for child in widget.winfo_children():
                if isinstance(child, selectable):
                    try:
                        child.configure(state=state)
                    except tk.TclError:
                        pass
                visit(child)
        visit(self.root)
        self.root.update_idletasks()

    def _progress_update(self, pct: float, text: str):
        self.progress.configure(value=max(0, min(100, pct)))
        self.status_var.set(text)

    def _export_done(self, output: str, quality_report: Optional[dict] = None):
        self.busy = False
        self.cancel_event = None
        with self.process_lock:
            self.active_processes.clear()
        self._set_controls_state(True)
        self.stop_button.configure(state="disabled")
        self.progress.configure(value=100)
        self.status_var.set(f"Đã xuất xong: {Path(output).name}")
        self._open_folder_after_success(output)
        folder_note = "\n\nĐã mở thư mục chứa video." if self.auto_open_output_var.get() else ""
        if quality_report:
            status = quality_report.get("status", "UNKNOWN")
            title = {"PASS": "Đã xuất · QC đạt", "WARN": "Đã xuất · Có cảnh báo", "FAIL": "Đã xuất · QC phát hiện lỗi"}.get(status, "Đã xuất")
            messagebox.showinfo(title, f"Video: {output}\n\nKiểm tra chất lượng: {status}\n{quality_report.get('summary', '')}{folder_note}\n\nMở báo cáo chi tiết để xem từng hạng mục.")
            self._show_quality_report(quality_report)
        else:
            messagebox.showinfo("Hoàn tất", f"Video đã xuất thành công:\n{output}{folder_note}\n\nBạn hãy mở file xem thử trước khi xuất bản đầy đủ.")

    def _export_error(self, detail: str):
        self.busy = False
        self.cancel_event = None
        with self.process_lock:
            self.active_processes.clear()
        self._set_controls_state(True)
        self.stop_button.configure(state="disabled")
        self.status_var.set("Xuất video chưa thành công.")
        messagebox.showerror("Lỗi khi xuất video", detail)

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        seconds = max(0, int(seconds))
        return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def main():
    root = tk.Tk()
    app = VideoToolApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
