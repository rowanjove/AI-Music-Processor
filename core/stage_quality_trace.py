"""Index one voice job's existing files and name the first objective change.

This module does not separate, convert, clean, mix, or master audio. It does
not rename the files QC and the cache already use. A metric flag is not a
listening verdict, and a user mark is not a quality acceptance.
"""
import json
import os

from core.analyzer import probe_metadata
from core.qc import sample_metrics
from core.storage import sha256_file

SCHEMA_VERSION = 1
HEADING = "## 阶段质量追踪"
SILENCE_RMS = 1e-5
AUDIBLE_RMS = 1e-4
DURATION_TOLERANCE_SECONDS = 0.5
HF_ABSOLUTE_DELTA = 0.20
HF_GROWTH = 2.0
HF_PREVIOUS_FLOOR = 0.01
SPECTRAL_PREFIX_SECONDS = 30.0
LISTENING_VALUES = ("ok", "degraded", "not_assessed")

# Separator reads the standardized wav, not the original file. That extra
# stage stops an ingest change from being blamed on the separator.
VOCAL_CHAIN = (
    "source",
    "separator_input",
    "vocal_separated",
    "vocal_preprocessed",
    "rvc_input",
    "rvc_raw",
    "rvc_post",
    "aligned",
    "mixed",
    "master",
)
REQUIRED = VOCAL_CHAIN[:1] + (
    "vocal_separated",
    "instrumental",
    "vocal_preprocessed",
    "rvc_input",
    "rvc_raw",
    "rvc_post",
    "aligned",
    "mixed",
    "master",
)
HF_PAIRS = (
    ("source", "separator_input"),
    ("vocal_separated", "vocal_preprocessed"),
    ("vocal_preprocessed", "rvc_input"),
    ("rvc_input", "rvc_raw"),
    ("rvc_raw", "rvc_post"),
    ("rvc_post", "aligned"),
    ("mixed", "master"),
)
_UNUSED_DUAL = (
    ("kim_instrumental", "Kim Vocal 未采用伴奏", "kim_instrumental.wav"),
    ("inst_hq_3_vocal", "Inst HQ 3 未采用人声", "insthq3_vocal.wav"),
)
_STAGE_SPECS = (
    ("source", "原曲", "00_source.wav", True),
    ("separator_input", "分离实际输入", "00_separator_input.wav", True),
    ("vocal_separated", "分离后人声", "01_vocal_separated.wav", True),
    ("instrumental", "分离后伴奏", "02_instrumental.wav", False),
    ("vocal_preprocessed", "RVC 前清理人声", "03_vocal_preprocessed.wav", True),
    ("rvc_input", "RVC 实际输入", "04_rvc_input.wav", True),
    ("rvc_raw", "RVC Raw", "05_rvc_raw.wav", True),
    ("rvc_post", "Postprocess", "06_rvc_post.wav", True),
    ("aligned", "对齐后人声", "07_aligned.wav", True),
    ("mixed", "Mix", "08_mixed.wav", True),
    ("master", "Master", "09_master.wav", True),
)
# rule, cause, checks, stages that should not be tuned first
_RULES = {
    "source": ("source", "原曲本身", ["原曲峰值", "削波", "静音", "文件是否损坏"], ["Separator", "RVC", "Clean", "Mix", "Master"]),
    "separator_input": ("ingest", "标准化", ["重采样", "声道", "峰值", "时长"], ["Separator", "RVC"]),
    "vocal_separated": ("A", "Separator", ["分离模型", "人声泄漏", "伴奏残留"], ["RVC"]),
    "instrumental": ("A", "Separator 的伴奏", ["伴奏空洞", "人声残留", "分离模型"], ["RVC"]),
    "vocal_preprocessed": ("B", "Preprocess", ["Clean", "高通", "deesser", "EQ"], ["RVC"]),
    "rvc_input": ("input_gain", "RVC 输入增益", ["rvc_input_gain_db", "峰值", "静音"], ["RVC 模型", "F0"]),
    "rvc_raw": ("C", "RVC", ["RVC 模型", "F0", "Pitch", "Index", "Protect", "RMS", "采样率", "输入音域"], ["Mix", "Master"]),
    "rvc_post": ("postprocess", "Postprocess", ["RVC 后处理", "后处理增益"], ["Separator"]),
    "aligned": ("alignment", "Alignment", ["对齐偏移", "时间拉伸"], ["Separator", "Master"]),
    "mixed": ("D", "Mix", ["Compressor", "Reverb", "相位", "Gain", "Alignment", "伴奏泄漏"], ["Master"]),
    "master": ("E", "Master", ["Limiter", "LUFS", "True Peak", "EQ", "Master preset"], []),
}
_RESOLVED_NOTES = {
    "separator_input": "没有单独的原曲路径，原曲条目指向分离实际输入。",
    "cleaned_vocal": "没有单独的增益文件。RVC 实际输入就是清理后人声。",
    "gain_file": "RVC 实际输入是增益后的文件。",
    "post_file": "Postprocess 是单独写出的文件。",
    "kept_vocal": "没有单独的后处理文件。这一条是音质辅助保留的文件，不是一条新的后处理算法。",
    "rvc_raw": "后处理关闭。对齐直接读 RVC Raw。",
    "source_path": "",
    "standard_wav": "",
    "master_wav": "",
    "master_glob": "",
    "missing": "文件不存在。",
    "ambiguous_rvc": "RVC 输出不是恰好一个，不能确定哪一个是 Raw。",
    "ambiguous_master": "母带 wav 不是恰好一个，不能确定试听文件。",
}


