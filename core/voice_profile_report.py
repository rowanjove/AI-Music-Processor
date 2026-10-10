"""Record the Profile a voice job actually froze. This does not retune it.

The RVC command keeps the job index_rate. The frozen profile supplies
protect, rms_mix_rate and filter_radius. A weights-side profile.json sits
next to the .pth file; the three installed weights share that directory, so
one sidecar would apply to all of them. config/model-profiles/<key>.json is
the per-model file. Neither file exists for the installed models today.
"""
import os

from core.config import CONFIG_WARNINGS, MODEL_PROFILE_DEFAULTS, get_available_rvc_models

HEADING = "## 模型生效值"
PROFILE_FIELDS = ("index_rate", "protect", "rms_mix_rate", "filter_radius", "f0_method", "pitch")
KNOWN_MODEL_KEYS = ("zhoujielun", "sunyanzi", "zhaolusi")


def _text(value):
    if type(value) is float:
        return format(value, ".12g")
    return str(value)


def _profile(model):
    if not isinstance(model, dict):
        return {}
    profile = model.get("profile")
    return profile if isinstance(profile, dict) else {}


def override_files(model):
    """Return existing override files. Absence means the global defaults."""
    local = ""
    config_path = ""
    if isinstance(model, dict):
        pth = model.get("original_pth") or model.get("pth") or ""
        if pth:
            candidate = os.path.join(os.path.dirname(os.path.abspath(pth)), "profile.json")
            if os.path.isfile(candidate):
                local = candidate
        raw = model.get("profile_path") or ""
        if raw and os.path.isfile(raw):
            config_path = os.path.abspath(raw)
    return local, config_path


def matches_defaults(profile):
    return all(profile.get(key) == MODEL_PROFILE_DEFAULTS.get(key) for key in PROFILE_FIELDS)


def format_effective_profile(job_index_rate, model, model_key):
    """Markdown for one frozen model. Job index_rate stays separate."""
    model = model if isinstance(model, dict) else {}
    profile = _profile(model)
    local, config_path = override_files(model)
    lines = [
        HEADING,
        "",
        "- 模型：%s" % (model_key or "未记录"),
        "- 显示名：%s" % (model.get("name") or "未记录"),
        "- 任务 index_rate：%s。RVC 使用这个值。" % _text(job_index_rate),
        "- 冻结 Profile index_rate：%s。它不会替换任务 index_rate。" % _text(profile.get("index_rate", "未记录")),
        "- protect：%s" % _text(profile.get("protect", "未记录")),
        "- rms_mix_rate：%s" % _text(profile.get("rms_mix_rate", "未记录")),
        "- filter_radius：%s" % _text(profile.get("filter_radius", "未记录")),
        "- f0_method：%s" % profile.get("f0_method", "未记录"),
        "- pitch：%s" % _text(profile.get("pitch", "未记录")),
        "- 权重旁 profile.json：%s" % ("无" if not local else "`%s`" % local),
        "- 模型配置 Profile：%s" % ("无" if not config_path else "`%s`" % config_path),
        "- 与全局默认相同：%s" % ("是" if matches_defaults(profile) else "否"),
    ]
    warnings = model.get("profile_warnings") or []
    if warnings:
        for item in warnings:
            lines.append("- 已忽略的 Profile：%s" % item)
    else:
        lines.append("- 已忽略的 Profile：无")
    return "\n".join(lines) + "\n"


def append_effective_profile(report_path, job_index_rate, model, model_key):
    """Append the section once. The caller writes it before the QC hash."""
    with open(report_path, encoding="utf-8") as handle:
        current = handle.read()
    if HEADING in current:
        return
    section = format_effective_profile(job_index_rate, model, model_key)
    with open(report_path, "a", encoding="utf-8") as handle:
        if current and not current.endswith("\n"):
            handle.write("\n")
        handle.write("\n")
        handle.write(section)


def audit_installed_models():
    """Read the three known models. Restores unrelated config warnings."""
    saved = list(CONFIG_WARNINGS)
    CONFIG_WARNINGS[:] = []
    try:
        models = get_available_rvc_models()
        recorded = {}
        for key in KNOWN_MODEL_KEYS:
            info = models.get(key)
            if not info:
                recorded[key] = {"present": False}
                continue
            profile = _profile(info)
            local, config_path = override_files(info)
            recorded[key] = {
                "present": True,
                "name": info.get("name"),
                "has_override_file": bool(local or config_path),
                "profile_warning_count": len(info.get("profile_warnings") or []),
                "matches_defaults": matches_defaults(profile),
                "effective": {field: profile.get(field) for field in PROFILE_FIELDS},
            }
        return {
            "role": "audit",
            "change": "VCQ-008",
            "defaults_changed": False,
            "global_defaults": {field: MODEL_PROFILE_DEFAULTS[field] for field in PROFILE_FIELDS},
            "models": recorded,
        }
    finally:
        CONFIG_WARNINGS[:] = saved
