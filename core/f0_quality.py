"""Explain an F0 contour that was already exported. Do not infer one.

RVC 0618 computes F0 inside VC.pipeline and the worker writes only the
waveform. A missing contour is unobservable. This module does not call
harvest, pm, crepe, or RMVPE, and it does not block a song job.
"""
import math

STATUS_UNOBSERVABLE = "不可观测"
STATUS_STABLE = "F0_STABLE"
STATUS_UNSTABLE = "F0_UNSTABLE"
JUMP_SEMITONES = 12
VOICED_MIN_HZ = 50
VOICED_MAX_HZ = 1100
EXPORT_REASON = "RVC 0618 worker 没有写出 F0 轨迹，不能从成品波形反推分数"
HEADING = "## F0 轨迹"


def worker_export_record():
    """The installed worker has no F0 sidecar. This is not a pitch score."""
    return {
        "status": STATUS_UNOBSERVABLE,
        "blocks_job": False,
        "score": None,
        "reason": EXPORT_REASON,
        "frames": None,
        "jump_count": None,
        "voiced_ratio": None,
    }


def assess_f0_track(hz):
    """Explain one Hz contour. Zero is unvoiced. Successive voiced frames are compared.

    Zeros between voiced frames are skipped. A step above 12 semitones is
    F0_UNSTABLE. The result has no numeric quality score.
    """
    record = {
        "status": STATUS_UNSTABLE,
        "blocks_job": False,
        "score": None,
        "reason": "",
        "frames": 0,
        "jump_count": 0,
        "voiced_ratio": None,
    }
    if isinstance(hz, (str, bytes)) or not hasattr(hz, "__iter__"):
        record["reason"] = "没有可检查的 F0 轨迹"
        record["frames"] = None
        record["jump_count"] = None
        return record
    values = list(hz)
    record["frames"] = len(values)
    if not values:
        record["reason"] = "F0 轨迹为空"
        return record
    for item in values:
        if type(item) is bool or type(item) not in (int, float) or not math.isfinite(item):
            record["reason"] = "F0 轨迹含有非有限数值"
            record["jump_count"] = None
            record["voiced_ratio"] = None
            return record
    voiced = [item for item in values if item > 0]
    record["voiced_ratio"] = len(voiced) / float(len(values))
    if not voiced:
        record["reason"] = "F0 轨迹没有有声帧"
        return record
    if any(item < VOICED_MIN_HZ or item > VOICED_MAX_HZ for item in voiced):
        record["reason"] = "有声帧超出 50 到 1100 Hz"
        return record
    jumps = 0
    previous = None
    for item in values:
        if item <= 0:
            continue
        if previous is not None:
            span = abs(12.0 * math.log(item / previous, 2))
            if span > JUMP_SEMITONES:
                jumps += 1
        previous = item
    record["jump_count"] = jumps
    if jumps:
        record["reason"] = "相邻有声帧超过 12 个半音"
        return record
    record["status"] = STATUS_STABLE
    record["reason"] = "相邻有声帧没有超过 12 个半音"
    return record


def format_f0_report(record):
    record = record if isinstance(record, dict) else {}
    blocks = record.get("blocks_job")
    if record.get("score") is None:
        score = "无"
    else:
        score = str(record.get("score"))
    lines = [
        HEADING,
        "",
        "- 状态：%s" % record.get("status", "未记录"),
        "- 不改变任务成败：%s" % ("是" if blocks is False else "否"),
        "- 分数：%s" % score,
        "- 原因：%s" % record.get("reason", ""),
    ]
    return "\n".join(lines) + "\n"


def append_f0_report(report_path, record):
    """Append the section once. A missing score stays unwritten as a number."""
    with open(report_path, encoding="utf-8") as handle:
        current = handle.read()
    if HEADING in current:
        return
    section = format_f0_report(record)
    with open(report_path, "a", encoding="utf-8") as handle:
        if current and not current.endswith("\n"):
            handle.write("\n")
        handle.write("\n")
        handle.write(section)