def build_stage_trace(project_dir, source_path=None, master_wav=None):
    """Measure the files already stored for one voice project."""
    project_dir = os.path.abspath(project_dir)
    if not os.path.isdir(project_dir):
        raise ValueError("没有项目目录: " + project_dir)
    resolved = _resolve(project_dir, source_path, master_wav)
    measured = {}
    stages = []
    for spec in _STAGE_SPECS:
        stage_id, label, trace_name, in_chain = spec
        path, resolved_from = resolved[stage_id]
        stage = {
            "id": stage_id,
            "label": label,
            "trace_name": trace_name,
            "in_vocal_chain": in_chain,
            "path": os.path.abspath(path) if path else None,
            "present": bool(path and os.path.isfile(path)),
            "resolved_from": resolved_from,
            "note": _RESOLVED_NOTES.get(resolved_from, ""),
            "same_path_as": None,
        }
        if stage["present"]:
            key = os.path.normcase(stage["path"])
            if key not in measured:
                measured[key] = _measure_file(stage["path"])
            stage.update(measured[key])
            for previous in stages:
                if previous.get("path") and os.path.normcase(previous["path"]) == key:
                    stage["same_path_as"] = previous["id"]
                    break
        else:
            stage.update(_empty_measure())
        stages.append(stage)
    trace = {
        "schema_version": SCHEMA_VERSION,
        "kind": "stage_quality_trace",
        "blocks_job": False,
        "quality_truth": False,
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "project_dir": project_dir,
        "copies_audio": False,
        "stages": stages,
    }
    trace["diagnosis"] = diagnose_stages(stages)
    extras = _unused_dual_audition(project_dir)
    if extras:
        trace["extra_audition"] = extras
    return trace


def diagnose_stages(stages, listening_labels=None):
    """Apply the stage rules. Labels are optional and never mean acceptance."""
    stages = _stage_list(stages)
    objective = _objective_diagnosis(stages)
    if listening_labels is None:
        return objective
    listening = _listening_diagnosis(stages, listening_labels)
    listening["flags"] = objective["flags"]
    listening["parallel_findings"] = objective["parallel_findings"]
    listening["missing_stages"] = objective["missing_stages"]
    listening["objective_status"] = objective["status"]
    listening["objective_first_flagged_stage"] = objective["first_flagged_stage"]
    listening["objective_rule"] = objective["rule"]
    listening["objective_conclusion"] = objective["conclusion"]
    return listening


def project_dir_from_manifest(manifest):
    """Find the project from the output paths a finished job already records."""
    if not isinstance(manifest, dict):
        return None
    outputs = manifest.get("output_paths") or {}
    stems = outputs.get("stems_dir")
    if isinstance(stems, str) and stems:
        return os.path.dirname(os.path.abspath(stems))
    master = outputs.get("master_wav")
    if isinstance(master, str) and master:
        return os.path.dirname(os.path.dirname(os.path.abspath(master)))
    return None


