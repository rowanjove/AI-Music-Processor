import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.waveform_preview import waveform_envelope


class WaveformPreviewTests(unittest.TestCase):
    def test_bounded_preview_decodes_audio_to_finite_envelope(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tone.wav"
            rate = 48000
            time = np.arange(rate, dtype=np.float32) / rate
            audio = np.column_stack((0.5 * np.sin(2 * np.pi * 220 * time),
                                     0.25 * np.sin(2 * np.pi * 440 * time)))
            sf.write(path, audio, rate, subtype="FLOAT")
            result = waveform_envelope(path, buckets=128)
            self.assertEqual(result["sample_rate"], 1000)
            self.assertAlmostEqual(result["duration_seconds"], 1.0, delta=.01)
            self.assertEqual(len(result["points"]), 128)
            self.assertGreater(result["peak"], .2)
            self.assertTrue(all(-1 <= low <= high <= 1 for low, high in result["points"]))
            self.assertEqual(result["quality_acceptance"], "NOT_ASSESSED")

    def test_duration_and_bucket_limits_fail_before_decode(self):
        with self.assertRaises(ValueError):
            waveform_envelope("missing.wav", buckets=8)


if __name__ == "__main__":
    unittest.main()
