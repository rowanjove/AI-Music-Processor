"""Resolve the legacy single-model voice-separator request without a worker.

HP2 remains the legacy single-pass request. The current two-model MDX route is
an independent Kim Vocal 2 + Inst HQ 3 profile resolved by separator_routing;
rejecting the legacy ``mdx`` id does not mean that dual profile is absent.
RoFormer remains an isolated candidate and is not selectable in production.
"""

HP2_BACKEND = "hp2"
HP2_MODEL = "HP2_all_vocals.pth"
HP2_AGG = 10
UNAVAILABLE = ("mdx", "roformer")
EXTERNAL = "READY_FOR_EXTERNAL_VALIDATION"
_MISSING = ["已审核的高质量分离权重", "同源短样本实测指标", "人工听审"]


def validate_separator_backend(value):
    """JobSpec can store only the runnable default."""
    if value != HP2_BACKEND:
        raise ValueError("任务只能冻结已实现的分离后端 hp2")
    return value


def resolve_separator_request(backend=None):
    """Return the HP2 call, or reject another id before any worker starts."""
    if backend is None or backend == HP2_BACKEND:
        return {"backend": HP2_BACKEND, "model_name": HP2_MODEL, "agg": HP2_AGG}
    if backend in UNAVAILABLE:
        raise ValueError(str(backend) + " 尚未审核，不能送进分离 worker")
    raise ValueError("未知的换声分离后端：" + str(backend))


def separator_acceptance():
    """What this workspace can claim. It does not invent comparison numbers."""
    return {
        "status": EXTERNAL,
        "default_backend": HP2_BACKEND,
        "selectable": [HP2_BACKEND],
        "unavailable": list(UNAVAILABLE),
        "runnable_alternates": False,
        "listening_quality": "not_assessed",
        "same_source_metrics": None,
        "missing_evidence": list(_MISSING),
    }


def compare_separator_metrics(source_id, hp2, other):
    """Subtract caller-supplied measurements. Missing input is not a result."""
    record = {
        "status": EXTERNAL,
        "default_backend": HP2_BACKEND,
        "listening_quality": "not_assessed",
        "metrics": None,
        "metrics_are_audited_separator": False,
        "missing_evidence": list(_MISSING),
    }
    if not _measured(hp2) or not _measured(other):
        return record
    if hp2.get("source_id") != source_id or other.get("source_id") != source_id:
        record["missing_evidence"] = ["两次测量的 source_id 不一致"]
        return record
    if other.get("backend") in (None, HP2_BACKEND):
        record["missing_evidence"] = ["对照后端不能仍是 hp2"]
        return record
    record["metrics"] = {
        "peak_delta": other["peak"] - hp2["peak"],
        "duration_delta": other["duration"] - hp2["duration"],
        "source_id": source_id,
        "sdr": other.get("sdr") if (other.get("has_clean_reference") and hp2.get("has_clean_reference")) else "NOT_MEASURABLE (无干净参考音轨)",
    }
    if other.get("audited") is True and hp2.get("audited") is True:
        record["metrics_are_audited_separator"] = True
        record["status"] = "MEASURED"
        record["missing_evidence"] = []
        listening = other.get("listening_quality")
        if listening not in (None, "not_assessed"):
            record["listening_quality"] = listening
        else:
            record["missing_evidence"] = ["人工听审"]
    return record


def _measured(payload):
    if not isinstance(payload, dict) or payload.get("measured") is not True:
        return False
    for key in ("peak", "duration", "source_id", "backend"):
        if key not in payload:
            return False
    peak = payload["peak"]
    duration = payload["duration"]
    if type(peak) is bool or type(peak) not in (int, float):
        return False
    if type(duration) is bool or type(duration) not in (int, float):
        return False
    return True
