"""Catalog voice-separator implementations and compare caller-supplied runs.

Implementation, local availability, job selection, and listening acceptance
are separate facts. The MDX route is a Kim Vocal 2 + Inst HQ 3 dual profile;
the legacy backend name ``mdx`` is never treated as a selectable model.
"""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

from core.analyzer import probe_metadata
from core.qc import sample_metrics
from core.separator_choice import HP2_BACKEND, resolve_separator_request
from core.separator_routing import INST_HQ_3, KIM_VOCAL_2, default_weight_dir, dual_model_identities, plan_separator_routes

SCHEMA_VERSION = 1
NOT_RUN = "NOT_RUN"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
SELECTABLE = "SELECTABLE"
WEIGHT_PRESENT = "WEIGHT_PRESENT_NOT_SELECTABLE"
WEIGHT_MISSING = "WEIGHT_MISSING"
IMPLEMENTED_NOT_SELECTABLE = "IMPLEMENTED_NOT_SELECTABLE"
DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
REQUIRES_PYTHON_310 = "REQUIRES_PYTHON_310"
NOT_A_VOICE_BACKEND = "NOT_A_VOICE_BACKEND"
MEASURED = "MEASURED"
FAILED = "FAILED"
EXTERNAL = "READY_FOR_EXTERNAL_VALIDATION"

# Quality names from the plan stay unassigned until a listened A/B exists.
QUALITY_MODES = ("fast", "balanced", "quality", "expert", "auto")
_BACKENDS = (
    {"id": "hp2", "model_name": "HP2_all_vocals.pth", "agg": 10, "family": "uvr_vr", "job_selectable": True, "probe": "uvr_file"},
    {"id": "hp3", "model_name": "HP3_all_vocals.pth", "agg": 10, "family": "uvr_vr", "job_selectable": False, "probe": "uvr_file"},
    {"id": "hp5", "model_name": "HP5_only_main_vocal.pth", "agg": 10, "family": "uvr_vr", "job_selectable": False, "probe": "uvr_file"},
    {"id": "mdx", "model_name": None, "agg": None, "family": "mdx", "job_selectable": False, "probe": "dual_profile"},
    {"id": "roformer", "model_name": None, "agg": None, "family": "roformer", "job_selectable": False, "probe": "candidate"},
    {"id": "pymss", "model_name": None, "agg": None, "family": "pymss", "job_selectable": False, "probe": "candidate"},
    {"id": "htdemucs", "model_name": None, "agg": None, "family": "demucs", "job_selectable": False, "probe": "transcribe_only"},
    {"id": "ensemble", "model_name": None, "agg": None, "family": "ensemble", "job_selectable": False, "probe": "ensemble"},
)


def separator_catalog(weight_dir=None, *, mdx_weight_dir=None, candidate_model_dir=None):
    """Describe candidates. benchmark_status stays NOT_RUN until a real run."""
    if weight_dir is None:
        from core.config import RVC_ROOT
        weight_dir = os.path.join(RVC_ROOT, "uvr5_weights")
    if mdx_weight_dir is None:
        mdx_weight_dir = default_weight_dir()
    if candidate_model_dir is None:
        from core.config import BASE_DIR
        candidate_model_dir = os.path.join(BASE_DIR, "tools", "UVR5", "models", "RoFormer_Models")
    rows = []
    for spec in _BACKENDS:
        if spec["probe"] == "dual_profile":
            rows.append(_mdx_dual_row(spec, mdx_weight_dir))
        elif spec["probe"] == "ensemble":
            rows.append(_ensemble_row(spec, weight_dir, mdx_weight_dir))
        elif spec["probe"] == "candidate":
            rows.append(_candidate_row(spec, candidate_model_dir))
        else:
            rows.append(_row(spec, weight_dir))
    selectable = [row["id"] for row in rows if row["status"] == SELECTABLE]
    return {
        "schema_version": SCHEMA_VERSION,
        "default_backend": HP2_BACKEND,
        "selectable": selectable,
        "quality_modes": {name: {"status": "NOT_CONFIGURED", "backend": None} for name in QUALITY_MODES},
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "backends": rows,
    }


