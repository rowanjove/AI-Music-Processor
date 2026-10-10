import json
import os
from core.config import RVC_PYTHON, GPU_LOCK, BASE_DIR
from core.worker_runtime import run_worker_with_recovery

VOICE_BACKEND = "hp2"
_VOICE_BACKENDS = {
    "hp2": ("HP2_all_vocals.pth", 10),
}


def resolve_voice_backend(backend=None):
    """Return the voice separator call. None selects HP2. Other ids are rejected."""
    if backend is None:
        backend = VOICE_BACKEND
    if not isinstance(backend, str) or backend not in _VOICE_BACKENDS:
        raise ValueError("未知的换声分离后端：" + str(backend))
    model_name, agg = _VOICE_BACKENDS[backend]
    return {"backend": backend, "model_name": model_name, "agg": agg}


def separate_audio(input_wav, vocal_wav, inst_wav, model_name="HP2_all_vocals.pth", agg=10, execution_policy=None):
    """
    使用 GPU 互斥锁调用无头 UVR5 提取纯伴奏与纯人声
    """
    input_wav = os.path.abspath(input_wav)
    vocal_wav = os.path.abspath(vocal_wav)
    inst_wav = os.path.abspath(inst_wav)
    worker_script = os.path.join(BASE_DIR, "core", "uvr_worker.py")
    cmd = [
        RVC_PYTHON,
        worker_script,
        "--input", input_wav,
        "--vocal_out", vocal_wav,
        "--inst_out", inst_wav,
        "--model", model_name,
        "--agg", str(agg),
        "--device", "auto",
        "--window-size", "512",
    ]

    if execution_policy is not None:
        from core.execution_policy import validate_execution_policy
        policy = validate_execution_policy(execution_policy)
        cmd[cmd.index('--device') + 1] = policy['device']
        cmd[cmd.index('--window-size') + 1] = str(policy['hp2_window'])
        cmd.extend(['--cpu-threads', str(policy['cpu_threads'])])
        if not policy.get('hp2_cudnn', True):
            cmd.append('--disable-cudnn')
        if policy.get('hp2_zero_initialized_bands', False):
            cmd.append('--zero-band-buffers')
    with GPU_LOCK:
        run_worker_with_recovery(
            cmd,
            [vocal_wav, inst_wav],
            timeout=14400,
            gpu_overrides={"window-size": 384},
            sidecar_path=vocal_wav + '.worker.json',
        )

    if not (os.path.exists(vocal_wav) and os.path.exists(inst_wav)):
        raise RuntimeError("UVR5 分轨结束，但未能生成预期的人声或伴奏音轨文件！")

    return vocal_wav, inst_wav


