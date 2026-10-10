"""Listening status for polish. A green test is not a listening pass.

QUALITY_ACCEPTED is returned only when both a real inference record and a human
listening record are present. This module never invents either one.
"""

EXTERNAL = "READY_FOR_EXTERNAL_VALIDATION"
ACCEPTED = "QUALITY_ACCEPTED"
TRACE_ROLES = ("source", "repaired", "tone", "dynamics", "stereo", "master")


def _listening_ok(record):
    if not isinstance(record, dict):
        return False
    listener = record.get("listener")
    date = record.get("date")
    notes = record.get("notes")
    return bool(isinstance(listener, str) and listener.strip() and isinstance(date, str) and date.strip() and isinstance(notes, str) and notes.strip())


def _inference_ok(record):
    if not isinstance(record, dict):
        return False
    if record.get("inference_ran") is not True:
        return False
    artifact = record.get("artifact")
    return isinstance(artifact, str) and bool(artifact.strip())


def polish_quality_status(inference_record, listening_record):
    """Accept only when both records exist. Otherwise stay externally unvalidated."""
    if _inference_ok(inference_record) and _listening_ok(listening_record):
        return ACCEPTED
    return EXTERNAL


def ab_manifest(paths, inference_record=None, listening_record=None):
    """Point at trace files that already exist. Missing listening stays external."""
    import os
    if not isinstance(paths, dict):
        raise ValueError("A/B 路径必须是字典")
    roles = {}
    for role in TRACE_ROLES:
        path = paths.get(role)
        if path and os.path.isfile(path) and os.path.getsize(path) > 0:
            roles[role] = {"status": "PRESENT", "bytes": os.path.getsize(path)}
        else:
            roles[role] = {"status": "NOT_PRESENT"}
    status = polish_quality_status(inference_record, listening_record)
    return {
        "roles": roles,
        "quality_acceptance": status,
        "listening_quality": "assessed" if _listening_ok(listening_record) else "not_assessed",
        "inference_ran": bool(_inference_ok(inference_record)),
    }


DEFECT_TAGS = (
    "distortion",      # 破音/过载
    "robotic",         # 电音/机械感
    "sibilance",       # 齿音过重
    "bleed",           # 串音残留
    "muffled",         # 声音发闷
    "sync_drift",      # 节奏/对齐漂移
)


def create_listening_evaluation(case_id, listener, date, choice_a_role, choice_b_role, preferred_choice, defect_tags=None, notes=None):
    """Create a structured human listening evaluation record."""
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("听审记录缺少 case_id")
    if not isinstance(listener, str) or not listener.strip():
        raise ValueError("听审记录缺少试听者姓名或身份")
    if not isinstance(date, str) or not date.strip():
        raise ValueError("听审记录缺少日期")
    if preferred_choice not in ("A", "B", "EQUAL", "NEITHER"):
        raise ValueError("盲听选择必须为 A、B、EQUAL 或 NEITHER")
    tags = list(defect_tags or [])
    invalid_tags = [t for t in tags if t not in DEFECT_TAGS]
    if invalid_tags:
        raise ValueError("未知问题标签: %s，有效标签: %s" % (", ".join(invalid_tags), ", ".join(DEFECT_TAGS)))

    return {
        "schema_version": 1,
        "case_id": case_id.strip(),
        "listener": listener.strip(),
        "date": date.strip(),
        "choice_a_role": choice_a_role,
        "choice_b_role": choice_b_role,
        "preferred_choice": preferred_choice,
        "defect_tags": tags,
        "notes": (notes or "").strip(),
        "listening_quality": "assessed",
        "quality_acceptance": "READY_FOR_EXTERNAL_VALIDATION",
    }


def save_listening_evaluation(record, user_data_dir):
    """Persist human listening evaluation to user data directory."""
    import hashlib
    import json
    from pathlib import Path

    user_data_dir = Path(user_data_dir)
    save_dir = user_data_dir / "listening_evaluations"
    save_dir.mkdir(parents=True, exist_ok=True)
    slug = f"{record['case_id']}_{record['listener']}_{record['date']}_{record['preferred_choice']}"
    file_id = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:12]
    out_path = save_dir / f"eval_{record['case_id']}_{file_id}.json"
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(out_path)


def export_anonymized_listening_summary(evaluations, output_path=None):
    """Aggregate listening evaluations with anonymized listener identities.

    Never publishes raw personal listener names or unredacted credentials.
    """
    import hashlib
    import json
    from pathlib import Path

    if not isinstance(evaluations, (list, tuple)):
        raise ValueError("evaluations 必须为列表或元组")

    total = len(evaluations)
    choices_count = {"A": 0, "B": 0, "EQUAL": 0, "NEITHER": 0}
    defects_count = {tag: 0 for tag in DEFECT_TAGS}
    cases = set()
    anonymized_records = []

    for item in evaluations:
        if not isinstance(item, dict):
            continue
        c = item.get("preferred_choice")
        if c in choices_count:
            choices_count[c] += 1
        for t in item.get("defect_tags", []):
            if t in defects_count:
                defects_count[t] += 1
        cid = item.get("case_id", "unknown")
        cases.add(cid)

        raw_listener = item.get("listener", "anonymous")
        anon_id = "evaluator-" + hashlib.sha256(raw_listener.encode("utf-8")).hexdigest()[:8]
        anonymized_records.append({
            "case_id": cid,
            "evaluator_anon_id": anon_id,
            "date": item.get("date"),
            "preferred_choice": c,
            "defect_tags": item.get("defect_tags", []),
            "choice_a_role": item.get("choice_a_role"),
            "choice_b_role": item.get("choice_b_role"),
        })

    summary = {
        "schema_version": 1,
        "total_evaluations": total,
        "distinct_cases_count": len(cases),
        "choices_distribution": choices_count,
        "defects_distribution": defects_count,
        "anonymized_evaluations": anonymized_records,
        "listening_quality": "assessed" if total > 0 else "not_assessed",
        "quality_acceptance": "READY_FOR_EXTERNAL_VALIDATION",
    }

    if output_path:
        out_path = Path(output_path).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return summary
