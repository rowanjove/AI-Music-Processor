"""Read-only peak, RMS, clipping, and duration for one voice chain.

The three points are the current pipeline files: standardized input before
separation, the RVC stem, and the mix in the job work directory. This module
does not separate, convert, clean, or mix audio, and it does not judge
whether a render sounds better.
"""
import hashlib
import json
import os

from core.analyzer import probe_metadata
from core.qc import sample_metrics

SCHEMA_VERSION = 1
POINT_KEYS = ("pre_separation", "rvc_output", "mix_output")
METRIC_KEYS = ("duration_seconds", "sample_peak", "channel_rms", "clipped_samples")


def resolve_project_points(project_dir):
    """Map one completed voice project to the three existing stage files."""
    project_dir = os.path.abspath(project_dir)
    stems = os.path.join(project_dir, "stems")
    work = os.path.join(project_dir, ".work")
    if not os.path.isdir(stems):
        raise ValueError("没有 stems 目录: " + project_dir)
    rvc_names = sorted(
        name for name in os.listdir(stems)
        if name.startswith("03_vocal_rvc_") and name.lower().endswith(".wav")
    )
    if len(rvc_names) != 1:
        raise ValueError("需要恰好一个 RVC 输出，实际 %d 个: %s" % (len(rvc_names), project_dir))
    return {
        "pre_separation": os.path.join(work, "01_standard_input.wav"),
        "rvc_output": os.path.join(stems, rvc_names[0]),
        "mix_output": os.path.join(work, "06_mixed.wav"),
    }


def measure_voice_chain(paths):
    """Measure the three points. The same files produce the same numbers."""
    unknown = sorted(set(paths) - set(POINT_KEYS))
    missing = [key for key in POINT_KEYS if key not in paths]
    if unknown or missing:
        raise ValueError("观测点必须是分离前、RVC 输出、Mix 输出")
    points = {}
    for key in POINT_KEYS:
        path = paths[key]
        if not os.path.isfile(path):
            raise ValueError("观测点文件不存在: " + key)
        metrics = sample_metrics(path)
        meta = probe_metadata(path)
        points[key] = {
            "filename": os.path.basename(path),
            "bytes": os.path.getsize(path),
            "sha256": _sha256(path),
            "sample_rate": meta["sample_rate"],
            "channels": meta["channels"],
            "duration_seconds": meta["duration"],
            "sample_peak": metrics["sample_peak"],
            "channel_rms": metrics["channel_rms"],
            "clipped_samples": metrics["clipped_samples"],
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "voice_chain_observation",
        "listening_quality": "not_assessed",
        "points": points,
    }


def compare_observations(before, after):
    """Numeric differences only. A positive delta is not a quality verdict."""
    load_observation_data(before)
    load_observation_data(after)
    deltas = {}
    for key in POINT_KEYS:
        left = before["points"][key]
        right = after["points"][key]
        if len(left["channel_rms"]) != len(right["channel_rms"]):
            raise ValueError("观测点声道数不一致: " + key)
        deltas[key] = {
            "duration_seconds": right["duration_seconds"] - left["duration_seconds"],
            "sample_peak": right["sample_peak"] - left["sample_peak"],
            "clipped_samples": right["clipped_samples"] - left["clipped_samples"],
            "channel_rms": [
                item - left["channel_rms"][index]
                for index, item in enumerate(right["channel_rms"])
            ],
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "voice_chain_delta",
        "listening_quality": "not_assessed",
        "points": deltas,
    }


def load_observation(path):
    with open(path, "r", encoding="utf-8") as handle:
        return load_observation_data(json.load(handle))


def load_observation_data(record):
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("换声观测记录版本不符")
    if record.get("kind") != "voice_chain_observation":
        raise ValueError("不是换声观测记录")
    points = record.get("points")
    if not isinstance(points, dict):
        raise ValueError("换声观测记录缺少观测点")
    for key in POINT_KEYS:
        point = points.get(key)
        if not isinstance(point, dict) or any(name not in point for name in METRIC_KEYS):
            raise ValueError("观测点指标不完整: " + key)
    return record


def dump_observation(record, path):
    load_observation_data(record)
    text = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
