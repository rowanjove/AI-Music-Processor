"""Run a capped, one-axis search. Nothing here becomes a model default."""
import hashlib
import time
import copy

from core.rvc_backend import convert_modern
from core.storage import canonical_json

SCHEMA_VERSION = 1
BUDGETS = {"fast": 1, "balanced": 3, "quality": 6}
PITCH_OFFSETS = (0, -1, 1, -2, 2)
MAX_EXPERT = 6


def budget_limit(mode, expert_limit=None):
    if mode == "expert":
        if type(expert_limit) is not int or not 1 <= expert_limit <= MAX_EXPERT:
            raise ValueError("expert 预算必须是 1 到 %d 的整数" % MAX_EXPERT)
        return expert_limit
    if mode not in BUDGETS:
        raise ValueError("未知搜索预算")
    return BUDGETS[mode]


def f0_search_status(root=None):
    """Ask the modern backend about RMVPE. This does not run harvest."""
    refused = convert_modern("rmvpe", root=root)
    return {
        "axis": "f0",
        "requested_f0": refused["requested_f0"],
        "actual_f0": refused["actual_f0"],
        "executed": False,
        "inference_status": refused["inference_status"],
        "fallback_reason": refused["fallback_reason"],
    }


def pitch_candidates(baseline_pitch, limit, f0_method=None, effective_f0=None):
    """Baseline first, then the nearest unused offsets. Order is fixed."""
    if type(baseline_pitch) is not int or not -24 <= baseline_pitch <= 24:
        raise ValueError("基线音高必须是 -24 到 24 的整数")
    if type(limit) is not int or limit < 1:
        raise ValueError("搜索次数至少为 1")
    if f0_method is None and isinstance(effective_f0, dict):
        f0_method = effective_f0.get("executed")
    if f0_method is not None and f0_method not in ("harvest", "pm", "crepe", "rmvpe"):
        raise ValueError("搜索 F0 必须沿用冻结任务方法")
    if f0_method is None:
        raise ValueError("搜索需要冻结的有效 F0 方法，不能猜测默认值")
    frozen_f0 = copy.deepcopy(effective_f0) if isinstance(effective_f0, dict) else None
    rows = []
    for offset in PITCH_OFFSETS:
        pitch = baseline_pitch + offset
        if not -24 <= pitch <= 24:
            continue
        rows.append({
            "axis": "baseline" if offset == 0 else "pitch_shift",
            "pitch_shift": pitch,
            "index_rate_unchanged": True,
            "protect_unchanged": True,
            "rms_unchanged": True,
            "filter_radius_unchanged": True,
            "f0_method": f0_method,
            "effective_f0": copy.deepcopy(frozen_f0),
        })
        if len(rows) >= limit:
            break
    return rows


def run_budgeted_search(candidates, infer, max_gpu_seconds, clock=None):
    """Call each inference with its remaining wall budget. Never promote."""
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("搜索没有候选")
    if not callable(infer):
        raise ValueError("搜索需要显式的推理函数")
    if type(max_gpu_seconds) is bool or type(max_gpu_seconds) not in (int, float) or max_gpu_seconds <= 0:
        raise ValueError("max_gpu_seconds 必须是正数")
    now = time.monotonic if clock is None else clock
    started = now()
    results = []
    stopped = None
    attempted_count = 0
    executed_count = 0
    for index, candidate in enumerate(candidates):
        elapsed_before = max(0.0, now() - started)
        remaining = float(max_gpu_seconds) - elapsed_before
        if remaining <= 0:
            stopped = "达到 max_gpu_seconds，剩余候选未推理"
            break
        parameter_hash = hashlib.sha256(canonical_json(candidate).encode("utf-8")).hexdigest()
        attempted_count += 1
        attempt_started = now()
        try:
            output = infer(dict(candidate), remaining)
            completed = not (isinstance(output, dict) and output.get("failed") is True)
            result = {
                "parameters": dict(candidate),
                "candidate_parameter_sha256": parameter_hash,
                "output": output,
                "attempted": True,
                "executed": completed,
                "completed": completed,
                "elapsed_seconds": max(0.0, now() - attempt_started),
            }
            results.append(result)
            if completed:
                executed_count += 1
            if isinstance(output, dict) and output.get("stop_search") is True:
                stopped = str(output.get("stop_reason") or "候选执行失败，停止后续推理")
                break
        except TimeoutError as exc:
            results.append({
                "parameters": dict(candidate),
                "candidate_parameter_sha256": parameter_hash,
                "output": {"failed": True, "error": type(exc).__name__ + ": " + str(exc)},
                "attempted": True,
                "executed": False,
                "completed": False,
                "elapsed_seconds": max(0.0, now() - attempt_started),
            })
            stopped = "单次候选推理达到剩余 GPU 预算，已终止 worker 并停止搜索"
            break
        if now() - started >= max_gpu_seconds:
            stopped = "达到 max_gpu_seconds，剩余候选未推理"
            break
    return {
        "schema_version": SCHEMA_VERSION,
        "executed_count": executed_count,
        "attempted_count": attempted_count,
        "planned_count": len(candidates),
        "results": results,
        "actual_elapsed_seconds": max(0.0, now() - started),
        "promoted": False,
        "selected_for_job": "baseline",
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "heuristic_only": True,
        "stopped_reason": stopped,
        "stop_reason": stopped,
    }
