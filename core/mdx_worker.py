"""Isolated CPU-ONNX worker for the two bundled UVR MDX separator models."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.mdx_separator import (
    MDX_FILENAMES,
    dual_model_arrays,
    model_identity,
    read_stereo_float,
    resample_stereo,
    separate_model_array,
)
from core.worker_runtime import clear_cuda, emit_worker_error, is_cuda_oom


def _write_outputs(output_dir: str, outputs, before_commit=None) -> dict[str, str]:
    import numpy as np
    import soundfile as sf

    os.makedirs(output_dir, exist_ok=True)
    names = {
        "kim_vocal": "kim_vocal.wav",
        "kim_instrumental": "kim_instrumental.wav",
        "insthq3_vocal": "insthq3_vocal.wav",
        "insthq3_instrumental": "insthq3_instrumental.wav",
    }
    final_paths = {key: os.path.abspath(os.path.join(output_dir, name)) for key, name in names.items()}
    temporary_paths = {key: path + f".part.{os.getpid()}.wav" for key, path in final_paths.items()}
    try:
        for key, path in final_paths.items():
            audio = np.asarray(outputs[key], dtype=np.float32)
            if audio.ndim != 2 or audio.shape[1] != 2 or audio.shape[0] <= 0 or not np.isfinite(audio).all():
                raise RuntimeError("MDX 输出无效：" + key)
            sf.write(temporary_paths[key], audio, int(outputs["sample_rate"]), subtype="FLOAT", format="WAV")
        if before_commit is not None:
            before_commit()
        for key in names:
            os.replace(temporary_paths[key], final_paths[key])
    finally:
        for path in temporary_paths.values():
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
    return final_paths


def run_dual(input_path: str, output_dir: str, kim_path: str, inst_path: str, device: str = "auto",
             expected_kim_sha256: str = "", expected_kim_md5: str = "",
             expected_inst_sha256: str = "", expected_inst_md5: str = "",
             expected_kim_metadata_sha256: str = "", expected_kim_metadata=None,
             expected_inst_metadata_sha256: str = "", expected_inst_metadata=None,
             metadata_path: str | None = None, progress_callback=None, cpu_threads=None) -> dict:
    """Run the two distinct MDX models on the same input and write all four stems."""
    import numpy as np

    if device not in ("auto", "cpu", "cuda"):
        raise ValueError("MDX 设备偏好必须是 auto、cpu 或 cuda")
    expected = (
        (kim_path, expected_kim_sha256, expected_kim_md5, expected_kim_metadata_sha256, expected_kim_metadata),
        (inst_path, expected_inst_sha256, expected_inst_md5, expected_inst_metadata_sha256, expected_inst_metadata),
    )
    identities = {}
    for path, sha256, md5, catalog_sha256, frozen_metadata in expected:
        identity = model_identity(path, metadata_path)
        if sha256 and identity["sha256"] != sha256:
            raise RuntimeError("MDX 权重 SHA-256 与任务冻结值不一致：" + os.path.basename(path))
        if md5 and identity["metadata_tail_md5"] != md5:
            raise RuntimeError("MDX 权重尾部 metadata hash 与任务冻结值不一致：" + os.path.basename(path))
        if catalog_sha256 and identity["metadata_sha256"] != catalog_sha256:
            raise RuntimeError("UVR model_data.json SHA-256 与任务冻结值不一致")
        if frozen_metadata is not None and identity["metadata"] != frozen_metadata:
            raise RuntimeError("MDX metadata values 与任务冻结值不一致：" + os.path.basename(path))
        identities[path] = identity

    original, source_rate = read_stereo_float(input_path)
    mix_44100 = resample_stereo(original, source_rate, 44100)
    kim_metadata = identities[kim_path]["metadata"]
    inst_metadata = identities[inst_path]["metadata"]
    if kim_metadata["primary_stem"] != "Vocals":
        raise RuntimeError("Kim Vocal 2 的 primary_stem 必须是 Vocals")
    if inst_metadata["primary_stem"] != "Instrumental":
        raise RuntimeError("Inst HQ 3 的 primary_stem 必须是 Instrumental")

    # Each ONNX session is released before loading the next 66 MiB model.
    def pass_progress(offset, label):
        if progress_callback is None:
            return None

        def report(processed, total):
            progress_callback({
                "fraction": offset + 0.5 * processed / total,
                "processed": processed,
                "total": total,
                "message": f"{label} 分块 {processed}/{total}",
            })

        return report

    execution = {}
    kim = separate_model_array(
        mix_44100, kim_path, kim_metadata, device=device,
        **({"cpu_threads": cpu_threads} if cpu_threads is not None else {}),
        progress_callback=pass_progress(0.0, "Kim Vocal 2"),
        execution_callback=lambda value: execution.update(kim_vocal_2=dict(value)),
    )
    inst = separate_model_array(
        mix_44100, inst_path, inst_metadata, device=device,
        **({"cpu_threads": cpu_threads} if cpu_threads is not None else {}),
        progress_callback=pass_progress(0.5, "Inst HQ 3"),
        execution_callback=lambda value: execution.update(inst_hq_3=dict(value)),
    )
    kim = resample_stereo(kim, 44100, source_rate)
    inst = resample_stereo(inst, 44100, source_rate)
    expected_frames = original.shape[0]
    kim = _match_frames(kim, expected_frames)
    inst = _match_frames(inst, expected_frames)
    outputs = dual_model_arrays(original, {"kim_vocal": kim, "insthq3_instrumental": inst})
    outputs["sample_rate"] = source_rate
    def verify_before_commit():
        for path, sha256, md5, catalog_sha256, frozen_metadata in expected:
            identity = model_identity(path, metadata_path)
            if sha256 and identity["sha256"] != sha256:
                raise RuntimeError("MDX 权重在输出提交前发生变化：" + os.path.basename(path))
            if md5 and identity["metadata_tail_md5"] != md5:
                raise RuntimeError("MDX 权重尾部 hash 在输出提交前发生变化：" + os.path.basename(path))
            if catalog_sha256 and identity["metadata_sha256"] != catalog_sha256:
                raise RuntimeError("UVR model_data.json 在输出提交前发生变化")
            if frozen_metadata is not None and identity["metadata"] != frozen_metadata:
                raise RuntimeError("MDX metadata values 在输出提交前发生变化：" + os.path.basename(path))

    paths = _write_outputs(output_dir, outputs, before_commit=verify_before_commit)
    clear_cuda(__import__("torch"))
    model_execution = execution
    devices = {row.get("inference_device") for row in model_execution.values()}
    actual_device = next(iter(devices)) if len(devices) == 1 else "mixed"
    providers = {name: row.get("selected_provider") for name, row in model_execution.items()}
    return {
        "outputs": paths,
        "sample_rate": source_rate,
        "frames": expected_frames,
        "onnx_provider": providers,
        "device": actual_device,
        "inference_device": actual_device,
        "torch_device": actual_device,
        "cpu_threads": cpu_threads,
        "models_execution": model_execution,
        "models": {
            "kim_vocal_2": {"path": os.path.abspath(kim_path), "metadata_tail_md5": identities[kim_path]["metadata_tail_md5"]},
            "inst_hq_3": {"path": os.path.abspath(inst_path), "metadata_tail_md5": identities[inst_path]["metadata_tail_md5"]},
        },
    }


def _match_frames(audio, expected: int):
    import numpy as np

    if audio.shape[0] > expected:
        return np.ascontiguousarray(audio[:expected])
    if audio.shape[0] < expected:
        return np.ascontiguousarray(np.pad(audio, ((0, expected - audio.shape[0]), (0, 0))))
    return audio


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--kim-model", required=True)
    parser.add_argument("--inst-model", required=True)
    parser.add_argument("--cpu-threads", type=int, choices=range(1, 5), default=None)
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument("--kim-sha256", default="")
    parser.add_argument("--kim-metadata-md5", default="")
    parser.add_argument("--inst-sha256", default="")
    parser.add_argument("--inst-metadata-md5", default="")
    parser.add_argument("--kim-metadata-sha256", default="")
    parser.add_argument("--inst-metadata-sha256", default="")
    parser.add_argument("--kim-frozen-metadata", default="")
    parser.add_argument("--inst-frozen-metadata", default="")
    parser.add_argument("--metadata-path", default=None)
    args = parser.parse_args(argv)
    device = args.device

    def emit_progress(value):
        print(json.dumps({"worker_progress": value}, ensure_ascii=False, sort_keys=True), flush=True)

    phase = "model_load"
    try:
        phase = "inference"
        report = run_dual(
            args.input, args.output_dir, args.kim_model, args.inst_model, device,
            expected_kim_sha256=args.kim_sha256, expected_kim_md5=args.kim_metadata_md5,
            expected_inst_sha256=args.inst_sha256, expected_inst_md5=args.inst_metadata_md5,
            expected_kim_metadata_sha256=args.kim_metadata_sha256,
            expected_kim_metadata=json.loads(args.kim_frozen_metadata) if args.kim_frozen_metadata else None,
            expected_inst_metadata_sha256=args.inst_metadata_sha256,
            expected_inst_metadata=json.loads(args.inst_frozen_metadata) if args.inst_frozen_metadata else None,
            metadata_path=args.metadata_path,
            progress_callback=emit_progress, cpu_threads=args.cpu_threads,
        )
        print(json.dumps({"worker": "mdx", "status": "complete", **report}, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        category = "cuda_oom" if is_cuda_oom(exc) else "worker_error"
        try:
            import torch
            clear_cuda(torch)
        except Exception:
            pass
        emit_worker_error(category, phase, device, exc, worker="mdx", traceback=traceback.format_exc()[-12000:])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
