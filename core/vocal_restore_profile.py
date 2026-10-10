"""Bounded, measured vocal-restoration DSP profiles for new jobs."""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from pathlib import Path

from core.config import FFMPEG_EXE

PROFILE_VERSION = 1
MODES = ("off", "auto", "light", "strong", "expert")


def normalize_profile(profile=None):
    """Return the canonical, JSON-safe profile frozen into a new job."""
    if profile is None:
        profile = {"mode": "auto"}
    if isinstance(profile, str):
        profile = {"mode": profile}
    if not isinstance(profile, dict):
        raise ValueError("VocalRestore 配置必须是对象")
    mode = profile.get("mode", "auto")
    if mode not in MODES:
        raise ValueError("VocalRestore 模式无效")

    if mode == "light":
        values = (True, 60.0, True, 0.08, 0.15, True, 0.8)
    elif mode == "strong":
        values = (True, 75.0, True, 0.15, 0.30, True, 0.8)
    elif mode == "expert":
        values = (
            profile.get("highpass_enabled", False),
            profile.get("highpass_hz", 60.0),
            profile.get("deesser_enabled", False),
            profile.get("deesser_intensity", 0.08),
            profile.get("deesser_max_reduction", 0.15),
            profile.get("normalize_enabled", False),
            profile.get("normalize_peak", 0.8),
        )
    else:
        # Auto owns its conservative rules; Off is a deliberate no-op.
        values = (False, 60.0, False, 0.08, 0.15, False, 0.8)

    hp_enabled, hp_hz, deess_enabled, intensity, reduction, norm_enabled, norm_peak = values
    if any(type(flag) is not bool for flag in (hp_enabled, deess_enabled, norm_enabled)):
        raise ValueError("VocalRestore 效果开关必须是布尔值")
    numeric = (hp_hz, intensity, reduction, norm_peak)
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in numeric):
        raise ValueError("VocalRestore 数值必须是有限数字")
    hp_hz, intensity, reduction, norm_peak = map(float, numeric)
    if not 20.0 <= hp_hz <= 120.0:
        raise ValueError("VocalRestore 高通频率必须在 20–120 Hz")
    if not 0.0 <= intensity <= 0.2:
        raise ValueError("VocalRestore Deesser 强度必须在 0–0.2")
    if not 0.0 <= reduction <= 0.4:
        raise ValueError("VocalRestore Deesser 最大衰减必须在 0–0.4")
    if not 0.5 <= norm_peak <= 0.95:
        raise ValueError("VocalRestore 归一化峰值必须在 0.5–0.95")
    return {
        "version": PROFILE_VERSION,
        "mode": mode,
        "highpass_enabled": hp_enabled,
        "highpass_hz": hp_hz,
        "deesser_enabled": deess_enabled,
        "deesser_intensity": intensity,
        "deesser_max_reduction": reduction,
        "normalize_enabled": norm_enabled,
        "normalize_peak": norm_peak,
    }


def _measure(path):
    try:
        import numpy as np
        import soundfile as sf
        from scipy.signal import butter, sosfilt
    except Exception as exc:
        raise RuntimeError("VocalRestore 测量依赖不可用：" + str(exc)) from exc

    with sf.SoundFile(str(path), "r") as source:
        rate = int(source.samplerate)
        channels = int(source.channels)
        if rate < 8000 or channels < 1 or channels > 32:
            raise ValueError("VocalRestore 输入采样率或声道数无效")
        frames_total = int(len(source))
        if frames_total <= 0:
            raise ValueError("VocalRestore 输入为空")
        peak = 0.0
        total_ac_energy = 0.0
        low_energy = 0.0
        frame_count = 0
        if rate > 120:
            sos = butter(4, [20.0, 60.0], btype="bandpass", fs=rate, output="sos")
            state = np.zeros((len(sos), 2, channels), dtype=np.float64)
        else:
            sos = state = None
        while True:
            block = source.read(65536, dtype="float64", always_2d=True)
            if not len(block):
                break
            if not np.isfinite(block).all():
                raise ValueError("VocalRestore 输入含非有限音频样本")
            peak = max(peak, float(np.max(np.abs(block))))
            centered = block - np.mean(block, axis=0, keepdims=True)
            total_ac_energy += float(np.sum(centered * centered))
            if sos is not None:
                filtered, state = sosfilt(sos, centered, axis=0, zi=state)
                low_energy += float(np.sum(filtered * filtered))
            frame_count += len(block)
        if frame_count != frames_total:
            raise ValueError("VocalRestore 输入读取不完整")
    low_ratio = (low_energy / total_ac_energy) if total_ac_energy > 1e-20 else 0.0
    duration = frame_count / float(rate)
    return {
        "sample_rate": rate,
        "channels": channels,
        "frames": frame_count,
        "duration_seconds": duration,
        "peak": peak,
        "low_20_60_hz_energy_ratio": low_ratio,
    }


