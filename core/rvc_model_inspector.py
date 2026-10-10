"""Read RVC checkpoint facts. This does not score a model or write a profile.

Speaker-table rows are not a trained speaker count. An index dimension match
is not proof that the index was trained with this checkpoint.
"""
import os
import hashlib

from core.voice_profile_report import matches_defaults, override_files

SCHEMA_VERSION = 1
ARCH_DIM = {"v1": 256, "v2": 768}
RATE_LABELS = {"32k": 32000, "40k": 40000, "48k": 48000}
WORKER_CLASS = {
    ("v1", 1): "SynthesizerTrnMs256NSFsid",
    ("v1", 0): "SynthesizerTrnMs256NSFsid_nono",
    ("v2", 1): "SynthesizerTrnMs768NSFsid",
    ("v2", 0): "SynthesizerTrnMs768NSFsid_nono",
}
PHONE = "enc_p.emb_phone.weight"
EMBEDDING = "emb_g.weight"


def inspect_checkpoint(checkpoint):
    """Inspect an already loaded checkpoint object. Does not import torch."""
    unread = _unread("checkpoint 不是 RVC 字典")
    if not isinstance(checkpoint, dict):
        return unread
    weight = checkpoint.get("weight")
    config = checkpoint.get("config")
    if not isinstance(weight, dict) or not isinstance(config, (list, tuple)) or not config:
        return unread
    version = checkpoint.get("version")
    if version not in ARCH_DIM:
        version = None
    f0_flag = checkpoint.get("f0")
    has_f0 = f0_flag if f0_flag in (0, 1) else None
    sample_rate = config[-1] if type(config[-1]) is int else None
    label = checkpoint.get("sr")
    label_rate = RATE_LABELS.get(label) if isinstance(label, str) else None
    phone = weight.get(PHONE)
    feature_dim = _axis(phone, 1)
    embedding = weight.get(EMBEDDING)
    info = checkpoint.get("info")
    training = info.strip() if isinstance(info, str) and info.strip() else None
    return {
        "readable": True,
        "version": version,
        "sample_rate": sample_rate,
        "sr_label": label if isinstance(label, str) else None,
        "rate_label_matches_config": bool(sample_rate is not None and label_rate == sample_rate),
        "f0": has_f0,
        "worker_class": WORKER_CLASS.get((version, has_f0)),
        "architecture_feature_dim": ARCH_DIM.get(version),
        "feature_dim": feature_dim,
        "embedding_rows": _axis(embedding, 0),
        "speaker_count": "NOT_ASSESSED",
        "training_metadata": training,
        "quality_acceptance": "NOT_ASSESSED",
        "listening_quality": "not_assessed",
    }


def read_checkpoint(path):
    if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
        return _unread("权重不存在或为空")
    before=os.stat(path)
    try:
        # Imported/user-supplied checkpoints must pass the restricted data-only
        # parser. `torch.load` can execute pickle globals before facts are read.
        from core.safe_checkpoint import load_rvc_checkpoint
        payload = load_rvc_checkpoint(path)
    except Exception as exc:
        return _unread(type(exc).__name__)
    after_parse=os.stat(path)
    before_identity=(before.st_size,before.st_mtime_ns,before.st_ctime_ns,before.st_ino)
    if before_identity!=(after_parse.st_size,after_parse.st_mtime_ns,after_parse.st_ctime_ns,after_parse.st_ino):
        return _unread('checkpoint changed during inspection')
    checkpoint_sha=_sha256(path)
    after_hash=os.stat(path)
    if before_identity!=(after_hash.st_size,after_hash.st_mtime_ns,after_hash.st_ctime_ns,after_hash.st_ino):
        return _unread('checkpoint changed during inspection')
    inspected = inspect_checkpoint(payload)
    inspected["bytes"] = os.path.getsize(path)
    inspected["sha256"] = checkpoint_sha
    inspected["parser_algorithm_version"] = 'safe-rvc-zip-tensor-v1'
    inspected["parser_source_sha256"] = _sha256(os.path.join(os.path.dirname(__file__),'safe_checkpoint.py'))
    try:
        from core.safe_checkpoint import validate_rvc_inference_checkpoint
        inspected["inference_guard"] = validate_rvc_inference_checkpoint(payload, complete=True)
        inspected["inference_compatible"] = True
        inspected["inference_incompatibility"] = None
    except Exception as exc:
        # A checkpoint may be safely readable while lacking required inference
        # weights. Keep that distinction visible to the model list.
        inspected["inference_guard"] = None
        inspected["inference_compatible"] = False
        inspected["inference_incompatibility"] = type(exc).__name__ + ": " + str(exc)
    return inspected


