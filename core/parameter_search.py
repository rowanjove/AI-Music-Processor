"""Explicit one-axis candidates. The pipeline does not call this module.

Baseline pitch and index_rate come from the job. Profile index_rate is only
a second candidate when it differs, so a job index is not replaced. Pitch
candidates come from suggest_pitch and stay unapplied. Protect stays at the
single VCQ-008 profile value on every row.
"""
import copy
import hashlib
import math

from core.pitch_suggestion import suggest_pitch
from core.storage import canonical_json


def one_axis_candidates(job, profile, suggestion=None, model=None, source_gender=None):
    """Return the baseline, then rows that change exactly one axis."""
    if not isinstance(job, dict) or not isinstance(profile, dict):
        raise ValueError("搜索需要任务参数和 Profile")
    baseline = {
        "pitch_shift": _pitch(job, profile),
        "index_rate": _job_index(job, profile),
        "protect": _protect(profile),
        "axis": "baseline",
    }
    effective_f0 = job.get("effective_f0")
    method = effective_f0.get("executed") if isinstance(effective_f0, dict) else None
    if method is None:
        method = job.get("f0_method")
    if method is None:
        method = profile.get("f0_method")
    if method not in ("harvest", "pm", "crepe", "rmvpe"):
        raise ValueError("参数搜索需要 JobSpec 冻结的 F0 方法")
    baseline["f0_method"] = method
    baseline["effective_f0"] = copy.deepcopy(effective_f0) if isinstance(effective_f0, dict) else None
    rows = [baseline]
    hint = suggestion
    if hint is None and model is not None:
        hint = suggest_pitch(model, source_gender)
    if isinstance(hint, dict) and hint.get("applied") is False and type(hint.get("semitones")) is int:
        if hint["semitones"] != baseline["pitch_shift"]:
            rows.append(_replace(baseline, "pitch_shift", hint["semitones"]))
    profile_index = profile.get("index_rate")
    if type(profile_index) in (int, float) and not isinstance(profile_index, bool) and math.isfinite(profile_index):
        if profile_index != baseline["index_rate"]:
            rows.append(_replace(baseline, "index_rate", profile_index))
    for row in rows:
        changed = [key for key in ("pitch_shift", "index_rate", "protect") if row[key] != baseline[key]]
        if row["axis"] == "baseline" and changed:
            raise ValueError("基线不能改轴")
        if row["axis"] != "baseline" and changed != [row["axis"]]:
            raise ValueError("每次只能改变一个轴")
    return rows


def explicit_search(job, profile, infer, suggestion=None, model=None, source_gender=None, clips=None):
    """Run infer once per candidate. This is not the default job path."""
    if not callable(infer):
        raise ValueError("搜索需要显式的推理函数")
    candidates = one_axis_candidates(job, profile, suggestion, model, source_gender)
    clip_log = clip_timecodes(clips)
    results = []
    for candidate in candidates:
        parameters = {
            "pitch_shift": candidate["pitch_shift"],
            "index_rate": candidate["index_rate"],
            "protect": candidate["protect"],
            "axis": candidate["axis"],
            "f0_method": candidate["f0_method"],
            "effective_f0": copy.deepcopy(candidate["effective_f0"]),
        }
        parameter_hash = hashlib.sha256(canonical_json(parameters).encode("utf-8")).hexdigest()
        results.append({
            "parameters": parameters,
            "candidate_parameter_sha256": parameter_hash,
            "clips": clip_log,
            "output": infer(parameters),
        })
    return results


def bounded_candidates(job, profile, budget="fast", suggestion=None, model=None, source_gender=None):
    """Build a deterministic, capped candidate list from a frozen job baseline."""
    from core.budgeted_search import budget_limit, pitch_candidates

    limit = budget_limit(budget)
    rows = one_axis_candidates(job, profile, suggestion, model, source_gender)
    baseline = rows[0]
    candidates = list(rows[1:])
    pitch_rows = pitch_candidates(
        baseline["pitch_shift"], 5, f0_method=baseline["f0_method"],
        effective_f0=baseline["effective_f0"],
    )
    for pitch in pitch_rows[1:]:
        row = copy.deepcopy(baseline)
        row.update(pitch_shift=pitch["pitch_shift"], axis="pitch_shift")
        candidates.append(row)
    unique = []
    seen = set()
    for row in candidates:
        identity = hashlib.sha256(canonical_json(row).encode("utf-8")).hexdigest()
        if identity not in seen:
            seen.add(identity)
            unique.append(row)
        if len(unique) >= limit:
            break
    return {"baseline": baseline, "candidates": unique, "planned_count": len(unique), "budget": budget}


def clip_timecodes(clips):
    """Log timecodes only. A whole short buffer has no cut points."""
    if clips is None or not isinstance(clips, list):
        return {"kind": "whole", "timecodes": []}
    timecodes = []
    for item in clips:
        if not isinstance(item, dict):
            continue
        timecodes.append({
            "start": item.get("start_timecode"),
            "end": item.get("end_timecode"),
        })
    return {"kind": "clips", "timecodes": timecodes, "count": len(timecodes)}


def _replace(baseline, axis, value):
    row = copy.deepcopy(baseline)
    row["axis"] = axis
    row[axis] = value
    return row


def _pitch(job, profile):
    value = job.get("pitch_shift", profile.get("pitch", 0))
    if type(value) is not int or not -24 <= value <= 24:
        raise ValueError("pitch_shift 必须是 -24 到 24 的整数")
    return value


def _job_index(job, profile):
    if "index_rate" in job:
        value = job["index_rate"]
    else:
        value = profile.get("index_rate", 0.65)
    return _finite(value, "index_rate")


def _protect(profile):
    return _finite(profile.get("protect", 0.33), "protect")


def _finite(value, name):
    if type(value) is bool or type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(name + " 无效")
    return value
