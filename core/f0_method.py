"""Resolve an F0 request before the RVC 0618 worker.

The worker injects an RMVPE F0 adapter without changing the installed vendor.
Only missing weights retain the explicitly recorded legacy harvest fallback.
Present but invalid checkpoints fail inside the worker instead of falling back.
"""
import os
import hashlib

from core.config import RVC_ROOT

DIRECT = ("harvest", "pm", "crepe")
FALLBACK = "harvest"
WEIGHT_NAME = "rmvpe.pt"
PINNED_RMVPE_SHA256 = '6d62215f4306e3ca278246188607209f09af3dc77ed4232efdd069798c4ec193'
PINNED_RMVPE_BYTES = 181184272
ABSENT_REASON = "rmvpe.pt 不存在，0618 RMVPE 适配器缺权重，明确回退 harvest"
PRESENT_REASON = "RMVPE 适配器将校验并执行权重；非法权重不会回退"


def weight_path(root=None):
    base = RVC_ROOT if root is None else root
    return os.path.join(os.fspath(base), WEIGHT_NAME)


def verify_rmvpe_weight(path, return_bytes=False):
    """Authenticate the managed official weight before old Torch deserialization."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise FileNotFoundError('RMVPE requires a nonempty pinned rmvpe.pt')
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        if return_bytes:
            data = handle.read(PINNED_RMVPE_BYTES + 1)
            digest.update(data)
            if len(data) != PINNED_RMVPE_BYTES:
                raise ValueError('RMVPE weight size/SHA-256 is not the audited artifact')
        else:
            if os.fstat(handle.fileno()).st_size != PINNED_RMVPE_BYTES:
                raise ValueError('RMVPE weight size/SHA-256 is not the audited artifact')
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
    if digest.hexdigest() != PINNED_RMVPE_SHA256:
        raise ValueError('RMVPE weight SHA-256 is not the audited official artifact; refusing deserialization')
    return data if return_bytes else PINNED_RMVPE_SHA256


def resolve_f0_method(requested, root=None):
    if requested in DIRECT:
        return {"requested": requested, "executed": requested, "fallback": False, "reason": ""}
    if requested != "rmvpe":
        raise ValueError("当前 RVC 运行时只支持 harvest、pm、crepe，不支持 " + str(requested))
    path = weight_path(root)
    if os.path.isfile(path) and os.path.getsize(path) == 0:
        raise ValueError("RMVPE 权重为空，拒绝使用")
    if os.path.isfile(path):
        return {"requested": "rmvpe", "executed": "rmvpe", "fallback": False, "reason": ""}
    else:
        reason = ABSENT_REASON
    return {"requested": "rmvpe", "executed": FALLBACK, "fallback": True, "reason": reason}


def rmvpe_acceptance(root=None):
    """Separate executable managed weights from inference and listening evidence."""
    path = weight_path(root)
    present = os.path.isfile(path) and os.path.getsize(path) > 0
    verified = False
    if present:
        try:
            verify_rmvpe_weight(path)
            verified = True
        except (OSError, ValueError):
            pass
    missing = [
        "可运行的 rmvpe.pt 与真实 RMVPE 推理",
        "短样本 Golden after",
        "人工听审记录",
    ]
    if not present:
        missing.insert(0, "rmvpe.pt 不存在")
    return {
        "status": "READY_FOR_EXTERNAL_VALIDATION",
        "implemented": True,
        "runnable": verified,
        "weight_present": present,
        "weight_verified": verified,
        "expected_weight_sha256": PINNED_RMVPE_SHA256,
        "executed_fallback": FALLBACK,
        "listening": "待人工听审",
        "golden_after": None,
        "missing_evidence": missing,
    }


def apply_f0_to_stage(parameters, requested, root=None):
    """Return stage parameters whose f0_method is what the worker will run."""
    if not isinstance(parameters, dict):
        raise ValueError("RVC 阶段参数无效")
    decision = resolve_f0_method(requested, root=root)
    updated = dict(parameters)
    updated["f0_method"] = decision["executed"]
    updated.pop("f0_requested", None)
    updated.pop("f0_fallback_reason", None)
    if decision["fallback"]:
        updated["f0_requested"] = decision["requested"]
        updated["f0_fallback_reason"] = decision["reason"]
    return updated, decision["executed"]