def audition_rows(trace):
    """Paths a person can open. Missing stages stay in the list."""
    load_trace_data(trace)
    rows = []
    for stage in trace["stages"]:
        rows.append({
            "id": stage["id"],
            "label": stage["label"],
            "trace_name": stage["trace_name"],
            "path": stage.get("path"),
            "present": stage.get("present") is True and bool(stage.get("path")) and os.path.isfile(stage["path"]),
            "note": stage.get("note") or "",
            "official": True,
        })
    for extra in trace.get("extra_audition") or []:
        path = extra.get("path")
        rows.append({
            "id": extra["id"],
            "label": extra["label"],
            "trace_name": extra["trace_name"],
            "path": path,
            "present": bool(path) and os.path.isfile(path),
            "note": extra.get("note") or "双模型未采用的输出",
            "official": False,
        })
    return rows


def append_stage_trace(report_path, project_dir, source_path=None, master_wav=None):
    """Write the JSON and one report section. This does not decide QC."""
    trace = build_stage_trace(project_dir, source_path=source_path, master_wav=master_wav)
    reports = os.path.join(os.path.abspath(project_dir), "reports")
    os.makedirs(reports, exist_ok=True)
    dump_trace(trace, os.path.join(reports, "stage_quality_trace.json"))
    if report_path and os.path.isfile(report_path):
        _append_report(report_path, trace)
    return trace


def write_listening_marks(project_dir, labels, stages):
    """Store marks a person made. The file is not a passed listening review."""
    diagnosis = diagnose_stages(stages, labels)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": "stage_quality_listening",
        "listening_quality": "user_marked",
        "quality_acceptance": "NOT_ASSESSED",
        "blocks_job": False,
        "labels": {key: labels[key] for key in labels},
        "diagnosis": diagnosis,
    }
    reports = os.path.join(os.path.abspath(project_dir), "reports")
    os.makedirs(reports, exist_ok=True)
    path = os.path.join(reports, "stage_quality_listening.json")
    _dump(payload, path)
    return payload


def format_stage_trace_report(trace):
    load_trace_data(trace)
    diagnosis = trace["diagnosis"]
    lines = [
        HEADING,
        "",
        "- 说明：索引现有文件。客观异常不是听审，也不改变任务成败。",
        "- 不复制音频：是",
        "- 听感：%s" % trace.get("listening_quality", "not_assessed"),
        "- 音质验收：%s" % trace.get("quality_acceptance", "NOT_ASSESSED"),
        "- 依据：%s" % diagnosis.get("basis"),
        "- 状态：%s" % diagnosis.get("status"),
        "- 最早异常阶段：%s" % (diagnosis.get("first_flagged_stage") or "无"),
        "- 规则：%s" % (diagnosis.get("rule") or "无"),
        "- 可能原因：%s" % (diagnosis.get("likely_cause") or "无"),
        "- 结论：%s" % diagnosis.get("conclusion", ""),
    ]
    for item in diagnosis.get("observations") or []:
        lines.append("- 观察：%s" % item.get("detail"))
    lines.extend(["", "试听文件："])
    for stage in trace["stages"]:
        location = stage.get("path") if stage.get("present") else "缺失"
        alias = "，与 %s 同一文件" % stage["same_path_as"] if stage.get("same_path_as") else ""
        lines.append("- %s（%s）：%s%s" % (stage["label"], stage["trace_name"], location, alias))
    return "\n".join(lines) + "\n"


def dump_trace(trace, path):
    load_trace_data(trace)
    _dump(trace, path)


def load_trace(path):
    with open(path, "r", encoding="utf-8") as handle:
        return load_trace_data(json.load(handle))


