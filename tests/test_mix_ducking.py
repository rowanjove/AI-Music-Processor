import json
import sys
import tempfile
import unittest
import copy
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.mix_ducking import ALGORITHM_VERSION, PROFILE, apply_vocal_ducking
from core.cache_fingerprint import fingerprints
from core.job_spec import digest, freeze, validate
from core.pipeline import AudioPipeline
from core.separator_routing import hp2_profile
from core.storage import JobStore
from core.ux_flow import mode_stages


class MixDuckingTests(unittest.TestCase):
    def test_vocal_sidechain_generates_float_accompaniment_and_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            rate = 48000
            time = np.arange(rate * 2, dtype=np.float32) / rate
            vocal = root / "vocal.wav"
            instrumental = root / "instrumental.wav"
            output = root / "ducked.wav"
            report = root / "reports" / "ducking.json"
            sf.write(vocal, np.column_stack((.25*np.sin(2*np.pi*220*time),)*2), rate, subtype="FLOAT")
            sf.write(instrumental, np.column_stack((.3*np.sin(2*np.pi*440*time),)*2), rate, subtype="FLOAT")
            result = apply_vocal_ducking(vocal, instrumental, output, report)
            info = sf.info(output)
            self.assertEqual(info.samplerate, 48000)
            self.assertEqual(info.channels, 2)
            self.assertEqual(info.subtype, "FLOAT")
            self.assertAlmostEqual(info.duration, 2.0, delta=.02)
            with report.open(encoding="utf-8") as handle:
                recorded = json.load(handle)
            self.assertEqual(recorded["algorithm"], ALGORITHM_VERSION)
            self.assertEqual(recorded["settings"], PROFILE)
            self.assertEqual(recorded["output_sha256"], result["report"]["output_sha256"])
            self.assertEqual(recorded["quality_acceptance"], "NOT_ASSESSED")

    def test_ducking_never_overwrites_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "vocal.wav"
            source.write_bytes(b"input")
            with self.assertRaises(ValueError):
                apply_vocal_ducking(source, source, Path(folder) / "out.wav")

    def test_disabled_new_job_records_skipped_stage_and_keeps_mix_route(self):
        self.assertIn("Ducking", mode_stages("voice_conversion", mix_ducking=False))
        self.assertNotIn("Ducking", mode_stages("voice_conversion"))
        with tempfile.TemporaryDirectory() as folder, JobStore(Path(folder) / "state.db") as store:
            root = Path(folder)
            source = root / "input.wav"
            source.write_bytes(b"audio")
            job_id = store.create_job(str(source), "voice_conversion", project_dir=folder)
            pipeline = AudioPipeline(storage=store, log_callback=lambda _message: None)
            with store.execution(job_id):
                result = pipeline._record_skipped_stage(
                    job_id, "Ducking", {"vocal": str(source), "instrumental": str(source)},
                    {"enabled": False}, "用户在任务参数中关闭了 Ducking", .83,
                )
                pipeline._record_skipped_stage(
                    job_id, "Ducking", {"vocal": str(source), "instrumental": str(source)},
                    {"enabled": False}, "用户在任务参数中关闭了 Ducking", .83,
                )
            stage = store.get_stage(job_id, "Ducking")
            self.assertTrue(result["skipped"])
            self.assertEqual(stage["status"], "SKIPPED")
            self.assertEqual(stage["parameters"]["skip_reason"], "用户在任务参数中关闭了 Ducking")
            self.assertEqual(stage["outputs"], {})
            self.assertEqual(stage["attempt"], 1)

    def test_voice_job_freezes_switch_and_old_snapshot_without_it_stays_legacy(self):
        with tempfile.TemporaryDirectory() as folder:
            model_path = Path(folder) / "model.pth"
            model_path.write_bytes(b"weights")
            model = {"name": "demo", "pth": str(model_path), "index": None,
                     "profile": {"protect": .33, "rms_mix_rate": .25, "filter_radius": 3}}
            parameters = {
                "clean_preset": "light", "master_preset": "natural", "rvc_model_key": "demo",
                "pitch_shift": 0, "f0_method": "harvest", "index_rate": .65,
                "generate_midi": False, "output_formats": ["wav"], "output_sample_rate": 48000,
                "bpm": 120, "separator_backend": "hp2", "separator_profile": hp2_profile(),
            }
            with patch("core.job_spec.get_available_rvc_models", return_value={"demo": model}):
                default = freeze(parameters, "voice_conversion")
                enabled = freeze(dict(parameters, mix_ducking=True), "voice_conversion")
                legacy = copy.deepcopy(default)
                del legacy["mix_ducking"]
                legacy["spec_hash"] = digest(legacy)
                restored = validate(legacy, "voice_conversion")
            self.assertFalse(default["mix_ducking"])
            self.assertTrue(enabled["mix_ducking"])
            self.assertNotIn("mix_ducking", restored)

    def test_ducking_stage_fingerprints_its_actual_processor(self):
        dependencies = fingerprints("Ducking", {})
        self.assertIn(str((ROOT / "core" / "mix_ducking.py").resolve()), dependencies)


if __name__ == "__main__":
    unittest.main()
