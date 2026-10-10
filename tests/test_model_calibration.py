import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_available_rvc_models
from core.model_calibration import calibration_status, promote_profile


class ModelCalibrationTests(unittest.TestCase):
    def test_search_results_cannot_be_saved(self):
        proposal = {"index_rate": 0.35, "protect": 0.2, "rms_mix_rate": 0.1, "filter_radius": 1}
        with self.assertRaises(ValueError) as denied:
            promote_profile("zhoujielun", proposal)
        self.assertIn("不能写成", str(denied.exception))
        with self.assertRaises(ValueError) as flagged:
            promote_profile("zhoujielun", proposal, listening_passed=True)
        self.assertIn("听审", str(flagged.exception))

    def test_installed_models_stay_uncalibrated(self):
        models = get_available_rvc_models()
        self.assertIn("zhoujielun", models)
        for key in ("zhoujielun", "sunyanzi", "zhaolusi"):
            status = calibration_status(key, models[key])
            self.assertTrue(status["profile_matches_global_default"])
            self.assertFalse(status["override_present"])
            self.assertFalse(status["calibrated"])
            self.assertIsNone(status["proposed_profile"])
            self.assertEqual(status["calibration_status"], "NOT_ASSESSED")
            self.assertEqual(status["quality_acceptance"], "NOT_ASSESSED")
