import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.rvc_float_audit import convert_worker_int16, measure_int16_roundtrip


class FloatPathTests(unittest.TestCase):
    def test_worker_scale_stays_default_and_error_is_measured(self):
        import numpy as np

        raw = np.array([0, 16384, -32768, 32767], dtype=np.int16)
        converted = convert_worker_int16(raw)
        self.assertEqual(float(converted[0]), 0.0)
        self.assertAlmostEqual(float(converted[1]), 0.5, places=6)
        self.assertEqual(float(converted[2]), -1.0)
        self.assertAlmostEqual(float(converted[3]), 32767 / 32768.0, places=6)
        sample_rate = 48000
        time = np.arange(sample_rate, dtype=np.float64) / sample_rate
        tone = 0.3 * np.sin(2.0 * math.pi * 440.0 * time)
        measured = measure_int16_roundtrip(tone)
        self.assertEqual(measured["default_path"], "int16_div_32768")
        self.assertFalse(measured["switched"])
        self.assertEqual(measured["listening_quality"], "not_assessed")
        self.assertLessEqual(measured["max_abs_error"], 1.0 / 32768.0)
        self.assertGreater(measured["max_abs_error"], 0.0)
        self.assertLess(measured["experimental_max_abs_error"], 1e-6)
        worker = (ROOT / "core" / "rvc_worker.py").read_text(encoding="utf-8")
        self.assertIn("if result.dtype == np.int16:", worker)
        self.assertIn("result.astype(np.float32) / 32768.", worker)
        self.assertNotIn("/ 32767.", worker)
        module = (ROOT / "core" / "rvc_float_audit.py").read_text(encoding="utf-8")
        self.assertIn("switched", module)
        self.assertNotIn("import torch", module)
        self.assertIn("spec['schema_version'] = 1", (ROOT / "core" / "job_spec.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