def run_ensemble_experiment(source_wav, output_dir, *, hp2_weight_dir=None, mdx_weight_dir=None, device="auto", hp2_tta=False):
    if type(hp2_tta) is not bool:raise ValueError('候选 TTA 必须是布尔值')
    """Run a same-source HP2 + Kim/Inst dual experiment and sample-wise mean.

    The output directory must be new. The original and normalized source,
    each backend's raw and normalized stems, and the experimental mean are
    preserved. This never enables the production job route.
    """
    from core.config import BASE_DIR, RVC_PYTHON, RVC_ROOT
    from core.mdx_separator import dual_model_identities
    from core.separator import separate_dual_audio
    from core.uvr_candidate_separator import separate_candidate_hp2
    from core.separator_ensemble import (
        combine_aligned_mean,
        sha256_file,
        standardize_source,
        standardize_stem_to_reference,
    )

    if device not in ("auto", "cpu", "cuda"):
        raise ValueError("ensemble 实验设备仅支持 auto/cpu/cuda")
    source = Path(os.path.abspath(os.fspath(source_wav)))
    if source.is_symlink() or not source.is_file():
        raise ValueError("分离基准源文件不存在或不是普通文件")
    source = source.resolve()
    output = Path(os.path.abspath(os.fspath(output_dir)))
    if os.path.lexists(str(output)):
        raise FileExistsError("分离实验只接受新的输出目录，拒绝覆盖已有目录")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()

    hp2_dir = Path(hp2_weight_dir or os.path.join(RVC_ROOT, "uvr5_weights")).resolve()
    mdx_dir = Path(mdx_weight_dir or default_weight_dir()).resolve()
    hp2_weight = hp2_dir / "HP2_all_vocals.pth"
    if hp2_weight.is_symlink() or not hp2_weight.is_file() or hp2_weight.stat().st_size <= 0:
        raise FileNotFoundError("缺少 HP2_all_vocals.pth；ensemble 实验没有运行")
    hp2_weight_identity = _file_identity(hp2_weight, sha256_file)
    dual_identity = dual_model_identities(mdx_dir)
    route = plan_separator_routes(KIM_VOCAL_2, INST_HQ_3, weight_dir=str(mdx_dir))
    if route.get("available") is not True:
        raise RuntimeError("Kim Vocal 2 / Inst HQ 3 dual 路线不可用：" + str(route.get("reason")))

    code_root = Path(BASE_DIR).resolve()
    rvc_root = Path(RVC_ROOT).resolve()
    engine_code_paths = [
        code_root / "core" / "separator.py",
        code_root / "core" / "uvr_worker.py",
        code_root / "core" / "uvr_candidate_worker.py",
        code_root / "core" / "uvr_candidate_separator.py",
        code_root / "core" / "uvr_exact_length.py",
        code_root / "core" / "mdx_separator.py",
        code_root / "core" / "mdx_worker.py",
        code_root / "core" / "separator_ensemble.py",
        code_root / "core" / "separator_benchmark.py",
        code_root / "core" / "worker_runtime.py",
        code_root / "core" / "config.py",
        code_root / "core" / "analyzer.py",
        rvc_root / "infer_uvr5.py",
    ]
    uvr_package = rvc_root / "uvr5_pack"
    if uvr_package.is_dir():
        engine_code_paths.extend(sorted(uvr_package.rglob("*.py")))
    code_before = _code_identity(engine_code_paths, sha256_file)
    runtime_identity = _runtime_identity(RVC_PYTHON, sha256_file)
    source_sha_before = sha256_file(source)
    manifest = {
        "schema_version": 1,
        "kind": "same_source_separator_ensemble_experiment",
        "status": "RUNNING",
        "default_backend": HP2_BACKEND,
        "job_selectable": False,
        "selectable_changed": False,
        "hp2_tta": hp2_tta,
        "source": {"path": str(source), "sha256_before": source_sha_before},
        "common_input": None,
        "runtime": runtime_identity,
        "engine_code_sha256": code_before,
        "quality_acceptance": "NOT_ASSESSED",
        "listening_quality": "not_assessed",
        "limitations": [
            "ensemble 仅为 HP2 与 dual 输出的逐样本算术均值，不代表音质提升。",
            "不更改 production 默认分轨或任务可选后端。",
            "源与单模型原始/规范化 stem 均独立保留。",
        ],
        "current_stage": "standardize_source",
        "runs": {},
    }
    manifest_path = output / "experiment.json"
    try:
        common_source = standardize_source(source, output / "source_48k_float.wav")
        manifest["common_input"] = common_source
        expected_frames = common_source["frames"]

        manifest["current_stage"] = "run_hp2"
        hp2_root = output / "hp2_candidate_raw"
        started = time.monotonic()
        try:
            hp2_outputs = separate_candidate_hp2(
                str(common_source["path"]), str(hp2_root), model_path=str(hp2_weight),
                device=device, agg=10, **({'tta':True} if hp2_tta else {}),
            )
        except Exception:
            failure_evidence = hp2_root / "candidate-worker-evidence.json"
            if failure_evidence.is_file():
                failed_hp2 = _read_json(failure_evidence)
                manifest["runs"]["hp2"] = {
                    "status": "FAILED",
                    "engine": "candidate-local HP2 exact-length worker",
                    "model": hp2_weight_identity,
                    "worker_evidence": failed_hp2,
                    "raw_outputs": {
                        name: _file_identity(path, sha256_file)
                        for name, path in (("vocal", hp2_root / "vocal.wav"),
                                           ("instrumental", hp2_root / "instrumental.wav"))
                        if path.is_file()
                    },
                    "quality_acceptance": "NOT_ASSESSED",
                    "listening_quality": "not_assessed",
                }
            raise
        hp2_vocal_raw = Path(hp2_outputs["vocal"])
        hp2_instrumental_raw = Path(hp2_outputs["instrumental"])
        hp2_elapsed = time.monotonic() - started
        hp2_worker = hp2_outputs["report"]
        hp2_record = {
            "status": "EXECUTED_RAW",
            "engine": "candidate-local bundled UVR5 VR worker with measured band ISTFT lengths",
            "model": hp2_weight_identity,
            "parameters": {"model_name": "HP2_all_vocals.pth", "agg": 10,
                           "device_request": device, "window_size": 512, "oom_retry_window_size": 384,
                           "length_adapter": "candidate-local exact source-count ISTFT",
                           "native_output_subtype": "PCM_16"},
            "elapsed_seconds": hp2_elapsed,
            "worker_evidence": hp2_worker,
            "worker_evidence_file": _file_identity(hp2_outputs["evidence"], sha256_file),
            "worker_sidecar": _read_json(hp2_outputs["worker_sidecar"]),
            "raw_outputs": {
                "vocal": _file_identity(hp2_vocal_raw, sha256_file),
                "instrumental": _file_identity(hp2_instrumental_raw, sha256_file),
            },
            "normalized_outputs": {},
        }
        manifest["runs"]["hp2"] = hp2_record
        manifest["current_stage"] = "verify_hp2_identity_after_run"
        hp2_weight_after = _file_identity(hp2_weight, sha256_file)
        hp2_record["model_after"] = hp2_weight_after
        if hp2_weight_after != hp2_weight_identity:
            raise RuntimeError("HP2 权重在实验期间发生变化")
        manifest["current_stage"] = "normalize_hp2_vocal"
        hp2_vocal = standardize_stem_to_reference(
            hp2_vocal_raw, output / "normalized" / "hp2_vocal_48k_float.wav", expected_frames,
        )
        hp2_record["normalized_outputs"]["vocal"] = hp2_vocal
        manifest["current_stage"] = "normalize_hp2_instrumental"
        hp2_instrumental = standardize_stem_to_reference(
            hp2_instrumental_raw, output / "normalized" / "hp2_instrumental_48k_float.wav", expected_frames,
        )
        hp2_record["normalized_outputs"]["instrumental"] = hp2_instrumental
        hp2_record["status"] = "EXECUTED"

        manifest["current_stage"] = "run_dual"
        dual_root = output / "dual_raw"
        dual_root.mkdir()
        started = time.monotonic()
        dual_outputs = separate_dual_audio(
            str(common_source["path"]), str(dual_root), artifacts=dual_identity,
            device=device, weight_dir=str(mdx_dir),
        )
        dual_elapsed = time.monotonic() - started
        dual_raw = {
            name: _file_identity(dual_outputs[name], sha256_file)
            for name in ("kim_vocal", "kim_instrumental", "insthq3_vocal", "insthq3_instrumental")
        }
        worker_path = dual_outputs.get("worker_path")
        dual_worker = _read_json(Path(worker_path)) if worker_path else None
        dual_execution = {
            key: dual_outputs.get(key)
            for key in ("selected_device", "device", "inference_device", "onnx_provider", "models_execution")
        }
        dual_record = {
            "status": "EXECUTED_RAW",
            "engine": "bundled MDX ONNX worker",
            "profile": "Kim Vocal 2 vocal + UVR-MDX-NET Inst HQ 3 instrumental",
            "model_identities": dual_identity,
            "parameters": {"device_request": device, "input_rate_hz": common_source["sample_rate"],
                           "model_rate_hz": 44100},
            "elapsed_seconds": dual_elapsed,
            "execution_summary": dual_execution,
            "worker_evidence": dual_worker,
            "raw_outputs": dual_raw,
            "normalized_outputs": {},
        }
        manifest["runs"]["dual"] = dual_record
        manifest["current_stage"] = "verify_dual_identity_after_run"
        dual_identity_after = dual_model_identities(mdx_dir)
        dual_record["model_identities_after"] = dual_identity_after
        if _canonical_json(dual_identity_after) != _canonical_json(dual_identity):
            raise RuntimeError("dual 权重/metadata 在实验期间发生变化")
        manifest["current_stage"] = "normalize_dual_stems"
        dual_normalized = {
            "vocal": standardize_stem_to_reference(
                dual_outputs["kim_vocal"], output / "normalized" / "dual_vocal_48k_float.wav", expected_frames,
            ),
            "instrumental": standardize_stem_to_reference(
                dual_outputs["insthq3_instrumental"], output / "normalized" / "dual_instrumental_48k_float.wav", expected_frames,
            ),
        }
        dual_record["normalized_outputs"] = dual_normalized
        dual_record["status"] = "EXECUTED"

        manifest["current_stage"] = "combine_stems"
        ensemble_root = output / "ensemble"
        ensemble_root.mkdir()
        mean_outputs = {
            "vocal": combine_aligned_mean(
                common_source["path"], hp2_vocal["path"], dual_normalized["vocal"]["path"],
                ensemble_root / "vocal_mean_48k_float.wav",
            ),
            "instrumental": combine_aligned_mean(
                common_source["path"], hp2_instrumental["path"], dual_normalized["instrumental"]["path"],
                ensemble_root / "instrumental_mean_48k_float.wav",
            ),
        }
        manifest["runs"]["ensemble"] = {
            "status": "EXECUTED",
            "combiner": "samplewise_arithmetic_mean",
            "parameters": {"inputs": ["hp2", "dual"],
                           "normalization": "independent to source 48000 Hz stereo float"},
            "outputs": mean_outputs,
            "quality_acceptance": "NOT_ASSESSED",
            "listening_quality": "not_assessed",
        }

        source_sha_after = sha256_file(source)
        if source_sha_after != source_sha_before:
            raise RuntimeError("分轨实验期间原始源发生变化")
        code_after = _code_identity(engine_code_paths, sha256_file)
        if code_after != code_before:
            raise RuntimeError("分轨实验期间 engine/helper 源码发生变化")
        runtime_after = _runtime_identity(RVC_PYTHON, sha256_file)
        if runtime_after != runtime_identity:
            raise RuntimeError("分轨实验期间 worker runtime 身份发生变化")
        manifest["source"]["sha256_after"] = source_sha_after
        manifest["status"] = "EXECUTED_NOT_ASSESSED"
        manifest["current_stage"] = None
        manifest["engine_code_sha256_after"] = code_after
        manifest["runtime_after"] = runtime_after
        _write_json_atomic(manifest_path, manifest)
        return manifest
    except Exception as exc:
        manifest["status"] = "FAILED"
        manifest["error"] = {"stage": manifest.get("current_stage"), "type": type(exc).__name__, "message": str(exc)}
        manifest["source"].setdefault("sha256_after", sha256_file(source))
        _write_json_atomic(manifest_path, manifest)
        raise