def load_trace_data(record):
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("阶段质量追踪版本不符")
    if record.get("kind") != "stage_quality_trace":
        raise ValueError("不是阶段质量追踪")
    stages = record.get("stages")
    if not isinstance(stages, list) or [item.get("id") for item in stages] != [spec[0] for spec in _STAGE_SPECS]:
        raise ValueError("阶段质量追踪的阶段不完整")
    extras = record.get("extra_audition", [])
    official = {spec[0] for spec in _STAGE_SPECS}
    if not isinstance(extras, list):
        raise ValueError("未采用分离输出记录无效")
    for item in extras:
        if not isinstance(item, dict) or not item.get("id") or item.get("id") in official:
            raise ValueError("未采用分离输出记录无效")
    diagnosis = record.get("diagnosis")
    if not isinstance(diagnosis, dict) or diagnosis.get("quality_acceptance") != "NOT_ASSESSED":
        raise ValueError("阶段质量追踪不能写成已验收")
    if diagnosis.get("blocks_job") is not False:
        raise ValueError("阶段质量追踪不能改变任务成败")
    return record


def _unused_dual_audition(project_dir):
    """Open stems a dual run kept beside the QC names. Missing files stay out."""
    stems = os.path.join(project_dir, "stems")
    rows = []
    for stage_id, label, filename in _UNUSED_DUAL:
        path = os.path.join(stems, filename)
        if os.path.isfile(path):
            rows.append({
                "id": stage_id,
                "label": label,
                "trace_name": filename,
                "path": os.path.abspath(path),
                "note": "双模型未采用的输出",
            })
    return rows


def _resolve(project_dir, source_path, master_wav):
    stems = os.path.join(project_dir, "stems")
    work = os.path.join(project_dir, ".work")
    standard = os.path.join(work, "01_standard_input.wav")
    cleaned = os.path.join(stems, "02_vocal_cleaned.wav")
    vocal = os.path.join(stems, "01_vocal_original.wav")
    instrumental = os.path.join(stems, "04_instrumental.wav")
    aligned = os.path.join(stems, "03_vocal_aligned.wav")
    mixed = os.path.join(work, "06_mixed.wav")
    gain = os.path.join(work, "05_rvc_input.wav")
    post = os.path.join(work, "07_rvc_post.wav")
    rvc_paths = []
    if os.path.isdir(stems):
        rvc_paths = sorted(
            os.path.join(stems, name)
            for name in os.listdir(stems)
            if name.startswith("03_vocal_rvc_") and name.lower().endswith(".wav")
        )
    if len(rvc_paths) == 1:
        rvc_raw, rvc_from = rvc_paths[0], "stem"
    elif len(rvc_paths) > 1:
        rvc_raw, rvc_from = None, "ambiguous_rvc"
    else:
        rvc_raw, rvc_from = None, "missing"
    if source_path and os.path.isfile(source_path):
        source, source_from = source_path, "source_path"
    elif os.path.isfile(standard):
        source, source_from = standard, "separator_input"
    else:
        source, source_from = None, "missing"
    separator = (standard, "standard_wav") if os.path.isfile(standard) else (None, "missing")
    if os.path.isfile(gain):
        rvc_input = (gain, "gain_file")
    elif os.path.isfile(cleaned):
        rvc_input = (cleaned, "cleaned_vocal")
    else:
        rvc_input = (None, "missing")
    kept = _kept_path(rvc_raw)
    if os.path.isfile(post):
        rvc_post = (post, "post_file")
    elif kept and os.path.isfile(kept):
        rvc_post = (kept, "kept_vocal")
    elif rvc_raw and os.path.isfile(rvc_raw):
        rvc_post = (rvc_raw, "rvc_raw")
    else:
        rvc_post = (None, "missing")
    master = _master_path(project_dir, master_wav)
    return {
        "source": (source, source_from),
        "separator_input": separator,
        "vocal_separated": (vocal if os.path.isfile(vocal) else None, "stem" if os.path.isfile(vocal) else "missing"),
        "instrumental": (instrumental if os.path.isfile(instrumental) else None, "stem" if os.path.isfile(instrumental) else "missing"),
        "vocal_preprocessed": (cleaned if os.path.isfile(cleaned) else None, "stem" if os.path.isfile(cleaned) else "missing"),
        "rvc_input": rvc_input,
        "rvc_raw": (rvc_raw, rvc_from),
        "rvc_post": rvc_post,
        "aligned": (aligned if os.path.isfile(aligned) else None, "stem" if os.path.isfile(aligned) else "missing"),
        "mixed": (mixed if os.path.isfile(mixed) else None, "work" if os.path.isfile(mixed) else "missing"),
        "master": master,
    }


