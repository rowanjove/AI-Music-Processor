"""Optional, explicitly selected vocal restoration effects."""
import hashlib
import json
import os
import uuid

import numpy as np
import soundfile as sf

from core.analyzer import probe_metadata
from core.qc import sample_metrics

SCHEMA_VERSION = 1
NOT_RUN = "NOT_RUN"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
WEIGHT_PRESENT = "WEIGHT_PRESENT_NOT_ENABLED"
WEIGHT_MISSING = "WEIGHT_MISSING"
MEASURED = "MEASURED"
FAILED = "FAILED"

_EFFECTS = (
    {"id": "de_echo_normal", "model_name": "VR-DeEchoNormal.pth", "kind": "de_echo"},
    {"id": "de_echo_aggressive", "model_name": "VR-DeEchoAggressive.pth", "kind": "de_echo"},
    {"id": "de_echo_dereverb", "model_name": "VR-DeEchoDeReverb.pth", "kind": "de_reverb"},
    {"id": "onnx_dereverb", "model_name": "onnx_dereverb_By_FoxJoy/vocals.onnx",
     "kind": "de_reverb", "backend": "mdxnet"},
    {"id": "denoise", "model_name": None, "kind": "denoise",
     "backend": "classical_spectral_gate", "algorithm": "conservative-nonvocal-gate-v1"},
    {"id": "bleed_suppression", "model_name": None, "kind": "bleed_suppression",
     "backend": "classical_spectral_gate", "algorithm": "conservative-nonvocal-gate-v1"},
)


def restoration_catalog(weight_dir=None):
    if weight_dir is None:
        weight_dir = _default_weight_dir()
    rows = [_row(spec, weight_dir) for spec in _EFFECTS]
    return {
        "schema_version": SCHEMA_VERSION,
        "applied_by_job": False,
        "default_enabled": [],
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "effects": rows,
    }


def freeze_effect(effect_id, weight_dir=None):
    """Freeze a neural weight or the explicit classical DSP contract."""
    if effect_id is None or effect_id == "off":
        return None
    catalog = restoration_catalog(weight_dir)
    row = next((item for item in catalog["effects"] if item["id"] == effect_id), None)
    if row is None:
        raise ValueError("未实现的 VocalRestore 效果：" + str(effect_id))
    backend = row.get("backend", "uvr")
    if backend == "classical_spectral_gate":
        return {
            "effect_id": effect_id,
            "model_name": None,
            "path": None,
            "weight_dir": None,
            "sha256": None,
            "backend": backend,
            "algorithm": row["algorithm"],
            "agg": 10,
        }
    if not row.get("model_name"):
        raise ValueError("未实现的 VocalRestore 效果：" + str(effect_id))
    if not row.get("weight_present"):
        raise FileNotFoundError("VocalRestore 权重不存在：" + str(row["model_name"]))
    root = os.path.abspath(weight_dir or _default_weight_dir())
    path = os.path.abspath(os.path.join(root, row["model_name"]))
    return {
        "effect_id": effect_id,
        "model_name": row["model_name"],
        "path": path,
        "weight_dir": root,
        "sha256": _sha256(path),
        "backend": backend,
        "agg": 10,
    }


