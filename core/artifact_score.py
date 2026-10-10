"""Heuristic waveform numbers for comparison. They are not a quality score.

The figures describe level, clipping, zero crossings, spectral flatness, and
high-frequency energy. A listener has not judged them. The job is not failed
from this record.
"""
import math
import os

import numpy as np

HEADING = "## 伪影启发式"
LABEL = "启发式指标，不是音质真值"
SILENCE_RMS = 1e-4
CLIP_ABS = 0.999
HF_HZ = 6000.0


def _blank(reason):
    return {
        "heuristic": True,
        "quality_truth": False,
        "listening_quality": "not_assessed",
        "blocks_job": False,
        "label": LABEL,
        "reason": reason,
        "silent": None,
        "peak": None,
        "rms": None,
        "crest_factor": None,
        "zero_crossing_rate": None,
        "spectral_flatness": None,
        "high_frequency_ratio": None,
        "clip_ratio": None,
        "frames": None,
        "sample_rate": None,
    }


def assess_artifact(samples, sample_rate):
    """Measure one buffer. The label stays heuristic for every outcome."""
    if type(sample_rate) is bool or type(sample_rate) not in (int, float):
        return _blank("采样率无效")
    if not math.isfinite(sample_rate) or sample_rate <= 0:
        return _blank("采样率无效")
    try:
        audio = np.asarray(samples, dtype=np.float64)
    except (TypeError, ValueError):
        return _blank("没有可检查的音频")
    if audio.ndim == 2 and audio.shape[1] >= 1:
        audio = audio.mean(axis=1)
    if audio.ndim != 1:
        return _blank("没有可检查的音频")
    record = _blank("")
    record["frames"] = int(audio.size)
    record["sample_rate"] = int(sample_rate) if float(sample_rate).is_integer() else float(sample_rate)
    if audio.size == 0:
        record["reason"] = "音频为空"
        return record
    if not np.isfinite(audio).all():
        record["reason"] = "音频含有非有限数值"
        return record
    peak = float(np.max(np.abs(audio)))
    rms = float(math.sqrt(float(np.mean(np.square(audio)))))
    record["peak"] = peak
    record["rms"] = rms
    record["silent"] = rms < SILENCE_RMS
    record["clip_ratio"] = float(np.mean(np.abs(audio) >= CLIP_ABS))
    if audio.size > 1:
        signs = np.signbit(audio)
        record["zero_crossing_rate"] = float(np.mean(signs[1:] != signs[:-1]))
    else:
        record["zero_crossing_rate"] = 0.0
    if record["silent"]:
        record["reason"] = "电平接近静音，启发式数字不能当成音质"
        return record
    record["crest_factor"] = peak / rms
    spectrum = np.abs(np.fft.rfft(audio))
    body = spectrum[1:] if spectrum.size > 1 else spectrum
    mean = float(np.mean(body)) if body.size else 0.0
    if mean > 0 and body.size and np.all(body > 0):
        geometric = math.exp(float(np.mean(np.log(body))))
        record["spectral_flatness"] = geometric / mean
    else:
        record["spectral_flatness"] = 0.0
    power = np.square(spectrum)
    total = float(np.sum(power))
    if total > 0:
        freqs = np.fft.rfftfreq(audio.size, d=1.0 / float(sample_rate))
        record["high_frequency_ratio"] = float(np.sum(power[freqs >= HF_HZ]) / total)
    else:
        record["high_frequency_ratio"] = 0.0
    record["reason"] = LABEL
    return record


def unread_record(reason):
    """A failed read is still a heuristic record, not a job failure."""
    return _blank(reason)


def assess_artifact_file(path):
    """Read one WAV and measure it. The caller decides whether a miss is fatal."""
    import soundfile as sf

    audio, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    record = assess_artifact(audio, int(sample_rate))
    record["source_name"] = os.path.basename(path)
    return record


def format_artifact_report(record):
    record = record if isinstance(record, dict) else {}
    lines = [
        HEADING,
        "",
        "- 说明：%s" % record.get("label", LABEL),
        "- 音质真值：否",
        "- 听感：not_assessed",
        "- 不改变任务成败：%s" % ("是" if record.get("blocks_job") is False else "否"),
        "- 原因：%s" % record.get("reason", ""),
        "- 峰值：%s" % _text(record.get("peak")),
        "- RMS：%s" % _text(record.get("rms")),
        "- crest factor：%s" % _text(record.get("crest_factor")),
        "- 过零率：%s" % _text(record.get("zero_crossing_rate")),
        "- 频谱平坦度：%s" % _text(record.get("spectral_flatness")),
        "- 高频能量比：%s" % _text(record.get("high_frequency_ratio")),
        "- 削波比例：%s" % _text(record.get("clip_ratio")),
    ]
    return "\n".join(lines) + "\n"


def append_artifact_report(report_path, record):
    """Append the heuristic section once."""
    with open(report_path, encoding="utf-8") as handle:
        current = handle.read()
    if HEADING in current:
        return
    section = format_artifact_report(record)
    with open(report_path, "a", encoding="utf-8") as handle:
        if current and not current.endswith("\n"):
            handle.write("\n")
        handle.write("\n")
        handle.write(section)


def _text(value):
    if type(value) not in (int, float) or isinstance(value, bool):
        return "无"
    return "%.6g" % value