def _master_path(project_dir, master_wav):
    if master_wav and os.path.isfile(master_wav):
        return master_wav, "master_wav"
    master_dir = os.path.join(project_dir, "master")
    found = []
    if os.path.isdir(master_dir):
        found = sorted(
            os.path.join(master_dir, name)
            for name in os.listdir(master_dir)
            if name.lower().endswith(".wav")
        )
    if len(found) == 1:
        return found[0], "master_glob"
    if len(found) > 1:
        return None, "ambiguous_master"
    return None, "missing"


def _kept_path(rvc_raw):
    if not rvc_raw:
        return None
    sidecar = rvc_raw + ".quality-flow.json"
    if not os.path.isfile(sidecar):
        return None
    from core.voice_quality_flow import completed_choice
    payload = completed_choice(rvc_raw)
    kept = payload.get("kept_path") if isinstance(payload, dict) else None
    if isinstance(kept, str) and os.path.isfile(kept) and os.path.normcase(os.path.abspath(kept)) != os.path.normcase(os.path.abspath(rvc_raw)):
        return kept
    return None


def _measure_file(path):
    record = _empty_measure()
    try:
        record["bytes"] = os.path.getsize(path)
        record["sha256"] = sha256_file(path)
        metrics = sample_metrics(path)
        meta = probe_metadata(path)
    except (OSError, ValueError, RuntimeError) as exc:
        record["error"] = str(exc)
        return record
    record["readable"] = True
    record["sample_peak"] = metrics["sample_peak"]
    record["clipped_samples"] = metrics["clipped_samples"]
    record["channel_rms"] = list(metrics["channel_rms"])
    record["duration_seconds"] = meta["duration"]
    record["sample_rate"] = meta["sample_rate"]
    record["channels"] = meta["channels"]
    try:
        ratio, scope = _spectral_prefix(path)
    except (OSError, ValueError, RuntimeError) as exc:
        record["spectral_scope"] = "unavailable"
        record["error"] = str(exc)
        return record
    record["high_frequency_ratio"] = ratio
    record["spectral_scope"] = scope
    return record


def _spectral_prefix(path):
    import soundfile as sf
    from core.artifact_score import assess_artifact

    with sf.SoundFile(path) as audio:
        total = len(audio)
        rate = int(audio.samplerate)
        frames = min(total, int(rate * SPECTRAL_PREFIX_SECONDS))
        data = audio.read(frames, dtype="float32", always_2d=False)
    assessed = assess_artifact(data, rate)
    scope = "full_file" if frames == total else "prefix_30s"
    return assessed.get("high_frequency_ratio"), scope


def _empty_measure():
    return {
        "readable": False,
        "sample_peak": None,
        "clipped_samples": None,
        "channel_rms": None,
        "duration_seconds": None,
        "sample_rate": None,
        "channels": None,
        "high_frequency_ratio": None,
        "spectral_scope": None,
        "bytes": None,
        "sha256": None,
        "error": None,
    }


def _objective_diagnosis(stages):
    by_id = {stage["id"]: stage for stage in stages}
    missing = [stage_id for stage_id in REQUIRED if not by_id[stage_id].get("present")]
    flags = _sort_flags(_chain_flags(stages) + _pair_flags(by_id))
    observations = _layout_observations(stages)
    parallel = _instrumental_flags(by_id)
    vocal_flags = [item for item in flags if item["stage_id"] in VOCAL_CHAIN]
    first = vocal_flags[0]["stage_id"] if vocal_flags else None
    if first is None and parallel:
        first = "instrumental"
    missing_before = _missing_before(first, by_id) if first else []
    confirmed = first is not None and not missing_before
    rule = cause = None
    checks = []
    hold = []
    if confirmed:
        rule, cause, checks, hold = _RULES[first]
        status = "OBJECTIVE_FLAG"
    elif first is not None:
        status = "INCOMPLETE"
    elif missing:
        status = "INCOMPLETE"
    else:
        status = "NO_OBJECTIVE_FLAG"
    conclusion = _objective_conclusion(status, first, rule, cause, missing, missing_before, parallel, observations)
    return {
        "basis": "objective_proxy",
        "status": status,
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "blocks_job": False,
        "quality_truth": False,
        "first_flagged_stage": first if confirmed else None,
        "candidate_stage": first,
        "rule": rule,
        "likely_cause": cause,
        "checks": checks,
        "do_not_tune": hold,
        "flags": flags,
        "parallel_findings": parallel,
        "observations": observations,
        "missing_stages": missing,
        "conclusion": conclusion,
    }


