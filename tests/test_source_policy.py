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

from core.source_policy import apply_source_preclean, freeze_source_policy, measure_source_facts, preprocess_policy


FACTS = {
    "sample_rate": 48000,
    "sample_peak": 0.85,
    "clipped_samples": 0,
    "channels": 2,
    "dc_offset": [0.0, 0.0],
    "peak_bounds": [[-0.85, 0.85], [-0.85, 0.85]],
    "spectral_rolloff_hz": 12000.0,
}


class SourcePolicyTests(unittest.TestCase):
    def test_source_hint_is_provenance_and_opt_in_defaults_off(self):
        suno = preprocess_policy(FACTS, "suno")
        studio = preprocess_policy(FACTS, "studio_master")
        self.assertEqual(suno["recommendation"], "opt_in_disabled")
        self.assertEqual(studio["recommendation"], suno["recommendation"])
        self.assertEqual(studio["reasons"], suno["reasons"])
        self.assertFalse(suno["applied"])
        self.assertFalse(suno["changes_job"])
        self.assertEqual(suno["pre_separator_clean"], "none")
        self.assertEqual(suno["quality_acceptance"], "NOT_ASSESSED")
        self.assertIn("reverb", suno["not_measured"])
        self.assertEqual(freeze_source_policy("suno", False)["enabled"], False)

    def test_bounded_dc_correction_requires_measurements_and_headroom(self):
        facts = dict(FACTS, dc_offset=[0.035, -0.025])
        applied = preprocess_policy(facts, "suno", enabled=True)
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["action"], "bounded_dc_offset_subtraction")
        self.assertEqual(applied["dc_correction"], [0.035, -0.025])
        self.assertEqual(applied["pre_separator_clean"], "dc_offset")

        clipped = preprocess_policy(dict(facts, clipped_samples=1, sample_peak=1.0), "auto", enabled=True)
        self.assertEqual(clipped["recommendation"], "review_clipping_no_edit")
        self.assertFalse(clipped["applied"])

        little_headroom = preprocess_policy(dict(facts, peak_bounds=[[-0.98, 0.98], [-0.98, 0.98]]), "auto", enabled=True)
        self.assertEqual(little_headroom["recommendation"], "review_headroom_no_edit")
        self.assertFalse(little_headroom["applied"])

        low_rolloff = preprocess_policy(dict(FACTS, spectral_rolloff_hz=6000.0), "mp3", enabled=True)
        self.assertEqual(low_rolloff["recommendation"], "no_safe_preclean_action")
        self.assertIn("95% 频谱滚降低于 8 kHz，仅提示复核", low_rolloff["reasons"])

    def test_real_float_audio_is_measured_and_duration_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "source.wav")
            output = os.path.join(folder, "cleaned.wav")
            report_path = os.path.join(folder, "reports", "source_policy.json")
            rate = 48000
            timeline = np.arange(rate, dtype=np.float32) / rate
            wave = np.column_stack((0.3 * np.sin(2 * np.pi * 440 * timeline) + 0.035,
                                    0.3 * np.sin(2 * np.pi * 440 * timeline) - 0.025)).astype(np.float32)
            sf.write(source, wave, rate, subtype="FLOAT")
            facts = measure_source_facts(source)
            self.assertEqual(facts["frames"], rate)
            self.assertEqual(facts["channels"], 2)
            self.assertAlmostEqual(facts["dc_offset"][0], 0.035, places=4)
            record = apply_source_preclean(source, output, report_path, "suno", enabled=True)
            self.assertTrue(record["applied"])
            self.assertTrue(record["duration_preserved"])
            self.assertEqual(record["output_facts"]["frames"], rate)
            self.assertLess(max(abs(value) for value in record["output_facts"]["dc_offset"]), 0.001)
            self.assertTrue(os.path.isfile(report_path))
            self.assertNotEqual(record["source_sha256"], record["output_sha256"])

    def test_invalid_inputs_fail_closed(self):
        with self.assertRaises(ValueError):
            preprocess_policy(FACTS, "if_suno", enabled=True)
        invalid_rate = preprocess_policy(dict(FACTS, sample_rate="48000"), "auto", enabled=True)
        self.assertFalse(invalid_rate["applied"])
        self.assertEqual(invalid_rate["recommendation"], "review_invalid_measurement_no_edit")
        invalid_clips = preprocess_policy(dict(FACTS, clipped_samples="0"), "auto", enabled=True)
        self.assertFalse(invalid_clips["applied"])
        invalid_dc = preprocess_policy(dict(FACTS, dc_offset=[float("nan"), 0.0]), "studio_master", enabled=True)
        self.assertFalse(invalid_dc["applied"])
        self.assertIn("直流偏移无效", invalid_dc["reasons"])
        missing = preprocess_policy({"sample_rate": 48000}, "mp3", enabled=True)
        self.assertEqual(missing["recommendation"], "insufficient_measurement")


if __name__ == "__main__":
    unittest.main()