def compare_recorded_runs(source_id, reference, other):
    """Subtract two executed measurements. Neither side becomes selectable here."""
    record = {
        "schema_version": SCHEMA_VERSION,
        "status": NOT_RUN,
        "source_id": source_id,
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "metrics_are_audited_separator": False,
        "selectable_changed": False,
        "reference_backend": None,
        "other_backend": None,
        "other_job_selectable": False,
        "metrics": None,
        "missing_evidence": ["同源实测", "人工听审"],
    }
    if not _executed(reference) or not _executed(other):
        return record
    if reference.get("source_id") != source_id or other.get("source_id") != source_id:
        record["missing_evidence"] = ["两次测量的 source_id 不一致", "人工听审"]
        return record
    if reference.get("backend") != HP2_BACKEND:
        record["missing_evidence"] = ["对照基准必须是 hp2", "人工听审"]
        return record
    if other.get("backend") == HP2_BACKEND:
        record["missing_evidence"] = ["对照后端不能仍是 hp2", "人工听审"]
        return record
    if other.get("job_selectable") is True:
        raise ValueError("测量结果不能把非默认后端写成任务可选")
    record["status"] = MEASURED
    record["reference_backend"] = HP2_BACKEND
    record["other_backend"] = other["backend"]
    record["other_job_selectable"] = False
    record["metrics"] = {
        "vocal_peak_delta": other["vocal_peak"] - reference["vocal_peak"],
        "vocal_duration_delta": other["vocal_duration"] - reference["vocal_duration"],
        "vocal_clipped_delta": other["vocal_clipped"] - reference["vocal_clipped"],
        "instrumental_peak_delta": other["instrumental_peak"] - reference["instrumental_peak"],
        "instrumental_clipped_delta": other["instrumental_clipped"] - reference["instrumental_clipped"],
    }
    record["missing_evidence"] = ["人工听审"]
    record["acceptance_status"] = EXTERNAL
    return record