def plan_profile(profile, measurement):
    """Resolve mode and measurements to concrete effects without quality claims."""
    frozen = normalize_profile(profile)
    mode = frozen["mode"]
    peak = float(measurement["peak"])
    low_ratio = float(measurement["low_20_60_hz_energy_ratio"])
    if not math.isfinite(peak) or peak < 0 or not math.isfinite(low_ratio) or low_ratio < 0:
        raise ValueError("VocalRestore 测量值无效")

    if mode == "off":
        hp, deess, norm = False, False, False
        reason = "用户选择 Off"
    elif mode == "auto":
        hp = low_ratio > 0.15
        deess = False  # No reliable automatic sibilance detector is available.
        norm = peak > 0.95
        reason = "Auto 测量后未发现达到保守触发阈值的处理项" if not (hp or norm) else None
    else:
        hp = frozen["highpass_enabled"]
        deess = frozen["deesser_enabled"]
        norm = frozen["normalize_enabled"] and peak > frozen["normalize_peak"]
        reason = "所选模式的效果复选框均关闭或无需衰减" if not (hp or deess or norm) else None

    gain = 1.0
    target = frozen["normalize_peak"]
    if norm and peak > 0:
        # This factor can attenuate only; normalization never adds gain.
        gain = min(1.0, target / peak)
    applied = {
        "highpass": ({"frequency_hz": frozen["highpass_hz"] if mode != "auto" else 60.0}
                     if hp else None),
        "deesser": ({"intensity": frozen["deesser_intensity"],
                     "max_reduction": frozen["deesser_max_reduction"]} if deess else None),
        "normalization": ({"target_peak": target, "linear_gain": gain,
                           "attenuation_only": True} if norm else None),
    }
    return {
        "profile": frozen,
        "measurements": dict(measurement),
        "applied": applied,
        "skip": not (hp or deess or norm),
        "skip_reason": reason,
        "quality_assessment": "NOT_ASSESSED",
    }


def evaluate_profile(input_path, profile):
    """Measure one exact input identity and produce the frozen decision."""
    source = Path(input_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(str(source))
    before = _sha256(source)
    measurement = _measure(source)
    after = _sha256(source)
    if before != after:
        raise RuntimeError("VocalRestore 测量期间输入文件发生变化")
    decision = plan_profile(profile, measurement)
    decision["source_sha256"] = after
    return decision


def apply_vocal_restore_profile(input_path, output_path, profile, report_path=None,
                                cancel=None, evaluation=None):
    """Measure and apply the requested bounded FFmpeg DSP profile."""
    source = Path(input_path).resolve()
    destination = Path(output_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(str(source))
    if source == destination:
        raise ValueError("VocalRestore 不可覆盖原始输入")
    if cancel and cancel():
        raise RuntimeError("VocalRestore 已取消")
    decision = evaluation if evaluation is not None else evaluate_profile(source, profile)
    if decision.get("source_sha256") != _sha256(source):
        raise RuntimeError("VocalRestore 执行前输入身份发生变化")
    report = {
        "schema_version": 1,
        "algorithm": "bounded_vocal_restore_profile_v1",
        "source": str(source),
        "source_sha256": decision["source_sha256"],
        "profile": decision["profile"],
        "measurements": decision["measurements"],
        "applied": decision["applied"],
        "decision": "SKIPPED" if decision["skip"] else "APPLIED",
        "reason": decision["skip_reason"],
        "quality_assessment": "NOT_ASSESSED",
    }
    if decision["skip"]:
        _write_report(report_path, report)
        return {"input": str(source), "output": str(source), "report_path": str(report_path) if report_path else None,
                "skipped": True, "reason": decision["skip_reason"], **report}

    destination.parent.mkdir(parents=True, exist_ok=True)
    filters = []
    hp = decision["applied"]["highpass"]
    deesser = decision["applied"]["deesser"]
    normalization = decision["applied"]["normalization"]
    if hp:
        filters.append("highpass=f=%s" % _format_float(hp["frequency_hz"]))
    if deesser:
        filters.append("deesser=i=%s:m=%s" % (
            _format_float(deesser["intensity"]), _format_float(deesser["max_reduction"])))
    if normalization:
        filters.append("volume=%s" % _format_float(normalization["linear_gain"]))
    fd, temporary = tempfile.mkstemp(prefix=destination.stem + ".", suffix=".tmp.wav", dir=str(destination.parent))
    os.close(fd)
    try:
        command = [FFMPEG_EXE, "-y", "-hide_banner", "-v", "error", "-i", str(source),
                   "-map", "0:a:0", "-vn", "-af", ",".join(filters), "-ar",
                   str(decision["measurements"]["sample_rate"]), "-ac", str(decision["measurements"]["channels"]),
                   "-c:a", "pcm_f32le", temporary]
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=1800)
        if cancel and cancel():
            raise RuntimeError("VocalRestore 已取消")
        output_measurement = _measure(temporary)
        if (output_measurement["sample_rate"] != decision["measurements"]["sample_rate"] or
                output_measurement["channels"] != decision["measurements"]["channels"]):
            raise RuntimeError("VocalRestore 输出采样率或声道数发生变化")
        sample_rate = decision["measurements"]["sample_rate"]
        if abs(output_measurement["duration_seconds"] - decision["measurements"]["duration_seconds"]) > max(0.02, 1.0 / sample_rate * 2):
            raise RuntimeError("VocalRestore 输出时长发生变化")
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    report["output"] = str(destination)
    report["output_sha256"] = _sha256(destination)
    report["output_measurements"] = output_measurement
    _write_report(report_path, report)
    return {"input": str(source), "output": str(destination),
            "report_path": str(report_path) if report_path else None,
            "skipped": False, **report}


def _format_float(value):
    return ("%.8f" % float(value)).rstrip("0").rstrip(".") or "0"


def _sha256(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_report(path, report):
    if not path:
        return
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
