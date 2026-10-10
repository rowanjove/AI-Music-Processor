import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.f0_method import PRESENT_REASON, resolve_f0_method, rmvpe_acceptance
from core.separator import resolve_voice_backend
from core.separator_choice import compare_separator_metrics, resolve_separator_request, separator_acceptance


class ExternalAcceptanceTests(unittest.TestCase):
    def test_rmvpe_weight_execution_and_listening_acceptance_are_distinct(self):
        decision = rmvpe_acceptance(ROOT / "tools" / "RVC" / "RVC-beta-v2-0618")
        self.assertEqual(decision["status"], "READY_FOR_EXTERNAL_VALIDATION")
        self.assertTrue(decision["implemented"])
        self.assertTrue(decision["runnable"])
        self.assertTrue(decision["weight_present"])
        self.assertTrue(decision["weight_verified"])
        self.assertIsNone(decision["golden_after"])
        self.assertEqual(decision["listening"], "待人工听审")
        self.assertIn("短样本 Golden after", decision["missing_evidence"])
        with tempfile.TemporaryDirectory() as folder:
            weight = os.path.join(folder, "rmvpe.pt")
            with open(weight, "wb") as handle:
                handle.write(b"not-a-network")
            present = rmvpe_acceptance(folder)
            resolved = resolve_f0_method("rmvpe", root=folder)
        self.assertTrue(present["weight_present"])
        self.assertFalse(present["runnable"])
        self.assertEqual(resolved["executed"], "rmvpe")
        self.assertFalse(resolved["fallback"])
        from core.rmvpe_adapter import load_rmvpe
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            weight = Path(folder) / 'rmvpe.pt'
            weight.write_bytes(b'present but unauthenticated pickle')
            with mock.patch('core.vendor.rmvpe.RMVPE') as native_loader:
                with self.assertRaisesRegex(ValueError, 'SHA-256'):
                    load_rmvpe(weight)
                native_loader.assert_not_called()
        vendor = (ROOT / "tools" / "RVC" / "RVC-beta-v2-0618" / "vc_infer_pipeline.py").read_text(encoding="utf-8")
        self.assertNotIn("rmvpe", vendor)

    def test_separator_switch_rejects_unaudited_backends_and_does_not_invent_metrics(self):
        chosen = resolve_separator_request()
        self.assertEqual(chosen, resolve_voice_backend())
        self.assertEqual(chosen["model_name"], "HP2_all_vocals.pth")
        self.assertEqual(chosen["agg"], 10)
        for backend in ("mdx", "roformer", "other"):
            with self.assertRaises(ValueError):
                resolve_separator_request(backend)
        acceptance = separator_acceptance()
        self.assertEqual(acceptance["status"], "READY_FOR_EXTERNAL_VALIDATION")
        self.assertEqual(acceptance["selectable"], ["hp2"])
        self.assertIsNone(acceptance["same_source_metrics"])
        self.assertFalse(acceptance["runnable_alternates"])
        missing = compare_separator_metrics("clip", {"measured": True}, None)
        self.assertIsNone(missing["metrics"])
        self.assertFalse(missing["metrics_are_audited_separator"])
        supplied = compare_separator_metrics(
            "clip",
            {"measured": True, "audited": False, "source_id": "clip", "backend": "hp2", "peak": 0.5, "duration": 1.0},
            {"measured": True, "audited": False, "source_id": "clip", "backend": "mdx", "peak": 0.25, "duration": 1.0},
        )
        self.assertEqual(supplied["metrics"]["peak_delta"], -0.25)
        self.assertFalse(supplied["metrics_are_audited_separator"])
        self.assertEqual(supplied["status"], "READY_FOR_EXTERNAL_VALIDATION")
        self.assertEqual(supplied["listening_quality"], "not_assessed")


if __name__ == "__main__":
    unittest.main()
