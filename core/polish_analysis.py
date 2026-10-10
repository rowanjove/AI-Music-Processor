"""Shared measurements for polish. Later stages read this report and do not re-analyze.

Mastering loudness stays in measure_loudness_ebur128. This module fills the
extra groups that function does not return. When a file measurement exists,
attach_polish_analysis copies integrated LUFS, true peak, and LRA over the
estimates. Full-band correlation goes through core.qc.pearson_correlation.
"""
import math

import numpy as np

from core.qc import pearson_correlation

BANDS = (
    ("sub", 20.0, 60.0),
    ("bass", 60.0, 120.0),
    ("low_mid", 120.0, 500.0),
    ("mid", 500.0, 2000.0),
    ("upper_mid", 2000.0, 4000.0),
    ("presence", 4000.0, 8000.0),
    ("air", 8000.0, 20000.0),
)
BAND_NAMES = tuple(name for name, _low, _high in BANDS)
_FLOOR_DB = -120.0


def _db(power):
    return float(10.0 * math.log10(max(float(power), 1e-20)))


def _finite(value, fallback=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if not math.isfinite(number):
        return float(fallback)
    return number


def as_channels(audio):
    data = np.asarray(audio, dtype=np.float64)
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    elif data.ndim != 2:
        raise ValueError("音频形状无效")
    return data


def band_power(mono, sample_rate, low, high):
    """Energy in one frequency span, as a linear power."""
    mono = np.asarray(mono, dtype=np.float64).reshape(-1)
    if mono.size == 0 or sample_rate <= 0:
        return 0.0
    window = np.hanning(mono.size) if mono.size > 1 else np.ones(1)
    spectrum = np.fft.rfft(mono * window)
    freqs = np.fft.rfftfreq(mono.size, 1.0 / float(sample_rate))
    nyquist = float(sample_rate) / 2.0
    high = min(float(high), nyquist)
    mask = (freqs >= float(low)) & (freqs < high)
    if not np.any(mask):
        return 0.0
    power = np.abs(spectrum[mask]) ** 2
    return float(np.mean(power))


def band_levels(mono, sample_rate):
    levels = {}
    for name, low, high in BANDS:
        levels[name] = _db(band_power(mono, sample_rate, low, high))
    return levels


def _frame_rms(mono, frame, hop):
    mono = np.asarray(mono, dtype=np.float64).reshape(-1)
    if mono.size == 0:
        return np.zeros(1)
    if mono.size < frame:
        return np.array([math.sqrt(float(np.mean(mono ** 2)) + 1e-20)])
    count = 1 + (mono.size - frame) // hop
    values = np.empty(count)
    for index in range(count):
        start = index * hop
        chunk = mono[start:start + frame]
        values[index] = math.sqrt(float(np.mean(chunk ** 2)) + 1e-20)
    return values


def _clipping_regions(mono, sample_rate):
    abs_mono = np.abs(mono)
    hot = abs_mono >= 0.999
    regions = []
    start = None
    for index, flagged in enumerate(hot):
        if flagged and start is None:
            start = index
        elif not flagged and start is not None:
            regions.append(_clip_region(mono, sample_rate, start, index - 1))
            start = None
    if start is not None:
        regions.append(_clip_region(mono, sample_rate, start, len(mono) - 1))
    return regions


def _clip_region(mono, sample_rate, start, end):
    samples = int(end - start + 1)
    short = 4
    moderate = max(short, int(0.002 * sample_rate))
    if samples <= short:
        severity = "light"
    elif samples <= moderate:
        severity = "moderate"
    else:
        severity = "severe"
    return {
        "start": int(start),
        "end": int(end),
        "samples": samples,
        "severity": severity,
        "peak": _finite(np.max(np.abs(mono[start:end + 1]))),
    }


def _detect_clicks(mono, sample_rate):
    """Short isolated spikes are clicks. A decaying burst is musical, not a click."""
    mono = np.asarray(mono, dtype=np.float64).reshape(-1)
    count = int(mono.size)
    events = []
    if count < 16:
        return events
    magnitude = np.abs(mono)
    difference = np.abs(np.diff(mono, prepend=mono[:1]))
    median_diff = float(np.median(difference))
    threshold = max(0.2, 8.0 * median_diff)
    candidates = np.flatnonzero(difference > threshold)
    musical_width = max(8, int(0.008 * sample_rate))
    low_width = max(8, int(0.001 * sample_rate))
    index = 0
    while index < candidates.size:
        position = int(candidates[index])
        if position < 2 or position >= count - 2:
            index += 1
            continue
        peak = float(magnitude[position])
        if peak < 0.12:
            index += 1
            continue
        level = max(0.08, peak * 0.25)
        left = position
        while left > 0 and magnitude[left - 1] >= level and position - left < musical_width * 2:
            left -= 1
        right = position
        while right < count - 1 and magnitude[right + 1] >= level and right - position < musical_width * 2:
            right += 1
        width = int(right - left + 1)
        left_span = magnitude[max(0, left - 32):left]
        right_span = magnitude[right + 1:min(count, right + 33)]
        neighborhood = np.concatenate((left_span, right_span)) if left_span.size or right_span.size else np.array([1e-8])
        base = float(np.median(neighborhood)) + 1e-8
        ratio = peak / base
        if width <= 6 and ratio >= 8.0 and peak >= 0.15:
            kind, confidence = "click", "high"
        elif width <= low_width and ratio >= 5.0 and peak >= 0.12:
            kind, confidence = "click", "low"
        elif width >= musical_width:
            kind, confidence = "musical", "none"
        else:
            index += 1
            continue
        events.append({
            "index": position,
            "start": int(left),
            "end": int(right),
            "width": width,
            "peak": _finite(peak),
            "ratio": _finite(ratio),
            "confidence": confidence,
            "kind": kind,
        })
        index += 1
        while index < candidates.size and int(candidates[index]) <= right:
            index += 1
    return events


def _lufs_estimate(mono, sample_rate):
    """Block estimate used only when the file loudness measurement is absent."""
    mono = np.asarray(mono, dtype=np.float64).reshape(-1)
    if mono.size == 0:
        return _FLOOR_DB, _FLOOR_DB, 0.0
    if mono.size > 1:
        weighted = np.empty_like(mono)
        weighted[0] = mono[0]
        weighted[1:] = mono[1:] - 0.95 * mono[:-1]
    else:
        weighted = mono
    block = max(8, int(0.4 * sample_rate))
    hop = max(4, block // 4)
    powers = []
    if weighted.size < block:
        powers.append(float(np.mean(weighted ** 2)))
    else:
        for start in range(0, weighted.size - block + 1, hop):
            powers.append(float(np.mean(weighted[start:start + block] ** 2)))
    lufs = np.array([-0.691 + _db(power) for power in powers])
    absolute = lufs[lufs >= -70.0]
    if absolute.size == 0:
        integrated = float(np.max(lufs))
        span = 0.0
    else:
        energy = float(np.mean(10.0 ** (absolute / 10.0)))
        integrated = float(-0.691 + 10.0 * math.log10(max(energy, 1e-20)))
        span = float(np.max(absolute) - np.min(absolute))
    return _finite(integrated, _FLOOR_DB), _finite(float(np.max(lufs)), _FLOOR_DB), _finite(span)


def _needs(level, dynamics, spectrum, noise, repair, stereo, transient):
    active = [spectrum[name] for name in BAND_NAMES]
    loudest = max(active)
    active_count = sum(1 for value in active if value > loudest - 20.0)
    median_band = float(np.median(active))
    low_mid_excess = spectrum["low_mid"] - median_band
    harsh_excess = max(spectrum["upper_mid"], spectrum["presence"]) - median_band
    sub_excess = spectrum["sub"] - spectrum["bass"]
    high_clicks = [item for item in repair["click_events"] if item["confidence"] == "high" and item["kind"] == "click"]
    low_clicks = [item for item in repair["click_events"] if item["confidence"] == "low" and item["kind"] == "click"]
    clip_regions = repair["clipping_regions"]
    denoise = (
        not repair["silence"]
        and noise["quiet_frame_count"] > 0
        and noise["noise_floor"] > -55.0
        and (noise["hf_hiss"] > -50.0 or noise["low_frequency_hum"] > 8.0)
    )
    auto_eq = (
        active_count >= 3
        and (low_mid_excess > 4.0 or harsh_excess > 4.0 or sub_excess > 6.0 or spectrum["air"] < spectrum["presence"] - 12.0)
    )
    dynamic_eq = active_count >= 3 and transient["crest_distribution"]["p99"] > 8.0 and (harsh_excess > 2.0 or low_mid_excess > 2.0)
    multiband = not repair["silence"] and dynamics["dynamic_range"] > 12.0 and level["crest_factor"] > 12.0
    transient = not repair["silence"] and transient["transient_density"] > 8.0 and transient["attack_energy"] > transient["sustain_energy"] + 6.0
    stereo_problem = (
        stereo["low_frequency_correlation"] is not None
        and stereo["low_frequency_correlation"] < 0.2
        and stereo["correlation"] is not None
    ) or (
        stereo["correlation"] is not None and stereo["correlation"] < 0.0
    )
    needs = {
        "declick": bool(high_clicks),
        "declip": bool(clip_regions),
        "denoise": bool(denoise),
        "auto_eq": bool(auto_eq),
        "dynamic_eq": bool(dynamic_eq),
        "multiband": bool(multiband),
        "transient": bool(transient),
        "stereo": bool(stereo_problem),
    }
    severity = {
        "declick": 1.0 if high_clicks else (0.3 if low_clicks else 0.0),
        "declip": 1.0 if any(item["severity"] == "severe" for item in clip_regions) else (0.6 if clip_regions else 0.0),
        "denoise": 0.7 if denoise else 0.0,
        "auto_eq": 0.7 if auto_eq else 0.0,
        "dynamic_eq": 0.6 if dynamic_eq else 0.0,
        "multiband": 0.6 if multiband else 0.0,
        "transient": 0.6 if transient else 0.0,
        "stereo": 0.7 if stereo_problem else 0.0,
    }
    reasons = {
        "declick": "高置信度 click" if high_clicks else ("只报告低置信度 click" if low_clicks else "没有 click"),
        "declip": "检测到削波" if clip_regions else "没有削波",
        "denoise": "安静段里有持续噪声" if denoise else "噪声低于门限",
        "auto_eq": "长期频段偏离" if auto_eq else "频谱没有明显失衡",
        "dynamic_eq": "频段短时过量" if dynamic_eq else "没有需要动态均衡的突发",
        "multiband": "动态范围偏大" if multiband else "动态不需要压缩",
        "transient": "瞬态明显强于持续段" if transient else "瞬态正常",
        "stereo": "低频相关性或相位需要收窄" if stereo_problem else "立体声正常",
    }
    return needs, {key: _finite(value) for key, value in severity.items()}, reasons


def analyze_polish_buffer(audio, sample_rate):
    """Return the polish report for one buffer. Numbers stay finite on a normal tone."""
    sample_rate = int(sample_rate)
    if sample_rate <= 0:
        raise ValueError("采样率无效")
    data = as_channels(audio)
    non_finite = not bool(np.isfinite(data).all())
    if non_finite:
        data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
    if data.shape[0] == 0:
        data = np.zeros((1, data.shape[1] if data.ndim == 2 else 1))
        silence = True
    mono = data.mean(axis=1)
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    rms = float(math.sqrt(float(np.mean(data ** 2)) + 1e-20))
    silence = bool(peak < 1e-5 or rms < 1e-5)
    crest = 0.0 if rms < 1e-8 else float(20.0 * math.log10(max(peak, 1e-12) / rms))
    dc_channels = data.mean(axis=0)
    dc_offset = float(np.max(np.abs(dc_channels))) if dc_channels.size else 0.0
    integrated, short_term, short_span = _lufs_estimate(mono, sample_rate)
    true_peak = _db(max(peak, 1e-12))
    frame = max(32, int(0.05 * sample_rate))
    hop = max(16, frame // 2)
    frames = _frame_rms(mono, frame, hop)
    frame_db = 20.0 * np.log10(frames + 1e-12)
    noise_floor = float(np.percentile(frame_db, 10))
    median_frame = float(np.median(frame_db))
    quiet = frame_db <= (median_frame - 12.0)
    quiet_count = int(np.count_nonzero(quiet))
    spectrum = band_levels(mono, sample_rate)
    if quiet_count:
        # HF share of the quiet frames only. A continuous tone has no such frames.
        quiet_span = quiet
        hiss_values = []
        hum_values = []
        step = max(1, mono.size // max(quiet_count, 1))
        for index, flagged in enumerate(quiet_span):
            if not flagged:
                continue
            start = min(mono.size - 1, index * hop)
            chunk = mono[start:start + frame]
            if chunk.size < 8:
                continue
            hiss_values.append(band_power(chunk, sample_rate, 8000, min(16000, sample_rate / 2.0)))
            hum_values.append(_hum_excess(chunk, sample_rate))
        hf_hiss = _db(float(np.median(hiss_values))) if hiss_values else _FLOOR_DB
        hum = float(np.median(hum_values)) if hum_values else 0.0
    else:
        hf_hiss = _FLOOR_DB
        hum = 0.0
    stationary = 0.0
    if quiet_count >= 2:
        stationary = float(max(0.0, 12.0 - float(np.std(frame_db[quiet]))))
    dynamic_range = float(np.percentile(frame_db, 95) - np.percentile(frame_db, 10))
    sections = min(8, max(1, mono.size // max(frame, 1)))
    edges = np.array_split(mono, sections)
    section_db = [_db(float(np.mean(part ** 2)) if part.size else 1e-20) for part in edges]
    macro = float(max(section_db) - min(section_db)) if section_db else 0.0
    left = data[:, 0]
    right = data[:, 1] if data.shape[1] > 1 else data[:, 0]
    mid = 0.5 * (left + right)
    side = 0.5 * (left - right)
    correlation = pearson_correlation(left, right) if data.shape[1] > 1 else 1.0
    low_left = _lowpass_mean(left, sample_rate, 120.0)
    low_right = _lowpass_mean(right, sample_rate, 120.0)
    low_correlation = pearson_correlation(low_left, low_right) if data.shape[1] > 1 else 1.0
    if low_correlation is None:
        low_correlation = 1.0 if not silence else 0.0
    if correlation is None:
        correlation = 1.0 if not silence else 0.0
    high_width = _finite(band_power(side, sample_rate, 8000, min(20000, sample_rate / 2.0)) / max(band_power(mid, sample_rate, 8000, min(20000, sample_rate / 2.0)), 1e-20))
    high_width = _finite(10.0 * math.log10(max(high_width, 1e-12)))
    clicks = [] if silence or non_finite else _detect_clicks(mono, sample_rate)
    regions = [] if silence or non_finite else _clipping_regions(np.max(np.abs(data), axis=1), sample_rate)
    density, attack, sustain, crest_distribution = _transients(mono, sample_rate, frames)
    repair = {
        "clipping_regions": regions,
        "click_events": clicks,
        "dc_offset": _finite(dc_offset),
        "dc_problem": bool(dc_offset > 0.005),
        "nan_inf": bool(non_finite),
        "silence": bool(silence or peak < 1e-5),
        "clipped_samples": int(np.count_nonzero(np.abs(data) >= 1.0)),
    }
    level = {
        "sample_peak": _finite(peak),
        "true_peak": _finite(true_peak, _FLOOR_DB),
        "rms": _finite(rms),
        "lufs_i": _finite(integrated, _FLOOR_DB),
        "lufs_s": _finite(short_term, _FLOOR_DB),
        "crest_factor": _finite(crest),
    }
    dynamics = {
        "dynamic_range": _finite(0.0 if silence else dynamic_range),
        "short_term_loudness_range": _finite(short_span),
        "macro_dynamics": _finite(0.0 if silence else macro),
    }
    noise = {
        "noise_floor": _finite(noise_floor, _FLOOR_DB),
        "stationary_noise": _finite(stationary),
        "hf_hiss": _finite(hf_hiss, _FLOOR_DB),
        "low_frequency_hum": _finite(hum),
        "quiet_frame_count": quiet_count,
    }
    stereo = {
        "left_rms": _finite(math.sqrt(float(np.mean(left ** 2)) + 1e-20)),
        "right_rms": _finite(math.sqrt(float(np.mean(right ** 2)) + 1e-20)),
        "mid_energy": _finite(float(np.mean(mid ** 2))),
        "side_energy": _finite(float(np.mean(side ** 2))),
        "correlation": _finite(correlation),
        "low_frequency_correlation": _finite(low_correlation),
        "high_frequency_width": _finite(high_width, _FLOOR_DB),
    }
    transient_report = {
        "transient_density": _finite(density),
        "attack_energy": _finite(attack, _FLOOR_DB),
        "sustain_energy": _finite(sustain, _FLOOR_DB),
        "crest_distribution": crest_distribution,
    }
    needs, severity, reasons = _needs(level, dynamics, spectrum, noise, repair, stereo, transient_report)
    if repair["nan_inf"] or repair["silence"]:
        needs = {key: False for key in needs}
    no_problem = (not repair["nan_inf"]) and (not repair["silence"]) and (not repair["dc_problem"]) and (not any(needs.values()))
    return {
        "level": level,
        "dynamics": dynamics,
        "spectrum": spectrum,
        "noise": noise,
        "repair": repair,
        "stereo": stereo,
        "transient": transient_report,
        "needs": needs,
        "severity": severity,
        "reasons": reasons,
        "no_problem": bool(no_problem),
    }


def _hum_excess(chunk, sample_rate):
    if chunk.size < 16:
        return 0.0
    spectrum = np.abs(np.fft.rfft(chunk * np.hanning(chunk.size))) ** 2
    freqs = np.fft.rfftfreq(chunk.size, 1.0 / float(sample_rate))
    excess = 0.0
    for tone in (50.0, 60.0):
        if tone >= sample_rate / 2.0:
            continue
        center = int(np.argmin(np.abs(freqs - tone)))
        left = max(0, center - 3)
        right = min(spectrum.size, center + 4)
        neighbors = np.concatenate((spectrum[left:center], spectrum[center + 1:right]))
        if neighbors.size == 0:
            continue
        base = float(np.mean(neighbors)) + 1e-20
        excess = max(excess, _db(spectrum[center]) - _db(base))
    return excess


def _one_pole(mono, sample_rate, cutoff, highpass):
    mono = np.asarray(mono, dtype=np.float64).reshape(-1)
    if mono.size == 0 or cutoff <= 0 or cutoff >= sample_rate / 2.0:
        return mono.copy()
    coeff = math.exp(-2.0 * math.pi * float(cutoff) / float(sample_rate))
    out = np.empty_like(mono)
    state = 0.0
    for index, sample in enumerate(mono):
        state = (1.0 - coeff) * sample + coeff * state
        out[index] = sample - state if highpass else state
    return out


def _lowpass_mean(mono, sample_rate, cutoff):
    return _one_pole(mono, sample_rate, cutoff, highpass=False)


def _highpass_mean(mono, sample_rate, cutoff):
    return _one_pole(mono, sample_rate, cutoff, highpass=True)


def _transients(mono, sample_rate, frames):
    mono = np.asarray(mono, dtype=np.float64).reshape(-1)
    if mono.size < 8:
        return 0.0, _FLOOR_DB, _FLOOR_DB, {"p50": 1.0, "p90": 1.0, "p99": 1.0}
    diff = np.abs(np.diff(mono, prepend=mono[:1]))
    frame = max(32, int(0.02 * sample_rate))
    hop = frame
    peaks = []
    onsets = 0
    attack_power = 0.0
    sustain_power = 0.0
    attack_count = 0
    sustain_count = 0
    median = float(np.median(diff)) + 1e-8
    for start in range(0, mono.size - frame, hop):
        chunk = diff[start:start + frame]
        peak = float(np.max(chunk))
        rms = float(np.sqrt(np.mean(mono[start:start + frame] ** 2)) + 1e-12)
        peaks.append(peak / rms)
        if peak > median * 8.0 and peak > 0.02:
            onsets += 1
            attack = mono[start:start + min(frame, int(0.01 * sample_rate))]
            sustain = mono[start + int(0.01 * sample_rate):start + frame]
            attack_power += float(np.mean(attack ** 2))
            attack_count += 1
            if sustain.size:
                sustain_power += float(np.mean(sustain ** 2))
                sustain_count += 1
    duration = max(mono.size / float(sample_rate), 1e-6)
    density = onsets / duration
    attack_db = _db(attack_power / attack_count) if attack_count else _FLOOR_DB
    sustain_db = _db(sustain_power / sustain_count) if sustain_count else _FLOOR_DB
    if not peaks:
        peaks = [1.0]
    array = np.array(peaks, dtype=np.float64)
    distribution = {
        "p50": _finite(np.percentile(array, 50), 1.0),
        "p90": _finite(np.percentile(array, 90), 1.0),
        "p99": _finite(np.percentile(array, 99), 1.0),
    }
    return _finite(density), _finite(attack_db, _FLOOR_DB), _finite(sustain_db, _FLOOR_DB), distribution


def attach_polish_analysis(result, audio_path):
    """Merge buffer measurements into analyze_audio's existing flat report."""
    import soundfile as sf
    from core.qc import sample_metrics
    audio, sample_rate = sf.read(str(audio_path), always_2d=True, dtype="float32")
    report = analyze_polish_buffer(audio, sample_rate)
    if isinstance(result, dict) and "integrated_lufs" in result:
        report["level"]["lufs_i"] = _finite(result["integrated_lufs"], report["level"]["lufs_i"])
        report["level"]["true_peak"] = _finite(result["true_peak_dbtp"], report["level"]["true_peak"])
        report["dynamics"]["short_term_loudness_range"] = _finite(result.get("lra", report["dynamics"]["short_term_loudness_range"]))
    if not report["repair"]["nan_inf"]:
        metrics = sample_metrics(audio_path)
        report["level"]["sample_peak"] = _finite(metrics["sample_peak"])
        report["repair"]["clipped_samples"] = int(metrics["clipped_samples"])
        report["repair"]["dc_offset"] = _finite(max(abs(value) for value in metrics["dc_offset"]))
        report["repair"]["dc_problem"] = bool(report["repair"]["dc_offset"] > 0.005)
        if metrics["stereo_correlation"] is not None:
            report["stereo"]["correlation"] = _finite(metrics["stereo_correlation"])
        report["no_problem"] = (
            (not report["repair"]["nan_inf"])
            and (not report["repair"]["silence"])
            and (not report["repair"]["dc_problem"])
            and (not any(report["needs"].values()))
        )
    merged = dict(result)
    merged.update(report)
    return merged


def spectrum_fingerprint(audio, sample_rate):
    """Stable hash of octave-band levels. Reference cache uses this, not Repair."""
    import hashlib
    from core.storage import canonical_json
    levels = band_levels(as_channels(audio).mean(axis=1), int(sample_rate))
    payload = canonical_json({name: round(float(levels[name]), 3) for name in BAND_NAMES})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
