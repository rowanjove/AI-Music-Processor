"""File-facing polish stages. The pipeline owns cache keys; this module owns audio."""
import os
import shutil

import numpy as np
import soundfile as sf

from core.polish_analysis import analyze_polish_buffer
from core.polish_dsp import (
    NonFiniteAudio,
    apply_dynamics,
    apply_repair,
    apply_stereo_stage,
    apply_tone,
)
from core.polish_policy import clamp_policy, route_polish


def _read(path):
    audio, sample_rate = sf.read(path, always_2d=True, dtype="float32")
    return np.asarray(audio, dtype=np.float64), int(sample_rate)


def _write(path, audio, sample_rate):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sf.write(path, np.asarray(audio, dtype=np.float32), int(sample_rate), subtype="FLOAT")
    return path


def _decision_path(wav_path):
    return os.path.splitext(wav_path)[0] + ".decision.json"


def _plain(value):
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _dump(path, payload):
    import json
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(_plain(payload), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


STAGE_STATUS_PROCESSED = "PROCESSED"
STAGE_STATUS_BYPASSED_BY_POLICY = "BYPASSED_BY_POLICY"
STAGE_STATUS_BYPASSED_BY_ERROR = "BYPASSED_BY_ERROR"
STAGE_STATUS_FAILED = "FAILED"


def render_stage(kind, source_wav, dest_wav, report, policy, reference=None, reference_rate=None):
    """Run one polish stage.

    Returns structured status:
      - PROCESSED: stage ran DSP operations and wrote modified audio.
      - BYPASSED_BY_POLICY: stage was skipped by configuration or clean analysis.
      - BYPASSED_BY_ERROR: stage encountered an unexpected DSP exception; original
        audio was preserved, and a searchable warning was logged.
    I/O errors, invalid inputs, and NonFiniteAudio stop without bypass.
    """
    policy = clamp_policy(policy)
    audio, sample_rate = _read(source_wav)
    if report is None:
        report = analyze_polish_buffer(audio, sample_rate)
    if report.get("repair", {}).get("nan_inf") or not np.isfinite(audio).all():
        raise NonFiniteAudio("音频包含 NaN/Inf")
    if kind not in ("repair", "tone", "dynamics", "stereo"):
        raise ValueError("未知精修阶段: " + str(kind))

    decisions = route_polish(report, policy)
    try:
        if kind == "repair":
            processed, info = apply_repair(audio, sample_rate, report, policy, decisions)
        elif kind == "tone":
            processed, info = apply_tone(audio, sample_rate, report, policy, decisions, reference, reference_rate)
        elif kind == "dynamics":
            processed, info = apply_dynamics(audio, sample_rate, report, policy, decisions)
        elif kind == "stereo":
            processed, info = apply_stereo_stage(audio, sample_rate, report, policy, decisions)
    except NonFiniteAudio:
        raise
    except Exception as exc:
        shutil.copyfile(source_wav, dest_wav)
        warning = type(exc).__name__ + ": " + str(exc)
        decision_path = _dump(_decision_path(dest_wav), {
            "stage": kind,
            "status": STAGE_STATUS_BYPASSED_BY_ERROR,
            "stage_status": STAGE_STATUS_BYPASSED_BY_ERROR,
            "bypassed": True,
            "degraded": True,
            "warning": warning,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        return {
            "wav": dest_wav,
            "decision_path": decision_path,
            "status": STAGE_STATUS_BYPASSED_BY_ERROR,
            "stage_status": STAGE_STATUS_BYPASSED_BY_ERROR,
            "bypassed": True,
            "degraded": True,
            "warning": warning,
            "error": str(exc),
        }

    if not np.isfinite(processed).all():
        raise NonFiniteAudio(f"精修阶段 {kind} 计算产物包含 NaN/Inf")

    if info.get("bypassed"):
        shutil.copyfile(source_wav, dest_wav)
        status = STAGE_STATUS_BYPASSED_BY_POLICY
        bypassed = True
    else:
        _write(dest_wav, processed, sample_rate)
        status = STAGE_STATUS_PROCESSED
        bypassed = False

    warning = info.get("warning")
    decision_path = _dump(_decision_path(dest_wav), {
        "stage": kind,
        "status": status,
        "stage_status": status,
        "bypassed": bypassed,
        "degraded": False,
        "decisions": decisions,
        "info": info,
        "warning": warning,
    })
    return {
        "wav": dest_wav,
        "decision_path": decision_path,
        "status": status,
        "stage_status": status,
        "bypassed": bypassed,
        "degraded": False,
        "warning": warning,
    }


def render_polish_files(source_wav, directory, policy=None, reference_path=None):
    """Write source, repaired, tone, dynamics, and stereo. Master is separate."""
    from core.polish_policy import default_policy, load_frozen_reference, reference_identity
    os.makedirs(directory, exist_ok=True)
    source = os.path.join(directory, "source.wav")
    shutil.copyfile(source_wav, source)
    audio, sample_rate = _read(source)
    report = analyze_polish_buffer(audio, sample_rate)
    if policy is None:
        policy = default_policy("balanced")
    policy = clamp_policy(policy)
    reference = None
    reference_rate = None
    if reference_path:
        identity = reference_identity(reference_path)
        policy["reference"].update(identity)
        policy["reference"]["enabled"] = True
        reference, reference_rate = load_frozen_reference(identity)
    repaired = os.path.join(directory, "repaired.wav")
    tone = os.path.join(directory, "tone.wav")
    dynamics = os.path.join(directory, "dynamics.wav")
    stereo = os.path.join(directory, "stereo.wav")
    repair = render_stage("repair", source, repaired, report, policy)
    tone_result = render_stage("tone", repaired, tone, report, policy, reference, reference_rate)
    dynamics_result = render_stage("dynamics", tone, dynamics, report, policy)
    stereo_result = render_stage("stereo", dynamics, stereo, report, policy)
    decisions = route_polish(report, policy)

    stages_results = {
        "repair": repair,
        "tone": tone_result,
        "dynamics": dynamics_result,
        "stereo": stereo_result,
    }
    degraded_stages = [
        name for name, res in stages_results.items()
        if res.get("degraded") or res.get("status") == STAGE_STATUS_BYPASSED_BY_ERROR
    ]
    summary_data = {
        "decisions": decisions,
        "no_problem": decisions.get("no_problem"),
        "has_degradation": bool(degraded_stages),
        "degraded_stages": degraded_stages,
        "stage_statuses": {name: res.get("status") for name, res in stages_results.items()},
    }
    summary = _dump(os.path.join(directory, "polish_decision.json"), summary_data)
    return {
        "source": source,
        "repaired": repaired,
        "tone": tone,
        "dynamics": dynamics,
        "stereo": stereo,
        "summary": summary,
        "repair": repair,
        "tone_stage": tone_result,
        "dynamics_stage": dynamics_result,
        "stereo_stage": stereo_result,
        "decisions": decisions,
        "degraded_stages": degraded_stages,
        "has_degradation": bool(degraded_stages),
    }
