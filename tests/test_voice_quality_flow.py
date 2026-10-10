import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cache_fingerprint import separator_choice_fingerprint_paths, voice_quality_fingerprint_paths
from core.job_spec import digest, freeze, validate
from core.storage import CorruptJobError
from core.voice_quality_flow import apply_voice_quality, completed_choice, format_quality_report


def _parameters():
    return dict(
        clean_preset="light", master_preset="natural", rvc_model_key="demo", pitch_shift=0,
        f0_method="harvest", index_rate=0.65, generate_midi=False, output_formats=["wav"],
        output_sample_rate=48000, bpm=120,
    )


def _profile(index_rate=0.4):
    return {"pitch": 0, "index_rate": index_rate, "protect": 0.33, "filter_radius": 3, "rms_mix_rate": 0.25}


class VoiceQualityFlowTests(unittest.TestCase):
    def test_default_records_the_plan_and_does_not_infer_again(self):
        with tempfile.TemporaryDirectory() as folder:
            primary = Path(folder) / "vocal.wav"
            primary.write_bytes(b"primary")
            calls = []

            def infer(payload):
                calls.append(payload["axis"])
                return {"failed": False, "artifact": str(primary)}

            job = {"pitch_shift": 0, "index_rate": 0.65,
                   "effective_f0": {"requested": "harvest", "executed": "harvest"}}
            record = apply_voice_quality(
                {}, job, _profile(), str(primary),
                {"clip_ratio": 0.2, "reason": "启发式指标，不是音质真值"},
                {"status": "F0_UNSTABLE"}, infer=infer,
            )
        self.assertEqual(calls, [])
        self.assertEqual(record["extra_inference_count"], 0)
        self.assertEqual(record["candidate_plan_status"], "READY")
        self.assertFalse(record["executed"])
        self.assertTrue(record["stopped"])
        self.assertEqual(record["kept_path"], record["rollback_path"])
        self.assertFalse(record["quality_truth"])
        self.assertEqual(record["listening_quality"], "not_assessed")
        self.assertFalse(record["blocks_job"])
        self.assertIn("默认关闭", record["reason"])
        self.assertIn("index_rate", [row["axis"] for row in record["candidates"]])
        text = format_quality_report(record)
        self.assertIn("音质真值：否", text)
        self.assertEqual(text.count("## 音质流程"), 1)

    def test_opt_in_retries_once_and_a_second_failure_keeps_the_first_file(self):
        with tempfile.TemporaryDirectory() as folder:
            primary = Path(folder) / "vocal.wav"
            second = Path(folder) / "assist.wav"
            primary.write_bytes(b"primary")
            calls = []

            def fail(payload):
                calls.append(payload["axis"])
                return {"failed": True, "artifact": str(second)}

            enabled = {"quality_assist": True, "quality_retry_limit": 1}
            job = {"pitch_shift": 7, "index_rate": 0.65,
                   "effective_f0": {"requested": "harvest", "executed": "harvest"}}
            held = apply_voice_quality(
                enabled, job, _profile(), str(primary),
                {"clip_ratio": 0.1}, {"status": "不可观测"}, infer=fail,
            )
            self.assertEqual(calls, ["index_rate"])
            self.assertEqual(held["kept_path"], str(primary))
            self.assertEqual(held["rollback_path"], str(primary))
            self.assertEqual(held["extra_inference_count"], 1)
            self.assertTrue(primary.read_bytes() == b"primary")
            calls.clear()
            second.write_bytes(b"better")

            def succeed(payload):
                calls.append(payload["attempt"])
                return {"failed": False, "artifact": str(second)}

            replaced = apply_voice_quality(
                enabled, job, _profile(), str(primary),
                {"clip_ratio": 0}, {"status": "F0_UNSTABLE"}, infer=succeed,
                suggestion={"applied": False, "semitones": 12},
            )
            self.assertEqual(calls, [2])
            self.assertEqual(replaced["kept_path"], str(second))
            self.assertEqual(replaced["rollback_path"], str(primary))
            self.assertTrue(primary.is_file())
            limit_only = apply_voice_quality(
                {"quality_retry_limit": 1}, job, _profile(), str(primary),
                {"clip_ratio": 1}, {"status": "F0_UNSTABLE"}, infer=succeed,
            )
            self.assertIn("音质辅助关闭", limit_only["reason"])
            self.assertEqual(limit_only["extra_inference_count"], 0)

    def test_completed_retry_is_reused_and_job_spec_defaults_stay_off(self):
        with tempfile.TemporaryDirectory() as folder:
            primary = Path(folder) / "vocal.wav"
            primary.write_bytes(b"primary")
            record = apply_voice_quality(
                {"quality_assist": True, "quality_retry_limit": 1},
                {"pitch_shift": 0, "index_rate": 0.65,
                 "effective_f0": {"requested": "harvest", "executed": "harvest"}},
                _profile(0.65), str(primary),
                {"clip_ratio": 0}, {"status": "不可观测"}, infer=lambda payload: payload,
            )
            from core.voice_quality_flow import write_quality_sidecars
            record["executed"] = True
            record["kept_path"] = str(primary)
            write_quality_sidecars(str(primary), record)
            self.assertEqual(completed_choice(str(primary), {"quality_assist": True, "quality_retry_limit": 1})["kept_path"], str(primary))
            self.assertIsNone(completed_choice(str(primary), {}))
            pth = Path(folder) / "model.pth"
            pth.write_bytes(b"weights")
            model = dict(
                name="demo", pth=str(pth), index=None,
                profile=dict(protect=0.33, rms_mix_rate=0.25, filter_radius=3),
            )
            with patch("core.job_spec.get_available_rvc_models", return_value={"demo": model}):
                spec = freeze(_parameters(), "voice_conversion")
                polish = freeze(_parameters(), "polish")
                legacy = copy.deepcopy(spec)
                del legacy["quality_assist"]
                del legacy["separator_backend"]
                legacy["spec_hash"] = digest(legacy)
                restored = validate(legacy, "voice_conversion")
                bad = copy.deepcopy(spec)
                bad["separator_backend"] = "mdx"
                bad["spec_hash"] = digest(bad)
                with self.assertRaises(CorruptJobError):
                    validate(bad, "voice_conversion")
                assisted = _parameters()
                assisted["quality_assist"] = True
                chosen = freeze(assisted, "voice_conversion")
        self.assertFalse(spec["quality_assist"])
        self.assertEqual(spec["separator_backend"], "hp2")
        self.assertEqual(spec["quality_retry_limit"], 0)
        self.assertNotIn("quality_assist", polish)
        self.assertNotIn("quality_assist", restored)
        self.assertTrue(chosen["quality_assist"])
        self.assertEqual(spec["schema_version"], 1)
        root = Path("workspace")
        self.assertEqual(voice_quality_fingerprint_paths(root, {}), [])
        self.assertEqual(voice_quality_fingerprint_paths(root, {"quality_assist": True}),
                         [root / "core" / name for name in ("voice_quality_flow.py", "quality_retry.py",
                          "parameter_search.py", "pitch_suggestion.py", "representative_clips.py")])
        self.assertEqual(separator_choice_fingerprint_paths(root, {"separator_backend": "hp2"}), [])
        pipeline = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn("apply_voice_quality", pipeline)
        self.assertIn('"f0_method": executed_f0', pipeline)
        self.assertIn('"effective_f0": frozen_f0', pipeline)
        self.assertNotIn("explicit_search", pipeline)
        self.assertNotIn("one_axis_candidates", pipeline)
        self.assertNotIn("run_quality_attempts", pipeline)
        fingerprint = (ROOT / "core" / "cache_fingerprint.py").read_text(encoding="utf-8")
        self.assertIn("quality_retry.py", fingerprint)
        self.assertIn("stage_contract_version=2", fingerprint)

    def test_partial_default_job_without_frozen_f0_keeps_primary_without_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            primary = Path(folder) / "vocal.wav"
            primary.write_bytes(b"primary")
            calls = []
            record = apply_voice_quality(
                {}, {"pitch_shift": 0, "index_rate": .65}, _profile(), str(primary),
                {"clip_ratio": .2}, {"status": "F0_UNSTABLE"},
                infer=lambda payload: calls.append(payload),
            )
        self.assertEqual(record["candidate_plan_status"], "INCOMPLETE")
        self.assertEqual(record["candidates"], [])
        self.assertFalse(record["executed"])
        self.assertFalse(record["blocks_job"])
        self.assertEqual(calls, [])

    def test_enabled_quality_assist_requires_frozen_job_f0(self):
        with tempfile.TemporaryDirectory() as folder:
            primary = Path(folder) / "vocal.wav"
            primary.write_bytes(b"primary")
            with self.assertRaisesRegex(ValueError, "JobSpec.*F0"):
                apply_voice_quality(
                    {"quality_assist": True, "quality_retry_limit": 1},
                    {"pitch_shift": 0, "index_rate": .65}, _profile(), str(primary),
                    {"clip_ratio": .2}, {"status": "F0_UNSTABLE"},
                    infer=lambda payload: {"failed": False, "artifact": str(primary)},
                )


if __name__ == "__main__":
    unittest.main()
