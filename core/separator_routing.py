"""Plan separate vocal and instrumental routes.

HP2 remains one VR pass with agg 10. Kim Vocal 2 keeps only the vocal stem
and UVR-MDX-NET Inst HQ 3 keeps only the instrumental stem. Those two models
do not take HP2 agg. This module does not start a worker and does not copy
HP2 audio into the other models' names.
"""
import copy
import os

from core.separator_choice import HP2_BACKEND, resolve_separator_request

SCHEMA_VERSION = 1
KIM_VOCAL_2 = "kim_vocal_2"
INST_HQ_3 = "inst_hq_3"
EXTERNAL = "READY_FOR_EXTERNAL_VALIDATION"
KIM_FILE = "Kim_Vocal_2.onnx"
INST_FILE = "UVR-MDX-NET-Inst_HQ_3.onnx"
_KIM_COMPONENT = "engine.separator.kim_vocal_2"
_INST_COMPONENT = "engine.separator.inst_hq_3"


def _model(model_id, display_name, model_file, backend_kind, keeps, parameters, component_id):
    return {
        "id": model_id,
        "display_name": display_name,
        "model_file": model_file,
        "backend_kind": backend_kind,
        "keeps": keeps,
        "parameters": dict(parameters),
        "component_id": component_id,
    }


def hp2_model():
    """The VR model the current worker can run. agg belongs only to this model."""
    return _model(
        HP2_BACKEND, "HP2", "HP2_all_vocals.pth", "vr", "both",
        {"agg": 10}, "engine.separator.hp2",
    )


def kim_vocal_model():
    """MDX vocal model. No agg, and no invented segment or overlap numbers."""
    return _model(
        KIM_VOCAL_2, "Kim Vocal 2", KIM_FILE, "mdx", "vocal",
        {}, _KIM_COMPONENT,
    )


def inst_hq_model():
    """MDX instrumental model. No agg, and no invented segment or overlap numbers."""
    return _model(
        INST_HQ_3, "UVR-MDX-NET Inst HQ 3", INST_FILE, "mdx", "instrumental",
        {}, _INST_COMPONENT,
    )


def hp2_profile():
    model = hp2_model()
    return {"mode": "hp2", "vocal": model, "instrumental": copy.deepcopy(model)}


def dual_profile():
    return {"mode": "dual", "vocal": kim_vocal_model(), "instrumental": inst_hq_model()}


def validate_separator_profile(value):
    """Accept only the HP2 record or the Kim vocal + Inst HQ instrumental record."""
    if value == hp2_profile():
        return hp2_profile()
    if value == dual_profile():
        return dual_profile()
    raise ValueError("分离配置必须使用该模型自己的参数")


def mdx_loader_implemented():
    """Return whether the application has a callable MDX worker adapter."""
    from core.mdx_separator import separate_model_array
    from core.separator import separate_dual_audio
    return callable(separate_model_array) and callable(separate_dual_audio)


def default_weight_dir():
    from core.config import BASE_DIR
    return os.path.join(BASE_DIR, "tools", "UVR5", "models", "MDX_Net_Models")


def dual_model_identities(weight_dir=None):
    """Real MDX paths and hashes to include in a frozen JobSpec."""
    from core.mdx_separator import dual_model_identities as _identities
    return _identities(weight_dir or default_weight_dir())


def weight_present(directory, filename):
    path = os.path.join(directory, filename)
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def plan_separator_routes(vocal_backend=None, instrumental_backend=None, weight_dir=None):
    vocal = HP2_BACKEND if vocal_backend is None else vocal_backend
    instrumental = HP2_BACKEND if instrumental_backend is None else instrumental_backend
    if vocal == HP2_BACKEND and instrumental == HP2_BACKEND:
        return _hp2_plan()
    if vocal == KIM_VOCAL_2 and instrumental == INST_HQ_3:
        return _dual_plan(weight_dir)
    vocal_reason = _reject_reason(vocal)
    instrumental_reason = _reject_reason(instrumental)
    reason = vocal_reason or instrumental_reason or "未审核后端不能分开路由"
    return _idle(vocal, instrumental, reason)


def voice_separate_binding(profile, mode):
    """Stage parameters for one Separate call. HP2 keeps the historical dict."""
    if profile is None:
        return _single_pass(mode)
    checked = validate_separator_profile(profile)
    if checked["mode"] == "hp2":
        return _single_pass(mode)
    route = plan_separator_routes(KIM_VOCAL_2, INST_HQ_3)
    return {
        "execution": "split" if route.get("available") is True else None,
        "backend": "dual",
        "parameters": {"mode": mode, "separator_profile": checked},
        "route": route,
        "quality_acceptance": "NOT_ASSESSED",
    }


