import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.job_spec import digest, freeze, validate
from core.parameter_search import bounded_candidates, explicit_search, one_axis_candidates
from core.pitch_suggestion import suggest_pitch
from core.quality_retry import (
    default_inference_count,
    resolve_quality_retry_limit,
    run_quality_attempts,
)
from core.representative_clips import select_clips
from core.storage import CorruptJobError


def _parameters():
    return dict(
        clean_preset="light", master_preset="natural", rvc_model_key="demo", pitch_shift=0,
        f0_method="harvest", index_rate=0.65, generate_midi=False, output_formats=["wav"],
        output_sample_rate=48000, bpm=120,
    )


class ParameterSearchTests(unittest.TestCase):
    def test_one_axis_order_and_fake_inference(self):
        import numpy as np

        model = {"gender": "female", "default_pitch_male": 12, "default_pitch_female": 0}
        suggestion = suggest_pitch(model, "male")
        self.assertEqual(suggestion["semitones"], 12)
        self.assertFalse(suggestion["applied"])
        profile = {
            "pitch": 0, "index_rate": 0.5, "protect": 0.33,
            "rms_mix_rate": 0.25, "filter_radius": 3, "f0_method": "harvest",
        }
        job = {"pitch_shift": 7, "index_rate": 0.65, "f0_method": "harvest"}
        rows = one_axis_candidates(job, profile, suggestion)
        self.assertEqual([row["axis"] for row in rows], ["baseline", "pitch_shift", "index_rate"])
        self.assertEqual(rows[0]["pitch_shift"], 7)
        self.assertEqual(rows[0]["index_rate"], 0.65)
        self.assertEqual(rows[1]["pitch_shift"], 12)
        self.assertEqual(rows[1]["index_rate"], 0.65)
        self.assertEqual(rows[2]["pitch_shift"], 7)
        self.assertEqual(rows[2]["index_rate"], 0.5)
        for row in rows:
            self.assertEqual(row["protect"], 0.33)
            changed = [
                key for key in ("pitch_shift", "index_rate", "protect")
                if row[key] != rows[0][key]
            ]
            if row["axis"] == "baseline":
                self.assertEqual(changed, [])
            else:
                self.assertEqual(changed, [row["axis"]])
        same = one_axis_candidates(
            {"pitch_shift": 0, "index_rate": 0.65, "f0_method": "harvest"},
            {"pitch": 0, "index_rate": 0.65, "protect": 0.33},
            suggest_pitch({"gender": "male", "default_pitch_male": 0, "default_pitch_female": -12}, "male"),
        )
        self.assertEqual(len(same), 1)
        long = np.arange(48000 * 10, dtype=np.float64)
        clips = select_clips(long, 48000, window_seconds=2.0, limit=3)
        calls = []

        def infer(parameters):
            calls.append(dict(parameters))
            return {"id": parameters["axis"]}

        found = explicit_search(job, profile, infer, suggestion=suggestion, clips=clips)
        self.assertEqual([item["parameters"]["axis"] for item in found], ["baseline", "pitch_shift", "index_rate"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(found[0]["clips"]["kind"], "clips")
        self.assertLessEqual(found[0]["clips"]["count"], 3)
        self.assertEqual(found[0]["output"]["id"], "baseline")
        pipeline = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        stage = pipeline.split("def _run_stage", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("bounded_candidates", pipeline)
        self.assertIn('"RVCSearch"', pipeline)
        self.assertIn('"selected_for_job": "baseline"', pipeline)
        self.assertNotIn("parameter_search", stage)
        self.assertNotIn("quality_retry", stage)

    def test_second_failure_keeps_the_first_artifact_and_default_is_one_inference(self):
        calls = []

        def infer(parameters):
            calls.append(parameters["attempt"])
            if parameters["attempt"] == 1:
                return {"failed": True, "artifact": {"id": "first", "peak": 0.2}}
            return {"failed": True, "artifact": {"id": "second", "peak": 0.9}}

        primary = {"attempt": 1, "pitch_shift": 0, "index_rate": 0.65, "protect": 0.33}
        retry = {"attempt": 2, "pitch_shift": 12, "index_rate": 0.65, "protect": 0.33}
        self.assertEqual(default_inference_count({}), 1)
        self.assertEqual(resolve_quality_retry_limit({}), 0)
        self.assertEqual(default_inference_count({"quality_retry_limit": 0}), 1)
        once = run_quality_attempts(primary, retry, infer, 0)
        self.assertEqual(once["inference_count"], 1)
        self.assertEqual(once["kept"]["id"], "first")
        calls.clear()
        twice = run_quality_attempts(primary, retry, infer, 1)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(twice["inference_count"], 2)
        self.assertTrue(twice["stopped"])
        self.assertEqual(twice["kept"]["id"], "first")
        self.assertEqual(twice["reason"], "第二次失败，保留第一次产物")
        self.assertEqual(twice["attempts"][0]["parameters"]["pitch_shift"], 0)
        self.assertEqual(twice["attempts"][1]["parameters"]["pitch_shift"], 12)
        self.assertEqual(len(twice["attempts"]), 2)
        calls.clear()

        def succeed(parameters):
            calls.append(parameters["attempt"])
            return {"failed": False, "artifact": {"id": "ok"}}

        held = run_quality_attempts(primary, retry, succeed, 1)
        self.assertEqual(calls, [1])
        self.assertEqual(held["kept"]["id"], "ok")
        with self.assertRaises(ValueError):
            run_quality_attempts(primary, retry, infer, 2)
        self.assertEqual(calls, [1])
        worker = (ROOT / "core" / "worker_runtime.py").read_text(encoding="utf-8")
        self.assertNotIn("quality_retry", worker)
        self.assertNotIn("run_quality_attempts", worker)
        self.assertIn("cuda_oom", worker)

    def test_job_spec_records_zero_retry_and_old_snapshots_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            pth = Path(folder) / "model.pth"
            pth.write_bytes(b"weights")
            model = dict(
                name="demo", pth=str(pth), index=None,
                profile=dict(protect=0.33, rms_mix_rate=0.25, filter_radius=3),
            )
            with patch("core.job_spec.get_available_rvc_models", return_value={"demo": model}):
                spec = freeze(_parameters(), "voice_conversion")
                polish = freeze(_parameters(), "polish")
                explicit = _parameters()
                explicit["quality_retry_limit"] = 1
                chosen = freeze(explicit, "voice_conversion")
                legacy = copy.deepcopy(spec)
                del legacy["quality_retry_limit"]
                for key in (
                    "parameter_search_enabled", "parameter_search_budget",
                    "vocal_restoration_effect", "rvc_input_headroom_strategy",
                    "source_hint", "source_preclean_enabled", "effective_source_policy",
                ):
                    legacy.pop(key, None)
                legacy["spec_hash"] = digest(legacy)
                restored = validate(legacy, "voice_conversion")
                bad = copy.deepcopy(spec)
                bad["quality_retry_limit"] = 2
                bad["spec_hash"] = digest(bad)
                with self.assertRaises(CorruptJobError):
                    validate(bad, "voice_conversion")
        self.assertEqual(spec["schema_version"], 1)
        self.assertEqual(spec["quality_retry_limit"], 0)
        self.assertFalse(spec["parameter_search_enabled"])
        self.assertEqual(spec["parameter_search_budget"], "fast")
        self.assertNotIn("quality_retry_limit", polish)
        self.assertEqual(chosen["quality_retry_limit"], 1)
        self.assertNotIn("quality_retry_limit", restored)
        self.assertNotIn("parameter_search_enabled", restored)
        self.assertNotIn("vocal_restoration_effect", restored)
        self.assertNotIn("source_preclean_enabled", restored)
        self.assertNotIn("effective_source_policy", restored)
        self.assertEqual(resolve_quality_retry_limit(restored), 0)
        self.assertEqual(default_inference_count(restored), 1)
        self.assertIn("spec['schema_version'] = 1", (ROOT / "core" / "job_spec.py").read_text(encoding="utf-8"))
        fingerprint = (ROOT / "core" / "cache_fingerprint.py").read_text(encoding="utf-8")
        self.assertIn("stage_contract_version=2", fingerprint)
        self.assertIn("quality_retry.py", fingerprint)

    def test_bounded_plan_preserves_frozen_f0_and_caps_alternates(self):
        frozen_f0 = {"requested": "rmvpe", "executed": "rmvpe", "fallback": False}
        job = {"pitch_shift": 3, "index_rate": 0.65, "f0_method": "rmvpe", "effective_f0": frozen_f0}
        profile = {"pitch": 0, "index_rate": 0.4, "protect": 0.33}
        plan = bounded_candidates(job, profile, budget="balanced")
        self.assertLessEqual(len(plan["candidates"]), 3)
        self.assertEqual(plan["baseline"]["effective_f0"], frozen_f0)
        self.assertEqual(plan["baseline"]["f0_method"], "rmvpe")
        for candidate in plan["candidates"]:
            self.assertEqual(candidate["f0_method"], "rmvpe")
            self.assertEqual(candidate["effective_f0"], frozen_f0)
            changed = [key for key in ("pitch_shift", "index_rate", "protect") if candidate[key] != plan["baseline"][key]]
            self.assertEqual(changed, [candidate["axis"]])


if __name__ == "__main__":
    unittest.main()
