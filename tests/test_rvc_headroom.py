import copy
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.analyzer import probe_metadata
from core.cache_fingerprint import rvc_headroom_fingerprint_paths
from core.job_spec import CorruptJobError, digest, freeze, validate
from core.qc import sample_metrics
from core.rvc_headroom import (
    DEFAULT_RVC_INPUT_GAIN_DB,
    OBSERVED_RVC_INPUT_PEAK,
    TARGET_PEAK_DBFS,
    apply_rvc_input_gain,
    resolve_rvc_input_headroom,
    resolve_rvc_input_gain_db,
    validate_rvc_input_gain_db,
)

RECORD = ROOT / "tests" / "fixtures" / "voice_chain_vcq003_headroom.json"
CLEANED = (
    ROOT / "output" / "acceptance" / "audit_repair"
    / "验收 中文短样本_20261004_192142_144337" / "stems" / "02_vocal_cleaned.wav"
)


def _parameters():
    return dict(
        clean_preset="light", master_preset="natural", rvc_model_key="demo",
        pitch_shift=0, f0_method="harvest", index_rate=0.65, generate_midi=False,
        output_formats=["wav"], output_sample_rate=48000, bpm=120,
    )


class RvcHeadroomTests(unittest.TestCase):
    def test_auto_peak_gain_uses_each_actual_input(self):
        landed = OBSERVED_RVC_INPUT_PEAK * 10 ** (DEFAULT_RVC_INPUT_GAIN_DB / 20.0)
        self.assertAlmostEqual(20 * math.log10(landed), TARGET_PEAK_DBFS, places=6)
        self.assertLess(DEFAULT_RVC_INPUT_GAIN_DB, 0)
        self.assertGreater(DEFAULT_RVC_INPUT_GAIN_DB, -12)
        self.assertEqual(validate_rvc_input_gain_db(0), 0.0)
        with self.assertRaises(ValueError):
            validate_rvc_input_gain_db(-12.01)
        with self.assertRaises(ValueError):
            validate_rvc_input_gain_db(True)

    def test_full_scale_peak_drops_without_silence(self):
        samples = np.zeros(480, np.float32)
        samples[0] = np.float32(1.0)
        samples[1] = np.float32(-1.0)
        with tempfile.TemporaryDirectory(prefix="headroom-") as folder:
            source = os.path.join(folder, "full.wav")
            gained = os.path.join(folder, "gained.wav")
            identity = os.path.join(folder, "identity.wav")
            sf.write(source, samples, 48000, subtype="FLOAT")
            apply_rvc_input_gain(source, gained, DEFAULT_RVC_INPUT_GAIN_DB)
            apply_rvc_input_gain(source, identity, 0)
            before = sample_metrics(source)
            after = sample_metrics(gained)
            unchanged = sample_metrics(identity)
            again = os.path.join(folder, "again.wav")
            apply_rvc_input_gain(source, again, DEFAULT_RVC_INPUT_GAIN_DB)
            self.assertEqual(sample_metrics(again), after)
        self.assertEqual(before["sample_peak"], 1.0)
        self.assertGreater(before["clipped_samples"], 0)
        self.assertLess(after["sample_peak"], before["sample_peak"])
        self.assertEqual(after["clipped_samples"], 0)
        self.assertGreater(max(after["channel_rms"]), 1e-3)
        self.assertEqual(unchanged["sample_peak"], before["sample_peak"])
        self.assertEqual(unchanged["channel_rms"], before["channel_rms"])

    def test_legacy_spec_keeps_zero_gain_and_new_spec_records_default(self):
        folder = tempfile.TemporaryDirectory(prefix="headroom-spec-")
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        pth = root / "model.pth"
        pth.write_bytes(b"original-model")
        model = dict(name="demo", pth=str(pth), index=None, profile=dict(protect=0.33, filter_radius=3, rms_mix_rate=0.25))
        with mock.patch("core.job_spec.get_available_rvc_models", return_value={"demo": model}):
            spec = freeze(_parameters(), "voice_conversion")
            polish = freeze(_parameters(), "polish")
        self.assertEqual(spec["schema_version"], 1)
        self.assertEqual(spec["rvc_input_headroom_strategy"], "auto_peak")
        self.assertNotIn("rvc_input_gain_db", spec)
        explicit_zero = dict(_parameters(), rvc_input_gain_db=0)
        explicit_gain = dict(_parameters(), rvc_input_gain_db=-6.0)
        with mock.patch("core.job_spec.get_available_rvc_models", return_value={"demo": model}):
            zero_spec = freeze(explicit_zero, "voice_conversion")
            gain_spec = freeze(explicit_gain, "voice_conversion")
        self.assertEqual(zero_spec["rvc_input_headroom_strategy"], "fixed")
        self.assertEqual(zero_spec["rvc_input_gain_db"], 0)
        self.assertEqual(gain_spec["rvc_input_headroom_strategy"], "fixed")
        self.assertEqual(gain_spec["rvc_input_gain_db"], -6.0)
        self.assertNotIn("rvc_input_gain_db", polish)
        legacy = copy.deepcopy(spec)
        del legacy["rvc_input_headroom_strategy"]
        legacy["rvc_input_gain_db"] = DEFAULT_RVC_INPUT_GAIN_DB
        legacy["spec_hash"] = digest(legacy)
        restored = validate(legacy, "voice_conversion")
        self.assertEqual(restored["rvc_input_gain_db"], DEFAULT_RVC_INPUT_GAIN_DB)
        self.assertEqual(resolve_rvc_input_gain_db(restored), DEFAULT_RVC_INPUT_GAIN_DB)
        self.assertEqual(resolve_rvc_input_headroom(restored)["strategy"], "legacy_fixed")
        self.assertEqual(resolve_rvc_input_gain_db({}), 0.0)
        bad = copy.deepcopy(spec)
        bad["rvc_input_gain_db"] = 3
        bad["spec_hash"] = digest(bad)
        with self.assertRaises(CorruptJobError):
            validate(bad, "voice_conversion")

    def test_fingerprint_includes_module_only_when_gain_is_used(self):
        root = Path("workspace")
        self.assertEqual(rvc_headroom_fingerprint_paths(root, {}), [])
        self.assertEqual(rvc_headroom_fingerprint_paths(root, {"rvc_input_gain_db": 0}), [])
        self.assertEqual(
            rvc_headroom_fingerprint_paths(root, {"rvc_input_headroom_strategy": "auto_peak", "rvc_input_gain_db": 0}),
            [root / "core" / "rvc_headroom.py"],
        )
        self.assertEqual(
            rvc_headroom_fingerprint_paths(root, {"rvc_input_headroom_strategy": "fixed", "rvc_input_gain_db": 0}),
            [root / "core" / "rvc_headroom.py"],
        )
        self.assertEqual(
            rvc_headroom_fingerprint_paths(root, {"rvc_input_gain_db": DEFAULT_RVC_INPUT_GAIN_DB}),
            [root / "core" / "rvc_headroom.py"],
        )
        text = (ROOT / "core" / "cache_fingerprint.py").read_text(encoding="utf-8")
        self.assertIn("stage_contract_version=2", text)

    def test_short_cleaned_vocal_input_is_lower_and_recorded(self):
        self.assertTrue(CLEANED.is_file(), "短样本的清理人声不在磁盘上")
        record = load_observation_record(RECORD)
        self.assertEqual(record["listening_quality"], "not_assessed")
        self.assertEqual(record["gain_db"], DEFAULT_RVC_INPUT_GAIN_DB)
        before = sample_metrics(str(CLEANED))
        self.assertEqual(before["sample_peak"], OBSERVED_RVC_INPUT_PEAK)
        self.assertEqual(before["clipped_samples"], 0)
        with tempfile.TemporaryDirectory(prefix="headroom-short-") as folder:
            dest = os.path.join(folder, "05_rvc_input.wav")
            apply_rvc_input_gain(str(CLEANED), dest, DEFAULT_RVC_INPUT_GAIN_DB)
            after = sample_metrics(dest)
            self.assertEqual(probe_metadata(dest)["duration"], probe_metadata(str(CLEANED))["duration"])
        self.assertLess(after["sample_peak"], before["sample_peak"])
        self.assertGreater(max(after["channel_rms"]), 1e-3)
        self.assertEqual(after["clipped_samples"], 0)
        for name in ("sample_peak", "channel_rms", "clipped_samples"):
            self.assertEqual(before[name], record["before"][name])
            self.assertEqual(after[name], record["after"][name])
        pipeline = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn("05_rvc_input.wav", pipeline)
        self.assertIn("resolve_rvc_input_headroom", pipeline)
        self.assertIn("headroom_report", pipeline)
        self.assertIn("apply_rvc_input_gain", pipeline)
        self.assertNotIn("rvc_input_gain_db", (ROOT / "core" / "rvc_engine.py").read_text(encoding="utf-8"))
        text = RECORD.read_text(encoding="utf-8")
        self.assertNotIn("听感变好", text)
        self.assertNotIn("improved", text)

    def test_auto_peak_measures_per_song_and_caps_without_boosting(self):
        with tempfile.TemporaryDirectory(prefix="headroom-auto-") as folder:
            quiet = os.path.join(folder, "quiet.wav")
            loud = os.path.join(folder, "loud.wav")
            silent = os.path.join(folder, "silent.wav")
            sf.write(quiet, np.full(480, 0.1, dtype=np.float32), 48000, subtype="FLOAT")
            sf.write(loud, np.full(480, 9.0, dtype=np.float32), 48000, subtype="FLOAT")
            sf.write(silent, np.zeros(480, dtype=np.float32), 48000, subtype="FLOAT")
            parameters = {"rvc_input_headroom_strategy": "auto_peak"}
            quiet_plan = resolve_rvc_input_headroom(parameters, quiet)
            loud_plan = resolve_rvc_input_headroom(parameters, loud)
            silent_plan = resolve_rvc_input_headroom(parameters, silent)
        self.assertEqual(quiet_plan["gain_db"], 0.0)
        self.assertAlmostEqual(loud_plan["gain_db"], -12.0)
        self.assertTrue(loud_plan["gain_limited"])
        self.assertEqual(silent_plan["gain_db"], 0.0)
        self.assertNotEqual(quiet_plan["gain_db"], loud_plan["gain_db"])


def load_observation_record(path):
    import json
    with open(path, "r", encoding="utf-8") as handle:
        record = json.load(handle)
    if record.get("kind") != "rvc_input_headroom":
        raise AssertionError("不是 RVC 输入余量记录")
    return record


if __name__ == "__main__":
    unittest.main()
