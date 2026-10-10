"""Measured, opt-in source pre-cleaning with frozen decisions.

Source labels are recorded for provenance only. The only automatic edit is a
small measured DC-offset subtraction; clipping and spectral roll-off are
reported for review and never repaired by guessing.
"""
import hashlib
import json
import math
import os

SCHEMA_VERSION = 2
HINTS = ("auto", "suno", "udio", "studio_master", "mp3", "youtube", "live", "vocal_only")
REQUIRED = ("sample_rate", "sample_peak", "clipped_samples", "channels")
DC_THRESHOLD = 0.02
DC_MAX_CORRECTION = 0.10
ROLLOFF_REVIEW_HZ = 8000.0


def freeze_source_policy(source_hint="auto", enabled=False):
    if source_hint not in HINTS:
        raise ValueError("未知来源提示")
    if type(enabled) is not bool:
        raise ValueError("source_preclean_enabled 只能是布尔值")
    return {
        "schema_version": SCHEMA_VERSION,
        "source_hint": source_hint,
        "enabled": enabled,
        "dc_threshold": DC_THRESHOLD,
        "dc_max_correction": DC_MAX_CORRECTION,
        "rolloff_review_hz": ROLLOFF_REVIEW_HZ,
    }


def preprocess_policy(facts, source_hint="auto", enabled=False):
    """Return a deterministic bounded decision based only on measured facts."""
    if source_hint not in HINTS:
        raise ValueError("未知来源提示")
    if type(enabled) is not bool:
        raise ValueError("source_preclean_enabled 只能是布尔值")
    if not isinstance(facts, dict):
        raise ValueError("音频事实无效")
    missing = [key for key in REQUIRED if facts.get(key) is None]
    record = {
        "schema_version": SCHEMA_VERSION,
        "source_hint": source_hint,
        "enabled_by_job": enabled,
        "applied": False,
        "changes_job": False,
        "action": "unchanged",
        "correction_db": 0.0,
        "pre_separator_clean": "none",
        "recommendation": "insufficient_measurement",
        "reasons": [],
        "not_measured": ["lufs", "true_peak", "noise_floor", "reverb"],
        "quality_acceptance": "NOT_ASSESSED",
        "listening_quality": "not_assessed",
    }
    if missing:
        record["reasons"] = ["缺少 " + "、".join(missing)]
        return record
    reasons = []
    sample_rate = facts.get("sample_rate")
    if _bad_number(sample_rate) or sample_rate <= 0:
        reasons.append("采样率无效")
    elif sample_rate < 44100:
        reasons.append("采样率低于 44100")
    peak = facts.get("sample_peak")
    peak_bad = _bad_number(peak) or peak < 0
    if peak_bad:
        reasons.append("峰值无效")
    clipped = facts.get("clipped_samples")
    if type(clipped) is not int or clipped < 0:
        reasons.append("削波计数无效")
    elif not peak_bad and (clipped > 0 or peak >= 1):
        reasons.append("存在削波或满幅")
    offsets = facts.get("dc_offset")
    if offsets is not None and (not isinstance(offsets, (list, tuple)) or not offsets):
        reasons.append("直流偏移格式无效")
        offsets = None
    if offsets is not None:
        if any(_bad_number(value) for value in offsets):
            reasons.append("直流偏移无效")
            offsets = None
        elif any(abs(value) > DC_THRESHOLD for value in offsets):
            reasons.append("直流偏移明显")
    rolloff = facts.get("spectral_rolloff_hz")
    if rolloff is not None:
        if _bad_number(rolloff) or rolloff < 0:
            reasons.append("频谱滚降测量无效")
        elif not _bad_number(sample_rate) and sample_rate >= 44100 and rolloff < ROLLOFF_REVIEW_HZ:
            reasons.append("95% 频谱滚降低于 8 kHz，仅提示复核")
    record["reasons"] = reasons
    if (type(clipped) is not int or clipped < 0 or peak_bad
            or _bad_number(sample_rate) or sample_rate <= 0):
        record["recommendation"] = "review_invalid_measurement_no_edit"
        return record
    if clipped > 0 or peak >= 1:
        record["recommendation"] = "review_clipping_no_edit"
        return record
    if not enabled:
        if offsets is not None and any(not _bad_number(value) and abs(value) > DC_THRESHOLD for value in offsets):
            record["recommendation"] = "dc_offset_detected_opt_in_disabled"
        elif any("频谱滚降" in reason for reason in reasons):
            record["recommendation"] = "review_rolloff_no_edit"
        else:
            record["recommendation"] = "opt_in_disabled"
        return record
    if offsets is not None and not any(_bad_number(value) for value in offsets):
        correction = [float(value) for value in offsets]
        if any(abs(value) > DC_MAX_CORRECTION for value in correction):
            record["recommendation"] = "review_large_dc_no_edit"
            return record
        if any(abs(value) > DC_THRESHOLD for value in correction):
            bounds = facts.get("peak_bounds")
            if not isinstance(bounds, (list, tuple)) or len(bounds) != len(correction):
                record["recommendation"] = "review_missing_peak_bounds"
                return record
            corrected_peaks = []
            for index, offset in enumerate(correction):
                bound = bounds[index]
                if (not isinstance(bound, (list, tuple)) or len(bound) != 2
                        or any(_bad_number(item) for item in bound)):
                    record["recommendation"] = "review_missing_peak_bounds"
                    return record
                corrected_peaks.append(max(abs(float(bound[0]) - offset), abs(float(bound[1]) - offset)))
            if max(corrected_peaks, default=0.0) > 0.98:
                record["recommendation"] = "review_headroom_no_edit"
                return record
            record.update({
                "applied": True,
                "changes_job": True,
                "action": "bounded_dc_offset_subtraction",
                "correction_db": max(_db(value) for value in correction if value),
                "dc_correction": correction,
                "pre_separator_clean": "dc_offset",
                "recommendation": "apply_bounded_dc_correction",
            })
            return record
    record["recommendation"] = "no_safe_preclean_action"
    return record


