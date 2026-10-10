"""Bounded polish processors. Each one takes a buffer and the shared analysis.

No problem, or a disabled switch, returns a pass-through. NaN or Inf stops.
Amounts are clamped to the caps in polish_policy before they touch the audio.
"""
import math

import numpy as np

from core.polish_analysis import BANDS, BAND_NAMES, as_channels, band_levels, band_power
from core.polish_policy import (
    AUTO_EQ_MAX_DB,
    DENOISE_MAX_HF_LOSS_DB,
    DENOISE_MAX_REDUCTION_DB,
    DYNAMIC_EQ_MAX_DB,
    MULTIBAND_MAX_GR_DB,
    REFERENCE_MATCH_MAX_DB,
    STEREO_MAX_WIDTH_DELTA,
    TRANSIENT_MAX_RATIO,
)
from core.qc import pearson_correlation


class NonFiniteAudio(ValueError):
    """Non-finite samples are fatal. Callers must not bypass them."""


def _guard(audio):
    data = as_channels(audio)
    if not np.isfinite(data).all():
        raise NonFiniteAudio("音频包含 NaN/Inf")
    return data


def _pass(audio, reason):
    return np.array(_guard(audio), copy=True), {"bypassed": True, "reason": reason, "amount": 0.0}


def _finish(audio, info):
    if not np.isfinite(audio).all():
        raise NonFiniteAudio("处理结果包含 NaN/Inf")
    info = dict(info)
    info.setdefault("bypassed", False)
    return audio, info


def _peaking(audio, sample_rate, frequency, q, gain_db):
    gain_db = float(gain_db)
    if abs(gain_db) < 0.05:
        return audio
    frequency = float(frequency)
    q = max(float(q), 0.2)
    if frequency <= 10 or frequency >= sample_rate / 2.0 - 10:
        return audio
    amp = 10.0 ** (gain_db / 40.0)
    omega = 2.0 * math.pi * frequency / float(sample_rate)
    alpha = math.sin(omega) / (2.0 * q)
    cosine = math.cos(omega)
    b0 = 1.0 + alpha * amp
    b1 = -2.0 * cosine
    b2 = 1.0 - alpha * amp
    a0 = 1.0 + alpha / amp
    a1 = -2.0 * cosine
    a2 = 1.0 - alpha / amp
    from scipy.signal import lfilter
    return lfilter([b0 / a0, b1 / a0, b2 / a0], [1.0, a1 / a0, a2 / a0], audio, axis=0)


def _interpolate(audio, start, end):
    if start <= 0 or end >= audio.shape[0] - 1:
        return False
    left = audio[start - 1].copy()
    right = audio[end + 1].copy()
    span = float(end - start + 2)
    for offset, index in enumerate(range(start, end + 1)):
        audio[index] = left + (right - left) * ((offset + 1) / span)
    return True


def apply_declick(audio, sample_rate, report, enabled=True):
    data = _guard(audio)
    events = (report.get("repair") or {}).get("click_events") or []
    high = [item for item in events if item.get("kind") == "click" and item.get("confidence") == "high"]
    low = [item for item in events if item.get("kind") == "click" and item.get("confidence") == "low"]
    if not enabled or not high:
        info = {"bypassed": True, "reason": "已关闭" if not enabled else "no problem", "repaired": 0, "reported": len(low), "amount": 0.0}
        return np.array(data, copy=True), info
    out = np.array(data, copy=True)
    repaired = 0
    for event in high:
        if event.get("kind") != "click" or event.get("confidence") != "high":
            continue
        if int(event.get("width", 99)) > 6:
            continue
        if _interpolate(out, int(event["start"]), int(event["end"])):
            repaired += 1
    return _finish(out, {"bypassed": repaired == 0, "reason": "修复高置信度 click" if repaired else "no problem", "repaired": repaired, "reported": len(low), "amount": float(repaired)})


def _clip_report(before, after, counts, reason, bypassed):
    return {
        "bypassed": bypassed,
        "reason": reason,
        "amount": float(counts["light"] + counts["moderate"]),
        "before_peak": float(np.max(np.abs(before))) if before.size else 0.0,
        "after_peak": float(np.max(np.abs(after))) if after.size else 0.0,
        "clipped_before": int(np.count_nonzero(np.abs(before) >= 0.999)),
        "clipped_after": int(np.count_nonzero(np.abs(after) >= 0.999)),
        "light": counts["light"],
        "moderate": counts["moderate"],
        "severe": counts["severe"],
    }


