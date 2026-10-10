"""Bounded quality retry outside the CUDA OOM recovery path.

Missing or 0 means one inference. 1 allows a single extra attempt. A second
failure stops and keeps the first artifact. This module is not called by the
default job path or by worker_runtime.
"""
import copy

HARD_CAP = 1


def validate_quality_retry_limit(value):
    if type(value) is not int or value not in (0, 1):
        raise ValueError("quality_retry_limit 只能是 0 或 1")
    return value


def resolve_quality_retry_limit(parameters):
    """Missing means the historical path: one inference and no quality retry."""
    if not isinstance(parameters, dict) or "quality_retry_limit" not in parameters:
        return 0
    return validate_quality_retry_limit(parameters["quality_retry_limit"])


def default_inference_count(parameters):
    """Jobs do not search or retry unless an explicit helper is called."""
    resolve_quality_retry_limit(parameters)
    return 1


def run_quality_attempts(primary, retry_candidate, infer, limit):
    """Call infer at most twice. The second failure keeps the first artifact."""
    allowed = validate_quality_retry_limit(limit)
    if not callable(infer):
        raise ValueError("重试需要显式的推理函数")
    if allowed > HARD_CAP:
        raise ValueError("音质重试超过硬上限")
    first_parameters = copy.deepcopy(primary)
    first = infer(first_parameters)
    attempts = [_attempt(1, first_parameters, first)]
    if allowed == 0 or not _failed(first):
        return _result(attempts, first, "未重试")
    second_parameters = copy.deepcopy(retry_candidate)
    second = infer(second_parameters)
    attempts.append(_attempt(2, second_parameters, second))
    if _failed(second):
        return _result(attempts, first, "第二次失败，保留第一次产物")
    return _result(attempts, second, "第二次返回了产物")


def _failed(output):
    return not isinstance(output, dict) or output.get("failed") is not False


def _artifact(output):
    if isinstance(output, dict) and "artifact" in output:
        return output["artifact"]
    return output


def _attempt(number, parameters, output):
    return {
        "attempt": number,
        "parameters": parameters,
        "failed": _failed(output),
        "artifact": _artifact(output),
    }


def _result(attempts, output, reason):
    return {
        "attempts": attempts,
        "kept": _artifact(output),
        "stopped": True,
        "inference_count": len(attempts),
        "reason": reason,
    }
