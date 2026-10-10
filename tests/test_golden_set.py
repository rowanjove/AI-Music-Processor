import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.golden_set import coverage, expected_axes, register_case


class GoldenSetTests(unittest.TestCase):
    def test_missing_audio_cannot_become_a_case(self):
        with tempfile.TemporaryDirectory() as folder:
            empty = os.path.join(folder, "empty.wav")
            open(empty, "wb").close()
            with self.assertRaises(ValueError):
                register_case("missing", {"source": os.path.join(folder, "no.wav")})
            with self.assertRaises(ValueError):
                register_case("empty", {"source": empty})
            ready = os.path.join(folder, "ready.wav")
            with wave.open(ready, "w") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(8000)
                handle.writeframes(b"\x00\x00" * 8)
            with self.assertRaises(ValueError) as caught:
                register_case("listen", {"source": ready}, listening_passed=True)
            self.assertIn("听审", str(caught.exception))
            with self.assertRaises(ValueError) as unknown:
                register_case("typo", {"souce": ready})
            self.assertIn("未知", str(unknown.exception))

    def test_a_real_file_stays_unaccepted_and_unclassified_coverage_is_not_run(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "source.wav")
            with wave.open(path, "w") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(8000)
                handle.writeframes(b"\x00\x00" * 8)
            case = register_case("short", {"source": path, "master": None}, source_kind="suno", gender_pair="female_to_female", singing="pop")
        self.assertEqual(case["roles"]["source"]["status"], "PRESENT")
        self.assertEqual(case["roles"]["rvc_raw"]["status"], "NOT_PRESENT")
        self.assertEqual(case["roles"]["master"]["status"], "NOT_PRESENT")
        self.assertFalse(case["after_created"])
        self.assertEqual(case["quality_acceptance"], "NOT_ASSESSED")
        self.assertEqual(case["listening_quality"], "not_assessed")
        report = coverage([case, {"source_kind": "unclassified", "quality_acceptance": "NOT_ASSESSED"}])
        self.assertEqual(report["status"], "PARTIAL_NOT_ACCEPTED")
        self.assertEqual(report["classified_cases"], 1)
        self.assertEqual(report["listening_passed"], 0)
        self.assertEqual(report["filled"]["source_kind"], ["suno"])
        empty = coverage([])
        self.assertEqual(empty["status"], "NOT_RUN")
        self.assertEqual(empty["classified_cases"], 0)
        axes = expected_axes()
        self.assertIn("live", axes["source_kind"])
        self.assertNotIn("unclassified", axes["source_kind"])
        with self.assertRaises(ValueError):
            coverage([{"source_kind": "suno", "quality_acceptance": "LISTENING_PASSED"}])

    def test_generate_synthetic_audio_fixtures(self):
        from core.golden_set import SYNTHETIC_KINDS, generate_synthetic_audio
        with tempfile.TemporaryDirectory() as folder:
            for kind in SYNTHETIC_KINDS:
                target = os.path.join(folder, f"{kind}.wav")
                res = generate_synthetic_audio(kind, target, duration_seconds=0.1, sample_rate=48000)
                self.assertTrue(os.path.isfile(target))
                self.assertGreater(res["sha256"], "")
                self.assertIn(res["channels"], (1, 2))
                self.assertEqual(res["sample_rate"], 48000)
            with self.assertRaises(ValueError):
                generate_synthetic_audio("invalid_kind", os.path.join(folder, "invalid.wav"))

    def test_create_golden_spec_and_verification(self):
        from core.golden_set import create_golden_spec, verify_golden_run
        spec = create_golden_spec(
            case_id="case-01",
            fixture_kind="silence",
            expected_metrics={"peak": 0.0, "clipped_samples": 0.0, "duration": 2.0},
            params={"mode": "polish"},
            execution_stages=["Clean", "Master"],
        )
        self.assertEqual(spec["listening_quality"], "not_assessed")
        self.assertEqual(spec["quality_acceptance"], "READY_FOR_EXTERNAL_VALIDATION")

        # In-tolerance run
        measured_good = {"peak": 0.005, "clipped_samples": 0.0, "duration": 2.01}
        stages_good = [{"stage_name": "Clean"}, {"stage_name": "Master"}]
        res_pass = verify_golden_run(spec, measured_good, stages_good)
        self.assertTrue(res_pass["passed"])
        self.assertEqual(res_pass["status"], "PASS")
        self.assertEqual(res_pass["stage_integrity"], "INTACT")

        # Exceeded peak tolerance
        measured_bad_peak = {"peak": 0.05, "clipped_samples": 0.0, "duration": 2.0}
        res_fail = verify_golden_run(spec, measured_bad_peak, stages_good)
        self.assertFalse(res_fail["passed"])
        self.assertEqual(res_fail["status"], "FAIL")
        self.assertFalse(res_fail["deviations"]["peak"]["passed"])

        # Stage degraded
        stages_bad = [{"stage_name": "Clean"}]
        res_degraded = verify_golden_run(spec, measured_good, stages_bad)
        self.assertFalse(res_degraded["passed"])
        self.assertEqual(res_degraded["status"], "FAIL")
        self.assertEqual(res_degraded["stage_integrity"], "DEGRADED")