def run_effect(source_wav, effect_id, output_dir, agg=10, weight_dir=None, expected_identity=None):
    """Run one selected UVR restoration weight and keep the source intact."""
    if not os.path.isfile(source_wav):
        raise ValueError("修复实验的源文件不存在")
    catalog = restoration_catalog(weight_dir)
    row = next((item for item in catalog["effects"] if item["id"] == effect_id), None)
    if row is None:
        return _stopped(effect_id, NOT_RUN, "这个效果没有可调用的权重")
    backend = row.get("backend", "uvr")
    identity_root = weight_dir or _default_weight_dir()
    if backend == "classical_spectral_gate":
        identity = freeze_effect(effect_id, weight_dir)
        weight_path = None
        weight_sha256 = None
    else:
        if not row["weight_present"]:
            return _stopped(effect_id, NOT_RUN, "权重文件不存在")
        identity = freeze_effect(effect_id, weight_dir)
        weight_path = identity["path"]
        weight_sha256 = identity["sha256"]
    if expected_identity is not None:
        if not isinstance(expected_identity, dict) or not _identity_matches(expected_identity, identity):
            return _stopped(effect_id, FAILED, "VocalRestore 权重与任务冻结身份不一致")
    folder = os.path.join(output_dir, effect_id)
    os.makedirs(folder, exist_ok=True)
    vocal = os.path.join(folder, "processed.wav")
    residual = os.path.join(folder, "residual.wav")
    execution_evidence = None
    try:
        if backend == "classical_spectral_gate":
            dsp_record = _run_classical_effect(source_wav, vocal, residual, effect_id)
        elif backend == "mdxnet":
            execution_evidence = _run_mdxnet_effect(
                source_wav, vocal, residual, weight_path, weight_sha256, output_dir)
            dsp_record = None
        else:
            from core.separator import separate_audio
            separate_audio(source_wav, vocal, residual, model_name=row["model_name"], agg=agg)
            _rewrite_float_output(source_wav, vocal)
            _rewrite_float_output(source_wav, residual)
            dsp_record = None
        if weight_path and _sha256(weight_path) != weight_sha256:
            raise RuntimeError("VocalRestore 推理期间权重发生变化")
        _validate_output_roles(source_wav, vocal, residual)
        before = _file_metrics(source_wav)
        after = _file_metrics(vocal)
        other = _file_metrics(residual)
    except Exception as exc:
        return _stopped(effect_id, FAILED, type(exc).__name__ + ": " + str(exc))
    report_path = vocal + ".restoration.json"
    result = {
        "effect_id": effect_id,
        "model_name": row["model_name"],
        "weight_path": weight_path,
        "weight_sha256": weight_sha256,
        "weight_dir": identity_root if weight_path else None,
        "backend": backend,
        "algorithm": row.get("algorithm"),
        "dsp_observation": dsp_record,
        "execution_evidence": execution_evidence,
        "output_roles": {"processed.wav": "restored vocal", "residual.wav": "removed component"},
        "source_path": os.path.abspath(source_wav),
        "source_sha256": _sha256(source_wav),
        "restored_path": vocal,
        "residual_path": residual,
        "agg": agg,
        "benchmark_status": MEASURED,
        "enabled_by_job": True,
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "before": before,
        "after": after,
        "residual": other,
        "vocal_peak_delta": after["sample_peak"] - before["sample_peak"],
        "error": None,
        "report_path": report_path,
    }
    worker_report = vocal + ".worker.json"
    if os.path.isfile(worker_report):
        result["worker_report_path"] = worker_report
    _write_json(report_path, result)
    return result


def _row(spec, weight_dir):
    path = None
    present = False
    backend = spec.get("backend", "uvr")
    if spec["model_name"]:
        path = os.path.join(weight_dir, spec["model_name"])
        present = os.path.isfile(path) and os.path.getsize(path) > 0
        status = WEIGHT_PRESENT if present else WEIGHT_MISSING
        available = present
    elif backend == "classical_spectral_gate":
        status = "CLASSICAL_IMPLEMENTED"
        available = True
    else:
        status = NOT_IMPLEMENTED
        available = False
    return {
        "id": spec["id"],
        "kind": spec["kind"],
        "model_name": spec["model_name"],
        "backend": backend,
        "algorithm": spec.get("algorithm"),
        "status": status,
        "benchmark_status": NOT_RUN,
        "weight_present": present,
        "available_by_job": available,
        "enabled_by_job": False,
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
    }


def _identity_matches(expected, current):
    if expected == current:
        return True
    # Existing schema_version=1 jobs froze UVR weights before `backend` and
    # `weight_dir` were added. Preserve those exact old identities.
    legacy_keys = {"effect_id", "model_name", "path", "sha256", "agg"}
    return (set(expected) == legacy_keys and
            all(expected.get(key) == current.get(key) for key in legacy_keys))


