"""Polish profiles, the auto router, and the stage parameter split.

Repair parameters do not include EQ or the reference identity. Tone parameters do.
Caps here are the hard limits the DSP is allowed to apply.
"""
import os
import tempfile

AUTO_EQ_MAX_DB = 3.0
REFERENCE_MATCH_MAX_DB = 3.0
DYNAMIC_EQ_MAX_DB = 3.0
TRANSIENT_MAX_RATIO = 0.15
MULTIBAND_MAX_GR_DB = 3.0
STEREO_MAX_WIDTH_DELTA = 0.15
DENOISE_MAX_REDUCTION_DB = 3.0
DENOISE_MAX_HF_LOSS_DB = 1.0

PROFILES = ("gentle", "balanced", "strong")
PROFILE_LABELS = {"轻柔": "gentle", "推荐": "balanced", "加强": "strong"}
STRENGTH_LABELS = {"gentle": "轻柔", "balanced": "推荐", "strong": "加强"}

# Higher threshold means the stage stays off unless the measurement is more severe.
_THRESHOLDS = {
    "gentle": {"declick": 0.8, "declip": 0.8, "denoise": 0.8, "auto_eq": 0.8, "dynamic_eq": 0.85, "multiband": 0.85, "transient": 0.85, "stereo": 0.8},
    "balanced": {"declick": 0.4, "declip": 0.4, "denoise": 0.4, "auto_eq": 0.4, "dynamic_eq": 0.5, "multiband": 0.5, "transient": 0.5, "stereo": 0.4},
    "strong": {"declick": 0.2, "declip": 0.2, "denoise": 0.2, "auto_eq": 0.2, "dynamic_eq": 0.2, "multiband": 0.2, "transient": 0.2, "stereo": 0.2},
}
_STRENGTH = {"gentle": 0.5, "balanced": 0.75, "strong": 1.0}

BASIC_CONTROLS = ("精修强度", "参考曲", "开始")
ADVANCED_CONTROLS = (
    "DeClick", "DeClip", "Denoise", "Auto EQ", "Dynamic EQ", "Reference Match",
    "Multiband", "Transient", "Stereo", "Target LUFS", "True Peak",
)
EXPERT_CONTROLS = (
    "threshold", "ratio", "attack", "release", "Q", "frequency", "max correction",
    "band crossover", "correlation guard", "noise threshold", "debug trace",
)

_PROCESSORS = ("declick", "declip", "denoise", "auto_eq", "dynamic_eq", "multiband", "transient", "stereo")


def _clamp(value, low, high):
    number = float(value)
    if number < low:
        return low
    if number > high:
        return high
    return number


def default_policy(profile="balanced"):
    if profile not in PROFILES:
        raise ValueError("未知精修强度")
    return {
        "profile": profile,
        "analysis_policy": {"version": 1},
        "repair": {"enabled": True, "declick": "auto", "declip": "auto"},
        "denoise": {"enabled": True, "mode": "auto", "max_reduction_db": DENOISE_MAX_REDUCTION_DB, "max_hf_loss_db": DENOISE_MAX_HF_LOSS_DB},
        "eq": {"enabled": True, "max_db": AUTO_EQ_MAX_DB},
        "dynamic_eq": {"enabled": True, "max_db": DYNAMIC_EQ_MAX_DB},
        "reference": {"enabled": True, "path": None, "sha256": None, "duration": None, "analysis_fingerprint": None, "strength": 1.0, "max_db": REFERENCE_MATCH_MAX_DB},
        "multiband": {"enabled": True, "max_gr_db": MULTIBAND_MAX_GR_DB},
        "transient": {"enabled": True, "max_ratio": TRANSIENT_MAX_RATIO},
        "stereo": {"enabled": True, "max_width_delta": STEREO_MAX_WIDTH_DELTA, "correlation_guard": 0.0},
        "master": {"preset": "natural"},
        "expert": {},
    }


def _merge(base, override):
    if not isinstance(override, dict):
        raise ValueError("精修配置必须是对象")
    merged = {}
    for key, value in base.items():
        if key not in override:
            merged[key] = value
            continue
        incoming = override[key]
        if isinstance(value, dict) and isinstance(incoming, dict):
            nested = dict(value)
            nested.update(incoming)
            merged[key] = nested
        else:
            merged[key] = incoming
    for key, value in override.items():
        if key not in merged:
            merged[key] = value
    return merged