def read_index(path):
    missing = {"readable": False, "dimension": None, "count": None, "bytes": None}
    if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
        missing["reason"] = "index 不存在或为空"
        return missing
    try:
        # Native faiss.read_index parses untrusted binary state. The bounded
        # reader validates a strict Flat/IVFFlat subset before reconstruction.
        from core.safe_faiss_index import ALGORITHM_VERSION,load_faiss_index
        loaded = load_faiss_index(os.fspath(path))
    except Exception as exc:
        missing["reason"] = type(exc).__name__
        return missing
    return {
        "readable": True,
        "dimension": int(loaded.d),
        "count": int(loaded.ntotal),
        "bytes": os.path.getsize(path),
        "parser_algorithm_version": ALGORITHM_VERSION,
    }


def inspect_model(key, model, checkpoint, index_facts):
    """Combine one catalog record with checkpoint and index facts."""
    model = model if isinstance(model, dict) else {}
    checkpoint = checkpoint if isinstance(checkpoint, dict) else _unread("未提供 checkpoint")
    index_facts = index_facts if isinstance(index_facts, dict) else {"readable": False, "dimension": None, "count": None}
    profile = model.get("profile") if isinstance(model.get("profile"), dict) else {}
    local, config_path = override_files(model)
    feature_dim = checkpoint.get("feature_dim")
    index_dim = index_facts.get("dimension")
    if checkpoint.get("readable") and index_facts.get("readable") and feature_dim is not None and index_dim is not None:
        relation = "DIMENSION_MATCH" if feature_dim == index_dim else "DIMENSION_MISMATCH"
    else:
        relation = "NOT_ASSESSED"
    return {
        "schema_version": SCHEMA_VERSION,
        "key": key,
        "name": model.get("name"),
        "checkpoint": checkpoint,
        "index": {
            "readable": bool(index_facts.get("readable")),
            "dimension": index_dim,
            "count": index_facts.get("count"),
            "bytes": index_facts.get("bytes"),
        "dimension_relation": relation,
        "same_training_run": "NOT_ASSESSED",
        },
        "profile_matches_global_default": matches_defaults(profile),
        "profile_override": bool(local or config_path),
        "profile_calibrated": False,
        "calibration_status": "NOT_ASSESSED",
        "health": (
            "INFERENCE_COMPATIBLE" if checkpoint.get("inference_compatible") is True else
            "READABLE_NOT_RUNNABLE" if checkpoint.get("inference_compatible") is False else
            "INSPECTED" if checkpoint.get("readable") else "UNREADABLE"),
        "runnable": checkpoint.get("inference_compatible"),
        "quality_acceptance": "NOT_ASSESSED",
        "listening_quality": "not_assessed",
        "singing_suitability": "NOT_ASSESSED",
    }


def _axis(tensor, axis):
    shape = getattr(tensor, "shape", None)
    if shape is None or len(shape) <= axis:
        return None
    return int(shape[axis])


def _unread(reason):
    return {
        "readable": False,
        "reason": reason,
        "version": None,
        "sample_rate": None,
        "feature_dim": None,
        "speaker_count": "NOT_ASSESSED",
        "quality_acceptance": "NOT_ASSESSED",
        "listening_quality": "not_assessed",
    }


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
