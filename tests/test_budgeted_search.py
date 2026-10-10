import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.budgeted_search import budget_limit, f0_search_status, pitch_candidates, run_budgeted_search
from core.f0_method import resolve_f0_method


class BudgetedSearchTests(unittest.TestCase):
    def test_budgets_are_capped_and_repeatable(self):
        self.assertEqual(budget_limit("fast"), 1)
        self.assertEqual(budget_limit("balanced"), 3)
        self.assertEqual(budget_limit("quality"), 6)
        self.assertEqual(budget_limit("expert", 4), 4)
        with self.assertRaises(ValueError):
            budget_limit("expert", 7)
        with self.assertRaises(ValueError):
            budget_limit("unlimited")
        first = pitch_candidates(0, 3, f0_method="harvest")
        second = pitch_candidates(0, 3, f0_method="harvest")
        self.assertEqual(first, second)
        self.assertEqual([row["pitch_shift"] for row in first], [0, -1, 1])
        self.assertEqual(first[0]["axis"], "baseline")
        self.assertEqual(first[1]["axis"], "pitch_shift")
        self.assertTrue(first[1]["index_rate_unchanged"])
        self.assertEqual(first[1]["f0_method"], "harvest")
        edge = pitch_candidates(24, 3, f0_method="harvest")
        self.assertEqual([row["pitch_shift"] for row in edge], [24, 23, 22])
        frozen = {"requested": "rmvpe", "executed": "rmvpe", "fallback": False}
        rmvpe = pitch_candidates(0, 2, f0_method="rmvpe", effective_f0=frozen)
        self.assertEqual(rmvpe[1]["f0_method"], "rmvpe")
        self.assertEqual(rmvpe[1]["effective_f0"], frozen)

    def test_time_budget_stops_and_does_not_promote(self):
        calls = []

        def infer(candidate, timeout_seconds):
            calls.append(candidate["pitch_shift"])
            return {"peak": 0.2}

        times = iter([0, 0, 0, 30, 30, 30])
        result = run_budgeted_search(pitch_candidates(0, 3, f0_method="harvest"), infer, 10, clock=lambda: next(times))
        self.assertEqual(calls, [0])
        self.assertEqual(result["executed_count"], 1)
        self.assertEqual(result["attempted_count"], 1)
        self.assertEqual(result["planned_count"], 3)
        self.assertFalse(result["promoted"])
        self.assertEqual(result["selected_for_job"], "baseline")
        self.assertTrue(result["heuristic_only"])
        self.assertIn("max_gpu_seconds", result["stopped_reason"])
        self.assertEqual(result["quality_acceptance"], "NOT_ASSESSED")

    def test_one_inference_gets_remaining_budget_and_timeout_stops_search(self):
        calls = []
        times = iter([0, 0, 0, 1, 5, 5, 5, 12, 12])

        def infer(candidate, timeout_seconds):
            calls.append(timeout_seconds)
            if len(calls) == 1:
                return {"peak": 0.2}
            raise TimeoutError("candidate worker exceeded timeout")

        result = run_budgeted_search(pitch_candidates(0, 3, f0_method="harvest"), infer, 10, clock=lambda: next(times))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], 10)
        self.assertEqual(calls[1], 5)
        self.assertEqual(result["executed_count"], 1)
        self.assertEqual(result["attempted_count"], 2)
        self.assertFalse(result["results"][1]["completed"])
        self.assertIn("单次候选推理", result["stopped_reason"])

    def test_rmvpe_search_does_not_call_harvest(self):
        with mock.patch("core.f0_method.resolve_f0_method", side_effect=AssertionError("must not run")):
            status = f0_search_status()
        self.assertEqual(status["requested_f0"], "rmvpe")
        self.assertIsNone(status["actual_f0"])
        self.assertFalse(status["executed"])
        self.assertEqual(status["inference_status"], "NOT_RUN")
        self.assertEqual(resolve_f0_method("rmvpe")["executed"], "rmvpe")

    def test_recorded_pitch_search_is_not_promoted(self):
        fixture = json.loads((ROOT / "tests" / "fixtures" / "budgeted_search_vcq027.json").read_text(encoding="utf-8"))
        self.assertEqual(fixture["executed_count"], 3)
        self.assertEqual(fixture["eligible_windows"], 1)
        self.assertFalse(fixture["promoted"])
        self.assertEqual(fixture["selected_for_job"], "baseline")
        self.assertIsNone(fixture["actual_f0"])
        self.assertEqual(fixture["f0_inference_status"], "NOT_RUN")
        self.assertEqual(fixture["quality_acceptance"], "NOT_ASSESSED")
        self.assertTrue(fixture["heuristic_only"])
        self.assertEqual([row["pitch_shift"] for row in fixture["runs"]], [0, -1, 1])
        self.assertEqual(fixture["runs"][0]["sample_peak"], 0.631256103515625)
        self.assertEqual(fixture["runs"][1]["sample_peak"], 0.654388427734375)
        self.assertEqual(fixture["runs"][2]["sample_peak"], 0.742340087890625)
        self.assertEqual(fixture["runs"][0]["channels"], 1)
        self.assertEqual(fixture["runs"][0]["duration_seconds"], 7.98)
        for row in fixture["runs"]:
            self.assertEqual(row["clipped_samples"], 0)
