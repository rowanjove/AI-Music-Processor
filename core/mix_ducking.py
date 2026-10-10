"""Bounded accompaniment ducking controlled by the vocal stem."""
import json
import math
import os
import uuid
from pathlib import Path

from core.config import FFMPEG_EXE
from core.processes import run
from core.storage import sha256_file
from core.analyzer import probe_metadata

ALGORITHM_VERSION = "vocal-sidechain-ducking-v1"
PROFILE = {
    "threshold_db": -30.0,
    "ratio": 2.0,
    "attack_ms": 30.0,
    "release_ms": 250.0,
    "makeup": 1.0,
}


def apply_vocal_ducking(vocal_path, instrumental_path, output_path, report_path=None):
    vocal_path = os.path.abspath(os.fspath(vocal_path))
    instrumental_path = os.path.abspath(os.fspath(instrumental_path))
    output_path = os.path.abspath(os.fspath(output_path))
    report_path = os.path.abspath(os.fspath(report_path or (output_path + ".ducking.json")))
    if not os.path.isfile(vocal_path) or not os.path.isfile(instrumental_path):
        raise FileNotFoundError("Ducking requires readable vocal and instrumental stems")
    if len({os.path.normcase(path) for path in (vocal_path, instrumental_path, output_path, report_path)}) != 4:
        raise ValueError("Ducking output and report must not overwrite an input or each other")
    if not all(math.isfinite(value) for value in PROFILE.values()):
        raise ValueError("Invalid frozen ducking profile")
    duration = float(probe_metadata(instrumental_path)["duration"])
    vocal_duration = float(probe_metadata(vocal_path)["duration"])
    if (not math.isfinite(duration) or not math.isfinite(vocal_duration) or
            duration <= 0 or vocal_duration <= 0):
        raise ValueError("Ducking stem duration is invalid")
    threshold = 10 ** (PROFILE["threshold_db"] / 20)
    graph = (
        "[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[inst];"
        "[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,apad=whole_dur=%.6f[voc];"
        "[inst][voc]sidechaincompress=threshold=%.9f:ratio=%.3f:attack=%.1f:release=%.1f:makeup=%.3f:mix=1,"
        "asetpts=PTS-STARTPTS,apad=whole_dur=%.6f,atrim=duration=%.6f[ducked]"
        % (duration, threshold, PROFILE["ratio"], PROFILE["attack_ms"], PROFILE["release_ms"],
           PROFILE["makeup"], duration, duration)
    )
    command = [FFMPEG_EXE, "-y", "-hide_banner", "-v", "error",
               "-i", instrumental_path, "-i", vocal_path,
               "-filter_complex", graph, "-map", "[ducked]",
               "-c:a", "pcm_f32le", "-ar", "48000", "-ac", "2", output_path]
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    temporary_audio = output_path + ".part." + uuid.uuid4().hex + ".wav"
    temporary_report = report_path + ".part." + uuid.uuid4().hex
    try:
        command[-1] = temporary_audio
        run(command, check=True)
        if not os.path.isfile(temporary_audio) or os.path.getsize(temporary_audio) < 44:
            raise RuntimeError("FFmpeg did not produce a valid ducked accompaniment")
        with open(temporary_audio, "rb") as handle:
            header = handle.read(44)
        if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise RuntimeError("Ducking output is not a WAV file")
        info = __import__("soundfile").info(temporary_audio)
        if (info.samplerate != 48000 or info.channels != 2 or info.subtype != "FLOAT" or
                abs(info.duration-duration) > max(0.02, duration*0.001)):
            raise RuntimeError("Ducking output format/duration mismatch: %s Hz, %s channels, %s, %.6f sec expected %.6f" %
                               (info.samplerate, info.channels, info.subtype, info.duration, duration))
        report = {
            "schema_version": 1,
            "algorithm": ALGORITHM_VERSION,
            "enabled": True,
            "settings": dict(PROFILE),
            "vocal_sha256": sha256_file(vocal_path),
            "instrumental_sha256": sha256_file(instrumental_path),
            "output_sha256": sha256_file(temporary_audio),
            "result": "APPLIED",
            "quality_acceptance": "NOT_ASSESSED",
        }
        with open(temporary_report, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, allow_nan=False, indent=2)
        os.replace(temporary_audio, output_path)
        os.replace(temporary_report, report_path)
        return {"instrumental": output_path, "report_path": report_path, "report": report}
    finally:
        for temporary in (temporary_audio, temporary_report):
            if os.path.exists(temporary):
                os.remove(temporary)