def _run_mdxnet_effect(source_wav, vocal, residual, weight_path, weight_sha256, output_dir):
    from core.config import BASE_DIR, RVC_PYTHON, GPU_LOCK
    from core.processes import run as process_run

    worker = os.path.join(BASE_DIR, "core", "restoration_worker.py")
    command = [RVC_PYTHON, worker, "--input", os.path.abspath(source_wav),
               "--vocal-out", os.path.abspath(vocal), "--residual-out", os.path.abspath(residual),
               "--weight", os.path.abspath(weight_path), "--sha256", weight_sha256,
               "--effect", "onnx_dereverb"]
    with GPU_LOCK:
        process_run(command, timeout=14400, check=True)
    sidecar = vocal + ".worker.json"
    if not os.path.isfile(sidecar):
        raise RuntimeError("MDXNet worker 缺少实际推理记录")
    with open(sidecar, "r", encoding="utf-8") as handle:
        evidence = json.load(handle)
    if (evidence.get("status") != "complete" or evidence.get("inference_ran") is not True
            or evidence.get("effect_id") != "onnx_dereverb"
            or evidence.get("weight_sha256_before") != weight_sha256
            or evidence.get("weight_sha256_after") != weight_sha256
            or evidence.get("output_roles") != ["restored vocal", "removed component"]
            or evidence.get("output_subtype") != "FLOAT"
            or evidence.get("inference_provider") not in ("mixed", "CUDAExecutionProvider", "CPUExecutionProvider")
            or evidence.get("inference_device") not in ("cuda", "cpu")
            or not evidence.get("inference_providers")
            or not set(evidence.get("inference_providers", [])).issubset(
                {"CUDAExecutionProvider", "CPUExecutionProvider"})
            or "CPUExecutionProvider" not in evidence.get("session_providers", [])):
        raise RuntimeError("MDXNet worker 未提供完成推理证据")
    return evidence


