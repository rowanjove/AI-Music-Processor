import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.reporting import build_candidate_comparison_summary, format_candidate_comparison_report
from core.separator_choice import compare_separator_metrics


class CandidateComparisonTests(unittest.TestCase):
    def test_comparison_without_reference_marks_sdr_not_measurable(self):
        cands = [
            {
                "candidate_id": "cand_a",
                "source_id": "source_1",
                "stage_records": [{"stage_name": "Separate"}, {"stage_name": "RVC"}],
                "metrics": {"sdr": None},
                "rvc_metrics": {"f0_validity_ratio": 0.95, "clipped_samples": 0, "residual_ms": 4.2},
                "model_identity": "rvc_v2_clean",
            },
            {
                "candidate_id": "cand_b",
                "source_id": "source_1",
                "stage_records": [{"stage_name": "Separate"}, {"stage_name": "RVC"}],
                "metrics": {"sdr": None},
                "rvc_metrics": {"f0_validity_ratio": 0.98, "clipped_samples": 0, "residual_ms": 3.1},
                "model_identity": "rvc_v2_opt",
            },
        ]
        summary = build_candidate_comparison_summary(
            cands,
            winner_id="cand_b",
            selection_reason="更高音高覆盖率且低残余误差",
            reference_stem_available=False,
            user_reviewed=False,
        )
        self.assertTrue(summary["fair_comparison"])
        self.assertFalse(summary["reference_stem_available"])
        self.assertEqual(summary["listening_quality"], "not_assessed")
        self.assertEqual(summary["quality_acceptance"], "READY_FOR_EXTERNAL_VALIDATION")
        self.assertIn("NOT_MEASURABLE", summary["candidate_rows"][0]["separation_sdr_status"])

        report = format_candidate_comparison_report(summary)
        self.assertIn("NOT_MEASURABLE", report)
        self.assertIn("not_assessed", report)
        self.assertIn("cand_b", report)

    def test_comparison_with_reference_reports_sdr(self):
        cands = [
            {
                "candidate_id": "cand_ref",
                "source_id": "source_1",
                "stage_records": [{"stage_name": "Separate"}],
                "metrics": {"sdr": 14.2},
                "rvc_metrics": {"f0_validity_ratio": 1.0, "clipped_samples": 0, "residual_ms": 0.5},
                "model_identity": "demucs_ht",
            }
        ]
        summary = build_candidate_comparison_summary(
            cands,
            winner_id="cand_ref",
            selection_reason="基准测量",
            reference_stem_available=True,
            user_reviewed=True,
        )
        self.assertTrue(summary["reference_stem_available"])
        self.assertEqual(summary["listening_quality"], "assessed")
        self.assertEqual(summary["candidate_rows"][0]["separation_sdr"], 14.2)

        report = format_candidate_comparison_report(summary)
        self.assertIn("14.2", report)

    def test_separator_choice_metrics_honesty(self):
        res = compare_separator_metrics(
            "src_1",
            {"source_id": "src_1", "backend": "hp2", "peak": 0.8, "duration": 3.0, "measured": True},
            {"source_id": "src_1", "backend": "kim", "peak": 0.85, "duration": 3.0, "measured": True},
        )
        self.assertIn("NOT_MEASURABLE", res["metrics"]["sdr"])


if __name__ == "__main__":
    unittest.main()