def run_weight_experiment(source_wav, backend_ids, output_dir):
    """Run UVR weights that already exist. This does not change the job default."""
    if not os.path.isfile(source_wav):
        raise ValueError("分离基准源文件不存在")
    catalog = separator_catalog()
    by_id = {row["id"]: row for row in catalog["backends"]}
    os.makedirs(output_dir, exist_ok=True)
    runs = []
    for backend_id in backend_ids:
        row = by_id.get(backend_id)
        if row is None:
            runs.append(_failed(backend_id, None, "未知分离后端"))
            continue
        if not row["model_name"]:
            runs.append(_not_run(row, row.get("unavailable_reason") or "该配置不是单个 UVR 权重实验"))
            continue
        if not row["weight_present"]:
            runs.append(_not_run(row, "权重文件不存在"))
            continue
        runs.append(_separate_one(source_wav, row, output_dir))
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "separator_weight_experiment",
        "default_backend": HP2_BACKEND,
        "selectable": [HP2_BACKEND],
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "source_wav": os.path.abspath(source_wav),
        "runs": runs,
    }


def _row(spec, weight_dir):
    weight_path = None
    present = False
    if spec["probe"] == "uvr_file" and spec["model_name"]:
        weight_path = os.path.join(weight_dir, spec["model_name"])
        present = os.path.isfile(weight_path) and os.path.getsize(weight_path) > 0
    if spec["probe"] == "none":
        status = NOT_IMPLEMENTED
    elif spec["probe"] == "transcribe_only":
        status = NOT_A_VOICE_BACKEND
    elif spec["job_selectable"] and present:
        status = SELECTABLE
    elif present:
        status = WEIGHT_PRESENT
    else:
        status = WEIGHT_MISSING
    job_rejects = False
    if spec["id"] != HP2_BACKEND:
        try:
            resolve_separator_request(spec["id"])
        except ValueError:
            job_rejects = True
        else:
            raise ValueError("任务路径接受了未审核后端：" + spec["id"])
    return {
        "id": spec["id"],
        "family": spec["family"],
        "model_name": spec["model_name"],
        "agg": spec["agg"],
        "status": status,
        "benchmark_status": NOT_RUN,
        "job_selectable": status == SELECTABLE,
        "job_path_rejects": job_rejects,
        "weight_present": present,
        "weight_path": weight_path if present else None,
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "implemented": status != NOT_IMPLEMENTED,
        "available": status == SELECTABLE or present,
    }