def clamp_policy(policy):
    """Force every automatic amount back inside the hard caps."""
    clamped = _merge(default_policy(policy.get("profile", "balanced") if policy.get("profile") in PROFILES else "balanced"), policy)
    if clamped.get("profile") not in PROFILES:
        raise ValueError("未知精修强度")
    clamped["eq"]["max_db"] = _clamp(clamped["eq"].get("max_db", AUTO_EQ_MAX_DB), 0.0, AUTO_EQ_MAX_DB)
    clamped["dynamic_eq"]["max_db"] = _clamp(clamped["dynamic_eq"].get("max_db", DYNAMIC_EQ_MAX_DB), 0.0, DYNAMIC_EQ_MAX_DB)
    reference = clamped["reference"]
    reference["max_db"] = _clamp(reference.get("max_db", REFERENCE_MATCH_MAX_DB), 0.0, REFERENCE_MATCH_MAX_DB)
    reference["strength"] = _clamp(reference.get("strength", 1.0), 0.0, 1.0)
    clamped["multiband"]["max_gr_db"] = _clamp(clamped["multiband"].get("max_gr_db", MULTIBAND_MAX_GR_DB), 0.0, MULTIBAND_MAX_GR_DB)
    clamped["transient"]["max_ratio"] = _clamp(clamped["transient"].get("max_ratio", TRANSIENT_MAX_RATIO), 0.0, TRANSIENT_MAX_RATIO)
    clamped["stereo"]["max_width_delta"] = _clamp(clamped["stereo"].get("max_width_delta", STEREO_MAX_WIDTH_DELTA), 0.0, STEREO_MAX_WIDTH_DELTA)
    clamped["denoise"]["max_reduction_db"] = _clamp(clamped["denoise"].get("max_reduction_db", DENOISE_MAX_REDUCTION_DB), 0.0, DENOISE_MAX_REDUCTION_DB)
    clamped["denoise"]["max_hf_loss_db"] = _clamp(clamped["denoise"].get("max_hf_loss_db", DENOISE_MAX_HF_LOSS_DB), 0.0, DENOISE_MAX_HF_LOSS_DB)
    for section in ("repair", "denoise", "eq", "dynamic_eq", "reference", "multiband", "transient", "stereo"):
        if type(clamped[section].get("enabled", True)) is not bool:
            raise ValueError("精修开关必须是布尔值：" + section)
    return _apply_expert(clamped)


def _expert_float(expert, key, low, high):
    if key not in expert or expert[key] is None or str(expert[key]).strip() == "":
        return None
    try:
        value = float(expert[key])
    except (TypeError, ValueError):
        raise ValueError("精修专家参数无效：" + key)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("精修专家参数无效：" + key)
    return _clamp(value, low, high)