def _objective_conclusion(status, first, rule, cause, missing, missing_before, parallel, observations):
    if status == "NO_OBJECTIVE_FLAG":
        text = "客观代理没有发现削波、静音、非有限数值、时长突变或高频能量比跳变。不能据此判断听感从哪一步变差。"
    elif status == "INCOMPLETE" and first:
        text = "已有文件里最早的客观异常在「%s」，但更早的 %s 缺失，不能确认它是第一处。" % (first, "、".join(missing_before))
    elif status == "INCOMPLETE":
        text = "追踪不完整，缺少 %s。不能判断声音从哪一步变差。" % "、".join(missing)
    else:
        text = "客观异常首先出现在「%s」。按规则 %s，优先检查 %s。" % (_label(first), rule, cause)
        if "RVC" in (cause, rule) or first == "vocal_separated":
            text += "这不能区分模型、F0、音高、Index、Protect 或 RMS 中的具体一项。" if first == "rvc_raw" else ""
        if first in ("vocal_separated", "instrumental"):
            text += "不要先调 RVC。"
        text += "这是客观代理，不是听审。"
    if parallel and first != "instrumental":
        text += "伴奏轨另外有客观异常，仍先对人声链的第一处下结论。"
    if observations:
        text += "另外观察到：" + "；".join(item["detail"] for item in observations) + "。这不是听审结论。"
    return text


def _listening_diagnosis(stages, labels):
    by_id = {stage["id"]: stage for stage in stages}
    _validate_labels(labels, by_id)
    first = None
    for stage_id in VOCAL_CHAIN:
        if labels.get(stage_id, "not_assessed") == "degraded":
            first = stage_id
            break
    if first is None and labels.get("instrumental") == "degraded":
        first = "instrumental"
    if first is None:
        return _listening_base(
            "NO_DEGRADED_MARK",
            None,
            None,
            None,
            [],
            [],
            "听审标记里没有变差的阶段。不能指出恶化起点。",
        )
    previous_label = _assessed_previous_label(first, labels, by_id)
    confirmed = previous_label == "ok"
    rule, cause, checks, hold = _RULES[first]
    if not confirmed:
        conclusion = "「%s」被标为变差，但前一阶段不是正常，不能确认分界。音质验收仍是 NOT_ASSESSED。" % _label(first)
        return _listening_base("LISTENING_INCOMPLETE", None, None, None, [], [], conclusion, candidate=first, candidate_rule=rule)
    conclusion = "听审标记认为从「%s」开始变差。按规则 %s，优先检查 %s。" % (_label(first), rule, cause)
    if first in ("vocal_separated", "instrumental"):
        conclusion += "不要先调 RVC。"
    if first == "rvc_raw":
        conclusion += "标记不能区分模型、F0、音高、Index、Protect、RMS、采样率或输入音域中的具体一项。"
    conclusion += "音质验收仍是 NOT_ASSESSED。"
    return _listening_base("LISTENING_RULE", first, rule, cause, checks, hold, conclusion)


def _listening_base(status, first, rule, cause, checks, hold, conclusion, candidate=None, candidate_rule=None):
    return {
        "basis": "supplied_listening_labels",
        "status": status,
        "listening_quality": "user_marked",
        "quality_acceptance": "NOT_ASSESSED",
        "blocks_job": False,
        "quality_truth": False,
        "first_flagged_stage": first,
        "candidate_stage": candidate if candidate is not None else first,
        "rule": rule,
        "candidate_rule": candidate_rule,
        "likely_cause": cause,
        "checks": list(checks),
        "do_not_tune": list(hold),
        "conclusion": conclusion,
    }


