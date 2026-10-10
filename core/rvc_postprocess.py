"""Nullable shaping after RVC and before Align.

Empty leaves the RVC file untouched, so Align still reads today's bytes.
Enabled writes a separate file. It does not use Mix echo or the compressor,
and it does not overwrite the raw RVC output.
"""
import os

import numpy as np
import soundfile as sf

# Opt-in only. Half scale makes the stage measurable. It is not a mix
# setting and not a listening recommendation.
POSTPROCESS_GAIN = 0.5


def validate_rvc_postprocess(value):
    if type(value) is not bool:
        raise ValueError("rvc_postprocess 必须是布尔值")
    return value


def resolve_rvc_postprocess(parameters):
    """Missing means the historical path: no postprocess."""
    if not isinstance(parameters, dict) or "rvc_postprocess" not in parameters:
        return False
    return validate_rvc_postprocess(parameters["rvc_postprocess"])


def apply_rvc_postprocess(source, dest):
    """Write the enabled stage. The source file stays byte-for-byte."""
    if os.path.abspath(source) == os.path.abspath(dest):
        raise ValueError("RVC 后处理不能覆盖原始输出")
    audio, sample_rate = sf.read(source, dtype="float32", always_2d=True)
    if not len(audio) or not np.isfinite(audio).all():
        raise ValueError("RVC 后处理拒绝空音频或 NaN/Inf")
    scaled = (audio.astype(np.float64) * POSTPROCESS_GAIN).astype(np.float32)
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if scaled.shape[1] == 1:
        sf.write(dest, scaled[:, 0], sample_rate, subtype="FLOAT")
    else:
        sf.write(dest, scaled, sample_rate, subtype="FLOAT")
    return dest


def prepare_align_input(source, dest, enabled):
    """Return the path Align should read. Empty returns the original path."""
    if not enabled:
        return source
    return apply_rvc_postprocess(source, dest)
