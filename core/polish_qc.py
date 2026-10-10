"""Before/after checks for a polish master. These warnings do not replace fail-closed QC."""
import math

import numpy as np

from core.polish_analysis import as_channels, band_levels
from core.qc import pearson_correlation

HF_LOSS_DB = 6.0
DYNAMIC_COLLAPSE_DB = 6.0
CORRELATION_DROP = 0.35


def _spectrum(audio, sample_rate):
    return band_levels(as_channels(audio).mean(axis=1), sample_rate)


def _dynamic_range(audio, sample_rate):
    mono = as_channels(audio).mean(axis=1)
    frame = max(32, int(0.05 * sample_rate))
    hop = max(16, frame // 2)
    if mono.size < frame:
        rms = math.sqrt(float(np.mean(mono ** 2)) + 1e-20)
        return 0.0
    values = []
    for start in range(0, mono.size - frame + 1, hop):
        values.append(20.0 * math.log10(math.sqrt(float(np.mean(mono[start:start + frame] ** 2)) + 1e-20)))
    array = np.array(values)
    return float(np.percentile(array, 95) - np.percentile(array, 10))


def _clip_count(audio):
    data = as_channels(audio)
    return int(np.count_nonzero(np.abs(data) >= 0.999))


def compare_polish_buffers(before, after, sample_rate):
    """Report HF loss, dynamic collapse, phase degradation, and new clipping.

    A gain change alone is not HF loss: air is compared relative to the mid band.
    """
    before = as_channels(before)
    after = as_channels(after)
    if before.shape[0] == 0 or after.shape[0] == 0:
        raise ValueError("对比音频为空")
    if not np.isfinite(before).all() or not np.isfinite(after).all():
        raise ValueError("对比音频包含 NaN/Inf")
    findings = []
    before_levels = _spectrum(before, sample_rate)
    after_levels = _spectrum(after, sample_rate)
    # A pure tone has no air to lose. Limiter harmonics can raise the mid floor
    # and look like an air cut if air is only the FFT floor.
    air_present = before_levels["air"] > max(before_levels.values()) - 60.0
    air_drop = (before_levels["air"] - before_levels["mid"]) - (after_levels["air"] - after_levels["mid"])
    if air_present and air_drop > HF_LOSS_DB:
        findings.append({
            "kind": "high_frequency_loss",
            "cause": "denoise",
            "warning": "高频相对中频下降 %.1f dB，可能来自 denoise" % air_drop,
            "delta_db": float(air_drop),
        })
    range_drop = _dynamic_range(before, sample_rate) - _dynamic_range(after, sample_rate)
    if range_drop > DYNAMIC_COLLAPSE_DB:
        findings.append({
            "kind": "dynamic_range_collapse",
            "cause": "compression",
            "warning": "动态范围下降 %.1f dB，可能来自 compression" % range_drop,
            "delta_db": float(range_drop),
        })
    if before.shape[1] > 1 and after.shape[1] > 1:
        before_correlation = pearson_correlation(before[:, 0], before[:, 1])
        after_correlation = pearson_correlation(after[:, 0], after[:, 1])
        if before_correlation is not None and after_correlation is not None:
            drop = before_correlation - after_correlation
            if drop > CORRELATION_DROP or (after_correlation < -0.25 and after_correlation < before_correlation):
                findings.append({
                    "kind": "phase_degradation",
                    "cause": "stereo",
                    "warning": "立体声相关性从 %.2f 降到 %.2f，可能来自 stereo" % (before_correlation, after_correlation),
                    "before": float(before_correlation),
                    "after": float(after_correlation),
                })
    before_clips = _clip_count(before)
    after_clips = _clip_count(after)
    if after_clips > before_clips and float(np.max(np.abs(after))) >= 0.999:
        findings.append({
            "kind": "clipping",
            "cause": "clipping",
            "warning": "处理后新增削波采样 %d" % (after_clips - before_clips),
            "before": before_clips,
            "after": after_clips,
        })
    return findings


def trace_metrics(trace_dir, master_path=None):
    """Peak and RMS for each polish trace. A missing file is recorded, not fatal."""
    import os
    import soundfile as sf
    rows = []
    for role in ("source", "repaired", "tone", "dynamics", "stereo", "master"):
        path = master_path if role == "master" and master_path else os.path.join(trace_dir, role + ".wav")
        if not path or not os.path.isfile(path):
            rows.append({"role": role, "present": False})
            continue
        try:
            audio, _rate = sf.read(path, always_2d=True, dtype="float32")
        except (OSError, RuntimeError, ValueError):
            rows.append({"role": role, "present": False})
            continue
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        rms = float(math.sqrt(float(np.mean(np.square(audio))) + 1e-20)) if audio.size else 0.0
        rows.append({"role": role, "present": True, "peak": peak, "rms": rms})
    return rows


def _at_rate(audio, source_rate, target_rate):
    """Resample a delivered master onto the 48 kHz working trace for comparison."""
    source_rate = int(source_rate)
    target_rate = int(target_rate)
    if source_rate == target_rate:
        return audio
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("对比音频采样率无效")
    from math import gcd
    from scipy.signal import resample_poly
    factor = gcd(source_rate, target_rate)
    converted = resample_poly(np.asarray(audio), target_rate // factor, source_rate // factor, axis=0)
    return np.asarray(converted, dtype=np.float32)


def comparison_from_files(before_path, after_path):
    import soundfile as sf
    before, before_rate = sf.read(before_path, always_2d=True, dtype="float32")
    after, after_rate = sf.read(after_path, always_2d=True, dtype="float32")
    after = _at_rate(after, after_rate, before_rate)
    findings = compare_polish_buffers(before, after, int(before_rate))
    return {
        "before": before_path,
        "after": after_path,
        "findings": findings,
        "high_frequency_loss": any(item["kind"] == "high_frequency_loss" for item in findings),
        "dynamic_range_collapse": any(item["kind"] == "dynamic_range_collapse" for item in findings),
        "phase_degradation": any(item["kind"] == "phase_degradation" for item in findings),
        "clipping": any(item["kind"] == "clipping" for item in findings),
    }
