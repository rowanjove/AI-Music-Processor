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


def render_stage(kind, source_wav, dest_wav, report, policy, reference=None, reference_rate=None):
    """Run one polish stage. A non-finite buffer stops. Any other failure bypasses."""
    policy = clamp_policy(policy)
    try:
        audio, sample_rate = _read(source_wav)
        if report is None:
            report = analyze_polish_buffer(audio, sample_rate)
        if report.get("repair", {}).get("nan_inf") or not np.isfinite(audio).all():
            raise NonFiniteAudio("音频包含 NaN/Inf")
        decisions = route_polish(report, policy)
        if kind == "repair":
            processed, info = apply_repair(audio, sample_rate, report, policy, decisions)
        elif kind == "tone":
            processed, info = apply_tone(audio, sample_rate, report, policy, decisions, reference, reference_rate)
        elif kind == "dynamics":
            processed, info = apply_dynamics(audio, sample_rate, report, policy, decisions)
        elif kind == "stereo":
            processed, info = apply_stereo_stage(audio, sample_rate, report, policy, decisions)
        else:
            raise ValueError("未知精修阶段")
        if info.get("bypassed"):
            shutil.copyfile(source_wav, dest_wav)
        else:
            _write(dest_wav, processed, sample_rate)
        decision_path = _dump(_decision_path(dest_wav), {"stage": kind, "decisions": decisions, "info": info, "warning": info.get("warning")})
        return {"wav": dest_wav, "decision_path": decision_path, "warning": info.get("warning")}
    except NonFiniteAudio:
        raise
    except Exception as exc:
        shutil.copyfile(source_wav, dest_wav)
        warning = type(exc).__name__ + ": " + str(exc)
        decision_path = _dump(_decision_path(dest_wav), {"stage": kind, "bypassed": True, "warning": warning})
        return {"wav": dest_wav, "decision_path": decision_path, "warning": warning}


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
    summary = _dump(os.path.join(directory, "polish_decision.json"), {"decisions": decisions, "no_problem": decisions.get("no_problem")})
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
    }
