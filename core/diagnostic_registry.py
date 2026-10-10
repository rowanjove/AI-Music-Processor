"""Attach catalog status beside the existing diagnostic checks.

The rows use registry status names. They do not replace FFmpeg-style
PASS/FAIL fields, and they do not call verify_runtime.
"""
from core.component_platform import capability_report
from core.f0_method import rmvpe_acceptance
from core.separator_choice import separator_acceptance
import os


def registry_status(registry):
    """Keep executable capability and unassessed listening status separate."""
    report = capability_report(registry)
    pitch = report["capabilities"].get("pitch.rmvpe") or {}
    components = registry["components"]
    rmvpe = components.get("engine.f0.rmvpe") or {}
    rows = []
    for component_id in registry["component_order"]:
        item = components[component_id]
        rows.append({
            "id": component_id,
            "status": item["status"],
            "implemented": item["implemented"],
        })
    registry_root = registry.get('root')
    acceptance = rmvpe_acceptance(os.path.join(registry_root, 'tools', 'RVC', 'RVC-beta-v2-0618')
                                  if registry_root else None)
    separators = separator_acceptance()
    return {
        "source": "component_registry",
        "note": "组件状态来自注册表，不替换工具检查的 PASS/FAIL",
        "pitch_rmvpe_available": bool(pitch.get("available")) and acceptance['runnable'],
        "engine_f0_rmvpe_status": rmvpe.get("status"),
        "rmvpe_acceptance_status": acceptance["status"],
        "rmvpe_runnable": acceptance["runnable"],
        "separator_default": separators["default_backend"],
        "separator_alternates_runnable": separators["runnable_alternates"],
        "quality_assist_default": False,
        "components": rows,
    }
