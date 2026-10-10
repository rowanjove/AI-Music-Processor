"""Legacy RVC and its real RMVPE adapter, with explicit missing-weight gates.

The modern compatibility identifier describes the pitch adapter, not a
replacement synthesizer. Calling the legacy API still records missing-weight
fallback; the adapter API never silently substitutes harvest.
"""
from core.f0_method import DIRECT, rmvpe_acceptance

SCHEMA_VERSION = 1
LEGACY = "legacy0618"
MODERN = "modern"
REFUSAL = "RMVPE 权重缺失或校验失败；适配器不会改跑 harvest"


def backend_catalog(root=None):
    """Only the authenticated managed weight permits an adapter request."""
    acceptance = rmvpe_acceptance(root)
    return {
        "schema_version": SCHEMA_VERSION,
        "default_backend": LEGACY,
        "selectable": [LEGACY, MODERN] if acceptance["runnable"] else [LEGACY],
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
        "backends": {
            LEGACY: {
                "id": LEGACY,
                "runnable": True,
                "implemented": True,
                "f0_methods": list(DIRECT) + (["rmvpe"] if acceptance["runnable"] else []),
                "rmvpe": acceptance["runnable"],
            },
            MODERN: {
                "id": MODERN,
                "runnable": acceptance["runnable"],
                "implemented": True,
                "description": "0618 synthesizer with pinned upstream RMVPE pitch adapter",
                "status": "AVAILABLE_UNVALIDATED" if acceptance["runnable"] else "UNAVAILABLE",
                "inference_status": "NOT_RUN",
                "f0_methods": ["rmvpe"],
                "acceptance_status": acceptance["status"],
                "weight_present": acceptance["weight_present"],
            },
        },
    }


def convert_modern(requested_f0, root=None, **conversion):
    """Execute supplied voice conversion arguments, or report a preflight.

Without input/output this is an honest NOT_RUN preflight. Missing RMVPE
weights are refused before delegating, even though the old API allows fallback.
"""
    if requested_f0 not in DIRECT and requested_f0 != "rmvpe":
        raise ValueError("未知 F0：" + str(requested_f0))
    acceptance = rmvpe_acceptance(root)
    record = {
        "schema_version": SCHEMA_VERSION,
        "backend": MODERN,
        "implemented": True,
        "executed": False,
        "inference_status": "NOT_RUN",
        "requested_f0": requested_f0,
        "actual_f0": None,
        "fallback_reason": REFUSAL if requested_f0 == "rmvpe" and not acceptance["runnable"] else "未提供推理输入/输出",
        "weight_present": acceptance["weight_present"],
        "acceptance_status": acceptance["status"],
        "quality_acceptance": "NOT_ASSESSED",
        "listening_quality": "not_assessed",
        "output": None,
    }
    if not conversion:
        return record
    if requested_f0 == "rmvpe" and not acceptance["runnable"]:
        raise FileNotFoundError(REFUSAL)
    if root is not None:
        from core.config import RVC_ROOT
        import os
        if os.path.realpath(root) != os.path.realpath(RVC_ROOT):
            raise ValueError("Conversion must use the frozen installed RVC root")
    from core.rvc_engine import convert_voice
    output = convert_voice(f0_method=requested_f0, **conversion)
    record.update(executed=True, inference_status="COMPLETE", actual_f0=requested_f0,
                  output=output, fallback_reason="")
    return record
