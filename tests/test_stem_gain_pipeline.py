import copy
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.job_spec import (
    CorruptJobError,
    digest,
    freeze,
    validate_mix_gain_db,
    validate_transcribe_stem,
    validate,
)
from core.pipeline import AudioPipeline


class StemGainPipelineTests(unittest.TestCase):
    def _transcribe_parameters(self):
        return {
            "clean_preset": "light",
            "master_preset": "natural",
            "rvc_model_key": "unused",
            "pitch_shift": 0,
            "f0_method": "harvest",
            "index_rate": 0.65,
            "generate_midi": False,
            "output_formats": ["wav"],
            "output_sample_rate": 48000,
            "bpm": 120,
        }

    def test_gain_and_stem_validation_is_strict(self):
        self.assertEqual(validate_mix_gain_db(0.5, "vocal"), 0.5)
        self.assertEqual(validate_mix_gain_db(-6, "inst"), -6)
        for value in (True, False, float("nan"), float("inf"), -6.01, 6.01, "0.5", 10**1000):
            with self.assertRaises(ValueError):
                validate_mix_gain_db(value, "gain")
        for stem in ("vocal", "bass", "other"):
            self.assertEqual(validate_transcribe_stem(stem), stem)
        for stem in ("drums", "", None, True):
            with self.assertRaises(ValueError):
                validate_transcribe_stem(stem)

    def test_new_specs_freeze_effective_defaults_and_custom_values(self):
        defaults = freeze(self._transcribe_parameters(), "transcribe")
        self.assertEqual(defaults["transcribe_stem"], "vocal")
        custom = freeze(dict(self._transcribe_parameters(), transcribe_stem="bass"), "transcribe")
        self.assertEqual(custom["transcribe_stem"], "bass")
        self.assertEqual(custom["spec_hash"], digest(custom))
        with self.assertRaises(CorruptJobError):
            broken = copy.deepcopy(custom)
            broken["transcribe_stem"] = "drums"
            broken["spec_hash"] = digest(broken)
            validate(broken, "transcribe")

    def test_legacy_missing_fields_validate_without_hash_mutation(self):
        current = freeze(self._transcribe_parameters(), "transcribe")
        legacy = copy.deepcopy(current)
        legacy.pop("transcribe_stem")
        legacy["spec_hash"] = digest(legacy)
        original = copy.deepcopy(legacy)
        self.assertIs(validate(legacy, "transcribe"), legacy)
        self.assertEqual(legacy, original)
        self.assertNotIn("transcribe_stem", legacy)

    def test_transcribe_stem_requires_an_actual_separate_output(self):
        with tempfile.TemporaryDirectory(prefix="stem-selection-") as folder:
            root = Path(folder)
            vocal = root / "vocal.wav"
            bass = root / "bass.wav"
            vocal.write_bytes(b"vocal")
            bass.write_bytes(b"bass")
            self.assertEqual(
                AudioPipeline._select_transcribe_stem(
                    {"vocal": str(vocal), "bass": str(bass)}, "bass"
                ),
                str(bass.resolve()),
            )
            with self.assertRaisesRegex(RuntimeError, "真实分轨缺失"):
                AudioPipeline._select_transcribe_stem(
                    {"vocal": str(vocal), "other": str(vocal)}, "bass"
                )

    def test_mix_cache_parameters_keep_effective_gain_values(self):
        parameters = AudioPipeline._cache_parameters(
            "Mix",
            {
                "reverb": False,
                "mix_vocal_gain_db": 1.25,
                "mix_instrumental_gain_db": -2.5,
            },
        )
        self.assertEqual(parameters["mix_vocal_gain_db"], 1.25)
        self.assertEqual(parameters["mix_instrumental_gain_db"], -2.5)
        self.assertTrue(math.isfinite(parameters["mix_vocal_gain_db"]))


if __name__ == "__main__":
    unittest.main()
