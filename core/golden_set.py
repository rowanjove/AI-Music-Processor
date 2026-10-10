"""Record which golden roles exist. This module does not copy or synthesize audio.

A classified Suno, studio, live, or vocal-only set is not created here.
Listening cannot be marked passed by the caller.
"""
import os

SCHEMA_VERSION = 1
SOURCE_KINDS = ("unclassified", "suno", "studio", "mp3", "live", "vocal_only")
GENDER_PAIRS = ("male_to_male", "male_to_female", "female_to_male", "female_to_female")
SINGING = ("pop", "high", "low", "breathy", "rap", "sustained", "dense_lyrics", "harmony", "reverb")
ROLES = (
    "source",
    "separated_vocal",
    "instrumental",
    "preprocessed_vocal",
    "rvc_input",
    "rvc_raw",
    "postprocess",
    "mix",
    "master",
)


def expected_axes():
    return {
        "schema_version": SCHEMA_VERSION,
        "source_kind": list(SOURCE_KINDS[1:]),
        "gender_pair": list(GENDER_PAIRS),
        "singing": list(SINGING),
        "roles": list(ROLES),
        "quality_acceptance": "NOT_ASSESSED",
        "listening_quality": "not_assessed",
    }


def register_case(case_id, roles, source_kind="unclassified", gender_pair=None, singing=None, listening_passed=False):
    """Register files that already exist. Missing roles stay NOT_PRESENT."""
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("Golden 样本缺少 id")
    if source_kind not in SOURCE_KINDS:
        raise ValueError("未知来源类型")
    if gender_pair is not None and gender_pair not in GENDER_PAIRS:
        raise ValueError("未知性别方向")
    if singing is not None and singing not in SINGING:
        raise ValueError("未知演唱类型")
    if listening_passed:
        raise ValueError("没有听审记录，不能标成通过")
    if not isinstance(roles, dict):
        raise ValueError("Golden 角色必须是字典")
    unknown = sorted(key for key in roles if key not in ROLES)
    if unknown:
        raise ValueError("未知 Golden 角色：" + "、".join(unknown))
    recorded = {}
    for role in ROLES:
        path = roles.get(role)
        if path is None:
            recorded[role] = {"status": "NOT_PRESENT"}
            continue
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise ValueError("Golden 角色文件不存在：" + role)
        recorded[role] = {"status": "PRESENT", "bytes": os.path.getsize(path)}
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "source_kind": source_kind,
        "gender_pair": gender_pair or "NOT_ASSESSED",
        "singing": singing or "NOT_ASSESSED",
        "roles": recorded,
        "after_created": False,
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
    }


def coverage(cases):
    """Count classified cases. An unclassified observation does not fill an axis."""
    if not isinstance(cases, (list, tuple)):
        raise ValueError("样本列表无效")
    filled = {axis: set() for axis in ("source_kind", "gender_pair", "singing")}
    classified = 0
    for case in cases:
        if not isinstance(case, dict) or case.get("quality_acceptance") != "NOT_ASSESSED":
            raise ValueError("样本不能自称为已验收")
        if case.get("source_kind") == "unclassified":
            continue
        classified += 1
        for axis, allowed in (("source_kind", SOURCE_KINDS), ("gender_pair", GENDER_PAIRS), ("singing", SINGING)):
            value = case.get(axis)
            if value in allowed:
                filled[axis].add(value)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "NOT_RUN" if classified == 0 else "PARTIAL_NOT_ACCEPTED",
        "classified_cases": classified,
        "filled": {key: sorted(value) for key, value in filled.items()},
        "listening_passed": 0,
        "quality_acceptance": "NOT_ASSESSED",
        "listening_quality": "not_assessed",
    }


SYNTHETIC_KINDS = (
    "silence",
    "clipping",
    "mono_tone",
    "sample_rates",
    "short_impulse",
    "phase_inverted",
    "discontinuous",
)