def measure_source_facts(path):
    """Measure peak, clipping, DC, channel count, and sampled spectral roll-off."""
    import numpy as np
    import soundfile as sf

    info = sf.info(str(path))
    if info.frames <= 0 or info.samplerate <= 0 or info.channels <= 0:
        raise ValueError("源音频没有有效帧率、采样率或声道")
    sums = np.zeros(info.channels, dtype=np.float64)
    minima = np.full(info.channels, np.inf, dtype=np.float64)
    maxima = np.full(info.channels, -np.inf, dtype=np.float64)
    peak = 0.0
    clipped = 0
    read_frames = 0
    with sf.SoundFile(str(path), mode="r") as audio:
        while True:
            block = audio.read(65536, dtype="float32", always_2d=True)
            if not len(block):
                break
            if not np.isfinite(block).all():
                raise ValueError("源音频包含 NaN 或 Inf")
            sums += block.sum(axis=0, dtype=np.float64)
            minima = np.minimum(minima, block.min(axis=0))
            maxima = np.maximum(maxima, block.max(axis=0))
            peak = max(peak, float(np.max(np.abs(block))))
            clipped += int(np.count_nonzero(np.abs(block) >= 0.999969))
            read_frames += len(block)
    if read_frames != info.frames:
        raise ValueError("源音频测量帧数不完整")
    offsets = (sums / read_frames).tolist()
    return {
        "sample_rate": int(info.samplerate),
        "sample_peak": peak,
        "clipped_samples": clipped,
        "channels": int(info.channels),
        "frames": int(info.frames),
        "duration_seconds": float(info.frames / info.samplerate),
        "dc_offset": offsets,
        "peak_bounds": [[float(minima[i]), float(maxima[i])] for i in range(info.channels)],
        "spectral_rolloff_hz": _measure_rolloff(path, info),
        "rolloff_method": "mean of up to five evenly spaced Hann-windowed FFT samples; 95% cumulative power",
    }


def apply_source_preclean(source_path, output_path, report_path, source_hint="auto", enabled=False):
    """Apply only a frozen, bounded DC correction and emit a per-source report."""
    import numpy as np
    import soundfile as sf

    facts = measure_source_facts(source_path)
    decision = preprocess_policy(facts, source_hint, enabled)
    output_path = os.path.abspath(output_path)
    report_path = os.path.abspath(report_path)
    source_hash = _sha256(source_path)
    if decision["applied"]:
        offsets = np.asarray(decision["dc_correction"], dtype=np.float32)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with sf.SoundFile(str(source_path), mode="r") as src:
            with sf.SoundFile(output_path, mode="w", samplerate=src.samplerate,
                              channels=src.channels, format="WAV", subtype="FLOAT") as dst:
                while True:
                    block = src.read(65536, dtype="float32", always_2d=True)
                    if not len(block):
                        break
                    dst.write(np.ascontiguousarray(block - offsets[None, :], dtype=np.float32))
        after = measure_source_facts(output_path)
        if after["frames"] != facts["frames"] or after["channels"] != facts["channels"] or after["sample_rate"] != facts["sample_rate"]:
            raise RuntimeError("SourcePolicy 预处理改变了音频时长或格式")
        if max((abs(value) for value in after["dc_offset"]), default=0.0) > DC_THRESHOLD:
            raise RuntimeError("SourcePolicy 预处理后直流偏移仍超出阈值")
        result_path = output_path
    else:
        result_path = os.path.abspath(source_path)
        after = facts
    report = {
        **decision,
        "source_path": os.path.abspath(source_path),
        "source_sha256": source_hash,
        "output_path": result_path,
        "output_sha256": _sha256(result_path),
        "facts": facts,
        "output_facts": after,
        "duration_preserved": after["frames"] == facts["frames"],
    }
    _write_json(report_path, report)
    return report


def _measure_rolloff(path, info):
    import numpy as np
    import soundfile as sf

    window_frames = min(32768, max(4096, 2 ** int(math.floor(math.log2(max(4096, info.samplerate))))))
    window_frames = min(window_frames, info.frames)
    starts = sorted(set(int(round(value * max(0, info.frames - window_frames))) for value in (0, .25, .5, .75, 1)))
    powers = None
    taper = np.hanning(window_frames).astype(np.float32)
    with sf.SoundFile(str(path), mode="r") as audio:
        for start in starts:
            audio.seek(start)
            block = audio.read(window_frames, dtype="float32", always_2d=True)
            if len(block) < window_frames:
                block = np.pad(block, ((0, window_frames - len(block)), (0, 0)))
            centered = block - block.mean(axis=0, keepdims=True)
            spectrum = np.fft.rfft(centered * taper[:, None], axis=0)
            power = np.mean(np.abs(spectrum) ** 2, axis=1)
            powers = power if powers is None else powers + power
    if powers is None or not np.isfinite(powers).all() or float(powers.sum()) <= 0:
        return None
    index = int(np.searchsorted(np.cumsum(powers), 0.95 * float(powers.sum())))
    return float(index * info.samplerate / window_frames)


def _db(value):
    return 20.0 * math.log10(max(abs(float(value)), 1e-12))


def _bad_number(value):
    return type(value) not in (int, float) or not math.isfinite(value)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
