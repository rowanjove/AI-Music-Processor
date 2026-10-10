"""Isolated MDXNet FoxJoy dereverb worker with frozen weight checks."""
import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fit(samples, frames):
    if len(samples) >= frames:
        return samples[:frames]
    return np.pad(samples, ((0, frames - len(samples)), (0, 0)))


def _float_wav(path, samples, rate):
    temporary = path + ".part." + uuid.uuid4().hex
    try:
        sf.write(temporary, samples, rate, format="WAV", subtype="FLOAT")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def run(input_path, vocal_out, residual_out, weight_path, expected_sha256, effect):
    if effect != "onnx_dereverb":
        raise ValueError("Unsupported restoration effect")
    source_path = os.path.abspath(input_path)
    weight_path = os.path.abspath(weight_path)
    rvc_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "tools", "RVC", "RVC-beta-v2-0618"))
    expected_path = os.path.abspath(os.path.join(rvc_root, "uvr5_weights", "onnx_dereverb_By_FoxJoy", "vocals.onnx"))
    if os.path.normcase(weight_path) != os.path.normcase(expected_path):
        raise ValueError("Frozen FoxJoy weight must resolve to the bundled model path")
    if not os.path.isfile(source_path) or not os.path.isfile(weight_path):
        raise FileNotFoundError("Restoration input or frozen FoxJoy weight is missing")
    initial_sha = _sha256(weight_path)
    if initial_sha != expected_sha256:
        raise RuntimeError("FoxJoy weight does not match the frozen SHA-256")

    source, source_rate = sf.read(source_path, dtype="float32", always_2d=True)
    if not len(source) or not np.isfinite(source).all():
        raise ValueError("Restoration input must contain finite audio")
    if source.shape[1] not in (1, 2):
        raise ValueError("FoxJoy restoration supports mono or stereo input")

    if rvc_root not in sys.path:
        sys.path.insert(0, rvc_root)
    os.chdir(rvc_root)
    import onnxruntime as ort
    available_providers = list(ort.get_available_providers())
    original_session = ort.InferenceSession
    provider_plan = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                     if "CUDAExecutionProvider" in available_providers
                     else ["CPUExecutionProvider"])
    fallback_reason = None
    sessions = []

    def selected_session(*args, **kwargs):
        options = ort.SessionOptions()
        options.enable_profiling = True
        kwargs["sess_options"] = options
        kwargs["providers"] = list(provider_plan)
        session = original_session(*args, **kwargs)
        sessions.append(session)
        return session

    ort.InferenceSession = selected_session
    import MDXNet
    original_write = MDXNet.sf.write

    def write_float(path, data, rate, *args, **kwargs):
        if str(path).lower().endswith(".wav"):
            kwargs.setdefault("subtype", "FLOAT")
        return original_write(path, data, rate, *args, **kwargs)

    MDXNet.sf.write = write_float
    work_dir = tempfile.mkdtemp(prefix="foxjoy-")
    try:
        vocal_dir = os.path.join(work_dir, "vocal")
        residual_dir = os.path.join(work_dir, "residual")
        def run_pass():
            sessions.clear()
            model = MDXNet.MDXNetDereverb(15)
            providers = list(model.pred.model.get_providers())
            model._path_audio_(source_path, vocal_dir, residual_dir, "wav")
            trace_path = model.pred.model.end_profiling()
            event_providers = _profile_providers(trace_path)
            try:
                os.remove(trace_path)
            except OSError:
                pass
            return model, providers, event_providers

        try:
            model, providers, event_providers = run_pass()
        except Exception as error:
            message = str(error).upper()
            cuda_failure = any(token in message for token in ("CUDA", "CUDNN", "OUT OF MEMORY", "CUDAERROR"))
            if "CUDAExecutionProvider" not in provider_plan or not cuda_failure:
                raise
            fallback_reason = type(error).__name__ + ": " + str(error)[-1000:]
            provider_plan = ["CPUExecutionProvider"]
            for path in (vocal_dir, residual_dir):
                import shutil
                shutil.rmtree(path, ignore_errors=True)
            os.makedirs(vocal_dir, exist_ok=True)
            os.makedirs(residual_dir, exist_ok=True)
            model, providers, event_providers = run_pass()
        if len(event_providers) == 1:
            inference_provider = event_providers[0]
        elif event_providers:
            inference_provider = "mixed"
        else:
            inference_provider = "unknown"
        if ("CUDAExecutionProvider" in provider_plan and
                "CUDAExecutionProvider" not in event_providers and not fallback_reason):
            fallback_reason = "CUDA provider was requested but the completed ORT profile contains no CUDA execution events"
        base = os.path.basename(source_path)
        candidates = (
            os.path.join(vocal_dir, base + "_main_vocal.wav"),
            os.path.join(residual_dir, base + "_others.wav"),
        )
        if any(not os.path.isfile(path) for path in candidates):
            raise RuntimeError("MDXNet did not produce both labeled output roles")
        from scipy.signal import resample_poly
        outputs = []
        for path in candidates:
            samples, rate = sf.read(path, dtype="float32", always_2d=True)
            if rate != source_rate:
                divisor = math.gcd(int(rate), int(source_rate))
                samples = resample_poly(samples, source_rate // divisor, rate // divisor, axis=0)
            if samples.shape[1] != source.shape[1]:
                if samples.shape[1] == 2 and source.shape[1] == 1:
                    samples = samples.mean(axis=1, keepdims=True)
                elif samples.shape[1] == 1 and source.shape[1] == 2:
                    samples = np.repeat(samples, 2, axis=1)
                else:
                    raise RuntimeError("MDXNet output channel count disagrees with the source")
            samples = _fit(np.asarray(samples, dtype=np.float32), len(source))
            if not np.isfinite(samples).all():
                raise RuntimeError("MDXNet output contains nonfinite samples")
            outputs.append(samples)
        _float_wav(os.path.abspath(vocal_out), outputs[0], source_rate)
        _float_wav(os.path.abspath(residual_out), outputs[1], source_rate)
        final_sha = _sha256(weight_path)
        if final_sha != initial_sha:
            raise RuntimeError("FoxJoy weight changed during inference")
        metadata = {
            "schema_version": 1,
            "worker": "mdxnet_foxjoy_dereverb",
            "status": "complete",
            "effect_id": effect,
            "weight_path": weight_path,
            "weight_sha256_before": initial_sha,
            "weight_sha256_after": final_sha,
            "available_providers": available_providers,
            "session_providers": providers,
            "all_session_providers": [list(item.get_providers()) for item in sessions],
            "requested_provider": "auto",
            "provider_plan": provider_plan,
            "inference_providers": event_providers,
            "inference_provider": inference_provider,
            "inference_device": ("cuda" if "CUDAExecutionProvider" in event_providers
                                 else "cpu" if "CPUExecutionProvider" in event_providers else "unknown"),
            "cpu_fallback_reason": fallback_reason,
            "inference_ran": True,
            "torch_stft_device": str(model.pred.model_.window.device),
            "output_roles": ["restored vocal", "removed component"],
            "source_frames": len(source),
            "sample_rate": source_rate,
            "channels": source.shape[1],
            "output_subtype": "FLOAT",
        }
        sidecar = os.path.abspath(vocal_out) + ".worker.json"
        temporary = sidecar + ".part." + uuid.uuid4().hex
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(metadata, handle, ensure_ascii=False, sort_keys=True, indent=2)
            os.replace(temporary, sidecar)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
        return metadata
    finally:
        ort.InferenceSession = original_session
        MDXNet.sf.write = original_write
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)


def _profile_providers(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            events = json.load(handle)
    except (OSError, ValueError, TypeError):
        return []
    found = set()
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            args = event.get("args")
            provider = args.get("provider") if isinstance(args, dict) else None
            if provider in ("CUDAExecutionProvider", "CPUExecutionProvider"):
                found.add(provider)
    order = ("CUDAExecutionProvider", "CPUExecutionProvider")
    return [provider for provider in order if provider in found]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--vocal-out", required=True)
    parser.add_argument("--residual-out", required=True)
    parser.add_argument("--weight", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--effect", required=True)
    args = parser.parse_args(argv)
    result = run(args.input, args.vocal_out, args.residual_out, args.weight, args.sha256, args.effect)
    print(json.dumps({"worker_progress": {"fraction": 1.0, "message": "VocalRestore FoxJoy 推理完成"}}, ensure_ascii=False), flush=True)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