def generate_synthetic_audio(kind, output_path, duration_seconds=2.0, sample_rate=48000):
    """Generate deterministic synthetic audio fixtures without external song dependencies.

    Uses pure standard library math/wave/struct to prevent private asset leakage.
    """
    import math
    import struct
    import wave
    from pathlib import Path
    from core.storage import sha256_file

    if kind not in SYNTHETIC_KINDS:
        raise ValueError("未知合成类型: %s，支持类型: %s" % (kind, ", ".join(SYNTHETIC_KINDS)))
    duration = float(duration_seconds)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("合成时长必须为正有限数")
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    num_samples = int(duration * sample_rate)
    channels = 1 if kind == "mono_tone" else 2

    frames = bytearray()
    for i in range(num_samples):
        t = i / sample_rate
        if kind == "silence":
            left, right = 0.0, 0.0
        elif kind == "clipping":
            raw = 2.5 * math.sin(2.0 * math.pi * 440.0 * t)
            left = right = max(-1.0, min(1.0, raw))
        elif kind == "mono_tone":
            left = 0.5 * math.sin(2.0 * math.pi * 440.0 * t)
            right = left
        elif kind == "sample_rates":
            left = right = 0.4 * math.sin(2.0 * math.pi * 1000.0 * t)
        elif kind == "short_impulse":
            val = 0.9 if i < 10 else 0.0
            left = right = val
        elif kind == "phase_inverted":
            left = 0.5 * math.sin(2.0 * math.pi * 440.0 * t)
            right = -left
        elif kind == "discontinuous":
            if t < duration * 0.4:
                val = 0.5 * math.sin(2.0 * math.pi * 440.0 * t)
            elif t < duration * 0.6:
                val = 0.0
            else:
                val = 0.5 * math.sin(2.0 * math.pi * 880.0 * t)
            left = right = val
        else:
            left, right = 0.0, 0.0

        if channels == 1:
            sample_val = int(max(-1.0, min(1.0, left)) * 32767)
            frames.extend(struct.pack("<h", sample_val))
        else:
            l_val = int(max(-1.0, min(1.0, left)) * 32767)
            r_val = int(max(-1.0, min(1.0, right)) * 32767)
            frames.extend(struct.pack("<hh", l_val, r_val))

    with wave.open(str(output_path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(frames)

    return {
        "path": str(output_path),
        "kind": kind,
        "duration_seconds": duration,
        "sample_rate": sample_rate,
        "channels": channels,
        "sha256": sha256_file(str(output_path)),
    }


def create_golden_spec(case_id, fixture_kind, expected_metrics, params=None, execution_stages=None, input_path=None):
    """Define reproducible golden baseline specification bound to release gates."""
    from core.storage import sha256_file
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("Golden 样本缺少 id")
    if fixture_kind not in SYNTHETIC_KINDS:
        raise ValueError("未知合成类型: %s" % fixture_kind)
    if not isinstance(expected_metrics, dict):
        raise ValueError("expected_metrics 必须为字典")
    required_keys = ("peak", "clipped_samples", "duration")
    for key in required_keys:
        if key not in expected_metrics:
            raise ValueError("expected_metrics 缺少必要键: %s" % key)

    input_sha = sha256_file(str(input_path)) if input_path and os.path.isfile(input_path) else None

    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "fixture_kind": fixture_kind,
        "input_sha256": input_sha,
        "params": dict(params or {}),
        "expected_metrics": {k: float(expected_metrics[k]) for k in required_keys},
        "tolerances": {
            "peak": 0.01,
            "clipped_samples": 0.0,
            "duration": 0.03,
        },
        "execution_stages": list(execution_stages or []),
        "listening_quality": "not_assessed",
        "quality_acceptance": "READY_FOR_EXTERNAL_VALIDATION",
    }


def verify_golden_run(golden_spec, measured_metrics, stage_records=None):
    """Compare measured metrics against a pinned golden spec using bounded tolerances.

    Never claims auditory improvement without independent human listening records.
    """
    if not isinstance(golden_spec, dict) or not isinstance(measured_metrics, dict):
        raise ValueError("golden_spec 与 measured_metrics 必须为字典")

    expected = golden_spec.get("expected_metrics", {})
    tolerances = golden_spec.get("tolerances", {"peak": 0.01, "clipped_samples": 0.0, "duration": 0.03})

    deviations = {}
    passed = True
    for key in ("peak", "clipped_samples", "duration"):
        if key not in measured_metrics:
            return {
                "status": "INVALID_METRICS",
                "passed": False,
                "missing_metrics": [key],
                "listening_quality": "not_assessed",
                "quality_acceptance": "READY_FOR_EXTERNAL_VALIDATION",
            }
        try:
            exp_val = float(expected[key])
            meas_val = float(measured_metrics[key])
        except (ValueError, TypeError):
            return {
                "status": "INVALID_METRICS",
                "passed": False,
                "invalid_metric": key,
                "listening_quality": "not_assessed",
                "quality_acceptance": "READY_FOR_EXTERNAL_VALIDATION",
            }

        diff = abs(meas_val - exp_val)
        tol = float(tolerances.get(key, 0.0))
        is_ok = diff <= tol
        deviations[key] = {
            "expected": exp_val,
            "measured": meas_val,
            "deviation": diff,
            "tolerance": tol,
            "passed": is_ok,
        }
        if not is_ok:
            passed = False

    stage_intact = True
    expected_stages = golden_spec.get("execution_stages", [])
    if stage_records is not None and expected_stages:
        actual_stages = [s.get("stage_name") for s in stage_records if isinstance(s, dict)]
        if actual_stages != expected_stages:
            stage_intact = False

    return {
        "case_id": golden_spec.get("case_id"),
        "fixture_kind": golden_spec.get("fixture_kind"),
        "status": "PASS" if (passed and stage_intact) else "FAIL",
        "passed": passed and stage_intact,
        "metrics_passed": passed,
        "stage_integrity": "INTACT" if stage_intact else "DEGRADED",
        "deviations": deviations,
        "listening_quality": "not_assessed",
        "quality_acceptance": "READY_FOR_EXTERNAL_VALIDATION",
    }