def _apply_expert(policy):
    """Map the Expert fields onto the caps and knobs the DSP already reads.

    Unknown keys stay in the object so a newer snapshot still loads. A known
    key that is not a finite number is rejected. Nothing here can exceed the
    hard caps applied above.
    """
    expert = policy.get("expert") or {}
    if not isinstance(expert, dict):
        raise ValueError("精修专家参数必须是对象")
    correction = _expert_float(expert, "max correction", 0.0, AUTO_EQ_MAX_DB)
    if correction is not None:
        policy["eq"]["max_db"] = min(float(policy["eq"]["max_db"]), correction)
        policy["dynamic_eq"]["max_db"] = min(float(policy["dynamic_eq"]["max_db"]), correction)
        policy["reference"]["max_db"] = min(float(policy["reference"]["max_db"]), correction)
        policy["multiband"]["max_gr_db"] = min(float(policy["multiband"]["max_gr_db"]), correction)
    guard = _expert_float(expert, "correlation guard", -1.0, 1.0)
    if guard is not None:
        policy["stereo"]["correlation_guard"] = guard
    noise = _expert_float(expert, "noise threshold", -80.0, -20.0)
    if noise is not None:
        policy["denoise"]["noise_floor_threshold"] = noise
    q_value = _expert_float(expert, "Q", 0.2, 4.0)
    if q_value is not None:
        policy["dynamic_eq"]["q"] = q_value
    frequency = _expert_float(expert, "frequency", 20.0, 18000.0)
    if frequency is not None:
        policy["dynamic_eq"]["frequency"] = frequency
    threshold = _expert_float(expert, "threshold", 50.0, 99.0)
    if threshold is not None:
        policy["dynamic_eq"]["threshold_percentile"] = threshold
        policy["multiband"]["threshold_percentile"] = threshold
    ratio = _expert_float(expert, "ratio", 1.0, 4.0)
    if ratio is not None:
        policy["multiband"]["ratio"] = ratio
    attack = _expert_float(expert, "attack", 1.0, 200.0)
    if attack is not None:
        policy["dynamic_eq"]["attack_ms"] = attack
        policy["multiband"]["attack_ms"] = attack
    release = _expert_float(expert, "release", 1.0, 200.0)
    if release is not None:
        policy["dynamic_eq"]["release_ms"] = release
        policy["multiband"]["release_ms"] = release
    crossover = _expert_float(expert, "band crossover", 40.0, 400.0)
    if crossover is not None:
        policy["multiband"]["crossover"] = crossover
    debug = expert.get("debug trace")
    if debug is not None and str(debug).strip() != "":
        token = str(debug).strip().lower()
        if token not in ("0", "1", "true", "false", "on", "off", "yes", "no"):
            raise ValueError("精修专家参数无效：debug trace")
        policy["expert"]["debug trace"] = "on" if token in ("1", "true", "on", "yes") else "off"
    return policy


def resolve_polish_policy(spec):
    """Old snapshots have no polish field. They still resolve to the balanced profile."""
    if not isinstance(spec, dict):
        raise ValueError("任务参数无效")
    stored = spec.get("polish")
    profile = spec.get("polish_profile") or (stored or {}).get("profile") or "balanced"
    if profile in PROFILE_LABELS:
        profile = PROFILE_LABELS[profile]
    policy = default_policy(profile if profile in PROFILES else "balanced")
    if isinstance(stored, dict):
        policy = _merge(policy, stored)
    options = spec.get("polish_options")
    if isinstance(options, dict):
        policy = _merge(policy, options)
    reference = spec.get("polish_reference") or (policy.get("reference") or {}).get("path")
    policy = clamp_policy(policy)
    if reference:
        policy["reference"]["path"] = os.path.abspath(reference)
    else:
        policy["reference"]["path"] = None
        policy["reference"]["enabled"] = False
    return policy


def reference_identity(path):
    """Freeze the source bytes and the decoded 48 kHz reference analysis."""
    if not path:
        return None
    from core.storage import sha256_file
    from core.polish_analysis import spectrum_fingerprint
    absolute = os.path.abspath(path)
    if not os.path.isfile(absolute) or os.path.islink(absolute):
        raise ValueError("参考曲不存在")
    source_sha256 = sha256_file(absolute)
    audio, sample_rate = decode_reference(absolute, expected_sha256=source_sha256)
    if sha256_file(absolute) != source_sha256:
        raise ValueError("参考曲在冻结期间发生变化")
    return {
        "path": absolute,
        "sha256": source_sha256,
        "duration": float(len(audio) / sample_rate),
        "analysis_fingerprint": spectrum_fingerprint(audio, sample_rate),
    }


def decode_reference(path, expected_sha256=None):
    """Decode an audio reference through the bundled FFmpeg into FLOAT WAV.

    `soundfile` cannot read every format accepted by the application, notably
    some M4A variants. The source hash brackets decoding so a changing input
    cannot be frozen under a mixed identity.
    """
    import numpy as np
    import soundfile as sf
    from core.analyzer import standardize_to_48k_float
    from core.storage import sha256_file

    absolute = os.path.abspath(os.fspath(path))
    if not os.path.isfile(absolute) or os.path.islink(absolute):
        raise ValueError("参考曲不存在或不是普通文件")
    before = sha256_file(absolute)
    if expected_sha256 is not None and before != expected_sha256:
        raise ValueError("参考曲与冻结 SHA-256 不一致")
    with tempfile.TemporaryDirectory(prefix="polish-reference-") as folder:
        decoded_path = os.path.join(folder, "reference.wav")
        standardize_to_48k_float(absolute, decoded_path)
        audio, sample_rate = sf.read(decoded_path, always_2d=True, dtype="float32")
    after = sha256_file(absolute)
    if after != before or (expected_sha256 is not None and after != expected_sha256):
        raise ValueError("参考曲在解码期间发生变化")
    if sample_rate != 48000 or audio.ndim != 2 or audio.shape[1] != 2 or not audio.shape[0]:
        raise ValueError("参考曲解码结果必须是非空 48 kHz 双声道音频")
    if not np.isfinite(audio).all():
        raise ValueError("参考曲解码结果包含 NaN/Inf")
    return audio, int(sample_rate)


