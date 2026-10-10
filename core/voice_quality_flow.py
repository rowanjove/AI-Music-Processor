"""Record a voice-quality plan and, only when asked, retry once.

The default job keeps the first vocal. Clips, the heuristic, and one-axis
candidates are written beside that file. A second inference needs both
quality_assist and quality_retry_limit 1, plus a mechanical measurement.
The first file stays on disk.
"""
import json
import os
import hashlib

from core.storage import canonical_json, sha256_file

from core.parameter_search import clip_timecodes, one_axis_candidates
from core.quality_retry import resolve_quality_retry_limit, run_quality_attempts
from core.representative_clips import select_clips

HEADING = "## 音质流程"


def validate_quality_assist(value):
    if type(value) is not bool:
        raise ValueError("quality_assist 只能是布尔值")
    return value


def resolve_quality_assist(parameters):
    """Missing means the historical path: record a plan, do not retry."""
    if not isinstance(parameters, dict) or "quality_assist" not in parameters:
        return False
    return validate_quality_assist(parameters["quality_assist"])


def retry_enabled(parameters):
    return resolve_quality_assist(parameters) and resolve_quality_retry_limit(parameters) == 1


def mechanical_reasons(artifact_record, f0_record):
    """Measurements that may justify one alternate. Not a listening score."""
    reasons = []
    if isinstance(artifact_record, dict):
        ratio = artifact_record.get("clip_ratio")
        if type(ratio) in (int, float) and not isinstance(ratio, bool) and ratio > 0:
            reasons.append("启发式削波比例大于 0")
    if isinstance(f0_record, dict) and f0_record.get("status") == "F0_UNSTABLE":
        reasons.append("提供的 F0 轨迹不稳定")
    return reasons


def read_clip_source(path):
    """Read one WAV for clip selection. A miss does not fail the job."""
    try:
        import soundfile as sf
        audio, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    except (OSError, RuntimeError, ValueError):
        return None, None
    return audio, int(sample_rate)


def clip_summary(samples, sample_rate):
    selected = select_clips(samples, sample_rate)
    if selected is samples:
        return {"kind": "whole", "count": 1, "timecodes": []}
    edges = []
    for item in selected:
        edges.append({
            "start_sample": item["start_sample"],
            "end_sample": item["end_sample"],
            "start": item["start_timecode"],
            "end": item["end_timecode"],
        })
    log = clip_timecodes(selected)
    log["edges"] = edges
    return log


def apply_voice_quality(parameters, job, profile, primary_path, artifact_record, f0_record,
                        infer=None, samples=None, sample_rate=None, suggestion=None):
    """Plan the next vocal. infer is called only for one opted-in alternate."""
    if not isinstance(job, dict) or not isinstance(profile, dict):
        raise ValueError("音质流程需要任务参数和 Profile")
    assist = resolve_quality_assist(parameters)
    limit = resolve_quality_retry_limit(parameters)
    has_frozen_f0 = _has_frozen_job_f0(job)
    if assist and not has_frozen_f0:
        raise ValueError("开启音质辅助需要 JobSpec 冻结的 F0 方法")
    if samples is None or sample_rate is None:
        clips = {"kind": "unread", "timecodes": [], "count": 0}
    else:
        clips = clip_summary(samples, sample_rate)
    candidates = one_axis_candidates(job, profile, suggestion) if has_frozen_f0 else []
    alternate = next((row for row in candidates if row["axis"] != "baseline"), None)
    reasons = mechanical_reasons(artifact_record, f0_record)
    record = {
        "quality_truth": False,
        "listening_quality": "not_assessed",
        "blocks_job": False,
        "quality_assist": assist,
        "quality_retry_limit": limit,
        "clips": clips,
        "candidates": [_public_row(row) for row in candidates],
        "candidate_plan_status": "READY" if has_frozen_f0 else "INCOMPLETE",
        "candidate_plan_reason": None if has_frozen_f0 else "JobSpec 缺少冻结 F0 方法；未生成搜索候选",
        "artifact_status": artifact_record.get("reason") if isinstance(artifact_record, dict) else None,
        "f0_status": f0_record.get("status") if isinstance(f0_record, dict) else None,
        "rollback_path": primary_path,
        "kept_path": primary_path,
        "extra_inference_count": 0,
        "executed": False,
        "stopped": False,
        "reversible": True,
        "reason": _idle_reason(assist, limit),
        "parameter_identity": _parameter_identity(parameters),
    }
    if not retry_enabled(parameters) or not reasons or alternate is None:
        if retry_enabled(parameters) and not reasons:
            record["reason"] = "已开启，启发式没有机械重试条件，未重试"
        elif retry_enabled(parameters) and alternate is None:
            record["reason"] = "已开启，没有可替换的单轴候选，未重试"
        record["stopped"] = True
        return record
    if not callable(infer):
        raise ValueError("开启音质重试时需要推理函数")
    primary = _attempt_row(candidates[0], 1)
    retry = _attempt_row(alternate, 2)

    def wrapped(payload):
        if payload.get("attempt") == 1 or payload.get("axis") == "baseline":
            return {"failed": True, "artifact": primary_path}
        try:
            produced = infer(payload)
        except Exception as exc:
            return {"failed": True, "artifact": primary_path, "error": type(exc).__name__}
        if not isinstance(produced, dict) or produced.get("failed") is not False:
            error = produced.get("error") if isinstance(produced, dict) else "推理没有返回产物"
            return {"failed": True, "artifact": primary_path, "error": error}
        return produced

    result = run_quality_attempts(primary, retry, wrapped, 1)
    kept = result["kept"] if isinstance(result["kept"], str) else primary_path
    if not os.path.isfile(kept):
        kept = primary_path
    record["executed"] = True
    record["stopped"] = True
    record["extra_inference_count"] = max(0, result["inference_count"] - 1)
    record["kept_path"] = kept
    record["rollback_path"] = primary_path
    record["reason"] = result["reason"] + "；触发：" + "、".join(reasons)
    return record