def _chain_flags(stages):
    flags = []
    previous = None
    reference_duration = None
    reference_label = None
    for stage in stages:
        if not stage.get("in_vocal_chain"):
            continue
        if previous and stage.get("path") and previous.get("path") and os.path.normcase(stage["path"]) == os.path.normcase(previous["path"]):
            continue
        if stage.get("present") and not stage.get("readable"):
            kind = "non_finite" if _non_finite(stage.get("error")) else "unreadable"
            flags.append(_flag(stage["id"], kind, stage.get("error") or "无法读取"))
        elif stage.get("readable"):
            clipped = int(stage.get("clipped_samples") or 0)
            if clipped > 0 and (previous is None or not previous.get("readable") or int(previous.get("clipped_samples") or 0) == 0):
                kind = "clipping_present" if previous is None else "clipping_onset"
                if previous is not None and previous.get("readable"):
                    flags.append(_flag(stage["id"], kind, "削波采样从 0 变为 %d" % clipped))
                elif previous is None:
                    flags.append(_flag(stage["id"], kind, "这一阶段开始时已经有 %d 个削波采样" % clipped))
            rms = _max_rms(stage)
            previous_rms = _max_rms(previous) if previous and previous.get("readable") else None
            if rms is not None and rms < SILENCE_RMS and previous_rms is not None and previous_rms >= AUDIBLE_RMS:
                flags.append(_flag(stage["id"], "silence_onset", "RMS 从可听降到接近静音"))
            duration = stage.get("duration_seconds")
            if stage["id"] == "separator_input" and previous and previous.get("readable") and duration is not None and previous.get("duration_seconds") is not None:
                if abs(duration - previous["duration_seconds"]) > DURATION_TOLERANCE_SECONDS:
                    flags.append(_flag(stage["id"], "duration_shift", "与原曲时长相差 %.3f 秒" % abs(duration - previous["duration_seconds"])))
            if stage["id"] in ("source", "separator_input") and stage.get("readable"):
                reference_duration = duration
                reference_label = _label(stage["id"])
            elif stage["id"] != "source" and reference_duration is not None and duration is not None:
                if abs(duration - reference_duration) > DURATION_TOLERANCE_SECONDS:
                    flags.append(_flag(
                        stage["id"],
                        "duration_shift",
                        "与%s时长相差 %.3f 秒" % (reference_label, abs(duration - reference_duration)),
                    ))
        if stage.get("readable"):
            previous = stage
    return flags


def _pair_flags(by_id):
    flags = []
    for left_id, right_id in HF_PAIRS:
        left = by_id[left_id]
        right = by_id[right_id]
        if not left.get("readable") or not right.get("readable"):
            continue
        if left.get("path") and right.get("path") and os.path.normcase(left["path"]) == os.path.normcase(right["path"]):
            continue
        before = left.get("high_frequency_ratio")
        after = right.get("high_frequency_ratio")
        if before is None or after is None:
            continue
        grown = before < HF_PREVIOUS_FLOOR or after >= before * HF_GROWTH
        if after - before >= HF_ABSOLUTE_DELTA and grown:
            flags.append(_flag(right_id, "hf_ratio_jump", "高频能量比从 %.4g 升到 %.4g" % (before, after)))
    return flags


def _instrumental_flags(by_id):
    stage = by_id["instrumental"]
    findings = []
    if stage.get("present") and not stage.get("readable"):
        findings.append(_flag("instrumental", "unreadable", stage.get("error") or "无法读取"))
        return findings
    if not stage.get("readable"):
        return findings
    if int(stage.get("clipped_samples") or 0) > 0:
        findings.append(_flag("instrumental", "clipping_present", "伴奏有削波采样"))
    vocal = by_id["vocal_separated"]
    if _max_rms(stage) is not None and _max_rms(stage) < SILENCE_RMS and vocal.get("readable") and (_max_rms(vocal) or 0) >= AUDIBLE_RMS:
        findings.append(_flag("instrumental", "silence_onset", "人声可听而伴奏接近静音"))
    reference = by_id["separator_input"] if by_id["separator_input"].get("readable") else by_id["source"]
    if stage.get("duration_seconds") is not None and reference.get("duration_seconds") is not None:
        delta = abs(stage["duration_seconds"] - reference["duration_seconds"])
        if delta > DURATION_TOLERANCE_SECONDS:
            findings.append(_flag("instrumental", "duration_shift", "伴奏时长相差 %.3f 秒" % delta))
    return findings


