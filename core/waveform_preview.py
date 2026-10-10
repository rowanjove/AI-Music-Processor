"""Bounded mono waveform summaries for the desktop preview canvas."""
import math
import os
import subprocess

import numpy as np

from core.analyzer import probe_metadata
from core.config import FFMPEG_EXE

MAX_DURATION_SECONDS = 30 * 60
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
PREVIEW_RATE = 1000


def waveform_envelope(path, buckets=600, timeout=30):
    """Decode at 1 kHz mono and return min/max points without retaining full audio."""
    if type(buckets) is not int or not 32 <= buckets <= 2000:
        raise ValueError("waveform buckets must be between 32 and 2000")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 60:
        raise ValueError("waveform timeout must be between 1 and 60 seconds")
    path = os.path.abspath(os.fspath(path))
    metadata = probe_metadata(path)
    duration = float(metadata["duration"])
    if not math.isfinite(duration) or duration <= 0 or duration > MAX_DURATION_SECONDS:
        raise ValueError("Waveform preview is limited to audio up to 30 minutes")
    expected_bytes = math.ceil(duration * PREVIEW_RATE) * 4 + PREVIEW_RATE * 4
    if expected_bytes > MAX_PREVIEW_BYTES:
        raise ValueError("Waveform preview exceeds its decoded-memory budget")
    command = [FFMPEG_EXE, "-hide_banner", "-v", "error", "-i", path,
               "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(PREVIEW_RATE),
               "-f", "f32le", "pipe:1"]
    completed = subprocess.run(
        command, stdin=subprocess.DEVNULL, capture_output=True,
        timeout=float(timeout), check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    payload = completed.stdout
    if not payload or len(payload) % 4 or len(payload) > MAX_PREVIEW_BYTES:
        raise ValueError("FFmpeg returned an invalid or oversized waveform")
    signal = np.frombuffer(payload, dtype="<f4")
    if not signal.size or not np.isfinite(signal).all():
        raise ValueError("Decoded waveform is empty or contains nonfinite samples")
    edges = np.linspace(0, signal.size, min(buckets, signal.size) + 1, dtype=np.int64)
    points = []
    for start, end in zip(edges[:-1], edges[1:]):
        block = signal[start:max(start + 1, end)]
        points.append([float(np.min(block)), float(np.max(block))])
    return {
        "sample_rate": PREVIEW_RATE,
        "duration_seconds": float(signal.size / PREVIEW_RATE),
        "points": points,
        "peak": float(np.max(np.abs(signal))),
        "source_path": path,
        "quality_acceptance": "NOT_ASSESSED",
    }