def load_frozen_reference(identity):
    """Decode a frozen reference and verify both its raw and analysis identity."""
    from core.polish_analysis import spectrum_fingerprint

    if not isinstance(identity, dict):
        raise ValueError("精修参考曲缺少冻结身份")
    path = identity.get("path")
    expected_sha = identity.get("sha256")
    expected_fingerprint = identity.get("analysis_fingerprint")
    if not isinstance(path, str) or not path or not isinstance(expected_sha, str) or len(expected_sha) != 64:
        raise ValueError("精修参考曲冻结身份无效")
    audio, sample_rate = decode_reference(path, expected_sha256=expected_sha)
    fingerprint = spectrum_fingerprint(audio, sample_rate)
    if not isinstance(expected_fingerprint, str) or fingerprint != expected_fingerprint:
        raise ValueError("参考曲解码频谱与冻结身份不一致")
    expected_duration = identity.get("duration")
    if (type(expected_duration) not in (int, float) or
            abs(float(expected_duration) - len(audio) / sample_rate) > 1.0 / sample_rate):
        raise ValueError("参考曲解码时长与冻结身份不一致")
    return audio, sample_rate


def freeze_polish_fields(spec):
    """Fold loose UI fields into one optional polish object. Schema version stays 1."""
    if "polish" not in spec and "polish_profile" not in spec and "polish_reference" not in spec and "polish_options" not in spec:
        spec["polish"] = default_policy("balanced")
    policy = resolve_polish_policy(spec)
    reference = policy.get("reference", {}).get("path")
    if reference:
        identity = reference_identity(reference)
        enabled = policy["reference"].get("enabled")
        policy["reference"].update(identity)
        policy["reference"]["enabled"] = bool(enabled)
    spec["polish"] = policy
    spec.pop("polish_profile", None)
    spec.pop("polish_reference", None)
    spec.pop("polish_options", None)
    return spec


def route_polish(report, policy):
    """Turn one analysis into per-processor on/off choices. No problem stays off."""
    policy = clamp_policy(policy)
    profile = policy["profile"]
    thresholds = _THRESHOLDS[profile]
    strength = _STRENGTH[profile]
    needs = report.get("needs") or {}
    severity = report.get("severity") or {}
    reasons = report.get("reasons") or {}
    decisions = {}
    switches = {
        "declick": policy["repair"].get("enabled", True) and policy["repair"].get("declick") != "off",
        "declip": policy["repair"].get("enabled", True) and policy["repair"].get("declip") != "off",
        "denoise": policy["denoise"].get("enabled", True) and policy["denoise"].get("mode") != "off",
        "auto_eq": policy["eq"].get("enabled", True),
        "dynamic_eq": policy["dynamic_eq"].get("enabled", True),
        "multiband": policy["multiband"].get("enabled", True),
        "transient": policy["transient"].get("enabled", True),
        "stereo": policy["stereo"].get("enabled", True),
    }
    denoise_threshold = (policy.get("denoise") or {}).get("noise_floor_threshold")
    noise_floor = float((report.get("noise") or {}).get("noise_floor", -120.0))
    for name in _PROCESSORS:
        allowed = bool(switches[name])
        wanted = bool(needs.get(name)) and float(severity.get(name, 0.0)) >= thresholds[name]
        blocked_by_noise = name == "denoise" and wanted and denoise_threshold is not None and noise_floor < float(denoise_threshold)
        if blocked_by_noise:
            wanted = False
        enabled = allowed and wanted
        if enabled:
            reason = reasons.get(name, "没有分析依据")
        elif blocked_by_noise:
            reason = "噪声低于门限"
        elif not wanted:
            reason = "no problem"
        else:
            reason = "已关闭"
        decisions[name] = {"enabled": enabled, "strength": strength if enabled else 0.0, "reason": reason}
    dc_problem = bool((report.get("repair") or {}).get("dc_problem"))
    decisions["dc"] = {
        "enabled": dc_problem,
        "strength": 1.0 if dc_problem else 0.0,
        "reason": "去除直流" if dc_problem else "没有直流",
    }
    reference = policy["reference"]
    reference_enabled = bool(reference.get("enabled") and reference.get("path") and reference.get("sha256"))
    if reference_enabled:
        reference_reason = "已提供参考曲"
    elif reference.get("read_error"):
        reference_reason = str(reference.get("read_error"))
    elif reference.get("enabled") is False and (reference.get("path") or reference.get("sha256")):
        reference_reason = "已关闭"
    else:
        reference_reason = "没有参考曲"
    decisions["reference"] = {
        "enabled": reference_enabled,
        "strength": strength * float(reference.get("strength", 1.0)) if reference_enabled else 0.0,
        "reason": reference_reason,
    }
    decisions["profile"] = profile
    decisions["no_problem"] = not any(item["enabled"] for key, item in decisions.items() if key not in ("profile", "no_problem") and isinstance(item, dict))
    return decisions