def apply_declip(audio, sample_rate, report, enabled=True):
    data = _guard(audio)
    regions = (report.get("repair") or {}).get("clipping_regions") or []
    if not enabled or not regions:
        copy = np.array(data, copy=True)
        empty = {"light": 0, "moderate": 0, "severe": 0}
        return copy, _clip_report(data, copy, empty, "已关闭" if not enabled else "no problem", True)
    out = np.array(data, copy=True)
    counts = {"light": 0, "moderate": 0, "severe": 0}
    for region in regions:
        start = int(region["start"])
        end = int(region["end"])
        severity = region.get("severity")
        if severity == "light":
            if _interpolate(out, start, end):
                counts["light"] += 1
        elif severity == "moderate":
            original = out[start:end + 1].copy()
            if _interpolate(out, start, end):
                out[start:end + 1] = original * 0.5 + out[start:end + 1] * 0.5
                counts["moderate"] += 1
        else:
            counts["severe"] += 1
    reason = "有界削波修复" if counts["light"] or counts["moderate"] else "严重削波只报告"
    if counts["severe"]:
        reason += "；严重削波未宣称恢复"
    bypassed = counts["light"] == 0 and counts["moderate"] == 0
    return _finish(out, _clip_report(data, out, counts, reason, bypassed))


def _high_residual(mono, sample_rate, cutoff=8000.0):
    coeff = math.exp(-2.0 * math.pi * float(cutoff) / float(sample_rate))
    high = np.empty(mono.shape[0], dtype=np.float64)
    state = 0.0
    for index, sample in enumerate(mono):
        state = (1.0 - coeff) * float(sample) + coeff * state
        high[index] = float(sample) - state
    return high


def _ms_coeff(milliseconds, sample_rate, fallback):
    if milliseconds is None:
        return fallback
    samples = max(1.0, float(milliseconds) * 0.001 * float(sample_rate))
    return math.exp(-1.0 / samples)


def _envelope_python(magnitude, attack, release):
    out = np.empty_like(magnitude)
    state = 0.0
    for index, sample in enumerate(magnitude):
        coeff = attack if sample > state else release
        state = coeff * state + (1.0 - coeff) * sample
        out[index] = state
    return out


_ENVELOPE_KERNEL = None


def _envelope(magnitude, attack, release):
    """Run the same sample recurrence without a Python iteration per sample.

    JIT writes no disk cache, and fastmath remains disabled so the original
    attack/release decisions and numerical ordering remain unchanged.
    """
    global _ENVELOPE_KERNEL
    if _ENVELOPE_KERNEL is None:
        try:
            from numba import njit
        except ImportError:
            _ENVELOPE_KERNEL = _envelope_python
        else:
            _ENVELOPE_KERNEL = njit(cache=False, nogil=True, fastmath=False)(_envelope_python)
    return _ENVELOPE_KERNEL(magnitude, attack, release)