def separate_dual_audio(input_wav, output_dir, artifacts=None, device="auto", weight_dir=None, execution_policy=None):
    """Run Kim Vocal 2 and Inst HQ 3 as separate ONNX passes.

    ``artifacts`` is the frozen pair of model identities captured in the job
    specification. Explicit absolute paths and hashes are passed to the child
    worker, which rechecks the files before it opens either ONNX session.
    """
    from core.mdx_separator import (
        dual_model_arrays,
        model_identity,
        model_path_for_id,
        read_stereo_float,
        resample_stereo,
        verify_model_identity,
    )

    input_wav = os.path.abspath(os.fspath(input_wav))
    output_dir = os.path.abspath(os.fspath(output_dir))
    os.makedirs(output_dir, exist_ok=True)
    if not os.path.isfile(input_wav):
        raise FileNotFoundError("双模型分离输入不存在：" + input_wav)

    frozen = artifacts
    if isinstance(frozen, dict) and isinstance(frozen.get("effective_separator"), dict):
        frozen = frozen["effective_separator"]
    if frozen is None:
        frozen = {}
    if not isinstance(frozen, dict):
        raise ValueError("冻结的双模型身份格式无效")
    kim_expected = frozen.get("vocal") or frozen.get("kim_vocal_2")
    inst_expected = frozen.get("instrumental") or frozen.get("inst_hq_3")

    default_dir = weight_dir or os.path.join(BASE_DIR, "tools", "UVR5", "models", "MDX_Net_Models")
    kim_path = os.path.abspath(os.fspath(kim_expected.get("path"))) if isinstance(kim_expected, dict) and kim_expected.get("path") else str(model_path_for_id("kim_vocal_2", default_dir))
    inst_path = os.path.abspath(os.fspath(inst_expected.get("path"))) if isinstance(inst_expected, dict) and inst_expected.get("path") else str(model_path_for_id("inst_hq_3", default_dir))
    kim_identity = verify_model_identity(kim_path, kim_expected)
    inst_identity = verify_model_identity(inst_path, inst_expected)
    kim_metadata_path = kim_identity["metadata_path"]
    if inst_identity["metadata_path"] != kim_metadata_path:
        raise ValueError("双模型任务必须冻结同一份 UVR model_data.json")
    if os.path.basename(kim_path) != "Kim_Vocal_2.onnx":
        raise ValueError("冻结的人声模型必须是 Kim_Vocal_2.onnx")
    if os.path.basename(inst_path) != "UVR-MDX-NET-Inst_HQ_3.onnx":
        raise ValueError("冻结的伴奏模型必须是 UVR-MDX-NET-Inst_HQ_3.onnx")

    names = {
        "kim_vocal": os.path.join(output_dir, "kim_vocal.wav"),
        "kim_instrumental": os.path.join(output_dir, "kim_instrumental.wav"),
        "insthq3_vocal": os.path.join(output_dir, "insthq3_vocal.wav"),
        "insthq3_instrumental": os.path.join(output_dir, "insthq3_instrumental.wav"),
    }
    worker_script = os.path.join(BASE_DIR, "core", "mdx_worker.py")
    command = [
        RVC_PYTHON, worker_script,
        "--input", input_wav,
        "--output-dir", output_dir,
        "--kim-model", kim_path,
        "--inst-model", inst_path,
        "--device", str(device),
        "--kim-sha256", kim_identity["sha256"],
        "--kim-metadata-md5", kim_identity["metadata_tail_md5"],
        "--kim-metadata-sha256", kim_identity["metadata_sha256"],
        "--kim-frozen-metadata", json.dumps(kim_identity["metadata"], sort_keys=True, separators=(",", ":")),
        "--inst-sha256", inst_identity["sha256"],
        "--inst-metadata-md5", inst_identity["metadata_tail_md5"],
        "--inst-metadata-sha256", inst_identity["metadata_sha256"],
        "--inst-frozen-metadata", json.dumps(inst_identity["metadata"], sort_keys=True, separators=(",", ":")),
        "--metadata-path", kim_metadata_path,
    ]
    if execution_policy is not None:
        from core.execution_policy import validate_execution_policy
        policy = validate_execution_policy(execution_policy)
        command[command.index('--device') + 1] = policy['device']
        command.extend(['--cpu-threads', str(policy['cpu_threads'])])
    with GPU_LOCK:
        completed = run_worker_with_recovery(
            command,
            list(names.values()),
            timeout=14400,
            # MDX dimensions are fixed by each model's ONNX shape. A CUDA OOM
            # retries in a fresh CPU-only torch transform process.
            gpu_overrides={"device": "cpu"},
            sidecar_path=os.path.join(output_dir, "worker.json"),
        )
    for path in names.values():
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            raise RuntimeError("双模型分离未生成完整输出：" + path)
    worker_path = os.path.join(output_dir, "worker.json")
    metadata = getattr(completed, "worker_metadata", None)
    if not isinstance(metadata, dict):
        try:
            with open(worker_path, "r", encoding="utf-8") as handle:
                metadata = json.load(handle)
        except (OSError, ValueError):
            metadata = {}
    stdout = getattr(completed, "stdout", b"") or b""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    worker_execution = None
    for line in reversed(str(stdout).splitlines()):
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("worker") == "mdx" and payload.get("status") == "complete":
            worker_execution = payload
            break
    if worker_execution is None:
        raise RuntimeError("双模型 worker 未返回实际 provider 与推理完成证据")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    metadata["worker_execution"] = worker_execution
    # run_worker_with_recovery writes the retry envelope first; enrich it with
    # the successful child's actual session providers and inference evidence.
    temporary = worker_path + f".part.{os.getpid()}"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temporary, worker_path)
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass
    return {
        **names,
        "worker_path": worker_path,
        "selected_device": metadata.get("selected_device"),
        "device": worker_execution.get("device"),
        "inference_device": worker_execution.get("inference_device"),
        "onnx_provider": worker_execution.get("onnx_provider"),
        "models_execution": worker_execution.get("models_execution"),
    }

def separate_four_stems(input_wav, output_dir, execution_policy=None):
    os.makedirs(output_dir, exist_ok=True)
    paths = {key: os.path.join(output_dir, key + '.wav') for key in ('vocal', 'drums', 'bass', 'other')}
    cmd = [RVC_PYTHON, os.path.join(BASE_DIR, 'core', 'creative_worker.py'), 'separate',
           os.path.abspath(input_wav), os.path.abspath(output_dir), '--device', 'auto', '--segment', '6']
    if execution_policy is not None:
        from core.execution_policy import validate_execution_policy
        policy = validate_execution_policy(execution_policy)
        cmd[cmd.index('--device') + 1] = policy['device']
        cmd[cmd.index('--segment') + 1] = str(policy['demucs_segment'])
        cmd.extend(['--cpu-threads', str(policy['cpu_threads'])])
    with GPU_LOCK:
        run_worker_with_recovery(
            cmd,
            paths.values(),
            timeout=14400,
            gpu_overrides={"segment": 3},
            sidecar_path=os.path.join(output_dir, 'worker.json'),
        )
    return paths
