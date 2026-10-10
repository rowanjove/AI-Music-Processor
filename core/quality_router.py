"""Build an evidence-backed quality plan from frozen inputs and stage records."""
from core.budgeted_search import budget_limit
from core.config import MODEL_PROFILE_DEFAULTS
from core.separator_routing import hp2_profile, plan_separator_routes, validate_separator_profile
from core.source_policy import HINTS, preprocess_policy

SCHEMA_VERSION = 2
_PROFILE_FIELDS = ("index_rate", "protect", "rms_mix_rate", "filter_radius", "f0_method")


def route_quality(source_hint="auto", facts=None, quality_mode="balanced", profile=None,
                  hardware=None, expert_limit=None, job_spec=None, executed_stages=None,
                  source_policy_record=None):
    """Summarize the frozen plan and what the pipeline actually executed.

    Standalone callers may omit ``job_spec`` and receive a non-applied preview.
    Pipeline reports pass the frozen spec and durable stage rows; a planned
    option is never described as executed unless its stage completed.
    """
    if not isinstance(quality_mode, str):
        raise ValueError("未知质量档")
    if source_hint not in HINTS:
        raise ValueError("未知来源提示")
    if facts is not None and not isinstance(facts, dict):
        raise ValueError("音频事实无效")
    stages = _stage_map(executed_stages)
    limit = budget_limit(quality_mode, expert_limit)
    policy = preprocess_policy(facts, source_hint) if facts is not None else None
    values, profile_source = _current_profile(profile)
    plan = {
        "schema_version": SCHEMA_VERSION,
        "applied": False,
        "changes_job": False,
        "quality_mode": quality_mode,
        "separator": _preview_separator(),
        "pre_separator_clean": None if policy is None else policy["pre_separator_clean"],
        "preprocess_recommendation": None if policy is None else policy["recommendation"],
        "preprocess_reasons": [] if policy is None else list(policy["reasons"]),
        "source_policy": source_policy_record,
        "source_preclean_enabled": False,
        "vocal_restore": [],
        "vocal_restore_selected": None,
        "f0_method": "harvest",
        "f0_method_planned": "harvest",
        "f0_requested": "harvest",
        "f0_execution_status": "NOT_RUN",
        "rmvpe": "NOT_RUN",
        "pitch_applied": False,
        "profile_source": profile_source,
        "profile_values": values,
        "values_are_current_defaults": profile_source == "global_default",
        "retry_budget": 0,
        "search_budget": limit,
        "search_enabled": False,
        "search_executed": False,
        "search_stage_status": "NOT_CONFIGURED",
        "executed_stages": [],
        "hardware": "NOT_SUPPLIED" if hardware is None else "caller_supplied",
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "reason": "没有已听审的更优链路，保持当前默认",
    }
    if not isinstance(job_spec, dict):
        return plan

    source_hint = job_spec.get("source_hint", source_hint)
    plan["source_preclean_enabled"] = job_spec.get("source_preclean_enabled") is True
    plan["source_policy"] = source_policy_record
    plan["frozen_spec_hash"] = job_spec.get("spec_hash")
    done = [name for name, row in stages.items() if row.get("status") == "DONE"]
    plan["executed_stages"] = done

    separator_profile = job_spec.get("separator_profile")
    if isinstance(separator_profile, dict):
        checked = validate_separator_profile(separator_profile)
        route = plan_separator_routes(
            checked["vocal"]["id"], checked["instrumental"]["id"])
        plan["separator"] = {
            "vocal": checked["vocal"]["id"],
            "instrumental": checked["instrumental"]["id"],
            "execution": "split" if checked["mode"] == "dual" else "single_pass",
            "available": route.get("available") is True,
            "frozen": True,
            "stage_status": stages.get("Separate", {}).get("status", "NOT_RUN"),
            "model_identities_frozen": bool(job_spec.get("effective_separator")),
        }
    else:
        plan["separator"]["stage_status"] = stages.get("Separate", {}).get("status", "NOT_RUN")
        plan["separator"]["frozen"] = False

    f0_stage = stages.get("RVC", {})
    f0_parameters = f0_stage.get("parameters") if isinstance(f0_stage.get("parameters"), dict) else {}
    f0_frozen = job_spec.get("effective_f0") if isinstance(job_spec.get("effective_f0"), dict) else {}
    plan["f0_requested"] = f0_parameters.get("f0_requested") or f0_frozen.get("requested") or job_spec.get("f0_method", "harvest")
    plan["f0_method_planned"] = f0_frozen.get("executed") or job_spec.get("f0_method", "harvest")
    plan["f0_method"] = f0_parameters.get("f0_method") if f0_stage.get("status") == "DONE" else None
    plan["f0_execution_status"] = f0_stage.get("status", "NOT_RUN")
    plan["rmvpe"] = ("EXECUTED" if plan["f0_method"] == "rmvpe"
                      else "FALLBACK_TO_HARVEST" if f0_stage.get("status") == "DONE" and plan["f0_requested"] == "rmvpe" and plan["f0_method"] == "harvest"
                      else "NOT_RUN")
    headroom_parameters = f0_parameters
    headroom_outputs = f0_stage.get("outputs") if isinstance(f0_stage.get("outputs"), dict) else {}
    plan["rvc_input_headroom"] = {
        "stage_status": f0_stage.get("status", "NOT_RUN"),
        "strategy": headroom_parameters.get("rvc_input_headroom_strategy"),
        "gain_db": headroom_parameters.get("rvc_input_gain_db") if f0_stage.get("status") == "DONE" else None,
        "input_peak_dbfs": headroom_parameters.get("rvc_input_peak_dbfs"),
        "report_path": headroom_outputs.get("headroom_report"),
    }
    plan["pitch_applied"] = stages.get("PitchCorrection", {}).get("status") == "DONE"

    restoration = stages.get("VocalRestore", {})
    if restoration.get("status") == "DONE":
        effect = (restoration.get("parameters") or {}).get("effect_id")
        if effect:
            plan["vocal_restore"] = [effect]
    frozen_restoration = job_spec.get("effective_vocal_restoration")
    if isinstance(frozen_restoration, dict):
        plan["vocal_restore_selected"] = frozen_restoration.get("effect_id")

    plan["search_enabled"] = job_spec.get("parameter_search_enabled") is True
    search = stages.get("RVCSearch", {})
    plan["search_stage_status"] = search.get("status", "NOT_RUN" if plan["search_enabled"] else "NOT_CONFIGURED")
    plan["search_executed"] = search.get("status") == "DONE"
    if plan["search_enabled"]:
        try:
            plan["search_budget"] = budget_limit(job_spec.get("parameter_search_budget", "fast"))
        except ValueError:
            plan["search_budget"] = 0
    else:
        plan["search_budget"] = 0
    plan["retry_budget"] = job_spec.get("quality_retry_limit", 0)
    plan["profile_values"], plan["profile_source"] = _current_profile(
        (job_spec.get("effective_rvc") or {}).get("profile") if isinstance(job_spec.get("effective_rvc"), dict) else profile)
    plan["values_are_current_defaults"] = plan["profile_source"] == "global_default"
    plan["source_hint"] = source_hint
    if source_policy_record:
        plan["pre_separator_clean"] = source_policy_record.get("pre_separator_clean", "none")
        plan["preprocess_recommendation"] = source_policy_record.get("recommendation")
        plan["preprocess_reasons"] = list(source_policy_record.get("reasons") or [])
    if not plan["executed_stages"]:
        plan["reason"] = "任务参数已冻结；尚无已完成阶段记录"
    elif plan["search_executed"]:
        plan["reason"] = "已记录独立参数候选；仍保留基线结果，未自动推广或宣称音质更优"
    else:
        plan["reason"] = "按任务冻结参数执行；音质听审仍未完成"
    return plan


def _stage_map(executed_stages):
    if isinstance(executed_stages, dict):
        rows = executed_stages.values()
    elif isinstance(executed_stages, (list, tuple)):
        rows = executed_stages
    else:
        return {}
    result = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = item.get("stage_name") or item.get("name")
        if isinstance(name, str):
            result[name] = item
    return result


def _preview_separator():
    routes = plan_separator_routes()
    return {
        "vocal": routes["vocal_backend"],
        "instrumental": routes["instrumental_backend"],
        "execution": routes["execution"],
        "frozen": False,
        "stage_status": "NOT_RUN",
    }


def _current_profile(profile):
    if not isinstance(profile, dict):
        return {key: MODEL_PROFILE_DEFAULTS[key] for key in _PROFILE_FIELDS}, "global_default"
    values = {}
    for key in _PROFILE_FIELDS:
        values[key] = profile[key] if key in profile else MODEL_PROFILE_DEFAULTS[key]
    same = all(values[key] == MODEL_PROFILE_DEFAULTS[key] for key in _PROFILE_FIELDS)
    return values, "global_default" if same else "caller_profile"
