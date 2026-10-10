"""Attenuate the file RVC will read. This does not run the model.

The default gain is the attenuation from the measured short-project cleaned
vocal down to -6 dBFS. That ceiling is the voice-plan limit. The gain number
is calculated from the measured peak. 0 dB leaves the old samples unchanged.
"""
import hashlib
import math
import os

import numpy as np
import soundfile as sf

# sample_metrics peak of stems/02_vocal_cleaned.wav on the VCQ-001 short project.
# Clean had already limited it; clipped_samples was 0. The three VCQ-001
# observation points are not this file.
OBSERVED_RVC_INPUT_PEAK = 0.6029990315437317
TARGET_PEAK_DBFS = -6.0
GAIN_MIN_DB = -12.0
GAIN_MAX_DB = 0.0
DEFAULT_RVC_INPUT_GAIN_DB = TARGET_PEAK_DBFS - 20.0 * math.log10(OBSERVED_RVC_INPUT_PEAK)
HEADROOM_STRATEGIES = ("auto_peak", "fixed")


def validate_rvc_headroom_strategy(value):
    if value not in HEADROOM_STRATEGIES:
        raise ValueError("RVC 输入余量策略必须是 auto_peak 或 fixed")
    return value


def validate_rvc_input_gain_db(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("RVC 输入增益无效")
    if value < GAIN_MIN_DB or value > GAIN_MAX_DB:
        raise ValueError("RVC 输入增益超出 -12 到 0 dB")
    return float(value)


def resolve_rvc_input_gain_db(parameters):
    """Resolve a frozen fixed gain; missing fields preserve historical behavior."""
    if not isinstance(parameters, dict) or "rvc_input_gain_db" not in parameters:
        return 0.0
    if parameters["rvc_input_gain_db"] is None:
        return 0.0
    return validate_rvc_input_gain_db(parameters["rvc_input_gain_db"])


def resolve_rvc_input_headroom(parameters, source_path=None):
    """Return this job's frozen policy and measured per-song gain decision.

    Old snapshots have no strategy field and retain their recorded fixed gain
    (or zero if they predate that field). New auto_peak jobs measure the actual
    cleaned vocal immediately before RVC, so batch songs get independent gains.
    """
    strategy = parameters.get("rvc_input_headroom_strategy") if isinstance(parameters, dict) else None
    if strategy is None:
        gain_db = resolve_rvc_input_gain_db(parameters)
        return {
            "strategy": "legacy_fixed",
            "gain_db": gain_db,
            "target_peak_dbfs": TARGET_PEAK_DBFS,
            "input_peak": None,
            "input_peak_dbfs": None,
            "gain_limited": False,
            "measured": False,
        }
    strategy = validate_rvc_headroom_strategy(strategy)
    if strategy == "fixed":
        gain_db = resolve_rvc_input_gain_db(parameters)
        return {
            "strategy": strategy,
            "gain_db": gain_db,
            "target_peak_dbfs": TARGET_PEAK_DBFS,
            "input_peak": None,
            "input_peak_dbfs": None,
            "gain_limited": False,
            "measured": False,
        }
    if not source_path:
        raise ValueError("auto_peak 需要实际 RVC 输入音频")
    import soundfile as sf

    audio, _sample_rate = sf.read(source_path, dtype="float32", always_2d=True)
    if audio.size == 0 or not np.isfinite(audio).all():
        raise ValueError("auto_peak 拒绝空音频或 NaN/Inf")
    peak = float(np.max(np.abs(audio)))
    peak_dbfs = 20.0 * math.log10(peak) if peak > 0 else None
    requested_gain = TARGET_PEAK_DBFS - peak_dbfs if peak_dbfs is not None else 0.0
    gain_db = min(GAIN_MAX_DB, max(GAIN_MIN_DB, requested_gain))
    return {
        "strategy": strategy,
        "gain_db": validate_rvc_input_gain_db(gain_db),
        "target_peak_dbfs": TARGET_PEAK_DBFS,
        "input_peak": peak,
        "input_peak_dbfs": peak_dbfs,
        "gain_limited": not math.isclose(gain_db, requested_gain, abs_tol=1e-12),
        "measured": True,
    }


def write_headroom_report(path, record, source_path, output_path=None):
    """Write an auditable decision record beside the produced RVC artifact."""
    import json

    payload = dict(record)
    payload.update({
        "schema_version": 1,
        "source_path": os.path.abspath(source_path),
        "source_sha256": _sha256(source_path),
        "output_path": os.path.abspath(output_path) if output_path else None,
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
    })
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return path


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apply_rvc_input_gain(source, dest, gain_db):
    gain_db = validate_rvc_input_gain_db(gain_db)
    audio, sample_rate = sf.read(source, dtype="float32", always_2d=True)
    if not len(audio) or not np.isfinite(audio).all():
        raise ValueError("RVC 输入增益拒绝空音频或 NaN/Inf")
    factor = 10.0 ** (gain_db / 20.0)
    scaled = (audio.astype(np.float64) * factor).astype(np.float32)
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if scaled.shape[1] == 1:
        sf.write(dest, scaled[:, 0], sample_rate, subtype="FLOAT")
    else:
        sf.write(dest, scaled, sample_rate, subtype="FLOAT")
    return dest