def completed_choice(primary_path, parameters=None):
    """Reuse one finished retry only while the same opt-in is still on."""
    path = primary_path + ".quality-flow.json"
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("executed") is not True or payload.get("stopped") is not True:
        return None
    if not retry_enabled(payload if parameters is None else parameters):
        return None
    if parameters is not None and payload.get("parameter_identity") != _parameter_identity(parameters):
        return None
    kept = payload.get("kept_path")
    if not isinstance(kept, str) or not os.path.isfile(kept):
        return None
    try:
        if payload.get("primary_sha256") != sha256_file(primary_path) or payload.get("kept_sha256") != sha256_file(kept):
            return None
    except OSError:
        return None
    return payload


def write_quality_sidecars(primary_path, record):
    record["primary_sha256"] = sha256_file(primary_path)
    record["kept_sha256"] = sha256_file(record.get("kept_path") or primary_path)
    _write_json(primary_path + ".quality-flow.json", record)
    _write_json(primary_path + ".clips.json", record.get("clips") or {})
    return record


def format_quality_report(record):
    record = record if isinstance(record, dict) else {}
    lines = [
        HEADING,
        "",
        "- 音质真值：否",
        "- 听感：not_assessed",
        "- 不改变任务成败：是" if record.get("blocks_job") is False else "- 不改变任务成败：否",
        "- 音质辅助：%s" % ("开" if record.get("quality_assist") is True else "关"),
        "- 重试上限：%s" % record.get("quality_retry_limit"),
        "- 额外推理：%s" % record.get("extra_inference_count"),
        "- 已执行重试：%s" % ("是" if record.get("executed") is True else "否"),
        "- 保留：%s" % os.path.basename(str(record.get("kept_path") or "")),
        "- 可回退到：%s" % os.path.basename(str(record.get("rollback_path") or "")),
        "- 原因：%s" % record.get("reason", ""),
    ]
    return "\n".join(lines) + "\n"


def append_quality_report(report_path, record):
    """Append the plan section once."""
    with open(report_path, encoding="utf-8") as handle:
        current = handle.read()
    if HEADING in current:
        return
    section = format_quality_report(record)
    with open(report_path, "a", encoding="utf-8") as handle:
        if current and not current.endswith("\n"):
            handle.write("\n")
        handle.write("\n")
        handle.write(section)


def _idle_reason(assist, limit):
    if assist and limit != 1:
        return "音质辅助已开，但重试上限不是 1，未重试"
    if limit == 1 and not assist:
        return "重试上限为 1，但音质辅助关闭，未重试"
    return "默认关闭，保留第一次产物"


def _public_row(row):
    return {
        "axis": row["axis"],
        "pitch_shift": row["pitch_shift"],
        "index_rate": row["index_rate"],
        "protect": row["protect"],
    }


def _attempt_row(row, attempt):
    payload = _public_row(row)
    payload["attempt"] = attempt
    return payload


def _write_json(path, payload):
    from core.pipeline_support import _json_file
    _json_file(path, payload)


def _parameter_identity(parameters):
    """Bind retry selection to the frozen job, including its model profile."""
    return hashlib.sha256(canonical_json(parameters).encode("utf-8")).hexdigest()


def _has_frozen_job_f0(job):
    if not isinstance(job, dict):
        return False
    effective_f0 = job.get("effective_f0")
    if isinstance(effective_f0, dict) and effective_f0.get("executed") in ("harvest", "pm", "crepe", "rmvpe"):
        return True
    return job.get("f0_method") in ("harvest", "pm", "crepe", "rmvpe")
