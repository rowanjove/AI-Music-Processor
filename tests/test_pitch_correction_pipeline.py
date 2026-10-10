import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cache_fingerprint import enrich
from core.job_spec import CorruptJobError, digest, freeze, validate
from core.ux_flow import mode_stages


def _parameters():
    return dict(
        clean_preset="light", master_preset="natural", rvc_model_key="demo",
        pitch_shift=0, f0_method="harvest", index_rate=0.65, generate_midi=False,
        output_formats=["wav"], output_sample_rate=48000, bpm=120,
        separator_backend="hp2",
    )


def _model(folder):
    pth = Path(folder) / "model.pth"
    pth.write_bytes(b"model")
    return {"demo": {
        "name": "demo", "pth": str(pth), "index": None,
        "profile": {"protect": .33, "filter_radius": 3, "rms_mix_rate": .25},
    }}


class PitchCorrectionPipelineTests(unittest.TestCase):
    def test_stage_graph_places_opt_in_correction_before_rvc(self):
        stages = mode_stages("voice_conversion", vocal_restore=True, pitch_correction=True)
        self.assertLess(stages.index("VocalRestore"), stages.index("PitchCorrection"))
        self.assertLess(stages.index("PitchCorrection"), stages.index("RVC"))
        self.assertNotIn("PitchCorrection", mode_stages("voice_conversion"))

    def test_job_spec_freezes_explicit_mode_and_key_but_legacy_stays_absent(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("core.job_spec.get_available_rvc_models", return_value=_model(folder)):
                default = freeze(_parameters(), "voice_conversion")
                chosen = freeze(dict(_parameters(), pitch_correction_mode="medium",
                                     pitch_correction_key="Bb minor"), "voice_conversion")
            legacy = copy.deepcopy(chosen)
            legacy.pop("pitch_correction_mode")
            legacy.pop("pitch_correction_key")
            legacy["spec_hash"] = digest(legacy)
            restored = validate(legacy, "voice_conversion")
            self.assertNotIn("pitch_correction_mode", restored)
            self.assertNotIn("pitch_correction_key", restored)
        self.assertEqual(default["pitch_correction_mode"], "off")
        self.assertIsNone(default["pitch_correction_key"])
        self.assertEqual(chosen["pitch_correction_mode"], "medium")
        self.assertEqual(chosen["pitch_correction_key"], "A# minor")

    def test_invalid_pitch_settings_are_rejected_and_stage_fingerprint_is_frozen(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("core.job_spec.get_available_rvc_models", return_value=_model(folder)):
                with self.assertRaises(CorruptJobError):
                    freeze(dict(_parameters(), pitch_correction_mode="auto"), "voice_conversion")
                frozen = freeze(dict(_parameters(), pitch_correction_mode="strong",
                                     pitch_correction_key="C major"), "voice_conversion")
            changed = copy.deepcopy(frozen)
            changed["pitch_correction_key"] = "invalid"
            changed["spec_hash"] = digest(changed)
            with self.assertRaises(CorruptJobError):
                validate(changed, "voice_conversion")
        parameters = {"mode": "strong", "key": "C major"}
        dependencies = enrich("PitchCorrection", parameters)["dependency_hashes"]
        self.assertIn(str((ROOT / "core" / "pitch_correction.py").resolve()), dependencies)


if __name__ == "__main__":
    unittest.main()