def _run_classical_effect(source_wav, vocal, residual, effect_id):
    """Conservative non-vocal spectral gate; uncertain observations bypass."""
    algorithm = "conservative-nonvocal-gate-v1"
    source, rate = sf.read(source_wav, dtype="float32", always_2d=True)
    if rate < 16000 or len(source) < rate // 2 or not np.isfinite(source).all():
        _write_float(vocal, source, rate)
        _write_float(residual, np.zeros_like(source), rate)
        return {"algorithm": algorithm, "status": "BYPASSED", "reason": "insufficient_valid_audio"}
    try:
        from scipy.signal import resample_poly, stft, istft
        import pyworld
        import math

        mono = np.mean(source, axis=1).astype(np.float64)
        divisor = math.gcd(rate, 16000)
        observed = np.ascontiguousarray(resample_poly(mono, 16000 // divisor, rate // divisor), dtype=np.float64)
        f0, times = pyworld.dio(observed, 16000, f0_floor=65, f0_ceil=1000, frame_period=10)
        f0 = pyworld.stonemask(observed, f0, times, 16000)
        frame_samples = 160
        count = min(len(times), max(0, len(observed) // frame_samples))
        if count < 50:
            raise ValueError("too_few_observation_frames")
        frame_energy = np.array([
            float(np.sqrt(np.mean(observed[index * frame_samples:min((index + 1) * frame_samples, len(observed))] ** 2)))
            for index in range(count)
        ])
        voiced = (f0[:count] >= 65) & (f0[:count] <= 1000) & np.isfinite(f0[:count])
        positive = frame_energy[frame_energy > 1e-5]
        if len(positive) < 10:
            raise ValueError("no_reliable_noise_floor")
        low_threshold = float(np.percentile(positive, 25))
        noise_frames = (~voiced) & (frame_energy <= low_threshold)
        noise_seconds = float(noise_frames.sum() * .01)
        if noise_seconds < .5:
            raise ValueError("insufficient_nonvocal_noise_observation")

        nperseg = min(2048, max(256, 2 ** int(np.floor(np.log2(rate * .05)))))
        hop = max(1, nperseg // 4)
        output = np.empty_like(source)
        changed_frames = 0
        maximum_attenuation = .28 if effect_id == "denoise" else .18
        for channel in range(source.shape[1]):
            _frequencies, stft_times, spectrum = stft(
                source[:, channel], fs=rate, nperseg=nperseg,
                noverlap=nperseg - hop, boundary="zeros", padded=True,
            )
            frame_voiced = np.interp(stft_times, times[:count], voiced.astype(float), left=0, right=0) > .5
            frame_low = np.interp(stft_times, times[:count], noise_frames.astype(float), left=0, right=0) > .5
            if not np.any(frame_low):
                raise ValueError("noise_observation_did_not_map_to_audio")
            magnitude = np.abs(spectrum)
            phase = np.exp(1j * np.angle(spectrum))
            floor = np.median(magnitude[:, frame_low], axis=1, keepdims=True)
            ratio = magnitude / np.maximum(floor, 1e-8)
            attenuation = np.clip((1.8 - ratio) / 1.8, 0, 1)
            eligible = ~frame_voiced
            gain = np.ones_like(magnitude)
            gain[:, eligible] = 1.0 - maximum_attenuation * attenuation[:, eligible]
            changed_frames += int(np.any(gain[:, eligible] < .999, axis=0).sum()) if np.any(eligible) else 0
            _, reconstructed = istft(
                spectrum * gain, fs=rate, nperseg=nperseg,
                noverlap=nperseg - hop, boundary=True,
            )
            output[:, channel] = _fit_frames(reconstructed, len(source))
        _write_float(vocal, output, rate)
        _write_float(residual, source - output, rate)
        return {
            "algorithm": algorithm,
            "status": "APPLIED",
            "source_role": "vocal stem",
            "voiced_detector": "pyworld.dio+stonemask",
            "noise_observation_seconds": noise_seconds,
            "noise_energy_threshold": low_threshold,
            "maximum_attenuation_db": 20 * np.log10(1 - maximum_attenuation),
            "changed_frame_count": changed_frames,
            "voiced_frames_preserved": True,
            "listening_quality": "not_assessed",
        }
    except Exception as exc:
        _write_float(vocal, source, rate)
        _write_float(residual, np.zeros_like(source), rate)
        return {
            "algorithm": algorithm,
            "status": "BYPASSED",
            "reason": type(exc).__name__ + ": " + str(exc),
            "listening_quality": "not_assessed",
        }


def _fit_frames(audio, frames):
    audio = np.asarray(audio, dtype=np.float32)
    if len(audio) >= frames:
        return audio[:frames]
    return np.pad(audio, (0, frames - len(audio)))


def _write_float(path, data, rate):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = os.fspath(path) + ".part." + uuid.uuid4().hex
    try:
        sf.write(temporary, np.asarray(data, dtype=np.float32), int(rate), format="WAV", subtype="FLOAT")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _rewrite_float_output(source_wav, output_path):
    reference, expected_rate = sf.read(source_wav, dtype="float32", always_2d=True)
    output, rate = sf.read(output_path, dtype="float32", always_2d=True)
    if rate != expected_rate:
        from scipy.signal import resample_poly
        import math
        divisor = math.gcd(int(rate), int(expected_rate))
        output = resample_poly(output, expected_rate // divisor, rate // divisor, axis=0)
    if output.shape[1] != reference.shape[1]:
        if output.shape[1] == 1 and reference.shape[1] == 2:
            output = np.repeat(output, 2, axis=1)
        elif output.shape[1] == 2 and reference.shape[1] == 1:
            output = output.mean(axis=1, keepdims=True)
        else:
            raise RuntimeError("VocalRestore 输出声道数无法与输入对齐")
    output = _fit_frames(output, len(reference))
    if not np.isfinite(output).all():
        raise RuntimeError("VocalRestore 输出包含 NaN/Inf")
    _write_float(output_path, output, expected_rate)


def _validate_output_roles(source_wav, vocal, residual):
    source, source_rate = sf.read(source_wav, dtype="float32", always_2d=True)
    for path, role in ((vocal, "restored vocal"), (residual, "removed component")):
        if not os.path.isfile(path) or os.path.getsize(path) <= 44:
            raise RuntimeError("VocalRestore 缺少 " + role + " 输出")
        info = sf.info(path)
        output, rate = sf.read(path, dtype="float32", always_2d=True)
        if (info.subtype != "FLOAT" or rate != source_rate or len(output) != len(source)
                or output.shape[1] != source.shape[1] or not np.isfinite(output).all()):
            raise RuntimeError("VocalRestore " + role + " 输出未满足 FLOAT/时长/声道契约")


def _file_metrics(path):
    metrics = sample_metrics(path)
    meta = probe_metadata(path)
    return {
        "sample_peak": metrics["sample_peak"],
        "clipped_samples": metrics["clipped_samples"],
        "channel_rms": list(metrics["channel_rms"]),
        "dc_offset": list(metrics["dc_offset"]),
        "duration_seconds": meta["duration"],
        "sample_rate": meta["sample_rate"],
        "channels": meta["channels"],
    }


def _stopped(effect_id, status, reason):
    return {
        "effect_id": effect_id,
        "benchmark_status": status,
        "enabled_by_job": False,
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "error": reason,
    }


def _default_weight_dir():
    from core.config import RVC_ROOT
    return os.path.join(RVC_ROOT, "uvr5_weights")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path, value):
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