def repair_parameters(policy):
    """Cache identity for Repair. EQ and the reference are intentionally absent."""
    policy = clamp_policy(policy)
    return {
        "mode": "polish",
        "profile": policy["profile"],
        "analysis_policy": dict(policy["analysis_policy"]),
        "repair": dict(policy["repair"]),
        "denoise": dict(policy["denoise"]),
    }


def tone_parameters(policy, identity=None):
    """Cache identity for Tone. Identity supplies the file fingerprint only.

    A caller that turns reference.enabled off must stay off even if the
    fingerprint dict still carries the old enabled flag.
    """
    policy = clamp_policy(policy)
    reference = dict(policy["reference"])
    if identity:
        for key in ("path", "sha256", "duration", "analysis_fingerprint"):
            if identity.get(key) is not None:
                reference[key] = identity[key]
    reference_enabled = bool(reference.get("enabled") and reference.get("sha256"))
    return {
        "mode": "polish",
        "profile": policy["profile"],
        "eq": dict(policy["eq"]),
        "dynamic_eq": dict(policy["dynamic_eq"]),
        "reference": {
            "enabled": reference_enabled,
            "sha256": reference.get("sha256") if reference_enabled else None,
            "duration": reference.get("duration") if reference_enabled else None,
            "analysis_fingerprint": reference.get("analysis_fingerprint") if reference_enabled else None,
            "strength": reference.get("strength", 1.0),
            "max_db": reference.get("max_db", REFERENCE_MATCH_MAX_DB),
            "path": reference.get("path") if reference_enabled else None,
        },
    }


def dynamics_parameters(policy):
    policy = clamp_policy(policy)
    return {
        "mode": "polish",
        "profile": policy["profile"],
        "multiband": dict(policy["multiband"]),
        "transient": dict(policy["transient"]),
    }


def stereo_parameters(policy):
    policy = clamp_policy(policy)
    return {"mode": "polish", "profile": policy["profile"], "stereo": dict(policy["stereo"])}


def master_stage_parameters(preset):
    """Loudness and true peak only. The new processors are not mastered again."""
    return {
        "preset": preset.get("name") if isinstance(preset, dict) else preset,
        "target_lufs": preset.get("target_lufs") if isinstance(preset, dict) else None,
        "true_peak": preset.get("true_peak") if isinstance(preset, dict) else None,
    }


def polish_visible_controls(tier):
    """Controls a tier may show. Basic is only strength, an optional reference, and start."""
    if tier not in ("basic", "advanced", "expert", "基础", "高级", "专家"):
        raise ValueError("未知精修界面层级")
    normalized = {"基础": "basic", "高级": "advanced", "专家": "expert"}.get(tier, tier)
    if normalized == "basic":
        return set(BASIC_CONTROLS)
    if normalized == "advanced":
        return set(BASIC_CONTROLS + ADVANCED_CONTROLS)
    return set(BASIC_CONTROLS + ADVANCED_CONTROLS + EXPERT_CONTROLS)
