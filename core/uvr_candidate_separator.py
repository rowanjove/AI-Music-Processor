"""Parent-side launcher for the experimental exact-length HP2 worker."""
import json
import os

from core.config import GPU_LOCK, RVC_PYTHON, RVC_ROOT, BASE_DIR
from core.worker_runtime import run_worker_with_recovery


def separate_candidate_hp2(source_wav, output_dir, *, model_path, device="auto", agg=10, tta=False):
    if type(tta) is not bool:raise ValueError('候选 TTA 必须是布尔值')
    source = _regular_file(source_wav, "candidate HP2 source")
    model = _regular_file(model_path, "candidate HP2 model")
    root = os.path.abspath(os.fspath(output_dir))
    if os.path.lexists(root):
        raise FileExistsError("candidate HP2 output directory must be new")
    os.makedirs(root)
    vocal = os.path.join(root, "vocal.wav")
    instrumental = os.path.join(root, "instrumental.wav")
    evidence = os.path.join(root, "candidate-worker-evidence.json")
    worker = os.path.join(BASE_DIR, "core", "uvr_candidate_worker.py")
    command = [
        RVC_PYTHON, worker,
        "--input", source,
        "--vocal-out", vocal,
        "--inst-out", instrumental,
        "--model-path", model,
        "--work-root", root,
        "--evidence", evidence,
        "--agg", str(int(agg)),
        "--device", device,
        "--window-size", "512",
    ]
    if tta:command.append('--tta')
    with GPU_LOCK:
        run_worker_with_recovery(
            command,
            [vocal, instrumental],
            timeout=14400,
            gpu_overrides={"window-size": 384},
            sidecar_path=vocal + ".worker.json",
        )
    if not (os.path.isfile(vocal) and os.path.isfile(instrumental) and os.path.isfile(evidence)):
        raise RuntimeError("candidate HP2 worker 未生成完整 stem/evidence")
    with open(evidence, "r", encoding="utf-8") as handle:
        report = json.load(handle)
    if report.get("status") != "EXECUTED":
        raise RuntimeError("candidate HP2 worker evidence 未确认完成")
    return {
        "vocal": vocal,
        "instrumental": instrumental,
        "evidence": evidence,
        "report": report,
        "worker_sidecar": vocal + ".worker.json",
    }


def _regular_file(path, label):
    target = os.path.abspath(os.fspath(path))
    if os.path.islink(target) or not os.path.isfile(target):
        raise FileNotFoundError(label + " missing or not a regular file: " + target)
    return target