def _mdx_dual_row(spec, weight_dir):
    """Report the actual dual profile without blessing the legacy ``mdx`` id."""
    route = plan_separator_routes(KIM_VOCAL_2, INST_HQ_3, weight_dir=weight_dir)
    identities = None
    identity_error = None
    try:
        identities = dual_model_identities(weight_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        identity_error = type(exc).__name__ + ": " + str(exc)
    implemented = bool(route.get("loader_implemented"))
    available = route.get("available") is True and identities is not None
    if available:
        status = IMPLEMENTED_NOT_SELECTABLE
        reason = "dual_profile 可调用并已冻结两个模型的实际身份；未执行本次基准，也不接受 legacy mdx 单后端。"
    elif not route.get("weight_present", {}).get(KIM_VOCAL_2) or not route.get("weight_present", {}).get(INST_HQ_3):
        status = WEIGHT_MISSING
        reason = route.get("reason") or "dual_profile 的一个或多个真实权重缺失。"
    elif not implemented:
        status = DEPENDENCY_MISSING
        reason = route.get("reason") or "MDX loader adapter 不可用。"
    else:
        status = WEIGHT_MISSING
        reason = identity_error or route.get("reason") or "MDX 双模型身份无法冻结。"
    job_rejects = False
    try:
        resolve_separator_request(spec["id"])
    except ValueError:
        job_rejects = True
    else:
        raise ValueError("legacy mdx id must remain rejected by the job path")
    return {
        "id": spec["id"],
        "profile_id": "dual_profile",
        "family": spec["family"],
        "model_name": None,
        "agg": None,
        "status": status,
        "implementation_status": "IMPLEMENTED" if implemented else "NOT_IMPLEMENTED",
        "availability_status": "AVAILABLE" if available else status,
        "benchmark_status": NOT_RUN,
        "job_selectable": False,
        "job_path_rejects": job_rejects,
        "implemented": implemented,
        "available": available,
        "weight_present": all(route.get("weight_present", {}).get(key) is True for key in (KIM_VOCAL_2, INST_HQ_3)),
        "model_identities": identities,
        "route": route,
        "unavailable_reason": reason,
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
    }


def _candidate_row(spec, model_dir):
    from core.candidate_separator import _BACKENDS as candidate_backends

    backend = spec["id"]
    candidate = candidate_backends[backend]
    implemented = True  # The adapter is implemented; it remains a non-job experiment.
    python_ok = sys.version_info >= (3, 10)
    try:
        dependency_available = importlib.util.find_spec(candidate["package"]) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        dependency_available = False
    model_path = Path(model_dir) / candidate["model"]
    config_path = Path(model_dir) / candidate["config"]
    support_paths = [Path(model_dir) / name for name in candidate["support_files"]]
    assets_present = all(path.is_file() and path.stat().st_size > 0
                         for path in [model_path, config_path, *support_paths])
    blockers = []
    if not python_ok:
        blockers.append(REQUIRES_PYTHON_310)
    if not dependency_available:
        blockers.append(DEPENDENCY_MISSING)
    if not assets_present:
        blockers.append(WEIGHT_MISSING)
    status = blockers[0] if blockers else IMPLEMENTED_NOT_SELECTABLE
    return {
        "id": backend,
        "family": spec["family"],
        "model_name": candidate["model"],
        "agg": None,
        "status": status,
        "implementation_status": "IMPLEMENTED",
        "availability_status": "AVAILABLE" if not blockers else blockers[0],
        "availability_blockers": blockers,
        "benchmark_status": NOT_RUN,
        "job_selectable": False,
        "job_path_rejects": True,
        "implemented": implemented,
        "available": not blockers,
        "weight_present": assets_present,
        "dependency_available": dependency_available,
        "python_version": ".".join(str(item) for item in sys.version_info[:3]),
        "model_path": str(model_path) if assets_present else None,
        "unavailable_reason": "；".join(blockers) if blockers else "候选适配器不在 production job 路径中，且质量未评估。",
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
    }


def _ensemble_row(spec, hp2_weight_dir, mdx_weight_dir):
    hp2_path = os.path.join(hp2_weight_dir, "HP2_all_vocals.pth")
    hp2_present = os.path.isfile(hp2_path) and os.path.getsize(hp2_path) > 0
    mdx_spec = next(item for item in _BACKENDS if item["id"] == "mdx")
    dual = _mdx_dual_row(mdx_spec, mdx_weight_dir)
    try:
        resolve_separator_request(spec["id"])
    except ValueError:
        job_rejects = True
    else:
        raise ValueError("实验性 ensemble 路线不得被 production job 接受")
    blockers = []
    if not hp2_present:
        blockers.append("HP2_WEIGHT_MISSING")
    if dual.get("available") is not True:
        blockers.extend(dual.get("availability_blockers") or [dual.get("availability_status") or dual.get("status")])
    available = not blockers
    return {
        "id": spec["id"],
        "family": spec["family"],
        "model_name": None,
        "agg": None,
        "status": IMPLEMENTED_NOT_SELECTABLE if available else (blockers[0] if blockers else WEIGHT_MISSING),
        "implementation_status": "IMPLEMENTED",
        "availability_status": "AVAILABLE" if available else blockers[0],
        "availability_blockers": blockers,
        "benchmark_status": NOT_RUN,
        "job_selectable": False,
        "job_path_rejects": job_rejects,
        "implemented": True,
        "available": available,
        "hp2_weight_present": hp2_present,
        "dual_profile_available": dual.get("available") is True,
        "profile": "hp2 + kim_vocal_2/inst_hq_3",
        "combiner": "samplewise_arithmetic_mean",
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "unavailable_reason": "；".join(blockers) if blockers else "仅限显式 same-source 实验，不在 production job route 中。",
    }


def _file_identity(path, hash_file):
    candidate = Path(os.path.abspath(os.fspath(path)))
    if candidate.is_symlink() or not candidate.is_file():
        raise FileNotFoundError("实验身份文件不存在或不是普通文件：" + str(candidate))
    for parent in candidate.parents:
        if parent.is_symlink():
            raise ValueError("实验身份路径包含符号链接：" + str(parent))
    target = candidate.resolve()
    return {"path": str(target), "size_bytes": target.stat().st_size, "sha256": hash_file(target)}


def _code_identity(paths, hash_file):
    return {str(Path(path).resolve()): _file_identity(path, hash_file)["sha256"] for path in paths}


def _runtime_identity(runtime_path, hash_file):
    path = Path(os.path.abspath(os.fspath(runtime_path)))
    if path.is_file() and not path.is_symlink():
        identity = _file_identity(path, hash_file)
    else:
        identity = {"path": str(path), "size_bytes": None, "sha256": None}
    return {"executable": identity, "runtime_versions": _runtime_version(path)}


def _runtime_version(path):
    import subprocess

    try:
        script = ("import importlib.metadata as m,platform,sys; "
                  "print(platform.python_version()); "
                  "print(';'.join(k+'='+str(m.version(k)) for k in "
                  "('onnxruntime','torch','librosa','scipy') if any(d.metadata.get('Name','').lower()==k "
                  "for d in m.distributions())))")
        completed = subprocess.run(
            [str(path), "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
        )
        if completed.returncode == 0:
            return completed.stdout.decode("utf-8", errors="replace").strip()
        return "UNAVAILABLE: exit {}".format(completed.returncode)
    except (OSError, subprocess.SubprocessError) as exc:
        return "UNAVAILABLE: {}".format(type(exc).__name__)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json_atomic(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".part")
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _separate_one(source_wav, row, output_dir):
    from core.separator import separate_audio

    folder = os.path.join(output_dir, row["id"])
    os.makedirs(folder, exist_ok=True)
    vocal = os.path.join(folder, "vocal.wav")
    instrumental = os.path.join(folder, "instrumental.wav")
    try:
        separate_audio(source_wav, vocal, instrumental, model_name=row["model_name"], agg=row["agg"])
        vocal_metrics = _file_metrics(vocal)
        instrumental_metrics = _file_metrics(instrumental)
    except Exception as exc:
        return _failed(row["id"], row["model_name"], type(exc).__name__ + ": " + str(exc))
    return {
        "backend": row["id"],
        "model_name": row["model_name"],
        "agg": row["agg"],
        "benchmark_status": MEASURED,
        "job_selectable": row["id"] == HP2_BACKEND,
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "vocal_wav": vocal,
        "instrumental_wav": instrumental,
        "vocal": vocal_metrics,
        "instrumental": instrumental_metrics,
        "error": None,
    }


def _file_metrics(path):
    metrics = sample_metrics(path)
    meta = probe_metadata(path)
    return {
        "sample_peak": metrics["sample_peak"],
        "clipped_samples": metrics["clipped_samples"],
        "channel_rms": list(metrics["channel_rms"]),
        "duration_seconds": meta["duration"],
        "sample_rate": meta["sample_rate"],
        "channels": meta["channels"],
    }


def _executed(payload):
    if not isinstance(payload, dict) or payload.get("executed") is not True:
        return False
    if payload.get("source_id") is None or payload.get("backend") is None:
        return False
    for key in ("vocal_peak", "vocal_duration", "vocal_clipped", "instrumental_peak", "instrumental_clipped"):
        value = payload.get(key)
        if type(value) is bool or type(value) not in (int, float):
            return False
    return True


def _not_run(row, reason):
    return {
        "backend": row["id"],
        "model_name": row.get("model_name"),
        "benchmark_status": NOT_RUN,
        "job_selectable": False if row["id"] != HP2_BACKEND else row.get("job_selectable") is True,
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "implemented": row.get("implemented", False),
        "available": row.get("available", False),
        "error": reason,
    }


def _failed(backend_id, model_name, error):
    return {
        "backend": backend_id,
        "model_name": model_name,
        "benchmark_status": FAILED,
        "job_selectable": False,
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "error": error,
    }