def _missing_before(stage_id, by_id):
    if stage_id == "instrumental":
        needed = ("source", "vocal_separated")
    elif stage_id in VOCAL_CHAIN:
        needed = VOCAL_CHAIN[:VOCAL_CHAIN.index(stage_id)]
    else:
        needed = ()
    return [item for item in needed if item in REQUIRED and not by_id[item].get("present")]


def _previous_id(stage_id):
    if stage_id == "instrumental":
        return "vocal_separated"
    index = VOCAL_CHAIN.index(stage_id)
    if index == 0:
        return None
    return VOCAL_CHAIN[index - 1]


def _assessed_previous_label(stage_id, labels, by_id):
    """Walk back through unlistened rows only while they are the same file."""
    previous = _previous_id(stage_id)
    if previous is None:
        return "ok"
    while previous is not None:
        label = labels.get(previous, "not_assessed")
        if label != "not_assessed":
            return label
        earlier = _previous_id(previous)
        if earlier is None or not _same_audio(by_id.get(previous), by_id.get(earlier)):
            return "not_assessed"
        previous = earlier
    return "not_assessed"


def _same_audio(left, right):
    if not left or not right or not left.get("path") or not right.get("path"):
        return False
    return os.path.normcase(left["path"]) == os.path.normcase(right["path"])


def _validate_labels(labels, by_id):
    if not isinstance(labels, dict):
        raise ValueError("听审标记无效")
    for key, value in labels.items():
        if key not in by_id:
            raise ValueError("未知阶段：" + str(key))
        if value not in LISTENING_VALUES:
            raise ValueError("听审标记只能是 ok、degraded 或 not_assessed")


def _stage_list(stages):
    if not isinstance(stages, list):
        raise ValueError("阶段列表无效")
    return stages


def _layout_observations(stages):
    """Record layout changes that are real but are not treated as damage."""
    observations = []
    previous = None
    for stage in stages:
        if not stage.get("in_vocal_chain") or not stage.get("readable"):
            continue
        if previous and stage.get("path") and previous.get("path") and os.path.normcase(stage["path"]) == os.path.normcase(previous["path"]):
            continue
        if previous and previous.get("channels") and stage.get("channels") and stage["channels"] != previous["channels"]:
            observations.append({
                "stage_id": stage["id"],
                "kind": "channel_change",
                "detail": "%s 声道从 %d 变为 %d" % (_label(stage["id"]), previous["channels"], stage["channels"]),
                "listening_quality": "not_assessed",
            })
        if previous and previous.get("duration_seconds") is not None and stage.get("duration_seconds") is not None:
            delta = abs(stage["duration_seconds"] - previous["duration_seconds"])
            if 0.01 < delta <= DURATION_TOLERANCE_SECONDS:
                observations.append({
                    "stage_id": stage["id"],
                    "kind": "duration_drift",
                    "detail": "%s 时长相差 %.3f 秒，未超过 %.1f 秒门限" % (_label(stage["id"]), delta, DURATION_TOLERANCE_SECONDS),
                    "listening_quality": "not_assessed",
                })
        previous = stage
    return observations


def _sort_flags(flags):
    order = {stage_id: index for index, stage_id in enumerate(VOCAL_CHAIN)}
    return sorted(flags, key=lambda item: (order.get(item["stage_id"], len(order)), item["kind"]))


def _flag(stage_id, kind, detail):
    return {"stage_id": stage_id, "kind": kind, "detail": detail, "listening_quality": "not_assessed"}


def _max_rms(stage):
    if not stage or not isinstance(stage.get("channel_rms"), list) or not stage["channel_rms"]:
        return None
    return max(stage["channel_rms"])


def _non_finite(error):
    text = error or ""
    return "NaN" in text or "Inf" in text or "空音频" in text


def _label(stage_id):
    for spec in _STAGE_SPECS:
        if spec[0] == stage_id:
            return spec[1]
    return stage_id


def _append_report(report_path, trace):
    with open(report_path, encoding="utf-8") as handle:
        current = handle.read()
    if HEADING in current:
        return
    section = format_stage_trace_report(trace)
    with open(report_path, "a", encoding="utf-8") as handle:
        if current and not current.endswith("\n"):
            handle.write("\n")
        handle.write("\n")
        handle.write(section)


def _dump(payload, path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