def refuse_unexecuted_route(binding):
    """Fail closed when a caller tries to treat a plan as a completed inference."""
    route = binding.get("route") if isinstance(binding, dict) else None
    if not isinstance(route, dict):
        raise RuntimeError("双模型分离未执行。不会改跑 HP2，也不会把 HP2 的人声或伴奏写成新模型输出。")
    if route.get("available") is True and route.get("execution") == "split":
        raise RuntimeError("双模型路线可调用，但本次没有执行推理。调用 separate_dual_audio；不会改跑 HP2。")
    reason = route.get("reason") or "双模型分离未执行。不会改跑 HP2。"
    raise RuntimeError(reason)


def same_source_ab_status(weight_dir=None):
    """A comparison exists only after both routes have written audio. This does not."""
    route = plan_separator_routes(KIM_VOCAL_2, INST_HQ_3, weight_dir=weight_dir)
    return {
        "status": EXTERNAL,
        "ran": False,
        "peaks": None,
        "source_id": None,
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "weight_present": dict(route["weight_present"]),
        "loader_implemented": route["loader_implemented"],
        "reason": route["reason"],
    }


def _single_pass(mode):
    return {
        "execution": "single_pass",
        "backend": HP2_BACKEND,
        "parameters": {"model": "four-stem-addon-or-HP2", "mode": mode},
        "quality_acceptance": "NOT_ASSESSED",
    }


def _hp2_plan():
    model = hp2_model()
    return _finish({
        "schema_version": SCHEMA_VERSION,
        "status": "PLANNED",
        "vocal_backend": HP2_BACKEND,
        "instrumental_backend": HP2_BACKEND,
        "execution": "single_pass",
        "applied_by_job": False,
        "reason": "人声和伴奏仍来自同一次 HP2",
        "executed": False,
        "vocal_model": model,
        "instrumental_model": copy.deepcopy(model),
        "kept": {"vocal": HP2_BACKEND, "instrumental": HP2_BACKEND},
    })


def _dual_plan(weight_dir):
    directory = default_weight_dir() if weight_dir is None else weight_dir
    vocal_file = weight_present(directory, KIM_FILE)
    instrumental_file = weight_present(directory, INST_FILE)
    loader = mdx_loader_implemented()
    missing = []
    if not vocal_file:
        missing.append(KIM_FILE)
    if not instrumental_file:
        missing.append(INST_FILE)
    parts = []
    if missing:
        parts.append("缺少权重：" + "、".join(missing))
    if not loader:
        parts.append("0618 worker 没有 MDX loader")
    metadata_ok = False
    metadata_reason = ""
    if vocal_file and instrumental_file and loader:
        try:
            from core.mdx_separator import load_model_metadata
            vocal_metadata = load_model_metadata(os.path.join(directory, KIM_FILE))
            instrumental_metadata = load_model_metadata(os.path.join(directory, INST_FILE))
            if vocal_metadata["primary_stem"] != "Vocals":
                raise ValueError("Kim Vocal 2 metadata does not select Vocals")
            if instrumental_metadata["primary_stem"] != "Instrumental":
                raise ValueError("Inst HQ 3 metadata does not select Instrumental")
            metadata_ok = True
        except (OSError, ValueError, RuntimeError) as exc:
            metadata_reason = "MDX metadata 无法验证：" + str(exc)
    if metadata_reason:
        parts.append(metadata_reason)
    can_run = bool(vocal_file and instrumental_file and loader and metadata_ok)
    if can_run:
        status = "PLANNED"
        execution = "split"
        reason = "Kim Vocal 2 与 UVR-MDX-NET Inst HQ 3 的权重和 MDX loader 都已验证，可执行 split。此计划没有运行音频推理，质量验收未做。"
    else:
        if not parts:
            parts.append("没有可调用的双模型推理")
        status = EXTERNAL
        execution = None
        reason = "双模型分离未执行。" + "；".join(parts) + "。不会改跑 HP2，也不会把 HP2 的人声或伴奏写成新模型输出。待外部验收。"
    return _finish({
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "vocal_backend": KIM_VOCAL_2,
        "instrumental_backend": INST_HQ_3,
        "execution": execution,
        "applied_by_job": False,
        "reason": reason,
        "available": can_run,
        "executed": False,
        "vocal_model": kim_vocal_model(),
        "instrumental_model": inst_hq_model(),
        "kept": {"vocal": KIM_VOCAL_2, "instrumental": INST_HQ_3},
        "weight_present": {KIM_VOCAL_2: vocal_file, INST_HQ_3: instrumental_file},
        "loader_implemented": loader,
    })


def _reject_reason(backend):
    try:
        resolved = resolve_separator_request(backend)
    except ValueError as exc:
        return str(exc)
    if resolved["backend"] != HP2_BACKEND:
        return "未审核后端不能分开路由"
    return ""


def _idle(vocal, instrumental, reason):
    return _finish({
        "schema_version": SCHEMA_VERSION,
        "status": "NOT_RUN",
        "vocal_backend": vocal,
        "instrumental_backend": instrumental,
        "execution": None,
        "applied_by_job": False,
        "reason": reason,
        "executed": False,
    })


def _finish(record):
    record["listening_quality"] = "not_assessed"
    record["quality_acceptance"] = "NOT_ASSESSED"
    return record
