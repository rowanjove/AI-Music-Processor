"""Measure the installed int16-to-float scale. Do not switch the worker.

rvc_worker.py converts an int16 result with `astype(np.float32) / 32768.`
and then writes a float WAV. This module repeats that scale for a measurement
and leaves an unchanged float array as an experiment that is not the default.
"""
import numpy as np

WORKER_SCALE = 32768.0
DEFAULT_PATH = "int16_div_32768"


def convert_worker_int16(samples):
    """Match the worker expression. The input must already be int16."""
    result = np.asarray(samples)
    if result.dtype != np.int16:
        raise ValueError("默认路径只接受 int16")
    result = result.astype(np.float32) / 32768.
    return result


def experimental_float_passthrough(samples):
    """Not the default. The worker still quantizes int16 before this exists."""
    return np.asarray(samples, dtype=np.float32)


def measure_int16_roundtrip(samples):
    """Compare the worker scale with an unquantized float copy.

    `switched` stays false. listening_quality is not a hearing result.
    """
    original = np.asarray(samples, dtype=np.float64)
    if original.ndim != 1 or original.size == 0 or not np.isfinite(original).all():
        raise ValueError("没有可测量的采样")
    limited = np.clip(original, -1.0, 32767.0 / WORKER_SCALE)
    quantized = np.clip(np.round(limited * WORKER_SCALE), -32768, 32767).astype(np.int16)
    restored = convert_worker_int16(quantized).astype(np.float64)
    experimental = experimental_float_passthrough(limited).astype(np.float64)
    return {
        "default_path": DEFAULT_PATH,
        "switched": False,
        "listening_quality": "not_assessed",
        "scale": WORKER_SCALE,
        "max_abs_error": float(np.max(np.abs(limited - restored))),
        "experimental_max_abs_error": float(np.max(np.abs(limited - experimental))),
    }
