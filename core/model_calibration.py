"""Report whether a model has its own listened profile. This does not write one.

Search output and a caller-supplied listening flag are not enough evidence
to change index, protect, rms, or filter defaults.
"""
from core.voice_profile_report import matches_defaults, override_files

SCHEMA_VERSION = 1


def calibration_status(model_key, model):
    model = model if isinstance(model, dict) else {}
    profile = model.get("profile") if isinstance(model.get("profile"), dict) else {}
    local, config_path = override_files(model)
    return {
        "schema_version": SCHEMA_VERSION,
        "model_key": model_key,
        "profile_matches_global_default": matches_defaults(profile),
        "override_present": bool(local or config_path),
        "calibrated": False,
        "calibration_status": "NOT_ASSESSED",
        "proposed_profile": None,
        "quality_acceptance": "NOT_ASSESSED",
        "listening_quality": "not_assessed",
    }


def promote_profile(model_key, proposal, listening_passed=False):
    """Refuse every write. A boolean is not a listening record."""
    if not model_key or not isinstance(proposal, dict):
        raise ValueError("没有可保存的模型校准")
    if listening_passed:
        raise ValueError("听审旗标不能写成模型 Profile")
    raise ValueError("搜索结果不能写成模型默认值")
