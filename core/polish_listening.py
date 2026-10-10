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
