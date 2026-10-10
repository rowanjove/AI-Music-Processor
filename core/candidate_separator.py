"""Strict candidate adapters for official RoFormer separator APIs.

These adapters are deliberately outside the selectable job route. Callers must
pass an immutable asset identity captured before the experiment; the adapter
checks code, weight and configuration bytes before loading and again before
publishing outputs. There is no fallback to HP2.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


ROFORMER = "roformer"
PYMSS = "pymss"
ROFORMER_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
ROFORMER_CONFIG = "model_bs_roformer_ep_317_sdr_12.9755.yaml"
PYMSS_MODEL_TYPE = "bs_roformer"
PYMSS_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
PYMSS_CONFIG = "model_bs_roformer_ep_317_sdr_12.9755.yaml"
_BACKENDS = {
    ROFORMER: {
        "package": "audio_separator",
        "api": "audio_separator.separator.Separator.load_model/separate",
        "model": ROFORMER_MODEL,
        "config": ROFORMER_CONFIG,
        "support_files": ("download_checks.json",),
    },
    PYMSS: {
        "package": "pymss",
        "api": "pymss.MSSeparator.separate",
        "model": PYMSS_MODEL,
        "config": PYMSS_CONFIG,
        "support_files": (),
    },
}


class CandidateBackendUnavailable(RuntimeError):
    """The fixed candidate implementation or its local assets are unavailable."""


class CandidateIdentityError(ValueError):
    """The frozen candidate identity does not match the bytes being executed."""


def package_source_sha256(package_name):
    """Hash a fixed installed package tree without importing its code."""
    spec = importlib.util.find_spec(package_name)
    if spec is None:
        raise CandidateBackendUnavailable("缺少候选分离依赖：" + package_name)
    roots = list(spec.submodule_search_locations or ())
    if not roots and spec.origin:
        roots = [os.path.dirname(os.path.abspath(spec.origin))]
    if len(roots) != 1:
        raise CandidateBackendUnavailable("候选分离依赖路径不唯一：" + package_name)
    root = Path(roots[0]).resolve()
    if not root.is_dir() or root.is_symlink():
        raise CandidateBackendUnavailable("候选分离依赖目录无效：" + package_name)
    digest = hashlib.sha256()
    rows = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise CandidateIdentityError("候选依赖目录包含符号链接：" + str(path))
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root).as_posix()
        rows.append((relative, path))
    for relative, path in sorted(rows, key=lambda item: item[0]):
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(sha256_file(path)))
    if not rows:
        raise CandidateBackendUnavailable("候选依赖包没有可校验文件：" + package_name)
    return digest.hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_candidate_separator(backend, source_wav, output_dir, frozen_identity, *, device="auto"):
    """Run one fixed RoFormer candidate and atomically publish its two stems.

    ``frozen_identity`` is part of the experiment snapshot, not a live GUI
    default. Required fields are ``api_package_sha256``, ``weight_path``,
    ``weight_sha256``, ``config_path``, ``config_sha256`` and (for RoFormer)
    ``support_file_sha256``. The model filename/type and stem names are fixed
    by the adapter. This records execution evidence only; it does not assess
    audio quality or make the candidate selectable.
    """
    if not isinstance(backend, str) or backend not in _BACKENDS:
        raise ValueError("未知候选分离后端：" + str(backend))
    if sys.version_info < (3, 10):
        raise CandidateBackendUnavailable("候选官方分离包要求 Python 3.10 或更新版本")
    spec = _BACKENDS[backend]
    if not isinstance(frozen_identity, dict) or frozen_identity.get("backend") != backend:
        raise CandidateIdentityError("候选分离身份缺少冻结的 backend 记录")
    source = _regular_file(source_wav, "输入音频")
    assets = _verify_identity(backend, spec, frozen_identity)
    _verify_managed_inventory(spec, assets, frozen_identity)
    target_dir = Path(output_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    vocal_out = target_dir / (backend + "_vocal.wav")
    instrumental_out = target_dir / (backend + "_instrumental.wav")
    if vocal_out.exists() or instrumental_out.exists():
        raise FileExistsError("候选输出已存在；为保护已有产物拒绝覆盖")
    source_sha = sha256_file(source)
    work = Path(tempfile.mkdtemp(prefix="." + backend + "-candidate-", dir=str(target_dir)))
    staged_vocal = work / "vocal.wav"
    staged_instrumental = work / "instrumental.wav"
    try:
        if backend == ROFORMER:
            _run_audio_separator(source, work, assets, staged_vocal, staged_instrumental, frozen_identity)
        else:
            _run_pymss(source, assets, staged_vocal, staged_instrumental, frozen_identity, device)
        if sha256_file(source) != source_sha:
            raise CandidateIdentityError("推理期间输入源发生变化，拒绝提交候选产物")
        _verify_identity(backend, spec, frozen_identity)
        _validate_wav(staged_vocal)
        _validate_wav(staged_instrumental)
        committed = []
        try:
            os.replace(staged_vocal, vocal_out)
            committed.append(vocal_out)
            os.replace(staged_instrumental, instrumental_out)
            committed.append(instrumental_out)
        except Exception:
            for path in committed:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        return {
            "status": "EXECUTED",
            "backend": backend,
            "api": spec["api"],
            "model_filename": spec["model"],
            "quality_acceptance": "NOT_ASSESSED",
            "listening_quality": "not_assessed",
            "source_sha256": source_sha,
            "api_package_sha256": frozen_identity["api_package_sha256"],
            "weight_sha256": frozen_identity["weight_sha256"],
            "config_sha256": frozen_identity["config_sha256"],
            "vocal": _artifact(vocal_out),
            "instrumental": _artifact(instrumental_out),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _verify_identity(backend, spec, identity):
    required = ("api_package_sha256", "weight_path", "weight_sha256", "config_path", "config_sha256")
    missing = [key for key in required if key not in identity]
    if missing:
        raise CandidateIdentityError("候选身份未冻结：" + ", ".join(missing))
    if identity.get("model_filename", spec["model"]) != spec["model"]:
        raise CandidateIdentityError("候选模型名与固定适配器不一致")
    package_hash = package_source_sha256(spec["package"])
    if not _is_sha(identity["api_package_sha256"]) or package_hash != identity["api_package_sha256"]:
        raise CandidateIdentityError("候选 API 包源码哈希与冻结身份不一致")
    weight = _verify_file_identity(identity["weight_path"], identity["weight_sha256"], "候选权重")
    config = _verify_file_identity(identity["config_path"], identity["config_sha256"], "候选配置")
    if Path(weight).name != spec["model"] or Path(config).name != spec["config"]:
        raise CandidateIdentityError("候选权重或配置文件名与固定适配器不一致")
    support = identity.get("support_file_sha256", {})
    if backend == ROFORMER:
        if Path(weight).parent != Path(config).parent:
            raise CandidateIdentityError("RoFormer 权重和模型参数必须位于同一冻结目录")
        if not isinstance(support, dict):
            raise CandidateIdentityError("audio-separator 缺少冻结的模型目录支持文件哈希")
        for name in spec["support_files"]:
            expected = support.get(name)
            path = Path(weight).parent / name
            _verify_file_identity(path, expected, "候选模型支持文件 " + name)
        if config.stat().st_size <= 0:
            raise CandidateIdentityError("RoFormer YAML 配置为空")
    else:
        stems = identity.get("stems")
        if stems != {"vocal": "vocals", "instrumental": "instrumental"}:
            raise CandidateIdentityError("PyMSS 当前固定适配要求 vocals/instrumental stem")
    return {"weight": weight, "config": config}


def _verify_managed_inventory(spec, assets, identity):
    """Require every candidate asset and API source file to be bundle-managed."""
    root_value = identity.get("managed_root")
    if not isinstance(root_value, str) or not os.path.isabs(root_value):
        raise CandidateIdentityError("候选分离身份缺少绝对 managed_root")
    root = Path(root_value).resolve()
    inventory_path = root / "runtime-manifest" / "inventory.json"
    _regular_file(inventory_path, "托管运行清单")
    try:
        with open(inventory_path, "r", encoding="utf-8") as handle:
            rows = json.load(handle).get("files")
    except (OSError, ValueError, AttributeError) as exc:
        raise CandidateIdentityError("托管运行清单无效：" + str(exc)) from exc
    if not isinstance(rows, list):
        raise CandidateIdentityError("托管运行清单没有文件表")
    trusted = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or not _is_sha(row.get("sha256")):
            raise CandidateIdentityError("托管运行清单有无效条目")
        key = row["path"].replace("\\", "/").casefold()
        if key in trusted:
            raise CandidateIdentityError("托管运行清单有重复路径")
        trusted[key] = row["sha256"]
    package_spec = importlib.util.find_spec(spec["package"])
    package_roots = list(package_spec.submodule_search_locations or ()) if package_spec else []
    if len(package_roots) != 1:
        raise CandidateBackendUnavailable("候选 API 包路径不可定位：" + spec["package"])
    package_root = Path(package_roots[0]).resolve()
    paths = [assets["weight"], assets["config"]]
    paths.extend(str(Path(assets["weight"]).parent / name) for name in spec["support_files"])
    paths.extend(str(path) for path in package_root.rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    for value in paths:
        path = _regular_file(value, "托管候选文件").resolve()
        try:
            relative = path.relative_to(root).as_posix().casefold()
        except ValueError as exc:
            raise CandidateIdentityError("候选代码或模型位于程序托管目录之外：" + str(path)) from exc
        expected = trusted.get(relative)
        if expected is None or expected != sha256_file(path):
            raise CandidateIdentityError("候选文件没有匹配的托管 inventory 哈希：" + relative)


def _run_audio_separator(source, work, assets, vocal_out, instrumental_out, identity):
    try:
        from audio_separator.separator import Separator
    except ImportError as exc:
        raise CandidateBackendUnavailable("未安装 audio-separator；RoFormer 候选未运行") from exc
    model_dir = os.path.dirname(assets["weight"])
    configured_model_dir = os.environ.get("AUDIO_SEPARATOR_MODEL_DIR")
    if configured_model_dir and os.path.abspath(configured_model_dir) != os.path.abspath(model_dir):
        raise CandidateIdentityError("AUDIO_SEPARATOR_MODEL_DIR 与冻结的托管模型目录冲突，拒绝改写全局环境")
    separator = Separator(
        model_file_dir=model_dir,
        output_dir=str(work),
        output_format="WAV",
        normalization_threshold=1.0,
        amplification_threshold=0.0,
        use_soundfile=True,
    )
    if os.path.abspath(getattr(separator, "model_file_dir", model_dir)) != os.path.abspath(model_dir):
        raise CandidateIdentityError("audio-separator 模型目录未采用冻结的托管路径")

    # The public loader calls this even for files already on disk. Permit only
    # exact frozen assets in the managed directory; never allow network access.
    def local_frozen_asset(_url, path):
        return _verify_local_frozen_asset(path, assets, identity)

    separator.download_file_if_not_exists = local_frozen_asset
    try:
        separator.load_model(model_filename=ROFORMER_MODEL, force_reload=True)
        outputs = separator.separate(str(source), custom_output_names={
            "Vocals": "candidate_vocal",
            "Instrumental": "candidate_instrumental",
        })
        if not isinstance(outputs, (list, tuple)):
            raise RuntimeError("audio-separator 未返回输出路径列表")
        resolved_outputs = [_resolve_candidate_output(path, work) for path in outputs]
        by_name = {path.stem.casefold(): path for path in resolved_outputs}
        vocal = next((path for name, path in by_name.items() if "candidate_vocal" in name), None)
        instrumental = next((path for name, path in by_name.items() if "candidate_instrumental" in name), None)
        if vocal is None or instrumental is None or vocal == instrumental:
            raise RuntimeError("audio-separator 未生成两个明确命名的人声/伴奏输出")
        shutil.copyfile(vocal, vocal_out)
        shutil.copyfile(instrumental, instrumental_out)
    finally:
        close = getattr(separator, "close", None)
        if callable(close):
            close()
        del separator


def _run_pymss(source, assets, vocal_out, instrumental_out, identity, device):
    if sys.version_info < (3, 10):
        raise CandidateBackendUnavailable("当前 PyMSS 官方包要求 Python 3.10 或更新版本")
    try:
        from pymss import MSSeparator
    except ImportError as exc:
        raise CandidateBackendUnavailable("未安装 pymss；PyMSS 候选未运行") from exc
    import numpy as np
    import soundfile as sf

    separator = MSSeparator(
        model_type=PYMSS_MODEL_TYPE,
        model_path=str(assets["weight"]),
        config_path=str(assets["config"]),
        device=device,
        output_format="wav",
        use_tta=False,
        store_dirs={},
        audio_params={"wav_bit_depth": "FLOAT"},
        inference_params={"normalize": False, "standardize": False},
    )
    try:
        audio, source_rate = sf.read(str(source), dtype="float32", always_2d=True)
        if not np.isfinite(audio).all():
            raise FloatingPointError("输入音频包含 NaN/Inf")
        model_rate = _pymss_sample_rate(separator)
        if source_rate != model_rate:
            from scipy.signal import resample_poly
            import math
            divisor = math.gcd(int(source_rate), int(model_rate))
            audio = resample_poly(audio, model_rate // divisor, source_rate // divisor, axis=0)
        stems = identity["stems"]
        results = separator.separate(np.ascontiguousarray(audio, dtype=np.float32), pbar=False,
                                     stems=[stems["vocal"], stems["instrumental"]])
        if not isinstance(results, dict) or any(name not in results for name in stems.values()):
            raise RuntimeError("PyMSS 没有返回已冻结的人声与伴奏 stem")
        converted = []
        for name in (stems["vocal"], stems["instrumental"]):
            value = np.asarray(results[name], dtype=np.float32)
            if value.ndim not in (1, 2) or not value.size or not np.isfinite(value).all():
                raise FloatingPointError("PyMSS 输出形状无效或包含 NaN/Inf：" + name)
            if source_rate != model_rate:
                from scipy.signal import resample_poly
                import math
                divisor = math.gcd(int(model_rate), int(source_rate))
                value = resample_poly(value, source_rate // divisor, model_rate // divisor, axis=0)
            converted.append(np.ascontiguousarray(value, dtype=np.float32))
        sf.write(str(vocal_out), converted[0], int(source_rate), format="WAV", subtype="FLOAT")
        sf.write(str(instrumental_out), converted[1], int(source_rate), format="WAV", subtype="FLOAT")
    finally:
        close = getattr(separator, "close", None)
        if callable(close):
            close()
        del separator


def _pymss_sample_rate(separator):
    config = getattr(separator, "config", None)
    audio = config.get("audio") if isinstance(config, dict) else getattr(config, "audio", None)
    value = audio.get("sample_rate") if isinstance(audio, dict) else getattr(audio, "sample_rate", None)
    if type(value) is not int or value < 8000 or value > 192000:
        raise CandidateIdentityError("PyMSS 模型配置缺少有效 audio.sample_rate")
    training = config.get("training") if isinstance(config, dict) else getattr(config, "training", None)
    instruments = training.get("instruments") if isinstance(training, dict) else getattr(training, "instruments", None)
    if not isinstance(instruments, (list, tuple)) or not {"vocals", "instrumental"}.issubset(set(instruments)):
        raise CandidateIdentityError("PyMSS 模型配置不包含 vocals/instrumental 两个输出")
    return value


def _verify_file_identity(path, expected, label):
    if not _is_sha(expected):
        raise CandidateIdentityError(label + " 缺少有效的冻结 SHA-256")
    target = _regular_file(path, label)
    if sha256_file(target) != expected:
        raise CandidateIdentityError(label + " 与冻结 SHA-256 不一致")
    return target


def _regular_file(path, label):
    target = Path(os.path.abspath(os.fspath(path)))
    if target.is_symlink() or not target.is_file():
        raise FileNotFoundError(label + " 不存在或不是普通文件：" + str(target))
    for parent in target.parents:
        if parent.is_symlink():
            raise CandidateIdentityError(label + " 路径包含符号链接：" + str(parent))
    return target


def _is_sha(value):
    return (isinstance(value, str) and len(value) == 64 and
            all(char in "0123456789abcdef" for char in value))


def _verify_local_frozen_asset(path, assets, identity):
    target = Path(os.path.abspath(os.fspath(path)))
    expected_by_path = {
        Path(assets["weight"]).resolve(): identity.get("weight_sha256"),
        Path(assets["config"]).resolve(): identity.get("config_sha256"),
    }
    if identity.get("backend") == ROFORMER:
        model_dir = Path(assets["weight"]).resolve().parent
        for name, digest in (identity.get("support_file_sha256") or {}).items():
            if name not in _BACKENDS[ROFORMER]["support_files"]:
                continue
            expected_by_path[(model_dir / name).resolve()] = digest
    expected = expected_by_path.get(target.resolve())
    if expected is None:
        raise CandidateBackendUnavailable("候选推理拒绝访问未冻结的模型目录文件：" + str(target))
    _verify_file_identity(target, expected, "冻结候选文件")
    return None


def _resolve_candidate_output(value, work):
    if not isinstance(value, (str, os.PathLike)):
        raise RuntimeError("audio-separator 返回了无效输出路径")
    raw = Path(os.fspath(value))
    candidate = raw if raw.is_absolute() else Path(work) / raw
    candidate = Path(os.path.abspath(candidate))
    root = Path(work).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise CandidateIdentityError("audio-separator 输出路径离开本次工作目录：" + str(candidate)) from exc
    _regular_file(candidate, "候选分离输出")
    return candidate


def _validate_wav(path):
    import numpy as np
    import soundfile as sf

    info = sf.info(str(path))
    if info.format != "WAV" or info.frames <= 0 or info.channels not in (1, 2) or info.samplerate <= 0:
        raise RuntimeError("候选分离产物不是有效的单/双声道 WAV")
    samples, _ = sf.read(str(path), dtype="float32", always_2d=True)
    if not np.isfinite(samples).all():
        raise FloatingPointError("候选分离产物包含 NaN/Inf")


def _artifact(path):
    import soundfile as sf
    info = sf.info(str(path))
    return {
        "path": str(path),
        "bytes": os.path.getsize(path),
        "sha256": sha256_file(path),
        "sample_rate": int(info.samplerate),
        "channels": int(info.channels),
        "duration_seconds": float(info.duration),
    }
