import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.voice_observation import (
    compare_observations,
    load_observation,
    measure_voice_chain,
    resolve_project_points,
)

BEFORE = ROOT / "tests" / "fixtures" / "voice_chain_before.json"
SHORT_PROJECT = (
    "output/acceptance/audit_repair/验收 中文短样本_20261004_192142_144337"
)


def _rms(samples):
    data = np.asarray(samples, dtype=np.float64)
    if data.ndim == 1:
        data = data[:, None]
    energy = (data * data).sum(axis=0)
    return np.sqrt(energy / len(data)).tolist()


class VoiceObservationTests(unittest.TestCase):
    def test_repeat_measure_matches_known_signal(self):
        sr = 48000
        count = 480
        pre = np.zeros(count, np.float32)
        pre[0] = np.float32(0.5)
        pre[1] = np.float32(-0.25)
        rvc = np.zeros(count, np.float32)
        rvc[0] = np.float32(1.0)
        rvc[1] = np.float32(-1.0)
        rvc[2] = np.float32(0.5)
        mix = np.zeros((count, 2), np.float32)
        mix[0, 0] = np.float32(0.25)
        mix[0, 1] = np.float32(-0.5)
        with tempfile.TemporaryDirectory(prefix="voice-obs-") as folder:
            paths = {
                "pre_separation": os.path.join(folder, "01_standard_input.wav"),
                "rvc_output": os.path.join(folder, "03_vocal_rvc_fixture.wav"),
                "mix_output": os.path.join(folder, "06_mixed.wav"),
            }
            sf.write(paths["pre_separation"], pre, sr, subtype="FLOAT")
            sf.write(paths["rvc_output"], rvc, sr, subtype="FLOAT")
            sf.write(paths["mix_output"], mix, sr, subtype="FLOAT")
            first = measure_voice_chain(paths)
            second = measure_voice_chain(paths)
            self.assertEqual(first, second)
            self.assertEqual(first["listening_quality"], "not_assessed")
            self.assertEqual(first["points"]["pre_separation"]["sample_peak"], 0.5)
            self.assertEqual(first["points"]["pre_separation"]["clipped_samples"], 0)
            self.assertEqual(first["points"]["pre_separation"]["channel_rms"], _rms(pre))
            self.assertEqual(first["points"]["pre_separation"]["duration_seconds"], count / sr)
            self.assertEqual(first["points"]["rvc_output"]["sample_peak"], 1.0)
            self.assertEqual(first["points"]["rvc_output"]["clipped_samples"], 2)
            self.assertEqual(first["points"]["rvc_output"]["channel_rms"], _rms(rvc))
            self.assertEqual(first["points"]["mix_output"]["sample_peak"], 0.5)
            self.assertEqual(first["points"]["mix_output"]["clipped_samples"], 0)
            self.assertEqual(first["points"]["mix_output"]["channel_rms"], _rms(mix))
            self.assertEqual(first["points"]["mix_output"]["channels"], 2)

    def test_compare_is_numeric_and_makes_no_quality_claim(self):
        before = _chain(peak=0.25, clipped=0)
        after = _chain(peak=0.5, clipped=3)
        delta = compare_observations(before, after)
        self.assertEqual(delta["kind"], "voice_chain_delta")
        self.assertEqual(delta["listening_quality"], "not_assessed")
        self.assertNotIn("improved", delta)
        self.assertNotIn("quality_score", delta)
        self.assertEqual(delta["points"]["rvc_output"]["sample_peak"], 0.25)
        self.assertEqual(delta["points"]["rvc_output"]["clipped_samples"], 3)
        self.assertEqual(delta["points"]["pre_separation"]["duration_seconds"], 0.0)

    def test_missing_point_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="voice-obs-") as folder:
            path = os.path.join(folder, "01_standard_input.wav")
            sf.write(path, np.zeros(48, np.float32), 48000, subtype="FLOAT")
            with self.assertRaises(ValueError):
                measure_voice_chain({"pre_separation": path})
            with self.assertRaises(ValueError):
                resolve_project_points(folder)

    def test_committed_short_chain_before_is_stable(self):
        record = load_observation(str(BEFORE))
        self.assertEqual(record["role"], "before")
        self.assertEqual(record["listening_quality"], "not_assessed")
        self.assertEqual(record["measured_from"], "existing_completed_project")
        self.assertEqual(record["project_relative"], SHORT_PROJECT)
        self.assertEqual(record["context"]["mode"], "voice_conversion")
        self.assertEqual(record["context"]["rvc_model"], "zhoujielun")
        self.assertEqual(record["context"]["f0_method"], "harvest")
        project = ROOT / SHORT_PROJECT
        self.assertTrue(project.is_dir(), "before 使用的短样本工程不在磁盘上")
        paths = resolve_project_points(str(project))
        first = measure_voice_chain(paths)
        second = measure_voice_chain(paths)
        self.assertEqual(first, second)
        for key in ("pre_separation", "rvc_output", "mix_output"):
            saved = record["points"][key]
            measured = first["points"][key]
            for name in ("duration_seconds", "sample_peak", "channel_rms", "clipped_samples", "sha256"):
                self.assertEqual(measured[name], saved[name], key + " " + name)
        text = BEFORE.read_text(encoding="utf-8")
        self.assertNotIn("听感变好", text)
        self.assertNotIn("improved", text)


def _chain(peak, clipped):
    rms = [0.1]
    return {
        "schema_version": 1,
        "kind": "voice_chain_observation",
        "listening_quality": "not_assessed",
        "points": {
            key: {
                "duration_seconds": 1.0,
                "sample_peak": peak,
                "channel_rms": rms,
                "clipped_samples": clipped,
            }
            for key in ("pre_separation", "rvc_output", "mix_output")
        },
    }


if __name__ == "__main__":
    unittest.main()
