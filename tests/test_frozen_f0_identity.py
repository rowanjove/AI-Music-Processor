import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.job_spec import freeze, validate
from core.storage import CorruptJobError


class FrozenF0IdentityTests(unittest.TestCase):
    def test_weight_change_rejects_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            model_path = Path(folder) / "model.pth"
            model_path.write_bytes(b"voice model")
            f0_path = Path(folder) / "rmvpe.pt"
            f0_path.write_bytes(b"pitch model")
            model = {"pth": str(model_path), "index": None,
                     "profile": {"protect": .33, "filter_radius": 3, "rms_mix_rate": .25}}
            parameters = dict(clean_preset="light", master_preset="natural", rvc_model_key="demo",
                pitch_shift=0, f0_method="rmvpe", index_rate=.65, generate_midi=False,
                output_formats=["wav"], output_sample_rate=48000, bpm=120)
            decision = {"requested": "rmvpe", "executed": "rmvpe", "fallback": False, "reason": ""}
            with patch("core.job_spec.get_available_rvc_models", return_value={"demo": model}), \
                 patch("core.f0_method.resolve_f0_method", return_value=decision), \
                 patch("core.f0_method.weight_path", return_value=str(f0_path)):
                frozen = freeze(parameters, "voice_conversion")
            self.assertEqual(frozen["effective_f0"]["executed"], "rmvpe")
            self.assertIs(validate(frozen, "voice_conversion"), frozen)
            f0_path.write_bytes(b"changed pitch model")
            with self.assertRaises(CorruptJobError):
                validate(frozen, "voice_conversion")


if __name__ == "__main__":
    unittest.main()