def apply_denoise(audio, sample_rate, report, enabled=True, strength=1.0, max_reduction_db=DENOISE_MAX_REDUCTION_DB, max_hf_loss_db=DENOISE_MAX_HF_LOSS_DB):
    data = _guard(audio)
    needs = bool((report.get("needs") or {}).get("denoise"))
    if not enabled or not needs:
        return np.array(data, copy=True), {"bypassed": True, "reason": "已关闭" if not enabled else "no problem", "amount": 0.0}
    strength = max(0.0, min(float(strength), 1.0))
    reduction_db = min(float(max_reduction_db), DENOISE_MAX_REDUCTION_DB) * strength
    hf_limit = min(float(max_hf_loss_db), DENOISE_MAX_HF_LOSS_DB)
    if reduction_db < 0.05:
        return np.array(data, copy=True), {"bypassed": True, "reason": "no problem", "amount": 0.0}
    mono = data.mean(axis=1)
    frame = max(32, int(0.02 * sample_rate))
    hop = max(16, frame // 2)
    rms = []
    for start in range(0, max(1, mono.size - frame), hop):
        rms.append(math.sqrt(float(np.mean(mono[start:start + frame] ** 2)) + 1e-20))
    rms = np.array(rms if rms else [0.0])
    median = float(np.median(rms))
    noise_floor = float((report.get("noise") or {}).get("noise_floor", -120.0))
    noise_lin = 10.0 ** (noise_floor / 20.0)
    running = 0.0
    gate = np.zeros(mono.size)
    for index, sample in enumerate(rms):
        start = index * hop
        end = min(mono.size, start + hop)
        running = max(running * 0.98, float(sample))
        quiet = float(sample) < max(noise_lin * 4.0, median * 0.25)
        tail = float(sample) < running * 0.35 and running > float(sample) * 2.0
        transient = float(sample) > median * 2.5
        if quiet and not tail and not transient:
            gate[start:end] = 1.0
    # Only the high band of quiet, non-tail frames moves, and only by the HF cap.
    # Each channel loses its own high residual. A shared mono residual would
    # write the noisy channel into a channel that was silent.
    hf_cut = min(reduction_db, hf_limit)
    keep = 10.0 ** (-hf_cut / 20.0)
    out = np.array(data, copy=True)
    weight = gate * (1.0 - keep)
    for channel in range(out.shape[1]):
        out[:, channel] = data[:, channel] - _high_residual(data[:, channel], sample_rate) * weight
    before_air = band_power(mono, sample_rate, 8000, min(18000, sample_rate / 2.0))
    after_air = band_power(out.mean(axis=1), sample_rate, 8000, min(18000, sample_rate / 2.0))
    before_db = 10.0 * math.log10(max(before_air, 1e-20))
    after_db = 10.0 * math.log10(max(after_air, 1e-20))
    if before_db - after_db > hf_limit + 0.05:
        return np.array(data, copy=True), {"bypassed": True, "reason": "高频损失超过安全门，已回退", "amount": 0.0, "reverted": True}
    return _finish(out, {"bypassed": False, "reason": "轻度抑制安静段噪声", "amount": reduction_db, "hf_change_db": after_db - before_db})


def plan_auto_eq(report, max_db=AUTO_EQ_MAX_DB):
    max_db = min(abs(float(max_db)), AUTO_EQ_MAX_DB)
    if not (report.get("needs") or {}).get("auto_eq"):
        return []
    spectrum = report.get("spectrum") or {}
    values = [float(spectrum.get(name, -120.0)) for name in BAND_NAMES]
    median = float(np.median(values))
    targets = []

    def add(name, hz, q, excess, sign, reason):
        gain = sign * min(max_db, max(0.0, (excess - 3.0) * 0.45))
        gain = max(-max_db, min(max_db, gain))
        if abs(gain) >= 0.3:
            targets.append({"band": name, "hz": hz, "q": q, "gain_db": float(gain), "reason": reason})

    low_mid = float(spectrum.get("low_mid", median)) - median
    harsh = max(float(spectrum.get("upper_mid", median)), float(spectrum.get("presence", median))) - median
    sub = float(spectrum.get("sub", median)) - float(spectrum.get("bass", median))
    air_gap = float(spectrum.get("presence", median)) - float(spectrum.get("air", median))
    if low_mid > 4.0:
        add("low_mid", 280.0, 0.8, low_mid, -1.0, "低中频长期能量偏高")
    if harsh > 4.0:
        add("upper_mid", 3000.0, 1.0, harsh, -1.0, "中高频长期偏刺")
    if sub > 6.0:
        add("sub", 40.0, 0.7, sub, -1.0, "超低频长期偏多")
    if air_gap > 12.0:
        add("air", 10000.0, 0.7, air_gap, 1.0, "空气感明显偏暗")
    for target in targets:
        if abs(target["gain_db"]) > AUTO_EQ_MAX_DB:
            raise RuntimeError("自动 EQ 超过 ±3 dB")
    return targets


def apply_eq_curve(audio, sample_rate, targets, max_db):
    data = _guard(audio)
    max_db = min(abs(float(max_db)), AUTO_EQ_MAX_DB)
    if not targets:
        return np.array(data, copy=True), {"bypassed": True, "reason": "no problem", "filters": [], "amount": 0.0}
    out = np.array(data, copy=True)
    applied = []
    for target in targets:
        gain = max(-max_db, min(max_db, float(target["gain_db"])))
        out = _peaking(out, sample_rate, target["hz"], target.get("q", 0.8), gain)
        applied.append(dict(target, gain_db=float(gain)))
    return _finish(out, {"bypassed": False, "reason": "有界均衡", "filters": applied, "amount": max(abs(item["gain_db"]) for item in applied)})


def apply_auto_eq(audio, sample_rate, report, enabled=True, max_db=AUTO_EQ_MAX_DB, strength=1.0):
    if not enabled:
        return _pass(audio, "已关闭")
    targets = plan_auto_eq(report, max_db)
    for target in targets:
        target["gain_db"] = float(target["gain_db"]) * max(0.0, min(float(strength), 1.0))
    return apply_eq_curve(audio, sample_rate, targets, max_db)


def apply_dynamic_eq(audio, sample_rate, report, enabled=True, max_db=DYNAMIC_EQ_MAX_DB, strength=1.0, q=None, frequency=None, threshold_percentile=None, attack_ms=None, release_ms=None):
    data = _guard(audio)
    max_db = min(abs(float(max_db)), DYNAMIC_EQ_MAX_DB) * max(0.0, min(float(strength), 1.0))
    if not enabled or not (report.get("needs") or {}).get("dynamic_eq") or max_db < 0.05:
        return np.array(data, copy=True), {"bypassed": True, "reason": "已关闭" if not enabled else "no problem", "amount": 0.0, "bands": []}
    bands = [("low", 80.0, 0.7), ("low_mid", 250.0, 0.8), ("harshness", 3500.0, 1.2), ("high", 8000.0, 0.8)]
    if frequency:
        target_hz = float(frequency)
        bands = [min(bands, key=lambda item: abs(item[1] - target_hz))]
    spectrum = report.get("spectrum") or {}
    median = float(np.median([spectrum.get(name, -120.0) for name in BAND_NAMES]))
    interesting = {
        "low": float(spectrum.get("bass", median)) - median > 2.0,
        "low_mid": float(spectrum.get("low_mid", median)) - median > 2.0,
        "harshness": float(spectrum.get("upper_mid", median)) - median > 2.0,
        "high": float(spectrum.get("presence", median)) - median > 2.0,
    }
    percentile = 90.0 if threshold_percentile is None else min(99.0, max(50.0, float(threshold_percentile)))
    attack = _ms_coeff(attack_ms, sample_rate, 0.2)
    release = _ms_coeff(release_ms, sample_rate, 0.02)
    out = np.array(data, copy=True)
    used = []
    full = np.max(np.abs(data), axis=1)
    for name, hz, default_q in bands:
        if not interesting.get(name):
            continue
        band_q = float(q if q is not None else default_q)
        width = hz / max(band_q, 0.2)
        low = max(20.0, hz - width / 2.0)
        high = min(sample_rate / 2.2, hz + width / 2.0)
        if high <= low + 10.0:
            continue
        band = _bandpass(data, sample_rate, low, high)
        follow = _envelope(np.abs(band.mean(axis=1)), attack, release)
        if float(np.max(follow)) < 1e-5:
            continue
        threshold = float(np.percentile(follow, percentile))
        if threshold <= 1e-8:
            continue
        over_db = 20.0 * np.log10((follow + 1e-12) / threshold)
        reduction = np.clip(over_db, 0.0, max_db)
        reduction = np.where(full < 1e-4, 0.0, reduction)
        if float(np.max(reduction)) < 0.2:
            continue
        gain = 10.0 ** (-reduction / 20.0)
        out = out + band * (gain.reshape(-1, 1) - 1.0)
        used.append({"band": name, "hz": hz, "q": band_q, "gain_db": float(-np.max(reduction)), "reason": "该频段短时过量"})
    if not used:
        return np.array(data, copy=True), {"bypassed": True, "reason": "no problem", "amount": 0.0, "bands": []}
    return _finish(out, {"bypassed": False, "reason": "动态均衡只处理短时过量", "amount": max(abs(item["gain_db"]) for item in used), "bands": used})


def _shape_levels(audio, sample_rate):
    """Band levels after RMS normalization, with the floor removed.

    A louder copy of the same song must not look like a different spectrum.
    """
    mono = as_channels(audio).mean(axis=1)
    rms = math.sqrt(float(np.mean(mono ** 2)) + 1e-20)
    if rms >= 1e-8:
        mono = mono / rms
    levels = np.array([band_levels(mono, int(sample_rate))[name] for name in BAND_NAMES])
    peak = float(np.max(levels))
    active = levels > peak - 40.0
    if not np.any(active):
        return np.zeros_like(levels)
    shifted = levels - float(np.mean(levels[active]))
    return np.where(active, shifted, 0.0)


def apply_reference_match(audio, sample_rate, report, reference, reference_rate, enabled=True, max_db=REFERENCE_MATCH_MAX_DB, strength=1.0):
    del report
    data = _guard(audio)
    if reference is None:
        reason = "已关闭" if not enabled else "参考曲不可用，已跳过"
        return np.array(data, copy=True), {"bypassed": True, "reason": reason, "amount": 0.0, "filters": []}
    if not enabled:
        return np.array(data, copy=True), {"bypassed": True, "reason": "已关闭", "amount": 0.0, "filters": []}
    ref = _guard(reference)
    if float(np.max(np.abs(ref))) < 1e-5:
        return np.array(data, copy=True), {"bypassed": True, "reason": "参考曲无有效频谱", "amount": 0.0, "filters": []}
    max_db = min(abs(float(max_db)), REFERENCE_MATCH_MAX_DB) * max(0.0, min(float(strength), 1.0))
    source = _shape_levels(data, sample_rate)
    target = _shape_levels(ref, reference_rate)
    delta = np.clip(target - source, -max_db, max_db)
    centers = [math.sqrt(low * min(high, sample_rate / 2.2)) for _name, low, high in BANDS]
    targets = []
    for name, gain, center in zip(BAND_NAMES, delta, centers):
        gain = float(gain)
        if abs(gain) < 0.5 or center >= sample_rate / 2.0:
            continue
        targets.append({"band": name, "hz": center, "q": 0.7, "gain_db": max(-REFERENCE_MATCH_MAX_DB, min(REFERENCE_MATCH_MAX_DB, gain)), "reason": "参考频谱差"})
    return apply_eq_curve(data, sample_rate, targets, REFERENCE_MATCH_MAX_DB)


def _bandpass(audio, sample_rate, low, high):
    """Zero-phase band split. A causal filter's ring would leak into silence."""
    from scipy.signal import butter, sosfiltfilt
    out = np.asarray(audio, dtype=np.float64)
    nyquist = float(sample_rate) / 2.0
    pieces = []
    if low > 5:
        pieces.append(butter(2, min(low, nyquist * 0.95), btype="highpass", fs=sample_rate, output="sos"))
    if high < nyquist * 0.98:
        pieces.append(butter(2, max(high, low + 10), btype="lowpass", fs=sample_rate, output="sos"))
    if not pieces or out.shape[0] < 64:
        return np.zeros_like(out)
    try:
        return sosfiltfilt(np.vstack(pieces), out, axis=0)
    except ValueError:
        return np.zeros_like(out)


def apply_multiband(audio, sample_rate, report, enabled=True, max_gr_db=MULTIBAND_MAX_GR_DB, strength=1.0, ratio=None, threshold_percentile=None, attack_ms=None, release_ms=None, crossover=None):
    data = _guard(audio)
    max_gr_db = min(abs(float(max_gr_db)), MULTIBAND_MAX_GR_DB) * max(0.0, min(float(strength), 1.0))
    if not enabled or not (report.get("needs") or {}).get("multiband") or max_gr_db < 0.05:
        return np.array(data, copy=True), {"bypassed": True, "reason": "已关闭" if not enabled else "no problem", "amount": 0.0, "bands": []}
    split = 120.0 if crossover is None else float(crossover)
    spans = ((20.0, split), (split, 500.0), (500.0, 3000.0), (3000.0, 20000.0))
    slope = 1.5 if ratio is None else max(1.0, float(ratio))
    percentile = 95.0 if threshold_percentile is None else min(99.0, max(50.0, float(threshold_percentile)))
    attack = _ms_coeff(attack_ms, sample_rate, 0.4)
    release = _ms_coeff(release_ms, sample_rate, 0.05)
    out = np.array(data, copy=True)
    used = []
    deepest = 0.0
    full = np.max(np.abs(data), axis=1)
    for low, high in spans:
        if high <= low + 10.0:
            continue
        band = _bandpass(data, sample_rate, low, high)
        mono = np.abs(band.mean(axis=1))
        band_rms = math.sqrt(float(np.mean(band ** 2)) + 1e-20)
        band_peak = float(np.max(np.abs(band))) if band.size else 0.0
        crest = 0.0 if band_rms < 1e-8 else 20.0 * math.log10(max(band_peak, 1e-12) / band_rms)
        if crest < 8.0:
            continue
        follow = _envelope(mono, attack, release)
        if float(np.max(follow)) < 1e-5:
            continue
        threshold = float(np.percentile(follow, percentile))
        over_db = 20.0 * np.log10((follow + 1e-12) / max(threshold, 1e-8))
        gr = np.clip(over_db * (1.0 - 1.0 / slope), 0.0, max_gr_db)
        gr = np.where(full < 1e-4, 0.0, gr)
        if float(np.max(gr)) < 0.2:
            continue
        gain = 10.0 ** (-gr / 20.0)
        out = out + band * (gain.reshape(-1, 1) - 1.0)
        deepest = max(deepest, float(np.max(gr)))
        used.append({"low": low, "high": high, "max_gr_db": float(np.max(gr))})
    if not used:
        return np.array(data, copy=True), {"bypassed": True, "reason": "no problem", "amount": 0.0, "bands": []}
    return _finish(out, {"bypassed": False, "reason": "多段压缩", "amount": deepest, "bands": used})


def apply_transient(audio, sample_rate, report, enabled=True, max_ratio=TRANSIENT_MAX_RATIO, strength=1.0):
    data = _guard(audio)
    limit = min(abs(float(max_ratio)), TRANSIENT_MAX_RATIO) * max(0.0, min(float(strength), 1.0))
    if not enabled or not (report.get("needs") or {}).get("transient") or limit < 1e-4:
        return np.array(data, copy=True), {"bypassed": True, "reason": "已关闭" if not enabled else "no problem", "amount": 0.0}
    mono = np.abs(data.mean(axis=1))
    fast = _envelope(mono, 0.05, 0.2)
    slow = _envelope(mono, 0.8, 0.1)
    mask = np.clip(fast - slow, 0.0, None)
    peak = float(np.max(mask))
    if peak < 1e-6:
        return np.array(data, copy=True), {"bypassed": True, "reason": "no problem", "amount": 0.0}
    mask = mask / peak
    attack = float((report.get("transient") or {}).get("attack_energy", -120.0))
    sustain = float((report.get("transient") or {}).get("sustain_energy", -120.0))
    amount = -limit if attack >= sustain else limit
    out = data * (1.0 + amount * mask.reshape(-1, 1))
    return _finish(out, {"bypassed": False, "reason": "瞬态整形", "amount": float(amount)})


def apply_stereo(audio, sample_rate, report, enabled=True, max_width_delta=STEREO_MAX_WIDTH_DELTA, strength=1.0, correlation_guard=0.0):
    data = _guard(audio)
    limit = min(abs(float(max_width_delta)), STEREO_MAX_WIDTH_DELTA) * max(0.0, min(float(strength), 1.0))
    if data.shape[1] < 2 or not enabled or not (report.get("needs") or {}).get("stereo") or limit < 1e-4:
        copied = np.array(data, copy=True)
        if copied.shape[1] == 1:
            copied = np.repeat(copied, 2, axis=1)
        return copied, {"bypassed": True, "reason": "已关闭" if not enabled else "no problem", "amount": 0.0}
    left = data[:, 0]
    right = data[:, 1]
    mid = 0.5 * (left + right)
    side = 0.5 * (left - right)
    coeff = math.exp(-2.0 * math.pi * 120.0 / float(sample_rate))
    low = np.empty_like(side)
    state = 0.0
    for index, sample in enumerate(side):
        state = (1.0 - coeff) * sample + coeff * state
        low[index] = state
    high = side - low
    before = pearson_correlation(left, right)
    low_correlation = float((report.get("stereo") or {}).get("low_frequency_correlation", 1.0))
    amount = limit if (low_correlation < 0.2 or (before is not None and before < 0.0)) else 0.0
    if amount <= 0.0:
        return np.array(data, copy=True), {"bypassed": True, "reason": "no problem", "amount": 0.0}
    narrowed = low * (1.0 - amount) + high
    new_left = mid + narrowed
    new_right = mid - narrowed
    after = pearson_correlation(new_left, new_right)
    guard = float(correlation_guard)
    if after is not None and before is not None and after < guard and after < before:
        return np.array(data, copy=True), {"bypassed": True, "reason": "相关性保护，已回退", "amount": 0.0}
    out = np.column_stack((new_left, new_right))
    return _finish(out, {"bypassed": False, "reason": "收窄不稳定的立体声", "amount": float(amount)})


def _remove_dc(audio, report, enabled):
    data = np.array(audio, copy=True)
    repair = report.get("repair") or {}
    if not enabled or not repair.get("dc_problem"):
        return data, False
    mean = data.mean(axis=0, keepdims=True)
    if float(np.max(np.abs(data - mean))) < 1e-4:
        return data, False
    return data - mean, True


def apply_repair(audio, sample_rate, report, policy, decisions):
    enabled = bool(policy.get("repair", {}).get("enabled", True))
    if not enabled:
        return _pass(audio, "已关闭")
    current, dc_done = _remove_dc(_guard(audio), report, True)
    declick_on = bool(decisions.get("declick", {}).get("enabled"))
    declip_on = bool(decisions.get("declip", {}).get("enabled"))
    denoise_on = bool(decisions.get("denoise", {}).get("enabled"))
    current, declick_info = apply_declick(current, sample_rate, report, enabled=declick_on)
    current, declip_info = apply_declip(current, sample_rate, report, enabled=declip_on)
    denoise = policy.get("denoise") or {}
    current, denoise_info = apply_denoise(
        current, sample_rate, report, enabled=denoise_on,
        strength=float(decisions.get("denoise", {}).get("strength", 0.0) or 0.0),
        max_reduction_db=denoise.get("max_reduction_db", DENOISE_MAX_REDUCTION_DB),
        max_hf_loss_db=denoise.get("max_hf_loss_db", DENOISE_MAX_HF_LOSS_DB),
    )
    bypassed = (not dc_done) and declick_info.get("bypassed") and declip_info.get("bypassed") and denoise_info.get("bypassed")
    if bypassed:
        return np.array(_guard(audio), copy=True), {"bypassed": True, "reason": "no problem", "declick": declick_info, "declip": declip_info, "denoise": denoise_info, "dc": False}
    return _finish(current, {"bypassed": False, "reason": "修复", "declick": declick_info, "declip": declip_info, "denoise": denoise_info, "dc": dc_done})


def apply_tone(audio, sample_rate, report, policy, decisions, reference=None, reference_rate=None):
    if not policy.get("eq", {}).get("enabled", True) and not policy.get("dynamic_eq", {}).get("enabled", True) and not decisions.get("reference", {}).get("enabled"):
        return _pass(audio, "已关闭")
    current = _guard(audio)
    eq_info = {"bypassed": True, "reason": "已关闭"}
    dynamic_info = {"bypassed": True, "reason": "已关闭"}
    reference_info = {"bypassed": True, "reason": "没有参考曲"}
    if policy.get("eq", {}).get("enabled", True):
        current, eq_info = apply_auto_eq(current, sample_rate, report, enabled=bool(decisions.get("auto_eq", {}).get("enabled")), max_db=policy["eq"].get("max_db", AUTO_EQ_MAX_DB), strength=float(decisions.get("auto_eq", {}).get("strength", 0.0) or 0.0))
    dynamic_policy = policy.get("dynamic_eq") or {}
    if dynamic_policy.get("enabled", True):
        current, dynamic_info = apply_dynamic_eq(
            current, sample_rate, report,
            enabled=bool(decisions.get("dynamic_eq", {}).get("enabled")),
            max_db=dynamic_policy.get("max_db", DYNAMIC_EQ_MAX_DB),
            strength=float(decisions.get("dynamic_eq", {}).get("strength", 0.0) or 0.0),
            q=dynamic_policy.get("q"),
            frequency=dynamic_policy.get("frequency"),
            threshold_percentile=dynamic_policy.get("threshold_percentile"),
            attack_ms=dynamic_policy.get("attack_ms"),
            release_ms=dynamic_policy.get("release_ms"),
        )
    current, reference_info = apply_reference_match(
        current, sample_rate, report, reference, reference_rate,
        enabled=bool(decisions.get("reference", {}).get("enabled")),
        max_db=policy["reference"].get("max_db", REFERENCE_MATCH_MAX_DB),
        strength=float(decisions.get("reference", {}).get("strength", 0.0) or 0.0),
    )
    bypassed = eq_info.get("bypassed") and dynamic_info.get("bypassed") and reference_info.get("bypassed")
    if bypassed:
        return np.array(_guard(audio), copy=True), {"bypassed": True, "reason": "no problem", "eq": eq_info, "dynamic_eq": dynamic_info, "reference": reference_info}
    return _finish(current, {"bypassed": False, "reason": "音色", "eq": eq_info, "dynamic_eq": dynamic_info, "reference": reference_info})


def apply_dynamics(audio, sample_rate, report, policy, decisions):
    if not policy.get("multiband", {}).get("enabled", True) and not policy.get("transient", {}).get("enabled", True):
        return _pass(audio, "已关闭")
    current = _guard(audio)
    multi_info = {"bypassed": True}
    transient_info = {"bypassed": True}
    multi_policy = policy.get("multiband") or {}
    if multi_policy.get("enabled", True):
        current, multi_info = apply_multiband(
            current, sample_rate, report,
            enabled=bool(decisions.get("multiband", {}).get("enabled")),
            max_gr_db=multi_policy.get("max_gr_db", MULTIBAND_MAX_GR_DB),
            strength=float(decisions.get("multiband", {}).get("strength", 0.0) or 0.0),
            ratio=multi_policy.get("ratio"),
            threshold_percentile=multi_policy.get("threshold_percentile"),
            attack_ms=multi_policy.get("attack_ms"),
            release_ms=multi_policy.get("release_ms"),
            crossover=multi_policy.get("crossover"),
        )
    if policy.get("transient", {}).get("enabled", True):
        current, transient_info = apply_transient(current, sample_rate, report, enabled=bool(decisions.get("transient", {}).get("enabled")), max_ratio=policy["transient"].get("max_ratio", TRANSIENT_MAX_RATIO), strength=float(decisions.get("transient", {}).get("strength", 0.0) or 0.0))
    if multi_info.get("bypassed") and transient_info.get("bypassed"):
        return np.array(_guard(audio), copy=True), {"bypassed": True, "reason": "no problem", "multiband": multi_info, "transient": transient_info}
    return _finish(current, {"bypassed": False, "reason": "动态", "multiband": multi_info, "transient": transient_info})


def apply_stereo_stage(audio, sample_rate, report, policy, decisions):
    if not policy.get("stereo", {}).get("enabled", True):
        out, info = _pass(audio, "已关闭")
        if out.shape[1] == 1:
            out = np.repeat(out, 2, axis=1)
        return out, info
    return apply_stereo(
        audio, sample_rate, report,
        enabled=bool(decisions.get("stereo", {}).get("enabled")),
        max_width_delta=policy["stereo"].get("max_width_delta", STEREO_MAX_WIDTH_DELTA),
        strength=float(decisions.get("stereo", {}).get("strength", 0.0) or 0.0),
        correlation_guard=policy["stereo"].get("correlation_guard", 0.0),
    )


def run_polish_buffers(audio, sample_rate, policy, reference=None, reference_rate=None):
    """Repair, tone, dynamics, and stereo. Master stays outside this function."""
    from core.polish_policy import clamp_policy, route_polish
    from core.polish_analysis import analyze_polish_buffer
    report = analyze_polish_buffer(audio, sample_rate)
    if report["repair"]["nan_inf"]:
        raise NonFiniteAudio("音频包含 NaN/Inf")
    policy = clamp_policy(policy)
    decisions = route_polish(report, policy)
    repaired, repair_info = apply_repair(audio, sample_rate, report, policy, decisions)
    tone, tone_info = apply_tone(repaired, sample_rate, report, policy, decisions, reference, reference_rate)
    dynamics, dynamics_info = apply_dynamics(tone, sample_rate, report, policy, decisions)
    stereo, stereo_info = apply_stereo_stage(dynamics, sample_rate, report, policy, decisions)
    return {
        "report": report,
        "decisions": decisions,
        "source": np.array(_guard(audio), copy=True),
        "repaired": repaired,
        "tone": tone,
        "dynamics": dynamics,
        "stereo": stereo,
        "info": {"repair": repair_info, "tone": tone_info, "dynamics": dynamics_info, "stereo": stereo_info},
    }
