import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cache_fingerprint import fingerprints
from core.job_spec import CorruptJobError, digest, freeze, validate
from core.vocal_restore_profile import apply_vocal_restore_profile, normalize_profile, plan_profile
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


class VocalRestoreProfileTests(unittest.TestCase):
    def test_profile_modes_and_expert_bounds_are_frozen(self):
        light = normalize_profile("light")
        strong = normalize_profile("strong")
        self.assertEqual(light["highpass_hz"], 60.0)
        self.assertEqual((light["deesser_intensity"], light["deesser_max_reduction"]), (.08, .15))
        self.assertEqual(strong["highpass_hz"], 75.0)
        self.assertEqual((strong["deesser_intensity"], strong["deesser_max_reduction"]), (.15, .30))
        expert = normalize_profile({"mode": "expert", "highpass_enabled": True,
                                    "highpass_hz": 120, "deesser_enabled": True,
                                    "deesser_intensity": .2, "deesser_max_reduction": .4,
                                    "normalize_enabled": True, "normalize_peak": .95})
        self.assertEqual(expert["mode"], "expert")
        for bad in (
            {"mode": "expert", "highpass_enabled": True, "highpass_hz": 121},
            {"mode": "expert", "deesser_enabled": True, "deesser_intensity": .21},
            {"mode": "expert", "normalize_enabled": True, "normalize_peak": .49},
        ):
            with self.assertRaises(ValueError):
                normalize_profile(bad)

    def test_auto_is_measurement_driven_and_normalization_only_attenuates(self):
        measurement = {"peak": .5, "low_20_60_hz_energy_ratio": .151}
        plan = plan_profile("auto", measurement)
        self.assertEqual(plan["applied"]["highpass"]["frequency_hz"], 60.0)
        self.assertIsNone(plan["applied"]["deesser"])
        loud = plan_profile("auto", {"peak": 1.1, "low_20_60_hz_energy_ratio": 0.0})
        self.assertLessEqual(loud["applied"]["normalization"]["linear_gain"], 1.0)
        self.assertEqual(loud["applied"]["normalization"]["linear_gain"], .8 / 1.1)
        self.assertTrue(loud["applied"]["normalization"]["attenuation_only"])
        quiet = plan_profile("auto", {"peak": .5, "low_20_60_hz_energy_ratio": .15})
        self.assertTrue(quiet["skip"])
        self.assertFalse(quiet["applied"]["highpass"])

    def test_actual_light_profile_preserves_float_audio_shape_and_writes_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            rate = 48000
            time = np.arange(rate, dtype=np.float32) / rate
            source = root / "source.wav"
            output = root / "restored.wav"
            report = root / "reports" / "vocal_restore.json"
            samples = np.column_stack((.7 * np.sin(2*np.pi*100*time) + .12*np.sin(2*np.pi*7000*time),
                                       .7 * np.sin(2*np.pi*100*time) + .12*np.sin(2*np.pi*7000*time)))
            sf.write(source, samples, rate, subtype="FLOAT")
            result = apply_vocal_restore_profile(source, output, "light", report)
            info = sf.info(output)
            self.assertFalse(result["skipped"])
            self.assertEqual(info.samplerate, rate)
            self.assertEqual(info.channels, 2)
            self.assertEqual(info.subtype, "FLOAT")
            self.assertAlmostEqual(info.duration, 1.0, delta=.01)
            with report.open(encoding="utf-8") as stream:
                recorded = json.load(stream)
            self.assertEqual(recorded["decision"], "APPLIED")
            self.assertIsNotNone(recorded["applied"]["highpass"])
            self.assertIsNotNone(recorded["applied"]["deesser"])
            self.assertEqual(recorded["quality_assessment"], "NOT_ASSESSED")

    def test_auto_noop_is_reported_without_copying_or_claiming_quality(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            rate = 48000
            time = np.arange(rate // 2, dtype=np.float32) / rate
            source = root / "source.wav"
            output = root / "unused.wav"
            report = root / "reports" / "auto.json"
            sf.write(source, np.column_stack((.2*np.sin(2*np.pi*440*time),)*2), rate, subtype="FLOAT")
            result = apply_vocal_restore_profile(source, output, "auto", report)
            self.assertTrue(result["skipped"])
            self.assertEqual(result["output"], str(source.resolve()))
            self.assertFalse(output.exists())
            self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["decision"], "SKIPPED")

    def test_new_jobs_default_to_auto_but_legacy_snapshots_remain_absent(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("core.job_spec.get_available_rvc_models", return_value=_model(folder)):
                default = freeze(_parameters(), "voice_conversion")
                explicit = freeze(dict(_parameters(), vocal_restore_profile={"mode": "off"}), "voice_conversion")
            legacy = copy.deepcopy(default)
            legacy.pop("vocal_restore_profile")
            legacy["spec_hash"] = digest(legacy)
            restored = validate(legacy, "voice_conversion")
            self.assertNotIn("vocal_restore_profile", restored)
            self.assertEqual(default["vocal_restore_profile"]["mode"], "auto")
            self.assertEqual(explicit["vocal_restore_profile"]["mode"], "off")
            damaged = copy.deepcopy(explicit)
            damaged["vocal_restore_profile"]["highpass_hz"] = 500
            damaged["spec_hash"] = digest(damaged)
            with self.assertRaises(CorruptJobError):
                validate(damaged, "voice_conversion")

    def test_stage_graph_and_fingerprint_follow_the_actual_processor(self):
        self.assertIn("VocalRestore", mode_stages("voice_conversion", vocal_restore=True))
        self.assertNotIn("VocalRestore", mode_stages("voice_conversion"))
        dependencies = fingerprints("VocalRestore", {})
        self.assertIn(str((ROOT / "core" / "vocal_restore_profile.py").resolve()), dependencies)


if __name__ == "__main__":
    unittest.main()
