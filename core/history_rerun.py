"""Safe helpers for creating new jobs from frozen history snapshots."""

from __future__ import annotations

import copy
import os
import re
import uuid
from datetime import datetime

from core.storage import CorruptJobError, sha256_path


_ALLOWED_MODES = {"polish", "voice_conversion", "transcribe"}


def clone_history_spec(job, stages=()):
    """Validate and copy a history JobSpec without consulting current GUI settings."""
    if not isinstance(job, dict) or job.get("snapshot_error"):
        raise CorruptJobError("历史任务参数快照损坏，不能安全新建重跑")
    mode = job.get("mode")
    if mode not in _ALLOWED_MODES:
        raise CorruptJobError("历史任务模式无效，不能新建重跑")
    spec = job.get("parameters")
    if not isinstance(spec, dict):
        raise CorruptJobError("历史任务缺少完整 FrozenSpec，不能安全新建重跑")

    from core.job_spec import digest, resolve_mix_reverb, validate

    # Validate the original before any compatibility normalization. This also
    # verifies model snapshots and all frozen parameter identities.
    spec = validate(copy.deepcopy(spec), mode)
    if mode == "voice_conversion" and "mix_reverb" not in spec:
        mix = next((stage for stage in stages
                    if isinstance(stage, dict) and stage.get("stage_name") == "Mix"), None)
        actual = (mix or {}).get("parameters")
        if isinstance(actual, dict) and "reverb" in actual:
            # A real historical Mix record is authoritative. Preserve it only
            # in the clone; never rewrite the original job's stored snapshot.
            spec["mix_reverb"] = resolve_mix_reverb(spec, actual)
            spec["spec_hash"] = digest(spec)
            spec = validate(spec, mode)
    return spec


def _project_title(source_path):
    raw = os.path.splitext(os.path.basename(os.fspath(source_path)))[0]
    title = re.sub(r"_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", "", raw, flags=re.I)
    title = re.sub(r"_[0-9a-f]{8}$", "", title, flags=re.I)
    title = re.sub(r"[^A-Za-z0-9._\-\u3400-\u9fff]+", "_", title).strip("._-")
    return title or "AI_Music"


def create_history_rerun_job(storage, source_path, mode, frozen_spec, *, output_root=None,
                             original_project_dir=None, now=None):
    """Create an empty, independent job/project using a validated frozen spec."""
    source_path = os.path.abspath(os.fspath(source_path))
    if not os.path.isfile(source_path):
        raise FileNotFoundError(source_path)
    if mode not in _ALLOWED_MODES:
        raise CorruptJobError("新建重跑的任务模式无效")

    from core.job_spec import validate

    spec = validate(copy.deepcopy(frozen_spec), mode)
    source_hash = sha256_path(source_path)
    if output_root is None:
        if original_project_dir:
            output_root = os.path.dirname(os.path.abspath(os.fspath(original_project_dir)))
        else:
            from core.config import OUTPUT_DIR
            output_root = OUTPUT_DIR
    output_root = os.path.abspath(os.fspath(output_root))
    os.makedirs(output_root, exist_ok=True)

    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    prefix = f"{_project_title(source_path)}_{stamp}_rerun_"
    project_dir = None
    for _ in range(20):
        candidate = os.path.join(output_root, prefix + uuid.uuid4().hex[:8])
        try:
            os.mkdir(candidate)
            project_dir = candidate
            break
        except FileExistsError:
            continue
    if project_dir is None:
        raise FileExistsError("无法为历史重跑分配独立项目目录")

    try:
        job_id = storage.create_job(source_path, mode, spec,
                                    source_hash=source_hash, project_dir=project_dir)
        created = storage.get_job(job_id)
        if not created or created.get("parameters") != spec or created.get("source_hash") != source_hash:
            raise RuntimeError("新历史重跑任务未能完整保存冻结参数或源文件 hash")
        if storage.list_stages(job_id):
            raise RuntimeError("新历史重跑任务意外包含旧 Stage 记录")
        return {"job_id": job_id, "mode": mode, "source_path": source_path,
                "source_hash": source_hash, "project_dir": project_dir,
                "parameters": spec}
    except Exception:
        try:
            os.rmdir(project_dir)
        except OSError:
            pass
        raise


def resolve_queue_item(item, defaults=None, storage=None):
    """Resolve immutable per-item mode/spec for preflight and execution."""
    if not isinstance(item, dict):
        raise ValueError("队列项目格式无效")
    defaults = defaults if isinstance(defaults, dict) else {}
    spec = item.get("frozen_spec")
    mode = item.get("source_mode")
    if item.get("resume") and not isinstance(spec, dict):
        if storage is None:
            raise ValueError("恢复任务缺少 JobStore")
        job = storage.get_job(item["resume"])
        if not job:
            raise CorruptJobError("找不到待恢复的历史任务")
        spec = job.get("parameters")
        mode = job.get("mode")
        item["frozen_spec"] = copy.deepcopy(spec)
        item["source_mode"] = mode
    params = item.get("params") if isinstance(item.get("params"), dict) else defaults
    mode = mode or (spec and item.get("source_mode")) or params.get("mode") or defaults.get("mode")
    if mode not in _ALLOWED_MODES:
        raise ValueError("队列项目缺少有效的处理模式")
    if isinstance(spec, dict):
        from core.job_spec import validate
        spec = validate(copy.deepcopy(spec), mode)
        item["frozen_spec"] = copy.deepcopy(spec)
        item["source_mode"] = mode
        return mode, spec, True
    params = copy.deepcopy(params)
    params["mode"] = mode
    item["source_mode"] = mode
    if not isinstance(item.get("params"), dict):
        item["params"] = copy.deepcopy(params)
    return mode, params, False


def preflight_queue_items(items, defaults, registry, *, storage=None, resolved=None):
    """Return the first blocked per-item capability decision, or None."""
    from core.ux_flow import preflight_decision, route_capability_report

    for _key, item in items:
        item_options = resolved.get(_key) if isinstance(resolved, dict) else None
        if item_options is None:
            mode, params, _is_frozen = resolve_queue_item(item, defaults, storage)
        else:
            mode, params, _is_frozen = item_options
        profile = params.get("separator_profile")
        report = route_capability_report(registry, mode, profile)
        decision = preflight_decision(report, mode)
        if not decision.get("allowed"):
            return {"mode": mode, "decision": decision}
    return None
