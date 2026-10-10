"""Conservative same-source separator ensemble for experiments only.

The helper keeps each source and backend stem intact.  The combined output is
only an arithmetic mean of two independently generated, sample-aligned WAVs;
it carries no quality or job-selection claim.
"""
from __future__ import absolute_import

import os
import shutil
import tempfile


SAMPLE_RATE = 48000


def sha256_file(path):
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_stereo_float(path, *, expected_frames=None, expected_rate=SAMPLE_RATE):
    """Read a finite stereo float WAV and optionally require exact frame count."""
    import numpy as np
    import soundfile as sf

    info = sf.info(str(path))
    if info.subtype != "FLOAT":
        raise ValueError("分轨实验 WAV 必须使用 IEEE FLOAT 子类型：" + str(path))
    audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
    if rate != expected_rate:
        raise ValueError("分轨实验采样率不符：{} != {}".format(rate, expected_rate))
    if audio.ndim != 2 or audio.shape[1] != 2 or audio.shape[0] <= 0:
        raise ValueError("分轨实验产物必须是非空双声道 WAV")
    if not np.isfinite(audio).all():
        raise FloatingPointError("分轨实验产物包含 NaN/Inf")
    if expected_frames is not None and audio.shape[0] != expected_frames:
        raise ValueError("分轨实验帧数不符：{} != {}".format(audio.shape[0], expected_frames))
    return audio, int(rate)


def inspect_raw_stereo(path):
    """Validate engine output before FFmpeg can silently remix channels."""
    import numpy as np
    import soundfile as sf

    info = sf.info(str(path))
    if info.format != "WAV" or info.channels != 2 or info.frames <= 0 or info.samplerate <= 0:
        raise ValueError("模型原始输出必须是非空双声道 WAV：" + str(path))
    audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
    if audio.shape != (info.frames, 2) or not np.isfinite(audio).all():
        raise FloatingPointError("模型原始输出形状无效或包含 NaN/Inf：" + str(path))
    return {"frames": int(info.frames), "sample_rate": int(rate), "channels": 2,
            "subtype": info.subtype, "duration_seconds": float(info.frames) / float(rate)}


def standardize_source(source_path, output_path):
    """Create the common 48 kHz stereo FLOAT source without changing source."""
    from core.analyzer import standardize_to_48k_float

    source = _regular_file(source_path, "实验源音频")
    target = _new_output(output_path, source)
    source_sha = sha256_file(source)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    standardize_to_48k_float(source, target)
    if sha256_file(source) != source_sha:
        _unlink_if_present(target)
        raise RuntimeError("规范化期间原始实验源发生变化")
    audio, rate = inspect_stereo_float(target)
    return {
        "path": target,
        "sha256": sha256_file(target),
        "frames": int(audio.shape[0]),
        "sample_rate": rate,
        "channels": 2,
        "source_sha256_before": source_sha,
        "source_sha256_after": sha256_file(source),
    }


def standardize_stem_to_reference(stem_path, output_path, reference_frames):
    """Resample a backend stem, permitting only one-sample end padding.

    Long results are rejected rather than trimmed.  That prevents a duration
    mismatch from being hidden by truncation.
    """
    import numpy as np
    import soundfile as sf

    from core.analyzer import standardize_to_48k_float

    source = _regular_file(stem_path, "模型分轨")
    raw_info = inspect_raw_stereo(source)
    target = _new_output(output_path, source)
    if type(reference_frames) is not int or reference_frames <= 0:
        raise ValueError("参考帧数无效")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    temporary_root = tempfile.mkdtemp(prefix=".ensemble-normalize-", dir=os.path.dirname(target))
    temporary = os.path.join(temporary_root, "normalized.wav")
    try:
        standardize_to_48k_float(source, temporary)
        audio, rate = inspect_stereo_float(temporary)
        difference = reference_frames - int(audio.shape[0])
        adjustment = {"kind": "none", "samples": 0}
        if difference == 1:
            audio = np.concatenate((audio, audio[-1:, :]), axis=0)
            adjustment = {"kind": "pad_end_repeat_last_frame", "samples": 1}
            padded = os.path.join(temporary_root, "padded.wav")
            sf.write(padded, audio, rate, format="WAV", subtype="FLOAT")
            os.replace(padded, target)
        elif difference == 0:
            os.replace(temporary, target)
        else:
            if difference == -1:
                raise ValueError("模型输出多 1 帧；实验不会截短或掩盖时长差异")
            raise ValueError("模型输出与共同源的时长差异超过 1 帧：{}".format(-difference))
        final_audio, final_rate = inspect_stereo_float(target, expected_frames=reference_frames)
        return {
            "path": target,
            "sha256": sha256_file(target),
            "frames": int(final_audio.shape[0]),
            "sample_rate": final_rate,
            "channels": 2,
            "frame_adjustment": adjustment,
            "raw_output": raw_info,
        }
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def combine_aligned_mean(reference_path, hp2_path, dual_path, output_path):
    """Write the finite sample-wise mean of two exact-shape stereo stems."""
    import numpy as np
    import soundfile as sf

    reference = _regular_file(reference_path, "共同规范化源")
    hp2 = _regular_file(hp2_path, "HP2 规范化 stem")
    dual = _regular_file(dual_path, "dual 规范化 stem")
    target = _new_output(output_path, reference)
    reference_audio, _ = inspect_stereo_float(reference)
    hp2_audio, _ = inspect_stereo_float(hp2, expected_frames=len(reference_audio))
    dual_audio, _ = inspect_stereo_float(dual, expected_frames=len(reference_audio))
    if hp2_audio.shape != dual_audio.shape or hp2_audio.shape != reference_audio.shape:
        raise ValueError("ensemble 输入形状不完全相同")
    result = ((hp2_audio.astype(np.float64) + dual_audio.astype(np.float64)) * 0.5).astype(np.float32)
    if not np.isfinite(result).all():
        raise FloatingPointError("ensemble 均值包含 NaN/Inf")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    temporary = target + ".part.wav"
    try:
        sf.write(temporary, result, SAMPLE_RATE, format="WAV", subtype="FLOAT")
        os.replace(temporary, target)
    finally:
        _unlink_if_present(temporary)
    final, _ = inspect_stereo_float(target, expected_frames=len(reference_audio))
    return {
        "path": target,
        "sha256": sha256_file(target),
        "frames": int(final.shape[0]),
        "sample_rate": SAMPLE_RATE,
        "channels": 2,
        "combiner": "samplewise_arithmetic_mean",
        "quality_acceptance": "NOT_ASSESSED",
        "listening_quality": "not_assessed",
    }


def _regular_file(path, label):
    target = os.path.abspath(os.fspath(path))
    if os.path.islink(target) or not os.path.isfile(target):
        raise FileNotFoundError(label + " 不存在或不是普通文件：" + target)
    return target


def _new_output(path, protected_path):
    target = os.path.abspath(os.fspath(path))
    protected = os.path.abspath(os.fspath(protected_path))
    if os.path.normcase(target) == os.path.normcase(protected):
        raise ValueError("分轨实验输出不能覆盖源或参考文件")
    if os.path.lexists(target):
        raise FileExistsError("分轨实验拒绝覆盖已有文件：" + target)
    return target


def _unlink_if_present(path):
    try:
        os.remove(path)
    except OSError:
        pass
